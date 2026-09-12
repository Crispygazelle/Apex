"""Ride event log: threshold spikes and scripted (or live) voice exchanges.

The dashboard reads this over REST on load and over the WebSocket live. The
detector is deliberately conservative — a 10 Hz feed would otherwise paint the
log with the same brake for a second and a half.
"""

from __future__ import annotations

import asyncio
import logging
import time
from collections import deque
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

from app.models import RideSample, SystemState

if TYPE_CHECKING:
    from app.pipeline.coordinator import Coordinator
    from app.streaming.websocket import TelemetryHub
    from app.voice.assistant import VoiceAssistant

logger = logging.getLogger(__name__)

QUIET_STATES = frozenset(
    {SystemState.STARTING, SystemState.CALIBRATING, SystemState.ACQUIRING_GPS}
)


@dataclass(frozen=True, slots=True)
class RideLogEntry:
    timestamp: float
    kind: str
    text: str
    rider: str = ""
    apex: str = ""
    latitude: float = 0.0
    longitude: float = 0.0
    extra: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "type": "voice" if self.kind == "voice" else "event",
            "kind": self.kind,
            "timestamp": self.timestamp,
            "text": self.text,
            "clock": time.strftime("%H:%M:%S", time.localtime(self.timestamp)),
            "latitude": self.latitude,
            "longitude": self.longitude,
        }
        if self.kind == "voice":
            payload["rider"] = self.rider
            payload["apex"] = self.apex
        payload.update(self.extra)
        return payload


class EventLog:
    """Bounded, ride-long record of voice lines and threshold events."""

    def __init__(self, maxlen: int = 400) -> None:
        self._items: deque[RideLogEntry] = deque(maxlen=maxlen)

    def add(self, entry: RideLogEntry) -> None:
        self._items.append(entry)

    def as_dicts(self) -> list[dict[str, Any]]:
        return [entry.to_dict() for entry in self._items]


class ThresholdDetector:
    """Emits at most one event per sample, with a cooldown per kind."""

    FAST_KMH = 70.0
    BRAKE_MPS2 = -2.8
    HIGH_G = 2.15
    LEAN_DEG = 24.0
    SUDDEN_STOP_KMH = 12.0
    WAS_MOVING_KMH = 40.0

    _COOLDOWN_S = {
        "hard_brake": 8.0,
        "sudden_stop": 8.0,
        "high_g": 6.0,
        "tight_turn": 10.0,
        "fast_zone": 20.0,
    }

    def __init__(self) -> None:
        self._last_fired: dict[str, float] = {}
        self._in_fast = False
        self._peak_speed_kmh = 0.0

    def observe(self, sample: RideSample) -> RideLogEntry | None:
        if sample.system_state in QUIET_STATES:
            return None

        speed = sample.metrics.speed_kmh
        accel = sample.metrics.longitudinal_accel_mps2
        g_force = sample.metrics.g_force
        lean = sample.metrics.lean_angle_deg
        now = sample.timestamp
        self._peak_speed_kmh = max(self._peak_speed_kmh, speed)

        if accel <= self.BRAKE_MPS2 and speed > 8.0 and self._ready("hard_brake", now):
            self._in_fast = speed >= self.FAST_KMH
            if g_force >= 1.35:
                text = (
                    f"sudden braking and high g-forces experienced "
                    f"({speed:.0f} km/h, {g_force:.1f} g)"
                )
            else:
                text = f"hard braking ({speed:.0f} km/h, {accel:.1f} m/s²)"
            return self._entry(sample, "hard_brake", text)

        if (
            speed <= self.SUDDEN_STOP_KMH
            and self._peak_speed_kmh >= self.WAS_MOVING_KMH
            and accel <= -1.5
            and self._ready("sudden_stop", now)
        ):
            peak = self._peak_speed_kmh
            self._peak_speed_kmh = speed
            self._in_fast = False
            return self._entry(
                sample,
                "sudden_stop",
                f"sudden stop, down from {peak:.0f} km/h",
            )

        if (
            g_force >= self.HIGH_G
            and accel > self.BRAKE_MPS2
            and self._ready("high_g", now)
        ):
            return self._entry(
                sample,
                "high_g",
                f"impact spike {g_force:.1f} g — pothole-level hit",
            )

        if 24.0 <= abs(lean) <= 50.0 and self._ready("tight_turn", now):
            side = "left" if lean < 0 else "right"
            return self._entry(
                sample,
                "tight_turn",
                f"tight {side} turn, {abs(lean):.0f}° lean",
            )

        if speed >= self.FAST_KMH:
            if not self._in_fast and self._ready("fast_zone", now):
                self._in_fast = True
                return self._entry(
                    sample,
                    "fast_zone",
                    f"entered a fast zone at {speed:.0f} km/h",
                )
            self._in_fast = True
        else:
            self._in_fast = False

        return None

    def _ready(self, kind: str, now: float) -> bool:
        last = self._last_fired.get(kind)
        if last is not None and now - last < self._COOLDOWN_S[kind]:
            return False
        self._last_fired[kind] = now
        return True

    def _entry(self, sample: RideSample, kind: str, text: str) -> RideLogEntry:
        return RideLogEntry(
            timestamp=sample.timestamp,
            kind=kind,
            text=text,
            latitude=sample.state.latitude,
            longitude=sample.state.longitude,
            extra={
                "speed_kmh": round(sample.metrics.speed_kmh, 1),
                "g_force": round(sample.metrics.g_force, 2),
                "longitudinal_accel_mps2": round(
                    sample.metrics.longitudinal_accel_mps2, 2
                ),
            },
        )


class RideDirector:
    """Fans threshold events and scripted voice out to the dashboard log."""

    def __init__(
        self,
        coordinator: Coordinator,
        *,
        event_log: EventLog,
        hub: TelemetryHub | None = None,
        voice: VoiceAssistant | None = None,
    ) -> None:
        self.coordinator = coordinator
        self.event_log = event_log
        self.hub = hub
        self.voice = voice
        self.detector = ThresholdDetector()
        self._fired_voice: set[int] = set()
        self._voice_busy = False

    async def on_sample(self, sample: RideSample) -> None:
        event = self.detector.observe(sample)
        if event is not None:
            await self._emit(event)
        await self._maybe_voice(sample)

    async def on_voice(self, payload: dict[str, Any]) -> None:
        entry = RideLogEntry(
            timestamp=float(payload.get("timestamp") or 0.0),
            kind="voice",
            text=str(payload.get("apex") or ""),
            rider=str(payload.get("rider") or ""),
            apex=str(payload.get("apex") or ""),
            latitude=float(payload.get("latitude") or 0.0),
            longitude=float(payload.get("longitude") or 0.0),
            extra={
                "intent": payload.get("intent", ""),
                "hazard_type": payload.get("hazard_type", ""),
            },
        )
        await self._emit(entry)

    async def _maybe_voice(self, sample: RideSample) -> None:
        if self.voice is None or self._voice_busy:
            return
        sim = self.coordinator.simulator
        if sim is None:
            return
        distance = sim.distance_m
        for index, (at_m, text) in enumerate(sim.profile.voice_script):
            if index in self._fired_voice or distance < at_m:
                continue
            self._fired_voice.add(index)
            self._voice_busy = True
            asyncio.create_task(self._run_voice(text), name="apex-demo-voice")
            return

    async def _run_voice(self, text: str) -> None:
        try:
            await self.voice.hear(text)  # type: ignore[union-attr]
        except Exception:  # noqa: BLE001 - a demo line must not stop the ride
            logger.exception("Scripted voice command failed: %s", text)
        finally:
            self._voice_busy = False

    async def _emit(self, entry: RideLogEntry) -> None:
        self.event_log.add(entry)
        if self.hub is not None:
            await self.hub.publish(entry.to_dict())
