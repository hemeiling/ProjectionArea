"""Central engineering configuration.

CONSTITUTION.md §14: tolerances must live in one place, never scattered as
magic numbers.

All geometry tolerances are expressed in **PDF user units** (1 unit = 1/72 inch
= 0.352778 mm at 1:1 print scale). Because a drawing of a 5 mm screw and a
drawing of a 60 m production line can both live on an A1 sheet, absolute
tolerances alone are wrong: what matters is the tolerance *relative to the
linework being reconstructed*. Tolerances are therefore derived from the page
diagonal with an absolute floor, via :meth:`Tolerances.for_page`.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, asdict
from typing import Any, Dict

# ── Physical constants ────────────────────────────────────────────────────────

MM_PER_INCH = 25.4
PDF_UNITS_PER_INCH = 72.0
#: One PDF user unit expressed in millimetres of *paper*, at 1:1 print scale.
MM_PER_PDF_UNIT_AT_1_1 = MM_PER_INCH / PDF_UNITS_PER_INCH  # 0.3527777...

#: Software version stamped into every result for reproducibility (§24).
ENGINE_VERSION = "0.3.0"


@dataclass(frozen=True)
class Tolerances:
    """Geometry tolerances in PDF user units.

    Attributes:
        snap: Endpoint snapping radius. Two path endpoints closer than this are
            treated as the same vertex. Sized to absorb CAD export rounding.
        closure: Largest gap that may be *bridged* to close an otherwise open
            contour. Every bridge is recorded as a repair (§16).
        duplicate: Two collinear segments whose endpoints agree within this
            distance are duplicates; only one is kept so overlapping linework
            cannot inflate area.
        min_segment: Segments shorter than this are dropped as zero-length.
        min_polygon_area: Faces smaller than this (PDF units²) are discarded as
            specks produced by line-width crossings rather than real geometry.
        simplify: Douglas-Peucker tolerance applied only to *output* polygons so
            overlays stay light. Never applied before area computation.
        bezier_flatness: Maximum chord deviation when flattening Bezier curves
            to polylines. Drives adaptive subdivision depth.
        arc_min_points: Floor on the number of points per flattened curve, so
            small circles (holes) never degenerate into triangles.
    """

    snap: float = 0.30
    closure: float = 1.00
    duplicate: float = 0.20
    min_segment: float = 0.05
    min_polygon_area: float = 4.0
    simplify: float = 0.04
    bezier_flatness: float = 0.02
    arc_min_points: int = 16

    @classmethod
    def for_page(cls, width: float, height: float, scale: float = 1.0) -> "Tolerances":
        """Derive tolerances for a page of the given size in PDF units.

        The reference sheet is A4 portrait (diagonal ~= 892 units). A larger
        sheet carries proportionally coarser linework, so tolerances grow with
        the diagonal but are clamped so they never exceed roughly 1 mm of paper
        (beyond that, "closing a gap" starts inventing geometry).

        Args:
            width: Page width in PDF units.
            height: Page height in PDF units.
            scale: Extra user-supplied multiplier for stubborn drawings.

        Returns:
            A new :class:`Tolerances` instance.
        """
        reference_diagonal = math.hypot(595.0, 842.0)  # A4
        diagonal = math.hypot(max(width, 1.0), max(height, 1.0))
        factor = max(1.0, min(2.6, diagonal / reference_diagonal)) * max(scale, 0.05)
        base = cls()
        return cls(
            snap=base.snap * factor,
            closure=base.closure * factor,
            duplicate=base.duplicate * factor,
            min_segment=base.min_segment * factor,
            min_polygon_area=base.min_polygon_area * factor * factor,
            simplify=base.simplify * factor,
            bezier_flatness=base.bezier_flatness,
            arc_min_points=base.arc_min_points,
        )

    def as_dict(self) -> Dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True)
class RegionSettings:
    """Controls candidate drawing-region (view) detection. §17."""

    #: Resolution of the occupancy grid used to cluster linework, in PDF units
    #: per cell. Coarse on purpose: we are locating views, not tracing them.
    grid_pitch: float = 3.0
    #: Linework separated by less than this is considered one view. Roughly the
    #: whitespace gutter a draughtsman leaves between views.
    cluster_gap: float = 26.0
    #: A region must cover at least this fraction of the page to be offered.
    min_page_area_fraction: float = 0.0025
    #: A region covering more than this fraction is almost certainly the sheet
    #: border/frame rather than a view.
    max_page_area_fraction: float = 0.92
    #: Regions are padded by this many PDF units when reported, so the user's
    #: selection box comfortably contains the linework.
    padding: float = 4.0


@dataclass(frozen=True)
class CalibrationSettings:
    """Controls automatic scale recovery from dimension annotations. §4."""

    #: A dimension line candidate must have its midpoint within this multiple of
    #: the text height from the dimension text centre.
    text_search_radius_factor: float = 6.0
    #: Candidate scales are voted in log space; two votes within this relative
    #: tolerance land in the same bin (1.5%).
    vote_relative_tolerance: float = 0.015
    #: Minimum independent dimension texts that must agree before an automatic
    #: scale is considered *verified* rather than merely *suggested*.
    min_supporting_dimensions: int = 3
    #: Dimension values outside this range (mm) are ignored as non-dimensional
    #: numbers (part numbers, revisions, sheet counts, years).
    min_dimension_mm: float = 1.0
    max_dimension_mm: float = 200_000.0
    #: A dimension line shorter than this in PDF units is too short to calibrate
    #: against reliably.
    min_line_units: float = 8.0


@dataclass(frozen=True)
class RasterSettings:
    """Path B — scanned/raster drawings. §19."""

    #: Render DPI for raster tracing. 300 dpi is the practical floor for
    #: engineering linework; above 600 memory cost stops paying for itself.
    dpi: int = 300
    #: Adaptive threshold window in pixels.
    threshold_block: int = 41
    threshold_offset: int = 12
    #: Morphological closing kernel, in pixels, used to bridge scan gaps.
    close_kernel: int = 3
    #: Contours smaller than this fraction of the page are noise.
    min_area_fraction: float = 0.0004


@dataclass(frozen=True)
class SilhouetteSettings:
    """Controls the raster-assisted silhouette fallback (strategy V2)."""

    #: Target resolution of the scan-conversion mask along the region's longest
    #: side, in pixels. High enough that a 0.5 mm hole survives on an A1 sheet.
    target_pixels: int = 2400
    #: Hard ceiling so a pathological region cannot exhaust memory.
    max_pixels: int = 4200


TOLERANCES = Tolerances()
REGIONS = RegionSettings()
CALIBRATION = CalibrationSettings()
RASTER = RasterSettings()
SILHOUETTE = SilhouetteSettings()

#: Uploaded documents are held for this long before the janitor deletes them.
#: Engineering drawings are proprietary (§35) — nothing is kept indefinitely.
DOCUMENT_TTL_SECONDS = 60 * 60 * 4
