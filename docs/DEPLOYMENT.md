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

Measured with `tools/measure_memory.py`, which runs each drawing in a fresh
process and reports peak resident size for the application and for the converter
child separately. **These numbers are from macOS 26.5 on arm64, Python 3.9** —
evidence about this machine. Linux figures will differ somewhat, but not by the
order of magnitude that decides the plan.

"Needs" is the larger of the two columns beside it, not their sum: `dwg2dxf` has
exited before a single entity is parsed, so the two peaks never coincide.

| Drawing | Input | Primitives | App | Converter | Needs | Time |
|---|---|---|---|---|---|---|
| 101 PDF | 2.3 MB | 313,138 | 798 MB | — | **0.8 GB** | 6.8 s |
| 102 PDF | 7.0 MB | 959,931 | 2,417 MB | — | **2.4 GB** | 21.6 s |
| 103 DWG | 30.1 MB | 421,517 | 2,526 MB | 441 MB | **2.5 GB** | 39.6 s |
| 101 DWG | 28.2 MB | 893,476 | 3,968 MB | 470 MB | **4.0 GB** | 48.1 s |
| 102 DWG | 97.1 MB | 2,374,784 | 8,166 MB | 1,551 MB | **8.2 GB** | 287 s |

The 102 DWG converts to a 120 MB DXF and takes 4.8 minutes end to end, of which
conversion is a small part — the time and the memory both go on reading 2.4
million entities.

### What this means for plan sizing

Render's plans, for reference: Free and Starter are both 512 MB, Standard
(`1c-2g`) is 2 GB, Pro (`2c-4g`) is 4 GB, then `2c-8g` and `2c-16g`.

| Plan | RAM | What it can actually do |
|---|---|---|
| Free / Starter | 512 MB | Demo drawings and the interface only. **No real drawing fits** — the smallest needs 0.8 GB. |
| Standard `1c-2g` | 2 GB | The 101 PDF. Fails on everything else. |
| Pro `2c-4g` | 4 GB | Both PDFs, the 103 DWG. Borderline on the 101 DWG at 4.0 GB against a 4 GB limit. |
| `2c-8g` | 8 GB | Both PDFs and the 101 and 103 DWGs, with margin. **Recommended minimum** for production drawings. |
| `2c-16g` | 16 GB | Adds the 102 DWG (8.2 GB). |

`render.yaml` sets `2c-8g`. That is a real cost decision, so it is stated rather
than buried: a smaller plan will not fail gracefully on a production drawing, it
will have the process killed part way through — which the application now explains
("Analysis interrupted", with the progress it had reached), but cannot avoid.

Nothing in the engine was weakened to fit a smaller instance. A projected area
that is wrong because the server was economising is worse than no answer.

### Why it is this large, and what would fix it

**The workload is memory-bound in the CAD reader, not in the web layer.** About
3.4 KB of Python objects per entity, times 2.4 million entities, is where the
gigabytes go. The upload path itself now costs one 1 MiB chunk.

The fix, if the large DWGs have to run on a small instance, is to stop holding
every primitive as a Python object: read entities in a streaming pass and keep
coordinates in numpy arrays rather than dataclasses. That is a real piece of
engineering with its own correctness risk — every geometry test exists to protect
exactly this code — so it is recorded here as the highest-value follow-up rather
than attempted as part of deployment preparation.

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
