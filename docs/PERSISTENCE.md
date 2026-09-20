# Durable analyses

Written for: whoever operates or extends this application's storage.

Measuring a production drawing costs between sixteen seconds and eight minutes,
and up to twelve gigabytes of memory. Having done it once, doing it again because
a tab was closed is waste. This is how a finished analysis is kept so it can be
reopened, and what that deliberately does *not* do.

## The two rules

**Persistence is observational.** A result is computed, and then — separately,
afterwards — recorded. Nothing in the save path can reach a measurement. The test
suite proves it: the same drawing is measured twice, once with the save path
exercised and once with it disabled, and every engineering value is compared.

**Persistence is optional.** With no `DATABASE_URL` the application runs exactly
as it otherwise would and keeps no history. `/health` reports that plainly. A
developer needs no database, and the test suite must never have one.

## Configuration

| Variable | Default | Meaning |
|---|---|---|
| `DATABASE_URL` | unset | PostgreSQL connection string. Absent ⇒ persistence disabled. On Render, the database's **internal** URL. |
| `DATABASE_SCHEMA` | `projection_area` | Where every object lives. `public` and the PostgreSQL system schemas are refused outright. |
| `PROJECTED_AREA_TEST_DATABASE_URL` | unset | Only for the database tests. Deliberately **not** `DATABASE_URL`. |

Locally these come from `.env`, which is git-ignored and read with
`override=False` so a real environment always wins. `.env.example` carries
placeholders and no values.

On Render nothing is read from a file: both come from the service environment. Use
the **internal** database URL — the service and the database are in the same
region, and the connection then never leaves Render's network. The external URL is
for local development and hand-run migrations.

### Why the test suite cannot reach production

A developer's `.env` points at the live instance, which is shared with another
application. Two things stop a test writing into it, and both are needed:

1. An autouse fixture deletes `DATABASE_URL` for every test.
2. The same fixture neutralises `load_local_env`, because the application reads
   `.env` at startup by design — deleting the variable alone would see it put
   straight back the moment a `TestClient` starts its lifespan.

Database tests opt in through `PROJECTED_AREA_TEST_DATABASE_URL`, create a schema
named `pa_test_<random>`, and drop it afterwards.

## Isolation

The instance is shared with another application, so this does not rest on
remembering to qualify each statement.

- Every connection is opened with `options=-c search_path=<schema>`, so the schema
  is a property of the connection, decided before any statement runs. `public` is
  **absent** from the path: an unqualified name that does not exist in this schema
  fails loudly rather than finding something in `public`.
- Every statement is schema-qualified *as well*. That is what makes the isolation
  checkable rather than merely intended — `unqualified_objects()` parses each
  migration statement, the migration runner **refuses to execute** one that names
  an object outside the schema, and the test suite asserts the same statically
  while also proving the check fires on `public.x`, a bare table name, an `ALTER`,
  a `TRUNCATE`, an index on a foreign table and a foreign key pointing outward.
- `DATABASE_SCHEMA` is validated as a plain lowercase identifier before it reaches
  SQL, and `public`, `information_schema`, `pg_catalog` and `pg_toast` are refused.

**Nothing belonging to the other application is ever read, written, migrated or
named.**

### A dedicated role (recommended, not required)

Designed for but not required, so credential management does not block this. To
add one:

```sql
-- as a superuser, once
CREATE ROLE projection_area_app LOGIN PASSWORD '<generated>';
GRANT USAGE ON SCHEMA projection_area TO projection_area_app;
GRANT ALL PRIVILEGES ON ALL TABLES IN SCHEMA projection_area TO projection_area_app;
ALTER DEFAULT PRIVILEGES IN SCHEMA projection_area
    GRANT ALL ON TABLES TO projection_area_app;
-- and explicitly not the other application's schema
REVOKE ALL ON SCHEMA public FROM projection_area_app;
```

The role needs `CREATE` on the database only for the first migration (it creates
the schema); afterwards that can be revoked. Point `DATABASE_URL` at the new role
and nothing else changes.

## The model

Three tables, plus bookkeeping. Small on purpose: normalising further would add
joins without answering a question anyone has.

```
projection_area.schema_migrations   version, name, applied_at

projection_area.analyses            one row per completed analysis
  identity      id, cache_key, source_sha256, original_filename, display_name,
                source_type, source_size_bytes, engine_version,
                interpretation_version, config_fingerprint
  lifecycle     status, created_at, completed_at, updated_at
  scale         declared_units, scale_source, scale_mm_per_unit, scale_verified,
                calibration_json
  headline      primary_interpretation, area_mm2, area_m2, component_count,
                hole_count, primitive_count
  evidence      geometry_summary_json, cad_metadata_json, interpretations_json,
                warnings_json, assumptions_json, confidence_json

projection_area.artifacts           the compressed viewer payload
  analysis_id, artifact_type, compression, payload BYTEA,
  original_size_bytes, compressed_size_bytes, sha256

projection_area.analysis_events     append-only audit
  analysis_id, event, at, detail_json
```

Structured columns are the ones a list or a filter needs. JSONB holds the
engineering evidence, which is read whole and never searched inside. The viewer
payload is neither: tens of megabytes of coordinates belong in `bytea`, compressed.

**One events table rather than separate calibration, interpretation and version
tables.** What is actually needed is the ability to answer "what changed, when,
and from what" — a typed event with a JSONB body does that. `created` records the
engine versions and the headline numbers; `recalibrated` and `recalculated` record
the scale and area before and after, with the calibration itself; `renamed` records
the name. The current state lives in the row, the history in the events.

## Measured artifact cost

Each real result run through the API, then encoded, compressed and loaded back
exactly as the artifact store does it. The round trip is asserted byte-identical.

| Drawing | Payload | Stored | Ratio | Compress | Load |
|---|---|---|---|---|---|
| 101 PDF | 2.83 MB | 0.16 MB | 5.8 % | 0.02 s | 0.02 s |
| 102 PDF | 3.44 MB | 0.31 MB | 9.1 % | 0.03 s | 0.03 s |
| 102 DWG | 44.74 MB | 9.85 MB | 22.0 % | 0.86 s | 0.65 s |

"Load" is decompression plus JSON parsing — what reopening costs before the
browser receives anything.

**What this says about object storage: not yet.** The worst production drawing
stores in under ten megabytes, a PDF in well under one. That is a few hundred of
the largest analyses per gigabyte of database, and reopening one costs under a
second against eight minutes to recompute it. Nothing here demonstrates a need for
S3, R2 or MinIO, so none was added. `ArtifactStore` is the seam where one would
go, and the numbers above are what to re-measure before deciding.

The ratio is worst on the DWG because its payload is mostly distinct coordinates;
the PDFs compress further because a large share of theirs is repeated structure.
Returning the summary and fetching geometry per selected reading (see
`docs/CAD_PERFORMANCE_ROADMAP.md`) would shrink the stored and transferred size
more than any compression setting.

## Analysis identity

```
cache_key = SHA256( source_sha256 | engine_version | interpretation_version
                    | config_fingerprint )
```

The source hash alone would be wrong: it would hand back a result produced by
different code as though it were current. `config_fingerprint` is a hash of the
tolerance and region settings, because a tolerance can change a reconstructed
contour with no version moving — and nobody remembers to bump a version for that.

The hash is computed **while the upload streams**, from chunks already being
written, so it costs no extra pass over a 97 MB drawing.

A cache hit is **offered, never substituted**: the upload returns 200 with
`cached`, the operator chooses *Open previous analysis* or *Re-analyse anyway*, and
`?reanalyse=true` skips the offer. Reopening a stored result and running a new
measurement are different acts and only the operator knows which they meant.

## What is not kept

The original drawing (§35). What is stored is its filename, its SHA-256, its size,
and what the engine concluded — enough to audit a result, not enough to
reconstruct a customer's property. A test asserts the schema has exactly one
binary column and that it is the derived viewer artifact.

If source retention is ever wanted it must be an explicit operator choice with
retention and deletion controls, and it is not implemented.

## Calibration

A calibration made on a drawing uploaded in this session is persisted: the
recalculation that applies it names its analysis, the new result becomes the saved
state, and a `recalibrated` event records the scale and area before and after
alongside the calibration itself — the two points, the known length and unit, the
span, the resulting mm per unit, `provenance: operator_supplied` and the time it
was recorded. A refresh or a reopen then shows the calibration that was actually
established.

The analysis id comes from the browser, so it is not trusted on its own: the write
is refused unless the document's source hash equals the analysis's. A calibration
measured on one drawing cannot land on another's record.

**Recalibrating a *reopened* analysis is not supported in this phase.** Reopening
restores the stored viewer payload; the prepared geometry a recalculation needs is
not stored, because the drawing it comes from is not retained (§35). Re-uploading
the drawing restores full recalculation. Physical area is, in principle, the
drawing-unit area times the square of the scale, and every reading stores its
drawing-unit area — so a reopened analysis *could* be rescaled without the
geometry. That would be a second path computing areas, though, and it should not
exist until the baseline oracle proves it agrees with the engine on every
production drawing.

## API

| | |
|---|---|
| `POST /api/analyse` | 202 + job as before; **200 + `cached`** when a compatible analysis exists; `?reanalyse=true` to skip |
| `POST /api/analyses` | `{job_id}` — save a finished job's result, for when the automatic save failed |
| `GET /api/analyses?limit=n` | recent completed analyses, summaries only |
| `GET /api/analyses/{id}` | everything needed to restore a workspace |
| `PATCH /api/analyses/{id}` | `{name}` — operator-chosen name; the filename is never overwritten |
| `DELETE /api/analyses/{id}` | removes the analysis, its artifact and its events |

Saving happens automatically on the server when a job completes, so a closed tab
does not lose the record. The manual endpoint takes a **job id, not a payload**:
the result is read from what the server measured, never from numbers a browser
sends back (§22).

With persistence disabled these return 503 `persistence_disabled` — a statement
that the instance keeps no history, not a broken endpoint.

## Health

```json
{"status": "ok", "database": true, "persistence": true}
```

`database` is whether one is configured; `persistence` is whether it answers.
They differ, and the difference matters: configured-but-unreachable is a different
problem from never-configured. Neither reports a host, a user, a path or a URL.

**The probe must stay cheap.** A platform restarts an instance whose health check
times out, and a restart mid-analysis destroys an eight-minute job. So the probe
never opens the pool (which would wait on connection retries), uses a two-second
timeout, and caches its answer for fifteen seconds. Before that was true, a
misconfigured `DATABASE_URL` made `/health` take ten seconds.

## Secrets

A connection string is a credential: it carries a password and a host.

- Never logged, never returned to a browser, never in an exception message, never
  in `/health`. `redact()` keeps the driver and the database name and drops
  everything else.
- Driver exceptions are converted to `PersistenceUnavailable` carrying only the
  exception *type*, because a psycopg error can contain the host and the user.
- psycopg logs its own connection failures, and those messages name the host. A
  redacting filter is installed on the driver's loggers at startup: the reason
  survives, the address does not.
