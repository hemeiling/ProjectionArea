"""Stable codes for the messages a user actually sees.

CONSTITUTION.md §31 (errors must be actionable) and the bilingual UI.

Most engine warnings are free text on purpose: they are the engineering record,
shared verbatim by the JSON API, the CLI report and the viewer, and rewording
them per client would mean the report and the screen stop saying the same thing.

A handful, though, are *interface* states rather than findings — "there is no
scale", "the scale came from a person" — and those are shown to every user on
every drawing. Those carry a stable code and parameters so the front end can say
them in the reader's language without parsing English prose.

The English text stays on the message. A client that does not know a code still
shows something correct, and the JSON record remains self-describing.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Dict, Optional


@dataclass(frozen=True)
class Message:
    """One user-facing message: a code, its parameters, and its English text."""

    code: str
    text: str
    params: Dict[str, Any] = field(default_factory=dict)

    def as_dict(self) -> Dict[str, Any]:
        return {"code": self.code, "text": self.text, "params": dict(self.params)}


#: Codes the front end localises. Keep in step with frontend/i18n/*.json.
SCALE_NOT_ESTABLISHED = "scale.notEstablished"
SCALE_NO_PHYSICAL_AREA = "scale.noPhysicalArea"
SCALE_OPERATOR_SUPPLIED = "scale.operatorSupplied"
SCALE_PRINTED_RATIO_ASSUMED = "scale.printedRatioAssumed"
GEOMETRY_NONE_RECONSTRUCTED = "geometry.noneReconstructed"
REGION_WHOLE_PAGE = "region.wholePage"


def message(code: str, text: str, **params: Any) -> Message:
    return Message(code=code, text=text, params=params)


def find_code(text: str) -> Optional[str]:
    """Best-effort code for a message that was produced as plain text.

    A bridge, not a design: it lets the highest-traffic warnings be localised
    without threading Message objects through every stage at once. Anything it
    does not recognise stays as the engine's own words.
    """
    lowered = text.lower()
    if "no scale could be established" in lowered:
        return SCALE_NOT_ESTABLISHED
    if "scale not verified" in lowered:
        return SCALE_NO_PHYSICAL_AREA
    if "no valid closed profile" in lowered or "no closed profile" in lowered:
        return GEOMETRY_NONE_RECONSTRUCTED
    if "whole page" in lowered and "selected" in lowered:
        return REGION_WHOLE_PAGE
    if "printed" in lowered and "ratio" in lowered and "assum" in lowered:
        return SCALE_PRINTED_RATIO_ASSUMED
    return None


def annotate(texts) -> list:
    """Pair each message with a code where one is known.

    Returns a list of ``{"text": ..., "code": ... | None}`` so the API can carry
    both: the engineering wording, and a key the UI can translate.
    """
    return [{"text": t, "code": find_code(t)} for t in texts]
