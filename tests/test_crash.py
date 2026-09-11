"""Crash detection: does it catch a crash, and does it ignore everything else?

The false-positive cases matter more than the true positive. A detector that
fires on potholes gets turned off, and a detector that is turned off catches
nothing at all, so most of these tests are scenarios that must *not* trigger.
"""

from __future__ import annotations

from app.config import AppConfig, SafetyConfig
from app.models import FusionMode, MotionState, RideMetrics, RideSample, SystemState
from app.pipeline.buffer import TelemetryBuffer
from app.safety.crash import MAX_WATCH_S, CrashDetector, DetectorPhase

RATE_HZ = 50.0
DT = 1.0 / RATE_HZ


def make_sample(t: float, speed_kmh: float, g: float) -> RideSample:
    return RideSample(
        timestamp=t,
        ride_id="ride-test",
        state=MotionState(
            timestamp=t,
            speed_mps=speed_kmh / 3.6,
            latitude=12.9716,
            longitude=77.5946,
            mode=FusionMode.FUSED,
        ),
        metrics=RideMetrics(speed_kmh=speed_kmh, g_force=g),
        system_state=SystemState.RIDING,
        gps_valid=True,
        satellites=9,
    )


class Ride:
    """Feeds a scripted ride through the detector one sample at a time."""

    def __init__(self, config: SafetyConfig | None = None) -> None:
        self.buffer = TelemetryBuffer(seconds=60.0, expected_rate_hz=RATE_HZ)
        self.detector = CrashDetector(
            config or SafetyConfig(), self.buffer, ride_id="ride-test"
        )
        self.t = 0.0
        self.events = []

    def feed(self, seconds: float, speed_kmh: float, g: float = 1.0) -> None:
        for _ in range(int(seconds * RATE_HZ)):
            self.t += DT
            sample = make_sample(self.t, speed_kmh, g)
            self.buffer.append(sample)
            event = self.detector.observe(sample)
            if event is not None:
                self.events.append(event)


def test_a_real_crash_is_confirmed() -> None:
    """60 km/h, a hard impact, then the helmet lying still on the road."""
    ride = Ride()
    ride.feed(10.0, speed_kmh=60.0)
    ride.feed(0.1, speed_kmh=55.0, g=7.5)  # impact
    ride.feed(6.0, speed_kmh=0.0, g=1.0)  # rider and bike down

    assert len(ride.events) == 1
    event = ride.events[0]
    assert event.confirmed
    assert event.peak_g == 7.5
    assert event.speed_before_kmh == 60.0
    assert event.speed_after_kmh == 0.0
    assert "helmet at rest" in event.reason
    assert ride.detector.stats.confirmed == 1
    assert ride.detector.stats.rejected == 0


def test_a_pothole_at_speed_is_rejected() -> None:
    """The single check that matters: the rider carried on at the same speed."""
    ride = Ride()
    ride.feed(10.0, speed_kmh=60.0)
    ride.feed(0.1, speed_kmh=60.0, g=6.0)  # pothole
    ride.feed(6.0, speed_kmh=59.0)  # still riding

    assert ride.events == []
    assert ride.detector.stats.triggers == 1
    assert ride.detector.stats.rejected == 1
    assert "road shock" in ride.detector.stats.last_reason


def test_a_low_speed_crash_is_caught_by_the_proportional_rule() -> None:
    """18 km/h cannot satisfy a 20 km/h drop, but it is still a crash.

    Junction collisions happen at exactly these speeds, so a detector that
    only understands absolute drops would miss the most common city crash.
    """
    ride = Ride()
    assert ride.detector.config.speed_drop_kmh == 20.0

    ride.feed(10.0, speed_kmh=18.0)
    ride.feed(0.1, speed_kmh=14.0, g=7.0)
    ride.feed(6.0, speed_kmh=0.0)

    assert len(ride.events) == 1
    assert ride.events[0].confirmed
    assert "stopped dead" in ride.events[0].reason


def test_a_tip_over_at_walking_pace_is_not_a_crash() -> None:
    """Dropping the bike at the lights must not call an ambulance."""
    ride = Ride()
    ride.feed(10.0, speed_kmh=6.0)
    ride.feed(0.1, speed_kmh=4.0, g=6.0)
    ride.feed(6.0, speed_kmh=0.0)

    assert ride.events == []
    assert ride.detector.stats.rejected == 1


def test_a_partial_slowdown_after_a_jolt_is_not_a_crash() -> None:
    """Still rolling means still upright, however hard the jolt was."""
    ride = Ride()
    ride.feed(10.0, speed_kmh=30.0)
    ride.feed(0.1, speed_kmh=26.0, g=6.5)
    ride.feed(6.0, speed_kmh=14.0)  # slowed a lot, but did not stop

    assert ride.events == []
    assert ride.detector.stats.rejected == 1


def test_a_dropped_helmet_is_rejected() -> None:
    """Stationary before, stationary after: nothing was lost but dignity."""
    ride = Ride()
    ride.feed(10.0, speed_kmh=0.0)
    ride.feed(0.1, speed_kmh=0.0, g=12.0)  # dropped on concrete
    ride.feed(6.0, speed_kmh=0.0)

    assert ride.events == []
    assert ride.detector.stats.rejected == 1


def test_hard_braking_never_trips_the_trigger() -> None:
    """Emergency stops are violent but stay well under the g threshold."""
    ride = Ride()
    ride.feed(10.0, speed_kmh=80.0)
    for step in range(20):
        ride.feed(0.2, speed_kmh=80.0 - step * 4.0, g=1.4)
    ride.feed(5.0, speed_kmh=0.0)

    assert ride.events == []
    assert ride.detector.stats.triggers == 0


def test_a_speed_bump_taken_badly_is_rejected() -> None:
    ride = Ride()
    ride.feed(10.0, speed_kmh=40.0)
    ride.feed(0.06, speed_kmh=38.0, g=4.5)
    ride.feed(6.0, speed_kmh=36.0)

    assert ride.events == []
    assert ride.detector.stats.rejected == 1


def test_a_tumbling_rider_extends_the_window_then_confirms() -> None:
    """Continued violence is not evidence against a crash."""
    ride = Ride()
    ride.feed(10.0, speed_kmh=70.0)
    ride.feed(0.1, speed_kmh=60.0, g=8.0)
    # Eight seconds of tumbling: never quiet, so the stillness test keeps
    # failing and the window keeps extending rather than rejecting.
    ride.feed(8.0, speed_kmh=10.0, g=2.4)

    assert ride.events == [], "must not conclude anything while still tumbling"
    assert ride.detector.stats.rejected == 0
    assert ride.detector.suspected

    ride.feed(4.0, speed_kmh=0.0, g=2.4)  # still thrashing past MAX_WATCH_S
    assert len(ride.events) == 1
    assert ride.events[0].confirmed
    assert "prolonged violent motion" in ride.events[0].reason


def test_the_window_never_extends_past_the_cap() -> None:
    ride = Ride()
    ride.feed(5.0, speed_kmh=70.0)
    impact_t = ride.t
    ride.feed(0.1, speed_kmh=60.0, g=8.0)
    ride.feed(MAX_WATCH_S + 1.0, speed_kmh=0.0, g=3.0)

    assert len(ride.events) == 1
    assert ride.events[0].timestamp - impact_t < 0.2
    # Verdict arrived at the cap, not later.
    assert ride.detector.phase is DetectorPhase.COOLDOWN


def test_position_recorded_is_the_impact_site_not_the_resting_place() -> None:
    """A slide can carry the bike well away from where the crash happened."""
    ride = Ride()
    ride.feed(5.0, speed_kmh=60.0)

    # Impact at a known point.
    ride.t += DT
    impact = make_sample(ride.t, 55.0, 8.0)
    impact = RideSample(
        timestamp=impact.timestamp,
        ride_id=impact.ride_id,
        state=MotionState(
            timestamp=impact.timestamp, latitude=12.9000, longitude=77.5000, speed_mps=15.0
        ),
        metrics=impact.metrics,
    )
    ride.buffer.append(impact)
    ride.detector.observe(impact)

    # Comes to rest 200 m down the road.
    for _ in range(int(6.0 * RATE_HZ)):
        ride.t += DT
        rest = RideSample(
            timestamp=ride.t,
            ride_id="ride-test",
            state=MotionState(timestamp=ride.t, latitude=12.9018, longitude=77.5000),
            metrics=RideMetrics(speed_kmh=0.0, g_force=1.0),
        )
        ride.buffer.append(rest)
        event = ride.detector.observe(rest)
        if event is not None:
            ride.events.append(event)

    assert len(ride.events) == 1
    assert ride.events[0].latitude == 12.9000


def test_an_early_crash_uses_the_oldest_sample_it_has() -> None:
    """Half a second into a ride the lookback window does not exist yet."""
    ride = Ride()
    ride.feed(0.5, speed_kmh=45.0)  # lookback is 2.0 s; only 0.5 s exists
    ride.feed(0.1, speed_kmh=40.0, g=9.0)
    ride.feed(6.0, speed_kmh=0.0)

    assert len(ride.events) == 1
    assert ride.events[0].speed_before_kmh == 45.0


def test_cooldown_stops_one_wreck_reporting_twice() -> None:
    ride = Ride()
    ride.feed(10.0, speed_kmh=60.0)
    ride.feed(0.1, speed_kmh=55.0, g=8.0)
    ride.feed(6.0, speed_kmh=0.0)
    assert len(ride.events) == 1

    # Secondary impact as the bike settles: same wreck, not a new crash.
    ride.feed(0.1, speed_kmh=0.0, g=6.0)
    ride.feed(10.0, speed_kmh=0.0)
    assert len(ride.events) == 1
    assert ride.detector.phase is DetectorPhase.COOLDOWN


def test_a_rejection_re_arms_almost_immediately() -> None:
    """A pothole followed by a real crash is an ordinary sequence."""
    ride = Ride()
    ride.feed(10.0, speed_kmh=60.0)
    ride.feed(0.1, speed_kmh=60.0, g=6.0)  # pothole
    ride.feed(5.0, speed_kmh=60.0)
    assert ride.detector.stats.rejected == 1

    ride.feed(2.0, speed_kmh=60.0)
    ride.feed(0.1, speed_kmh=50.0, g=9.0)  # genuine crash moments later
    ride.feed(6.0, speed_kmh=0.0)

    assert len(ride.events) == 1
    assert ride.events[0].confirmed


def test_suspected_is_true_only_while_corroborating() -> None:
    ride = Ride()
    ride.feed(10.0, speed_kmh=60.0)
    assert not ride.detector.suspected

    ride.feed(0.1, speed_kmh=55.0, g=8.0)
    assert ride.detector.suspected, "must flag suspicion before the verdict"

    ride.feed(6.0, speed_kmh=0.0)
    assert not ride.detector.suspected


def test_a_disabled_detector_does_nothing(config: AppConfig) -> None:
    config.safety.enabled = False
    ride = Ride(config.safety)
    ride.feed(10.0, speed_kmh=60.0)
    ride.feed(0.1, speed_kmh=55.0, g=15.0)
    ride.feed(6.0, speed_kmh=0.0)

    assert ride.events == []
    assert ride.detector.stats.triggers == 0


def test_reset_re_arms_from_cooldown() -> None:
    ride = Ride()
    ride.feed(10.0, speed_kmh=60.0)
    ride.feed(0.1, speed_kmh=55.0, g=8.0)
    ride.feed(6.0, speed_kmh=0.0)
    assert ride.detector.phase is DetectorPhase.COOLDOWN

    ride.detector.reset()
    assert ride.detector.phase is DetectorPhase.IDLE

    ride.feed(3.0, speed_kmh=60.0)
    ride.feed(0.1, speed_kmh=55.0, g=8.0)
    ride.feed(6.0, speed_kmh=0.0)
    assert len(ride.events) == 2
