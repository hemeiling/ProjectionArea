"""Present a CAD drawing to the engine as the page analysis it already knows.

CONSTITUTION.md §37. The geometry, area, confidence and interpretation stages
were written against :class:`~backend.pdf.document.PageAnalysis` and know nothing
about PDFs beyond that shape. So the CAD path does not need its own copy of any
of them — it needs an adapter, which is this module.

**Tolerances are the one thing that cannot carry over.** The PDF path derives
them from the sheet diagonal in PDF points, which is meaningful because a sheet
is always roughly A-series. A CAD drawing has no sheet: its coordinates are real
millimetres, or metres, or inches, and a 12 000 × 6 000 mm line layout and a
12 × 6 m one are the same drawing with different ``$INSUNITS``. Scaling a
tolerance to the *extent* would make snapping 1000× coarser on the metric one for
no reason. So CAD tolerances are declared in millimetres — the units an engineer
would state them in — and converted into drawing units through the file's own
``mm_per_unit``.
"""

from __future__ import annotations

import math
from typing import Any, Dict, List, Optional

from backend.cad.dxf import CadDrawing
from backend.config import Tolerances
from backend.models import BBox, DrawingType, Primitive, TextItem
from backend.pdf.document import PageAnalysis

#: Engineering tolerances for CAD geometry, in **millimetres of the real part**.
#: A CAD file carries exact coordinates, so these are much tighter than the PDF
#: path's: there is no export rounding to absorb, only genuine draughting slop.
CAD_TOLERANCES_MM: Dict[str, float] = {
    #: Endpoint coincidence. CAD corners either meet or they do not; 0.5 mm
    #: absorbs a drafter's snap error without welding separate machines.
    "snap": 0.5,
    #: Largest gap that may be bridged to close a contour. Deliberately small:
    #: on a layout, a 5 mm gap is usually a real doorway, not a drafting slip.
    "closure": 2.0,
    "duplicate": 0.5,
    "min_segment": 0.1,
    #: Faces below 100 mm² (a 1 cm square) are line-crossing specks, not rooms.
    "min_polygon_area": 100.0,
    "simplify": 0.5,
    "bezier_flatness": 0.25,
}


def cad_tolerances(mm_per_unit: Optional[float]) -> Tolerances:
    """Tolerances in drawing units, from the millimetre values above.

    Args:
        mm_per_unit: The drawing's declared scale. When the file does not declare
            its units, the defaults are returned unchanged rather than guessed at
            — an undeclared drawing gets the engine's generic tolerances and a
            warning, not a silent assumption of millimetres (§3).
    """
    if not mm_per_unit or mm_per_unit <= 0:
        return Tolerances()
    return Tolerances(
        snap=CAD_TOLERANCES_MM["snap"] / mm_per_unit,
        closure=CAD_TOLERANCES_MM["closure"] / mm_per_unit,
        duplicate=CAD_TOLERANCES_MM["duplicate"] / mm_per_unit,
        min_segment=CAD_TOLERANCES_MM["min_segment"] / mm_per_unit,
        min_polygon_area=CAD_TOLERANCES_MM["min_polygon_area"] / (mm_per_unit ** 2),
        simplify=CAD_TOLERANCES_MM["simplify"] / mm_per_unit,
        bezier_flatness=CAD_TOLERANCES_MM["bezier_flatness"] / mm_per_unit,
        arc_min_points=Tolerances().arc_min_points,
    )


def _text_items(drawing: CadDrawing) -> List[TextItem]:
    """CAD text as the engine's :class:`TextItem`, keeping position and size.

    A CAD TEXT has an insertion point and a height but no bounding box, so the
    box is estimated from the height and the string length. That estimate is
    only ever used for annotation *masking*, never for a measurement.
    """
    items: List[TextItem] = []
    for text in drawing.texts:
        height = text.height or 2.5
        width = max(len(text.text), 1) * height * 0.62
        angle = math.radians(text.rotation or 0.0)
        x, y = text.insert
        items.append(
            TextItem(
                text=text.text,
                bbox=BBox(x, y, x + width, y + height),
                size=height,
                direction=(math.cos(angle), math.sin(angle)),
            )
        )
    return items


def analysis_from_cad(drawing: CadDrawing, page_number: int = 1) -> PageAnalysis:
    """Wrap a :class:`~backend.cad.dxf.CadDrawing` as a :class:`PageAnalysis`.

    The result feeds the ordinary geometry, area, confidence and interpretation
    stages unchanged. Provenance is *not* carried on the PageAnalysis — it stays
    on the :class:`CadDrawing`, indexed by primitive, so nothing is lost and the
    domain model needs no CAD-specific field.

    Args:
        drawing: A loaded DXF.
        page_number: Reported as the page; a DXF model space is always 1.

    Returns:
        A PageAnalysis whose ``primitives`` are the drawing's, in drawing units.
    """
    box = drawing.bbox or BBox(0.0, 0.0, 1.0, 1.0)
    # Origin is preserved: CAD coordinates are meaningful and are not re-based,
    # so a handle's position still matches the drawing a drafter opens.
    width = max(box.x1, 1e-6)
    height = max(box.y1, 1e-6)

    texts = _text_items(drawing)
    return PageAnalysis(
        page_number=page_number,
        width=width,
        height=height,
        rotation=0,
        drawing_type=DrawingType.VECTOR,
        primitives=list(drawing.primitives),
        text_items=texts,
        dimension_texts=[],
        tolerances=cad_tolerances(drawing.info.mm_per_unit),
        image_count=0,
        image_coverage=0.0,
        vector_ink=sum(p.length for p in drawing.primitives),
        sheet_unit="mm" if drawing.info.mm_per_unit == 1.0 else None,
        scale_ratio=None,
        view_labels=[],
        raw_text="\n".join(t.text for t in drawing.texts),
    )
