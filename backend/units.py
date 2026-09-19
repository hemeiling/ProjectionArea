"""Unit-aware quantities. CONSTITUTION.md §13.

The canonical internal units are **millimetres** and **square millimetres**.
Naked floats must not cross module boundaries when a physical unit is implied.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Dict, Optional

#: Length unit -> millimetres.
LENGTH_TO_MM: Dict[str, float] = {
    "mm": 1.0,
    "cm": 10.0,
    "m": 1000.0,
    "in": 25.4,
    "ft": 304.8,
}

#: Area unit -> square millimetres.
AREA_TO_MM2: Dict[str, float] = {
    "mm2": 1.0,
    "cm2": 100.0,
    "m2": 1_000_000.0,
    "in2": 645.16,
    "ft2": 92_903.04,
}


def to_mm(value: float, unit: str) -> float:
    """Convert a length to millimetres."""
    try:
        return value * LENGTH_TO_MM[unit]
    except KeyError:
        raise ValueError(f"Unsupported length unit {unit!r}; expected one of {sorted(LENGTH_TO_MM)}")


def mm2_to(value_mm2: float, unit: str) -> float:
    """Convert a square-millimetre area into another area unit."""
    try:
        return value_mm2 / AREA_TO_MM2[unit]
    except KeyError:
        raise ValueError(f"Unsupported area unit {unit!r}; expected one of {sorted(AREA_TO_MM2)}")


@dataclass(frozen=True)
class Length:
    """A length with an explicit unit."""

    value: float
    unit: str = "mm"

    @property
    def mm(self) -> float:
        return to_mm(self.value, self.unit)

    def as_dict(self) -> Dict[str, object]:
        return {"value": self.value, "unit": self.unit}


@dataclass(frozen=True)
class Area:
    """An area carried internally in mm², rendered on demand in any unit.

    Full precision is preserved (§40); rounding happens only in
    :meth:`display`, which is what the UI shows.
    """

    mm2: float

    def to(self, unit: str) -> float:
        return mm2_to(self.mm2, unit)

    def display(self, unit: str, digits: Optional[int] = None) -> float:
        value = self.to(unit)
        if digits is None:
            magnitude = abs(value)
            digits = 0 if magnitude >= 10_000 else 1 if magnitude >= 1_000 else 2 if magnitude >= 1 else 4
        return round(value, digits)

    def as_dict(self) -> Dict[str, object]:
        return {
            "mm2": self.mm2,
            "cm2": self.to("cm2"),
            "m2": self.to("m2"),
            "in2": self.to("in2"),
            "ft2": self.to("ft2"),
        }
