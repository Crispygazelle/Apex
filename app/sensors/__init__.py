"""Sensor drivers and the factory that picks real hardware or simulation."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field

from app.config import AppConfig
from app.models import SensorSource
from app.sensors.base import Sensor, SensorError
from app.sensors.recording import Recording, ReplayGps, ReplayImu
from app.sensors.simulated import (
    RideProfile,
    RideSimulator,
    SimulatedGps,
    SimulatedImu,
    SimulatedMic,
)

__all__ = [
    "Sensor",
    "SensorError",
    "SensorSet",
    "Recording",
    "ReplayGps",
    "ReplayImu",
    "RideProfile",
    "RideSimulator",
    "SimulatedGps",
    "SimulatedImu",
    "SimulatedMic",
    "build_sensors",
]


@dataclass
class SensorSet:
    """The sensors this node will run, plus whatever owns the ride's truth."""

    sensors: list[Sensor] = field(default_factory=list)
    simulator: RideSimulator | None = None
    recording: Recording | None = None

    def by_source(self, source: SensorSource) -> Sensor | None:
        return next((s for s in self.sensors if s.source is source), None)

    @property
    def route(self) -> RideSimulator | Recording | None:
        """Whichever object knows the route: a simulator or a recording."""
        return self.simulator or self.recording


def build_sensors(
    config: AppConfig,
    *,
    simulator: RideSimulator | None = None,
    recording: Recording | None = None,
    include_mic: bool | None = None,
    time_source: Callable[[], float] | None = None,
) -> SensorSet:
    """Instantiate the configured sensors.

    Hardware drivers are imported lazily so that `import app.sensors` stays
    safe on a machine without `smbus2`, `pyserial`, or `pyaudio`.

    `include_mic` overrides the config flag; the voice layer uses it to turn the
    microphone on without editing the file. `time_source` only affects the
    simulated backend, where it lets a ride be driven faster than real time.

    A `recording` wins over both backends: replaying captured readings is the
    point, so it must not be silently mixed with freshly generated ones.
    """
    want_mic = config.sensors.mic.enabled if include_mic is None else include_mic
    sensor_set = SensorSet()

    if recording is not None:
        sensor_set.recording = recording
        if config.sensors.imu.enabled:
            sensor_set.sensors.append(ReplayImu(recording, time_source=time_source))
        if config.sensors.gps.enabled:
            sensor_set.sensors.append(ReplayGps(recording, time_source=time_source))
        if want_mic:
            # Replay carries no audio, so the voice layer still needs a source
            # of silence to sit on while the demo director drives it.
            sensor_set.sensors.append(
                SimulatedMic(config.sensors.mic, RideSimulator(), time_source=time_source)
            )
        return sensor_set

    if config.use_hardware:
        if config.sensors.imu.enabled:
            from app.sensors.mpu6050 import Mpu6050

            sensor_set.sensors.append(Mpu6050(config.sensors.imu))
        if config.sensors.gps.enabled:
            from app.sensors.neo_m8n import NeoM8n

            sensor_set.sensors.append(NeoM8n(config.sensors.gps))
        if want_mic:
            from app.sensors.inmp441 import Inmp441

            sensor_set.sensors.append(Inmp441(config.sensors.mic))
        return sensor_set

    sim = simulator or RideSimulator()
    sensor_set.simulator = sim
    if config.sensors.imu.enabled:
        sensor_set.sensors.append(
            SimulatedImu(config.sensors.imu, sim, time_source=time_source)
        )
    if config.sensors.gps.enabled:
        sensor_set.sensors.append(
            SimulatedGps(config.sensors.gps, sim, time_source=time_source)
        )
    if want_mic:
        sensor_set.sensors.append(
            SimulatedMic(config.sensors.mic, sim, time_source=time_source)
        )
    return sensor_set
