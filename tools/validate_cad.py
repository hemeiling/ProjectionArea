"""Validate a real DXF, and cross-check it against the PDF of the same drawing.

CONSTITUTION.md §26 for real sources, §30 for never hiding a choice, §7 for never
guessing. This is the CAD counterpart of ``tools.validate_drawings``:

    DXF -> ingest -> units -> inventory -> model/paper space
        -> metadata discriminability
        -> footprint interpretations supported by the evidence
        -> compare with the PDF + manual calibration path
        -> percentage discrepancy
        -> overlays
        -> geometric / provisional / confirmed verdict per reading

**It assigns no meaning to any layer or block name.** The discriminability
section answers "can this metadata separate the drawing into groups, and how
cleanly?" — a structural question. It never answers "which group is the fence",
because that is a production rule and production rules are written against real
drawings with evidence, by a person who has looked at them. Every per-group
footprint is reported as a fact about a named group, and stays `provisional`
until a human confirms what the group is.

Usage::

    .venv/bin/python -m tools.validate_cad Inputs --out-dir validation/cad
    .venv/bin/python -m tools.validate_cad a.dxf --pdf a.pdf --calibrate 75000
"""

from __future__ import annotations

import argparse
import datetime as _datetime
import json
import os
import sys
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Sequence, Tuple

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from backend.area.projected import compute_projected_area
from backend.cad.analysis import analysis_from_cad
from backend.cad.dxf import CadDrawing, DxfReadError, load_dxf, scale_from_cad_units
from backend.models import AreaResult, BBox, FootprintSemantics, ViewSource
from backend.units import Area

#: A metadata axis that puts almost everything in one group cannot separate
#: anything; one that puts almost everything in its own group is noise. Both are
#: reported, with this as the threshold for calling an axis "useful".
_USEFUL_MIN_GROUPS = 2
_USEFUL_MAX_DOMINANCE = 0.95


@dataclass
class GroupFootprint:
    """The geometry that carries one metadata value, measured on its own."""

    axis: str
    value: str
    primitive_count: int
    ink_units: float
    ink_share: float
    bbox: Optional[List[float]] = None
    area_m2: Optional[float] = None
    closed_loops: int = 0
    semantics: str = FootprintSemantics.PROVISIONAL.value

    def as_dict(self) -> Dict[str, Any]:
        return {
            "axis": self.axis,
            "value": self.value,
            "primitive_count": self.primitive_count,
            "ink_units": round(self.ink_units, 2),
            "ink_share": round(self.ink_share, 4),
            "bbox": self.bbox,
            "area_m2": self.area_m2,
            "closed_loops": self.closed_loops,
            "semantics": self.semantics,
        }


def _axis_values(drawing: CadDrawing) -> Dict[str, Dict[str, List[int]]]:
    """Primitive indices grouped by each metadata axis the file provides."""
    axes: Dict[str, Dict[str, List[int]]] = {
        "layer": {}, "block": {}, "linetype": {}, "color": {}, "entity_type": {}, "space": {},
    }
    for index, prov in drawing.provenance.items():
        colour = prov.color if prov.color is not None else prov.layer_color
        for axis, value in (
            ("layer", prov.layer or "(none)"),
            ("block", prov.owner_block or "(model space)"),
            ("linetype", prov.linetype or "(bylayer)"),
            ("color", str(colour) if colour is not None else "(bylayer)"),
            ("entity_type", prov.entity_type),
            ("space", prov.space),
        ):
            axes[axis].setdefault(value, []).append(index)
    return axes


def discriminability(drawing: CadDrawing) -> Dict[str, Any]:
    """How well each metadata axis partitions the drawing.

    Structural only. Says *whether* layers separate the geometry into distinct,
    spatially meaningful groups — not what those groups are.
    """
    total_ink = sum(p.length for p in drawing.primitives) or 1.0
    by_index = {p.index: p for p in drawing.primitives}
    report: Dict[str, Any] = {}

    for axis, groups in _axis_values(drawing).items():
        rows = []
        for value, indices in groups.items():
            ink = sum(by_index[i].length for i in indices if i in by_index)
            points = [pt for i in indices if i in by_index for pt in by_index[i].points]
            box = BBox.from_points(points) if len(points) >= 2 else None
            rows.append(
                {
                    "value": value,
                    "primitives": len(indices),
                    "ink_share": round(ink / total_ink, 4),
                    "bbox": [round(v, 2) for v in (box.x0, box.y0, box.x1, box.y1)] if box else None,
                    "closed_loops": sum(
                        1 for i in indices if i in by_index and by_index[i].closed
                    ),
                }
            )
        rows.sort(key=lambda r: -r["ink_share"])
        dominance = rows[0]["ink_share"] if rows else 1.0
        report[axis] = {
            "group_count": len(rows),
            "largest_group_ink_share": dominance,
            "useful": len(rows) >= _USEFUL_MIN_GROUPS and dominance <= _USEFUL_MAX_DOMINANCE,
            "why_not": (
                "only one group — this axis separates nothing"
                if len(rows) < _USEFUL_MIN_GROUPS
                else f"one group holds {dominance * 100:.1f} % of the ink"
                if dominance > _USEFUL_MAX_DOMINANCE
                else None
            ),
            "groups": rows[:40],
            "truncated": max(0, len(rows) - 40),
        }
    return report


def measure_cad(drawing: CadDrawing, file_name: str) -> AreaResult:
    """Run the ordinary engine over the whole model space."""
    analysis = analysis_from_cad(drawing)
    return compute_projected_area(
        analysis=analysis,
        fitz_page=None,
        document_id="cad",
        file_name=file_name,
        scale=scale_from_cad_units(drawing.info),
        region_bbox=None,
        view_source=ViewSource.WHOLE_PAGE,
        view_label="Model space",
    )


def measure_group(
    drawing: CadDrawing, indices: Sequence[int], file_name: str
) -> Optional[AreaResult]:
    """Measure only the geometry carrying one metadata value.

    This is what CAD buys over the PDF: the drawing can be measured *per layer*
    or *per block* rather than as one undifferentiated mass. The result is still
    a fact about a named group, never a claim about what the group is.
    """
    keep = set(indices)
    subset = [p for p in drawing.primitives if p.index in keep]
    # One closed polyline is a perfectly good footprint — a site boundary is
    # usually exactly that — so the test is on usable vertices, not entity count.
    if not subset or sum(len(p.points) for p in subset) < 3:
        return None
    trimmed = CadDrawing(
        info=drawing.info,
        primitives=subset,
        texts=[],
        dimensions=[],
        provenance={i: drawing.provenance[i] for i in keep if i in drawing.provenance},
    )
    analysis = analysis_from_cad(trimmed)
    try:
        return compute_projected_area(
            analysis=analysis,
            fitz_page=None,
            document_id="cad-group",
            file_name=file_name,
            scale=scale_from_cad_units(drawing.info),
            region_bbox=None,
            view_source=ViewSource.WHOLE_PAGE,
            view_label="group",
        )
    except Exception:
        return None


def group_footprints(drawing: CadDrawing, file_name: str, axis: str = "layer") -> List[GroupFootprint]:
    """Per-group footprints along one metadata axis, largest first."""
    total_ink = sum(p.length for p in drawing.primitives) or 1.0
    by_index = {p.index: p for p in drawing.primitives}
    out: List[GroupFootprint] = []

    for value, indices in _axis_values(drawing).get(axis, {}).items():
        ink = sum(by_index[i].length for i in indices if i in by_index)
        points = [pt for i in indices if i in by_index for pt in by_index[i].points]
        box = BBox.from_points(points) if len(points) >= 2 else None
        result = measure_group(drawing, indices, file_name)
        area_m2 = None
        if result is not None and result.area_mm2 is not None:
            area_m2 = Area(result.area_mm2).to("m2")
        out.append(
            GroupFootprint(
                axis=axis,
                value=value,
                primitive_count=len(indices),
                ink_units=ink,
                ink_share=ink / total_ink,
                bbox=[round(v, 2) for v in (box.x0, box.y0, box.x1, box.y1)] if box else None,
                area_m2=area_m2,
                closed_loops=sum(1 for i in indices if i in by_index and by_index[i].closed),
            )
        )
    out.sort(key=lambda g: -(g.area_m2 or 0.0))
    return out


def render_overlay(drawing: CadDrawing, result: AreaResult, path: str, dpi: int = 110) -> Optional[str]:
    """Draw the CAD geometry and the measured footprint to a PNG.

    Uses PyMuPDF as a plotter so the palette matches the PDF path's overlays —
    green counted, red holes, grey everything else.
    """
    try:
        import fitz
    except ImportError:
        return None

    box = drawing.bbox
    if box is None or box.width <= 0 or box.height <= 0:
        return None

    margin = 0.04 * max(box.width, box.height)
    page_w, page_h = 1000.0, 1000.0 * (box.height + 2 * margin) / (box.width + 2 * margin)
    scale = page_w / (box.width + 2 * margin)

    doc = fitz.open()
    page = doc.new_page(width=page_w, height=page_h)

    def to_page(pt: Tuple[float, float]) -> "fitz.Point":
        # CAD y runs up; page y runs down.
        return fitz.Point(
            (pt[0] - box.x0 + margin) * scale,
            page_h - (pt[1] - box.y0 + margin) * scale,
        )

    shape = page.new_shape()
    for prim in drawing.primitives:
        pts = [to_page(p) for p in prim.points]
        if len(pts) < 2:
            continue
        shape.draw_polyline(pts)
        shape.finish(color=(0.29, 0.35, 0.37), width=0.4, closePath=False, stroke_opacity=0.35)
    shape.commit()

    shape = page.new_shape()
    for comp in result.components:
        if not comp.included:
            continue
        ring = [to_page(p) for p in comp.outer]
        if len(ring) >= 3:
            shape.draw_polyline(ring)
            shape.finish(color=(0.17, 0.42, 0.38), fill=(0.24, 0.60, 0.55),
                         fill_opacity=0.26, width=1.1, closePath=True)
        for hole in comp.holes:
            hring = [to_page(p) for p in hole]
            if len(hring) >= 3:
                shape.draw_polyline(hring)
                shape.finish(color=(0.60, 0.20, 0.07), fill=(0.60, 0.20, 0.07),
                             fill_opacity=0.28, width=0.9, closePath=True)
    shape.commit()

    page.get_pixmap(matrix=fitz.Matrix(dpi / 72.0, dpi / 72.0), alpha=False).save(path)
    doc.close()
    return path


def compare_with_pdf(
    cad: AreaResult, pdf_result: Optional[AreaResult]
) -> List[Dict[str, Any]]:
    """Percentage discrepancy per reading, CAD against PDF.

    The goal is not to make the two agree. It is to see *where* they differ and
    why, because a difference is evidence about which definition each path is
    actually measuring.
    """
    if pdf_result is None:
        return []
    pdf_by_type = {i.type: i for i in pdf_result.footprint_interpretations}
    rows: List[Dict[str, Any]] = []
    for item in cad.footprint_interpretations:
        other = pdf_by_type.get(item.type)
        if other is None or item.area_mm2 is None or other.area_mm2 is None:
            rows.append({
                "type": item.type.value,
                "cad_m2": Area(item.area_mm2).to("m2") if item.area_mm2 is not None else None,
                "pdf_m2": Area(other.area_mm2).to("m2") if other and other.area_mm2 is not None else None,
                "difference_pct": None,
                "note": "not comparable — one side has no verified area",
            })
            continue
        cad_m2 = Area(item.area_mm2).to("m2")
        pdf_m2 = Area(other.area_mm2).to("m2")
        base = (cad_m2 + pdf_m2) / 2.0
        rows.append({
            "type": item.type.value,
            "cad_m2": cad_m2,
            "pdf_m2": pdf_m2,
            "difference_pct": ((cad_m2 - pdf_m2) / base * 100.0) if base else None,
            "note": None,
        })
    return rows


def verdicts(cad: AreaResult, evidence: Dict[str, Any]) -> List[Dict[str, str]]:
    """Which readings stay geometric, stay provisional, or could be confirmed.

    Nothing is promoted here. A reading becomes ``confirmed`` only when a person
    states what a group is, or a rule written against real evidence does — and
    this function reports *what would be needed*, which is the useful output
    before that evidence exists.
    """
    useful = [axis for axis, data in evidence.items() if data.get("useful")]
    out: List[Dict[str, str]] = []
    for item in cad.footprint_interpretations:
        semantics = (item.semantics or FootprintSemantics.GEOMETRIC).value
        if semantics == FootprintSemantics.GEOMETRIC.value:
            verdict, needs = "stays geometric", "nothing — the definition claims no meaning"
        else:
            verdict = "stays provisional"
            needs = (
                "a person confirming which of the "
                + ", ".join(useful)
                + " groups this geometry belongs to"
                if useful
                else "a metadata axis that separates the drawing; none of the "
                "available axes does"
            )
        out.append({
            "type": item.type.value,
            "current": semantics,
            "verdict": verdict,
            "to_become_confirmed": needs,
        })
    return out


# ── PDF pairing ──────────────────────────────────────────────────────────────


def _stem_key(name: str) -> str:
    """A loose key for matching a DXF to the PDF of the same drawing.

    Real exports differ in case, spacing and version suffix
    (``…-V3.6.dwg`` vs ``…-v3.6.pdf``), so matching is on the leading
    alphanumeric run of the file name.
    """
    stem = os.path.splitext(os.path.basename(name))[0].lower()
    return "".join(ch for ch in stem if ch.isalnum())[:24]


def find_pdf_for(dxf_path: str, search_dirs: Sequence[str]) -> Optional[str]:
    """The PDF that looks like the same drawing as ``dxf_path``, if any."""
    key = _stem_key(dxf_path)
    for directory in search_dirs:
        if not os.path.isdir(directory):
            continue
        for name in sorted(os.listdir(directory)):
            if name.lower().endswith(".pdf") and _stem_key(name) == key:
                return os.path.join(directory, name)
    return None


def measure_pdf(path: str, calibrate_mm: Optional[float]) -> Optional[AreaResult]:
    """Measure the paired PDF, calibrating only if a length was supplied.

    Without a calibration the PDF path reports no physical area at all on these
    drawings — which is correct, and means there is simply nothing to compare.
    """
    try:
        import fitz

        from backend.calibration.scale import scale_from_two_points
        from backend.pipeline import prepare_page, region_scale
    except ImportError:
        return None

    try:
        doc = fitz.open(path)
        prepared = prepare_page(doc, 1)
        scale, warnings = region_scale(prepared, None)
        if calibrate_mm and calibrate_mm > 0:
            first = compute_projected_area(
                analysis=prepared.analysis, fitz_page=doc.load_page(0),
                document_id="pdf", file_name=os.path.basename(path), scale=scale,
                region_bbox=None, view_source=ViewSource.WHOLE_PAGE, view_label="Whole page")
            if first.components:
                big = max(first.components, key=lambda c: c.area_units2)
                ys = [p[1] for p in big.outer]
                xs = [p[0] for p in big.outer]
                scale = scale_from_two_points((xs[0], min(ys)), (xs[0], max(ys)), calibrate_mm)
                warnings = [
                    f"Operator calibration: the longest extent of the largest body was "
                    f"declared {calibrate_mm:g} mm. This is an assumption about which "
                    f"span that dimension refers to, and must be confirmed."
                ]
        result = compute_projected_area(
            analysis=prepared.analysis, fitz_page=doc.load_page(0), document_id="pdf",
            file_name=os.path.basename(path), scale=scale, region_bbox=None,
            view_source=ViewSource.WHOLE_PAGE, view_label="Whole page",
            extra_warnings=warnings)
        doc.close()
        return result
    except Exception:
        return None


# ── report ───────────────────────────────────────────────────────────────────


def _m2(value: Optional[float]) -> str:
    return "—" if value is None else f"{value:,.4f}"


def build_report(records: Sequence[Dict[str, Any]], out_dir: str) -> str:
    stamp = _datetime.datetime.now().strftime("%Y-%m-%d %H:%M")
    lines = [
        "# Real CAD (DXF) validation",
        "",
        f"Generated {stamp} · {len(records)} file(s).",
        "",
        "No layer or block name is given a meaning anywhere in this report. Groups are",
        "named exactly as the file names them, and every per-group footprint stays",
        "**provisional** until a person states what that group is.",
        "",
    ]

    for record in records:
        lines.append(f"## {record['file_name']}")
        lines.append("")
        if record.get("error"):
            lines += [f"**BLOCKED** — {record['error']}", ""]
            continue

        info = record["info"]
        units = info["units"]
        lines += [
            "### 1 · Ingestion and units",
            "",
            f"- DXF version **{info['dxf_version']}**, layouts {info['layouts']}",
            f"- `$INSUNITS` = **{units['insunits']} ({units['name']})** → "
            + (f"1 unit = {units['mm_per_unit']:g} mm — **scale read from the header, not measured**"
               if units["declared"] else "**not declared** — no scale can be taken from the header"),
            f"- extents {info['extents']['min']} → {info['extents']['max']}",
            f"- geometry: {info['geometry']['primitive_count']:,} primitives, "
            f"{info['text_count']} text, {info['dimension_count']} dimensions",
            f"- entity counts: `{info['entity_counts']}`",
            f"- unreadable: `{info['unsupported_counts'] or 'none'}`",
            f"- XREFs: {info['xrefs'] or 'none'}",
        ]
        for note in info.get("notes", []):
            lines.append(f"- note: {note}")
        lines.append("")

        lines += ["### 2 · Inventory", "", "| Layer | Colour | Linetype | Entities |", "| --- | ---: | --- | ---: |"]
        for layer in sorted(info["layers"], key=lambda l: -l["entity_count"])[:30]:
            if layer["entity_count"] == 0:
                continue
            lines.append(f"| `{layer['name']}` | {layer['color']} | {layer['linetype']} | {layer['entity_count']:,} |")
        lines.append("")
        blocks = [b for b in info["blocks"] if b["insert_count"]]
        if blocks:
            lines += ["| Block | XREF | Entities | Placed |", "| --- | :-: | ---: | ---: |"]
            for block in sorted(blocks, key=lambda b: -b["insert_count"])[:30]:
                lines.append(
                    f"| `{block['name']}` | {'yes' if block['is_xref'] else ''} "
                    f"| {block['entity_count']:,} | {block['insert_count']:,} |")
            lines.append("")

        lines += ["### 3 · Which metadata can actually separate this drawing", "",
                  "| Axis | Groups | Largest group ink | Separates? |", "| --- | ---: | ---: | --- |"]
        for axis, data in record["discriminability"].items():
            verdict = "**yes**" if data["useful"] else f"no — {data['why_not']}"
            lines.append(f"| {axis} | {data['group_count']} | "
                         f"{data['largest_group_ink_share'] * 100:.1f} % | {verdict} |")
        lines.append("")

        lines += ["### 4 · Footprint interpretations (whole model space)", "",
                  "| Reading | Semantics | Area m² | Area ft² |", "| --- | --- | ---: | ---: |"]
        for item in record["interpretations"]:
            lines.append(
                f"| {item['name']} | {item['semantics']} | "
                f"{_m2((item['units'] or {}).get('m2'))} | "
                f"{(item['units'] or {}).get('ft2', 0):,.0f} |")
        lines.append("")

        groups = record.get("group_footprints") or []
        if groups:
            lines += ["### 5 · Footprint per layer — facts about named groups", "",
                      "Each row is the geometry carrying that layer name, measured on its own.",
                      "What the group *is* remains unstated.", "",
                      "| Layer | Primitives | Closed loops | Ink share | Area m² |",
                      "| --- | ---: | ---: | ---: | ---: |"]
            for group in groups[:25]:
                lines.append(
                    f"| `{group['value']}` | {group['primitive_count']:,} | {group['closed_loops']:,} "
                    f"| {group['ink_share'] * 100:.1f} % | {_m2(group['area_m2'])} |")
            lines.append("")

        comparison = record.get("comparison") or []
        if comparison:
            lines += ["### 6 · CAD ↔ PDF discrepancy", "",
                      "| Reading | CAD m² | PDF m² | Difference | Note |",
                      "| --- | ---: | ---: | ---: | --- |"]
            for row in comparison:
                diff = f"{row['difference_pct']:+.2f} %" if row["difference_pct"] is not None else "—"
                lines.append(f"| {row['type']} | {_m2(row['cad_m2'])} | {_m2(row['pdf_m2'])} "
                             f"| {diff} | {row['note'] or ''} |")
            lines.append("")
        elif record.get("paired_pdf"):
            lines += [f"### 6 · CAD ↔ PDF", "",
                      f"Paired with `{os.path.basename(record['paired_pdf'])}`, but the PDF "
                      "reports no verified area (no text layer, so no automatic scale). "
                      "Pass `--calibrate <mm>` to supply the operator calibration.", ""]

        lines += ["### 7 · Verdict per reading", "",
                  "| Reading | Now | Verdict | What would confirm it |", "| --- | --- | --- | --- |"]
        for row in record["verdicts"]:
            lines.append(f"| {row['type']} | {row['current']} | {row['verdict']} "
                         f"| {row['to_become_confirmed']} |")
        lines.append("")
        if record.get("overlay"):
            lines += [f"### 8 · Overlay", "", f"`{record['overlay']}`", ""]

    report = "\n".join(lines)
    with open(os.path.join(out_dir, "CAD_REPORT.md"), "w") as handle:
        handle.write(report)
    return report


def validate_dxf(
    path: str, out_dir: str, pdf_path: Optional[str], calibrate_mm: Optional[float], dpi: int
) -> Dict[str, Any]:
    """Run every stage over one DXF; never raises."""
    file_name = os.path.basename(path)
    try:
        drawing = load_dxf(path)
    except DxfReadError as error:
        return {"file_name": file_name, "error": str(error)}
    except Exception as error:  # pragma: no cover - defensive
        return {"file_name": file_name, "error": f"{type(error).__name__}: {error}"}

    result = measure_cad(drawing, file_name)
    evidence = discriminability(drawing)
    stem = os.path.splitext(file_name)[0].replace(os.sep, "_")
    overlay = render_overlay(drawing, result, os.path.join(out_dir, f"{stem}_cad_overlay.png"), dpi)

    pdf_result = measure_pdf(pdf_path, calibrate_mm) if pdf_path else None
    record = {
        "file_name": file_name,
        "info": drawing.summary(),
        "discriminability": evidence,
        "interpretations": [i.as_dict(include_geometry=False) for i in result.footprint_interpretations],
        "group_footprints": [g.as_dict() for g in group_footprints(drawing, file_name, "layer")],
        "paired_pdf": pdf_path,
        "comparison": compare_with_pdf(result, pdf_result),
        "verdicts": verdicts(result, evidence),
        "overlay": overlay,
    }
    with open(os.path.join(out_dir, f"{stem}_cad.json"), "w") as handle:
        json.dump(record, handle, indent=2, default=str)
    return record


def main(argv: Optional[List[str]] = None) -> int:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument("targets", nargs="+", help="DXF files, or directories of them")
    parser.add_argument("--out-dir", default="validation/cad")
    parser.add_argument("--pdf", help="explicit PDF to compare against")
    parser.add_argument("--calibrate", type=float,
                        help="known length in mm for the PDF's manual calibration")
    parser.add_argument("--dpi", type=int, default=110)
    args = parser.parse_args(argv)

    paths: List[str] = []
    search_dirs: List[str] = []
    for target in args.targets:
        if os.path.isdir(target):
            search_dirs.append(target)
            paths += [os.path.join(target, n) for n in sorted(os.listdir(target))
                      if n.lower().endswith(".dxf")]
        else:
            paths.append(target)
            search_dirs.append(os.path.dirname(target) or ".")
    search_dirs += ["Inputs", "samples"]

    if not paths:
        print(f"No .dxf files found in {args.targets}.")
        print("DWG cannot be read directly — export DXF from AutoCAD (Save As ->")
        print("AutoCAD DXF), or convert with the ODA File Converter.")
        return 0

    os.makedirs(args.out_dir, exist_ok=True)
    records = []
    for path in paths:
        pdf = args.pdf or find_pdf_for(path, search_dirs)
        print(f"[ dxf ] {os.path.basename(path)}"
              + (f"  ↔  {os.path.basename(pdf)}" if pdf else "  (no paired PDF)"))
        records.append(validate_dxf(path, args.out_dir, pdf, args.calibrate, args.dpi))

    build_report(records, args.out_dir)
    print(f"\nreport    {os.path.join(args.out_dir, 'CAD_REPORT.md')}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
