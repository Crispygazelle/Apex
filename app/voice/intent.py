"""Rule-based intent parser for a handful of helmet commands.

The old README promised a CRF/HMM tokenizer. That is the wrong tool here:
there are about ten phrases a rider will actually say with a visor down, and
a statistical model trained on clean speech fails on helmet audio long before
it fails on vocabulary. A small set of patterns we can read in a code review
is the honest version of "offline NLP".
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum

from app.voice.wake_word import normalize


class IntentName(StrEnum):
    SPEED = "speed"
    STATUS = "status"
    LOCATION = "location"
    HEADING = "heading"
    DISTANCE = "distance"
    HAZARD = "hazard"
    SOS = "sos"
    CANCEL = "cancel"
    DESTINATION = "destination"
    UNKNOWN = "unknown"


@dataclass(frozen=True, slots=True)
class Intent:
    name: IntentName
    hazard_type: str = ""
    raw: str = ""


# Order matters: more specific phrases first so "status report" does not
# fall through to a shorter token that happens to appear inside it.
_CANCEL = (
    "cancel",
    "i am okay",
    "i am ok",
    "im okay",
    "im ok",
    "i am fine",
    "im fine",
    "false alarm",
    "abort",
    "never mind",
    "nevermind",
)
_SOS = ("emergency", "sos", "send help", "call for help", "mayday")
_SPEED = ("what is my speed", "whats my speed", "how fast", "current speed", "speed")
_STATUS = ("status report", "status", "how am i doing", "vitals")
_LOCATION = ("where am i", "location", "coordinates", "gps")
_HEADING = ("heading", "which way", "what direction")
_DESTINATION = (
    "how far am i to the destination",
    "how far to the destination",
    "how far to destination",
    "how far am i from the destination",
    "remaining distance",
    "are we there yet",
    "eta",
    "time to destination",
)
_DISTANCE = ("how far", "distance", "odometer")

# "log hazard pothole" / "mark a pothole" / "hazard oil spill"
_HAZARD_PREFIXES = (
    "log hazard",
    "log a hazard",
    "mark hazard",
    "mark a hazard",
    "report hazard",
    "hazard",
)
_LOG_VERBS = ("log", "mark", "report")
_HAZARD_SKIP = frozenset(
    {
        "a",
        "an",
        "the",
        "here",
        "ahead",
        "please",
        "at",
        "my",
        "location",
        "this",
        "spot",
        "hazard",
    }
)
_KNOWN_HAZARDS = (
    "pothole",
    "oil",
    "oil spill",
    "gravel",
    "sand",
    "livestock",
    "animal",
    "flood",
    "water",
    "debris",
    "glass",
    "construction",
    "speed breaker",
    "speedbump",
    "speed bump",
)


class IntentParser:
    """Maps a normalised transcript onto one `Intent`."""

    def parse(self, text: str) -> Intent:
        raw = normalize(text)
        if not raw:
            return Intent(IntentName.UNKNOWN, raw=raw)

        if _starts_with(raw, _CANCEL):
            return Intent(IntentName.CANCEL, raw=raw)
        if _starts_with(raw, _SOS):
            return Intent(IntentName.SOS, raw=raw)

        hazard = _parse_hazard(raw)
        if hazard is not None:
            return hazard

        if _starts_with(raw, _STATUS):
            return Intent(IntentName.STATUS, raw=raw)
        if _starts_with(raw, _SPEED):
            return Intent(IntentName.SPEED, raw=raw)
        if _starts_with(raw, _LOCATION):
            return Intent(IntentName.LOCATION, raw=raw)
        if _starts_with(raw, _HEADING):
            return Intent(IntentName.HEADING, raw=raw)
        if _starts_with(raw, _DESTINATION):
            return Intent(IntentName.DESTINATION, raw=raw)
        if _starts_with(raw, _DISTANCE):
            return Intent(IntentName.DISTANCE, raw=raw)

        return Intent(IntentName.UNKNOWN, raw=raw)


def _starts_with(text: str, phrases: tuple[str, ...]) -> bool:
    return any(text == phrase or text.startswith(phrase + " ") for phrase in phrases)


def _parse_hazard(text: str) -> Intent | None:
    remainder = text
    matched_prefix = False
    for prefix in _HAZARD_PREFIXES:
        if text == prefix:
            return Intent(IntentName.HAZARD, hazard_type="unspecified", raw=text)
        lead = prefix + " "
        if text.startswith(lead):
            remainder = text[len(lead) :]
            matched_prefix = True
            break

    if not matched_prefix:
        tokens = text.split()
        if tokens and tokens[0] in _LOG_VERBS:
            remainder = " ".join(t for t in tokens[1:] if t not in _HAZARD_SKIP)
        elif any(text == name or text.startswith(name + " ") for name in _KNOWN_HAZARDS):
            remainder = text
        else:
            return None

    # Drop a leading "a"/"an"/"the" so "log hazard a pothole" is still a pothole.
    tokens = remainder.split()
    if tokens and tokens[0] in {"a", "an", "the"}:
        tokens = tokens[1:]
    remainder = " ".join(tokens)

    for name in sorted(_KNOWN_HAZARDS, key=len, reverse=True):
        if remainder == name or remainder.startswith(name + " "):
            return Intent(IntentName.HAZARD, hazard_type=name, raw=text)

    if matched_prefix:
        return Intent(IntentName.HAZARD, hazard_type=remainder or "unspecified", raw=text)
    if remainder:
        # "log a pothole here" already matched a known type above; a log-verb
        # with no recognised noun is not a hazard.
        return None
    return None
