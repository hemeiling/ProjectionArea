"""PDF document/page loading and source classification.

CONSTITUTION.md §19: classify before processing. §37: PyMuPDF is confined to
this package so the rest of the engine talks only to the domain model, leaving
room for future DXF/STEP source adapters.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Tuple

import fitz

from backend.config import Tolerances
from backend.models import BBox, DrawingType, Primitive, TextItem
from backend.pdf.primitives import extract_primitives
from backend.progress import NULL_PROGRESS, Progress
from backend.pdf.text import (
    DimensionText,
    detect_scale_ratio,
    detect_sheet_unit,
    detect_view_labels,
    extract_text_items,
    parse_dimension_texts,
)

#: A page needs at least this many real primitives, or this much ink relative to
#: its diagonal, before we call it vector-bearing. Tuned so that a scanned sheet
#: carrying a handful of stamp/border paths is still classified RASTER.
_MIN_VECTOR_PRIMITIVES = 12
_MIN_VECTOR_INK_RATIO = 3.0

#: Raster coverage above this fraction of the page means a real scan/image.
_RASTER_COVERAGE = 0.12


@dataclass
class PageAnalysis:
    """Everything read off a single page, before any geometry decisions.

    This is the *raw* layer of §15 and is cached per page: repeated area
    calculations with different regions or scales never re-parse the PDF.
    """

    page_number: int                      # 1-based
    width: float
    height: float
    rotation: int
    drawing_type: DrawingType
    primitives: List[Primitive]
    text_items: List[TextItem]
    dimension_texts: List[DimensionText]
    tolerances: Tolerances
    image_count: int
    image_coverage: float
    vector_ink: float
    sheet_unit: Optional[str]
    scale_ratio: Optional[Dict[str, Any]]
    view_labels: List[Tuple[str, TextItem]] = field(default_factory=list)
    raw_text: str = ""

    @property
    def page_bbox(self) -> BBox:
        return BBox(0.0, 0.0, self.width, self.height)

    @property
    def diagonal(self) -> float:
        return math.hypot(self.width, self.height)

    def summary(self) -> Dict[str, Any]:
        return {
            "page": self.page_number,
            "width_pt": round(self.width, 2),
            "height_pt": round(self.height, 2),
            "width_mm_paper": round(self.width * 25.4 / 72.0, 1),
            "height_mm_paper": round(self.height * 25.4 / 72.0, 1),
            "rotation": self.rotation,
            "drawing_type": self.drawing_type.value,
            "primitive_count": len(self.primitives),
            "vector_ink_units": round(self.vector_ink, 1),
            "image_count": self.image_count,
            "image_coverage": round(self.image_coverage, 4),
            "text_span_count": len(self.text_items),
            "dimension_text_count": len(self.dimension_texts),
            "sheet_unit": self.sheet_unit,
            "printed_scale": (
                {k: v for k, v in self.scale_ratio.items() if k != "bbox"}
                if self.scale_ratio
                else None
            ),
            "view_labels": [
                {"name": name, "text": item.text.strip(), "bbox": item.bbox.as_dict()}
                for name, item in self.view_labels
            ],
            "tolerances": self.tolerances.as_dict(),
        }


def _image_coverage(page: Any, page_area: float) -> Tuple[int, float]:
    """Fraction of the page covered by raster images.

    Overlapping placements are merged on a coarse grid so a tiled scan is not
    counted several times over.
    """
    rects: List[fitz.Rect] = []
    for info in page.get_images(full=True):
        xref = info[0]
        try:
            for rect in page.get_image_rects(xref):
                rects.append(rect)
        except Exception:
            continue
    if not rects or page_area <= 0:
        return len(rects), 0.0

    cells = 64
    grid = [[False] * cells for _ in range(cells)]
    page_rect = page.rect
    for rect in rects:
        x0 = max(0, int((rect.x0 - page_rect.x0) / max(page_rect.width, 1e-6) * cells))
        x1 = min(cells, int(math.ceil((rect.x1 - page_rect.x0) / max(page_rect.width, 1e-6) * cells)))
        y0 = max(0, int((rect.y0 - page_rect.y0) / max(page_rect.height, 1e-6) * cells))
        y1 = min(cells, int(math.ceil((rect.y1 - page_rect.y0) / max(page_rect.height, 1e-6) * cells)))
        for row in range(y0, y1):
            for col in range(x0, x1):
                grid[row][col] = True
    filled = sum(1 for row in grid for cell in row if cell)
    return len(rects), filled / float(cells * cells)


def classify_page(
    primitives: List[Primitive], vector_ink: float, diagonal: float, image_coverage: float
) -> DrawingType:
    """Decide whether a page is VECTOR, RASTER, MIXED or UNKNOWN. §19."""
    significant = [p for p in primitives if p.length > 0.5]
    has_vector = (
        len(significant) >= _MIN_VECTOR_PRIMITIVES
        or (diagonal > 0 and vector_ink / diagonal >= _MIN_VECTOR_INK_RATIO)
    )
    has_raster = image_coverage >= _RASTER_COVERAGE

    if has_vector and has_raster:
        return DrawingType.MIXED
    if has_vector:
        return DrawingType.VECTOR
    if has_raster:
        return DrawingType.RASTER
    return DrawingType.UNKNOWN


def analyze_page(
    doc: Any, page_number: int, progress: Progress = NULL_PROGRESS
) -> PageAnalysis:
    """Read one page into a :class:`PageAnalysis`.

    Args:
        doc: An open ``fitz.Document``.
        page_number: 1-based page index.
        progress: Optional observer; reading the paths is the slow part.

    Returns:
        The raw, unprocessed reading of the page.

    Raises:
        ValueError: If ``page_number`` is out of range.
    """
    if page_number < 1 or page_number > doc.page_count:
        raise ValueError(f"Page {page_number} out of range; document has {doc.page_count} pages")

    page = doc.load_page(page_number - 1)
    rect = page.rect
    width, height = float(rect.width), float(rect.height)
    tolerances = Tolerances.for_page(width, height)

    primitives = extract_primitives(page, tolerances, progress)
    vector_ink = sum(p.length for p in primitives)
    image_count, coverage = _image_coverage(page, width * height)

    text_items = extract_text_items(page)
    raw_text = page.get_text("text")
    sheet_unit = detect_sheet_unit(raw_text)
    dimension_texts = parse_dimension_texts(text_items, sheet_unit or "mm")

    return PageAnalysis(
        page_number=page_number,
        width=width,
        height=height,
        rotation=int(page.rotation),
        drawing_type=classify_page(primitives, vector_ink, math.hypot(width, height), coverage),
        primitives=primitives,
        text_items=text_items,
        dimension_texts=dimension_texts,
        tolerances=tolerances,
        image_count=image_count,
        image_coverage=coverage,
        vector_ink=vector_ink,
        sheet_unit=sheet_unit,
        scale_ratio=detect_scale_ratio(text_items),
        view_labels=detect_view_labels(text_items),
        raw_text=raw_text,
    )


def document_summary(doc: Any, file_name: str) -> Dict[str, Any]:
    """Cheap whole-document overview used right after upload.

    Deliberately avoids full primitive extraction on every page — a 40-page
    drawing set would otherwise stall the upload (§33).
    """
    pages: List[Dict[str, Any]] = []
    for index in range(doc.page_count):
        page = doc.load_page(index)
        rect = page.rect
        drawing_count = len(page.get_drawings())
        image_count, coverage = _image_coverage(page, rect.width * rect.height)
        text_length = len(page.get_text("text").strip())
        # Path count is a good enough proxy for classification at this stage.
        if drawing_count >= _MIN_VECTOR_PRIMITIVES and coverage >= _RASTER_COVERAGE:
            drawing_type = DrawingType.MIXED
        elif drawing_count >= _MIN_VECTOR_PRIMITIVES:
            drawing_type = DrawingType.VECTOR
        elif coverage >= _RASTER_COVERAGE:
            drawing_type = DrawingType.RASTER
        else:
            drawing_type = DrawingType.UNKNOWN
        pages.append(
            {
                "page": index + 1,
                "width_pt": round(float(rect.width), 2),
                "height_pt": round(float(rect.height), 2),
                "rotation": int(page.rotation),
                "path_count": drawing_count,
                "image_count": image_count,
                "image_coverage": round(coverage, 4),
                "text_length": text_length,
                "drawing_type": drawing_type.value,
            }
        )

    types = {p["drawing_type"] for p in pages}
    if types == {DrawingType.VECTOR.value}:
        overall = DrawingType.VECTOR.value
    elif types == {DrawingType.RASTER.value}:
        overall = DrawingType.RASTER.value
    elif types <= {DrawingType.UNKNOWN.value}:
        overall = DrawingType.UNKNOWN.value
    else:
        overall = DrawingType.MIXED.value

    return {
        "file_name": file_name,
        "page_count": doc.page_count,
        "metadata": {k: v for k, v in (doc.metadata or {}).items() if v},
        "is_encrypted": bool(doc.is_encrypted),
        "overall_drawing_type": overall,
        "pages": pages,
        #: Best page to start on: most vector paths wins, ties broken by order.
        "suggested_page": max(pages, key=lambda p: (p["path_count"], -p["page"]))["page"] if pages else 1,
    }
