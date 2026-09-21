"""Where a large derived artifact is kept, behind an interface.

A finished analysis of a production drawing carries tens of megabytes of polygon
geometry — the 97 MB DWG produces a 44.7 MB result payload. That is what the
viewer needs to reopen the analysis without recomputing anything, and it is the
wrong shape for JSONB: it is never queried, only fetched whole. So it is
serialised, compressed and stored as bytes, with the searchable engineering
metadata kept separately in columns.

:class:`ArtifactStore` exists so that the domain never learns where bytes live.
Today the only implementation is PostgreSQL. Object storage can be added later as
another implementation, and nothing in the analysis or geometry code will know.
"""

from __future__ import annotations

import gzip
import hashlib
import json
import time
import uuid
from dataclasses import dataclass
from typing import Any, Dict, Optional, Protocol

from backend.db import config, pool

#: The viewer payload: everything needed to restore a workspace.
VIEWER_RESULT = "viewer_result"

#: gzip level 6 is the default and the right trade here. Level 9 spends noticeably
#: more CPU on a 45 MB payload for a percent or two, and this runs at the end of a
#: job the operator is already waiting on.
GZIP_LEVEL = 6


@dataclass(frozen=True)
class StoredArtifact:
    """What was stored, and what it cost.

    The sizes are kept because the decision to hold artifacts in PostgreSQL rather
    than object storage should be revisited against measurements rather than
    impressions.
    """

    id: str
    artifact_type: str
    compression: str
    original_size_bytes: int
    compressed_size_bytes: int
    sha256: str

    @property
    def ratio(self) -> float:
        if not self.original_size_bytes:
            return 0.0
        return round(self.compressed_size_bytes / self.original_size_bytes, 4)

    def as_dict(self) -> Dict[str, Any]:
        return {
            "id": self.id,
            "artifact_type": self.artifact_type,
            "compression": self.compression,
            "original_size_bytes": self.original_size_bytes,
            "compressed_size_bytes": self.compressed_size_bytes,
            "compression_ratio": self.ratio,
            "sha256": self.sha256,
        }


class ArtifactStore(Protocol):
    """Somewhere to put bytes that belong to an analysis.

    Deliberately narrow: put, get, delete. Nothing here mentions PostgreSQL,
    compression or a schema, so an S3 or R2 implementation can be added without
    the analysis pipeline changing.
    """

    def put(self, analysis_id: str, artifact_type: str, payload: bytes) -> StoredArtifact:
        """Store bytes for an analysis, replacing any of the same type."""
        ...

    def get(self, analysis_id: str, artifact_type: str) -> Optional[bytes]:
        """The bytes, or ``None`` if this analysis has no such artifact."""
        ...

    def delete(self, analysis_id: str) -> int:
        """Remove every artifact for an analysis. Returns how many."""
        ...


def encode(document: Any) -> bytes:
    """A result payload as compact, sorted JSON bytes.

    Sorted keys so that the same result always produces the same bytes, which
    makes the stored hash meaningful: two artifacts with equal hashes really are
    the same payload.
    """
    return json.dumps(
        document, ensure_ascii=False, separators=(",", ":"), sort_keys=True,
        allow_nan=False,
    ).encode("utf-8")


def compress(payload: bytes) -> bytes:
    # mtime=0 so identical content compresses to identical bytes; a timestamp in
    # the header would make every hash unique and the comparison worthless.
    return gzip.compress(payload, compresslevel=GZIP_LEVEL, mtime=0)


def decompress(blob: bytes) -> bytes:
    return gzip.decompress(blob)


class PostgresArtifactStore:
    """Artifacts as compressed ``bytea`` rows in this application's schema."""

    def __init__(self, url: Optional[str] = None, schema: Optional[str] = None) -> None:
        self._url = url
        self._schema = schema or config.schema_name()

    def put(self, analysis_id: str, artifact_type: str, payload: bytes) -> StoredArtifact:
        blob = compress(payload)
        artifact = StoredArtifact(
            id=str(uuid.uuid4()),
            artifact_type=artifact_type,
            compression="gzip",
            original_size_bytes=len(payload),
            compressed_size_bytes=len(blob),
            sha256=hashlib.sha256(payload).hexdigest(),
        )
        with pool.connection(self._url, self._schema) as conn:
            with conn.cursor() as cursor:
                cursor.execute(
                    f"""INSERT INTO {self._schema}.artifacts
                            (id, analysis_id, artifact_type, compression, payload,
                             original_size_bytes, compressed_size_bytes, sha256)
                        VALUES (%s, %s, %s, %s, %s, %s, %s, %s)
                        ON CONFLICT (analysis_id, artifact_type) DO UPDATE SET
                            compression = EXCLUDED.compression,
                            payload = EXCLUDED.payload,
                            original_size_bytes = EXCLUDED.original_size_bytes,
                            compressed_size_bytes = EXCLUDED.compressed_size_bytes,
                            sha256 = EXCLUDED.sha256,
                            created_at = now()""",
                    (artifact.id, analysis_id, artifact_type, artifact.compression,
                     blob, artifact.original_size_bytes, artifact.compressed_size_bytes,
                     artifact.sha256),
                )
            conn.commit()
        return artifact

    def get(self, analysis_id: str, artifact_type: str) -> Optional[bytes]:
        with pool.connection(self._url, self._schema) as conn:
            with conn.cursor() as cursor:
                cursor.execute(
                    f"""SELECT payload, compression, sha256 FROM {self._schema}.artifacts
                        WHERE analysis_id = %s AND artifact_type = %s""",
                    (analysis_id, artifact_type),
                )
                row = cursor.fetchone()
        if row is None:
            return None
        blob, compression, expected = row
        payload = decompress(bytes(blob)) if compression == "gzip" else bytes(blob)
        actual = hashlib.sha256(payload).hexdigest()
        if actual != expected:
            # Refuse to hand back an artifact that is not what was stored. A
            # corrupted payload restored silently would put wrong geometry in front
            # of an engineer, which is the one outcome worth failing over (§3).
            raise ValueError(
                f"stored artifact {artifact_type} for {analysis_id} failed its "
                "checksum and was not restored"
            )
        return payload

    def stats(self, analysis_id: str) -> Optional[Dict[str, Any]]:
        """Sizes and load cost for an analysis's viewer artifact, for diagnostics."""
        started = time.perf_counter()
        with pool.connection(self._url, self._schema) as conn:
            with conn.cursor() as cursor:
                cursor.execute(
                    f"""SELECT artifact_type, compression, original_size_bytes,
                               compressed_size_bytes, sha256, octet_length(payload)
                        FROM {self._schema}.artifacts WHERE analysis_id = %s""",
                    (analysis_id,),
                )
                rows = cursor.fetchall()
        if not rows:
            return None
        fetch_seconds = time.perf_counter() - started
        return {
            "fetch_seconds": round(fetch_seconds, 4),
            "artifacts": [
                {
                    "artifact_type": row[0],
                    "compression": row[1],
                    "original_size_bytes": row[2],
                    "compressed_size_bytes": row[3],
                    "compression_ratio": (
                        round(row[3] / row[2], 4) if row[2] else 0.0),
                    "sha256": row[4],
                    "stored_bytes": row[5],
                }
                for row in rows
            ],
        }

    def delete(self, analysis_id: str) -> int:
        with pool.connection(self._url, self._schema) as conn:
            pool.verify_target(conn, self._schema, self._url)
            with conn.cursor() as cursor:
                cursor.execute(
                    f"DELETE FROM {self._schema}.artifacts WHERE analysis_id = %s",
                    (analysis_id,),
                )
                removed = cursor.rowcount
            conn.commit()
        return removed
