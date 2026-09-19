"""Synthetic engineering drawings with exact ground truth.

CONSTITUTION.md §25/§26: synthetic fixtures are necessary but not sufficient.
They exist so geometry correctness can be validated independently of drawing
interpretation — every fixture here has an analytically known area.

Each drawing is generated the way a CAD package exports one: a sheet border, a
title block, real dimension lines with arrowheads and extension lines, dashed
centrelines, hatching and annotation text. That matters, because the pipeline
must *ignore* all of it and still find the profile.

The drafter works in real millimetres and converts to PDF units through a
declared drawing scale, so the fixtures also exercise automatic calibration
against a scale the test knows in advance.
"""

from __future__ import annotations

import math
import os
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Sequence, Tuple

import fitz

from backend.config import MM_PER_PDF_UNIT_AT_1_1

BLACK = (0.0, 0.0, 0.0)
THIN = 0.35
MEDIUM = 0.7
THICK = 1.0


@dataclass
class Drafter:
    """Draws in real millimetres onto a PDF page at a declared drawing scale.

    Attributes:
        page: Target ``fitz.Page``.
        ratio_denominator: ``N`` in a 1:N drawing ratio.
        origin: Where model (0, 0) sits on the sheet, in PDF units.
    """

    page: fitz.Page
    ratio_denominator: float
    origin: Tuple[float, float]
    _shape: Optional[fitz.Shape] = field(default=None, repr=False)

    @property
    def mm_per_unit(self) -> float:
        """Ground-truth scale: millimetres of the real part per PDF unit."""
        return MM_PER_PDF_UNIT_AT_1_1 * self.ratio_denominator

    def units(self, mm: float) -> float:
        return mm / self.mm_per_unit

    def p(self, x_mm: float, y_mm: float) -> fitz.Point:
        return fitz.Point(self.origin[0] + self.units(x_mm), self.origin[1] + self.units(y_mm))

    @property
    def shape(self) -> fitz.Shape:
        if self._shape is None:
            self._shape = self.page.new_shape()
        return self._shape

    # ── primitives ──────────────────────────────────────────────────────────

    def polyline(self, points_mm: Sequence[Tuple[float, float]], width: float = THICK,
                 close: bool = True, dashes: Optional[str] = None) -> None:
        shape = self.shape
        shape.draw_polyline([self.p(x, y) for x, y in points_mm])
        shape.finish(color=BLACK, width=width, closePath=close, dashes=dashes)

    def rect(self, x_mm: float, y_mm: float, w_mm: float, h_mm: float, width: float = THICK) -> None:
        self.polyline(
            [(x_mm, y_mm), (x_mm + w_mm, y_mm), (x_mm + w_mm, y_mm + h_mm), (x_mm, y_mm + h_mm)],
            width=width,
        )

    def circle(self, cx_mm: float, cy_mm: float, r_mm: float, width: float = THICK) -> None:
        shape = self.shape
        shape.draw_circle(self.p(cx_mm, cy_mm), self.units(r_mm))
        shape.finish(color=BLACK, width=width)

    def line(self, a_mm: Tuple[float, float], b_mm: Tuple[float, float],
             width: float = THIN, dashes: Optional[str] = None) -> None:
        shape = self.shape
        shape.draw_line(self.p(*a_mm), self.p(*b_mm))
        shape.finish(color=BLACK, width=width, dashes=dashes, closePath=False)

    def centre_cross(self, cx_mm: float, cy_mm: float, r_mm: float) -> None:
        """Dashed centre lines, which must never enter the silhouette."""
        over = r_mm * 1.45
        self.line((cx_mm - over, cy_mm), (cx_mm + over, cy_mm), width=THIN, dashes="[6 2 1 2] 0")
        self.line((cx_mm, cy_mm - over), (cx_mm, cy_mm + over), width=THIN, dashes="[6 2 1 2] 0")

    def hatch(self, x_mm: float, y_mm: float, w_mm: float, h_mm: float, pitch_mm: float) -> None:
        """45-degree section hatching, which must be classified out."""
        steps = int((w_mm + h_mm) / pitch_mm)
        for i in range(1, steps):
            offset = i * pitch_mm
            ax, ay = x_mm + max(0.0, offset - h_mm), y_mm + min(offset, h_mm)
            bx, by = x_mm + min(offset, w_mm), y_mm + max(0.0, offset - w_mm)
            if ax < bx:
                self.line((ax, ay), (bx, by), width=THIN)

    def text(self, x_mm: float, y_mm: float, value: str, size: float = 8.0) -> None:
        self.commit()
        self.page.insert_text(self.p(x_mm, y_mm), value, fontsize=size, color=BLACK)

    # ── dimensioning ────────────────────────────────────────────────────────

    def _arrowhead(self, tip: fitz.Point, towards: fitz.Point, size: float = 4.5) -> None:
        dx, dy = towards.x - tip.x, towards.y - tip.y
        length = math.hypot(dx, dy) or 1.0
        ux, uy = dx / length, dy / length
        base = fitz.Point(tip.x + ux * size, tip.y + uy * size)
        half = size * 0.28
        shape = self.shape
        shape.draw_polyline(
            [tip, fitz.Point(base.x - uy * half, base.y + ux * half),
             fitz.Point(base.x + uy * half, base.y - ux * half)]
        )
        shape.finish(color=None, fill=BLACK, closePath=True)

    def dim_horizontal(self, x0_mm: float, x1_mm: float, y_mm: float, offset_mm: float,
                       label: Optional[str] = None, size: float = 8.0) -> str:
        """A horizontal dimension: extension lines, dimension line, arrows, text.

        The dimension line spans exactly ``x1 - x0`` millimetres, so automatic
        calibration must recover :attr:`mm_per_unit` from it exactly.
        """
        text_value = label if label is not None else f"{x1_mm - x0_mm:g}"
        dim_y = y_mm + offset_mm
        for x in (x0_mm, x1_mm):
            self.line((x, y_mm), (x, dim_y + math.copysign(2.0 * self.mm_per_unit, offset_mm)), width=THIN)
        self.line((x0_mm, dim_y), (x1_mm, dim_y), width=THIN)
        self._arrowhead(self.p(x0_mm, dim_y), self.p(x1_mm, dim_y))
        self._arrowhead(self.p(x1_mm, dim_y), self.p(x0_mm, dim_y))
        midpoint = self.p(0.5 * (x0_mm + x1_mm), dim_y)
        self.commit()
        self.page.insert_text(
            fitz.Point(midpoint.x - size * 0.3 * len(text_value), midpoint.y - size * 0.45),
            text_value, fontsize=size, color=BLACK,
        )
        return text_value

    def dim_vertical(self, y0_mm: float, y1_mm: float, x_mm: float, offset_mm: float,
                     label: Optional[str] = None, size: float = 8.0) -> str:
        text_value = label if label is not None else f"{y1_mm - y0_mm:g}"
        dim_x = x_mm + offset_mm
        for y in (y0_mm, y1_mm):
            self.line((x_mm, y), (dim_x + math.copysign(2.0 * self.mm_per_unit, offset_mm), y), width=THIN)
        self.line((dim_x, y0_mm), (dim_x, y1_mm), width=THIN)
        self._arrowhead(self.p(dim_x, y0_mm), self.p(dim_x, y1_mm))
        self._arrowhead(self.p(dim_x, y1_mm), self.p(dim_x, y0_mm))
        midpoint = self.p(dim_x, 0.5 * (y0_mm + y1_mm))
        self.commit()
        self.page.insert_text(
            fitz.Point(midpoint.x + size * 0.35, midpoint.y), text_value, fontsize=size, color=BLACK
        )
        return text_value

    def dim_diameter(self, cx_mm: float, cy_mm: float, r_mm: float, label: Optional[str] = None,
                     size: float = 7.5) -> str:
        """A diameter callout drawn across the circle, arrow to arrow."""
        text_value = label if label is not None else f"Ø{2 * r_mm:g}"
        angle = math.radians(35.0)
        ax = cx_mm - r_mm * math.cos(angle)
        ay = cy_mm - r_mm * math.sin(angle)
        bx = cx_mm + r_mm * math.cos(angle)
        by = cy_mm + r_mm * math.sin(angle)
        self.line((ax, ay), (bx, by), width=THIN)
        self._arrowhead(self.p(ax, ay), self.p(bx, by), size=3.2)
        self._arrowhead(self.p(bx, by), self.p(ax, ay), size=3.2)
        midpoint = self.p(cx_mm, cy_mm)
        self.commit()
        self.page.insert_text(
            fitz.Point(midpoint.x + 6, midpoint.y - 4), text_value, fontsize=size, color=BLACK
        )
        return text_value

    def commit(self) -> None:
        if self._shape is not None:
            self._shape.commit()
            self._shape = None


def _sheet_furniture(page: fitz.Page, title: str, scale_text: str, extra_notes: Sequence[str] = ()) -> None:
    """Draw the border and title block every real sheet carries."""
    rect = page.rect
    margin = 14.0
    shape = page.new_shape()
    shape.draw_rect(fitz.Rect(margin, margin, rect.width - margin, rect.height - margin))
    shape.finish(color=BLACK, width=1.4)

    block_w, block_h = 210.0, 62.0
    x0 = rect.width - margin - block_w
    y0 = rect.height - margin - block_h
    shape.draw_rect(fitz.Rect(x0, y0, x0 + block_w, y0 + block_h))
    shape.finish(color=BLACK, width=1.0)
    for i in (1, 2):
        shape.draw_line(fitz.Point(x0, y0 + i * block_h / 3), fitz.Point(x0 + block_w, y0 + i * block_h / 3))
        shape.finish(color=BLACK, width=0.5)
    shape.commit()

    page.insert_text(fitz.Point(x0 + 6, y0 + 14), title, fontsize=9)
    page.insert_text(fitz.Point(x0 + 6, y0 + 34), scale_text, fontsize=9)
    page.insert_text(fitz.Point(x0 + 6, y0 + 54), "ALL DIMENSIONS IN MM", fontsize=7.5)
    for index, note in enumerate(extra_notes):
        page.insert_text(fitz.Point(margin + 8, margin + 16 + index * 11), note, fontsize=7.5)


# ── fixture builders ─────────────────────────────────────────────────────────


def build_plate_with_holes(path: str) -> Dict[str, object]:
    """A 200 x 120 mm plate with three Ø20 holes, drawn at 1:2 on A3 landscape."""
    doc = fitz.open()
    page = doc.new_page(width=1190, height=842)  # A3 landscape, PDF units
    drafter = Drafter(page, ratio_denominator=2.0, origin=(140.0, 150.0))

    width_mm, height_mm, hole_r = 200.0, 120.0, 10.0
    holes = [(45.0, 60.0), (100.0, 60.0), (155.0, 60.0)]

    drafter.rect(0, 0, width_mm, height_mm, width=THICK)
    for cx, cy in holes:
        drafter.circle(cx, cy, hole_r, width=MEDIUM)
        drafter.centre_cross(cx, cy, hole_r)

    drafter.dim_horizontal(0, width_mm, height_mm, 26.0)
    drafter.dim_vertical(0, height_mm, width_mm, 26.0)
    drafter.dim_horizontal(0, holes[0][0], 0, -22.0)
    drafter.dim_diameter(holes[1][0], holes[1][1], hole_r)
    drafter.text(60, -40, "TOP VIEW", size=11)
    drafter.commit()

    _sheet_furniture(page, "PLATE 200x120", "SCALE 1:2", ["3 x Ø20 THRU", "MATERIAL: AL 6061"])
    doc.save(path)
    doc.close()

    return {
        "path": path,
        "mm_per_unit": MM_PER_PDF_UNIT_AT_1_1 * 2.0,
        "net_area_mm2": width_mm * height_mm - 3 * math.pi * hole_r ** 2,
        "gross_area_mm2": width_mm * height_mm,
        "hole_count": 3,
        "ratio_denominator": 2.0,
    }


def build_two_views(path: str) -> Dict[str, object]:
    """Two separate views on one sheet, so region selection actually matters."""
    doc = fitz.open()
    page = doc.new_page(width=1190, height=842)

    top = Drafter(page, ratio_denominator=2.0, origin=(120.0, 140.0))
    top.rect(0, 0, 160.0, 100.0)
    top.circle(80.0, 50.0, 18.0, width=MEDIUM)
    top.centre_cross(80.0, 50.0, 18.0)
    top.dim_horizontal(0, 160.0, 100.0, 24.0)
    top.dim_vertical(0, 100.0, 160.0, 24.0)
    top.text(40, -26, "TOP VIEW", size=11)
    top.commit()

    front = Drafter(page, ratio_denominator=2.0, origin=(700.0, 140.0))
    front.rect(0, 0, 160.0, 40.0)
    front.hatch(0, 0, 160.0, 40.0, 6.0)
    front.dim_vertical(0, 40.0, 160.0, 24.0)
    front.text(40, -26, "FRONT VIEW", size=11)
    front.commit()

    _sheet_furniture(page, "BRACKET", "SCALE 1:2")
    doc.save(path)
    doc.close()

    return {
        "path": path,
        "mm_per_unit": MM_PER_PDF_UNIT_AT_1_1 * 2.0,
        "top_net_area_mm2": 160.0 * 100.0 - math.pi * 18.0 ** 2,
        "front_area_mm2": 160.0 * 40.0,
    }


def build_broken_contour(path: str, gap_mm: float = 0.9) -> Dict[str, object]:
    """A plate whose outline has a sub-millimetre break, as bad exports produce."""
    doc = fitz.open()
    page = doc.new_page(width=842, height=595)
    drafter = Drafter(page, ratio_denominator=2.0, origin=(120.0, 140.0))

    w, h = 150.0, 90.0
    drafter.polyline([(0, 0), (w - gap_mm, 0)], close=False)
    drafter.polyline([(w, 0), (w, h), (0, h), (0, 0)], close=False)
    drafter.circle(75.0, 45.0, 15.0, width=MEDIUM)
    drafter.dim_horizontal(0, w, h, 24.0)
    drafter.dim_vertical(0, h, w, 24.0)
    drafter.commit()

    _sheet_furniture(page, "BROKEN OUTLINE", "SCALE 1:2")
    doc.save(path)
    doc.close()

    return {
        "path": path,
        "mm_per_unit": MM_PER_PDF_UNIT_AT_1_1 * 2.0,
        "net_area_mm2": w * h - math.pi * 15.0 ** 2,
        "gap_mm": gap_mm,
    }


def build_layout_1_100(path: str) -> Dict[str, object]:
    """A production-line footprint at 1:100, the large-dimension regime.

    Equipment footprints are the other half of this tool's job: same geometry,
    dimensions in the thousands of millimetres, area reported in square metres.
    """
    doc = fitz.open()
    page = doc.new_page(width=1190, height=842)
    drafter = Drafter(page, ratio_denominator=100.0, origin=(120.0, 180.0))

    # An L-shaped cell: 12000 x 6000 with a 4000 x 2000 notch removed.
    outline = [
        (0.0, 0.0), (12000.0, 0.0), (12000.0, 6000.0),
        (8000.0, 6000.0), (8000.0, 4000.0), (0.0, 4000.0),
    ]
    drafter.polyline(outline, width=THICK)
    drafter.circle(3000.0, 2000.0, 700.0, width=MEDIUM)   # rotary table opening
    drafter.centre_cross(3000.0, 2000.0, 700.0)

    drafter.dim_horizontal(0.0, 12000.0, 0.0, -900.0)
    drafter.dim_vertical(0.0, 4000.0, 0.0, -900.0)
    drafter.dim_horizontal(8000.0, 12000.0, 6000.0, 900.0)
    drafter.text(500.0, -1600.0, "CELL FOOTPRINT - PLAN VIEW", size=11)
    drafter.commit()

    _sheet_furniture(page, "CTP LINE CELL", "SCALE 1:100", ["UNITS: mm", "FLOOR PLAN"])
    doc.save(path)
    doc.close()

    # The outline removes the strip x < 8000, y > 4000 from a 12000 x 6000 sheet.
    gross = 12000.0 * 6000.0 - 8000.0 * 2000.0
    return {
        "path": path,
        "mm_per_unit": MM_PER_PDF_UNIT_AT_1_1 * 100.0,
        "gross_area_mm2": gross,
        "net_area_mm2": gross - math.pi * 700.0 ** 2,
        "ratio_denominator": 100.0,
    }


def build_obround_with_slot(path: str) -> Dict[str, object]:
    """A curved profile dimensioned *through* its own face.

    Two things here break naive pipelines:

    * The outline is an obround — two semicircular ends joined by straight
      flanks — so it is drawn as Bezier arcs, not line segments. Its area is
      still exact: ``(W - H) * H + pi * (H/2)^2``.
    * The dimensions are drawn *across the part*, not outside it, which is
      normal on a crowded sheet. Their extension lines cross the profile and
      their dimension lines run through it, so a pipeline that fails to demote
      them will slice the face into pieces or annexe area outside the part.
    """
    doc = fitz.open()
    page = doc.new_page(width=1190, height=842)
    drafter = Drafter(page, ratio_denominator=2.0, origin=(180.0, 200.0))

    width_mm, height_mm = 240.0, 90.0
    slot_w, slot_h = 90.0, 30.0
    outer_r, inner_r = height_mm / 2.0, slot_h / 2.0

    def obround(cx, cy, w, h, line_width):
        """Draw a stadium: straight flanks capped by true Bezier semicircles.

        The caps are two cubic quarter-arcs each, using the standard circular
        kappa. Building them explicitly rather than through ``draw_sector``
        keeps the sweep direction unambiguous, and means the fixture exercises
        the same Bezier flattening path a real CAD export would produce.
        """
        kappa = 0.5522847498307936
        r = h / 2.0
        left, right = cx - (w - h) / 2.0, cx + (w - h) / 2.0
        shape = drafter.shape

        def cap(hx, direction):
            """Semicircle at x = hx bulging along ``direction`` (+1 right)."""
            k = kappa * r * direction
            shape.draw_bezier(
                drafter.p(hx, cy - r), drafter.p(hx + k, cy - r),
                drafter.p(hx + r * direction, cy - kappa * r), drafter.p(hx + r * direction, cy),
            )
            shape.draw_bezier(
                drafter.p(hx + r * direction, cy), drafter.p(hx + r * direction, cy + kappa * r),
                drafter.p(hx + k, cy + r), drafter.p(hx, cy + r),
            )

        shape.draw_line(drafter.p(left, cy - r), drafter.p(right, cy - r))
        cap(right, +1)
        shape.draw_line(drafter.p(right, cy + r), drafter.p(left, cy + r))
        cap(left, -1)
        shape.finish(color=BLACK, width=line_width, closePath=False)

    obround(width_mm / 2.0, height_mm / 2.0, width_mm, height_mm, THICK)
    obround(width_mm / 2.0, height_mm / 2.0, slot_w, slot_h, MEDIUM)
    drafter.centre_cross(width_mm / 2.0, height_mm / 2.0, slot_w / 2.0)

    # Dimensions placed across the face, the crowded-sheet case.
    drafter.dim_horizontal(0.0, width_mm, height_mm / 2.0, -24.0)
    drafter.dim_horizontal(
        (width_mm - slot_w) / 2.0, (width_mm + slot_w) / 2.0, height_mm / 2.0, 24.0
    )
    drafter.dim_vertical(0.0, height_mm, width_mm, 26.0)
    drafter.text(4.0, -34.0, "PLAN VIEW", size=11)
    drafter.commit()

    _sheet_furniture(page, "OBROUND LINK", "SCALE 1:2", ["R45 ENDS", "SLOT 90x30"])
    doc.save(path)
    doc.close()

    def stadium_area(w, h):
        r = h / 2.0
        return (w - h) * h + math.pi * r ** 2

    gross = stadium_area(width_mm, height_mm)
    return {
        "path": path,
        "mm_per_unit": MM_PER_PDF_UNIT_AT_1_1 * 2.0,
        "gross_area_mm2": gross,
        "net_area_mm2": gross - stadium_area(slot_w, slot_h),
        "outer_radius_mm": outer_r,
        "inner_radius_mm": inner_r,
    }


def build_raster_plate(path: str, source_pdf: str, dpi: int = 200) -> Dict[str, object]:
    """A scanned-looking PDF: the plate drawing flattened to a single image."""
    source = fitz.open(source_pdf)
    page = source.load_page(0)
    pixmap = page.get_pixmap(matrix=fitz.Matrix(dpi / 72.0, dpi / 72.0), alpha=False)
    source.close()

    doc = fitz.open()
    out = doc.new_page(width=1190, height=842)
    out.insert_image(out.rect, pixmap=pixmap)
    doc.save(path)
    doc.close()
    return {"path": path, "dpi": dpi}


def build_all(directory: str) -> Dict[str, Dict[str, object]]:
    """Generate every fixture into ``directory`` and return their ground truth."""
    os.makedirs(directory, exist_ok=True)
    truth: Dict[str, Dict[str, object]] = {}
    truth["plate_with_holes"] = build_plate_with_holes(os.path.join(directory, "plate_with_holes.pdf"))
    truth["two_views"] = build_two_views(os.path.join(directory, "two_views.pdf"))
    truth["broken_contour"] = build_broken_contour(os.path.join(directory, "broken_contour.pdf"))
    truth["layout_1_100"] = build_layout_1_100(os.path.join(directory, "layout_1_100.pdf"))
    truth["obround_with_slot"] = build_obround_with_slot(
        os.path.join(directory, "obround_with_slot.pdf")
    )
    truth["raster_plate"] = build_raster_plate(
        os.path.join(directory, "raster_plate.pdf"), truth["plate_with_holes"]["path"]
    )
    return truth


if __name__ == "__main__":
    import json
    import sys

    target = sys.argv[1] if len(sys.argv) > 1 else "samples/generated"
    print(json.dumps(build_all(target), indent=2, default=str))
