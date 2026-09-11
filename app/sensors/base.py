"""Sensor interface shared by hardware drivers and their simulated twins.

Reads are blocking by design: I2C, UART, and I2S all block. `app.pipeline.
acquisition` runs each sensor on its own thread and marshals results onto the
event loop, so drivers stay simple and synchronous.

No module here may import a hardware library at import time. `smbus2`,
`serial`, and `pyaudio` are imported inside `open()` so that the whole package
remains importable on a laptop.
"""

from __future__ import annotations

import abc

from app.models import SensorReading, SensorSource


class SensorError(RuntimeError):
    """Raised when a sensor cannot be opened or has failed unrecoverably."""


class Sensor(abc.ABC):
    """A source of timestamped readings.

    Subclasses set `source` and `rate_hz`. A `rate_hz` of 0 or less means the
    sensor paces itself, i.e. `read()` blocks until the device yields data,
    which is how UART and I2S behave.
    """

    source: SensorSource
    rate_hz: float = 0.0

    @property
    def name(self) -> str:
        return self.source.value

    @property
    def self_paced(self) -> bool:
        return self.rate_hz <= 0.0

    @abc.abstractmethod
    def open(self) -> None:
        """Acquire the device. Raise `SensorError` if unavailable."""

    @abc.abstractmethod
    def read(self) -> SensorReading | None:
        """Return one reading, or None if no data was available this tick."""

    def close(self) -> None:  # noqa: B027 - a no-op default is correct here
        """Release the device. Must be safe to call twice.

        Deliberately concrete rather than abstract: a sensor with nothing to
        release should not be forced to write an empty override.
        """

    def __enter__(self) -> Sensor:
        self.open()
        return self

    def __exit__(self, *exc_info: object) -> None:
        self.close()
