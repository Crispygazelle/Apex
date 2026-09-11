"""Command handlers: turn an intent plus live telemetry into a spoken reply.

These are the only place the voice layer reads the coordinator, so a new
command is one method and one intent name, not a change to fusion.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

from app import clock
from app.models import HazardReport, RideSample, SystemState
from app.voice.intent import Intent, IntentName

if TYPE_CHECKING:
    from app.pipeline.coordinator import Coordinator
    from app.safety.monitor import SafetyMonitor
    from app.storage.batch_writer import BatchWriter

logger = logging.getLogger(__name__)

PREEMPT_STATES = frozenset(
    {SystemState.CRASH_SUSPECTED, SystemState.SOS_ACTIVE}
)


class CommandHandler:
    """Executes a parsed intent against the running node."""

    def __init__(
        self,
        coordinator: Coordinator,
        *,
        safety: SafetyMonitor | None = None,
        batch_writer: BatchWriter | None = None,
    ) -> None:
        self.coordinator = coordinator
        self.safety = safety
        self.batch_writer = batch_writer
        self.hazards: list[HazardReport] = []

    @property
    def preempted(self) -> bool:
        """True while a crash or SOS owns the rider's attention."""
        return self.coordinator.system_state in PREEMPT_STATES

    async def handle(self, intent: Intent) -> str:
        if self.preempted and intent.name not in {IntentName.CANCEL, IntentName.SOS}:
            return self._preempt_prompt()

        if intent.name is IntentName.CANCEL:
            return self._cancel()
        if intent.name is IntentName.SOS:
            return self._sos()
        if intent.name is IntentName.SPEED:
            return self._speed()
        if intent.name is IntentName.STATUS:
            return self._status()
        if intent.name is IntentName.LOCATION:
            return self._location()
        if intent.name is IntentName.HEADING:
            return self._heading()
        if intent.name is IntentName.DISTANCE:
            return self._distance()
        if intent.name is IntentName.HAZARD:
            return await self._hazard(intent)
        return "I didn't catch that."

    # --- handlers ---------------------------------------------------------

    def _sample(self) -> RideSample | None:
        return self.coordinator.latest

    def _speed(self) -> str:
        sample = self._sample()
        if sample is None:
            return "I don't have a speed reading yet."
        return f"{sample.metrics.speed_kmh:.0f} kilometres per hour."

    def _status(self) -> str:
        sample = self._sample()
        if sample is None:
            return "Telemetry is still starting."
        return (
            f"{sample.metrics.speed_kmh:.0f} kilometres per hour, "
            f"{sample.metrics.g_force:.1f} g, "
            f"heading {sample.state.heading_deg:.0f}, "
            f"{sample.metrics.distance_m / 1000.0:.1f} kilometres this ride."
        )

    def _location(self) -> str:
        sample = self._sample()
        if sample is None or not sample.gps_valid:
            return "No GPS fix."
        return (
            f"{sample.state.latitude:.5f}, {sample.state.longitude:.5f}, "
            f"{sample.satellites} satellites."
        )

    def _heading(self) -> str:
        sample = self._sample()
        if sample is None:
            return "I don't have a heading yet."
        return f"Heading {sample.state.heading_deg:.0f} degrees."

    def _distance(self) -> str:
        sample = self._sample()
        if sample is None:
            return "No distance yet."
        km = sample.metrics.distance_m / 1000.0
        if km < 1.0:
            return f"{sample.metrics.distance_m:.0f} metres this ride."
        return f"{km:.1f} kilometres this ride."

    async def _hazard(self, intent: Intent) -> str:
        sample = self._sample()
        if sample is None:
            return "I can't mark a hazard until telemetry starts."
        if not sample.gps_valid:
            return "No GPS fix; I won't log a hazard I cannot place."

        report = HazardReport(
            timestamp=clock.now(),
            ride_id=self.coordinator.ride_id,
            hazard_type=intent.hazard_type or "unspecified",
            latitude=sample.state.latitude,
            longitude=sample.state.longitude,
        )
        self.hazards.append(report)
        if self.batch_writer is not None:
            try:
                await self.batch_writer.record_hazard(report)
            except Exception:  # noqa: BLE001 - a failed write is still spoken
                logger.exception("Failed to persist hazard %s", report.hazard_type)
        logger.info(
            "Hazard %s at %.5f, %.5f",
            report.hazard_type,
            report.latitude,
            report.longitude,
        )
        return f"Logged {report.hazard_type} at current position."

    def _cancel(self) -> str:
        if self.safety is None:
            return "There is no SOS to cancel."
        if self.safety.cancel_sos("cancelled by voice"):
            return "SOS cancelled."
        if self.safety.sos.active:
            return "Too late to cancel; the alert is already going out."
        return "There is no SOS running."

    def _sos(self) -> str:
        if self.safety is None:
            return "Safety layer is off; I cannot send an SOS."
        if self.safety.sos.active:
            remaining = self.safety.sos.remaining_s
            if remaining > 0:
                return f"SOS already counting down, {remaining:.0f} seconds left."
            return "SOS is already being sent."
        return (
            "I will not send an SOS from a voice command alone. "
            "If you have crashed, stay still and the helmet will start the countdown."
        )

    def _preempt_prompt(self) -> str:
        if self.safety is not None and self.safety.sos.state.value == "countdown":
            remaining = self.safety.sos.remaining_s
            return (
                f"Crash detected. Sending SOS in {remaining:.0f} seconds. "
                "Say cancel if you are okay."
            )
        return "Crash detected. Say cancel if you are okay."
