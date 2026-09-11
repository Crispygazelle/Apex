"""Status LED: the only output channel a rider has before they set off."""

from __future__ import annotations

import asyncio

from app.config import StatusLedConfig
from app.models import SystemState
from app.safety.status_led import (
    PATTERNS,
    NullLedBackend,
    RecordingLedBackend,
    StatusLed,
    build_backend,
)


def test_every_system_state_has_a_pattern() -> None:
    """A state with no entry would silently fall back and mislead the rider."""
    assert set(PATTERNS) == set(SystemState)


def test_the_states_a_rider_must_distinguish_look_different() -> None:
    distinct = [
        SystemState.CALIBRATING,
        SystemState.ACQUIRING_GPS,
        SystemState.RIDING,
        SystemState.DEGRADED,
        SystemState.CRASH_SUSPECTED,
        SystemState.SOS_ACTIVE,
    ]
    patterns = [PATTERNS[state] for state in distinct]
    assert len(set(patterns)) == len(patterns)


def test_solid_patterns_ignore_time() -> None:
    riding = PATTERNS[SystemState.RIDING]
    assert riding.lit_at(0.0) == riding.lit_at(3.7) == (False, False, True)


def test_blinking_alternates_at_the_stated_rate() -> None:
    crash = PATTERNS[SystemState.CRASH_SUSPECTED]
    assert crash.blink_hz == 4.0

    # One full cycle is 0.25 s: lit for the first half, dark for the second.
    assert crash.lit_at(0.0) == (True, False, False)
    assert crash.lit_at(0.1) == (True, False, False)
    assert crash.lit_at(0.2) == (False, False, False)
    assert crash.lit_at(0.25) == (True, False, False)


def test_stopping_turns_everything_off() -> None:
    assert PATTERNS[SystemState.STOPPING].lit_at(0.0) == (False, False, False)


def test_degraded_keeps_green_lit_because_recording_continues() -> None:
    degraded = PATTERNS[SystemState.DEGRADED]
    assert degraded.green and degraded.yellow


async def test_update_writes_through_to_the_backend() -> None:
    backend = RecordingLedBackend()
    led = StatusLed(StatusLedConfig(enabled=True), backend)

    led.update(SystemState.RIDING)
    assert backend.transitions[-1] == (False, False, True)

    led.update(SystemState.SOS_ACTIVE)
    assert backend.transitions[-1] == (True, False, False)


async def test_a_blinking_pattern_actually_toggles() -> None:
    backend = RecordingLedBackend()
    led = StatusLed(StatusLedConfig(enabled=True), backend)
    led.start()
    led.update(SystemState.CRASH_SUSPECTED)

    await asyncio.sleep(0.4)  # at 4 Hz that is three transitions or so
    await led.stop()

    lit = [t for t in backend.transitions if t == (True, False, False)]
    dark = [t for t in backend.transitions if t == (False, False, False)]
    assert lit and dark, "a blinking LED must be seen both on and off"


async def test_stop_leaves_the_leds_off() -> None:
    backend = RecordingLedBackend()
    led = StatusLed(StatusLedConfig(enabled=True), backend)
    led.start()
    led.update(SystemState.RIDING)
    await led.stop()

    assert backend.transitions[-1] == (False, False, False)


async def test_a_failing_backend_does_not_stop_the_ride() -> None:
    class Broken:
        def set(self, red: bool, yellow: bool, green: bool) -> None:
            raise OSError("GPIO gone")

        def close(self) -> None:
            return None

    led = StatusLed(StatusLedConfig(enabled=True), Broken())
    led.start()
    led.update(SystemState.RIDING)  # must not raise
    await led.stop()


def test_a_disabled_led_uses_the_null_backend() -> None:
    assert isinstance(build_backend(StatusLedConfig(enabled=False)), NullLedBackend)


def test_missing_gpio_falls_back_instead_of_raising() -> None:
    """gpiozero is absent on the Mac; the node must still start."""
    assert isinstance(build_backend(StatusLedConfig(enabled=True)), NullLedBackend)
