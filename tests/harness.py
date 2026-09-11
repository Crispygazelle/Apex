"""Offline ride driver used by the accuracy tests.

The point of this harness is that it compares the estimate against the
simulator's *ground truth*. A pipeline test that only checks "a sample came
out" would pass with the filter wired backwards. Here we can assert that the
position error stays inside a few metres, which is a claim worth making.

It drives the simulated sensors with a virtual clock, so a 90-second ride runs
in a fraction of a second and is fully deterministic.

Ground truth is captured at the moment a reading is generated, not when it is
later drained. `RideSimulator` integrates position forward only, so asking it
about a past timestamp would return the present position and quietly inflate
the reference by the width of the reorder window.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from app.config import AppConfig
from app.geo import haversine_m
from app.models import FusionMode, RideSample
from app.pipeline.processor import Processor
from app.processing.synchronization import FrameSynchronizer
from app.sensors.simulated import RideProfile, RideSimulator, SimulatedGps, SimulatedImu, TrueState


class VirtualClock:
    """Manually advanced clock shared by every simulated sensor."""

    def __init__(self, start: float = 1_800_000_000.0) -> None:
        self.t = start

    def __call__(self) -> float:
        return self.t

    def advance(self, dt: float) -> None:
        self.t += dt


@dataclass
class RideResult:
    samples: list[RideSample] = field(default_factory=list)
    truth: list[TrueState] = field(default_factory=list)
    processor: Processor | None = None
    simulator: RideSimulator | None = None

    @property
    def final(self) -> RideSample:
        return self.samples[-1]

    def _pairs(
        self, after_s: float, mode: FusionMode | None = None
    ) -> list[tuple[RideSample, TrueState]]:
        start = self.samples[0].timestamp
        return [
            (sample, truth)
            for sample, truth in zip(self.samples, self.truth, strict=True)
            if sample.timestamp - start >= after_s
            and (mode is None or sample.state.mode is mode)
        ]

    def position_errors_m(
        self, *, after_s: float = 0.0, mode: FusionMode | None = None
    ) -> list[float]:
        """Metres between the estimate and the truth, per emitted sample.

        Pass `mode` to separate periods where GPS was available from periods of
        pure dead reckoning; averaging the two together hides both.
        """
        return [
            haversine_m(
                sample.state.latitude,
                sample.state.longitude,
                truth.latitude,
                truth.longitude,
            )
            for sample, truth in self._pairs(after_s, mode)
            if sample.state.latitude != 0.0
        ]

    def speed_errors_mps(
        self, *, after_s: float = 0.0, mode: FusionMode | None = None
    ) -> list[float]:
        return [
            abs(sample.state.speed_mps - truth.speed_mps)
            for sample, truth in self._pairs(after_s, mode)
        ]

    def lean_errors_deg(self, *, after_s: float = 0.0) -> list[float]:
        return [
            abs(sample.metrics.lean_angle_deg - truth.lean_deg)
            for sample, truth in self._pairs(after_s)
        ]


def run_offline_ride(
    config: AppConfig,
    duration_s: float = 90.0,
    *,
    profile: RideProfile | None = None,
) -> RideResult:
    """Replay a synthetic ride through the real pipeline on a virtual clock."""
    clock = VirtualClock()
    simulator = RideSimulator(profile or RideProfile())

    imu = SimulatedImu(config.sensors.imu, simulator, time_source=clock)
    gps = SimulatedGps(config.sensors.gps, simulator, time_source=clock)
    imu.open()
    gps.open()

    processor = Processor(config, ride_id="test-ride")
    synchronizer = FrameSynchronizer(config.fusion.reorder_window_s)
    result = RideResult(processor=processor, simulator=simulator)

    imu_period = 1.0 / config.sensors.imu.rate_hz
    gps_period = 1.0 / config.sensors.gps.rate_hz
    next_gps = gps_period
    elapsed = 0.0

    # Truth captured at generation time. Keyed by source as well as time,
    # because the virtual clock gives an IMU sample and a GPS fix generated on
    # the same tick the same timestamp.
    truth_by_key: dict[tuple[float, object], TrueState] = {}

    def pump() -> None:
        for ordered in synchronizer.drain():
            sample = processor.process(ordered)
            reference = truth_by_key.pop((ordered.timestamp, ordered.source), None)
            if sample is not None and reference is not None:
                result.samples.append(sample)
                result.truth.append(reference)

    while elapsed < duration_s:
        reading = imu.read()
        if reading is not None:
            truth_by_key[(reading.timestamp, reading.source)] = simulator.state_at(elapsed)
            synchronizer.push(reading)

        if elapsed >= next_gps:
            fix = gps.read()
            if fix is not None:
                truth_by_key[(fix.timestamp, fix.source)] = simulator.state_at(elapsed)
                synchronizer.push(fix)
            next_gps += gps_period

        pump()
        clock.advance(imu_period)
        elapsed += imu_period

    for ordered in synchronizer.drain(flush=True):
        sample = processor.process(ordered)
        reference = truth_by_key.pop((ordered.timestamp, ordered.source), None)
        if sample is not None and reference is not None:
            result.samples.append(sample)
            result.truth.append(reference)

    imu.close()
    gps.close()
    return result
