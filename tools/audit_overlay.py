"""Render an audit overlay to PNG, outside the browser.

CONSTITUTION.md §8: every result must be visually verifiable. The viewer does
this live with SVG; this script does the same thing headlessly so a result can
be checked in CI, attached to a report, or inspected over SSH.

Usage::

    .venv/bin/python -m tools.audit_overlay DRAWING.pdf --page 1 --out audit.png
    .venv/bin/python -m tools.audit_overlay DRAWING.pdf --region r2 --calibrate 425 \\
        --points 100,200 500,200
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from typing import List, Optional, Sequence

import fitz

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from backend.area.projected import compute_projected_area
from backend.calibration.scale import scale_from_two_points
from backend.models import AreaResult, GeometryRole, ViewSource
from backend.pipeline import prepare_page, region_scale
from backend.units import Area

#: Overlay palette, matching the viewer (§8).
INCLUDED = (0.24, 0.60, 0.55)
HOLE = (0.60, 0.20, 0.07)
IGNORED = (0.29, 0.35, 0.37)
UNCERTAIN = (0.71, 0.50, 0.16)
REGION = (0.71, 0.50, 0.16)


def draw_overlay(page: fitz.Page, result: AreaResult, primitives: Sequence, dpi: int) -> fitz.Pixmap:
    """Paint the audit overlay onto a copy of the page and rasterise it."""
    shape = page.new_shape()

    for prim in primitives:
        if prim.role in (GeometryRole.PROFILE,):
            continue
        colour = UNCERTAIN if prim.role is GeometryRole.UNCERTAIN else IGNORED
        points = [fitz.Point(x, y) for x, y in prim.points]
        if len(points) >= 2:
            shape.draw_polyline(points)
            shape.finish(color=colour, width=0.5, closePath=False, stroke_opacity=0.45)

    for component in result.components:
        outer = [fitz.Point(x, y) for x, y in component.outer]
        if len(outer) >= 3:
            shape.draw_polyline(outer)
            shape.finish(
                color=INCLUDED if component.included else IGNORED,
                fill=INCLUDED if component.included else None,
                fill_opacity=0.28 if component.included else 0,
                width=1.4, closePath=True, stroke_opacity=0.95,
            )
        for hole in component.holes:
            ring = [fitz.Point(x, y) for x, y in hole]
            if len(ring) >= 3:
                shape.draw_polyline(ring)
                shape.finish(color=HOLE, fill=HOLE, fill_opacity=0.30, width=1.1, closePath=True)

    if result.region_bbox:
        box = result.region_bbox
        shape.draw_rect(fitz.Rect(box.x0, box.y0, box.x1, box.y1))
        shape.finish(color=REGION, width=1.6, dashes="[6 4] 0")

    shape.commit()
    return page.get_pixmap(matrix=fitz.Matrix(dpi / 72.0, dpi / 72.0), alpha=False)


def main(argv: Optional[List[str]] = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("pdf")
    parser.add_argument("--page", type=int, default=1)
    parser.add_argument("--region", help="region id, e.g. r1; default is the strongest view")
    parser.add_argument("--whole-page", action="store_true", help="measure the entire page")
    parser.add_argument("--calibrate", type=float, help="known distance for two-point calibration")
    parser.add_argument("--unit", default="mm", help="unit of --calibrate")
    parser.add_argument("--points", nargs=2, help="x,y x,y in PDF units, with --calibrate")
    parser.add_argument("--keep-holes", action="store_true", help="do not subtract enclosed holes")
    parser.add_argument("--out", default="audit.png")
    parser.add_argument("--dpi", type=int, default=150)
    parser.add_argument("--json", help="also write the full result as JSON")
    args = parser.parse_args(argv)

    doc = fitz.open(args.pdf)
    prepared = prepare_page(doc, args.page)

    region = None
    if not args.whole_page:
        region = prepared.region_by_id(args.region) if args.region else prepared.default_region()
        if args.region and region is None:
            parser.error(f"no region {args.region!r}; available: {[r.id for r in prepared.regions]}")

    warnings: List[str] = []
    if args.calibrate is not None:
        if not args.points:
            parser.error("--calibrate requires --points x0,y0 x1,y1")
        from backend.units import to_mm

        a, b = (tuple(float(v) for v in p.split(",")) for p in args.points)
        scale = scale_from_two_points(a, b, to_mm(args.calibrate, args.unit))
    else:
        scale, warnings = region_scale(prepared, region)

    result = compute_projected_area(
        analysis=prepared.analysis,
        fitz_page=doc.load_page(args.page - 1),
        document_id="cli",
        file_name=os.path.basename(args.pdf),
        scale=scale,
        region_bbox=region.bbox if region else None,
        view_source=ViewSource.USER_SELECTED if region else ViewSource.WHOLE_PAGE,
        view_label=region.label if region else "Whole page",
        subtract_holes=not args.keep_holes,
        extra_warnings=warnings,
    )

    pixmap = draw_overlay(doc.load_page(args.page - 1), result, prepared.analysis.primitives, args.dpi)
    pixmap.save(args.out)

    payload = result.as_dict()
    if args.json:
        with open(args.json, "w") as handle:
            json.dump(payload, handle, indent=2)

    print(f"file      {result.file_name}  page {result.page}")
    print(f"type      {result.drawing_type.value}")
    print(f"view      {result.view_label} ({result.view_source.value})")
    print(f"method    {result.method.value}")
    print(f"scale     {result.scale.source.value}"
          + (f"  {result.scale.mm_per_unit:.6f} mm/unit  {payload['scale']['implied_ratio']}"
             if result.scale.verified else "  NOT VERIFIED"))
    if result.scale.verified:
        area = Area(result.area_mm2)
        print(f"area      {area.display('mm2')} mm²   {area.display('cm2')} cm²   "
              f"{area.display('m2')} m²   {area.display('in2')} in²")
    else:
        print("area      Scale not verified. Physical projected area cannot yet be calculated.")
        print(f"          (page-space area {result.area_units2:.2f} PDF units², not a manufacturing dimension)")
    print(f"geometry  {result.geometry.outer_contours} outer, {result.geometry.holes} holes, "
          f"{result.geometry.ignored_primitive_count} primitives ignored")
    print(f"confidence {result.confidence.overall * 100:.0f}% ({result.confidence.band})")
    for repair in result.geometry.repairs:
        print(f"  repair   {repair.type} x{repair.count}: {repair.detail}")
    for warning in result.warnings:
        print("  warning  " + warning.replace("\n", "\n           "))
    print(f"overlay   {args.out}")
    doc.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
