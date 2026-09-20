"""Saving, finding and reopening a completed analysis.

The expensive thing this application does is measure a drawing: eight minutes and
eleven gigabytes for the largest production DWG. Having done it once, doing it
again because a browser was refreshed is waste. This module keeps the result so it
can be reopened.

Two rules govern it.

**Persistence is observational.** Nothing here participates in a measurement. A
result is computed, and then — separately, afterwards — recorded. A save that
fails leaves the analysis exactly as correct as it was; the operator is told the
save failed, not that the measurement did.

**A saved analysis is only reusable by the algorithm that made it.** The cache key
is the source's hash *and* the versions of everything that materially decides the
answer. A drawing re-analysed after a tolerance change is a different question, and
handing back the old answer would be presenting a stale number as a current one
(§3). The source hash alone would do exactly that.

The original drawing is **not** stored (§35). What is kept is its name, its hash,
its size, and what the engine concluded — enough to audit a result, not enough to
reconstruct a customer's property.
"""

from __future__ import annotations

import hashlib
import json
import logging
import uuid
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

from backend.config import ENGINE_VERSION, INTERPRETATION_VERSION, config_fingerprint
from backend.db import config, pool
from backend.db.artifacts import (
    VIEWER_RESULT,
    ArtifactStore,
    PostgresArtifactStore,
    StoredArtifact,
    encode,
)

logger = logging.getLogger("projected_area.db")

STATUS_COMPLETED = "completed"
STATUS_SUPERSEDED = "superseded"

EVENT_CREATED = "created"
EVENT_RENAMED = "renamed"
EVENT_RECALCULATED = "recalculated"
EVENT_RECALIBRATED = "recalibrated"

#: Columns for a listing. Deliberately excludes every JSONB column and the
#: artifact: "Recent Analyses" must not fetch 45 MB per row to draw a list.
_SUMMARY_COLUMNS = """
    id, source_sha256, original_filename, display_name, source_type,
    source_size_bytes, status, created_at, completed_at, updated_at,
    engine_version, interpretation_version, declared_units, scale_source,
    scale_mm_per_unit, scale_verified, primary_interpretation, area_mm2, area_m2,
    component_count, hole_count, primitive_count, analysis_seconds
"""


def cache_key(source_sha256: str) -> str:
    """The identity of *this drawing measured by this algorithm*.

    Combines the source hash with every version that changes the answer: the
    geometry engine, the interpretation layer, and a fingerprint of the tolerance
    and region configuration. Any of them moving produces a different key, so an
    analysis made under the old behaviour is never silently reused.
    """
    material = "|".join([
        source_sha256,
        ENGINE_VERSION,
        INTERPRETATION_VERSION,
        config_fingerprint(),
    ])
    return hashlib.sha256(material.encode("utf-8")).hexdigest()


@dataclass
class AnalysisSummary:
    """A saved analysis as a list shows it — no geometry, no artifact."""

    id: str
    source_sha256: str
    original_filename: str
    display_name: Optional[str]
    source_type: str
    source_size_bytes: int
    status: str
    created_at: str
    completed_at: Optional[str]
    updated_at: Optional[str]
    engine_version: str
    interpretation_version: str
    declared_units: Optional[str]
    scale_source: Optional[str]
    scale_mm_per_unit: Optional[float]
    scale_verified: bool
    primary_interpretation: Optional[str]
    area_mm2: Optional[float]
    area_m2: Optional[float]
    component_count: Optional[int]
    hole_count: Optional[int]
    primitive_count: Optional[int]
    analysis_seconds: Optional[float]

    def as_dict(self) -> Dict[str, Any]:
        return {
            "id": self.id,
            "source_sha256": self.source_sha256,
            "file_name": self.original_filename,
            "display_name": self.display_name,
            "name": self.display_name or self.original_filename,
            "source_type": self.source_type,
            "source_size_bytes": self.source_size_bytes,
            "status": self.status,
            "created_at": self.created_at,
            "completed_at": self.completed_at,
            "updated_at": self.updated_at,
            "engine_version": self.engine_version,
            "interpretation_version": self.interpretation_version,
            "declared_units": self.declared_units,
            "scale_source": self.scale_source,
            "scale_mm_per_unit": self.scale_mm_per_unit,
            "scale_verified": self.scale_verified,
            "primary_interpretation": self.primary_interpretation,
            "area_mm2": self.area_mm2,
            "area_m2": self.area_m2,
            "component_count": self.component_count,
            "hole_count": self.hole_count,
            "primitive_count": self.primitive_count,
            "analysis_seconds": self.analysis_seconds,
        }


def _summary_from_row(row: Any) -> AnalysisSummary:
    def stamp(value: Any) -> Optional[str]:
        return value.isoformat() if value is not None else None

    return AnalysisSummary(
        id=str(row[0]), source_sha256=row[1], original_filename=row[2],
        display_name=row[3], source_type=row[4], source_size_bytes=row[5],
        status=row[6], created_at=stamp(row[7]) or "", completed_at=stamp(row[8]),
        updated_at=stamp(row[9]), engine_version=row[10],
        interpretation_version=row[11], declared_units=row[12], scale_source=row[13],
        scale_mm_per_unit=row[14], scale_verified=bool(row[15]),
        primary_interpretation=row[16], area_mm2=row[17], area_m2=row[18],
        component_count=row[19], hole_count=row[20], primitive_count=row[21],
        analysis_seconds=row[22],
    )


def _extract(payload: Dict[str, Any]) -> Dict[str, Any]:
    """Pull the queryable columns out of a finished result payload.

    Reads what the engine produced; computes nothing. If a field is absent the
    column is null rather than guessed — a saved analysis must not invent a number
    the measurement did not state.
    """
    document = payload.get("document") or {}
    area = payload.get("area") or {}
    geometry = area.get("geometry") or {}
    scale = area.get("scale") or {}
    cad = document.get("cad") or {}
    units = cad.get("units") or {}

    readings = area.get("footprint_interpretations") or []
    primary = readings[0] if readings else {}
    physical = (primary.get("units") or {}) if primary else {}

    return {
        "source_type": document.get("source_kind") or "pdf",
        "declared_units": units.get("name"),
        "scale_source": scale.get("source"),
        "scale_mm_per_unit": scale.get("mm_per_unit"),
        "scale_verified": bool(scale.get("verified")),
        "calibration_json": scale.get("calibration"),
        "primary_interpretation": primary.get("type"),
        "area_mm2": primary.get("area_mm2"),
        "area_m2": physical.get("m2") if primary.get("area_mm2") is not None else None,
        "component_count": geometry.get("components"),
        "hole_count": geometry.get("holes"),
        "primitive_count": geometry.get("raw_primitives"),
        "geometry_summary_json": geometry,
        "cad_metadata_json": _cad_metadata(cad),
        "interpretations_json": readings,
        "warnings_json": area.get("warnings") or [],
        "assumptions_json": area.get("assumptions") or [],
        "confidence_json": area.get("confidence") or {},
    }


def _cad_metadata(cad: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    """CAD provenance worth keeping, without the geometry.

    Layers, blocks, units, spaces, entity counts, XREFs and the conversion record —
    the things that let someone later answer what the drawing actually contained.
    Per-entity detail is left out: it belongs to the artifact, not to a metadata
    column.
    """
    if not cad:
        return None
    return {
        "dxf_version": cad.get("dxf_version"),
        "acad_release": cad.get("acad_release"),
        "units": cad.get("units"),
        "extents": cad.get("extents"),
        "layouts": cad.get("layouts"),
        "layers": cad.get("layers"),
        "blocks": cad.get("blocks"),
        "xrefs": cad.get("xrefs"),
        "entity_counts": cad.get("entity_counts"),
        "unsupported_counts": cad.get("unsupported_counts"),
        "text_count": cad.get("text_count"),
        "dimension_count": cad.get("dimension_count"),
        "notes": cad.get("notes"),
        "conversion": cad.get("conversion"),
    }


@dataclass
class AnalysisRepository:
    """Saved analyses, in this application's schema."""

    url: Optional[str] = None
    schema: Optional[str] = None
    store: ArtifactStore = field(default=None)  # type: ignore[assignment]

    def __post_init__(self) -> None:
        self.schema = self.schema or config.schema_name()
        if self.store is None:
            self.store = PostgresArtifactStore(self.url, self.schema)

    # ── writing ─────────────────────────────────────────────────────────────

    def save(
        self,
        payload: Dict[str, Any],
        source_sha256: str,
        file_name: str,
        source_size_bytes: int,
        analysis_seconds: Optional[float] = None,
    ) -> Dict[str, Any]:
        """Record a completed analysis and its viewer artifact.

        Args:
            payload: The finished result, exactly as the API returns it. Read, never
                modified.

        Returns:
            The stored summary plus the artifact's measured sizes.
        """
        columns = _extract(payload)
        analysis_id = str(uuid.uuid4())
        key = cache_key(source_sha256)

        with pool.connection(self.url, self.schema) as conn:
            with conn.cursor() as cursor:
                # An earlier analysis of the same drawing by the same algorithm is
                # superseded rather than deleted: its audit trail stays readable.
                cursor.execute(
                    f"""UPDATE {self.schema}.analyses SET status = %s, updated_at = now()
                        WHERE cache_key = %s AND status = %s""",
                    (STATUS_SUPERSEDED, key, STATUS_COMPLETED),
                )
                cursor.execute(
                    f"""INSERT INTO {self.schema}.analyses (
                            id, cache_key, source_sha256, original_filename,
                            source_type, source_size_bytes, engine_version,
                            interpretation_version, config_fingerprint, status,
                            completed_at, declared_units, scale_source,
                            scale_mm_per_unit, scale_verified, calibration_json,
                            primary_interpretation, area_mm2, area_m2,
                            component_count, hole_count, primitive_count,
                            geometry_summary_json, cad_metadata_json,
                            interpretations_json, warnings_json, assumptions_json,
                            confidence_json, analysis_seconds)
                        VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, now(),
                                %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s,
                                %s, %s, %s, %s, %s, %s, %s)""",
                    (
                        analysis_id, key, source_sha256, file_name,
                        columns["source_type"], source_size_bytes, ENGINE_VERSION,
                        INTERPRETATION_VERSION, config_fingerprint(), STATUS_COMPLETED,
                        columns["declared_units"], columns["scale_source"],
                        columns["scale_mm_per_unit"], columns["scale_verified"],
                        _json(columns["calibration_json"]),
                        columns["primary_interpretation"], columns["area_mm2"],
                        columns["area_m2"], columns["component_count"],
                        columns["hole_count"], columns["primitive_count"],
                        _json(columns["geometry_summary_json"]),
                        _json(columns["cad_metadata_json"]),
                        _json(columns["interpretations_json"]),
                        _json(columns["warnings_json"]),
                        _json(columns["assumptions_json"]),
                        _json(columns["confidence_json"]),
                        analysis_seconds,
                    ),
                )
            conn.commit()

        artifact = self.store.put(analysis_id, VIEWER_RESULT, encode(payload))
        self._event(analysis_id, EVENT_CREATED, {
            "engine_version": ENGINE_VERSION,
            "interpretation_version": INTERPRETATION_VERSION,
            "scale_source": columns["scale_source"],
            "scale_verified": columns["scale_verified"],
            "area_mm2": columns["area_mm2"],
            "component_count": columns["component_count"],
            "hole_count": columns["hole_count"],
        })
        logger.info(
            "analysis saved · %s · %s · artifact %.1f MB -> %.1f MB (%.1f%%)",
            analysis_id, columns["source_type"],
            artifact.original_size_bytes / 1e6, artifact.compressed_size_bytes / 1e6,
            artifact.ratio * 100,
        )
        summary = self.get_summary(analysis_id)
        return {
            "analysis": summary.as_dict() if summary else {"id": analysis_id},
            "artifact": artifact.as_dict(),
        }

    def update_result(
        self, analysis_id: str, payload: Dict[str, Any], event: str = EVENT_RECALCULATED
    ) -> Dict[str, Any]:
        """Replace a saved analysis's result, keeping what changed in the audit trail.

        Used when the operator calibrates or changes a selection: the measurement is
        genuinely different, the stored state should be the current one, and the
        previous scale and area stay readable in the events.
        """
        before = self.get_summary(analysis_id)
        columns = _extract(payload)

        with pool.connection(self.url, self.schema) as conn:
            with conn.cursor() as cursor:
                cursor.execute(
                    f"""UPDATE {self.schema}.analyses SET
                            declared_units = %s, scale_source = %s,
                            scale_mm_per_unit = %s, scale_verified = %s,
                            calibration_json = %s, primary_interpretation = %s,
                            area_mm2 = %s, area_m2 = %s, component_count = %s,
                            hole_count = %s, primitive_count = %s,
                            geometry_summary_json = %s, interpretations_json = %s,
                            warnings_json = %s, assumptions_json = %s,
                            confidence_json = %s, updated_at = now()
                        WHERE id = %s""",
                    (
                        columns["declared_units"], columns["scale_source"],
                        columns["scale_mm_per_unit"], columns["scale_verified"],
                        _json(columns["calibration_json"]),
                        columns["primary_interpretation"], columns["area_mm2"],
                        columns["area_m2"], columns["component_count"],
                        columns["hole_count"], columns["primitive_count"],
                        _json(columns["geometry_summary_json"]),
                        _json(columns["interpretations_json"]),
                        _json(columns["warnings_json"]),
                        _json(columns["assumptions_json"]),
                        _json(columns["confidence_json"]),
                        analysis_id,
                    ),
                )
                changed = cursor.rowcount
            conn.commit()
        if not changed:
            raise KeyError(analysis_id)

        self.store.put(analysis_id, VIEWER_RESULT, encode(payload))
        self._event(analysis_id, event, {
            "before": {
                "scale_source": before.scale_source if before else None,
                "scale_mm_per_unit": before.scale_mm_per_unit if before else None,
                "scale_verified": before.scale_verified if before else None,
                "area_mm2": before.area_mm2 if before else None,
            },
            "after": {
                "scale_source": columns["scale_source"],
                "scale_mm_per_unit": columns["scale_mm_per_unit"],
                "scale_verified": columns["scale_verified"],
                "area_mm2": columns["area_mm2"],
            },
            "calibration": columns["calibration_json"],
        })
        summary = self.get_summary(analysis_id)
        return {"analysis": summary.as_dict() if summary else {"id": analysis_id}}

    def rename(self, analysis_id: str, name: str) -> Optional[AnalysisSummary]:
        """Give an analysis an operator-chosen name. The filename is never lost."""
        cleaned = (name or "").strip()[:200] or None
        with pool.connection(self.url, self.schema) as conn:
            with conn.cursor() as cursor:
                cursor.execute(
                    f"""UPDATE {self.schema}.analyses
                        SET display_name = %s, updated_at = now() WHERE id = %s""",
                    (cleaned, analysis_id),
                )
                changed = cursor.rowcount
            conn.commit()
        if not changed:
            return None
        self._event(analysis_id, EVENT_RENAMED, {"display_name": cleaned})
        return self.get_summary(analysis_id)

    def delete(self, analysis_id: str) -> bool:
        """Remove an analysis, its artifacts and its events."""
        with pool.connection(self.url, self.schema) as conn:
            with conn.cursor() as cursor:
                cursor.execute(
                    f"DELETE FROM {self.schema}.analyses WHERE id = %s", (analysis_id,))
                removed = cursor.rowcount
            conn.commit()
        return bool(removed)

    def _event(self, analysis_id: str, event: str, detail: Any) -> None:
        with pool.connection(self.url, self.schema) as conn:
            with conn.cursor() as cursor:
                cursor.execute(
                    f"""INSERT INTO {self.schema}.analysis_events
                            (id, analysis_id, event, detail_json)
                        VALUES (%s, %s, %s, %s)""",
                    (str(uuid.uuid4()), analysis_id, event, _json(detail)),
                )
            conn.commit()

    # ── reading ─────────────────────────────────────────────────────────────

    def find_compatible(self, source_sha256: str) -> Optional[AnalysisSummary]:
        """The most recent completed analysis of this drawing by this algorithm.

        Matched on the full cache key, so a result from an earlier engine or a
        different tolerance configuration is not returned. Never returned
        automatically by the caller — the operator is offered it.
        """
        with pool.connection(self.url, self.schema) as conn:
            with conn.cursor() as cursor:
                cursor.execute(
                    f"""SELECT {_SUMMARY_COLUMNS} FROM {self.schema}.analyses
                        WHERE cache_key = %s AND status = %s
                        ORDER BY created_at DESC LIMIT 1""",
                    (cache_key(source_sha256), STATUS_COMPLETED),
                )
                row = cursor.fetchone()
        return _summary_from_row(row) if row else None

    def recent(self, limit: int = 20) -> List[AnalysisSummary]:
        """Completed analyses, newest first. No geometry, no artifacts."""
        limit = max(1, min(int(limit), 100))
        with pool.connection(self.url, self.schema) as conn:
            with conn.cursor() as cursor:
                cursor.execute(
                    f"""SELECT {_SUMMARY_COLUMNS} FROM {self.schema}.analyses
                        WHERE status = %s ORDER BY created_at DESC LIMIT %s""",
                    (STATUS_COMPLETED, limit),
                )
                rows = cursor.fetchall()
        return [_summary_from_row(row) for row in rows]

    def get_summary(self, analysis_id: str) -> Optional[AnalysisSummary]:
        with pool.connection(self.url, self.schema) as conn:
            with conn.cursor() as cursor:
                cursor.execute(
                    f"SELECT {_SUMMARY_COLUMNS} FROM {self.schema}.analyses WHERE id = %s",
                    (analysis_id,),
                )
                row = cursor.fetchone()
        return _summary_from_row(row) if row else None

    def reopen(self, analysis_id: str) -> Optional[Dict[str, Any]]:
        """Everything needed to restore a workspace, without recomputing anything.

        Returns the stored viewer payload — the same shape the analysis produced —
        alongside the summary, or ``None`` if there is no such analysis.
        """
        summary = self.get_summary(analysis_id)
        if summary is None:
            return None
        payload = self.store.get(analysis_id, VIEWER_RESULT)
        if payload is None:
            return None
        return {
            "analysis": summary.as_dict(),
            "result": json.loads(payload.decode("utf-8")),
            "events": self.events(analysis_id),
        }

    def events(self, analysis_id: str, limit: int = 50) -> List[Dict[str, Any]]:
        """The audit trail: what happened to this analysis, and when."""
        with pool.connection(self.url, self.schema) as conn:
            with conn.cursor() as cursor:
                cursor.execute(
                    f"""SELECT event, at, detail_json FROM {self.schema}.analysis_events
                        WHERE analysis_id = %s ORDER BY at LIMIT %s""",
                    (analysis_id, max(1, min(int(limit), 500))),
                )
                rows = cursor.fetchall()
        return [
            {"event": row[0], "at": row[1].isoformat() if row[1] else None,
             "detail": row[2]}
            for row in rows
        ]


def _json(value: Any) -> Optional[str]:
    """Serialise for a JSONB column, or ``None``."""
    if value is None:
        return None
    from psycopg.types.json import Json

    return Json(value)  # type: ignore[return-value]
