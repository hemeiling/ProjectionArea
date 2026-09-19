"""Establish the physical scale of a drawing.

CONSTITUTION.md §4: PDF coordinates are never assumed to be manufacturing
dimensions. §3: when no scale can be established, no physical area is reported.

Three sources, strongest first
------------------------------
1. **User two-point calibration** — the engineer picks two points and states the
   real distance. Nothing beats it, so it always wins when supplied.
2. **Dimension consensus** — the automatic method implemented here. Every
   dimension annotation on the sheet is matched to the linework it dimensions,
   giving one candidate mm-per-unit each. The true scale is shared by *all* of
   them, so correct matches pile into one bin while mismatches scatter. The peak
   of that vote is the scale, and the number of independent dimensions agreeing
   is the evidence for it.
3. **Printed drawing ratio** — ``SCALE 1:2`` in the title block. Recorded, but
   never trusted on its own: printing a sheet "fit to page" silently invalidates
   it. When a consensus scale exists it is cross-checked against the printed
   ratio, and a mismatch is surfaced as a warning rather than hidden.
"""

from __future__ import annotations

import math
import statistics
from collections import defaultdict
from dataclasses import dataclass
from typing import Dict, List, Optional, Sequence, Tuple

from backend.config import CALIBRATION, MM_PER_PDF_UNIT_AT_1_1, CalibrationSettings
from backend.models import BBox, Point, Primitive, Scale, ScaleCandidate, ScaleSource

#: Physically plausible bounds on mm per PDF unit. Below this a sheet could not
#: hold readable linework; above it a single page would span kilometres.
_MIN_MM_PER_UNIT = 0.01
_MAX_MM_PER_UNIT = 2000.0

#: Nominal ratios a drawing office actually uses, for the sanity cross-check.
_COMMON_RATIOS = (
    0.1, 0.2, 0.5, 1, 2, 2.5, 4, 5, 10, 15, 20, 25, 30, 40, 50, 75,
    100, 125, 150, 200, 250, 500, 1000, 2000,
)

#: Angle bin when grouping nearby segments into one dimension line, degrees.
_ANGLE_BIN = 4.0


@dataclass
class _Measurement:
    """One matched (dimension text, measured PDF distance) pair."""

    value_mm: float
    units: float
    mm_per_unit: float
    text: str
    kind: str
    at: Point


def scale_from_ratio(denominator: float) -> Scale:
    """Scale implied by a printed drawing ratio 1:N, assuming a 1:1 print.

    Args:
        denominator: ``N`` in ``1:N``. Use ``0.5`` for a ``2:1`` enlargement.

    Returns:
        A :class:`Scale` carrying the assumption explicitly.
    """
    if denominator <= 0:
        raise ValueError("Drawing ratio denominator must be positive")
    return Scale(
        mm_per_unit=MM_PER_PDF_UNIT_AT_1_1 * denominator,
        source=ScaleSource.DRAWING_RATIO,
        confidence=0.50,
        detail=(
            f"Printed drawing ratio 1:{denominator:g}, assuming the PDF page is "
            f"at true sheet size (no fit-to-page rescaling)."
        ),
        evidence=[f"1 PDF unit = {MM_PER_PDF_UNIT_AT_1_1:.6f} mm of paper x {denominator:g}"],
    )


def scale_from_two_points(a: Point, b: Point, known_length_mm: float) -> Scale:
    """Scale from a user-measured span of known real length.

    Args:
        a: First picked point, PDF units.
        b: Second picked point, PDF units.
        known_length_mm: The real distance between them, in millimetres.

    Returns:
        A verified :class:`Scale`.

    Raises:
        ValueError: If the points coincide or the length is not positive.
    """
    distance = math.hypot(b[0] - a[0], b[1] - a[1])
    if distance <= 1e-6:
        raise ValueError("Calibration points are coincident; pick a longer span")
    if known_length_mm <= 0:
        raise ValueError("Known length must be positive")

    # A short pick amplifies click error. 200 units is roughly a third of an A4
    # width, below which a one-unit misclick costs more than half a percent.
    precision_penalty = min(1.0, distance / 200.0)
    confidence = 0.80 + 0.17 * precision_penalty

    return Scale(
        mm_per_unit=known_length_mm / distance,
        source=ScaleSource.USER_TWO_POINT,
        confidence=round(confidence, 4),
        detail=(
            f"User calibration: {known_length_mm:g} mm measured across "
            f"{distance:.3f} PDF units."
        ),
        evidence=[
            f"({a[0]:.2f}, {a[1]:.2f}) to ({b[0]:.2f}, {b[1]:.2f}) = {distance:.3f} units",
            f"declared real distance {known_length_mm:g} mm",
        ],
    )


def _segments_near(
    primitives: Sequence[Primitive], centre: Point, radius: float
) -> List[Tuple[Point, Point, float]]:
    """Straight two-point segments whose midpoint lies within ``radius``."""
    found: List[Tuple[Point, Point, float]] = []
    for prim in primitives:
        if prim.filled or len(prim.points) > 3:
            continue
        for a, b in zip(prim.points, prim.points[1:]):
            mid = (0.5 * (a[0] + b[0]), 0.5 * (a[1] + b[1]))
            if math.hypot(mid[0] - centre[0], mid[1] - centre[1]) > radius:
                continue
            length = math.hypot(b[0] - a[0], b[1] - a[1])
            if length > 0:
                found.append((a, b, length))
    return found


def _candidate_spans(segments: Sequence[Tuple[Point, Point, float]]) -> List[float]:
    """Distances a nearby dimension line could be measuring.

    Both individual segment lengths and, per direction family, the total extent
    of collinear segments are offered. The second case matters because many CAD
    exporters break a dimension line either side of its text, so neither half
    alone measures the dimension.
    """
    spans: List[float] = [length for _a, _b, length in segments]

    families: Dict[int, List[Tuple[Point, Point]]] = defaultdict(list)
    for a, b, _length in segments:
        angle = math.degrees(math.atan2(b[1] - a[1], b[0] - a[0])) % 180.0
        families[int(angle // _ANGLE_BIN)].append((a, b))

    for key, members in families.items():
        if len(members) < 2:
            continue
        angle = math.radians((key + 0.5) * _ANGLE_BIN)
        ux, uy = math.cos(angle), math.sin(angle)
        projections = [p[0] * ux + p[1] * uy for pair in members for p in pair]
        spans.append(max(projections) - min(projections))
    return spans


def collect_measurements(
    primitives: Sequence[Primitive],
    dimension_texts: Sequence,
    text_height: float,
    settings: CalibrationSettings = CALIBRATION,
    region: Optional[BBox] = None,
) -> List[_Measurement]:
    """Pair every dimension annotation with the linework it plausibly measures.

    Args:
        primitives: All primitives on the page — dimension linework included,
            since that is precisely what is being measured here.
        dimension_texts: Parsed dimension annotations.
        text_height: Median annotation height, the local length yardstick.
        settings: Calibration parameters.
        region: Restrict to annotations inside this box, when given.

    Returns:
        One measurement per (annotation, candidate span) pair that survives the
        plausibility filters.
    """
    radius = max(settings.text_search_radius_factor * text_height, 6.0)
    measurements: List[_Measurement] = []

    for dim in dimension_texts:
        if dim.kind == "thread":
            continue  # M8 is a thread designation, not a measurable distance.
        if not (settings.min_dimension_mm <= dim.value_mm <= settings.max_dimension_mm):
            continue
        centre = dim.bbox.center
        if region is not None and not region.contains_point(centre):
            continue

        nearby = _segments_near(primitives, centre, radius)
        if not nearby:
            continue

        for span in _candidate_spans(nearby):
            if span < settings.min_line_units:
                continue
            mm_per_unit = dim.value_mm / span
            if not (_MIN_MM_PER_UNIT <= mm_per_unit <= _MAX_MM_PER_UNIT):
                continue
            measurements.append(
                _Measurement(
                    value_mm=dim.value_mm,
                    units=span,
                    mm_per_unit=mm_per_unit,
                    text=dim.raw,
                    kind=dim.kind,
                    at=centre,
                )
            )
    return measurements


def vote_for_scale(
    measurements: Sequence[_Measurement], settings: CalibrationSettings = CALIBRATION
) -> Optional[ScaleCandidate]:
    """Find the scale that the largest number of independent dimensions agree on.

    Candidates are binned in log space, so the tolerance is relative rather than
    absolute and a 6000 mm dimension is judged as tightly as a 12 mm one. A bin
    is scored by the number of *distinct annotations* supporting it, never by
    the number of raw segment matches — otherwise one dimension surrounded by
    dense linework could outvote the rest of the sheet.

    Args:
        measurements: Output of :func:`collect_measurements`.
        settings: Calibration parameters.

    Returns:
        The winning candidate, or ``None`` when nothing could be matched.
    """
    if not measurements:
        return None

    step = math.log1p(settings.vote_relative_tolerance)
    bins: Dict[int, List[_Measurement]] = defaultdict(list)
    for measurement in measurements:
        bins[int(round(math.log(measurement.mm_per_unit) / step))].append(measurement)

    def distinct_texts(items: Sequence[_Measurement]) -> int:
        return len({(item.text, round(item.value_mm, 4)) for item in items})

    # Merge each bin with its immediate neighbours so a cluster straddling a bin
    # edge is not split in half.
    best_key, best_items, best_support = None, [], 0
    for key in sorted(bins):
        merged = bins.get(key - 1, []) + bins[key] + bins.get(key + 1, [])
        support = distinct_texts(merged)
        # Ties break toward the longer measured spans, which are more precise.
        if support > best_support or (
            support == best_support
            and best_items
            and sum(m.units for m in merged) > sum(m.units for m in best_items)
        ):
            best_key, best_items, best_support = key, merged, support

    if best_key is None or not best_items:
        return None

    # Weight by measured span: a 6000 mm dimension pins the scale far more
    # tightly than a 12 mm one carrying the same click and rounding error.
    weights = [item.units for item in best_items]
    total_weight = sum(weights) or 1.0
    mm_per_unit = sum(item.mm_per_unit * w for item, w in zip(best_items, weights)) / total_weight

    values = sorted(item.mm_per_unit for item in best_items)
    median = statistics.median(values)
    spread = (values[-1] - values[0]) / median if median > 0 else 0.0

    total_distinct = len({(m.text, round(m.value_mm, 4)) for m in measurements})
    agreement = best_support / max(total_distinct, 1)

    evidence = []
    for item in sorted(best_items, key=lambda m: -m.units)[:8]:
        evidence.append(
            f"'{item.text}' = {item.value_mm:g} mm across {item.units:.2f} units "
            f"-> {item.mm_per_unit:.5f} mm/unit"
        )

    source = (
        ScaleSource.DIMENSION_CONSENSUS
        if best_support >= settings.min_supporting_dimensions
        else ScaleSource.SINGLE_DIMENSION
    )
    return ScaleCandidate(
        mm_per_unit=mm_per_unit,
        source=source,
        support=best_support,
        agreement=agreement,
        detail=(
            f"{best_support} independent dimension(s) agree within "
            f"{spread * 100:.2f}% ({len(best_items)} matched spans)."
        ),
        evidence=evidence,
    )


def nearest_common_ratio(mm_per_unit: float) -> Tuple[float, float]:
    """Closest conventional drawing ratio and its relative error.

    Returns:
        ``(ratio_denominator, relative_error)``.
    """
    implied = mm_per_unit / MM_PER_PDF_UNIT_AT_1_1
    best = min(_COMMON_RATIOS, key=lambda r: abs(math.log(r / implied)) if implied > 0 else 1e9)
    return float(best), abs(implied - best) / best


def resolve_scale(
    primitives: Sequence[Primitive],
    dimension_texts: Sequence,
    text_height: float,
    printed_ratio: Optional[Dict] = None,
    region: Optional[BBox] = None,
    settings: CalibrationSettings = CALIBRATION,
) -> Tuple[Scale, List[ScaleCandidate], List[str]]:
    """Determine the best available scale, with every candidate kept.

    Args:
        primitives: All page primitives.
        dimension_texts: Parsed dimension annotations.
        text_height: Median annotation height.
        printed_ratio: Result of :func:`backend.pdf.text.detect_scale_ratio`.
        region: Restrict dimension matching to this box, when given.
        settings: Calibration parameters.

    Returns:
        ``(chosen_scale, all_candidates, warnings)``. The chosen scale is
        unverified (``mm_per_unit is None``) when nothing reliable was found —
        the caller must then refuse to report a physical area (§3).
    """
    warnings: List[str] = []
    candidates: List[ScaleCandidate] = []

    # Dimension matching is only attempted within the region when one is given,
    # then retried page-wide: many sheets place a governing dimension outside
    # the view it belongs to.
    measurements = collect_measurements(
        primitives, dimension_texts, text_height, settings, region=region
    )
    if len(measurements) < 2 and region is not None:
        measurements = collect_measurements(
            primitives, dimension_texts, text_height, settings, region=None
        )

    consensus = vote_for_scale(measurements, settings)
    if consensus is not None:
        candidates.append(consensus)

    ratio_candidate: Optional[ScaleCandidate] = None
    if printed_ratio:
        denominator = printed_ratio.get("denominator") or 0.0
        if denominator > 0:
            ratio_candidate = ScaleCandidate(
                mm_per_unit=MM_PER_PDF_UNIT_AT_1_1 * denominator,
                source=ScaleSource.DRAWING_RATIO,
                support=1,
                agreement=0.0,
                detail=f"Printed on the sheet as '{printed_ratio.get('text', '')}'.",
                evidence=[f"assumes the PDF page is at true sheet size"],
            )
            candidates.append(ratio_candidate)

    if consensus is not None:
        confidence = _consensus_confidence(consensus, settings)
        evidence = list(consensus.evidence)
        spread = _spread_from_detail(consensus)

        ratio, ratio_error = nearest_common_ratio(consensus.mm_per_unit)
        if ratio_error <= 0.02:
            evidence.append(f"implied ratio is within {ratio_error * 100:.1f}% of 1:{ratio:g}")
            confidence = min(0.99, confidence + 0.03)

        if ratio_candidate is not None:
            printed_error = abs(
                consensus.mm_per_unit - ratio_candidate.mm_per_unit
            ) / ratio_candidate.mm_per_unit
            if printed_error <= 0.03:
                evidence.append(
                    f"agrees with the printed scale '{printed_ratio.get('text', '')}' "
                    f"to {printed_error * 100:.1f}%"
                )
                confidence = min(0.99, confidence + 0.04)
            else:
                warnings.append(
                    f"Measured scale disagrees with the printed scale "
                    f"'{printed_ratio.get('text', '')}' by {printed_error * 100:.1f}%. "
                    f"The page was most likely rescaled on export or printed to fit. "
                    f"The measured dimensions were used."
                )

        return (
            Scale(
                mm_per_unit=consensus.mm_per_unit,
                source=consensus.source,
                confidence=round(confidence, 4),
                detail=consensus.detail,
                evidence=evidence,
                cross_check_spread=spread,
            ),
            candidates,
            warnings,
        )

    if ratio_candidate is not None:
        warnings.append(
            "No dimension annotation could be matched to linework, so the printed "
            "drawing scale was used. This is unverified: a page exported or printed "
            "'fit to page' makes it wrong. Calibrate against a known dimension to confirm."
        )
        return (
            Scale(
                mm_per_unit=ratio_candidate.mm_per_unit,
                source=ScaleSource.DRAWING_RATIO,
                confidence=0.50,
                detail=ratio_candidate.detail,
                evidence=ratio_candidate.evidence,
            ),
            candidates,
            warnings,
        )

    warnings.append(
        "No scale could be established from the drawing. Pick two points of known "
        "distance to calibrate."
    )
    return (
        Scale(mm_per_unit=None, source=ScaleSource.NONE, confidence=0.0,
              detail="No dimension consensus and no printed scale found."),
        candidates,
        warnings,
    )


def _consensus_confidence(candidate: ScaleCandidate, settings: CalibrationSettings) -> float:
    """Map dimension agreement onto a confidence in [0, 0.95]."""
    support_term = min(1.0, candidate.support / float(max(settings.min_supporting_dimensions, 1) * 2))
    agreement_term = min(1.0, candidate.agreement * 1.4)
    return 0.55 + 0.25 * support_term + 0.15 * agreement_term


def _spread_from_detail(candidate: ScaleCandidate) -> Optional[float]:
    """Recover the percentage spread the vote recorded in its detail string."""
    import re

    match = re.search(r"within\s+([\d.]+)%", candidate.detail)
    return float(match.group(1)) / 100.0 if match else None
