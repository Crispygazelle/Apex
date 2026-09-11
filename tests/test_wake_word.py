"""Wake-word matching: punctuation, burial in noise, and leftover command text."""

from __future__ import annotations

from app.voice.wake_word import WakeWordDetector, normalize


def detector() -> WakeWordDetector:
    return WakeWordDetector(["apex", "hey apex"])


def test_normalize_strips_punctuation_and_case() -> None:
    assert normalize("Hey, Apex!") == "hey apex"
    assert normalize("hey-apex") == "hey apex"


def test_longest_phrase_wins() -> None:
    match = detector().match("hey apex what's my speed")
    assert match.heard
    assert match.phrase == "hey apex"
    assert match.remainder == "whats my speed"


def test_bare_wake_word_has_no_remainder() -> None:
    match = detector().match("Apex")
    assert match.heard
    assert match.remainder == ""


def test_wake_word_buried_in_wind_noise_still_counts() -> None:
    match = detector().match("uh wind apex how fast")
    assert match.heard
    assert match.remainder == "how fast"


def test_unrelated_speech_is_ignored() -> None:
    assert not detector().heard("turn left at the next junction")
