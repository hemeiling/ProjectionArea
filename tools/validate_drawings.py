"""Run the full pipeline over a set of real drawings and report what it did.

CONSTITUTION.md §26: synthetic fixtures prove the geometry is sound, they do not
prove the *interpretation* is. This is the harness for the real sheets — it runs
each drawing through every stage and writes down what happened at each one:

    source drawing
      -> geometry extraction      how many primitives, of what roles
      -> scale / unit             source, value, evidence, cross-check
      -> candidate footprint      which region was measured and why
      -> union polygon            components, holes, repairs
      -> projected area           mm^2, or an explicit refusal
      -> visual overlay           PNG showing exactly what was counted
      -> confidence + assumptions component scores and every assumption made

It measures and reports. It does **not** tune anything, and nothing here should
ever grow a special case for a particular drawing — if a sheet comes out wrong,
the finding goes to the engine with a regression fixture behind it (§27).

Usage::

    .venv/bin/python -m tools.validate_drawings samples --out-dir validation
    .venv/bin/python -m tools.validate_drawings a.pdf b.pdf --all-pages
    .venv/bin/python -m tools.validate_drawings samples --page 3

The comparison table lands on stdout and in ``<out-dir>/REPORT.md``, one row per
measured page, alongside a per-page JSON result and an overlay PNG.

`Utilization` is projected area over the bounding box of the measured profile —
how much of its own envelope the part actually fills. It is a *reporting*
number derived from the result, not an engineering input to it.
"""

from __future__ import annotations

import argparse
import datetime as _datetime
import json
import os
import sys
import traceback
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Sequence, Tuple

import fitz

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from backend.area.interpretations import body_breakdown, bounding_extent
from backend.models import AreaResult, BBox, ViewSource
from backend.pdf.document import document_summary
from backend.pipeline import prepare_page, region_scale
from backend.units import Area
from tools.audit_overlay import draw_overlay

#: Files whose first bytes are not "%PDF" never reach PyMuPDF — saying so is far
#: more useful than relaying "no objects found" from deep inside the parser.
_PDF_MAGIC = b"%PDF"


@dataclass
class PageRow:
    """One measured page: the comparison-table row plus its evidence."""

    file_name: str
    page: int
    status: str                                  # measured | refused | failed
    detail: str = ""
    drawing_type: str = ""
    view_label: str = ""
    scale_text: str = "—"
    source: str = "PDF"
    area_units2: Optional[float] = None
    area_mm2: Optional[float] = None
    bounding_width_mm: Optional[float] = None
    bounding_height_mm: Optional[float] = None
    bounding_area_mm2: Optional[float] = None
    utilization: Optional[float] = None
    confidence: Optional[float] = None
    interpretations: List[Dict[str, Any]] = field(default_factory=list)
    bodies: List[Dict[str, Any]] = field(default_factory=list)
    warnings: List[str] = field(default_factory=list)
    assumptions: List[str] = field(default_factory=list)
    stages: Dict[str, Any] = field(default_factory=dict)
    overlay_path: Optional[str] = None
    json_path: Optional[str] = None


def profile_bbox(result: AreaResult) -> Optional[BBox]:
    """Bounding box of the geometry that was actually counted, in PDF units.

    Deliberately the *included* components rather than the selected region: the
    region is a user's rough pick and would flatter the utilisation figure.
    """
    points: List[Tuple[float, float]] = []
    for component in result.components:
        if component.included:
            points.extend(component.outer)
    return BBox.from_points(points) if len(points) >= 3 else None


def _scale_text(result: AreaResult) -> str:
    if not result.scale.verified:
        return "NOT VERIFIED"
    payload = result.as_dict()
    ratio = payload.get("scale", {}).get("implied_ratio") or ""
    return f"{result.scale.mm_per_unit:.6f} mm/unit {ratio}".strip()


def _count_kinds(regions: Sequence[Any]) -> Dict[str, int]:
    counts: Dict[str, int] = {}
    for region in regions:
        counts[region.kind] = counts.get(region.kind, 0) + 1
    return counts


def _title_block_ambiguity(prepared: Any, result: AreaResult) -> Optional[Dict[str, Any]]:
    """Flag the known bottom-right / title-block confusion when it shows up.

    `_classify_region` calls any bottom-right cluster carrying >= 2 text spans a
    title block, and its `sparse` ink guard does not discriminate (README
    limitation 6). On a real layout sheet that can hide a genuine view. Two
    symptoms are worth reporting, and neither is inferred from shape alone:

    * a cluster labelled `title_block` that carries a *lot* of linework, so it
      may be a view that merely sits in the corner;
    * more than one `title_block` on the page, which no sheet has.

    Returns None when nothing looks ambiguous. This only ever *reports* — the
    classifier is left alone until real drawings say how to change it.
    """
    blocks = [r for r in prepared.regions if r.kind == "title_block"]
    if not blocks:
        return None

    primitives = prepared.analysis.primitives
    findings: List[Dict[str, Any]] = []
    for block in blocks:
        inside = [
            p
            for p in primitives
            if block.bbox.x0 <= p.bbox.center[0] <= block.bbox.x1
            and block.bbox.y0 <= p.bbox.center[1] <= block.bbox.y1
        ]
        ink = sum(p.length for p in inside)
        perimeter = max(2 * (block.bbox.width + block.bbox.height), 1e-9)
        findings.append(
            {
                "region_id": block.id,
                "label": block.label,
                "primitives_inside": len(inside),
                "ink_units": round(ink, 1),
                "ink_ratio": round(ink / perimeter, 3),
                "area_share_of_page": round(
                    block.bbox.area / max(prepared.analysis.page_bbox.area, 1e-9), 4
                ),
            }
        )

    suspicious = [f for f in findings if f["primitives_inside"] >= 20]
    if not suspicious and len(blocks) <= 1:
        return None

    return {
        "reason": (
            "more than one region was called a title block"
            if len(blocks) > 1
            else "a region called a title block carries a lot of linework, so it may be "
            "a view that happens to sit in the bottom-right corner"
        ),
        "regions": findings,
        "measured_region_was_a_title_block": any(
            b.label == result.view_label for b in blocks
        ),
        "note": (
            "Known limitation: the classifier's `sparse` ink guard does not "
            "discriminate (threshold 2.5; a plain plate scores 1.108). Reported, "
            "not corrected — select the region manually to override."
        ),
    }


def _stage_record(prepared: Any, result: AreaResult) -> Dict[str, Any]:
    """The per-stage trace: what each step of the pipeline produced."""
    analysis = prepared.analysis
    return {
        "source": {
            "drawing_type": analysis.drawing_type.value,
            "page_size_pt": [round(analysis.width, 1), round(analysis.height, 1)],
            "rotation": analysis.rotation,
            "sheet_unit": analysis.sheet_unit,
            "printed_scale": (
                {k: v for k, v in analysis.scale_ratio.items() if k != "bbox"}
                if analysis.scale_ratio
                else None
            ),
        },
        "geometry_extraction": {
            "primitives": len(analysis.primitives),
            "role_counts": dict(sorted(prepared.role_counts.items())),
            "text_spans": len(analysis.text_items),
            "dimension_texts": len(analysis.dimension_texts),
            # Counted vs set aside, the split that decides the area.
            "counted_primitives": result.geometry.profile_primitive_count,
            "excluded_primitives": result.geometry.ignored_primitive_count,
            "unresolved_primitives": prepared.role_counts.get("uncertain", 0),
        },
        "scale": {
            "source": result.scale.source.value,
            "verified": result.scale.verified,
            "mm_per_unit": result.scale.mm_per_unit if result.scale.verified else None,
            "detail": result.scale.detail,
            "candidates": len(prepared.scale_candidates),
        },
        "candidate_footprint": {
            "regions": [
                {
                    "id": r.id,
                    "label": r.label,
                    "kind": r.kind,
                    "bbox": [round(v, 1) for v in (r.bbox.x0, r.bbox.y0, r.bbox.x1, r.bbox.y1)],
                    "width_pt": round(r.bbox.width, 1),
                    "height_pt": round(r.bbox.height, 1),
                }
                for r in prepared.regions
            ],
            "regions_by_kind": _count_kinds(prepared.regions),
            "measured": result.view_label,
            "source": result.view_source.value,
            "title_block_ambiguity": _title_block_ambiguity(prepared, result),
        },
        "union_polygon": {
            "components": result.geometry.component_count,
            "outer_contours": result.geometry.outer_contours,
            "holes": result.geometry.holes,
            "faces": result.geometry.face_count,
            "repairs": [
                {"type": r.type, "count": r.count, "detail": r.detail}
                for r in result.geometry.repairs
            ],
            "method": result.method.value,
        },
        "projected_area": {
            "units2": round(result.area_units2, 4),
            "mm2": result.area_mm2,
            "gross_mm2": result.gross_area_mm2,
            "hole_mm2": result.hole_area_mm2,
            "in_units": (
                Area(result.area_mm2).as_dict() if result.area_mm2 is not None else None
            ),
        },
        "interpretations": [
            i.as_dict(include_geometry=False) for i in result.footprint_interpretations
        ],
        "pending_interpretations": result.pending_interpretations,
        "bodies": body_breakdown(result),
        "confidence": {
            "overall": result.confidence.overall,
            "band": result.confidence.band,
            "components": {
                "scale": round(result.confidence.scale, 4),
                "geometry": round(result.confidence.geometry, 4),
                "repair": round(result.confidence.repair, 4),
                "source": round(result.confidence.source, 4),
                "view": round(result.confidence.view, 4),
            },
            "notes": list(result.confidence.notes),
        },
    }


def measure_page(
    doc: Any, path: str, page_number: int, out_dir: str, dpi: int, whole_page: bool
) -> PageRow:
    """Run one page end to end and collect its row, overlay and JSON."""
    file_name = os.path.basename(path)
    stem = os.path.splitext(file_name)[0].replace(os.sep, "_")

    prepared = prepare_page(doc, page_number)
    region = None if whole_page else prepared.default_region()
    scale, warnings = region_scale(prepared, region)

    from backend.area.projected import compute_projected_area

    result = compute_projected_area(
        analysis=prepared.analysis,
        fitz_page=doc.load_page(page_number - 1),
        document_id="validate",
        file_name=file_name,
        scale=scale,
        region_bbox=region.bbox if region else None,
        view_source=ViewSource.AUTO_DETECTED if region else ViewSource.WHOLE_PAGE,
        view_label=region.label if region else "Whole page",
        extra_warnings=warnings,
    )

    overlay_path = os.path.join(out_dir, f"{stem}_p{page_number}_overlay.png")
    draw_overlay(
        doc.load_page(page_number - 1), result, prepared.analysis.primitives, dpi
    ).save(overlay_path)

    json_path = os.path.join(out_dir, f"{stem}_p{page_number}.json")
    with open(json_path, "w") as handle:
        json.dump(
            {"result": result.as_dict(), "stages": _stage_record(prepared, result)},
            handle,
            indent=2,
        )

    box = profile_bbox(result)
    bounding_mm2 = None
    bounding_w_mm = None
    bounding_h_mm = None
    utilization = None
    if box is not None and result.scale.verified:
        mm_per_unit = result.scale.mm_per_unit
        extent = bounding_extent(result)
        if extent is not None:
            bounding_w_mm = extent[0] * mm_per_unit
            bounding_h_mm = extent[1] * mm_per_unit
        bounding_mm2 = box.area * (mm_per_unit ** 2)
        if bounding_mm2 > 0 and result.area_mm2 is not None:
            utilization = result.area_mm2 / bounding_mm2

    stages = _stage_record(prepared, result)

    return PageRow(
        file_name=file_name,
        page=page_number,
        status="measured" if result.area_mm2 is not None else "refused",
        detail="" if result.area_mm2 is not None else result.as_dict()["projected_area"]["message"],
        drawing_type=result.drawing_type.value,
        view_label=result.view_label,
        scale_text=_scale_text(result),
        source="PDF",
        area_units2=result.area_units2,
        area_mm2=result.area_mm2,
        bounding_width_mm=bounding_w_mm,
        bounding_height_mm=bounding_h_mm,
        bounding_area_mm2=bounding_mm2,
        utilization=utilization,
        confidence=result.confidence.overall,
        warnings=list(result.warnings),
        assumptions=list(result.assumptions),
        stages=stages,
        interpretations=stages.get("interpretations", []),
        bodies=stages.get("bodies", []),
        overlay_path=overlay_path,
        json_path=json_path,
    )


def readable_pdf(path: str) -> Optional[str]:
    """Why this file cannot be opened, or ``None`` if it can.

    Encrypted-at-rest exports (DRM viewers, some enterprise sync clients) are
    whole-file encrypted: there is no ``%PDF`` header and no ``%PDF`` string
    anywhere in the file. That is not a damaged PDF and no amount of repair will
    open it — the file has to be exported again from the application that owns
    it. Saying exactly that is more actionable than a parser error (§31).
    """
    try:
        with open(path, "rb") as handle:
            head = handle.read(1024)
    except OSError as exc:
        return f"cannot be read from disk: {exc}"

    if head.startswith(_PDF_MAGIC):
        return None
    if _PDF_MAGIC in head:
        return None  # leading junk before the header; PyMuPDF recovers this
    preview = head[:4].hex()
    return (
        f"not a readable PDF — the file begins {preview} rather than 25504446 ('%PDF'), "
        "so it is encrypted or wrapped at rest. Open it in the application that owns "
        "it and export or 'Save As' a decrypted copy."
    )


def validate_file(
    path: str, out_dir: str, dpi: int, page: Optional[int], all_pages: bool, whole_page: bool
) -> List[PageRow]:
    """Validate one drawing; never raises, always returns at least one row."""
    file_name = os.path.basename(path)

    blocked = readable_pdf(path)
    if blocked:
        return [PageRow(file_name=file_name, page=0, status="blocked", detail=blocked)]

    try:
        doc = fitz.open(path)
    except Exception as exc:
        return [PageRow(file_name=file_name, page=0, status="blocked", detail=str(exc))]

    rows: List[PageRow] = []
    try:
        if doc.is_encrypted and not doc.authenticate(""):
            return [
                PageRow(
                    file_name=file_name,
                    page=0,
                    status="blocked",
                    detail="password-protected; supply the password and re-export",
                )
            ]

        summary = document_summary(doc, file_name)
        if all_pages:
            pages: Sequence[int] = range(1, doc.page_count + 1)
        elif page is not None:
            pages = [page]
        else:
            pages = [summary["suggested_page"]]

        for number in pages:
            try:
                rows.append(measure_page(doc, path, number, out_dir, dpi, whole_page))
            except Exception as exc:  # one bad page must not lose the whole set
                rows.append(
                    PageRow(
                        file_name=file_name,
                        page=number,
                        status="failed",
                        detail=f"{type(exc).__name__}: {exc}",
                        stages={"traceback": traceback.format_exc(limit=6)},
                    )
                )
    finally:
        doc.close()
    return rows


# ── reporting ────────────────────────────────────────────────────────────────


def _fmt_area(value: Optional[float]) -> str:
    if value is None:
        return "—"
    if value >= 1_000_000:
        return f"{value / 1_000_000:,.4f} m²"
    return f"{value:,.1f} mm²"


def _m2(value_mm2: Optional[float]) -> str:
    return "—" if value_mm2 is None else f"{Area(value_mm2).to('m2'):,.4f}"


def _major_warning(row: PageRow) -> str:
    """The single warning that most affects trust in the number."""
    if row.status == "blocked":
        return row.detail
    if row.status == "refused":
        return "scale not verified — no physical area reported"
    ambiguity = (row.stages.get("candidate_footprint") or {}).get("title_block_ambiguity")
    if ambiguity and ambiguity.get("measured_region_was_a_title_block"):
        return "measured region was classified as a title block"
    if ambiguity:
        return f"title-block ambiguity: {ambiguity['reason']}"
    if row.warnings:
        first = row.warnings[0].split("\n")[0]
        return first[:120] + ("…" if len(first) > 120 else "")
    return "none"


def summary_table(rows: Sequence[PageRow]) -> str:
    """The milestone deliverable table."""
    lines = [
        "| Drawing | Source | Selected View | Scale/Units | Projected Area m² "
        "| Bounding Area m² | Utilization | Confidence | Major Warning |",
        "| --- | --- | --- | --- | ---: | ---: | ---: | ---: | --- |",
    ]
    for row in rows:
        if row.status in ("blocked", "failed"):
            lines.append(
                f"| {row.file_name} | {row.source} | — | **{row.status.upper()}** "
                f"| — | — | — | — | {_major_warning(row)} |"
            )
            continue
        lines.append(
            f"| {row.file_name} p{row.page} | {row.source} | {row.view_label} "
            f"| {row.scale_text} | {_m2(row.area_mm2)} | {_m2(row.bounding_area_mm2)} "
            f"| {f'{row.utilization * 100:.1f} %' if row.utilization is not None else '—'} "
            f"| {f'{row.confidence * 100:.0f} %' if row.confidence is not None else '—'} "
            f"| {_major_warning(row)} |"
        )
    return "\n".join(lines)


def comparison_table(rows: Sequence[PageRow]) -> str:
    """Full per-page table, with every unit the milestone asks for."""
    columns = (
        "Drawing", "Page", "Detected Scale/Units", "Projected Area", "native units²",
        "m²", "ft²", "Bounding W×H", "Bounding Area", "Utilization", "Confidence",
        "Warnings",
    )
    align = ("---", "---:", "---", "---:", "---:", "---:", "---:", "---:", "---:",
             "---:", "---:", "---")
    lines = ["| " + " | ".join(columns) + " |", "| " + " | ".join(align) + " |"]

    for row in rows:
        if row.status in ("blocked", "failed"):
            cells = [row.file_name, str(row.page or "—"), f"**{row.status.upper()}**"]
            cells += ["—"] * 8 + [row.detail]
            lines.append("| " + " | ".join(cells) + " |")
            continue

        area = Area(row.area_mm2) if row.area_mm2 is not None else None
        warnings = f"{len(row.warnings)}" if row.warnings else "none"
        if row.status == "refused":
            warnings += " · scale not verified"

        cells = [
            row.file_name,
            str(row.page),
            row.scale_text,
            _fmt_area(row.area_mm2),
            f"{row.area_units2:,.2f}" if row.area_units2 is not None else "—",
            f"{area.to('m2'):,.4f}" if area else "—",
            f"{area.to('ft2'):,.2f}" if area else "—",
            (
                f"{row.bounding_width_mm:,.1f} × {row.bounding_height_mm:,.1f} mm"
                if row.bounding_width_mm is not None
                else "—"
            ),
            _fmt_area(row.bounding_area_mm2),
            f"{row.utilization * 100:.1f} %" if row.utilization is not None else "—",
            f"{row.confidence * 100:.0f} %" if row.confidence is not None else "—",
            warnings,
        ]
        lines.append("| " + " | ".join(cells) + " |")
    return "\n".join(lines)


def interpretation_table(row: PageRow) -> str:
    """Every defensible reading of this drawing's projected area, side by side.

    The milestone's central question — *what physical region does this number
    represent?* — has more than one answer on a line-layout sheet, so the answers
    are shown together rather than one being chosen silently (§30).
    """
    if not row.interpretations:
        return "_No geometry was reconstructed, so there is no region to interpret._"

    lines = [
        "| Interpretation | Area m² | Area ft² | vs union | What physical region it is |",
        "| --- | ---: | ---: | ---: | --- |",
    ]
    union = next(
        (i for i in row.interpretations if i["type"] == "equipment_union"), None
    )
    base = (union or {}).get("area_units2") or 0.0
    for item in row.interpretations:
        mm2 = item["area_mm2"]
        area = Area(mm2) if mm2 is not None else None
        ratio = (
            f"{item['area_units2'] / base * 100:.1f} %"
            if base > 0 and item["area_units2"] is not None
            else "—"
        )
        square_metres = f"{area.to('m2'):,.4f}" if area else "—"
        square_feet = f"{area.to('ft2'):,.2f}" if area else "—"
        lines.append(
            f"| **{item['name']}** | {square_metres} | {square_feet} "
            f"| {ratio} | {item['means']} |"
        )

    pending = row.stages.get("pending_interpretations") or []
    if pending:
        lines += [
            "",
            "Readings the product intends to support but that geometry alone cannot "
            "supply — reported as known and unavailable rather than omitted:",
            "",
            "| Reading | Needs | What it would be |",
            "| --- | --- | --- |",
        ]
        for item in pending:
            lines.append(f"| {item['name']} | {item['requires']} | {item['means']} |")
    return "\n".join(lines)


def detail_section(row: PageRow) -> str:
    """Per-drawing evidence: every stage, every warning, every assumption."""
    out = [f"### {row.file_name} — page {row.page or '—'}", ""]
    if row.status in ("blocked", "failed"):
        out += [f"**{row.status.upper()}** — {row.detail}", ""]
        return "\n".join(out)

    stages = row.stages
    geometry = stages["geometry_extraction"]
    union = stages["union_polygon"]
    out += [
        f"- **1 ingestion** — {stages['source']['drawing_type']}, "
        f"{stages['source']['page_size_pt'][0]} × {stages['source']['page_size_pt'][1]} pt, "
        f"rotation {stages['source']['rotation']}, sheet unit "
        f"{stages['source']['sheet_unit'] or 'not declared'}, printed scale "
        f"{stages['source']['printed_scale'] or 'none found'}",
        f"- **2 scale / units** — {stages['scale']['source']} "
        f"({'verified' if stages['scale']['verified'] else 'NOT verified'}), "
        f"{stages['scale']['candidates']} candidate(s) from "
        f"{geometry['dimension_texts']} dimension annotation(s); {stages['scale']['detail']}",
        f"- **3 region detection** — measured {row.view_label!r} "
        f"({stages['candidate_footprint']['source']}) of "
        f"{len(stages['candidate_footprint']['regions'])} regions "
        f"{stages['candidate_footprint']['regions_by_kind']}",
        f"- **4 classification** — {geometry['primitives']} primitives: "
        f"{geometry['counted_primitives']} counted, {geometry['excluded_primitives']} excluded, "
        f"{geometry['unresolved_primitives']} unresolved · by role {geometry['role_counts']} "
        f"· {geometry['text_spans']} text spans",
        f"- **5 area construction** — {union['components']} component(s), "
        f"{union['outer_contours']} outer ring(s), {union['holes']} hole(s), "
        f"{union['faces']} face(s), method {union['method']}, "
        f"{len(union['repairs'])} repair(s)",
        f"- **6 results** — {_fmt_area(row.area_mm2)}"
        + (f" ({row.detail})" if row.status == "refused" else "")
        + (
            f" · bounding {row.bounding_width_mm:,.1f} × {row.bounding_height_mm:,.1f} mm"
            if row.bounding_width_mm is not None
            else ""
        )
        + (f" · utilization {row.utilization * 100:.1f} %" if row.utilization is not None else ""),
        f"- **confidence** — {stages['confidence']['overall'] * 100:.0f} % "
        f"({stages['confidence']['band']}) {stages['confidence']['components']}",
        f"- **7 visual QA** — `{row.overlay_path}`",
        "",
    ]
    if stages["candidate_footprint"]["regions"]:
        out += [
            "| Region | Kind | Size pt | Measured |",
            "| --- | --- | ---: | :-: |",
        ]
        for region in stages["candidate_footprint"]["regions"]:
            measured = "✓" if region["label"] == row.view_label else ""
            out.append(
                f"| {region['id']} {region['label']} | {region['kind']} "
                f"| {region['width_pt']:,.0f} × {region['height_pt']:,.0f} | {measured} |"
            )
        out.append("")
    out += ["**What physical region is this?**", "", interpretation_table(row), ""]

    if row.bodies and len(row.bodies) > 1:
        out += [
            f"**Bodies found ({len(row.bodies)})** — several disconnected silhouettes may "
            "be one installation or several; listed so any subset can be summed.",
            "",
            "| Body | Included | Area m² | W × H mm | Holes |",
            "| --- | --- | ---: | ---: | ---: |",
        ]
        for body in row.bodies[:12]:
            body_m2 = _m2(body["area_mm2"])
            out.append(
                f"| {body['id']} | {'yes' if body['included'] else 'no'} | {body_m2} "
                f"| {body['width_units']:,.1f} × {body['height_units']:,.1f} (units) "
                f"| {body['hole_count']} |"
            )
        if len(row.bodies) > 12:
            out.append(f"| … {len(row.bodies) - 12} more | | | | |")
        out.append("")

    ambiguity = stages["candidate_footprint"].get("title_block_ambiguity")
    if ambiguity:
        out += [
            "**⚠ Title-block ambiguity**",
            "",
            f"- {ambiguity['reason']}",
            f"- measured region was itself classified a title block: "
            f"**{ambiguity['measured_region_was_a_title_block']}**",
        ]
        for finding in ambiguity["regions"]:
            out.append(
                f"- `{finding['region_id']}` {finding['label']}: "
                f"{finding['primitives_inside']} primitives, ink ratio "
                f"{finding['ink_ratio']}, {finding['area_share_of_page'] * 100:.1f} % of the page"
            )
        out += [f"- {ambiguity['note']}", ""]

    if union["repairs"]:
        out.append("**Repairs**")
        out += [f"- {r['type']} ×{r['count']}: {r['detail']}" for r in union["repairs"]]
        out.append("")
    if row.assumptions:
        out.append("**Assumptions**")
        out += [f"- {a}" for a in row.assumptions]
        out.append("")
    if row.warnings:
        out.append("**Warnings**")
        out += [f"- {w}" for w in row.warnings]
        out.append("")
    return "\n".join(out)


def build_report(rows: Sequence[PageRow], out_dir: str) -> str:
    stamp = _datetime.datetime.now().strftime("%Y-%m-%d %H:%M")
    measured = [r for r in rows if r.status == "measured"]
    refused = [r for r in rows if r.status == "refused"]
    blocked = [r for r in rows if r.status in ("blocked", "failed")]

    out = [
        "# Real-drawing validation",
        "",
        "**Overlay legend** — every overlay PNG uses one palette (§8):",
        "",
        "| Colour | Meaning |",
        "| --- | --- |",
        "| grey, thin | source geometry that was **excluded** — dimensions, annotation, sheet frame, hatching |",
        "| orange, thin | **unresolved** geometry: counted, but the classifier was unsure |",
        "| green outline + fill | **included** geometry — the final projected-area boundary |",
        "| red outline + fill | **holes** subtracted from the area |",
        "| amber dashed box | the **selected view / region** that was measured |",
        "",
        "Geometry drawn in no colour at all was not seen by the engine — that is",
        "itself a finding, and the overlay is the place it shows up.",
        "",
        f"Generated {stamp} · {len(rows)} page(s): "
        f"{len(measured)} measured, {len(refused)} refused for want of a verified scale, "
        f"{len(blocked)} blocked.",
        "",
        "Every number below is produced by the same backend the viewer calls, and every",
        "row links an overlay PNG showing exactly which geometry was counted (§8).",
        "",
        "## Summary",
        "",
        summary_table(rows),
        "",
        "## All units and bounding extents",
        "",
        comparison_table(rows),
        "",
        "## Evidence per drawing",
        "",
        "Each drawing below is validated stage by stage: ingestion, scale and units,",
        "view/region detection, geometry classification, area construction, results,",
        "and the overlay for visual QA.",
        "",
    ]
    out += [detail_section(row) for row in rows]

    if blocked:
        out += [
            "## Blocked",
            "",
            "These files could not be opened at all, so nothing about them has been",
            "measured, estimated or inferred:",
            "",
        ]
        out += [f"- **{r.file_name}** — {r.detail}" for r in blocked]
        out.append("")

    report = "\n".join(out)
    with open(os.path.join(out_dir, "REPORT.md"), "w") as handle:
        handle.write(report)
    return report


def collect_pdfs(targets: Sequence[str]) -> List[str]:
    """Expand directories into the PDFs inside them, keeping explicit files."""
    paths: List[str] = []
    for target in targets:
        if os.path.isdir(target):
            for name in sorted(os.listdir(target)):
                if name.lower().endswith(".pdf"):
                    paths.append(os.path.join(target, name))
        else:
            paths.append(target)
    return paths


def main(argv: Optional[List[str]] = None) -> int:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument("targets", nargs="+", help="PDF files, or directories of them")
    parser.add_argument("--out-dir", default="validation", help="where overlays and JSON land")
    parser.add_argument("--page", type=int, help="measure this page of every drawing")
    parser.add_argument("--all-pages", action="store_true", help="measure every page")
    parser.add_argument("--whole-page", action="store_true", help="skip region detection")
    parser.add_argument("--dpi", type=int, default=150, help="overlay resolution")
    args = parser.parse_args(argv)

    paths = collect_pdfs(args.targets)
    if not paths:
        parser.error(f"no PDFs found in {args.targets}")
    os.makedirs(args.out_dir, exist_ok=True)

    rows: List[PageRow] = []
    for path in paths:
        rows.extend(
            validate_file(path, args.out_dir, args.dpi, args.page, args.all_pages, args.whole_page)
        )
        for row in rows[-1:]:
            marker = {"measured": "ok", "refused": "no scale", "blocked": "BLOCKED", "failed": "FAILED"}
            print(f"[{marker.get(row.status, row.status):>8}] {row.file_name} p{row.page}")

    print()
    print(comparison_table(rows))
    build_report(rows, args.out_dir)
    print(f"\nreport    {os.path.join(args.out_dir, 'REPORT.md')}")

    blocked = [r for r in rows if r.status == "blocked"]
    if blocked:
        print(f"\n{len(blocked)} file(s) could not be opened:")
        for row in blocked:
            print(f"  {row.file_name}: {row.detail}")
    # Blocked inputs are a fact about the files, not a failure of this run.
    return 1 if any(r.status == "failed" for r in rows) else 0


if __name__ == "__main__":
    raise SystemExit(main())
