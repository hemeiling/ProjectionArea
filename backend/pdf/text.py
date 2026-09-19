"""Text extraction and drawing-annotation parsing.

CONSTITUTION.md §6: OCR (and text in general) is *not* geometry. Text is used
here for three things only — masking annotation linework, recovering the
nominal drawing scale, and supplying dimension values that the calibration
stage matches against measured vector distances.
"""

from __future__ import annotations

import math
import re
import unicodedata
from dataclasses import dataclass
from typing import Any, Dict, List, Optional, Tuple

from backend.models import BBox, TextItem

#: ``SCALE 1:2``, ``比例 1：100``, ``SCALE 2:1``, ``M 1:50``.
SCALE_RE = re.compile(
    r"(?:scale|比例|比 例|maßstab|echelle|échelle|scl)?\s*[:：]?\s*"
    r"(?P<a>\d{1,4}(?:\.\d+)?)\s*[:：]\s*(?P<b>\d{1,5}(?:\.\d+)?)",
    re.IGNORECASE,
)

#: Explicit unit declarations found in general notes.
_UNIT_PATTERNS: List[Tuple[re.Pattern, str]] = [
    (re.compile(r"\b(?:dim(?:ension)?s?\s+(?:are\s+)?in|units?\s*[:：]?)\s*(mm|millimet(?:er|re)s?)\b", re.I), "mm"),
    (re.compile(r"\b(?:dim(?:ension)?s?\s+(?:are\s+)?in|units?\s*[:：]?)\s*(cm|centimet(?:er|re)s?)\b", re.I), "cm"),
    (re.compile(r"\b(?:dim(?:ension)?s?\s+(?:are\s+)?in|units?\s*[:：]?)\s*(m|met(?:er|re)s?)\b", re.I), "m"),
    (re.compile(r"\b(?:dim(?:ension)?s?\s+(?:are\s+)?in|units?\s*[:：]?)\s*(in|inch(?:es)?)\b", re.I), "in"),
    (re.compile(r"单\s*位\s*[:：]?\s*(mm|毫米)", re.I), "mm"),
    (re.compile(r"单\s*位\s*[:：]?\s*(m|米)\b", re.I), "m"),
    (re.compile(r"\bunless\s+otherwise\s+(?:specified|stated)[^.]{0,40}\b(mm)\b", re.I), "mm"),
]

#: View labels worth surfacing to the user. §17.
VIEW_LABEL_PATTERNS: List[Tuple[re.Pattern, str]] = [
    (re.compile(r"\b(top|plan)\s*view\b|\b俯视图\b|\b平面图\b", re.I), "top"),
    (re.compile(r"\bfront\s*view\b|\b主视图\b|\b正视图\b", re.I), "front"),
    (re.compile(r"\b(side|left|right|end)\s*view\b|\b侧视图\b|\b左视图\b|\b右视图\b", re.I), "side"),
    (re.compile(r"\bbottom\s*view\b|\b仰视图\b", re.I), "bottom"),
    (re.compile(r"\bsection\s+[A-Z]{1,2}\s*[-–]\s*[A-Z]{1,2}\b|\bsection\b|\b剖视图\b|\b剖面\b", re.I), "section"),
    (re.compile(r"\bdetail\s+[A-Z]\b|\bdetail\b|\b局部放大\b|\b详图\b", re.I), "detail"),
    (re.compile(r"\b(iso(?:metric)?|axonometric)\b|\b轴测\b|\b立体图\b", re.I), "isometric"),
]

#: A dimension value token. Handles ``Ø18``, ``R25``, ``4-Ø12``, ``2X 30``,
#: ``(425)``, ``425±0.2``, ``425 mm``, ``M8x1.25``.
_DIM_RE = re.compile(
    r"""
    (?:(?P<count>\d{1,3})\s*[xX×\-]\s*)?              # repeat count: 4-, 2X
    (?P<prefix>[ØøΦφ⌀]|R|SR|M|PHI|DIA|%%c)?\s*        # symbol prefix
    (?P<value>\d{1,7}(?:[.,]\d{1,4})?)                # the number
    (?:\s*(?P<unit>mm|cm|m|in|inch|")\b)?             # optional explicit unit
    """,
    re.VERBOSE,
)

#: Tokens that look numeric but are never lengths.
_NON_DIMENSION_CONTEXT = re.compile(
    r"(rev|sheet|page|drawing|dwg|part|no\.?|item|qty|date|scale|比例|图号|第|共|页|版本|数量)",
    re.IGNORECASE,
)


@dataclass
class DimensionText:
    """A numeric annotation that may be a real dimension.

    Attributes:
        value: Numeric value as written on the drawing.
        unit: Unit inferred for this token (defaults to the sheet unit).
        value_mm: ``value`` converted to millimetres.
        kind: ``linear`` | ``diameter`` | ``radius`` | ``thread``.
        count: Repeat prefix, e.g. 4 in ``4-Ø12``.
        reference: True for parenthesised reference dimensions.
        raw: The original span text.
        bbox: Where the text sits, in PDF units.
        direction: Text writing direction, used to pair with dimension lines.
    """

    value: float
    unit: str
    value_mm: float
    kind: str
    count: int
    reference: bool
    raw: str
    bbox: BBox
    direction: Tuple[float, float]

    def as_dict(self) -> Dict[str, Any]:
        return {
            "value": self.value,
            "unit": self.unit,
            "value_mm": self.value_mm,
            "kind": self.kind,
            "count": self.count,
            "reference": self.reference,
            "raw": self.raw,
            "bbox": self.bbox.as_dict(),
        }


def extract_text_items(page: Any) -> List[TextItem]:
    """Collect every text span on the page with its bounding box and direction."""
    items: List[TextItem] = []
    data = page.get_text("dict")
    for block in data.get("blocks", []):
        if block.get("type") != 0:
            continue
        for line in block.get("lines", []):
            direction = tuple(line.get("dir", (1.0, 0.0)))
            for span in line.get("spans", []):
                text = span.get("text", "")
                if not text.strip():
                    continue
                x0, y0, x1, y1 = span["bbox"]
                items.append(
                    TextItem(
                        text=text,
                        bbox=BBox(x0, y0, x1, y1),
                        size=float(span.get("size", 0.0)),
                        direction=(float(direction[0]), float(direction[1])),
                    )
                )
    return items


def detect_sheet_unit(page_text: str) -> Optional[str]:
    """Infer the drawing's dimension unit from general notes.

    Returns ``None`` when no explicit declaration exists — the caller must then
    treat ``mm`` as an *assumption* and say so (§3: never quietly assume units).
    """
    normalised = unicodedata.normalize("NFKC", page_text)
    for pattern, unit in _UNIT_PATTERNS:
        if pattern.search(normalised):
            return unit
    return None


def detect_scale_ratio(text_items: List[TextItem]) -> Optional[Dict[str, Any]]:
    """Find the printed drawing scale, e.g. ``SCALE 1:2``.

    The printed ratio is recorded as *evidence*, never as truth: a PDF that was
    printed "fit to page" invalidates it entirely (§4).

    Returns:
        ``{"a": 1.0, "b": 2.0, "denominator": 2.0, "text": "SCALE 1:2"}`` or None.
    """
    best: Optional[Dict[str, Any]] = None
    for item in text_items:
        normalised = unicodedata.normalize("NFKC", item.text)
        match = SCALE_RE.search(normalised)
        if not match:
            continue
        a, b = float(match.group("a")), float(match.group("b"))
        if a <= 0 or b <= 0 or a > 1000 or b > 10000:
            continue
        labelled = bool(re.search(r"scale|比例|maßstab|echelle|scl", normalised, re.I))
        # A bare "1:2" anywhere on a sheet is weak; a labelled one is strong.
        candidate = {
            "a": a,
            "b": b,
            "denominator": b / a,
            "text": item.text.strip(),
            "labelled": labelled,
            "bbox": item.bbox,
        }
        if best is None or (labelled and not best["labelled"]):
            best = candidate
    return best


def detect_view_labels(text_items: List[TextItem]) -> List[Tuple[str, TextItem]]:
    """Find view captions such as ``TOP VIEW`` or ``SECTION A-A``."""
    found: List[Tuple[str, TextItem]] = []
    for item in text_items:
        for pattern, name in VIEW_LABEL_PATTERNS:
            if pattern.search(item.text):
                found.append((name, item))
                break
    return found


def parse_dimension_texts(
    text_items: List[TextItem], sheet_unit: str = "mm"
) -> List[DimensionText]:
    """Turn numeric text spans into candidate dimension values.

    Args:
        text_items: Spans from :func:`extract_text_items`.
        sheet_unit: Unit assumed when a token carries none.

    Returns:
        Dimension candidates in reading order. Tokens whose surrounding text
        marks them as metadata (sheet numbers, revisions, dates) are dropped.
    """
    from backend.units import to_mm

    results: List[DimensionText] = []
    for item in text_items:
        raw = unicodedata.normalize("NFKC", item.text).strip()
        if not raw or not any(ch.isdigit() for ch in raw):
            continue
        if _NON_DIMENSION_CONTEXT.search(raw):
            continue
        if SCALE_RE.fullmatch(raw.replace(" ", "")):
            continue
        reference = raw.startswith("(") and raw.endswith(")")
        body = raw.strip("()[] ")
        # Tolerance suffixes carry no calibration information; strip them.
        body = re.split(r"[±±]|\s[+\-]\s*\d", body)[0].strip()

        match = _DIM_RE.match(body)
        if not match:
            continue
        # Guard against dates and part numbers such as 2026-08 or 104-NSY1682.
        if re.match(r"^\d{4}[-/]\d{1,2}", body) or re.match(r"^\d+[A-Za-z]{2,}", body):
            continue

        try:
            value = float(match.group("value").replace(",", "."))
        except ValueError:
            continue
        if value <= 0:
            continue

        prefix = (match.group("prefix") or "").upper()
        kind = "linear"
        if prefix in {"Ø", "ø", "Φ", "φ", "⌀", "PHI", "DIA", "%%C"}:
            kind = "diameter"
        elif prefix in {"R", "SR"}:
            kind = "radius"
        elif prefix == "M":
            kind = "thread"

        unit_token = (match.group("unit") or "").lower()
        unit = {"inch": "in", '"': "in"}.get(unit_token, unit_token) or sheet_unit
        try:
            value_mm = to_mm(value, unit)
        except ValueError:
            continue

        results.append(
            DimensionText(
                value=value,
                unit=unit,
                value_mm=value_mm,
                kind=kind,
                count=int(match.group("count") or 1),
                reference=reference,
                raw=raw,
                bbox=item.bbox,
                direction=item.direction,
            )
        )
    return results


def text_mask_boxes(text_items: List[TextItem], pad_factor: float = 0.35) -> List[BBox]:
    """Bounding boxes covering text, padded so glyph decoration is included.

    Linework falling inside these boxes is annotation, not profile (§2).
    """
    boxes: List[BBox] = []
    for item in text_items:
        pad = max(item.size * pad_factor, 0.5)
        boxes.append(item.bbox.padded(pad))
    return boxes
