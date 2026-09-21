"""Golden production baselines — the correctness oracle for future optimisation.

    .venv/bin/python -m tools.baseline capture Inputs/*.dwg Inputs/*.pdf
    .venv/bin/python -m tools.baseline compare Inputs/101-....dwg
    .venv/bin/python -m tools.baseline compare --via-app Inputs/101-....dwg

The plan in docs/CAD_PERFORMANCE_ROADMAP.md rests on one rule: the current
implementation is the reference, and an optimised implementation must reproduce
it. That rule is worth nothing without a record precise enough to catch a subtle
regression, which is what this writes.

``capture`` runs a drawing through the engine and records everything needed to
detect a change later — source identity, CAD provenance, every count, every
reading, the geometry's extent, warnings, semantics and what the engine could not
resolve. ``compare`` re-runs the same drawing and reports every difference.

Counts are compared **exactly**. Areas are compared within a relative tolerance,
because reordering floating-point geometry operations legitimately moves the last
bits and nothing else should. A difference outside that is a finding to
investigate, never a price worth paying for speed.

Baselines are written to ``baselines/`` and are **not** committed: they are derived
from customer drawings, which are not in this repository either. Keep them
alongside the drawings.
"""

from __future__ import annotations

import hashlib
import json
import os
import platform
import resource
import subprocess
import sys
import time
from typing import Any, Dict, List, Optional, Tuple

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
BASELINE_DIR = os.path.join(PROJECT_ROOT, "baselines")

#: Areas are floating-point geometry. Reordering the same operations can move the
#: last bits; anything larger is a real change in what was measured. One part per
#: billion is far tighter than any tolerance in the geometry engine itself, and
#: still loose enough not to fail on a legitimate re-association.
AREA_RELATIVE_TOLERANCE = 1e-9

#: Fields that must agree exactly. A component or hole appearing or vanishing is a
#: topology change, whatever it does to the area.
EXACT_FIELDS = (
    "source_sha256", "units", "primitive_count", "profile_primitive_count",
    "ignored_primitive_count", "segment_count", "face_count", "component_count",
    "outer_contour_count", "hole_count", "layer_count", "layers_with_entities",
    "block_count", "blocks_inserted", "entity_counts", "unsupported_counts",
    "text_count", "dimension_count", "layouts", "xrefs", "scale_source",
    "scale_verified", "semantic_status", "warnings", "repairs", "role_counts",
    "footprint_types", "engine_version",
)


def _sha256(path: str) -> str:
    digest = hashlib.sha256()
    with open(path, "rb") as handle:
        for chunk in iter(lambda: handle.read(1 << 20), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _peak_rss_mb() -> float:
    raw = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
    return round((raw if sys.platform == "darwin" else raw * 1024) / 1e6, 1)


def _measure(path: str) -> Dict[str, Any]:
    """Run one drawing through the engine and record the reference result.

    Ingest, prepare the page, resolve the scale, compute the area — the same steps
    as ``POST /api/analyse``, by **calling the route's own functions** rather than
    reproducing them. An earlier version of this file resolved the scale itself and
    silently lost the CAD-declared units, so two DWGs that the application measures
    to the square metre were recorded here as "scale not verified". A baseline that
    does not match the application is worse than none, so the shared step is
    imported, not imitated.
    """
    from backend.api.routes import _detect_kind, _resolve_scale
    from backend.api.schemas import ScaleSpec
    from backend.area.projected import compute_projected_area
    from backend.models import ViewSource
    from backend.store import DocumentStore

    with open(path, "rb") as handle:
        head = handle.read(4096)
    file_name = os.path.basename(path)
    kind = _detect_kind(head, file_name)

    store = DocumentStore()
    started = time.time()
    spooled = store.spool_path(".upload")
    with open(path, "rb") as source, open(spooled, "wb") as target:
        for chunk in iter(lambda: source.read(1 << 20), b""):
            target.write(chunk)

    if kind == "dwg":
        stored = store.adopt_dwg(spooled, file_name)
    elif kind == "dxf":
        stored = store.adopt_cad(spooled, file_name)
    else:
        stored = store.adopt_pdf(spooled, file_name)
    ingested = time.time()

    _stored, prepared = store.prepared_page(stored.id, 1)
    region = prepared.default_region()
    # The route's resolution, which prefers a verified CAD-declared scale over
    # anything measuring the linework could derive.
    scale, scale_warnings = _resolve_scale(
        prepared, region, ScaleSpec(mode="auto"), stored)
    result = compute_projected_area(
        analysis=prepared.analysis,
        fitz_page=stored.doc.load_page(0) if stored.doc is not None else None,
        document_id="baseline",
        file_name=file_name,
        scale=scale,
        region_bbox=region.bbox if region else None,
        view_source=ViewSource.WHOLE_PAGE,
        view_label="Whole page",
        extra_warnings=scale_warnings,
    )
    finished = time.time()

    record = _build_record(
        path, file_name, kind, result.as_dict(),
        stored.cad.summary() if stored.cad is not None else None,
        stored.conversion.as_dict() if stored.conversion is not None else None,
        _bounds(result),
        {
            "ingest_seconds": round(ingested - started, 2),
            "analyse_seconds": round(finished - ingested, 2),
            "total_seconds": round(finished - started, 2),
            "peak_rss_mb": _peak_rss_mb(),
        },
    )
    store.shutdown()
    return record


def _build_record(
    path: str, file_name: str, kind: str, payload: Dict[str, Any],
    info: Optional[Dict[str, Any]], conversion: Optional[Dict[str, Any]],
    bounds: Optional[Dict[str, float]], cost: Dict[str, Any],
) -> Dict[str, Any]:
    """The baseline record, from the engine's own serialised result.

    Built from dictionaries — the result's ``as_dict``, the CAD summary, the
    conversion record — so the same builder serves both a direct engine run and
    a run through the application, where only the serialised form comes back.
    """
    geometry = payload["geometry"]
    record: Dict[str, Any] = {
        # ── identity ────────────────────────────────────────────────────────
        "file_name": file_name,
        "source_kind": kind,
        "source_sha256": _sha256(path),
        "source_bytes": os.path.getsize(path),
        "engine_version": payload["engine_version"],

        # ── CAD provenance ──────────────────────────────────────────────────
        "units": None,
        "extents": None,
        "layer_count": None,
        "layers_with_entities": None,
        "layer_entity_counts": None,
        "block_count": None,
        "blocks_inserted": None,
        "block_insert_counts": None,
        "entity_counts": None,
        "unsupported_counts": None,
        "text_count": None,
        "dimension_count": None,
        "layouts": None,
        "xrefs": None,
        "conversion": None,

        # ── normalised geometry ─────────────────────────────────────────────
        "primitive_count": geometry.get("raw_primitives"),
        "profile_primitive_count": geometry.get("profile_primitives"),
        "ignored_primitive_count": geometry.get("ignored_primitives"),
        "segment_count": geometry.get("segments"),
        "face_count": geometry.get("faces"),
        "component_count": geometry.get("components"),
        "outer_contour_count": geometry.get("outer_contours"),
        "hole_count": geometry.get("holes"),
        "role_counts": geometry.get("role_counts"),
        "repairs": geometry.get("repairs"),

        # ── scale and meaning ───────────────────────────────────────────────
        "scale_source": payload["scale"]["source"],
        "scale_verified": payload["scale"]["verified"],
        "scale_mm_per_unit": payload["scale"]["mm_per_unit"],
        "semantic_status": sorted(
            {i["semantics"] for i in payload["footprint_interpretations"]}),
        "warnings": sorted(payload["warnings"]),
        "assumptions": sorted(payload["assumptions"]),
        "confidence": payload["confidence"],

        # ── the readings themselves ─────────────────────────────────────────
        "footprint_types": [i["type"] for i in payload["footprint_interpretations"]],
        "footprints": [
            {
                "type": i["type"],
                "semantics": i["semantics"],
                "area_units2": i["area_units2"],
                "area_mm2": i["area_mm2"],
                "confidence": i["confidence"],
                "requires_cad_semantics": i["requires_cad_semantics"],
                "warnings": sorted(i["warnings"]),
            }
            for i in payload["footprint_interpretations"]
        ],
        "pending_interpretations": payload.get("pending_interpretations"),
        "projected_area": payload["projected_area"],
        "area_pdf_units2": payload["area_pdf_units2"],

        # ── bounding geometry, as a cheap whole-drawing fingerprint ─────────
        "bounds": bounds,

        # ── cost, for the roadmap's benchmarks ──────────────────────────────
        "cost": cost,
        "captured_on": {
            "platform": platform.platform(),
            "machine": platform.machine(),
            "python": platform.python_version(),
        },
    }

    if info is not None:
        record.update({
            "units": info.get("units"),
            "extents": info.get("extents"),
            "layer_count": len(info.get("layers") or []),
            "layers_with_entities": sum(
                1 for layer in (info.get("layers") or []) if layer.get("entity_count")),
            "layer_entity_counts": {
                layer["name"]: layer.get("entity_count", 0)
                for layer in (info.get("layers") or []) if layer.get("entity_count")
            },
            "block_count": len(info.get("blocks") or []),
            "blocks_inserted": sum(
                1 for block in (info.get("blocks") or []) if block.get("insert_count")),
            "block_insert_counts": {
                block["name"]: block.get("insert_count", 0)
                for block in (info.get("blocks") or []) if block.get("insert_count")
            },
            "entity_counts": info.get("entity_counts"),
            "unsupported_counts": info.get("unsupported_counts"),
            "text_count": info.get("text_count"),
            "dimension_count": info.get("dimension_count"),
            "layouts": info.get("layouts"),
            "xrefs": info.get("xrefs"),
        })
    if conversion is not None:
        as_dict = conversion
        record["conversion"] = {
            "source_type": as_dict["source_type"],
            "source_sha256": as_dict["source_sha256"],
            "dwg_signature": as_dict["dwg_signature"],
            "dwg_version": as_dict["dwg_version"],
            "tool": as_dict["tool"],
            "tool_version": as_dict["tool_version"],
            "intermediate_dxf_sha256": as_dict["intermediate_dxf_sha256"],
            "intermediate_dxf_bytes": as_dict["intermediate_dxf_bytes"],
            "warnings": sorted(as_dict["warnings"]),
            "metadata_warnings": sorted(as_dict["metadata_warnings"]),
            # Duration deliberately excluded: it is a property of the machine.
        }
    return record


#: Coordinates in the application's payload are rounded to two decimals
#: (``FootprintInterpretation.as_dict``), so bounds read back from it can differ
#: from the engine's own by up to half of that. Areas are not rounded.
PAYLOAD_COORDINATE_TOLERANCE = 0.005


def _bounds_from_payload(payload: Dict[str, Any]) -> Optional[Dict[str, float]]:
    box = None
    for interpretation in payload.get("footprint_interpretations") or []:
        for ring in interpretation.get("outer") or []:
            for x, y in ring:
                if box is None:
                    box = [x, y, x, y]
                else:
                    box = [min(box[0], x), min(box[1], y), max(box[2], x), max(box[3], y)]
    if box is None:
        return None
    return {"x0": box[0], "y0": box[1], "x1": box[2], "y1": box[3]}


def _measure_via_app(path: str) -> Dict[str, Any]:
    """Measure through the application itself: upload, job, analysis process.

    The direct measurement above proves the engine; this proves what the operator
    gets — the same engine run in the supervised child process, its result carried
    back across the process boundary and serialised by the API. No database: the
    run must not be substituted by, or recorded to, a saved analysis.
    """
    os.environ["DATABASE_URL"] = ""
    os.environ.setdefault("ANALYSIS_ISOLATION", "process")
    import backend.main as application

    application.load_local_env = lambda *args, **kwargs: None
    from fastapi.testclient import TestClient

    file_name = os.path.basename(path)
    started = time.time()
    with TestClient(application.app) as client, open(path, "rb") as handle:
        response = client.post("/api/analyse?reanalyse=true",
                               files={"file": (file_name, handle, "application/octet-stream")})
        if response.status_code != 202:
            raise RuntimeError(f"upload refused: HTTP {response.status_code}")
        job_id = response.json()["job_id"]
        while True:
            snap = client.get(f"/api/jobs/{job_id}").json()
            if snap["state"] != "running":
                break
            time.sleep(1.0)
    if snap["state"] != "done":
        raise RuntimeError(f"analysis failed: {json.dumps(snap.get('error'))[:500]}")
    payload = snap["result"]
    document = payload["document"]
    info = document.get("cad")
    conversion = (info or {}).get("conversion")
    peaks = [d.get("memory_peak_bytes") or 0 for d in snap.get("diagnostics") or []]
    peak = max(peaks + [((snap.get("resources") or {}).get("peak_bytes") or 0)])
    return _build_record(
        path, file_name, document.get("source_kind", "pdf"), payload["area"],
        info, conversion, _bounds_from_payload(payload["area"]),
        {"total_seconds": round(time.time() - started, 2),
         "analyse_seconds": snap.get("elapsed_seconds"),
         "peak_rss_mb": round(peak / 1e6, 1) if peak else None,
         "via": "application (analysis process)"},
    )


def _bounds(result: Any) -> Optional[Dict[str, float]]:
    """The extent of the measured profile, as a whole-drawing fingerprint.

    Cheap to compare and sensitive to geometry going missing at an edge, which a
    total area can hide when the lost piece is small.
    """
    box = None
    for interpretation in result.footprint_interpretations:
        for ring in getattr(interpretation, "outer", None) or []:
            for x, y in ring:
                if box is None:
                    box = [x, y, x, y]
                else:
                    box[0] = min(box[0], x)
                    box[1] = min(box[1], y)
                    box[2] = max(box[2], x)
                    box[3] = max(box[3], y)
    if box is None:
        return None
    return {"x0": round(box[0], 6), "y0": round(box[1], 6),
            "x1": round(box[2], 6), "y1": round(box[3], 6)}


# ── comparison ───────────────────────────────────────────────────────────────


def _close(a: Any, b: Any) -> bool:
    if a is None or b is None:
        return a is b or a == b
    if a == b:
        return True
    try:
        scale = max(abs(float(a)), abs(float(b)))
        return abs(float(a) - float(b)) <= AREA_RELATIVE_TOLERANCE * max(scale, 1.0)
    except (TypeError, ValueError):
        return False


def _diff(baseline: Dict[str, Any], current: Dict[str, Any]) -> List[str]:
    """Every engineering difference between a baseline and a fresh run."""
    problems: List[str] = []
    via_app = (current.get("cost") or {}).get("via") is not None

    for field in EXACT_FIELDS:
        was, now = baseline.get(field), current.get(field)
        if was != now:
            problems.append(f"{field}: {was!r} -> {now!r}")

    # Areas, within tolerance, per reading rather than in aggregate.
    by_type_was = {f["type"]: f for f in baseline.get("footprints") or []}
    by_type_now = {f["type"]: f for f in current.get("footprints") or []}
    for kind in sorted(set(by_type_was) | set(by_type_now)):
        was, now = by_type_was.get(kind), by_type_now.get(kind)
        if was is None or now is None:
            problems.append(f"footprint {kind}: {'added' if was is None else 'removed'}")
            continue
        for field in ("area_mm2", "area_units2", "confidence"):
            if not _close(was.get(field), now.get(field)):
                problems.append(f"footprint {kind}.{field}: {was.get(field)!r} -> {now.get(field)!r}")
        for field in ("semantics", "requires_cad_semantics", "warnings"):
            if was.get(field) != now.get(field):
                problems.append(f"footprint {kind}.{field}: {was.get(field)!r} -> {now.get(field)!r}")

    was_bounds, now_bounds = baseline.get("bounds"), current.get("bounds")
    if (was_bounds is None) != (now_bounds is None):
        problems.append(f"bounds: {was_bounds!r} -> {now_bounds!r}")
    elif was_bounds:
        for edge in ("x0", "y0", "x1", "y1"):
            close = (abs(was_bounds[edge] - now_bounds[edge]) <= PAYLOAD_COORDINATE_TOLERANCE
                     if via_app else _close(was_bounds[edge], now_bounds[edge]))
            if not close:
                problems.append(
                    f"bounds.{edge}: {was_bounds[edge]!r} -> {now_bounds[edge]!r}")

    if baseline.get("conversion") and current.get("conversion"):
        for field in ("dwg_signature", "dwg_version", "intermediate_dxf_sha256",
                      "intermediate_dxf_bytes", "warnings", "metadata_warnings"):
            was = baseline["conversion"].get(field)
            now = current["conversion"].get(field)
            if was != now:
                problems.append(f"conversion.{field}: {was!r} -> {now!r}")
    return problems


def _baseline_path(path: str) -> str:
    return os.path.join(BASELINE_DIR, os.path.basename(path) + ".baseline.json")


def capture(paths: List[str]) -> int:
    os.makedirs(BASELINE_DIR, exist_ok=True)
    for path in paths:
        print(f"capturing {os.path.basename(path)} …", flush=True)
        record = _run_isolated(path)
        if record is None:
            print("  FAILED — no baseline written")
            continue
        target = _baseline_path(path)
        with open(target, "w") as handle:
            json.dump(record, handle, indent=2, ensure_ascii=False, sort_keys=True)
        cost = record["cost"]
        print(f"  {record['component_count']} components, "
              f"{record['hole_count']} holes, {cost['total_seconds']}s, "
              f"{cost['peak_rss_mb']} MB -> {os.path.relpath(target, PROJECT_ROOT)}")
    return 0


def compare(paths: List[str], via_app: bool = False) -> int:
    failures = 0
    for path in paths:
        target = _baseline_path(path)
        if not os.path.exists(target):
            print(f"{os.path.basename(path)}: no baseline — run `capture` first")
            failures += 1
            continue
        with open(target) as handle:
            baseline = json.load(handle)
        print(f"comparing {os.path.basename(path)} …", flush=True)
        current = _run_isolated(path, via_app=via_app)
        if current is None:
            print("  FAILED to run")
            failures += 1
            continue
        problems = _diff(baseline, current)
        if problems:
            failures += 1
            print(f"  {len(problems)} DIFFERENCE(S) — investigate, do not accept:")
            for problem in problems:
                print(f"    {problem}")
        else:
            was = baseline["cost"]["total_seconds"]
            now = current["cost"]["total_seconds"]
            was_rss = baseline["cost"]["peak_rss_mb"]
            now_rss = current["cost"]["peak_rss_mb"]
            print(f"  identical engineering result · {was}s -> {now}s · "
                  f"{was_rss} MB -> {now_rss} MB")
    return 1 if failures else 0


def _run_isolated(path: str, via_app: bool = False) -> Optional[Dict[str, Any]]:
    """Measure in a fresh subprocess, so peak RSS belongs to this drawing alone."""
    proc = subprocess.run(
        [sys.executable, "-m", "tools.baseline", "--one-app" if via_app else "--one", path],
        capture_output=True, text=True, cwd=PROJECT_ROOT,
    )
    for line in proc.stdout.splitlines():
        if line.startswith("__RECORD__"):
            return json.loads(line[len("__RECORD__"):])
    sys.stderr.write(proc.stdout[-2000:] + proc.stderr[-2000:])
    return None


def main(argv: List[str]) -> int:
    if not argv:
        print(__doc__)
        return 2
    if argv[0] == "--one":
        print("__RECORD__" + json.dumps(_measure(argv[1]), ensure_ascii=False))
        return 0
    if argv[0] == "--one-app":
        print("__RECORD__" + json.dumps(_measure_via_app(argv[1]), ensure_ascii=False))
        return 0
    command, paths = argv[0], argv[1:]
    via_app = "--via-app" in paths
    paths = [p for p in paths if p != "--via-app"]
    if command == "capture":
        return capture(paths)
    if command == "compare":
        return compare(paths, via_app=via_app)
    print(f"unknown command {command!r}; expected capture or compare")
    return 2


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
