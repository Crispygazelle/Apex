"""Speech-to-text, with the raw PCM dropped the moment a transcript exists.

Vosk is imported only when a model is present. Tests and a laptop without the
~50 MB model use `FakeTranscriber`, which never touches audio at all.

The privacy claim in the README is a real constraint, not a slogan: once
`transcribe` returns, the caller must not retain the buffer, and this module
does not keep one of its own.
"""

from __future__ import annotations

import json
import logging
import struct
from collections import deque
from pathlib import Path
from typing import Protocol

logger = logging.getLogger(__name__)

# 16-bit PCM below this RMS is treated as silence so we do not spend CPU
# transcribing wind. Tuned against INMP441 helmet recordings; a shout is ~2000.
SILENCE_RMS = 280.0


class Transcriber(Protocol):
    """Turns a 16-bit little-endian mono buffer into text, then forgets it."""

    def transcribe(self, pcm: bytes, sample_rate: int) -> str:
        """Return the transcript. Must not store `pcm`."""
        ...

    def reset(self) -> None:
        """Clear any streaming state between utterances."""
        ...


def pcm_rms(pcm: bytes) -> float:
    """Root-mean-square amplitude of 16-bit little-endian mono PCM."""
    if len(pcm) < 2:
        return 0.0
    count = len(pcm) // 2
    samples = struct.unpack_from(f"<{count}h", pcm)
    return (sum(sample * sample for sample in samples) / count) ** 0.5


def is_silence(pcm: bytes, threshold: float = SILENCE_RMS) -> bool:
    return pcm_rms(pcm) < threshold


class NullTranscriber:
    """Always silent. Used when the Vosk model is missing so the node still boots."""

    def transcribe(self, pcm: bytes, sample_rate: int) -> str:
        return ""

    def reset(self) -> None:
        return None


class FakeTranscriber:
    """Hands back scripted phrases. Used by tests and `--voice` demos on a Mac.

    `queue` is the only input: each `transcribe` call pops one phrase. PCM is
    inspected for silence and then discarded, never stored.
    """

    def __init__(self) -> None:
        self.queue: deque[str] = deque()
        self.calls = 0
        self.bytes_seen = 0

    def push(self, text: str) -> None:
        self.queue.append(text)

    def transcribe(self, pcm: bytes, sample_rate: int) -> str:
        self.calls += 1
        self.bytes_seen += len(pcm)
        if is_silence(pcm):
            return ""
        return self.queue.popleft() if self.queue else ""

    def reset(self) -> None:
        return None


class VoskTranscriber:
    """Offline recogniser. `vosk` is imported here, not at module import."""

    def __init__(self, model_path: str | Path, sample_rate: int = 16_000) -> None:
        from vosk import KaldiRecognizer, Model  # noqa: PLC0415 - optional extra

        path = Path(model_path)
        if not path.exists():
            raise FileNotFoundError(f"Vosk model not found at {path}")

        self.sample_rate = sample_rate
        self._model = Model(str(path))
        # A recogniser carries acoustic state across utterances. Reusing one
        # after "hey apex" made the next "apex" come out as "a backs". Fresh
        # per call is cheap next to loading the model.
        self._recognizer_cls = KaldiRecognizer

    def _new_recognizer(self, sample_rate: int) -> object:
        recognizer = self._recognizer_cls(self._model, sample_rate)
        recognizer.SetWords(False)
        return recognizer

    def transcribe(self, pcm: bytes, sample_rate: int) -> str:
        if not pcm:
            return ""
        rate = sample_rate if sample_rate > 0 else self.sample_rate
        recognizer = self._new_recognizer(rate)
        recognizer.AcceptWaveform(pcm)
        payload = json.loads(recognizer.FinalResult())
        return str(payload.get("text", "")).strip()

    def reset(self) -> None:
        return None


def build_transcriber(model_path: str, sample_rate: int = 16_000) -> Transcriber:
    """Real Vosk when the model is on disk, otherwise a silent fallback."""
    path = Path(model_path)
    if not path.exists():
        logger.warning(
            "Vosk model missing at %s; voice will hear nothing until "
            "scripts/fetch_models.sh is run",
            path,
        )
        return NullTranscriber()
    try:
        return VoskTranscriber(path, sample_rate=sample_rate)
    except Exception as exc:  # noqa: BLE001 - missing extra must not stop the ride
        logger.warning("Vosk unavailable (%s); voice input disabled", exc)
        return NullTranscriber()
