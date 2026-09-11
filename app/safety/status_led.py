"""Three-LED status indicator driven by the node's state.

This is the idea behind the old `traffic_light.py`, rebuilt as something the
rest of the system can actually use. That script cycled red-yellow-green on a
timer and reported nothing; it also never turned two of its LEDs off, because
`green_led.off` and `yellow_led.off` were written without call parentheses, so
they evaluated the bound method and discarded it.

The indicator matters more than it looks. A helmet has no screen the rider can
read at speed, so the LED is the only channel that answers "is it recording?"
before they set off, and "did it see the crash?" afterwards. Blink rate does
the work that colour alone cannot: yellow solid and yellow flashing mean very
different things, and a rider learns the difference in one ride.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
from dataclasses import dataclass
from typing import Protocol

from app import clock
from app.config import StatusLedConfig
from app.models import SystemState

logger = logging.getLogger(__name__)

# Fast enough that a 4 Hz blink looks like a blink rather than a stutter.
TICK_HZ = 20.0


@dataclass(frozen=True, slots=True)
class LedPattern:
    red: bool = False
    yellow: bool = False
    green: bool = False
    blink_hz: float = 0.0

    def lit_at(self, elapsed_s: float) -> tuple[bool, bool, bool]:
        """Which LEDs are on `elapsed_s` into the pattern."""
        if self.blink_hz <= 0.0:
            return self.red, self.yellow, self.green
        on = (elapsed_s * self.blink_hz * 2.0) % 2.0 < 1.0
        return self.red and on, self.yellow and on, self.green and on


PATTERNS: dict[SystemState, LedPattern] = {
    # Booting and calibrating: working, but do not ride yet. Calibration needs
    # the bike held still, and a slow pulse is the cue to wait.
    SystemState.STARTING: LedPattern(yellow=True, blink_hz=1.0),
    SystemState.CALIBRATING: LedPattern(yellow=True, blink_hz=2.0),
    # Sensors are good, position is not. Recording works; the map will not.
    SystemState.ACQUIRING_GPS: LedPattern(yellow=True),
    SystemState.RIDING: LedPattern(green=True),
    # A sensor died mid-ride. Green stays lit because the ride is still being
    # recorded; yellow joins it to say something is wrong.
    SystemState.DEGRADED: LedPattern(yellow=True, green=True, blink_hz=1.0),
    # Urgent and cancellable: fast red is the rider's cue that a countdown is
    # running and they have seconds to speak up.
    SystemState.CRASH_SUSPECTED: LedPattern(red=True, blink_hz=4.0),
    SystemState.SOS_ACTIVE: LedPattern(red=True),
    SystemState.STOPPING: LedPattern(),
}


class LedBackend(Protocol):
    def set(self, red: bool, yellow: bool, green: bool) -> None: ...
    def close(self) -> None: ...


class NullLedBackend:
    """No wiring present. Used on the Mac and whenever the LED is disabled."""

    def set(self, red: bool, yellow: bool, green: bool) -> None:
        return None

    def close(self) -> None:
        return None


class RecordingLedBackend:
    """Captures every transition, for tests and for `--backend sim` diagnosis."""

    def __init__(self) -> None:
        self.transitions: list[tuple[bool, bool, bool]] = []

    def set(self, red: bool, yellow: bool, green: bool) -> None:
        state = (red, yellow, green)
        if not self.transitions or self.transitions[-1] != state:
            self.transitions.append(state)

    def close(self) -> None:
        self.set(False, False, False)


class GpioLedBackend:
    """Real LEDs over gpiozero.

    `gpiozero` is imported here rather than at module scope so that importing
    `app.safety` on a development machine does not require the Pi GPIO stack.
    """

    def __init__(self, config: StatusLedConfig) -> None:
        from gpiozero import LED  # noqa: PLC0415 - Pi-only dependency

        self._red = LED(config.red_pin)
        self._yellow = LED(config.yellow_pin)
        self._green = LED(config.green_pin)

    def set(self, red: bool, yellow: bool, green: bool) -> None:
        for led, wanted in ((self._red, red), (self._yellow, yellow), (self._green, green)):
            # The parenthesis bug from traffic_light.py lived on this line.
            led.on() if wanted else led.off()

    def close(self) -> None:
        self.set(False, False, False)
        for led in (self._red, self._yellow, self._green):
            led.close()


def build_backend(config: StatusLedConfig) -> LedBackend:
    """Real LEDs when enabled and the GPIO stack is present, else a no-op."""
    if not config.enabled:
        return NullLedBackend()
    try:
        return GpioLedBackend(config)
    except Exception as exc:  # noqa: BLE001 - missing GPIO must not stop the ride
        logger.warning("Status LED unavailable (%s); continuing without it", exc)
        return NullLedBackend()


class StatusLed:
    """Drives the LED pattern for the current system state."""

    def __init__(self, config: StatusLedConfig, backend: LedBackend | None = None) -> None:
        self.config = config
        self.backend = backend if backend is not None else build_backend(config)
        self._pattern = PATTERNS[SystemState.STARTING]
        self._pattern_started = clock.monotonic()
        self._task: asyncio.Task[None] | None = None

    @property
    def pattern(self) -> LedPattern:
        return self._pattern

    def update(self, state: SystemState) -> None:
        """Switch patterns. Restarting the phase makes a change visible at once."""
        pattern = PATTERNS.get(state, PATTERNS[SystemState.STARTING])
        if pattern == self._pattern:
            return
        self._pattern = pattern
        self._pattern_started = clock.monotonic()
        self._apply()

    def start(self) -> None:
        if self._task is None:
            self._task = asyncio.create_task(self._blink_loop(), name="apex-led")
        self._apply()

    async def stop(self) -> None:
        if self._task is not None:
            self._task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._task
            self._task = None
        self.backend.close()

    def _apply(self) -> None:
        elapsed = clock.monotonic() - self._pattern_started
        red, yellow, green = self._pattern.lit_at(elapsed)
        try:
            self.backend.set(red, yellow, green)
        except Exception:  # noqa: BLE001 - a flaky LED is not worth a ride
            logger.exception("Status LED write failed")

    async def _blink_loop(self) -> None:
        period = 1.0 / TICK_HZ
        while True:
            await asyncio.sleep(period)
            if self._pattern.blink_hz > 0.0:
                self._apply()
