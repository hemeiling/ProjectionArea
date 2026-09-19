# Sample drawings

## Your real drawings

Drop real engineering PDFs straight into this directory. Nothing in here is
committed — `.gitignore` keeps every file but this README out of version control,
because these are customer drawings.

Validate the whole set at once, with a comparison table and an overlay per page:

```bash
.venv/bin/python -m tools.validate_drawings samples --out-dir validation
```

Or audit a single page:

```bash
.venv/bin/python -m tools.audit_overlay samples/YOUR-DRAWING.pdf --page 3 --out audit.png
```

Both print the classification, the recovered scale and its evidence, the
projected area, the confidence breakdown and every warning, and write a PNG
showing exactly which geometry was counted.

**If a drawing is encrypted at rest** — the four files in `Inputs/` are — the
harness reports it as `BLOCKED` and names the reason. Such a file has no `%PDF`
header anywhere in it and cannot be repaired; export a decrypted copy from the
application that owns it.

Nothing here is uploaded anywhere. The backend stores uploads in a private
temporary directory and deletes them on request or after four hours.

## Generated fixtures

`samples/generated/` holds synthetic CAD-style drawings with analytically known
areas, rebuilt on demand and excluded from version control:

```bash
.venv/bin/python -m tests.fixtures samples/generated
```

| Drawing | Ground truth | Exercises |
|---|---|---|
| `plate_with_holes.pdf` | 23 057.52 mm² net, 24 000 mm² gross | 1:2 vector drawing, 3 holes, dimension consensus |
| `two_views.pdf` | 14 982.12 mm² top, 6 400 mm² front | two views on one sheet, section hatching |
| `broken_contour.pdf` | 12 793.14 mm² | a 0.9 mm outline break, repair and escalation |
| `layout_1_100.pdf` | 54.46 m² | 1:100 floor plan, L-shaped profile, metre-scale dimensions |
| `raster_plate.pdf` | 23 057.52 mm² | scanned page: raster tracing, refusal to guess scale |
