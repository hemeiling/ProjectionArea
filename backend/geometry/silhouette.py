"""Strategy V2 — gap-closed silhouette via scan conversion.

Used only when :mod:`backend.geometry.contours` cannot close the profile from
the native linework. CONSTITUTION.md §30: it is reported as a distinct method
with lower confidence, never substituted silently.

What this is and is not
-----------------------
This is **not** "rasterize the PDF and rediscover the lines with computer
vision", which §5 forbids. The input is the *already extracted, already
classified* vector segments. Scan-converting known geometry into a bitmap is a
standard way to run topological queries (gap closing, containment) that are
brittle in floating-point exact arithmetic. The bitmap is a scratch pad, not a
source of geometry.

Method
------
Segments are drawn one pixel wide, a morphological closing bridges breaks up to
the closure tolerance, and the contour tree is read with the same nesting-parity
rule used by the exact path: contours at odd depth bound solid regions, contours
at even depth of two or more are holes. Working from the *inner* boundary of a
one-pixel stroke biases every edge inward by half a pixel, so the result is
grown by exactly that much before it is returned.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from typing import List, Optional, Sequence, Tuple

import cv2
import numpy as np
from shapely.geometry import Polygon
from shapely.geometry.base import BaseGeometry

from backend.config import SilhouetteSettings
from backend.geometry.polygons import ensure_valid, union_polygons
from backend.geometry.segments import Segment
from backend.models import BBox, Repair


@dataclass
class ScanFrame:
    """Mapping between a PDF-unit box and the pixel grid covering it."""

    origin: Tuple[float, float]
    pixel_size: float          # PDF units per pixel
    width: int
    height: int

    def to_pixels(self, x: float, y: float) -> Tuple[int, int]:
        return (
            int(round((x - self.origin[0]) / self.pixel_size)),
            int(round((y - self.origin[1]) / self.pixel_size)),
        )

    def to_units(self, px: float, py: float) -> Tuple[float, float]:
        return (
            self.origin[0] + (px + 0.5) * self.pixel_size,
            self.origin[1] + (py + 0.5) * self.pixel_size,
        )


def make_frame(bbox: BBox, settings: SilhouetteSettings, margin_px: int = 4) -> ScanFrame:
    """Choose a pixel grid for ``bbox``, capped so memory cannot run away."""
    longest = max(bbox.width, bbox.height, 1e-6)
    target = min(settings.target_pixels, settings.max_pixels)
    pixel_size = longest / float(target)
    width = int(math.ceil(bbox.width / pixel_size)) + 2 * margin_px
    height = int(math.ceil(bbox.height / pixel_size)) + 2 * margin_px
    origin = (bbox.x0 - margin_px * pixel_size, bbox.y0 - margin_px * pixel_size)
    return ScanFrame(origin=origin, pixel_size=pixel_size, width=width, height=height)


def rasterize_segments(segments: Sequence[Segment], frame: ScanFrame) -> np.ndarray:
    """Draw segments one pixel wide into a binary mask."""
    mask = np.zeros((frame.height, frame.width), dtype=np.uint8)
    for a, b in segments:
        p0 = frame.to_pixels(*a)
        p1 = frame.to_pixels(*b)
        cv2.line(mask, p0, p1, color=255, thickness=1, lineType=cv2.LINE_8)
    return mask


def close_gaps(mask: np.ndarray, closure_units: float, frame: ScanFrame) -> Tuple[np.ndarray, int]:
    """Morphologically close breaks up to ``closure_units`` PDF units.

    Returns:
        ``(closed_mask, kernel_pixels)``. A kernel of one means no closing was
        applied, because the tolerance was below one pixel.
    """
    kernel_px = int(round(closure_units / max(frame.pixel_size, 1e-9)))
    if kernel_px < 2:
        return mask, 1
    kernel_px = min(kernel_px, 31)
    if kernel_px % 2 == 0:
        kernel_px += 1
    kernel = cv2.getStructuringElement(cv2.MORPH_ELLIPSE, (kernel_px, kernel_px))
    return cv2.morphologyEx(mask, cv2.MORPH_CLOSE, kernel), kernel_px


def _contour_depths(hierarchy: np.ndarray) -> List[int]:
    """Nesting depth of every contour returned by ``RETR_TREE``."""
    flat = hierarchy[0]
    depths = [0] * len(flat)
    for index in range(len(flat)):
        depth, parent = 0, flat[index][3]
        guard = 0
        while parent != -1 and guard < len(flat):
            depth += 1
            parent = flat[parent][3]
            guard += 1
        depths[index] = depth
    return depths


def mask_to_geometry(
    mask: np.ndarray,
    frame: ScanFrame,
    min_polygon_area: float,
    stroke_inset_pixels: float = 0.5,
    simplify_pixels: float = 0.75,
) -> Optional[BaseGeometry]:
    """Read a solid region out of a line mask using contour nesting parity.

    Args:
        mask: Binary image where non-zero pixels are linework.
        frame: The pixel/PDF mapping used to draw it.
        min_polygon_area: Minimum face area in PDF units².
        stroke_inset_pixels: How far the read boundary sits inside the true
            edge, in pixels. The result is grown by this much to compensate.
        simplify_pixels: Douglas-Peucker tolerance for contour simplification,
            in pixels. Keeps vertex counts sane on long organic outlines.

    Returns:
        A Polygon/MultiPolygon in PDF units, or ``None`` if nothing enclosed.
    """
    contours, hierarchy = cv2.findContours(mask, cv2.RETR_TREE, cv2.CHAIN_APPROX_SIMPLE)
    if hierarchy is None or not contours:
        return None

    depths = _contour_depths(hierarchy)
    epsilon = max(simplify_pixels, 0.0)

    solids: List[Polygon] = []
    holes: List[Polygon] = []
    for contour, depth in zip(contours, depths):
        if len(contour) < 3:
            continue
        approx = cv2.approxPolyDP(contour, epsilon, True) if epsilon > 0 else contour
        if len(approx) < 3:
            continue
        ring = [frame.to_units(float(p[0][0]), float(p[0][1])) for p in approx]
        polygon = Polygon(ring)
        if not polygon.is_valid:
            polygon, _ = ensure_valid(polygon)
            if polygon.geom_type not in ("Polygon", "MultiPolygon") or polygon.is_empty:
                continue
            polygon = max(
                [polygon] if polygon.geom_type == "Polygon" else list(polygon.geoms),
                key=lambda p: p.area,
            )
        if polygon.area < min_polygon_area:
            continue
        # Depth 0 is the outside of a stroke; depth 1 is the region a loop
        # encloses; depth 2 is the outside of a stroke drawn inside that loop.
        if depth % 2 == 1:
            solids.append(polygon)
        elif depth >= 2:
            holes.append(polygon)

    if not solids:
        return None

    geometry = union_polygons(solids)
    if geometry is None:
        return None
    hole_geometry = union_polygons(holes)
    if hole_geometry is not None:
        geometry = geometry.difference(hole_geometry)
        geometry, _ = ensure_valid(geometry)

    if stroke_inset_pixels > 0 and not geometry.is_empty:
        # One buffer grows the outer boundary and shrinks every hole by the
        # same half stroke width, correcting both biases at once.
        geometry = geometry.buffer(
            stroke_inset_pixels * frame.pixel_size, join_style=2, mitre_limit=2.0
        )
        geometry, _ = ensure_valid(geometry)

    return None if geometry.is_empty else geometry


def gap_closed_silhouette(
    segments: Sequence[Segment],
    bbox: BBox,
    closure_units: float,
    min_polygon_area: float,
    settings: SilhouetteSettings,
) -> Tuple[Optional[BaseGeometry], List[Repair], List[str]]:
    """Reconstruct a silhouette from linework that would not close exactly.

    Args:
        segments: Cleaned segments, already restricted to the chosen region.
        bbox: Bounding box of that linework, in PDF units.
        closure_units: Largest gap that may be bridged.
        min_polygon_area: Minimum face area in PDF units².
        settings: Scan-conversion resolution settings.

    Returns:
        ``(geometry, repairs, notes)``.
    """
    if not segments:
        return None, [], ["no segments to scan-convert"]

    frame = make_frame(bbox, settings)
    mask = rasterize_segments(segments, frame)
    closed, kernel_px = close_gaps(mask, closure_units, frame)

    repairs: List[Repair] = []
    if kernel_px > 1:
        repairs.append(
            Repair(
                type="morphological_gap_close",
                count=1,
                magnitude_units=round(kernel_px * frame.pixel_size, 4),
                detail=(
                    f"closed breaks up to {kernel_px * frame.pixel_size:.3f} PDF units "
                    f"({kernel_px} px at {frame.pixel_size:.4f} units/px)"
                ),
            )
        )

    geometry = mask_to_geometry(closed, frame, min_polygon_area)
    notes = [
        f"scan-converted {len(segments)} segments at {frame.pixel_size:.4f} PDF units/pixel "
        f"({frame.width}x{frame.height} px)"
    ]
    if geometry is None:
        notes.append("no enclosed region found even after gap closing")
    return geometry, repairs, notes
