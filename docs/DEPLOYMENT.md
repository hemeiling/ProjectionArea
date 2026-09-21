# Deployment

Written for: whoever operates this service — deploying it, sizing it, or working
out why a drawing failed on the server but not on a laptop.

The application runs in two places and the difference between them is deliberate:

| | Local | Deployed (Render) |
|---|---|---|
| Start | `python run.py` | `python -m uvicorn backend.main:app --host 0.0.0.0 --port $PORT` |
| Port | 8000, or the next free one | whatever `PORT` says, or fail |
| Bind | `127.0.0.1` | `0.0.0.0` |
| Auto-reload | on | off |
| DWG converter | built by `tools/install_dwg_support.py` into `~/.local/libredwg` | in the image, on `PATH` |
| Build | `pip install -r requirements-dev.txt` | `Dockerfile` |

`run.py` reads `PORT` too, so running it on a platform works — it binds every
interface and turns reload off. It is still the local entry point: it looks for a
free port when one was not assigned, which is the opposite of what production
wants.

## Why Docker rather than Render's native Python runtime

Reading a DWG means running GNU LibreDWG's `dwg2dxf` as a subprocess. **No Debian
or Ubuntu release packages LibreDWG** — there is no source or binary package of
that name in any suite. A native Python service would therefore have to compile
it during the build step, against whatever toolchain the platform's base image
provides, and a failure there produces a service that starts cleanly and has
silently lost DWG support.

The `Dockerfile` builds it once from the GNU release tarball, checks the
tarball's SHA-256, links it statically, and fails the build if the resulting
image cannot find a converter. `/health` then reports the capability, so the same
question can be asked of a running instance from outside.

LibreDWG is GPL-3.0. It is executed as a separate process and never linked into
this application — aggregation, not a derived work — and its licence ships in the
image at `/opt/libredwg/share/licences/libredwg/COPYING`.

## Configuration

Nothing is required. The service starts with no environment set beyond `PORT`.

| Variable | Default | Purpose |
|---|---|---|
| `PORT` | 8000 | Set by the platform. Its presence is also what switches the bind address to `0.0.0.0` and turns reload off. |
| `MAX_UPLOAD_MB` | 200 | Upload ceiling. Production DWGs reach 93 MB. |
| `LIBREDWG_BIN` | unset | Explicit converter path, for a host that puts it somewhere unusual. Never needed when `dwg2dxf` is on `PATH`. |
| `PROJECTED_AREA_LOG` | `INFO` | Log level. |
| `CLAUDE_API_KEY`, `GEMINI_API_KEY` | unset | Optional. Nothing in the engine reads them; AI is for interpretation, never measurement. The service must start without them, and does. |

## Memory: the thing that decides the plan

Measured against the five production drawings, **end to end through the running
application** — upload, convert, read, footprint candidates, area — with a freshly
started server for each so its peak belongs to that drawing alone. Python does not
return freed memory to the OS promptly, so a reused server carries the previous
drawing's high-water mark and every figure after the first is inflated.

**macOS 26.5, arm64, Python 3.9.** Evidence about this machine; Linux will differ
somewhat, but not by the order of magnitude that decides the plan.

| Drawing | Input | Convert | Intermediate DXF | Primitives | Wall | Peak RSS |
|---|---|---|---|---|---|---|
| 101 PDF | 2.3 MB | — | — | 313,138 | 16 s | **0.9 GB** |
| 102 PDF | 7.0 MB | — | — | 959,931 | 41 s | **2.6 GB** |
| 103 DWG | 30.1 MB | 1.6 s | 120.1 MB | 421,517 | 102 s | **6.7 GB** |
| 101 DWG | 28.2 MB | 1.4 s | 111.7 MB | 893,476 | 161 s | **9.4 GB** |
| 102 DWG | 97.1 MB | 5.0 s | 381.4 MB | 2,374,784 | 496 s | **11.7 GB** |

Conversion is never the expensive part: LibreDWG turns the 97 MB DWG into 381 MB
of DXF in five seconds. The time and the memory both go on reading the result and
reconstructing faces from it.

An earlier version of this table was measured with `tools/measure_memory.py`,
which stops after `prepare_page` and never runs the footprint and area stages —
the expensive ones. It understated the 101 DWG by more than half (4.0 GB against
9.4 GB). Use the table above; the tool remains useful for comparing the *reading*
cost of two drawings, which is what it measures.

### What this means for plan sizing

Render's plans: Free and Starter are both 512 MB, Standard (`1c-2g`) 2 GB, Pro
(`2c-4g`) 4 GB, then `2c-8g` and `2c-16g`.

| Plan | RAM | What it can actually do |
|---|---|---|
| Free / Starter | 512 MB | Demo drawings and the interface only. **No production drawing fits** — the smallest needs 0.9 GB. |
| Standard `1c-2g` | 2 GB | The 101 PDF. |
| Pro `2c-4g` | 4 GB | Both PDFs. No DWG. |
| `2c-8g` | 8 GB | Adds the 103 DWG (6.7 GB). Not the other two. |
| `2c-16g` | 16 GB | **All five**, the largest at 11.7 GB. |

`render.yaml` asks for `2c-16g`, because that is the smallest plan on which every
production drawing this tool was built for actually completes. If DWG support can
wait, `2c-8g` runs both PDFs and the 103 with margin and is materially cheaper —
change one line. Either way this is a deliberate cost decision, which is why the
numbers above are here rather than a bare recommendation.

On a smaller plan a production drawing does not fail gracefully: the process is
killed part way through. The application explains that when it happens — the bar
keeps the progress it earned and the heading reads "Analysis interrupted" — but it
cannot prevent it.

Nothing in the engine was weakened to fit a smaller instance. A projected area
that is wrong because the server was economising is worse than no answer.

### Why it is this large, and what would fix it

**The workload is memory-bound in the CAD reader and the face reconstruction, not
in the web layer.** The 102 DWG becomes 2.37 million primitives, 6.9 million
segments and 175,231 faces, all as Python objects. The upload path itself now
costs one 1 MiB chunk.

Two separate follow-ups, both real engineering rather than configuration:

1. **Hold geometry in numpy arrays rather than per-primitive dataclasses**, and
   read entities in a streaming pass. This is what would move the 102 DWG from
   11.7 GB to something a 4 GB instance could serve.
2. **Stop sending the whole result in one response.** The 102 DWG's job payload is
   **44.7 MB** — 27.9 MB of it the full polygon geometry of all five footprint
   readings, and a further 15.8 MB of components that largely duplicate them. On
   localhost this is invisible. Over the internet it is the single thing most
   likely to make a deployed instance feel broken, and it also costs the server
   that much memory to serialise at the very end of a job. The fix is to return
   the summary and fetch geometry for the reading the operator actually selected.

Both are recorded rather than attempted here: they touch the code every geometry
test exists to protect, and deployment preparation is the wrong moment for that.

### When an instance runs out of memory

Two different failures, both explained rather than silent:

- Python sees the allocation fail → the job reports `out_of_memory`, naming it as
  a sizing problem rather than a fault in the drawing.
- The kernel kills the process → the job is simply gone, because jobs live in the
  process. The next poll gets a 404 whose body says the process restarted and
  that large drawings need more memory. The progress bar keeps the progress it
  had earned and the heading changes to "Analysis interrupted".

## Migrating the existing Render service

A native-Python service `projection-area-analyzer` already exists (Ohio, 0.5 CPU /
512 MB, auto-deploy from `main`). It serves PDF and DXF and reports `dwg: false`,
because a native Python build cannot install LibreDWG. It is the rollback and
stays running.

Two things to know about it:

- **Its builds have been failing since `6411b3c`.** That commit moved the
  dependency manifest from `backend/requirements.txt` to the repository root, so a
  build command pointing at the old path cannot install anything. The last good
  deploy — `82ad49f` — keeps serving, which is why the live service still answers
  `/api/health` in the old shape and leaks host paths from `/api/capabilities`.
  Repointing that build command at `requirements.txt` is enough to unstick it.
- **Nothing in the current code requires Docker.** It runs on the native runtime
  and reports `dwg: false`; Docker is needed only for DWG support. So the old
  service can be brought up to date as a rollback target, or deliberately frozen
  at `82ad49f` — both are defensible, but a frozen service with auto-deploy still
  enabled will keep generating failed deploys, so turn auto-deploy off if you
  freeze it.

The migration runs the Docker service *alongside* it:

1. Apply this blueprint. It creates `projection-area-analyzer-docker` — a
   deliberately different name, not a near-miss of the existing one — in the same
   region, on `1c-2g`.
2. Smoke test at 2 GB: `/health` reporting `dwg: true`, the landing page, EN/中文,
   a PDF, a DXF, the progress bar, JSON and CSV export. All of that fits.
3. Raise the plan to `2c-16g` in the dashboard and put the real DWGs through. A
   production DWG cannot run at 2 GB; the smallest needs 6.7 GB.
4. Only then decide whether the old service is renamed, repointed or retired.

## Known limitation: in-memory jobs are not deployment-safe

**An analysis is held in the memory of the instance that started it.** Nothing
else knows it exists: not the database, not another instance, not a restarted copy
of the same one.

That was a deliberate choice for a single-user tool (a dictionary and a thread,
§27), and it fails in a specific, now-observed way. The hosted 102 run, job
`5edbc6630c474a8a`:

```
old instance …-ld89q   GET /api/jobs/5edbc…   200   through 03:54:35
new instance …-nkzg8   GET /api/jobs/5edbc…   404   at      03:54:36
old instance …-ld89q   still running CAD work after the 404
```

A rolling deployment started the new instance and moved traffic to it while the old
one was mid-measurement. The job had not crashed or disappeared; the poll had simply
reached a process that never heard of it. When the old instance is then retired, its
work is lost with it.

What follows from that:

- **A deployment during an analysis loses the analysis.** The operator has to upload
  again. On the largest drawings that is eight minutes of work.
- **A job 404 cannot tell why.** It knows only that *this* instance has no record.
  The message used to say the server had restarted or run out of memory; on the 102
  run neither was true. It now reports what the evidence supports: every job
  snapshot carries an opaque token for the instance holding it, a 404 reports the
  token of the instance answering, and when they differ the page says a different
  instance answered — which is what happens during a deployment — and that the work
  may still be finishing elsewhere.
- **More than one instance, or more than one worker, makes this constant** rather
  than occasional: any poll can land on a process without the job. That is why the
  service runs one instance with `WEB_CONCURRENCY=1`.

### Operational guidance, until this is fixed

Avoid deploying while an analysis is running. Render deploys `main` on every push,
so on a busy day that means holding pushes, or turning auto-deploy off and deploying
deliberately.

### The fix, when it is worth building

Two separate problems, and only the first is small:

1. **Status that survives the process.** Keep job state in PostgreSQL — it is
   already there — rather than in a dictionary: a `projection_area.jobs` row per job
   holding the stage, progress snapshot, error and, when finished, the analysis id.
   Any instance can then answer a poll. The worker also writes a **heartbeat**, which
   turns "no record of this job" into evidence: *the worker last reported 40 s ago*
   means interrupted; *2 s ago* means still running on another instance.
2. **Work that survives the process.** Durable status does not make the measurement
   itself survive: the work still lives in one process and dies with it. Surviving
   that needs either a drain — the old instance finishes its jobs before exiting,
   within the platform's shutdown grace period — or a queue with retry, where an
   interrupted job is picked up again. The drain is the smaller step; the queue is
   the roadmap's item 10.

The child-process design recommended in `docs/INCIDENT_103_DWG_RESTART.md` fits
either: the web process would own the job record, and the worker would own only the
measurement.

## Long-running requests

A DWG takes minutes. No request is held open for one:

```
POST /api/analyse   ->  202 { job_id }      (returns as soon as the upload lands)
GET  /api/jobs/{id} ->  progress snapshot   (polled every 700 ms)
GET  /api/jobs/{id} ->  { state: "done", result }
```

So a platform's request timeout applies only to the upload itself and to each
poll, never to the analysis. This is also why a redeploy mid-analysis is a
recoverable, explained event rather than a hung browser.

**The health check has to stay cheap for this to hold.** Render restarts an
instance whose health check fails, and a restart mid-job kills the job. The work
runs in a thread inside the same process, so while a CAD parse holds several
gigabytes, anything expensive in `/health` competes with it. `/health` therefore
opens no drawing and — after the first call — spawns no subprocess: the
converter's version is memoised against the binary's size and modification time,
rather than asked of `dwg2dxf --version` on every poll. On one CPU, a long job and
a forking health check together are a plausible way to lose that job, and the
symptom would look like an unexplained restart rather than a failure.

## Files and privacy

- Uploads are streamed to a file in a per-process temporary directory. A 93 MB
  DWG is never held in memory as a whole; peak upload memory is one 1 MiB chunk.
- The intermediate DXF from a conversion — around 120 MB for the large drawings —
  lives in its own temporary workspace and is deleted as soon as it has been
  read, in a `finally` block.
- Documents are deleted on request, swept after a TTL, and the whole directory is
  removed when the process stops.
- Treat the platform filesystem as ephemeral, because it is. Nothing persists
  between deploys by design: no disk is attached, and attaching one would mean
  customer drawings surviving a restart. No database is involved.
- `/health` reports capability only — no paths, no environment values.
- The build context excludes `Inputs/`, `samples/`, and every `*.dwg`/`*.dxf`, so
  a customer drawing cannot reach a registry via an image layer.

## CI and the deploy gate

`.github/workflows/ci.yml` runs two jobs:

- **Test suite** — the whole suite including browser tests, with no LibreDWG
  present. The DWG tests skip, which also proves they skip cleanly for a new
  contributor.
- **Deployment image** — builds the image, starts it with `PORT` set, and
  requires `/health` to report `dwg: true` with the `libredwg` converter. This is
  the check that catches a broken LibreDWG build, and it is the reason the
  Dockerfile can be trusted without a local Docker daemon.

Render deploys `main` on push, and CI does **not** block that: a push to `main`
is a production action. To gate it, either

1. turn off auto-deploy in Render and call its deploy hook from a CI job that
   runs after both of the above, or
2. protect `main` with a branch rule requiring both checks, and merge only via
   pull request.

Until one of those is in place, the workflow for a deployment-specific fix is
still: reproduce, test, fix, run the full suite, commit, push — never experiment
against production with uncommitted code.

## Acceptance checks against a deployed instance

```bash
BASE=https://<service>.onrender.com

curl -fsS $BASE/health | python -m json.tool     # 200, dwg: true
curl -fsSI $BASE/ | head -1                      # 200, the landing page
```

Then, in a browser: switch EN / 中文, upload the 101 PDF and reach the
calibration workflow, upload the 102 PDF, upload a DXF, upload a DWG and watch
the conversion stages, confirm the progress bar moves during a large job, and
export JSON and CSV. Use the real production drawings for this and never commit
them.

### Local acceptance, for comparison

All five production drawings, each through the real page against a freshly started
server. Progress was monotonic in every run, reached 100 % before the workspace
replaced it, animated every stage whose internal progress is unknown, loaded the
result card, and left no conversion workspace or spooled upload behind.

| Drawing | Result |
|---|---|
| 101 PDF | scale unverified — no text layer to calibrate from |
| 102 PDF | scale unverified — no text layer to calibrate from |
| 103 DWG | **2,024.95 m²** · 3,388 components, 3,405 holes · 23 layers (15 used), 197 blocks (46 inserted) · `$INSUNITS 4` |
| 101 DWG | scale unverified — `$INSUNITS 0`, unitless, and only 4 dimensions · 15,051 components · 11 layers (4 used), 109 blocks (100 inserted) |
| 102 DWG | **89,646.90 m²** · 11,715 components, 24,855 holes · 160 layers (95 used), 1,866 blocks (262 inserted) · `$INSUNITS 4` |

Two of the five decline to state an area. That is the intended behaviour, not a
gap: a drawing that declares no units and carries no dimension consensus cannot
be measured without an operator calibrating two points, and inventing a scale
would be the one unforgivable failure for this tool.
