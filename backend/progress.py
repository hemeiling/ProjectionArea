"""Reporting how far along a long job is, without the engine knowing what a job is.

CONSTITUTION.md §27 (the right amount of infrastructure) and §37 (seams).

A production DWG takes minutes: five seconds to convert and then four to parse
2.4 million entities. A progress bar over that has to be driven by what the
pipeline is *actually* doing, or it is decoration — and a decorative bar that
sits at 90 % for three minutes is worse than none.

So the geometry, CAD and area modules take an optional :class:`Progress` and
call it as they work. They never import jobs, HTTP or anything else: the whole
contract is two methods, and :data:`NULL_PROGRESS` makes the parameter free to
ignore. Nothing about a measurement changes because someone is watching.

Weights
-------
Overall percentage is interpolated across stages whose weights come from
*measured* cost on the real drawings, not from dividing 100 by the number of
steps. On a production DWG, reading the converted DXF is over half the wall
clock; giving it the same slice as "validate the signature" would make the bar
lie in both directions.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Callable, Dict, List, Optional, Sequence, Tuple


@dataclass
class Stage:
    """One reported step, and the share of the whole it accounts for.

    Attributes:
        key: Stable identifier; the UI translates it.
        weight: Relative cost, measured rather than assumed.
    """

    key: str
    weight: float


#: Measured on the production drawings. A DWG's cost is dominated by parsing the
#: converted DXF — 36 s of 42 s for 101, 240 s of 248 s for 102 — so that stage
#: carries the largest share and the bar keeps moving through it via entity
#: counts rather than jumping.
DWG_STAGES: Tuple[Stage, ...] = (
    Stage("validated", 3.0),
    Stage("converted", 15.0),
    Stage("geometry", 52.0),
    Stage("units", 4.0),
    Stage("layers", 6.0),
    Stage("candidates", 15.0),
    Stage("area", 5.0),
)

#: A DXF is read directly. The conversion stage is *absent*, not skipped with a
#: tick: claiming a step that never ran would be a small lie in a tool whose
#: whole point is not telling them.
DXF_STAGES: Tuple[Stage, ...] = (
    Stage("validated", 3.0),
    Stage("geometry", 58.0),
    Stage("units", 4.0),
    Stage("layers", 7.0),
    Stage("candidates", 20.0),
    Stage("area", 8.0),
)

#: Measured on 101 (163 k paths): read 1.9 s, normalise 3.4 s, classify 0.7 s,
#: regions 2.0 s, then silhouette and area dominate the remainder.
PDF_STAGES: Tuple[Stage, ...] = (
    Stage("loaded", 4.0),
    Stage("geometry", 38.0),
    Stage("regions", 14.0),
    Stage("scale", 8.0),
    Stage("candidates", 28.0),
    Stage("area", 8.0),
)

STAGE_PLANS: Dict[str, Tuple[Stage, ...]] = {
    "dwg": DWG_STAGES,
    "dxf": DXF_STAGES,
    "pdf": PDF_STAGES,
}


class Progress:
    """What a long-running pipeline reports to. Deliberately tiny.

    Two verbs: a stage begins, and work inside it advances. Everything else —
    percentages, elapsed time, labels, translation — belongs to whoever is
    displaying it.
    """

    def begin(self, key: str, detail: str = "") -> None:
        """Enter a stage. Anything earlier in the plan is implicitly finished."""

    def advance(self, current: int, total: Optional[int] = None, detail: str = "") -> None:
        """Report progress inside the current stage.

        ``total`` of ``None`` means the size is genuinely unknown; the display
        shows an indeterminate state rather than inventing a denominator.
        """

    def finish(self, key: str, detail: str = "") -> None:
        """Mark a stage complete, optionally replacing its detail line."""


class _NullProgress(Progress):
    """Used when nobody is watching, so callers need no conditionals."""

    def begin(self, key: str, detail: str = "") -> None:
        return

    def advance(self, current: int, total: Optional[int] = None, detail: str = "") -> None:
        return

    def finish(self, key: str, detail: str = "") -> None:
        return


#: The default for every ``progress`` parameter in the engine.
NULL_PROGRESS: Progress = _NullProgress()


@dataclass
class StageRecord:
    """A stage that has been entered, and what it reported."""

    key: str
    detail: str = ""
    done: bool = False
    current: Optional[int] = None
    total: Optional[int] = None


@dataclass
class ProgressTracker(Progress):
    """Turns stage events into an overall fraction, for a client to render.

    The fraction only ever moves forward: a stage's own sub-progress is
    interpolated inside its slice, so a re-entered or out-of-order stage cannot
    make the bar jump backwards. A bar that goes down is read as a fault.
    """

    stages: Sequence[Stage]
    on_change: Optional[Callable[["ProgressTracker"], None]] = None
    started_at: float = field(default_factory=time.time)

    current_key: str = ""
    current_detail: str = ""
    current_count: Optional[int] = None
    current_total: Optional[int] = None
    records: List[StageRecord] = field(default_factory=list)
    _fraction: float = 0.0

    # ── plan helpers ────────────────────────────────────────────────────────

    @property
    def total_weight(self) -> float:
        return sum(s.weight for s in self.stages) or 1.0

    def _index(self, key: str) -> int:
        for index, stage in enumerate(self.stages):
            if stage.key == key:
                return index
        return -1

    def _fraction_before(self, index: int) -> float:
        return sum(s.weight for s in self.stages[:index]) / self.total_weight

    def _slice(self, index: int) -> float:
        return self.stages[index].weight / self.total_weight

    # ── Progress interface ──────────────────────────────────────────────────

    def begin(self, key: str, detail: str = "") -> None:
        index = self._index(key)
        # Everything earlier in the plan is finished by definition.
        for record in self.records:
            if self._index(record.key) < index:
                record.done = True
        if not any(r.key == key for r in self.records):
            self.records.append(StageRecord(key=key, detail=detail))
        self.current_key = key
        self.current_detail = detail
        self.current_count = None
        self.current_total = None
        if index >= 0:
            self._set_fraction(self._fraction_before(index))
        self._notify()

    def advance(self, current: int, total: Optional[int] = None, detail: str = "") -> None:
        self.current_count = current
        self.current_total = total
        if detail:
            self.current_detail = detail
        record = self._record(self.current_key)
        if record is not None:
            record.current, record.total = current, total
            if detail:
                record.detail = detail
        index = self._index(self.current_key)
        if index >= 0 and total:
            share = max(0.0, min(1.0, current / total))
            self._set_fraction(self._fraction_before(index) + self._slice(index) * share)
        self._notify()

    def finish(self, key: str, detail: str = "") -> None:
        record = self._record(key)
        if record is None:
            record = StageRecord(key=key)
            self.records.append(record)
        record.done = True
        if detail:
            record.detail = detail
            if key == self.current_key:
                self.current_detail = detail
        index = self._index(key)
        if index >= 0:
            self._set_fraction(self._fraction_before(index) + self._slice(index))
        self._notify()

    # ── state ───────────────────────────────────────────────────────────────

    def complete(self) -> None:
        for record in self.records:
            record.done = True
        self._set_fraction(1.0)
        self._notify()

    def _record(self, key: str) -> Optional[StageRecord]:
        for record in self.records:
            if record.key == key:
                return record
        return None

    def _set_fraction(self, value: float) -> None:
        # Monotonic by construction: progress that goes backwards reads as a bug.
        self._fraction = max(self._fraction, max(0.0, min(1.0, value)))

    def _notify(self) -> None:
        if self.on_change:
            self.on_change(self)

    @property
    def fraction(self) -> float:
        return self._fraction

    @property
    def completed_stages(self) -> int:
        return sum(1 for r in self.records if r.done)

    @property
    def elapsed(self) -> float:
        return time.time() - self.started_at

    def stage_span(self) -> Tuple[float, float]:
        """Where the current stage sits in the whole, as (start, end) fractions.

        The weights live here, so the client does not need a second copy of them
        to draw the region a stage is working somewhere inside.
        """
        index = self._index(self.current_key)
        if index < 0:
            return (self._fraction, self._fraction)
        start = self._fraction_before(index)
        return (start, start + self._slice(index))

    def snapshot(self) -> Dict[str, object]:
        """The whole progress state, in the shape the client renders."""
        usable_total = self.current_total
        if usable_total and self.current_count and self.current_count > usable_total:
            usable_total = None
        span = self.stage_span()
        return {
            "stage": self.current_key,
            "stage_detail": self.current_detail,
            "completed_stages": self.completed_stages,
            "total_stages": len(self.stages),
            "progress": round(self._fraction, 4),
            "current": self.current_count,
            "total": usable_total,
            # No usable total means how far through this stage we are is
            # genuinely unknown, so the client animates the stage's own slice
            # instead of inventing a position inside it. That covers both the
            # stages that report no counts at all — polygonising a silhouette is
            # one operation, not n of m — and a count that has overrun its
            # estimate: the DXF reader's floor is modelspace and block contents
            # are walked on top of it, so "17 / 9" is not a ratio worth showing.
            "indeterminate": bool(self.current_key) and not usable_total,
            # The region the current stage covers, for that animation.
            "stage_start": round(span[0], 4),
            "stage_end": round(span[1], 4),
            "elapsed_seconds": round(self.elapsed, 1),
            "plan": [s.key for s in self.stages],
            "stages": [
                {"stage": r.key, "detail": r.detail, "done": r.done,
                 "current": r.current,
                 "total": r.total if not (r.total and r.current and r.current > r.total) else None}
                for r in self.records
            ],
        }


def tracker_for(kind: str, on_change: Optional[Callable[[ProgressTracker], None]] = None) -> ProgressTracker:
    """A tracker with the measured stage plan for a source kind."""
    return ProgressTracker(stages=STAGE_PLANS.get(kind, PDF_STAGES), on_change=on_change)
