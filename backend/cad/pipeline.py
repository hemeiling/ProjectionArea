"""Prepare a CAD drawing for the same downstream stages a PDF page uses.

CONSTITUTION.md §37. ``backend.pipeline.prepare_page`` classifies primitives,
detects regions and attempts calibration for a PDF page. A DXF needs the same
:class:`~backend.pipeline.PreparedPage` out the other end, but two of those
steps are answered by the file itself rather than inferred:

* **Scale** is declared (``$INSUNITS``), so there is nothing to calibrate.
* **Regions** are not a CAD concept. A DXF model space is one drawing, not a
  sheet with views laid out on it, so the whole model space is offered as the
  single region rather than inventing view detection for it.

Classification still runs: a CAD drawing has dimension and annotation entities
whose linework should not be measured either.
"""

from __future__ import annotations

from typing import Any

from backend.cad.analysis import analysis_from_cad
from backend.cad.dxf import CadDrawing, scale_from_cad_units
from backend.geometry.classify import classify_primitives, mark_dimension_linework
from backend.models import GeometryRole, Region, median_glyph_height
from backend.pdf.text import text_mask_boxes
from backend.pipeline import PreparedPage


def prepare_cad_page(drawing: CadDrawing) -> PreparedPage:
    """Classify a DXF's geometry and hand back a PreparedPage.

    Args:
        drawing: A loaded DXF.

    Returns:
        A PreparedPage whose scale comes from the file header and whose single
        region is the whole model space.
    """
    analysis = analysis_from_cad(drawing)

    boxes = text_mask_boxes(analysis.text_items)
    role_counts = classify_primitives(
        analysis.primitives, analysis.text_items, analysis.page_bbox, text_boxes=boxes
    )
    # A DXF model space has no sheet. The `sheet` role exists to demote a PDF's
    # drawing border and title block, which span almost the whole page — but in
    # model space the *part* legitimately spans the whole extent, so that rule
    # would demote the very geometry being measured. It is not applicable here.
    reinstated = 0
    for prim in analysis.primitives:
        if prim.role is GeometryRole.SHEET:
            prim.role = GeometryRole.PROFILE
            prim.role_reason = "model space has no sheet frame; sheet rule not applied"
            reinstated += 1

    demoted = mark_dimension_linework(
        analysis.primitives, analysis.dimension_texts, median_glyph_height(analysis.text_items)
    )
    if demoted or reinstated:
        role_counts = {}
        for prim in analysis.primitives:
            role_counts[prim.role.value] = role_counts.get(prim.role.value, 0) + 1

    box = drawing.bbox or analysis.page_bbox
    region = Region(
        id="r1",
        bbox=box,
        primitive_count=len(analysis.primitives),
        ink_length=analysis.vector_ink,
        text_count=len(analysis.text_items),
        label="Model space",
        kind="view",
    )

    prepared = PreparedPage(analysis=analysis, regions=[region], role_counts=role_counts)
    prepared.auto_scale = scale_from_cad_units(drawing.info)
    prepared.scale_candidates = []
    prepared.scale_warnings = (
        []
        if prepared.auto_scale.verified
        else ["The DXF does not declare its units, so no scale could be read from it."]
    )
    return prepared
