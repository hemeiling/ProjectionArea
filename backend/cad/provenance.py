"""What a CAD entity was, kept alongside what it looks like.

CONSTITUTION.md §15 (never destroy source geometry) and §24 (preserve processing
metadata), applied to the CAD source adapter.

Production evidence made this the whole point of the DXF path. The PDF export of
GLTR-101 carries geometry and nothing else — no text, no layers, no block
structure — so the engine could measure the linework but could not tell a site
boundary from a machine. The DWG/DXF still holds all of that. Flattening a DXF
straight to polygons would throw away precisely the information that makes the
CAD path worth having.

So every primitive read from a DXF carries a :class:`CadProvenance` recording
where it came from: its handle, layer, block path, linetype, colour, space and
original coordinates. **No meaning is assigned here.** "Layer FENCE means the
safety perimeter" is a production rule, and production rules are not invented in
advance — they are derived from real drawings, with evidence. This module only
makes sure the evidence survives.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Sequence, Tuple

from backend.models import Point


@dataclass
class CadProvenance:
    """Where one primitive came from in the CAD file.

    Attributes:
        handle: The entity's DXF handle — its identity in the source file, so a
            finding can be taken back to the drawing and shown to a drafter.
        entity_type: ``LINE``, ``LWPOLYLINE``, ``CIRCLE``, ``HATCH`` and so on.
        layer: Layer name, verbatim, in the drawing's own language.
        space: ``model``, or ``paper:<layout name>``.
        linetype: Linetype name, verbatim (``CONTINUOUS``, ``CENTER``, …).
        color: ACI colour index, or ``None`` when the entity is BYLAYER.
        true_color: 24-bit RGB when the entity carries one.
        layer_color: The layer's own ACI colour, which is what BYLAYER resolves to.
        block_path: Block names from outermost to innermost, empty in modelspace.
            This is the nesting hierarchy, kept because a machine is usually one
            block and its parts are blocks inside it.
        insert_handles: Handle of each INSERT traversed, parallel to block_path.
        transform: The flattened 4x4 matrix actually applied to reach world
            coordinates, so the placement is reproducible.
        is_xref: True when the block is an external reference.
        xref_path: The referenced file, when known.
        source_file: The DXF this came from.
        raw_points: Coordinates exactly as read, before any normalisation (§15).
    """

    handle: str = ""
    entity_type: str = ""
    layer: str = ""
    space: str = "model"
    linetype: str = ""
    color: Optional[int] = None
    true_color: Optional[int] = None
    layer_color: Optional[int] = None
    block_path: List[str] = field(default_factory=list)
    insert_handles: List[str] = field(default_factory=list)
    transform: Optional[List[float]] = None
    is_xref: bool = False
    xref_path: Optional[str] = None
    source_file: str = ""
    raw_points: List[Point] = field(default_factory=list)

    @property
    def block_depth(self) -> int:
        return len(self.block_path)

    @property
    def owner_block(self) -> Optional[str]:
        """The innermost block this entity belongs to, if any."""
        return self.block_path[-1] if self.block_path else None

    def as_dict(self, include_raw: bool = False) -> Dict[str, Any]:
        payload: Dict[str, Any] = {
            "handle": self.handle,
            "entity_type": self.entity_type,
            "layer": self.layer,
            "space": self.space,
            "linetype": self.linetype,
            "color": self.color,
            "true_color": self.true_color,
            "layer_color": self.layer_color,
            "block_path": list(self.block_path),
            "block_depth": self.block_depth,
            "owner_block": self.owner_block,
            "insert_handles": list(self.insert_handles),
            "is_xref": self.is_xref,
            "xref_path": self.xref_path,
            "source_file": self.source_file,
        }
        if include_raw:
            payload["raw_points"] = [[round(x, 6), round(y, 6)] for x, y in self.raw_points]
            payload["transform"] = self.transform
        return payload


@dataclass
class CadLayer:
    """One layer table entry, as written."""

    name: str
    color: Optional[int] = None
    true_color: Optional[int] = None
    linetype: str = ""
    is_off: bool = False
    is_frozen: bool = False
    is_locked: bool = False
    #: Populated by the loader: how much geometry actually sits on this layer.
    entity_count: int = 0

    def as_dict(self) -> Dict[str, Any]:
        return {
            "name": self.name,
            "color": self.color,
            "true_color": self.true_color,
            "linetype": self.linetype,
            "off": self.is_off,
            "frozen": self.is_frozen,
            "locked": self.is_locked,
            "entity_count": self.entity_count,
        }


@dataclass
class CadBlock:
    """One block definition, with whether it is an external reference."""

    name: str
    is_xref: bool = False
    xref_path: Optional[str] = None
    entity_count: int = 0
    insert_count: int = 0

    def as_dict(self) -> Dict[str, Any]:
        return {
            "name": self.name,
            "is_xref": self.is_xref,
            "xref_path": self.xref_path,
            "entity_count": self.entity_count,
            "insert_count": self.insert_count,
        }


#: ``$INSUNITS`` header value -> (name, millimetres per drawing unit).
#: A drawing that declares its units gives the scale as a *fact*, which is the
#: single biggest advantage of the CAD path over a textless PDF export.
INSUNITS_MM: Dict[int, Tuple[str, Optional[float]]] = {
    0: ("unitless", None),
    1: ("inches", 25.4),
    2: ("feet", 304.8),
    3: ("miles", 1609344.0),
    4: ("millimeters", 1.0),
    5: ("centimeters", 10.0),
    6: ("meters", 1000.0),
    7: ("kilometers", 1000000.0),
    8: ("microinches", 25.4e-6),
    9: ("mils", 0.0254),
    10: ("yards", 914.4),
    11: ("angstroms", 1e-7),
    12: ("nanometers", 1e-6),
    13: ("microns", 0.001),
    14: ("decimeters", 100.0),
    15: ("decameters", 10000.0),
    16: ("hectometers", 100000.0),
    17: ("gigameters", 1e12),
    18: ("astronomical units", 1.495978707e14),
    19: ("light years", 9.4607304725808e18),
    20: ("parsecs", 3.0856775814914e19),
}


@dataclass
class CadDocumentInfo:
    """Everything read off the file that is not geometry.

    This is what a first look at a real DXF should answer, and what any future
    semantic rule will be written against.
    """

    file_name: str = ""
    dxf_version: str = ""
    acad_release: str = ""
    insunits: int = 0
    units_name: str = "unitless"
    mm_per_unit: Optional[float] = None
    extents_min: Optional[Point] = None
    extents_max: Optional[Point] = None
    layouts: List[str] = field(default_factory=list)
    layers: List[CadLayer] = field(default_factory=list)
    blocks: List[CadBlock] = field(default_factory=list)
    linetypes: List[str] = field(default_factory=list)
    xrefs: List[str] = field(default_factory=list)
    entity_counts: Dict[str, int] = field(default_factory=dict)
    unsupported_counts: Dict[str, int] = field(default_factory=dict)
    text_count: int = 0
    dimension_count: int = 0
    notes: List[str] = field(default_factory=list)

    @property
    def units_declared(self) -> bool:
        """True when the drawing states its units, so no scale guess is needed."""
        return self.mm_per_unit is not None

    @property
    def extents_size(self) -> Optional[Tuple[float, float]]:
        if not self.extents_min or not self.extents_max:
            return None
        return (
            self.extents_max[0] - self.extents_min[0],
            self.extents_max[1] - self.extents_min[1],
        )

    def as_dict(self) -> Dict[str, Any]:
        size = self.extents_size
        return {
            "file_name": self.file_name,
            "dxf_version": self.dxf_version,
            "acad_release": self.acad_release,
            "units": {
                "insunits": self.insunits,
                "name": self.units_name,
                "mm_per_unit": self.mm_per_unit,
                "declared": self.units_declared,
            },
            "extents": {
                "min": list(self.extents_min) if self.extents_min else None,
                "max": list(self.extents_max) if self.extents_max else None,
                "size": list(size) if size else None,
            },
            "layouts": list(self.layouts),
            "layers": [layer.as_dict() for layer in self.layers],
            "blocks": [block.as_dict() for block in self.blocks],
            "linetypes": list(self.linetypes),
            "xrefs": list(self.xrefs),
            "entity_counts": dict(sorted(self.entity_counts.items())),
            "unsupported_counts": dict(sorted(self.unsupported_counts.items())),
            "text_count": self.text_count,
            "dimension_count": self.dimension_count,
            "notes": list(self.notes),
        }


@dataclass
class CadText:
    """A TEXT/MTEXT/ATTRIB string with where it sits and what it belongs to.

    Kept separate from :class:`~backend.models.TextItem` geometry-side usage so
    the CAD layer/block of a label survives — that is what will eventually let a
    station label be tied to the equipment it names.
    """

    text: str
    insert: Point
    height: float
    rotation: float = 0.0
    provenance: CadProvenance = field(default_factory=CadProvenance)

    def as_dict(self) -> Dict[str, Any]:
        return {
            "text": self.text,
            "insert": [round(self.insert[0], 4), round(self.insert[1], 4)],
            "height": self.height,
            "rotation": self.rotation,
            "provenance": self.provenance.as_dict(),
        }


@dataclass
class CadDimension:
    """A dimension entity: its measurement and its text, straight from the file.

    A DXF states what a dimension measures. No OCR, no matching annotation to
    linework by proximity — the value is simply recorded, which removes the
    entire class of error the PDF path has to defend against.
    """

    measurement: Optional[float]
    text_override: str
    dim_type: str
    defpoints: List[Point] = field(default_factory=list)
    provenance: CadProvenance = field(default_factory=CadProvenance)

    def as_dict(self) -> Dict[str, Any]:
        return {
            "measurement": self.measurement,
            "text_override": self.text_override,
            "dim_type": self.dim_type,
            "defpoints": [[round(x, 4), round(y, 4)] for x, y in self.defpoints],
            "provenance": self.provenance.as_dict(),
        }
