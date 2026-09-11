"""INMP441 I2S MEMS microphone driver.

Replaces the old root-level `mic.py`, which recorded a fixed 5-second WAV file
to disk. The voice pipeline needs a continuous stream it can run wake-word
detection over, and the privacy claim in the README requires that audio never
touch the filesystem, so this yields in-memory PCM chunks instead.

Self-paced: `read()` blocks until a full chunk is captured.
"""

from __future__ import annotations

import contextlib
from typing import Any

from app import clock
from app.config import MicConfig
from app.models import SensorReading, SensorSource
from app.sensors.base import Sensor, SensorError

SAMPLE_WIDTH_BYTES = 2  # 16-bit signed PCM, what Vosk expects


class Inmp441(Sensor):
    """Captures 16-bit mono PCM at the configured sample rate.

    Payload carries raw `bytes` rather than a decoded array; decoding is the
    voice layer's job and most chunks are discarded without ever being decoded.
    """

    source = SensorSource.MIC
    rate_hz = 0.0

    def __init__(self, config: MicConfig) -> None:
        self.config = config
        self._audio: Any = None
        self._stream: Any = None
        self._overflow_count = 0

    @property
    def chunk_duration_s(self) -> float:
        return self.config.chunk / float(self.config.sample_rate)

    def open(self) -> None:
        try:
            import pyaudio
        except ImportError as exc:  # pragma: no cover - hardware path
            raise SensorError(
                "pyaudio is not installed. Install the Pi extras: pip install -e '.[pi]'"
            ) from exc

        try:
            self._audio = pyaudio.PyAudio()
            self._stream = self._audio.open(
                format=pyaudio.paInt16,
                channels=self.config.channels,
                rate=self.config.sample_rate,
                input=True,
                input_device_index=self.config.device_index,
                frames_per_buffer=self.config.chunk,
            )
        except Exception as exc:  # pragma: no cover - hardware path
            self.close()
            raise SensorError(
                "Cannot open the I2S microphone. Is the i2s-mems overlay loaded "
                "and the device index correct? Check `arecord -l`."
            ) from exc

    def read(self) -> SensorReading | None:
        if self._stream is None:
            raise SensorError("read() before open()")

        try:
            # An overflow means we were too slow; dropping the gap is better
            # than raising, because audio is best-effort by nature.
            pcm = self._stream.read(self.config.chunk, exception_on_overflow=False)
        except Exception as exc:  # pragma: no cover - hardware path
            raise SensorError(f"Audio read failed: {exc}") from exc

        if not pcm:
            return None

        expected = self.config.chunk * self.config.channels * SAMPLE_WIDTH_BYTES
        if len(pcm) < expected:
            self._overflow_count += 1

        return SensorReading(
            timestamp=clock.now(),
            source=SensorSource.MIC,
            payload={
                "pcm": pcm,
                "frames": len(pcm) // (self.config.channels * SAMPLE_WIDTH_BYTES),
                "sample_rate": self.config.sample_rate,
            },
            quality=1.0,
        )

    def close(self) -> None:
        if self._stream is not None:
            try:
                self._stream.stop_stream()
                self._stream.close()
            except Exception:  # pragma: no cover - hardware path
                pass
            self._stream = None
        if self._audio is not None:
            with contextlib.suppress(Exception):  # pragma: no cover - hardware path
                self._audio.terminate()
            self._audio = None
