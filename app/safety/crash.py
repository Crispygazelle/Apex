"""Crash detection with corroboration.

The version this replaces was a single line: if g-force exceeds 4, print
"CRASH". That cannot work. A motorcycle hitting a pothole at 60 km/h puts
5-8 g through the frame, a dropped helmet registers 10 g or more, and a
speed bump taken badly clears 4 g without trouble. A detector built on a
single threshold would page emergency services several times per commute,
and a detector that cries wolf gets switched off, which is worse than not
having one.

So the threshold is only the *trigger*. Confirmation needs corroboration from
two independent signals that a pothole cannot fake:

  1. **The bike lost speed.** A real impact sheds velocity. A pothole does
     not: the rider carries on at the same speed. This single check rejects
     the overwhelming majority of false positives, including the dropped
     helmet, which was doing 0 km/h before and 0 km/h after. It is measured
     two ways, absolute and proportional, for the reason set out at
     `MIN_SPEED_KMH` below.
  2. **The helmet went quiet.** After a crash, the accelerometer settles to
     gravity alone, because the rider and bike are lying on the road. Normal
     riding never produces three consecutive seconds of that.

The cost of corroboration is latency: the verdict arrives a few seconds after
the impact rather than instantly. That is the right trade. The SOS countdown
that follows is measured in tens of seconds, so a three-second confirmation
delay is noise in the overall response time, and it buys a detector the rider
will actually leave enabled.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from enum import StrEnum

from app.config import SafetyConfig
from app.models import CrashEvent, RideSample
from app.pipeline.buffer import TelemetryBuffer

logger = logging.getLogger(__name__)

# The impact transient is not stillness. Ignore the first half second after the
# trigger so the spike itself does not count against the quiet-window test.
IMPACT_SETTLE_S = 0.5

# A rider who is still tumbling has not yet gone quiet, but that is emphatically
# not evidence against a crash, so the quiet window extends rather than failing.
# This caps the extension: past it, prolonged violent motion is itself damning.
MAX_WATCH_S = 12.0

# After a confirmed crash the SOS layer owns the situation; re-triggering on the
# same wreckage would be noise. After a rejection, re-arm almost immediately:
# a pothole followed by a genuine crash is an ordinary sequence of events.
CONFIRMED_COOLDOWN_S = 60.0
REJECTED_COOLDOWN_S = 1.0

# An absolute speed-drop threshold on its own is blind at low speed. A rider
# doing 18 km/h who is hit at a junction can only ever lose 18 km/h, so a
# 20 km/h requirement rejects every such crash no matter how bad it is - and
# junction collisions are among the most common way riders get hurt in a city.
#
# So a proportional rule sits alongside the absolute one: a bike that was
# genuinely moving, has now essentially stopped, and lost nearly all of its
# speed doing so, has crashed. The three bounds together are what keep this
# from firing on ordinary events:
#   - below MIN_SPEED_KMH, stopping is a tip-over at the lights, not a crash
#   - above STOPPED_KMH, the rider is still travelling, so they are still up
#   - COLLAPSE_FRACTION rejects a gentle coast down to a halt
MIN_SPEED_KMH = 12.0
STOPPED_KMH = 5.0
COLLAPSE_FRACTION = 0.8


class DetectorPhase(StrEnum):
    """Where the detector is in the trigger-corroborate-verdict cycle."""

    IDLE = "idle"
    WATCHING = "watching"
    COOLDOWN = "cooldown"


@dataclass
class DetectorStats:
    triggers: int = 0
    confirmed: int = 0
    rejected: int = 0
    last_reason: str = ""


class CrashDetector:
    """Threshold trigger plus speed-drop and stillness corroboration."""

    def __init__(
        self,
        config: SafetyConfig,
        buffer: TelemetryBuffer,
        ride_id: str,
    ) -> None:
        self.config = config
        self.buffer = buffer
        self.ride_id = ride_id
        self.stats = DetectorStats()

        self._phase = DetectorPhase.IDLE
        self._cooldown_until = 0.0

        # Populated at trigger time and read at adjudication time.
        self._impact_ts = 0.0
        self._peak_g = 0.0
        self._speed_before_kmh = 0.0
        self._quiet_since = 0.0
        self._max_quiet_deviation = 0.0
        self._trigger_sample: RideSample | None = None

    # --- state ------------------------------------------------------------

    @property
    def phase(self) -> DetectorPhase:
        return self._phase

    @property
    def suspected(self) -> bool:
        """True while an impact is being corroborated."""
        return self._phase is DetectorPhase.WATCHING

    def reset(self) -> None:
        """Re-arm from any phase. Used when an SOS is cancelled or resolved."""
        self._phase = DetectorPhase.IDLE
        self._cooldown_until = 0.0
        self._trigger_sample = None

    # --- detection --------------------------------------------------------

    def observe(self, sample: RideSample) -> CrashEvent | None:
        """Feed one fused sample. Returns a confirmed `CrashEvent`, or None.

        Rejected impacts are counted in `stats` rather than returned: the
        caller only ever needs to act on a confirmation.
        """
        if not self.config.enabled:
            return None

        if self._phase is DetectorPhase.COOLDOWN:
            if sample.timestamp >= self._cooldown_until:
                self._phase = DetectorPhase.IDLE
            return None

        if self._phase is DetectorPhase.IDLE:
            self._maybe_trigger(sample)
            return None

        return self._corroborate(sample)

    def _maybe_trigger(self, sample: RideSample) -> None:
        if sample.metrics.g_force < self.config.crash_g_threshold:
            return

        # Pre-impact speed has to come from history, because by the time the
        # spike is visible the speed has already collapsed.
        before = self.buffer.sample_at(self.config.speed_lookback_s)
        if before is None:
            # Early in a ride the buffer is shorter than the lookback. Falling
            # back to the oldest sample keeps the detector armed during the
            # first seconds rather than blind, which is when a rider pulling
            # into traffic is arguably at most risk.
            before = self.buffer.oldest()

        self._phase = DetectorPhase.WATCHING
        self._impact_ts = sample.timestamp
        self._peak_g = sample.metrics.g_force
        self._speed_before_kmh = before.metrics.speed_kmh if before else 0.0
        self._quiet_since = sample.timestamp + IMPACT_SETTLE_S
        self._max_quiet_deviation = 0.0
        self._trigger_sample = sample

        self.stats.triggers += 1
        logger.warning(
            "Impact trigger: %.1f g at %.1f km/h, corroborating for %.1fs",
            self._peak_g,
            self._speed_before_kmh,
            self.config.stillness_window_s,
        )

    def _corroborate(self, sample: RideSample) -> CrashEvent | None:
        self._peak_g = max(self._peak_g, sample.metrics.g_force)

        elapsed = sample.timestamp - self._impact_ts
        if sample.timestamp >= self._quiet_since:
            # Deviation from 1 g, in either direction: a helmet at rest on the
            # road reads exactly gravity, while a tumbling one swings both ways.
            deviation = abs(sample.metrics.g_force - 1.0)
            self._max_quiet_deviation = max(self._max_quiet_deviation, deviation)

        quiet_elapsed = sample.timestamp - self._quiet_since
        if quiet_elapsed < self.config.stillness_window_s and elapsed < MAX_WATCH_S:
            return None

        still = self._max_quiet_deviation <= self.config.stillness_g_tolerance
        if not still and elapsed < MAX_WATCH_S:
            # Still moving violently. Restart the quiet window and keep waiting
            # rather than concluding there was no crash.
            self._quiet_since = sample.timestamp
            self._max_quiet_deviation = 0.0
            return None

        return self._adjudicate(sample, still=still)

    def _adjudicate(self, sample: RideSample, *, still: bool) -> CrashEvent | None:
        before = self._speed_before_kmh
        after = sample.metrics.speed_kmh
        drop = before - after

        lost_enough = drop >= self.config.speed_drop_kmh
        came_to_a_stop = (
            before >= MIN_SPEED_KMH
            and after <= STOPPED_KMH
            and drop >= before * COLLAPSE_FRACTION
        )

        if not (lost_enough or came_to_a_stop):
            reason = (
                f"{before:.1f} to {after:.1f} km/h is not an impact signature; "
                f"{self._peak_g:.1f} g reads as road shock"
            )
            return self._reject(sample, reason)

        evidence = "stopped dead" if came_to_a_stop and not lost_enough else f"{drop:.1f} km/h lost"
        reason = (
            f"{self._peak_g:.1f} g, {evidence}, "
            + ("helmet at rest" if still else "prolonged violent motion")
        )
        return self._confirm(sample, reason)

    def _confirm(self, sample: RideSample, reason: str) -> CrashEvent:
        self.stats.confirmed += 1
        self.stats.last_reason = reason
        self._phase = DetectorPhase.COOLDOWN
        self._cooldown_until = sample.timestamp + CONFIRMED_COOLDOWN_S

        origin = self._trigger_sample or sample
        logger.critical("Crash confirmed: %s", reason)
        return CrashEvent(
            timestamp=self._impact_ts,
            ride_id=self.ride_id,
            peak_g=round(self._peak_g, 2),
            speed_before_kmh=round(self._speed_before_kmh, 1),
            speed_after_kmh=round(sample.metrics.speed_kmh, 1),
            # The position at the moment of impact, not where the wreck came to
            # rest: a slide can carry the bike well away from what a witness or
            # a responder would recognise as the crash site.
            latitude=origin.state.latitude,
            longitude=origin.state.longitude,
            confirmed=True,
            reason=reason,
        )

    def _reject(self, sample: RideSample, reason: str) -> None:
        self.stats.rejected += 1
        self.stats.last_reason = reason
        self._phase = DetectorPhase.COOLDOWN
        self._cooldown_until = sample.timestamp + REJECTED_COOLDOWN_S
        logger.info("Impact not corroborated: %s", reason)
        return None
