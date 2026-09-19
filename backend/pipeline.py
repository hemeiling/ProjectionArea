"""Stage coordinator: raw page -> classified geometry -> regions -> scale.

CONSTITUTION.md §11: detection and calculation stay separate, and every stage
produces inspectable output. This module owns the ordering; it performs no
measurement of its own.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Tuple

from backend.calibration.scale import resolve_scale
from backend.config import REGIONS
from backend.geometry.classify import classify_primitives, mark_dimension_linework
from backend.geometry.regions import detect_regions, detect_regions_from_page_image
from backend.models import BBox, Region, Scale, ScaleCandidate
from backend.pdf.document import PageAnalysis, analyze_page
from backend.pdf.text import text_mask_boxes


@dataclass
class PreparedPage:
    """A page analysed, classified and clustered — cached per (document, page).

    Preparing a page is the expensive part; area calculations then run against
    this cache, so changing region, scale or hole handling is instant (§33).
    """

    analysis: PageAnalysis
    regions: List[Region]
    role_counts: Dict[str, int] = field(default_factory=dict)
    scale_candidates: List[ScaleCandidate] = field(default_factory=list)
    auto_scale: Optional[Scale] = None
    scale_warnings: List[str] = field(default_factory=list)

    @property
    def text_height(self) -> float:
        heights = sorted(t.bbox.height for t in self.analysis.text_items if t.bbox.height > 0.1)
        return heights[len(heights) // 2] if heights else 7.0

    def default_region(self) -> Optional[Region]:
        """The region the UI should preselect: the strongest actual view."""
        views = [r for r in self.regions if r.kind == "view"]
        return views[0] if views else None

    def region_by_id(self, region_id: str) -> Optional[Region]:
        for region in self.regions:
            if region.id == region_id:
                return region
        return None

    def summary(self) -> Dict[str, Any]:
        return {
            **self.analysis.summary(),
            "role_counts": self.role_counts,
            "regions": [r.as_dict() for r in self.regions],
            "default_region_id": (self.default_region().id if self.default_region() else None),
            "scale": self.auto_scale.as_dict() if self.auto_scale else None,
            "scale_candidates": [c.as_dict() for c in self.scale_candidates],
            "scale_warnings": self.scale_warnings,
        }


def prepare_page(doc: Any, page_number: int) -> PreparedPage:
    """Read, classify and cluster one page, then attempt automatic calibration.

    Args:
        doc: An open ``fitz.Document``.
        page_number: 1-based page index.

    Returns:
        A :class:`PreparedPage` ready for repeated area calculations.
    """
    analysis = analyze_page(doc, page_number)

    boxes = text_mask_boxes(analysis.text_items)
    role_counts = classify_primitives(
        analysis.primitives, analysis.text_items, analysis.page_bbox, text_boxes=boxes
    )

    # Dimension linework is a page-level judgement; resolve it here so the
    # cached roles are final and no later stage has to mutate shared state.
    text_heights = sorted(t.bbox.height for t in analysis.text_items if t.bbox.height > 0.1)
    median_text_height = text_heights[len(text_heights) // 2] if text_heights else 7.0
    demoted = mark_dimension_linework(
        analysis.primitives, analysis.dimension_texts, median_text_height
    )
    if demoted:
        role_counts = {}
        for prim in analysis.primitives:
            role_counts[prim.role.value] = role_counts.get(prim.role.value, 0) + 1

    regions = detect_regions(
        analysis.primitives,
        analysis.text_items,
        analysis.page_bbox,
        REGIONS,
        view_labels=analysis.view_labels,
    )
    if not regions:
        # A scanned or image-only page still has views to choose between; find
        # them from the rendered ink instead of from primitives.
        regions = detect_regions_from_page_image(
            doc.load_page(page_number - 1), analysis.page_bbox, REGIONS
        )

    prepared = PreparedPage(analysis=analysis, regions=regions, role_counts=role_counts)

    scale, candidates, warnings = resolve_scale(
        analysis.primitives,
        analysis.dimension_texts,
        prepared.text_height,
        printed_ratio=analysis.scale_ratio,
    )
    prepared.auto_scale = scale
    prepared.scale_candidates = candidates
    prepared.scale_warnings = warnings
    return prepared


def region_scale(prepared: PreparedPage, region: Optional[Region]) -> Tuple[Scale, List[str]]:
    """Re-resolve the scale using only the dimensions inside one region.

    A sheet can legitimately mix scales — a detail view at 2:1 next to a general
    view at 1:10 — so calibrating within the selected region is more correct
    than calibrating page-wide whenever the region has enough evidence of its
    own. :func:`resolve_scale` falls back to the whole page automatically when
    it does not.
    """
    from backend.models import ScaleSource

    def resolve(box: Optional[BBox]):
        return resolve_scale(
            prepared.analysis.primitives,
            prepared.analysis.dimension_texts,
            prepared.text_height,
            printed_ratio=prepared.analysis.scale_ratio,
            region=box,
        )

    if region is None:
        scale, _candidates, warnings = resolve(None)
        return scale, warnings

    scale, _candidates, warnings = resolve(region.bbox)
    if scale.source is ScaleSource.DIMENSION_CONSENSUS:
        return scale, warnings

    # The region alone did not carry enough agreeing dimensions. A page-wide
    # consensus, when one exists, is stronger evidence than a lone in-region
    # match that could have latched onto the wrong line.
    page_scale, _page_candidates, page_warnings = resolve(None)
    if page_scale.source is ScaleSource.DIMENSION_CONSENSUS or (
        page_scale.verified and page_scale.confidence > scale.confidence
    ):
        page_scale.evidence = list(page_scale.evidence) + [
            "calibrated from dimensions across the whole sheet: the selected "
            "region did not carry enough agreeing dimensions of its own"
        ]
        return page_scale, page_warnings
    return scale, warnings
