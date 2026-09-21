# Incident: hosted 103 DWG run restarted at ~70 %

Written for: whoever investigates the next hosted failure, or decides whether the
CAD pipeline moves into a child process.

**Status: cause narrowed, not yet confirmed from production logs.** The
instrumentation added for this incident is what confirms it. This records what is
measured, what is inferred, and what the next hosted run will settle.

## What happened

Render service `projection-area-analyzer-docker`, 2 CPU / 16 GB, one instance,
`WEB_CONCURRENCY=1`.

```
POST /api/analyse                    202
GET  /api/jobs/d7cded39fd754917      200   repeatedly, ~2 minutes
                                           progress reached ~70 % (geometry)
GET  /api/jobs/…                     502
                                     instance restarted ~2 s later
```

Render memory telemetry, 30-second resolution:

```
01:45:30   0.59 GB
01:46:00   0.81 GB
01:46:30   1.45 GB      17 s before the restart
limit     16    GB
```

## What was measured locally

The same drawing, through the same code path, with stage instrumentation. macOS,
twelve cores — faster per core than the hosted instance, which matters below.

| Stage | Time | Peak RSS after | Event loop blocked ≥ 2 s |
|---|---|---|---|
| `dwg2dxf` (child process) | 1.5 s | child 441 MB | — |
| `dxf.parse` — ezdxf reads 120 MB | **21.3 s** | 1.0 GB | **2.3 s** |
| `dxf.expand_and_normalise` — INSERTs expanded, 421,517 primitives | **13.4 s** | 2.6 GB | **2.3 s** |
| `segments` — 1,688,512 segments built | 13.8 s | 3.4 GB | — |
| `noding` — one `unary_union` | 16.7 s | 5.1 GB | — |
| `polygonize` — 35,616 faces | 9.1 s | **6.7 GB** | — |
| `union` — 14,267 parts | 2.2 s | 6.7 GB | — |

## Finding 1 — this was not memory exhaustion

The DWG progress plan puts `geometry` at 18–70 %. That stage *is* the DXF load:
`dxf.parse` plus `dxf.expand_and_normalise`. At ~70 % the run was finishing that
stage.

The memory agrees. Locally, `dxf.expand_and_normalise` runs from 1.0 GB to 2.6 GB;
the hosted instance read **1.45 GB** seventeen seconds before it died. Both place
the failure in the same stage, at the same scale.

**The 6.7 GB peak happens three stages later, in `polygonize`.** The hosted run
never reached it. A larger instance would not have helped, so none is recommended.

A sub-30-second spike cannot be ruled out from telemetry alone, but it would have to
take the process from ~2 GB to 16 GB inside a stage that locally never exceeds
2.6 GB. Nothing measured supports that.

## Finding 2 — the process stops answering HTTP during exactly that stage

The analysis runs on a worker thread and is meant to leave the server free. It does
not, because both CAD-loading stages are dominated by work that holds the GIL:

- `/health` took **6.3 s** (then a sync endpoint) and a job poll **2.8 s** — on
  twelve cores.
- An asyncio watchdog recorded the **event loop itself** blocked for **2.3 s** at a
  time, in `dxf.parse` and in `dxf.expand_and_normalise`, and nowhere else.

And, correcting an early guess: **the native geometry stages do not block**.
`unary_union` and `polygonize` run for 26 seconds combined with zero loop stalls —
Shapely 2 releases the GIL inside GEOS. The problem is the Python-level CAD load,
not the geometry engine.

## Leading hypothesis — and what would confirm it

On a two-CPU instance with slower cores, the same stalls last correspondingly
longer. If one exceeds Render's health-check timeout, Render concludes the instance
is dead and restarts it — a healthy process, part way through a measurement, well
inside its memory limit. A job poll in flight at that moment gets a 502.

That fits every observation: the timing, the stage, the memory, and the 502
immediately followed by a restart. **It is not yet proven.** Two things would
confirm or refute it:

1. **Render's Events tab** for the restart at ~01:46:47. A health-check failure is
   recorded as such; an out-of-memory kill is recorded differently.
2. **The next hosted run's logs**, now that the instrumentation is deployed:

| Last lines before a restart | Diagnosis |
|---|---|
| `event loop blocked 8.4s …` then silence | health check starved — this hypothesis |
| `container=15900MB/16000MB` climbing, then silence | memory exhaustion |
| `stage dxf.… begin`, modest memory, no stall warning, then silence | native crash in the process |
| `subprocess dwg2dxf killed by SIGKILL` / `SIGSEGV` | the converter, not the analysis |

Not excluded: a native crash inside ezdxf's parse of LibreDWG output on Linux that
does not occur on macOS. The table above tells it apart.

## A mistake made during this investigation

The first version of the instrumentation logged an `analysis.complete` line that
read `result.geometry.hole_count`. The field is `holes`. The resulting
`AttributeError` failed every analysis at 100 % — including the instrumented local
103 runs above, which completed every geometry stage and then failed on the log
line. Their stage timings and memory figures are valid, because they were recorded
before the failure; the runs themselves did not complete.

It was caught by the test suite, not by reading the stage logs, which is a lesson
worth keeping. The fix is structural rather than a corrected spelling: diagnostic
facts are now read with `facts_of()`, which turns a missing attribute into `None`,
the call is guarded, and `note()` cannot raise. Two tests hold that line — one that
an analysis completes with diagnostics on, and one that a diagnostic which throws
still leaves the job intact.

## What was changed

Nothing about any measurement. The baseline oracle reports an identical engineering
result with all instrumentation in place.

- **Stage diagnostics** (`backend/diagnostics.py`): PID, stage, elapsed time, RSS,
  VMS, peak RSS, child peak RSS, and container memory and limit from cgroups — logged
  before and after `dxf.parse`, `dxf.expand_and_normalise`, `segments`, `noding`,
  `polygonize`, the STRtree build and `union`. Exception *type* only on failure,
  never its message, because a CAD library's message can quote file contents.
- **Converter outcome**: exit code, or the signal name when it was killed —
  `SIGKILL` and `SIGSEGV` are different diagnoses that "conversion failed" hides.
- **Event-loop watchdog**: an asyncio task, deliberately not a thread. A plain
  thread is scheduled promptly under GIL contention and reported nothing; the
  first version of this watchdog saw a healthy process while `/health` was taking
  six seconds.
- **`/health` and `/api/jobs/{id}` are now async**, served from the event loop
  instead of the threadpool the analysis starves. Job polls fell from 2.8 s to
  1.1 s. This does **not** stop the event-loop stalls, which come from the GIL, not
  from the threadpool — it removes one layer of delay, not the cause.
- **The database probe never blocks**: it answers from a cached value and
  refreshes on its own thread, so an unreachable database cannot slow the health
  check.

## Recommendation: the CAD pipeline belongs in a child process

Not implemented, as agreed, until a hosted run confirms the cause. But the evidence
already points one way.

A separate process has its own interpreter and its own GIL. Nothing it does can
stall the web server's event loop, so the health check and job polls stay fast
whatever the CAD load is doing. It also turns the failure modes this incident could
not distinguish into facts the parent observes directly: the worker exits 0, raises
(and reports it), is killed by `SIGKILL` (out of memory), or dies of `SIGSEGV`
(native crash). The parent stays alive to record which, and to tell the operator.

What it costs: the result has to cross a process boundary — 45 MB for the largest
drawing, serialised once — and the job store has to learn to track a PID. Both are
modest next to an instance that restarts mid-measurement.

The in-process fixes above make the current design as good as it can be. They
cannot make it immune, because the GIL is shared by every thread in the process.
