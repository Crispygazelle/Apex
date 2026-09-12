"""Intent parser: the phrases a visor-down rider will actually say."""

from __future__ import annotations

import pytest

from app.voice.intent import IntentName, IntentParser

PARSER = IntentParser()


@pytest.mark.parametrize(
    ("text", "name"),
    [
        ("what's my speed", IntentName.SPEED),
        ("how fast am I going", IntentName.SPEED),
        ("status report", IntentName.STATUS),
        ("where am I", IntentName.LOCATION),
        ("heading", IntentName.HEADING),
        ("how far have I gone", IntentName.DISTANCE),
        ("how far am I to the destination", IntentName.DESTINATION),
        ("how far to the destination", IntentName.DESTINATION),
        ("log a pothole here", IntentName.HAZARD),
        ("emergency", IntentName.SOS),
        ("I'm okay", IntentName.CANCEL),
        ("cancel", IntentName.CANCEL),
        ("false alarm", IntentName.CANCEL),
    ],
)
def test_common_phrases(text: str, name: IntentName) -> None:
    assert PARSER.parse(text).name is name


@pytest.mark.parametrize(
    ("text", "hazard"),
    [
        ("log hazard pothole", "pothole"),
        ("log hazard: pothole", "pothole"),
        ("mark a hazard oil spill", "oil spill"),
        ("pothole", "pothole"),
        ("log a pothole here", "pothole"),
        ("log a pothole", "pothole"),
        ("log hazard", "unspecified"),
        ("hazard gravel ahead", "gravel"),
    ],
)
def test_hazard_types(text: str, hazard: str) -> None:
    intent = PARSER.parse(text)
    assert intent.name is IntentName.HAZARD
    assert intent.hazard_type == hazard


def test_empty_and_unknown() -> None:
    assert PARSER.parse("").name is IntentName.UNKNOWN
    assert PARSER.parse("play some music").name is IntentName.UNKNOWN


def test_cancel_beats_a_hazard_that_happens_to_contain_the_word() -> None:
    """'cancel' must not be parsed as an unknown hazard type."""
    assert PARSER.parse("cancel sos").name is IntentName.CANCEL
