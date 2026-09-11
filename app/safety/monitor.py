"""Composes crash detection, SOS escalation, and the status LED.

Keeping the wiring here means `main.py` starts one object, and the detector,
the state machine, and the indicator each stay testable on their own.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Any

from app.config import AppConfig
from app.models import CrashEvent, SystemState
from app.safety.crash import CrashDetector
from app.safety.sos import Notifier, SosController, SosState
from app.safety.status_led import LedBackend, StatusLed

if TYPE_CHECKING:
    from app.models import RideSample
    from app.pipeline.coordinator import Coordinator
    from app.storage.batch_writer import BatchWriter
    from app.streaming.websocket import TelemetryHub

logger = logging.getLogger(__name__)


class SafetyMonitor:
    """Watches the telemetry stream and escalates a confirmed crash."""

    def __init__(
        self,
        config: AppConfig,
        coordinator: Coordinator,
        *,
        hub: TelemetryHub | None = None,
        batch_writer: BatchWriter | None = None,
        notifier: Notifier | None = None,
        led_backend: LedBackend | None = None,
        retry_backoff_s: float = 2.0,
    ) -> None:
        self.config = config
        self.coordinator = coordinator
        self.hub = hub
        self.batch_writer = batch_writer

        self.detector = CrashDetector(config.safety, coordinator.buffer, coordinator.ride_id)
        self.sos = SosController(
            config.safety,
            config.node.helmet_id,
            notifier=notifier,
            retry_backoff_s=retry_backoff_s,
        )
        self.led = StatusLed(config.safety.led, led_backend)

        self.crashes: list[CrashEvent] = []
        # What the node said about itself before safety took the wheel. Restored
        # on cancel so a DEGRADED sensor does not get quietly forgotten because
        # a pothole briefly raised CRASH_SUSPECTED over the top of it.
        self._previous_override: SystemState | None = None
        self._override_held = False

        self.sos.subscribe(self._on_sos_state)

    # --- lifecycle --------------------------------------------------------

    async def start(self) -> None:
        if not self.config.safety.enabled:
            logger.info("Safety layer disabled by config")
            return
        self.coordinator.subscribe_sample(self.on_sample)
        self.led.start()
        self.led.update(self.coordinator.system_state)
        logger.info(
            "Safety armed: %.1f g trigger, %.0f km/h drop, %.1fs stillness, %.0fs countdown",
            self.config.safety.crash_g_threshold,
            self.config.safety.speed_drop_kmh,
            self.config.safety.stillness_window_s,
            self.config.safety.sos_countdown_s,
        )

    async def stop(self) -> None:
        await self.sos.close()
        await self.led.stop()

    # --- telemetry --------------------------------------------------------

    async def on_sample(self, sample: RideSample) -> None:
        event = self.detector.observe(sample)

        # Raise CRASH_SUSPECTED as soon as corroboration begins, so the LED and
        # the dashboard show something during the seconds before the verdict.
        if self.detector.suspected and not self._override_held:
            self._hold_override(SystemState.CRASH_SUSPECTED)
        elif not self.detector.suspected and not self.sos.active and self._override_held:
            self._release_override()

        self.led.update(self.coordinator.system_state)

        if event is not None:
            await self._escalate(event)

    async def _escalate(self, event: CrashEvent) -> None:
        self.crashes.append(event)
        self._hold_override(SystemState.SOS_ACTIVE)
        self.led.update(self.coordinator.system_state)

        # Persist and alert before the countdown expires, so the record exists
        # even if the node loses power the moment the dispatch goes out.
        if self.batch_writer is not None:
            try:
                await self.batch_writer.record_crash(event)
            except Exception:  # noqa: BLE001 - storage must not block an SOS
                logger.exception("Failed to record crash event")

        await self._broadcast("crash", event)
        self.sos.arm(event)

    # --- rider control ----------------------------------------------------

    def cancel_sos(self, reason: str = "rider cancelled") -> bool:
        """Called by the dashboard and, in Phase 4, by voice."""
        return self.sos.cancel(reason)

    # --- internals --------------------------------------------------------

    async def _on_sos_state(self, state: SosState, event: CrashEvent | None) -> None:
        if state is SosState.CANCELLED:
            self._release_override()
            self.detector.reset()
        self.led.update(self.coordinator.system_state)
        await self._broadcast("sos", event, state=state)

    def _hold_override(self, state: SystemState) -> None:
        if not self._override_held:
            self._previous_override = self.coordinator.processor.state_override
            self._override_held = True
        self.coordinator.processor.state_override = state

    def _release_override(self) -> None:
        if not self._override_held:
            return
        self.coordinator.processor.state_override = self._previous_override
        self._previous_override = None
        self._override_held = False

    async def _broadcast(
        self,
        kind: str,
        event: CrashEvent | None,
        state: SosState | None = None,
    ) -> None:
        if self.hub is None:
            return
        message: dict[str, Any] = {
            "type": kind,
            "sos": self.sos.stats(),
            "system_state": self.coordinator.system_state.value,
        }
        if event is not None:
            message["event"] = {
                "timestamp": event.timestamp,
                "peak_g": event.peak_g,
                "speed_before_kmh": event.speed_before_kmh,
                "speed_after_kmh": event.speed_after_kmh,
                "latitude": event.latitude,
                "longitude": event.longitude,
                "reason": event.reason,
            }
        if state is not None:
            message["sos_state"] = state.value
        # publish, not broadcast: an alert must never be dropped by the rate
        # throttle that keeps ordinary telemetry frames manageable.
        await self.hub.publish(message)

    def stats(self) -> dict[str, Any]:
        return {
            "enabled": self.config.safety.enabled,
            "phase": self.detector.phase.value,
            "triggers": self.detector.stats.triggers,
            "confirmed": self.detector.stats.confirmed,
            "rejected": self.detector.stats.rejected,
            "last_reason": self.detector.stats.last_reason,
            "sos": self.sos.stats(),
        }
