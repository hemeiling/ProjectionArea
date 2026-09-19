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

from backend.models import AreaResult, BBox, ViewSource
from backend.pdf.document import document_summary
from backend.pipeline import prepare_page, region_scale
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
    area_mm2: Optional[float] = None
    bounding_area_mm2: Optional[float] = None
    utilization: Optional[float] = None
    confidence: Optional[float] = None
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
                {"id": r.id, "label": r.label, "kind": r.kind} for r in prepared.regions
            ],
            "measured": result.view_label,
            "source": result.view_source.value,
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
        },
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
    utilization = None
    if box is not None and result.scale.verified:
        bounding_mm2 = box.area * (result.scale.mm_per_unit ** 2)
        if bounding_mm2 > 0 and result.area_mm2 is not None:
            utilization = result.area_mm2 / bounding_mm2

    return PageRow(
        file_name=file_name,
        page=page_number,
        status="measured" if result.area_mm2 is not None else "refused",
        detail="" if result.area_mm2 is not None else result.as_dict()["projected_area"]["message"],
        drawing_type=result.drawing_type.value,
        view_label=result.view_label,
        scale_text=_scale_text(result),
        area_mm2=result.area_mm2,
        bounding_area_mm2=bounding_mm2,
        utilization=utilization,
        confidence=result.confidence.overall,
        warnings=list(result.warnings),
        assumptions=list(result.assumptions),
        stages=_stage_record(prepared, result),
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


def comparison_table(rows: Sequence[PageRow]) -> str:
    """The requested comparison table, as GitHub-flavoured Markdown."""
    header = (
        "| Drawing | Page | Detected Scale/Units | Projected Area | Bounding Area "
        "| Utilization | Confidence | Warnings |\n"
        "| --- | ---: | --- | ---: | ---: | ---: | ---: | --- |"
    )
    lines = [header]
    for row in rows:
        if row.status in ("blocked", "failed"):
            lines.append(
                f"| {row.file_name} | {row.page or '—'} | **{row.status.upper()}** | — | — | — | — "
                f"| {row.detail} |"
            )
            continue
        warnings = f"{len(row.warnings)}" if row.warnings else "none"
        if row.status == "refused":
            warnings = f"{warnings} · scale not verified"
        lines.append(
            f"| {row.file_name} | {row.page} | {row.scale_text} | {_fmt_area(row.area_mm2)} "
            f"| {_fmt_area(row.bounding_area_mm2)} "
            f"| {f'{row.utilization * 100:.1f} %' if row.utilization is not None else '—'} "
            f"| {f'{row.confidence * 100:.0f} %' if row.confidence is not None else '—'} "
            f"| {warnings} |"
        )
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
        f"- **source** — {stages['source']['drawing_type']}, "
        f"{stages['source']['page_size_pt'][0]} × {stages['source']['page_size_pt'][1]} pt, "
        f"rotation {stages['source']['rotation']}, sheet unit {stages['source']['sheet_unit'] or 'not declared'}",
        f"- **geometry extraction** — {geometry['primitives']} primitives "
        f"{geometry['role_counts']}, {geometry['dimension_texts']} dimension annotations",
        f"- **scale** — {stages['scale']['source']} ({'verified' if stages['scale']['verified'] else 'NOT verified'}); "
        f"{stages['scale']['detail']}",
        f"- **candidate footprint** — measured {row.view_label!r} "
        f"({stages['candidate_footprint']['source']}) of "
        f"{len(stages['candidate_footprint']['regions'])} candidate regions",
        f"- **union polygon** — {union['components']} component(s), "
        f"{union['outer_contours']} outer, {union['holes']} hole(s), method {union['method']}",
        f"- **projected area** — {_fmt_area(row.area_mm2)}"
        + (f" ({row.detail})" if row.status == "refused" else ""),
        f"- **bounding area / utilization** — {_fmt_area(row.bounding_area_mm2)}"
        + (f" · {row.utilization * 100:.1f} %" if row.utilization is not None else ""),
        f"- **confidence** — {stages['confidence']['overall'] * 100:.0f} % "
        f"({stages['confidence']['band']}) {stages['confidence']['components']}",
        f"- **overlay** — `{row.overlay_path}`",
        "",
    ]
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
        f"Generated {stamp} · {len(rows)} page(s): "
        f"{len(measured)} measured, {len(refused)} refused for want of a verified scale, "
        f"{len(blocked)} blocked.",
        "",
        "Every number below is produced by the same backend the viewer calls, and every",
        "row links an overlay PNG showing exactly which geometry was counted (§8).",
        "",
        "## Comparison",
        "",
        comparison_table(rows),
        "",
        "## Evidence per drawing",
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
