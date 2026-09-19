"""Read a DXF into the engine's domain model, keeping its CAD meaning.

CONSTITUTION.md §37: the source adapter seam. ``pdf/`` confines PyMuPDF; this
module confines ezdxf, and hands the rest of the engine the same
:class:`~backend.models.Primitive` list it already understands — with a
:class:`~backend.cad.provenance.CadProvenance` attached to every one.

Why this path exists
--------------------
The production PDFs are "Microsoft Print to PDF" exports of AutoCAD drawings.
They carry geometry and nothing else: no text layer, no layers, no blocks. The
engine can measure their linework but cannot tell a site boundary from a machine,
and cannot recover a scale at all. The DXF of the same drawing holds all of it —
declared units, layer names, block structure, real dimension values.

Two consequences shape this module:

* **Scale is a fact, not an inference.** ``$INSUNITS`` states the drawing's units,
  so mm-per-unit comes from the header rather than from matching annotations to
  linework. :data:`~backend.models.ScaleSource.CAD_UNITS` records that.
* **Nothing is flattened away.** Blocks are exploded so the geometry is usable,
  but the block path, the INSERT transforms, the handles and the original
  coordinates are all kept, because that is the information the PDF lacks.

**No production rules live here.** This module never decides that a layer is a
fence or a conveyor. It reads what the file says and preserves it; the rules that
read meaning into layer and block names must be written against real drawings,
with evidence, and belong elsewhere.
"""

from __future__ import annotations

import math
import os
from dataclasses import dataclass, field
from typing import Any, Dict, Iterable, List, Optional, Sequence, Tuple

from backend.cad.provenance import (
    INSUNITS_MM,
    CadBlock,
    CadDimension,
    CadDocumentInfo,
    CadLayer,
    CadProvenance,
    CadText,
)
from backend.models import BBox, Point, Primitive, PrimitiveKind, Scale

#: How finely a curve is sampled. Matches the PDF path's intent: fine enough
#: that flattening error is far below any drawing tolerance.
_ARC_SEGMENTS_PER_TURN = 72
_MIN_ARC_SEGMENTS = 8

#: Recursion guard for pathological / self-referencing block structures.
_MAX_BLOCK_DEPTH = 12


class DxfReadError(RuntimeError):
    """Raised when the file cannot be read as a DXF at all."""


@dataclass
class CadDrawing:
    """One DXF, normalised but not stripped."""

    info: CadDocumentInfo
    primitives: List[Primitive] = field(default_factory=list)
    texts: List[CadText] = field(default_factory=list)
    dimensions: List[CadDimension] = field(default_factory=list)
    #: provenance parallel to ``primitives``, indexed by primitive index.
    provenance: Dict[int, CadProvenance] = field(default_factory=dict)

    @property
    def bbox(self) -> Optional[BBox]:
        points = [p for prim in self.primitives for p in prim.points]
        return BBox.from_points(points) if len(points) >= 2 else None

    def by_layer(self) -> Dict[str, List[Primitive]]:
        """Primitives grouped by layer — the natural axis for CAD semantics."""
        groups: Dict[str, List[Primitive]] = {}
        for prim in self.primitives:
            layer = self.provenance[prim.index].layer if prim.index in self.provenance else ""
            groups.setdefault(layer, []).append(prim)
        return groups

    def by_block(self) -> Dict[Optional[str], List[Primitive]]:
        """Primitives grouped by the innermost block that owns them."""
        groups: Dict[Optional[str], List[Primitive]] = {}
        for prim in self.primitives:
            prov = self.provenance.get(prim.index)
            groups.setdefault(prov.owner_block if prov else None, []).append(prim)
        return groups

    def summary(self) -> Dict[str, Any]:
        info = self.info.as_dict()
        box = self.bbox
        info["geometry"] = {
            "primitive_count": len(self.primitives),
            "text_count": len(self.texts),
            "dimension_count": len(self.dimensions),
            "bbox": box.as_dict() if box else None,
            "layers_with_geometry": sorted(
                {p.layer for p in self.provenance.values() if p.layer}
            ),
            "blocks_with_geometry": sorted(
                {p.owner_block for p in self.provenance.values() if p.owner_block}
            ),
        }
        return info


def _aci_to_none(value: Any) -> Optional[int]:
    try:
        index = int(value)
    except (TypeError, ValueError):
        return None
    # 256 = BYLAYER, 0 = BYBLOCK: both mean "not set on the entity itself".
    return None if index in (0, 256) else index


def _points_from_arc(
    centre: Point, radius: float, start_deg: float, end_deg: float
) -> List[Point]:
    """Sample an arc, densely enough that flattening error is negligible."""
    sweep = (end_deg - start_deg) % 360.0
    if sweep <= 1e-9:
        sweep = 360.0
    count = max(_MIN_ARC_SEGMENTS, int(math.ceil(_ARC_SEGMENTS_PER_TURN * sweep / 360.0)))
    return [
        (
            centre[0] + radius * math.cos(math.radians(start_deg + sweep * i / count)),
            centre[1] + radius * math.sin(math.radians(start_deg + sweep * i / count)),
        )
        for i in range(count + 1)
    ]


def _bulge_arc(start: Point, end: Point, bulge: float) -> List[Point]:
    """Expand a polyline bulge into points, excluding the start.

    A bulge is the tangent of a quarter of the included angle — the compact form
    LWPOLYLINE uses for arc segments. Dropping it would turn every rounded corner
    in the drawing into a chord.
    """
    if abs(bulge) < 1e-12:
        return [end]
    dx, dy = end[0] - start[0], end[1] - start[1]
    chord = math.hypot(dx, dy)
    if chord < 1e-12:
        return [end]
    included = 4.0 * math.atan(abs(bulge))
    radius = chord / (2.0 * math.sin(included / 2.0))
    # Centre sits off the chord midpoint, on the side the bulge sign selects.
    apothem = math.sqrt(max(radius * radius - (chord / 2.0) ** 2, 0.0))
    mx, my = (start[0] + end[0]) / 2.0, (start[1] + end[1]) / 2.0
    ux, uy = -dy / chord, dx / chord
    sign = 1.0 if bulge > 0 else -1.0
    centre = (mx - ux * apothem * sign, my - uy * apothem * sign)
    start_deg = math.degrees(math.atan2(start[1] - centre[1], start[0] - centre[0]))
    end_deg = math.degrees(math.atan2(end[1] - centre[1], end[0] - centre[0]))
    if bulge < 0:
        start_deg, end_deg = end_deg, start_deg
    points = _points_from_arc(centre, radius, start_deg, end_deg)
    if bulge < 0:
        points.reverse()
    return points[1:] or [end]


def _transform_points(points: Sequence[Point], matrix: Any) -> List[Point]:
    """Apply an ezdxf Matrix44 to 2-D points, dropping z."""
    if matrix is None:
        return [(float(x), float(y)) for x, y in points]
    out: List[Point] = []
    for x, y in points:
        vx, vy, _vz = matrix.transform((float(x), float(y), 0.0))
        out.append((float(vx), float(vy)))
    return out


class _Reader:
    """Walks a DXF layout, exploding blocks while recording where things came from."""

    def __init__(self, doc: Any, file_name: str) -> None:
        self.doc = doc
        self.file_name = file_name
        self.primitives: List[Primitive] = []
        self.provenance: Dict[int, CadProvenance] = {}
        self.texts: List[CadText] = []
        self.dimensions: List[CadDimension] = []
        self.entity_counts: Dict[str, int] = {}
        self.unsupported: Dict[str, int] = {}
        self.layer_colors: Dict[str, Optional[int]] = {}

    # ── primitive construction ───────────────────────────────────────────────

    def _add(
        self,
        points: Sequence[Point],
        closed: bool,
        kind: PrimitiveKind,
        entity: Any,
        context: "_Context",
        filled: bool = False,
    ) -> None:
        cleaned = [(float(x), float(y)) for x, y in points]
        if len(cleaned) < 2:
            return
        index = len(self.primitives)
        layer = str(getattr(entity.dxf, "layer", "") or "")
        prim = Primitive(
            index=index,
            kind=kind,
            points=_transform_points(cleaned, context.matrix),
            closed=closed,
            stroked=True,
            filled=filled,
            line_width=0.0,
            dashed=self._is_dashed(entity, layer),
            layer=layer or None,
            path_index=index,
        )
        self.primitives.append(prim)
        self.provenance[index] = CadProvenance(
            handle=str(getattr(entity.dxf, "handle", "") or ""),
            entity_type=entity.dxftype(),
            layer=layer,
            space=context.space,
            linetype=str(getattr(entity.dxf, "linetype", "") or ""),
            color=_aci_to_none(getattr(entity.dxf, "color", None)),
            true_color=getattr(entity.dxf, "true_color", None),
            layer_color=self.layer_colors.get(layer),
            block_path=list(context.block_path),
            insert_handles=list(context.insert_handles),
            transform=list(context.matrix) if context.matrix is not None else None,
            is_xref=context.is_xref,
            xref_path=context.xref_path,
            source_file=self.file_name,
            raw_points=cleaned,
        )

    def _is_dashed(self, entity: Any, layer: str) -> bool:
        name = str(getattr(entity.dxf, "linetype", "") or "").upper()
        if name in ("BYLAYER", "", "BYBLOCK") and layer:
            try:
                name = str(self.doc.layers.get(layer).dxf.linetype or "").upper()
            except Exception:
                name = ""
        return name not in ("CONTINUOUS", "BYLAYER", "BYBLOCK", "")

    # ── entity dispatch ──────────────────────────────────────────────────────

    def visit(self, entities: Iterable[Any], context: "_Context") -> None:
        for entity in entities:
            kind = entity.dxftype()
            self.entity_counts[kind] = self.entity_counts.get(kind, 0) + 1
            handler = getattr(self, f"_on_{kind.lower()}", None)
            if handler is None:
                self.unsupported[kind] = self.unsupported.get(kind, 0) + 1
                continue
            try:
                handler(entity, context)
            except Exception:
                # One unreadable entity must not lose the drawing (§31).
                self.unsupported[kind] = self.unsupported.get(kind, 0) + 1

    def _on_line(self, entity: Any, context: "_Context") -> None:
        a, b = entity.dxf.start, entity.dxf.end
        self._add([(a.x, a.y), (b.x, b.y)], False, PrimitiveKind.LINE, entity, context)

    def _on_lwpolyline(self, entity: Any, context: "_Context") -> None:
        vertices = list(entity.get_points("xyb"))
        if len(vertices) < 2:
            return
        points: List[Point] = [(vertices[0][0], vertices[0][1])]
        for i in range(len(vertices) - 1):
            x, y, bulge = vertices[i]
            nx, ny = vertices[i + 1][0], vertices[i + 1][1]
            points.extend(_bulge_arc((x, y), (nx, ny), float(bulge or 0.0)))
        if entity.closed:
            x, y, bulge = vertices[-1]
            points.extend(_bulge_arc((x, y), (vertices[0][0], vertices[0][1]), float(bulge or 0.0)))
        self._add(points, bool(entity.closed), PrimitiveKind.POLYLINE, entity, context)

    def _on_polyline(self, entity: Any, context: "_Context") -> None:
        points = [(v.dxf.location.x, v.dxf.location.y) for v in entity.vertices]
        if len(points) < 2:
            return
        closed = bool(entity.is_closed)
        if closed:
            points.append(points[0])
        self._add(points, closed, PrimitiveKind.POLYLINE, entity, context)

    def _on_circle(self, entity: Any, context: "_Context") -> None:
        centre = (entity.dxf.center.x, entity.dxf.center.y)
        points = _points_from_arc(centre, float(entity.dxf.radius), 0.0, 360.0)
        self._add(points, True, PrimitiveKind.BEZIER, entity, context)

    def _on_arc(self, entity: Any, context: "_Context") -> None:
        centre = (entity.dxf.center.x, entity.dxf.center.y)
        points = _points_from_arc(
            centre, float(entity.dxf.radius), float(entity.dxf.start_angle), float(entity.dxf.end_angle)
        )
        self._add(points, False, PrimitiveKind.BEZIER, entity, context)

    def _on_ellipse(self, entity: Any, context: "_Context") -> None:
        points = [(p.x, p.y) for p in entity.flattening(distance=0.01)]
        self._add(points, bool(entity.is_closed), PrimitiveKind.BEZIER, entity, context)

    def _on_spline(self, entity: Any, context: "_Context") -> None:
        points = [(p.x, p.y) for p in entity.flattening(distance=0.01)]
        self._add(points, bool(entity.closed), PrimitiveKind.CURVE_CHAIN, entity, context)

    def _on_solid(self, entity: Any, context: "_Context") -> None:
        corners = []
        for name in ("vtx0", "vtx1", "vtx3", "vtx2"):  # DXF SOLID order is bow-tie
            try:
                v = getattr(entity.dxf, name)
            except AttributeError:
                continue
            corners.append((v.x, v.y))
        if len(corners) >= 3:
            corners.append(corners[0])
            self._add(corners, True, PrimitiveKind.QUAD, entity, context, filled=True)

    _on_trace = _on_solid

    def _on_hatch(self, entity: Any, context: "_Context") -> None:
        """Hatch boundaries only — the fill pattern is not geometry to measure."""
        for path in entity.paths:
            points: List[Point] = []
            for vertex in getattr(path, "vertices", []) or []:
                points.append((vertex[0], vertex[1]))
            if len(points) >= 3:
                points.append(points[0])
                self._add(points, True, PrimitiveKind.POLYLINE, entity, context, filled=True)

    def _on_point(self, entity: Any, context: "_Context") -> None:
        return  # a point bounds no area; counted, not converted

    def _on_text(self, entity: Any, context: "_Context") -> None:
        self._record_text(
            str(entity.dxf.text or ""),
            (entity.dxf.insert.x, entity.dxf.insert.y),
            float(getattr(entity.dxf, "height", 0.0) or 0.0),
            float(getattr(entity.dxf, "rotation", 0.0) or 0.0),
            entity,
            context,
        )

    def _on_mtext(self, entity: Any, context: "_Context") -> None:
        try:
            text = entity.plain_text()
        except Exception:
            text = str(getattr(entity, "text", "") or "")
        self._record_text(
            text,
            (entity.dxf.insert.x, entity.dxf.insert.y),
            float(getattr(entity.dxf, "char_height", 0.0) or 0.0),
            float(getattr(entity.dxf, "rotation", 0.0) or 0.0),
            entity,
            context,
        )

    def _on_attdef(self, entity: Any, context: "_Context") -> None:
        return

    def _record_text(
        self, text: str, insert: Point, height: float, rotation: float,
        entity: Any, context: "_Context",
    ) -> None:
        if not text.strip():
            return
        placed = _transform_points([insert], context.matrix)[0]
        layer = str(getattr(entity.dxf, "layer", "") or "")
        self.texts.append(
            CadText(
                text=text,
                insert=placed,
                height=height,
                rotation=rotation,
                provenance=CadProvenance(
                    handle=str(getattr(entity.dxf, "handle", "") or ""),
                    entity_type=entity.dxftype(),
                    layer=layer,
                    space=context.space,
                    linetype=str(getattr(entity.dxf, "linetype", "") or ""),
                    color=_aci_to_none(getattr(entity.dxf, "color", None)),
                    layer_color=self.layer_colors.get(layer),
                    block_path=list(context.block_path),
                    insert_handles=list(context.insert_handles),
                    is_xref=context.is_xref,
                    xref_path=context.xref_path,
                    source_file=self.file_name,
                    raw_points=[insert],
                ),
            )
        )

    def _on_dimension(self, entity: Any, context: "_Context") -> None:
        """Record the dimension's own measurement — no matching required."""
        measurement: Optional[float]
        try:
            measurement = float(entity.get_measurement())
        except Exception:
            measurement = None
        defpoints: List[Point] = []
        for name in ("defpoint", "defpoint2", "defpoint3", "text_midpoint"):
            try:
                v = getattr(entity.dxf, name)
                defpoints.append((float(v.x), float(v.y)))
            except Exception:
                continue
        layer = str(getattr(entity.dxf, "layer", "") or "")
        self.dimensions.append(
            CadDimension(
                measurement=measurement,
                text_override=str(getattr(entity.dxf, "text", "") or ""),
                dim_type=str(getattr(entity.dxf, "dimtype", "") or ""),
                defpoints=_transform_points(defpoints, context.matrix),
                provenance=CadProvenance(
                    handle=str(getattr(entity.dxf, "handle", "") or ""),
                    entity_type=entity.dxftype(),
                    layer=layer,
                    space=context.space,
                    block_path=list(context.block_path),
                    is_xref=context.is_xref,
                    source_file=self.file_name,
                ),
            )
        )

    def _on_insert(self, entity: Any, context: "_Context") -> None:
        """Explode a block reference, keeping the hierarchy and the transform."""
        if context.depth >= _MAX_BLOCK_DEPTH:
            self.unsupported["INSERT(depth limit)"] = (
                self.unsupported.get("INSERT(depth limit)", 0) + 1
            )
            return
        name = str(entity.dxf.name)
        block = self.doc.blocks.get(name) if name in self.doc.blocks else None
        if block is None:
            self.unsupported["INSERT(missing block)"] = (
                self.unsupported.get("INSERT(missing block)", 0) + 1
            )
            return

        try:
            local = entity.matrix44()
        except Exception:
            local = None
        if local is None:
            matrix = context.matrix
        elif context.matrix is None:
            matrix = local
        else:
            # Local placement first, then everything the parent INSERTs applied.
            matrix = local * context.matrix

        is_xref = bool(getattr(block.block, "dxf", None) and _block_is_xref(block))
        child = _Context(
            space=context.space,
            block_path=context.block_path + [name],
            insert_handles=context.insert_handles + [str(getattr(entity.dxf, "handle", "") or "")],
            matrix=matrix,
            depth=context.depth + 1,
            is_xref=context.is_xref or is_xref,
            xref_path=context.xref_path or _block_xref_path(block),
        )
        # ATTRIBs carry the values a drafter typed into the block instance.
        for attrib in getattr(entity, "attribs", []) or []:
            self._record_text(
                str(attrib.dxf.text or ""),
                (attrib.dxf.insert.x, attrib.dxf.insert.y),
                float(getattr(attrib.dxf, "height", 0.0) or 0.0),
                float(getattr(attrib.dxf, "rotation", 0.0) or 0.0),
                attrib,
                context,
            )
        self.visit(block, child)


@dataclass
class _Context:
    """Where in the block hierarchy the reader currently is."""

    space: str = "model"
    block_path: List[str] = field(default_factory=list)
    insert_handles: List[str] = field(default_factory=list)
    matrix: Any = None
    depth: int = 0
    is_xref: bool = False
    xref_path: Optional[str] = None


def _block_is_xref(block: Any) -> bool:
    try:
        return bool(block.block.dxf.flags & 0b1100)  # XREF | XREF_OVERLAY
    except Exception:
        return False


def _block_xref_path(block: Any) -> Optional[str]:
    try:
        path = block.block.dxf.xref_path
        return str(path) if path else None
    except Exception:
        return None


def load_dxf(path: str, include_paperspace: bool = False) -> CadDrawing:
    """Read a DXF file into primitives, text and dimensions, with provenance.

    Args:
        path: Path to a ``.dxf`` file.
        include_paperspace: Also walk paper-space layouts. Off by default —
            model space holds the real geometry; paper space holds the sheet.

    Returns:
        A :class:`CadDrawing`.

    Raises:
        DxfReadError: If the file cannot be parsed as DXF.
    """
    try:
        import ezdxf
        from ezdxf import recover
    except ImportError as error:  # pragma: no cover - dependency is declared
        raise DxfReadError(
            "ezdxf is required to read DXF files. Install it with "
            "`.venv/bin/python -m pip install ezdxf`."
        ) from error

    file_name = os.path.basename(path)

    # A DWG is not a DXF. ezdxf's recovery reader will chew on one and hand back
    # an empty drawing, which would look like "the file has no geometry" rather
    # than "this format needs converting" — so it is caught here, by signature,
    # with the instruction the user actually needs (§31).
    try:
        with open(path, "rb") as handle:
            head = handle.read(8)
    except OSError as error:
        raise DxfReadError(f"{file_name} could not be opened: {error}") from error
    if head[:2] == b"AC" and head[2:6].isdigit():
        version = head[:6].decode("ascii", "replace")
        raise DxfReadError(
            f"{file_name} is a DWG file ({version}), not a DXF. There is no pure-Python "
            "DWG reader: export DXF from AutoCAD (Save As -> AutoCAD DXF), or convert "
            "it locally with the ODA File Converter."
        )

    try:
        doc, auditor = recover.readfile(path)
    except IOError as error:
        raise DxfReadError(f"{file_name} could not be opened: {error}") from error
    except Exception as error:
        raise DxfReadError(
            f"{file_name} is not a readable DXF ({type(error).__name__}: {error}). "
            "A .dwg must be converted to DXF first — AutoCAD's Save As, or the ODA "
            "File Converter."
        ) from error

    info = CadDocumentInfo(file_name=file_name)
    info.dxf_version = str(doc.dxfversion)
    info.acad_release = str(getattr(doc, "acad_release", "") or "")
    if auditor.has_errors:
        info.notes.append(
            f"{len(auditor.errors)} structural error(s) were recovered on load; "
            "geometry may be incomplete."
        )

    header = doc.header
    info.insunits = int(header.get("$INSUNITS", 0) or 0)
    info.units_name, info.mm_per_unit = INSUNITS_MM.get(info.insunits, ("unknown", None))
    if not info.units_declared:
        info.notes.append(
            "The drawing does not declare its units ($INSUNITS = 0), so no scale "
            "can be taken from the header; it must be calibrated or stated."
        )
    for key, target in (("$EXTMIN", "extents_min"), ("$EXTMAX", "extents_max")):
        value = header.get(key)
        if value is None:
            continue
        x, y = float(value[0]), float(value[1])
        # A drawing with no stored extents writes +/-1e20 rather than omitting them.
        if abs(x) >= 1e19 or abs(y) >= 1e19:
            info.notes.append(f"{key} is unset in the header; extents taken from geometry.")
            continue
        setattr(info, target, (x, y))

    reader = _Reader(doc, file_name)
    for layer in doc.layers:
        entry = CadLayer(
            name=str(layer.dxf.name),
            color=_aci_to_none(getattr(layer.dxf, "color", None)) or abs(int(layer.dxf.color or 0)),
            true_color=getattr(layer.dxf, "true_color", None),
            linetype=str(getattr(layer.dxf, "linetype", "") or ""),
            is_off=bool(int(getattr(layer.dxf, "color", 1) or 1) < 0),
            is_frozen=bool(layer.is_frozen()),
            is_locked=bool(layer.is_locked()),
        )
        info.layers.append(entry)
        reader.layer_colors[entry.name] = entry.color

    for block in doc.blocks:
        name = str(block.name)
        if name.lower().startswith(("*model_space", "*paper_space")):
            continue
        info.blocks.append(
            CadBlock(
                name=name,
                is_xref=_block_is_xref(block),
                xref_path=_block_xref_path(block),
                entity_count=sum(1 for _ in block),
            )
        )
    info.xrefs = [b.xref_path or b.name for b in info.blocks if b.is_xref]
    info.linetypes = sorted({str(lt.dxf.name) for lt in doc.linetypes})
    info.layouts = [str(name) for name in doc.layout_names()]

    reader.visit(doc.modelspace(), _Context(space="model"))
    if include_paperspace:
        for name in doc.layout_names():
            if name == "Model":
                continue
            reader.visit(doc.layout(name), _Context(space=f"paper:{name}"))

    info.entity_counts = reader.entity_counts
    info.unsupported_counts = reader.unsupported
    info.text_count = len(reader.texts)
    info.dimension_count = len(reader.dimensions)

    counts: Dict[str, int] = {}
    for prov in reader.provenance.values():
        counts[prov.layer] = counts.get(prov.layer, 0) + 1
    for layer in info.layers:
        layer.entity_count = counts.get(layer.name, 0)

    inserts: Dict[str, int] = {}
    for prov in reader.provenance.values():
        for name in prov.block_path:
            inserts[name] = inserts.get(name, 0) + 1
    for block in info.blocks:
        block.insert_count = inserts.get(block.name, 0)

    return CadDrawing(
        info=info,
        primitives=reader.primitives,
        texts=reader.texts,
        dimensions=reader.dimensions,
        provenance=reader.provenance,
    )


def scale_from_cad_units(info: CadDocumentInfo) -> "Scale":
    """The drawing's own declared units, as a :class:`~backend.models.Scale`.

    This is the one place in the engine where a scale is *read* rather than
    measured or picked. ``$INSUNITS`` is a statement by the file about its own
    coordinates, so there is no matching, no consensus vote and no calibration
    error — which is exactly why the CAD path is the authoritative one.

    An undeclared unit (``$INSUNITS = 0``) yields an unverified scale rather
    than an assumption of millimetres (§3).
    """
    from backend.models import Scale, ScaleSource

    if not info.units_declared:
        return Scale(
            mm_per_unit=None,
            source=ScaleSource.NONE,
            confidence=0.0,
            detail=(
                "The drawing does not declare its units ($INSUNITS = 0). No scale "
                "can be taken from the header; calibrate or state it."
            ),
        )
    return Scale(
        mm_per_unit=info.mm_per_unit,
        source=ScaleSource.CAD_UNITS,
        confidence=0.99,
        detail=(
            f"Drawing units declared as {info.units_name} "
            f"($INSUNITS = {info.insunits}), so 1 unit = {info.mm_per_unit:g} mm."
        ),
        evidence=[
            f"$INSUNITS = {info.insunits} ({info.units_name})",
            f"DXF version {info.dxf_version}",
            "read from the file header; nothing was measured or matched",
        ],
    )
