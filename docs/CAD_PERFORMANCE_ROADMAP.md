# CAD performance roadmap

Written for: whoever picks up the CAD pipeline's performance work later.

**This is a direction, not a task list, and none of it is started.** The current
priority is correctness, deployment and validation. This file exists so the
thinking is not lost, and so the constraint that governs the work is recorded
before anyone is tempted to relax it.

## The rule that governs all of it

> **The current implementation is the correctness oracle. Any future optimised
> implementation must reproduce it.**

An optimisation is accepted only when

```
optimised result == baseline engineering result
```

within the geometry engine's stated numerical tolerance. Counts — components,
holes, primitives — must match **exactly**. If an optimisation changes an area, a
topology, a count or an interpretation, that is a finding to investigate, never a
cost of speed to accept.

`tools/baseline.py` captures and compares those baselines. Establish them before
touching anything here.

## What we are optimising away from

Measured end to end on the production drawings, on a 32 GB laptop, with a freshly
started server per drawing:

| Drawing | Input | Primitives | Segments | Faces | Wall | Peak RSS |
|---|---|---|---|---|---|---|
| 103 DWG | 30.1 MB | 421,517 | — | — | 102 s | 6.7 GB |
| 101 DWG | 28.2 MB | 893,476 | 2,728,025 | 221,192 | 161 s | 9.4 GB |
| 102 DWG | 97.1 MB | 2,374,784 | 6,900,951 | 175,231 | 496 s | 11.7 GB |

Conversion is not the problem: LibreDWG turns the 97 MB DWG into 381 MB of DXF in
five seconds. The cost is everything after it. The present shape is:

```
DWG
  ↓  LibreDWG
full DXF on disk
  ↓  ezdxf
the entire CAD document in memory
  ↓  normalisation
hundreds of thousands to millions of Python objects
  ↓
all polygons at once
  ↓
one very large boolean union
```

Two copies of the drawing are live simultaneously — the ezdxf document and the
normalised primitives — and the union sees everything at once.

## The shape to investigate

```
DWG
  ↓
CAD extraction
  ↓
lightweight metadata pass
  ├─ units
  ├─ extents
  ├─ layers
  ├─ blocks
  ├─ spaces / layouts
  └─ entity counts
  ↓
structured geometry processing
  ├─ batch / stream entities
  ├─ preserve provenance
  ├─ process spatially
  ├─ process hierarchically
  └─ release intermediates early
  ↓
footprint engine
```

## Candidate lines of work

Each is independent, and each needs its own benchmark and its own baseline
comparison. They are listed in the order I would try them, which is roughly
expected benefit over risk — not an order they must be done in.

1. **Two-pass processing.** Metadata first — units, extents, layers, blocks,
   layouts, entity counts — then geometry. The metadata pass is what the UI needs
   to show a drawing at all, and it is cheap; today it comes out of the same
   traversal that builds every primitive.
2. **Stream the DXF rather than retaining the document and a normalised copy.**
   Read entities iteratively, emit normalised geometry, drop the native object.
   This attacks the "two copies live at once" problem directly.
3. **Early release of native objects** once their normalised form exists — the
   narrow version of the above, worth doing even if streaming proves hard.
4. **Hierarchical block processing.** A block inserted 200 times is normalised 200
   times into unrelated objects today. Normalise once, transform per insert. The
   102 drawing has 1,866 blocks with 262 inserted; the 101 has 109 with 100
   inserted, so this is not a marginal case.
5. **Spatial tiling / component partitioning before the union**, so the boolean
   work is done on bounded neighbourhoods rather than the whole sheet.
6. **Hierarchical unions** instead of one enormous boolean operation — pairwise or
   tree-shaped, which also bounds peak memory during the union.
7. **Compact geometry representations** — coordinate arrays rather than large
   Python object graphs — where it does not cost clarity in the geometry code.
8. **Use CAD semantics to organise computation**: layers and entity types are good
   partitioning keys. They are **not** grounds for exclusion. Nothing may be left
   out of an engineering result because a layer name looks irrelevant.
9. **Direct DWG extraction**, considered only later, and only if it beats
   LibreDWG → DXF materially *and* preserves provenance — layers, blocks, INSERT
   hierarchy, handles, units, dimensions, spaces, transformations, XREFs, source
   identity — as reliably as the current path.
10. **Background workers / a job queue**, so a 16 GB eight-minute job is not
    running inside the interactive web process. Today `JobStore` is a dict and a
    thread, which is right for one user and wrong for a shared instance.

## Related, and not a geometry problem

The 102 DWG's job response is **44.7 MB** — 27.9 MB of footprint polygon geometry
for all five readings, plus 15.8 MB of components that largely duplicate it.
Invisible on localhost; over the internet it is the most likely reason a deployed
instance feels broken, and the server pays that much memory to serialise it at the
very end of a job. Returning the summary and fetching geometry for the reading the
operator actually selected is a smaller, separate piece of work with a more
immediate payoff than anything above.

## What not to do

Not for performance, not ever for a number an engineer will rely on:

- geometry simplification before area computation
- heuristic dropping of small or awkward geometry
- layer exclusion by name
- semantic filtering to reduce memory

If a drawing needs 16 GB to produce the right answer today, it uses 16 GB. The
reference result comes first; making it cheaper comes second.
