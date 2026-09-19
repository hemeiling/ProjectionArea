"""Candidate drawing-region detection.

CONSTITUTION.md §17: a sheet is not a part. Views, sections, details, the BOM
and the title block all share the page, and a projected area must be computed
inside one chosen region.

Detection is deterministic clustering, not inference: profile linework is
stamped onto a coarse occupancy grid, the grid is dilated by the whitespace
gutter a draughtsman leaves between views, and connected components become
candidate regions. Each region is then *labelled* using nearby text — a
"TOP VIEW" caption raises the guess to ``top`` — but the geometry never depends
on that label (§7: AI and text understand, geometry measures).
"""

from __future__ import annotations

import math
from typing import Any, Dict, List, Optional, Sequence, Tuple

import cv2
import numpy as np

from backend.config import RegionSettings
from backend.models import BBox, GeometryRole, Primitive, Region, TextItem

#: Roles that mark where a drawing *is*. Sheet frames are excluded so the border
#: cannot merge every view into one blob; dimensions and annotation are included
#: because they cluster with the view they belong to.
_OCCUPANCY_ROLES = {
    GeometryRole.PROFILE,
    GeometryRole.UNCERTAIN,
    GeometryRole.HATCH,
    GeometryRole.CENTERLINE,
    GeometryRole.HIDDEN,
    GeometryRole.DIMENSION,
}

#: A region is called a title block or table when text dominates this strongly.
_TEXT_DOMINANT_DENSITY = 0.0055
#: ...and its profile ink is this sparse relative to its perimeter.
_SPARSE_INK_RATIO = 2.5
#: A text-dominant block needs at least this many spans to be a table/BOM
#: rather than a small view that happens to carry a caption.
_TABLE_MIN_TEXTS = 8


def _stamp(grid: np.ndarray, bbox: BBox, origin: Tuple[float, float], pitch: float) -> None:
    x0 = int((bbox.x0 - origin[0]) / pitch)
    x1 = int(math.ceil((bbox.x1 - origin[0]) / pitch))
    y0 = int((bbox.y0 - origin[1]) / pitch)
    y1 = int(math.ceil((bbox.y1 - origin[1]) / pitch))
    height, width = grid.shape
    x0, x1 = max(0, x0), min(width, max(x1, x0 + 1))
    y0, y1 = max(0, y0), min(height, max(y1, y0 + 1))
    if x1 > x0 and y1 > y0:
        grid[y0:y1, x0:x1] = 255


def _stamp_polyline(
    grid: np.ndarray, points: Sequence[Tuple[float, float]], origin: Tuple[float, float], pitch: float
) -> None:
    """Stamp the linework itself rather than its bounding box.

    Bounding boxes bridge unrelated views whenever a single long diagonal or a
    leader line crosses the sheet; stamping the actual path does not.
    """
    height, width = grid.shape
    pixels = [
        (
            int((x - origin[0]) / pitch),
            int((y - origin[1]) / pitch),
        )
        for x, y in points
    ]
    for (ax, ay), (bx, by) in zip(pixels, pixels[1:]):
        if max(ax, bx) < 0 or max(ay, by) < 0 or min(ax, bx) >= width or min(ay, by) >= height:
            continue
        cv2.line(grid, (ax, ay), (bx, by), color=255, thickness=1)


def detect_regions(
    primitives: Sequence[Primitive],
    text_items: Sequence[TextItem],
    page_bbox: BBox,
    settings: RegionSettings,
    view_labels: Optional[Sequence[Tuple[str, TextItem]]] = None,
) -> List[Region]:
    """Cluster page content into candidate drawing regions.

    Args:
        primitives: Classified primitives for the page.
        text_items: Text spans, used for labelling and title-block detection.
        page_bbox: The page rectangle in PDF units.
        settings: Clustering parameters.
        view_labels: Detected view captions, from
            :func:`backend.pdf.text.detect_view_labels`.

    Returns:
        Candidate regions ordered by score, best first. Empty when the page
        carries no clusterable linework.
    """
    pitch = max(settings.grid_pitch, 0.5)
    width = max(int(math.ceil(page_bbox.width / pitch)), 1)
    height = max(int(math.ceil(page_bbox.height / pitch)), 1)
    origin = (page_bbox.x0, page_bbox.y0)

    grid = np.zeros((height, width), dtype=np.uint8)
    considered = [p for p in primitives if p.role in _OCCUPANCY_ROLES]
    for prim in considered:
        _stamp_polyline(grid, prim.points, origin, pitch)
    for item in text_items:
        _stamp(grid, item.bbox, origin, pitch)

    if not grid.any():
        return []

    radius = max(int(round(settings.cluster_gap / pitch)), 1)
    kernel = cv2.getStructuringElement(cv2.MORPH_ELLIPSE, (2 * radius + 1, 2 * radius + 1))
    dilated = cv2.dilate(grid, kernel)

    count, labels, stats, _centroids = cv2.connectedComponentsWithStats(dilated, connectivity=8)
    page_area = max(page_bbox.area, 1e-9)

    regions: List[Region] = []
    for label in range(1, count):
        x, y, w, h, _area = stats[label]
        box = BBox(
            origin[0] + x * pitch,
            origin[1] + y * pitch,
            origin[0] + (x + w) * pitch,
            origin[1] + (y + h) * pitch,
        )
        # The dilation inflated the cluster; shrink back to the ink it contains.
        tight = _tighten(box, considered, text_items)
        if tight is None:
            continue
        fraction = tight.area / page_area
        if fraction < settings.min_page_area_fraction or fraction > settings.max_page_area_fraction:
            continue

        inside = [p for p in considered if tight.intersects(p.bbox)]
        texts = [t for t in text_items if tight.intersects(t.bbox)]
        ink = sum(p.length for p in inside)
        profile_ink = sum(p.length for p in inside if p.role is GeometryRole.PROFILE)

        regions.append(
            Region(
                id=f"r{len(regions) + 1}",
                bbox=tight.padded(settings.padding),
                primitive_count=len(inside),
                ink_length=ink,
                text_count=len(texts),
                kind=_classify_region(tight, profile_ink, texts, page_bbox),
                score=profile_ink,
            )
        )

    _label_views(regions, view_labels or [], page_bbox)
    regions.sort(key=lambda r: (-r.score, r.bbox.x0))
    for index, region in enumerate(regions):
        region.id = f"r{index + 1}"
    return regions


def _tighten(
    box: BBox, primitives: Sequence[Primitive], text_items: Sequence[TextItem]
) -> Optional[BBox]:
    """Shrink a dilated cluster box back onto the content it actually holds."""
    boxes = [p.bbox for p in primitives if box.intersects(p.bbox)]
    boxes += [t.bbox for t in text_items if box.intersects(t.bbox)]
    if not boxes:
        return None
    clipped = [
        BBox(max(b.x0, box.x0), max(b.y0, box.y0), min(b.x1, box.x1), min(b.y1, box.y1))
        for b in boxes
    ]
    return BBox.union_of([b for b in clipped if b.width >= 0 and b.height >= 0])


def _classify_region(
    box: BBox, profile_ink: float, texts: Sequence[TextItem], page_bbox: BBox
) -> str:
    """Label a region as a view, a title block, a table or a note block."""
    area = max(box.area, 1e-9)
    text_density = len(texts) / area * 100.0
    perimeter = 2 * (box.width + box.height)
    ink_ratio = profile_ink / max(perimeter, 1e-9)

    near_bottom = box.y1 > page_bbox.y1 - 0.3 * page_bbox.height
    near_right = box.x1 > page_bbox.x1 - 0.4 * page_bbox.width

    sparse = ink_ratio < _SPARSE_INK_RATIO
    if profile_ink <= 0 and texts:
        return "note"
    if near_bottom and near_right and sparse and len(texts) >= 2:
        # Corner + text + rectangular rule work is a title block, whatever its
        # text density; a small view in that corner would carry closed profile
        # ink well above the sparse threshold.
        return "title_block"
    if sparse and text_density >= _TEXT_DOMINANT_DENSITY and len(texts) >= _TABLE_MIN_TEXTS:
        return "table"
    return "view"


def _label_views(
    regions: List[Region], view_labels: Sequence[Tuple[str, TextItem]], page_bbox: BBox
) -> None:
    """Attach caption-derived view guesses, then fall back to layout order.

    Layout inference follows first-angle/third-angle convention only loosely, so
    it is recorded as ``layout_position`` — a hint for the user, never a fact the
    calculation depends on.
    """
    for name, item in view_labels:
        centre = item.bbox.center
        best: Optional[Region] = None
        best_distance = float("inf")
        for region in regions:
            if region.kind != "view":
                continue
            if region.bbox.contains_point(centre):
                best, best_distance = region, 0.0
                break
            rx, ry = region.bbox.center
            distance = math.hypot(rx - centre[0], ry - centre[1])
            if distance < best_distance:
                best, best_distance = region, distance
        if best is not None and best.view_guess == "unknown":
            best.view_guess = name
            best.view_guess_source = "caption"
            best.label = item.text.strip()

    views = [r for r in regions if r.kind == "view"]
    if len(views) >= 2:
        for region in views:
            if region.view_guess != "unknown":
                continue
            region.view_guess_source = "layout_position"
            region.view_guess = "unknown"
    for region in regions:
        if not region.label:
            region.label = {
                "title_block": "Title block",
                "table": "Table / BOM",
                "note": "Notes",
            }.get(region.kind, f"View {region.id.upper()}")


def region_primitives(primitives: Sequence[Primitive], box: BBox) -> List[Primitive]:
    """Primitives whose linework lies inside ``box``.

    Containment is judged on the primitive's bounding box: a path that merely
    clips the region edge (a dimension line running out of the view) is excluded,
    so a selection never drags in half of a neighbouring view.
    """
    selected: List[Primitive] = []
    for prim in primitives:
        pb = prim.bbox
        if pb.x0 >= box.x0 and pb.y0 >= box.y0 and pb.x1 <= box.x1 and pb.y1 <= box.y1:
            selected.append(prim)
    return selected


def detect_regions_from_page_image(
    page,
    page_bbox: BBox,
    settings: RegionSettings,
    dpi: int = 110,
) -> List[Region]:
    """Cluster candidate regions on a page that carries no vector linework.

    Path B needs region selection just as much as Path A — a scanned sheet has a
    title block and several views too. The ink is found by rendering and
    thresholding, then clustered by exactly the same dilate-and-label method, so
    a raster page and a vector page produce comparable regions.

    Args:
        page: A ``fitz.Page``.
        page_bbox: The page rectangle in PDF units.
        settings: Clustering parameters.
        dpi: Render resolution. Deliberately low — this locates views, it does
            not trace them.

    Returns:
        Candidate regions ordered by ink content, best first.
    """
    import fitz

    zoom = dpi / 72.0
    pixmap = page.get_pixmap(matrix=fitz.Matrix(zoom, zoom), alpha=False)
    image = np.frombuffer(pixmap.samples, dtype=np.uint8).reshape(
        pixmap.height, pixmap.width, pixmap.n
    )
    gray = cv2.cvtColor(image, cv2.COLOR_RGB2GRAY) if pixmap.n >= 3 else image[:, :, 0]
    _threshold, ink = cv2.threshold(gray, 0, 255, cv2.THRESH_BINARY_INV | cv2.THRESH_OTSU)

    units_per_pixel = 1.0 / zoom
    radius = max(int(round(settings.cluster_gap / units_per_pixel)), 1)
    kernel = cv2.getStructuringElement(cv2.MORPH_ELLIPSE, (2 * radius + 1, 2 * radius + 1))
    dilated = cv2.dilate(ink, kernel)

    count, labels, stats, _centroids = cv2.connectedComponentsWithStats(dilated, connectivity=8)
    page_area = max(page_bbox.area, 1e-9)

    regions: List[Region] = []
    for label in range(1, count):
        x, y, w, h, _blob_area = stats[label]
        # Tighten onto the actual ink inside the dilated blob.
        component = (labels[y:y + h, x:x + w] == label) & (ink[y:y + h, x:x + w] > 0)
        rows = np.flatnonzero(component.any(axis=1))
        cols = np.flatnonzero(component.any(axis=0))
        if rows.size == 0 or cols.size == 0:
            continue
        box = BBox(
            page_bbox.x0 + (x + int(cols[0])) * units_per_pixel,
            page_bbox.y0 + (y + int(rows[0])) * units_per_pixel,
            page_bbox.x0 + (x + int(cols[-1]) + 1) * units_per_pixel,
            page_bbox.y0 + (y + int(rows[-1]) + 1) * units_per_pixel,
        )
        fraction = box.area / page_area
        if fraction < settings.min_page_area_fraction or fraction > settings.max_page_area_fraction:
            continue

        ink_pixels = int(component.sum())
        regions.append(
            Region(
                id=f"r{len(regions) + 1}",
                bbox=box.padded(settings.padding),
                primitive_count=0,
                ink_length=ink_pixels * units_per_pixel,
                text_count=0,
                kind="view",
                label=f"Region {len(regions) + 1}",
                score=float(ink_pixels),
            )
        )

    regions.sort(key=lambda r: (-r.score, r.bbox.x0))
    for index, region in enumerate(regions):
        region.id = f"r{index + 1}"
        region.label = f"Region {index + 1} (raster)"
    return regions


#: A cluster labelled `title_block` carrying at least this many primitives is
#: more linework than sheet furniture needs, so it may be a view in the corner.
_TITLE_BLOCK_DENSE_PRIMITIVES = 20


def detect_title_block_ambiguity(
    regions: Sequence[Region],
    primitives: Sequence[Primitive],
    page_bbox: BBox,
    measured_label: Optional[str] = None,
) -> Optional[Dict[str, Any]]:
    """Report the known bottom-right / title-block confusion, without fixing it.

    :func:`_classify_region` calls any bottom-right cluster carrying >= 2 text
    spans a title block, and its ``sparse`` ink guard does not discriminate — a
    plain plate scores 1.108 against a threshold of 2.5. On a real layout sheet
    that can hide a genuine view, so two symptoms are worth surfacing:

    * a cluster labelled ``title_block`` that carries a lot of linework;
    * more than one ``title_block`` on one page, which no sheet has.

    This **reports only**. Retuning the classifier needs evidence from real
    drawings, and guessing from shape is what §7 forbids — so the user is told
    what looks wrong and can override the region by hand.

    Args:
        regions: Detected regions.
        primitives: Page primitives, for measuring what sits inside each block.
        page_bbox: The page, for the area share.
        measured_label: Label of the region actually measured, if any.

    Returns:
        A findings dict, or ``None`` when nothing looks ambiguous.
    """
    blocks = [r for r in regions if r.kind == "title_block"]
    if not blocks:
        return None

    findings: List[Dict[str, Any]] = []
    for block in blocks:
        inside = [
            p
            for p in primitives
            if block.bbox.x0 <= p.bbox.center[0] <= block.bbox.x1
            and block.bbox.y0 <= p.bbox.center[1] <= block.bbox.y1
        ]
        ink = sum(p.length for p in inside)
        perimeter = max(2 * (block.bbox.width + block.bbox.height), 1e-9)
        findings.append(
            {
                "region_id": block.id,
                "label": block.label,
                "bbox": block.bbox.as_dict(),
                "primitives_inside": len(inside),
                "ink_units": round(ink, 1),
                "ink_ratio": round(ink / perimeter, 3),
                "area_share_of_page": round(block.bbox.area / max(page_bbox.area, 1e-9), 4),
            }
        )

    dense = [f for f in findings if f["primitives_inside"] >= _TITLE_BLOCK_DENSE_PRIMITIVES]
    if not dense and len(blocks) <= 1:
        return None

    return {
        "kind": "title_block_ambiguity",
        "headline": "Review recommended",
        "reason": (
            "More than one region was classified as a title block."
            if len(blocks) > 1
            else "A dense bottom-right region may have been classified as a title block."
        ),
        "regions": findings,
        "suspect_region_ids": [f["region_id"] for f in dense] or [f["region_id"] for f in findings],
        "measured_region_was_a_title_block": bool(
            measured_label and any(b.label == measured_label for b in blocks)
        ),
        "note": (
            "The classifier's sparse-ink guard does not discriminate here "
            "(threshold 2.5; a plain plate scores 1.108). Reported, not corrected — "
            "select the region manually to override it."
        ),
    }
