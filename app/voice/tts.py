"""Text-to-speech. Piper is imported only when a model is on disk.

On a laptop without the voice model the node still speaks: it logs the line
and records it for tests. A missing speaker must never raise into the fusion
loop — a crash announcement that throws would be worse than silence.
"""

from __future__ import annotations

import asyncio
import logging
import wave
from pathlib import Path
from typing import Protocol

logger = logging.getLogger(__name__)


class Speaker(Protocol):
    async def speak(self, text: str) -> None:
        """Say `text` to the rider. Must not raise on a missing device."""
        ...


class RecordingSpeaker:
    """Captures every line. The default in tests, and the fallback without Piper."""

    def __init__(self) -> None:
        self.spoken: list[str] = []

    async def speak(self, text: str) -> None:
        cleaned = text.strip()
        if not cleaned:
            return
        self.spoken.append(cleaned)
        logger.info("TTS: %s", cleaned)


class NullSpeaker:
    async def speak(self, text: str) -> None:
        return None


class PiperSpeaker:
    """Offline Piper voice, synthesised on a worker thread.

    Playback goes through `aplay` when that exists (the Pi), otherwise the WAV
    is written to a temp file and discarded after synthesis so a Mac without
    speakers still exercises the model path.
    """

    def __init__(self, model_path: str | Path, output_device: str = "") -> None:
        from piper import PiperVoice  # noqa: PLC0415 - optional extra

        path = Path(model_path)
        if not path.exists():
            raise FileNotFoundError(f"Piper model not found at {path}")
        self.output_device = output_device
        self._voice = PiperVoice.load(str(path))

    async def speak(self, text: str) -> None:
        cleaned = text.strip()
        if not cleaned:
            return
        try:
            await asyncio.to_thread(self._synthesize, cleaned)
        except Exception:  # noqa: BLE001 - speech failure is not a ride failure
            logger.exception("Piper failed to speak %r", cleaned)

    def _synthesize(self, text: str) -> None:
        import shutil
        import subprocess
        import tempfile

        with tempfile.NamedTemporaryFile(suffix=".wav", delete=True) as handle:
            with wave.open(handle, "wb") as wav:
                self._voice.synthesize_wav(text, wav)
            handle.flush()
            aplay = shutil.which("aplay")
            if aplay is None:
                logger.info("TTS (no aplay): %s", text)
                return
            command = [aplay, "-q"]
            if self.output_device:
                command.extend(["-D", self.output_device])
            command.append(handle.name)
            subprocess.run(command, check=False, capture_output=True)  # noqa: S603


def build_speaker(model_path: str, output_device: str = "", *, speak: bool = True) -> Speaker:
    if not speak:
        return NullSpeaker()
    path = Path(model_path)
    if not path.exists():
        logger.info("Piper model missing at %s; spoken replies will be logged only", path)
        return RecordingSpeaker()
    try:
        return PiperSpeaker(path, output_device=output_device)
    except Exception as exc:  # noqa: BLE001
        logger.warning("Piper unavailable (%s); spoken replies will be logged only", exc)
        return RecordingSpeaker()
