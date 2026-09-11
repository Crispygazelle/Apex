"""Wake-word matching on already-transcribed text.

The acoustic work lives in the transcriber. This module only answers "did the
rider just address the helmet?" so the rest of the pipeline can stay quiet
until they have. Matching on text rather than a second acoustic model keeps
the Pi Zero from loading two speech networks into 512 MB.

A wake word in the middle of a sentence still counts: wind and road noise
often swallow the start of an utterance, and "…apex what's my speed" should
still open a session.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

# Apostrophes are deleted so "what's" and "I'm" stay one token. Everything
# else that is not a letter or digit becomes a space, so "hey-apex" and
# "Hey, Apex!" collapse to the same string.
_APOSTROPHE = re.compile(r"['’`]")
_NOISE = re.compile(r"[^a-z0-9]+")


def normalize(text: str) -> str:
    """Lowercase, drop apostrophes, strip remaining punctuation."""
    stripped = _APOSTROPHE.sub("", text.lower())
    return " ".join(_NOISE.sub(" ", stripped).split())


@dataclass(frozen=True, slots=True)
class WakeMatch:
    heard: bool
    phrase: str = ""
    remainder: str = ""


class WakeWordDetector:
    """Looks for configured phrases at the start of a transcript."""

    def __init__(self, phrases: list[str]) -> None:
        cleaned = [normalize(p) for p in phrases if normalize(p)]
        # Longest first so "hey apex" wins over "apex" at the same position.
        self.phrases = sorted(set(cleaned), key=len, reverse=True)

    def match(self, text: str) -> WakeMatch:
        normalised = normalize(text)
        if not normalised:
            return WakeMatch(heard=False)

        for phrase in self.phrases:
            if normalised == phrase:
                return WakeMatch(heard=True, phrase=phrase, remainder="")
            prefix = phrase + " "
            if normalised.startswith(prefix):
                return WakeMatch(
                    heard=True, phrase=phrase, remainder=normalised[len(prefix) :]
                )
            # Buried in noise: keep the words after the wake phrase.
            token = " " + phrase + " "
            padded = f" {normalised} "
            index = padded.find(token)
            if index != -1:
                after = padded[index + len(token) :].strip()
                return WakeMatch(heard=True, phrase=phrase, remainder=after)

        return WakeMatch(heard=False, remainder=normalised)

    def heard(self, text: str) -> bool:
        return self.match(text).heard
