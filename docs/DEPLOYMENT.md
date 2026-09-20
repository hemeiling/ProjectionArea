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
