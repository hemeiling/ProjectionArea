"""Domain model for the projected-area pipeline.

CONSTITUTION.md §12 (preserve intermediate geometry), §16 (record every
repair), §24 (preserve processing metadata) and §41 (result structure).

These are plain dataclasses rather than Pydantic models: they are the internal
engineering model and must stay usable from tests and scripts without a web
framework. The API layer serialises them with ``as_dict``.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Dict, List, Optional, Sequence, Tuple

Point = Tuple[float, float]


class DrawingType(str, Enum):
    """How a page carries its geometry. §19."""

    VECTOR = "vector"
    RASTER = "raster"
    MIXED = "mixed"
    UNKNOWN = "unknown"


class PrimitiveKind(str, Enum):
    """Normalised primitive types. §12."""

    LINE = "line"
    POLYLINE = "polyline"
    BEZIER = "bezier"
    RECT = "rect"
    QUAD = "quad"
    CURVE_CHAIN = "curve_chain"


class GeometryRole(str, Enum):
    """What a primitive appears to *mean* on an engineering drawing. §2.

    Only :attr:`PROFILE` geometry is allowed to contribute to a projected area.
    Everything else is retained and shown in the overlay so the user can see
    what was set aside and why.
    """

    PROFILE = "profile"            # candidate component outline / real linework
    CENTERLINE = "centerline"      # dashed or chain-dashed construction line
    HIDDEN = "hidden"              # dashed hidden edge, behind the silhouette
    DIMENSION = "dimension"        # dimension line, extension line, arrowhead
    ANNOTATION = "annotation"      # text decoration, leader, balloon, symbol
    SHEET = "sheet"                # drawing border, title block, revision table
    HATCH = "hatch"                # section hatching
    UNCERTAIN = "uncertain"        # kept, but flagged for the engineer


class ScaleSource(str, Enum):
    """Where a physical scale came from, weakest last. §4, §10."""

    USER_TWO_POINT = "user_two_point_calibration"
    #: The CAD file states its own units ($INSUNITS), so the scale is read, not
    #: inferred. Stronger than any measurement of the drawing, because nothing
    #: was measured — which is the central advantage of the CAD path.
    CAD_UNITS = "cad_declared_units"
    DIMENSION_CONSENSUS = "dimension_consensus"
    SINGLE_DIMENSION = "single_dimension"
    DRAWING_RATIO = "drawing_scale_ratio"
    NONE = "none"


class ViewSource(str, Enum):
    USER_SELECTED = "user_selected"
    AUTO_DETECTED = "auto_detected"
    WHOLE_PAGE = "whole_page"


class Method(str, Enum):
    """How the silhouette was reconstructed. §30 — never hide the method."""

    VECTOR_EXACT = "vector_exact_polygonization"
    VECTOR_GAP_CLOSED = "vector_gap_closed_silhouette"
    RASTER_TRACE = "raster_contour_trace"
    USER_POLYGON = "user_drawn_polygon"


class FootprintType(str, Enum):
    """Which physical region a measured area is claimed to represent. §2.

    "Projected area" is unambiguous for one machined part and ambiguous for a
    manufacturing line, where the same sheet defensibly yields several different
    numbers. Making the *type* explicit — rather than reducing everything to one
    ``projected_area`` field — is what lets a new definition be added without
    touching the area engine.

    The first group follows from geometry alone. The second cannot: knowing which
    linework is a fence or a conveyor is CAD semantics (layer, block, linetype),
    never a property of the shape, so those types exist here but are only ever
    produced by a semantics-aware source (§7 — no guessing).
    """

    # Purely geometric: the definition makes no claim about what the shape *is*.
    GEOMETRY_UNION = "geometry_union"
    CONVEX_ENVELOPE = "convex_envelope"
    BOUNDING_RECTANGLE = "bounding_rectangle"

    # Geometric constructions that invite a semantic reading, and must not be
    # given one without evidence. See FootprintSemantics.PROVISIONAL.
    ENCLOSING_BOUNDARY = "enclosing_boundary"
    INTERNAL_UNION = "internal_union"

    # Require CAD semantics; never inferred from shape.
    CONVEYOR_FOOTPRINT = "conveyor_footprint"
    GUARDED_AREA = "guarded_area"
    LINE_FOOTPRINT = "line_footprint"

    @property
    def requires_cad_semantics(self) -> bool:
        return self in _CAD_SEMANTIC_FOOTPRINTS


#: Types that a shape-only source must never claim to have measured.
_CAD_SEMANTIC_FOOTPRINTS = frozenset(
    {
        FootprintType.CONVEYOR_FOOTPRINT,
        FootprintType.GUARDED_AREA,
        FootprintType.LINE_FOOTPRINT,
    }
)


class FootprintSemantics(str, Enum):
    """How much is actually known about what a measured region *means*.

    Production evidence forced this distinction. On a manufacturing-line layout
    the largest closed loop is the site boundary, not a machine — so a reading
    can be arithmetically exact and semantically wrong. Naming that gap is the
    difference between a measurement and a claim (§3, §30).
    """

    #: The definition is purely geometric and asserts nothing about meaning.
    #: "The union of the counted geometry" is true whatever the geometry depicts.
    GEOMETRIC = "geometric"

    #: A geometric construction that suggests a physical meaning which has NOT
    #: been confirmed. Shown with its candidate readings, never as a fact.
    PROVISIONAL = "provisional"

    #: Backed by CAD metadata (layer, block, linetype) or by a human confirming
    #: it. Nothing produced from shape alone reaches this.
    CONFIRMED = "confirmed"


@dataclass
class FootprintInterpretation:
    """One defensible reading of "the projected area of this drawing".

    Carries its own geometry so each interpretation can be drawn as its own
    overlay — the user switches between readings and sees the region change,
    rather than trusting that a number means what they assume (§8).

    Attributes:
        id: Stable identifier within one result.
        type: Which physical region this claims to be.
        name: Short human label.
        means: One sentence naming the physical region, for the UI and report.
        outer: Outer rings of the interpreted region, in page units.
        holes: Rings subtracted from it.
        area_units2: Area in the drawing's own squared units.
        area_mm2: The same area in mm², or ``None`` without a verified scale (§3).
        evidence: What this reading was derived from, so it is reproducible.
        confidence: Confidence that this number *is* the named region. Inherited
            from the geometry and scale beneath it; a derivation that is exact
            arithmetic adds no certainty of its own.
        assumptions: Everything taken for granted to produce it.
        warnings: Anything that should reduce trust in it.
    """

    id: str
    type: FootprintType
    name: str
    means: str
    semantics: "FootprintSemantics" = None  # type: ignore[assignment]
    #: Physical regions this geometry *might* be, when semantics is PROVISIONAL.
    candidate_meanings: List[str] = field(default_factory=list)
    outer: List[List[Point]] = field(default_factory=list)
    holes: List[List[Point]] = field(default_factory=list)
    area_units2: float = 0.0
    area_mm2: Optional[float] = None
    evidence: List[str] = field(default_factory=list)
    confidence: Optional[float] = None
    assumptions: List[str] = field(default_factory=list)
    warnings: List[str] = field(default_factory=list)

    @property
    def requires_cad_semantics(self) -> bool:
        return self.type.requires_cad_semantics

    def as_dict(self, digits: int = 2, include_geometry: bool = True) -> Dict[str, Any]:
        from backend.units import Area

        def ring(points: Sequence[Point]) -> List[List[float]]:
            return [[round(x, digits), round(y, digits)] for x, y in points]

        payload: Dict[str, Any] = {
            "id": self.id,
            "type": self.type.value,
            "name": self.name,
            "means": self.means,
            "semantics": (self.semantics or FootprintSemantics.GEOMETRIC).value,
            "provisional": (self.semantics or FootprintSemantics.GEOMETRIC)
            is FootprintSemantics.PROVISIONAL,
            "candidate_meanings": list(self.candidate_meanings),
            "area_units2": self.area_units2,
            "area_mm2": self.area_mm2,
            "units": Area(self.area_mm2).as_dict() if self.area_mm2 is not None else None,
            "evidence": list(self.evidence),
            "confidence": self.confidence,
            "assumptions": list(self.assumptions),
            "warnings": list(self.warnings),
            "requires_cad_semantics": self.requires_cad_semantics,
        }
        if include_geometry:
            payload["outer"] = [ring(r) for r in self.outer]
            payload["holes"] = [ring(r) for r in self.holes]
        return payload


@dataclass
class BBox:
    """Axis-aligned bounding box in PDF user units."""

    x0: float
    y0: float
    x1: float
    y1: float

    @property
    def width(self) -> float:
        return self.x1 - self.x0

    @property
    def height(self) -> float:
        return self.y1 - self.y0

    @property
    def area(self) -> float:
        return max(0.0, self.width) * max(0.0, self.height)

    @property
    def center(self) -> Point:
        return (0.5 * (self.x0 + self.x1), 0.5 * (self.y0 + self.y1))

    @property
    def diagonal(self) -> float:
        return math.hypot(self.width, self.height)

    def padded(self, pad: float) -> "BBox":
        return BBox(self.x0 - pad, self.y0 - pad, self.x1 + pad, self.y1 + pad)

    def contains_point(self, p: Point) -> bool:
        return self.x0 <= p[0] <= self.x1 and self.y0 <= p[1] <= self.y1

    def intersects(self, other: "BBox") -> bool:
        return not (self.x1 < other.x0 or other.x1 < self.x0 or self.y1 < other.y0 or other.y1 < self.y0)

    def intersection_area(self, other: "BBox") -> float:
        dx = min(self.x1, other.x1) - max(self.x0, other.x0)
        dy = min(self.y1, other.y1) - max(self.y0, other.y0)
        return max(0.0, dx) * max(0.0, dy)

    @classmethod
    def from_points(cls, points: Sequence[Point]) -> "BBox":
        xs = [p[0] for p in points]
        ys = [p[1] for p in points]
        return cls(min(xs), min(ys), max(xs), max(ys))

    @classmethod
    def union_of(cls, boxes: Sequence["BBox"]) -> Optional["BBox"]:
        if not boxes:
            return None
        return cls(
            min(b.x0 for b in boxes),
            min(b.y0 for b in boxes),
            max(b.x1 for b in boxes),
            max(b.y1 for b in boxes),
        )

    def as_dict(self) -> Dict[str, float]:
        return {"x0": self.x0, "y0": self.y0, "x1": self.x1, "y1": self.y1}


@dataclass
class Primitive:
    """One normalised drawing primitive, in PDF user units, y-down page space.

    This is the *preserved raw layer* of §15: flattening curves into ``points``
    is lossless enough for area work, and ``kind`` records what it originally
    was so a future DXF/CAD exporter can rebuild true arcs.
    """

    index: int
    kind: PrimitiveKind
    points: List[Point]
    closed: bool
    stroked: bool
    filled: bool
    line_width: float
    dashed: bool
    color: Optional[Tuple[float, float, float]] = None
    fill_color: Optional[Tuple[float, float, float]] = None
    layer: Optional[str] = None
    role: GeometryRole = GeometryRole.PROFILE
    role_reason: str = ""
    path_index: int = -1

    @property
    def bbox(self) -> BBox:
        return BBox.from_points(self.points)

    @property
    def length(self) -> float:
        total = 0.0
        for a, b in zip(self.points, self.points[1:]):
            total += math.hypot(b[0] - a[0], b[1] - a[1])
        if self.closed and len(self.points) > 2:
            a, b = self.points[-1], self.points[0]
            total += math.hypot(b[0] - a[0], b[1] - a[1])
        return total

    def as_dict(self, include_points: bool = True) -> Dict[str, Any]:
        data: Dict[str, Any] = {
            "index": self.index,
            "kind": self.kind.value,
            "closed": self.closed,
            "stroked": self.stroked,
            "filled": self.filled,
            "dashed": self.dashed,
            "line_width": self.line_width,
            "role": self.role.value,
            "role_reason": self.role_reason,
            "bbox": self.bbox.as_dict(),
        }
        if include_points:
            data["points"] = [[round(x, 3), round(y, 3)] for x, y in self.points]
        return data


@dataclass
class TextItem:
    """A text span with its bounding box, used for annotation masking and for
    dimension-driven calibration. §6 — text informs, it never *is* geometry."""

    text: str
    bbox: BBox
    size: float
    direction: Tuple[float, float] = (1.0, 0.0)

    @property
    def glyph_height(self) -> float:
        """Cap height of the text, measured across the reading direction.

        The bounding box alone is not this number: rotate a page 90° and a
        span's ``bbox.height`` becomes the length of the *string* instead of the
        height of its letters. That matters because this height is the engine's
        yardstick for "small" — arrowhead size, annotation masking reach and the
        calibration search radius are all multiples of it — so on a rotated
        sheet the yardstick would inflate by the aspect ratio of the text.

        Taking the extent perpendicular to the reading direction is invariant
        under rotation, and for ordinary horizontal text it *is* ``bbox.height``.
        """
        dx, dy = self.direction
        return self.bbox.height if abs(dx) >= abs(dy) else self.bbox.width

    def as_dict(self) -> Dict[str, Any]:
        return {
            "text": self.text,
            "bbox": self.bbox.as_dict(),
            "size": self.size,
            "direction": list(self.direction),
        }


def median_glyph_height(text_items: Sequence["TextItem"], fallback: float = 7.0) -> float:
    """Typical annotation height on a page — the engine's yardstick for "small".

    Single definition shared by classification, region detection, calibration
    and the area stage, so the four of them can never drift apart on what counts
    as a "text-sized" length. Rotation-invariant via
    :attr:`TextItem.glyph_height`.

    Args:
        text_items: Spans read off the page.
        fallback: Returned when the page carries no measurable text.
    """
    heights = sorted(t.glyph_height for t in text_items if t.glyph_height > 0.1)
    return heights[len(heights) // 2] if heights else fallback


@dataclass
class Repair:
    """One recorded automatic geometry repair. §16 — nothing silent."""

    type: str
    count: int = 1
    magnitude_units: Optional[float] = None
    detail: str = ""

    def as_dict(self) -> Dict[str, Any]:
        return {
            "type": self.type,
            "count": self.count,
            "magnitude_units": self.magnitude_units,
            "detail": self.detail,
        }


@dataclass
class Region:
    """A candidate drawing region — a view, a detail, a table. §17."""

    id: str
    bbox: BBox
    primitive_count: int
    ink_length: float
    text_count: int
    label: str = ""
    kind: str = "view"          # view | title_block | sheet_frame | table | note
    view_guess: str = "unknown"  # top | front | side | section | detail | isometric
    view_guess_source: str = "none"
    score: float = 0.0

    def as_dict(self) -> Dict[str, Any]:
        return {
            "id": self.id,
            "bbox": self.bbox.as_dict(),
            "primitive_count": self.primitive_count,
            "ink_length": round(self.ink_length, 2),
            "text_count": self.text_count,
            "label": self.label,
            "kind": self.kind,
            "view_guess": self.view_guess,
            "view_guess_source": self.view_guess_source,
            "score": round(self.score, 4),
        }


@dataclass
class ScaleCandidate:
    """One piece of evidence for the physical scale. §4."""

    mm_per_unit: float
    source: ScaleSource
    support: int = 1
    agreement: float = 0.0
    detail: str = ""
    evidence: List[str] = field(default_factory=list)

    @property
    def ratio_denominator(self) -> float:
        """The nominal drawing ratio 1:N implied by this scale, at 1:1 print."""
        from backend.config import MM_PER_PDF_UNIT_AT_1_1

        return self.mm_per_unit / MM_PER_PDF_UNIT_AT_1_1

    def as_dict(self) -> Dict[str, Any]:
        return {
            "mm_per_unit": self.mm_per_unit,
            "source": self.source.value,
            "support": self.support,
            "agreement": round(self.agreement, 4),
            "implied_ratio": f"1 : {self.ratio_denominator:.4g}",
            "detail": self.detail,
            "evidence": self.evidence[:12],
        }


@dataclass
class Calibration:
    """An operator's two-point pick, kept so the result can be re-checked.

    A manually calibrated area is only as good as the span it was derived from,
    so the span itself is part of the record (§8): the viewer draws it back onto
    the drawing and the explanation states it, which is what lets someone else
    confirm the operator picked the right line.
    """

    a: Point
    b: Point
    known_length: float
    known_unit: str
    span_units: float

    def as_dict(self) -> Dict[str, Any]:
        return {
            "a": [round(self.a[0], 3), round(self.a[1], 3)],
            "b": [round(self.b[0], 3), round(self.b[1], 3)],
            "known_length": self.known_length,
            "known_unit": self.known_unit,
            "span_units": round(self.span_units, 4),
            "label": f"{self.known_length:g} {self.known_unit}",
        }


@dataclass
class Scale:
    """The scale actually used for a calculation, with its provenance."""

    mm_per_unit: Optional[float]
    source: ScaleSource
    confidence: float
    detail: str = ""
    evidence: List[str] = field(default_factory=list)
    cross_check_spread: Optional[float] = None
    #: Present only for a two-point calibration: the span the operator picked.
    calibration: Optional["Calibration"] = None
    #: Set explicitly when a human supplied part of the scale. Left ``None`` it
    #: is inferred from the source, so existing callers need no change.
    stated_by_operator: Optional[bool] = None

    @property
    def operator_supplied(self) -> bool:
        """True when a human established this scale rather than the engine.

        Kept distinct from ``verified``: an operator-supplied scale is usable
        and auditable, but it was not *derived* from the drawing, so the UI must
        say so rather than presenting it as an automatic finding (§30).

        A CAD drawing that does not declare ``$INSUNITS`` is the case that forced
        this to be a field rather than a test on the source: its *coordinates*
        are the drawing's own, but which physical unit they are in was stated by
        a person, so the reading is part measured and part asserted.
        """
        if self.stated_by_operator is not None:
            return self.stated_by_operator
        return self.source is ScaleSource.USER_TWO_POINT

    @property
    def verified(self) -> bool:
        """True only when a physical area may be reported (§3)."""
        return self.mm_per_unit is not None and self.mm_per_unit > 0

    def as_dict(self) -> Dict[str, Any]:
        from backend.config import MM_PER_PDF_UNIT_AT_1_1

        return {
            "mm_per_unit": self.mm_per_unit,
            "unit": "mm_per_pdf_unit",
            "source": self.source.value,
            "confidence": round(self.confidence, 4),
            "verified": self.verified,
            "implied_ratio": (
                f"1 : {self.mm_per_unit / MM_PER_PDF_UNIT_AT_1_1:.4g}" if self.verified else None
            ),
            "detail": self.detail,
            "evidence": self.evidence[:12],
            "cross_check_spread": self.cross_check_spread,
            "operator_supplied": self.operator_supplied,
            "calibration": self.calibration.as_dict() if self.calibration else None,
        }


@dataclass
class ConfidenceBreakdown:
    """Interpretable confidence. §10 — every number traceable to evidence."""

    overall: float
    source: float
    geometry: float
    scale: float
    view: float
    repair: float
    notes: List[str] = field(default_factory=list)

    @property
    def band(self) -> str:
        if self.overall >= 0.85:
            return "high"
        if self.overall >= 0.6:
            return "medium"
        return "low"

    def as_dict(self) -> Dict[str, Any]:
        return {
            "overall": round(self.overall, 4),
            "percent": int(round(self.overall * 100)),
            "band": self.band,
            "components": {
                "source": round(self.source, 4),
                "geometry": round(self.geometry, 4),
                "scale": round(self.scale, 4),
                "view": round(self.view, 4),
                "repair": round(self.repair, 4),
            },
            "notes": self.notes,
        }


@dataclass
class ProfileComponent:
    """One connected silhouette component, with its holes."""

    id: str
    outer: List[Point]
    holes: List[List[Point]]
    area_units2: float
    gross_area_units2: float
    included: bool = True

    def as_dict(self, digits: int = 2) -> Dict[str, Any]:
        def ring(points: Sequence[Point]) -> List[List[float]]:
            return [[round(x, digits), round(y, digits)] for x, y in points]

        return {
            "id": self.id,
            "outer": ring(self.outer),
            "holes": [ring(h) for h in self.holes],
            "area_units2": self.area_units2,
            "gross_area_units2": self.gross_area_units2,
            "hole_count": len(self.holes),
            "included": self.included,
        }


@dataclass
class GeometryReport:
    """What the geometry stage found and what it did about it. §11, §12."""

    raw_primitive_count: int = 0
    profile_primitive_count: int = 0
    ignored_primitive_count: int = 0
    segment_count: int = 0
    face_count: int = 0
    component_count: int = 0
    outer_contours: int = 0
    holes: int = 0
    repairs: List[Repair] = field(default_factory=list)
    role_counts: Dict[str, int] = field(default_factory=dict)

    def as_dict(self) -> Dict[str, Any]:
        return {
            "raw_primitives": self.raw_primitive_count,
            "profile_primitives": self.profile_primitive_count,
            "ignored_primitives": self.ignored_primitive_count,
            "segments": self.segment_count,
            "faces": self.face_count,
            "components": self.component_count,
            "outer_contours": self.outer_contours,
            "holes": self.holes,
            "repairs": [r.as_dict() for r in self.repairs],
            "role_counts": self.role_counts,
        }


@dataclass
class AreaResult:
    """The auditable projected-area result. §41, §42."""

    document_id: str
    file_name: str
    page: int
    method: Method
    drawing_type: DrawingType
    view_source: ViewSource
    view_label: str
    region_bbox: Optional[BBox]
    scale: Scale
    geometry: GeometryReport
    confidence: ConfidenceBreakdown
    components: List[ProfileComponent]
    area_units2: float
    gross_area_units2: float
    hole_area_units2: float
    subtract_holes: bool
    warnings: List[str] = field(default_factory=list)
    assumptions: List[str] = field(default_factory=list)
    engine_version: str = ""
    timestamp: str = ""
    #: Competing readings of this result, primary first. Populated by
    #: :func:`backend.area.interpretations.interpretations` once the silhouette
    #: exists; empty when nothing was reconstructed.
    footprint_interpretations: List[FootprintInterpretation] = field(default_factory=list)
    #: Readings the product intends to support that need CAD semantics, reported
    #: as known-and-unavailable rather than omitted.
    pending_interpretations: List[Dict[str, Any]] = field(default_factory=list)

    # ── Physical values, available only when scale is verified (§3) ──────────

    @property
    def has_profile(self) -> bool:
        """True when a silhouette was actually reconstructed.

        A run that reconstructs nothing must not present ``0.00 mm²`` as a
        verified measurement — that reads as "this part has no area" rather
        than "nothing was found" (§3).
        """
        return bool(self.components) and self.area_units2 > 0.0

    @property
    def area_mm2(self) -> Optional[float]:
        if not self.scale.verified or not self.has_profile:
            return None
        return self.area_units2 * (self.scale.mm_per_unit ** 2)

    @property
    def gross_area_mm2(self) -> Optional[float]:
        if not self.scale.verified or not self.has_profile:
            return None
        return self.gross_area_units2 * (self.scale.mm_per_unit ** 2)

    @property
    def hole_area_mm2(self) -> Optional[float]:
        if not self.scale.verified or not self.has_profile:
            return None
        return self.hole_area_units2 * (self.scale.mm_per_unit ** 2)

    def as_dict(self) -> Dict[str, Any]:
        from backend.messages import annotate as _annotate
        from backend.units import Area

        area_block: Dict[str, Any]
        if self.scale.verified and self.has_profile:
            area_block = {
                "verified": True,
                "net": Area(self.area_mm2).as_dict(),
                "gross": Area(self.gross_area_mm2).as_dict(),
                "holes": Area(self.hole_area_mm2).as_dict(),
            }
        else:
            area_block = {
                "verified": False,
                "message": (
                    "No closed profile was reconstructed, so there is no area to report."
                    if not self.has_profile
                    else "Scale not verified. Physical projected area cannot yet be "
                         "calculated. Calibrate against a known dimension."
                ),
                "net": None,
                "gross": None,
                "holes": None,
            }

        return {
            "document_id": self.document_id,
            "file_name": self.file_name,
            "page": self.page,
            "method": self.method.value,
            "drawing_type": self.drawing_type.value,
            "view": {
                "label": self.view_label,
                "source": self.view_source.value,
                "bbox": self.region_bbox.as_dict() if self.region_bbox else None,
            },
            "projected_area": area_block,
            "area_pdf_units2": {
                "net": self.area_units2,
                "gross": self.gross_area_units2,
                "holes": self.hole_area_units2,
            },
            "subtract_holes": self.subtract_holes,
            "scale": self.scale.as_dict(),
            "geometry": self.geometry.as_dict(),
            "confidence": self.confidence.as_dict(),
            "components": [c.as_dict() for c in self.components],
            # Every defensible reading of "the projected area", each with its own
            # geometry so the UI can draw and switch between them. The primary
            # reading is also in "projected_area" above; it is not collapsed into
            # that one field, because on a line layout the definition is the
            # question (§2, §30).
            "footprint_interpretations": [i.as_dict() for i in self.footprint_interpretations],
            "pending_interpretations": self.pending_interpretations,
            "warnings": self.warnings,
            # The same warnings, each paired with a stable code where one is
            # known, so a localised client can translate without parsing prose.
            "warnings_coded": _annotate(self.warnings),
            "assumptions": self.assumptions,
            "engine_version": self.engine_version,
            "timestamp": self.timestamp,
        }
