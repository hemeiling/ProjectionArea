"""Request schemas for the HTTP API.

CONSTITUTION.md §13: units are explicit on the wire, never implied.
"""

from __future__ import annotations

from typing import Dict, List, Literal, Optional

from pydantic import BaseModel, Field

from backend.units import AREA_TO_MM2, LENGTH_TO_MM


class BBoxIn(BaseModel):
    """A rectangle in PDF user units, origin top-left."""

    x0: float
    y0: float
    x1: float
    y1: float


class ScaleSpec(BaseModel):
    """How the caller wants the physical scale established.

    ``auto`` re-runs dimension consensus for the selected region. ``two_point``
    is the engineer's own calibration and always wins. ``ratio`` trusts the
    printed 1:N and is flagged as an assumption. ``mm_per_unit`` is the escape
    hatch for a scale already established elsewhere.
    """

    mode: Literal["auto", "two_point", "ratio", "mm_per_unit", "cad_unit", "none"] = "auto"
    points: Optional[List[List[float]]] = Field(
        default=None, description="Two [x, y] points in PDF units, for two_point mode"
    )
    known_length: Optional[float] = Field(default=None, description="Real distance between the points")
    known_unit: str = Field(default="mm", description=f"One of {sorted(LENGTH_TO_MM)}")
    ratio_denominator: Optional[float] = Field(default=None, description="N in a 1:N drawing ratio")
    mm_per_unit: Optional[float] = Field(default=None, description="Millimetres per PDF user unit")


class AreaRequest(BaseModel):
    """A projected-area calculation request."""

    region_id: Optional[str] = Field(default=None, description="Detected region to measure")
    region_bbox: Optional[BBoxIn] = Field(default=None, description="Explicit region, overrides region_id")
    scale: ScaleSpec = Field(default_factory=ScaleSpec)
    subtract_holes: bool = True
    exclude_components: List[str] = Field(default_factory=list)
    component_selection: Literal["auto", "all", "largest"] = Field(
        default="auto",
        description=(
            "auto keeps every component on the vector paths and only the dominant "
            "one on a raster trace; all and largest force the choice"
        ),
    )
    include_roles: Optional[List[str]] = Field(
        default=None, description="Override which geometry roles may contribute"
    )
    role_overrides: Dict[str, str] = Field(
        default_factory=dict,
        description=(
            "Primitive index to geometry role, e.g. {\"42\": \"profile\"}. Applied "
            "last, so the engineer always overrules the classifier."
        ),
    )
    manual_add: List[List[List[float]]] = Field(
        default_factory=list, description="Hand-drawn rings, PDF units, unioned into the profile"
    )
    manual_subtract: List[List[List[float]]] = Field(
        default_factory=list, description="Hand-erased rings, PDF units, removed from the profile"
    )
    close_gaps: bool = True
    force_raster: bool = False


class PolygonMeasureRequest(BaseModel):
    """Authoritative area for user-drawn geometry. §22.

    Rings are in PDF user units. Positive rings are added, ``subtract`` rings
    are removed, and overlaps are resolved by union so nothing is double counted.
    """

    add: List[List[List[float]]] = Field(default_factory=list)
    subtract: List[List[List[float]]] = Field(default_factory=list)
    scale: ScaleSpec = Field(default_factory=ScaleSpec)
    output_unit: str = Field(default="mm2", description=f"One of {sorted(AREA_TO_MM2)}")
