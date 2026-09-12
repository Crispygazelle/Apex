"""Simulated sensors driven by one shared ground-truth ride.

The point of this module is that the fake IMU and the fake GPS are *consistent*
with each other. Independent random noise per sensor would make the Kalman
filter look like it works when it does not: there would be no true trajectory
for it to converge on. Here a single `RideSimulator` owns the truth, and each
sensor reports a noisy, biased view of it.

The synthetic ride deliberately includes:
  - a few stationary seconds at the start, so auto-calibration has a window
  - a GPS cold start, so the node passes through ACQUIRING_GPS
  - a mid-ride GPS dropout, so dead reckoning is exercised
  - a hard braking event and a rolling hill, so braking-g and gradient are real
  - constant sensor bias, so calibration has something to actually find
"""

from __future__ import annotations

import random
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from math import atan, copysign, cos, degrees, exp, pi, radians, sin

from app import clock
from app.config import GpsConfig, ImuConfig, MicConfig
from app.geo import GeoOrigin, angle_difference_deg
from app.models import SensorReading, SensorSource
from app.sensors.base import Sensor
from app.sensors.path import RidePath

GRAVITY = 9.80665
SAMPLE_WIDTH_BYTES = 2


@dataclass(frozen=True, slots=True)
class TrueState:
    """Ground truth at one instant. No noise, no bias."""

    t: float
    latitude: float
    longitude: float
    altitude_m: float
    speed_mps: float
    heading_deg: float
    yaw_rate_dps: float
    lean_deg: float
    roll_rate_dps: float
    pitch_deg: float
    pitch_rate_dps: float
    accel_forward_mps2: float
    gradient: float
    distance_m: float


@dataclass
class RideProfile:
    """Shape of the synthetic ride.

    With no waypoints this is the original analytic weave: a heading sine,
    a speed wave, one Gaussian brake. Tests rely on that. Fill `waypoints` and
    `speed_zones` to follow a real A-to-B polyline instead.
    """

    start_latitude: float = 21.14631  # Nagpur (Sitabuldi / city centre)
    start_longitude: float = 79.08491
    start_altitude_m: float = 310.0
    start_heading_deg: float = 45.0
    cruise_mps: float = 22.2  # ~80 km/h
    stationary_s: float = 4.0
    spool_up_s: float = 5.0
    speed_wave_period_s: float = 45.0
    heading_amplitude_deg: float = 40.0
    heading_period_s: float = 15.0
    hill_amplitude_m: float = 35.0
    hill_period_s: float = 120.0
    brake_at_s: float = 65.0
    brake_width_s: float = 2.0
    brake_depth: float = 0.75
    gps_lock_s: float = 6.0
    gps_dropouts: list[tuple[float, float]] = field(default_factory=lambda: [(45.0, 57.0)])
    # Off by default. Set it to rehearse the crash and SOS path end to end,
    # which is otherwise only testable by actually crashing a motorcycle.
    crash_at_s: float | None = None
    crash_peak_g: float = 8.0
    # Optional GPX-style polyline (lat, lon). Empty keeps the analytic weave.
    waypoints: list[tuple[float, float]] = field(default_factory=list)
    # (start_m, end_m, speed_mps) along that polyline.
    speed_zones: list[tuple[float, float, float]] = field(default_factory=list)
    potholes_m: list[float] = field(default_factory=list)
    pothole_peak_g: float = 2.4
    max_accel_mps2: float = 2.4
    max_brake_mps2: float = 7.5
    voice_script: list[tuple[float, str]] = field(default_factory=list)
    route_name: str = ""
    destination_name: str = ""


# A crash is three distinct phases, and the detector depends on all three:
# a violent transient, a period of tumbling, then stillness.
CRASH_IMPACT_S = 0.12
CRASH_DECEL_S = 0.45
CRASH_TUMBLE_S = 1.6
# Path-following corners from OSM can be vertex-sharp. Cap yaw so the IMU
# does not report a 90-degree snap in 5 ms, which winds the lean filter up
# into hundreds of degrees and can trip the crash detector.
MAX_PATH_YAW_DPS = 48.0
MAX_PATH_LEAN_DEG = 38.0
MAX_PATH_ROLL_DPS = 70.0


class RideSimulator:
    """Speed/heading/altitude profile with integrated position.

    Two modes share one object so IMU and GPS cannot drift apart:

    * analytic — the original heading sine, used by the accuracy tests
    * path-following — a polyline plus distance-based speed zones, used by the
      dashboard demo so the trail sits on real Nagpur roads
    """

    def __init__(self, profile: RideProfile | None = None) -> None:
        self.profile = profile or RideProfile()
        self._origin = GeoOrigin(
            self.profile.start_latitude,
            self.profile.start_longitude,
            self.profile.start_altitude_m,
        )
        self._path = RidePath(self.profile.waypoints) if self.profile.waypoints else None
        self._t = 0.0
        self._east = 0.0
        self._north = 0.0
        self._distance = 0.0
        self._speed = 0.0
        self._accel = 0.0
        self._heading_unwrapped = (
            self._path.heading_deg(0.0)
            if self._path is not None
            else self.profile.start_heading_deg
        )
        self._yaw_rate = 0.0
        self._lean_deg = 0.0
        self._roll_rate = 0.0

    @property
    def distance_m(self) -> float:
        return self._distance

    @property
    def destination(self) -> tuple[float, float] | None:
        if self._path is None:
            return None
        return self._path.waypoints[-1]

    @property
    def route_length_m(self) -> float:
        return 0.0 if self._path is None else self._path.length_m

    # --- analytic profile -------------------------------------------------

    def speed_at(self, t: float) -> float:
        if self._path is not None:
            self._advance_to(t)
            return self._speed

        since_crash = self.crash_elapsed(t)
        if since_crash is not None:
            # The bike stops in under half a second and stays stopped. This is
            # what gives the detector its speed-drop corroboration, and it is
            # also what a brake, however hard, cannot reproduce.
            if since_crash >= CRASH_DECEL_S:
                return 0.0
            impact_speed = self._cruise_speed_at(self.profile.crash_at_s or 0.0)
            return impact_speed * (1.0 - since_crash / CRASH_DECEL_S) ** 2

        return self._cruise_speed_at(t)

    def _cruise_speed_at(self, t: float) -> float:
        p = self.profile
        u = t - p.stationary_s
        if u <= 0.0:
            return 0.0

        spool = min(1.0, u / max(p.spool_up_s, 1e-3))
        wave = 0.70 + 0.30 * sin(2.0 * pi * u / p.speed_wave_period_s)
        # Smooth Gaussian dip rather than a step, so the derivative stays sane.
        z = (u - p.brake_at_s) / max(p.brake_width_s, 1e-3)
        brake = 1.0 - p.brake_depth * exp(-z * z)
        return max(0.0, p.cruise_mps * spool * wave * brake)

    def _zone_speed(self, distance_m: float) -> float:
        for start, end, mps in self.profile.speed_zones:
            if start <= distance_m < end:
                return mps
        return self.profile.cruise_mps

    def _target_speed(self, t: float, distance_m: float) -> float:
        since_crash = self.crash_elapsed(t)
        if since_crash is not None:
            if since_crash >= CRASH_DECEL_S:
                return 0.0
            impact_speed = self._zone_speed(distance_m)
            return impact_speed * (1.0 - since_crash / CRASH_DECEL_S) ** 2

        p = self.profile
        u = t - p.stationary_s
        if u <= 0.0:
            return 0.0
        zone = self._zone_speed(distance_m)
        spool = min(1.0, u / max(p.spool_up_s, 1e-3))
        return zone * spool

    def crash_elapsed(self, t: float) -> float | None:
        """Seconds since the scripted impact, or None if it has not happened."""
        crash_at = self.profile.crash_at_s
        if crash_at is None or t < crash_at:
            return None
        return t - crash_at

    def heading_unwrapped_at(self, t: float) -> float:
        if self._path is not None:
            self._advance_to(t)
            return self._heading_unwrapped

        p = self.profile
        if p.crash_at_s is not None and t >= p.crash_at_s:
            # A wreck does not keep steering.
            t = p.crash_at_s
        u = max(0.0, t - p.stationary_s)
        return p.start_heading_deg + p.heading_amplitude_deg * sin(
            2.0 * pi * u / p.heading_period_s
        )

    def altitude_at(self, t: float) -> float:
        p = self.profile
        u = max(0.0, t - p.stationary_s)
        return p.start_altitude_m + p.hill_amplitude_m * sin(2.0 * pi * u / p.hill_period_s)

    # --- integration and sampling ----------------------------------------

    def _advance_to(self, t: float) -> None:
        """Integrate ground-track position forward. Requests must not go back."""
        if self._path is not None:
            self._advance_along_path(t)
            return

        while self._t < t:
            step = min(0.005, t - self._t)
            mid = self._t + step / 2.0
            speed = self.speed_at(mid)
            heading = radians(self.heading_unwrapped_at(mid))
            self._east += speed * sin(heading) * step
            self._north += speed * cos(heading) * step
            self._distance += speed * step
            self._t += step

    def _advance_along_path(self, t: float) -> None:
        """Follow the polyline by arc-length. Speed is zone-based with accel caps."""
        path = self._path
        assert path is not None
        p = self.profile

        while self._t < t:
            step = min(0.005, t - self._t)
            mid = self._t + step / 2.0
            target = self._target_speed(mid, self._distance)
            delta = target - self._speed
            limit = p.max_accel_mps2 if delta >= 0.0 else p.max_brake_mps2
            max_step = limit * step
            new_speed = target if abs(delta) <= max_step else self._speed + (
                max_step if delta > 0.0 else -max_step
            )
            new_speed = max(0.0, new_speed)
            self._accel = (new_speed - self._speed) / step if step > 0.0 else 0.0
            self._speed = new_speed
            self._distance = min(path.length_m, self._distance + self._speed * step)

            latitude, longitude = path.point(self._distance)
            self._east, self._north = self._origin.to_enu(latitude, longitude)

            heading_raw = path.heading_deg(self._distance)
            wrapped = self._heading_unwrapped % 360.0
            turn = angle_difference_deg(heading_raw, wrapped)
            max_turn = MAX_PATH_YAW_DPS * step
            if abs(turn) > max_turn:
                turn = copysign(max_turn, turn)
            self._heading_unwrapped += turn
            self._yaw_rate = turn / step if step > 0.0 else 0.0

            target_lean = degrees(atan(self._speed * radians(self._yaw_rate) / GRAVITY))
            target_lean = max(-MAX_PATH_LEAN_DEG, min(MAX_PATH_LEAN_DEG, target_lean))
            max_dlean = MAX_PATH_ROLL_DPS * step
            dlean = target_lean - self._lean_deg
            if abs(dlean) > max_dlean:
                dlean = copysign(max_dlean, dlean)
            self._roll_rate = dlean / step if step > 0.0 else 0.0
            self._lean_deg += dlean
            self._t += step

    def _lean_at(self, t: float) -> float:
        """Lean angle implied by the speed and turn rate at time t."""
        if self._path is not None:
            self._advance_to(t)
            return self._lean_deg
        yaw_rate = _derivative(self.heading_unwrapped_at, t)
        return degrees(atan(self.speed_at(t) * radians(yaw_rate) / GRAVITY))

    def state_at(self, t: float) -> TrueState:
        self._advance_to(t)

        if self._path is not None:
            speed = self._speed
            accel_forward = self._accel
            yaw_rate = self._yaw_rate
            lean = self._lean_deg
            roll_rate = self._roll_rate
            heading_unwrapped = self._heading_unwrapped
        else:
            speed = self.speed_at(t)
            accel_forward = _derivative(self.speed_at, t)
            yaw_rate = _derivative(self.heading_unwrapped_at, t)
            lean = self._lean_at(t)
            roll_rate = _derivative(self._lean_at, t)
            heading_unwrapped = self.heading_unwrapped_at(t)

        # altitude_at is a closed function of time, so a central difference is
        # safe. speed_at is not: on a polyline it integrates forward and must
        # not be sampled around t.
        climb_rate = _derivative(self.altitude_at, t)
        gradient = climb_rate / speed if speed > 0.5 else 0.0
        pitch = degrees(atan(gradient))
        if self._path is not None:
            pitch_rate = 0.0
        else:
            pitch_rate = _derivative(
                lambda x: degrees(
                    atan(
                        _derivative(self.altitude_at, x) / self.speed_at(x)
                        if self.speed_at(x) > 0.5
                        else 0.0
                    )
                ),
                t,
            )

        latitude, longitude = self._origin.to_geodetic(self._east, self._north)
        return TrueState(
            t=t,
            latitude=latitude,
            longitude=longitude,
            altitude_m=self.altitude_at(t),
            speed_mps=speed,
            heading_deg=heading_unwrapped % 360.0,
            yaw_rate_dps=yaw_rate,
            lean_deg=lean,
            roll_rate_dps=roll_rate,
            pitch_deg=pitch,
            pitch_rate_dps=pitch_rate,
            accel_forward_mps2=accel_forward,
            gradient=gradient,
            distance_m=self._distance,
        )

    def gps_available_at(self, t: float) -> bool:
        if t < self.profile.gps_lock_s:
            return False
        return not any(start <= t <= end for start, end in self.profile.gps_dropouts)

    def pothole_force(self) -> tuple[float, float]:
        """Body-frame accel bump if the wheel is on a scripted pothole."""
        width_m = 1.4
        peak = self.profile.pothole_peak_g * GRAVITY
        for hole in self.profile.potholes_m:
            offset = self._distance - hole
            if abs(offset) <= width_m:
                shape = cos(pi * offset / (2.0 * width_m))
                return -0.40 * peak * shape, 0.90 * peak * shape
        return 0.0, 0.0


def _derivative(fn: Callable[[float], float], t: float, h: float = 1e-3) -> float:
    """Central difference. The profile is smooth, so this is accurate enough."""
    return (fn(t + h) - fn(t - h)) / (2.0 * h)


class _SimulatedSensor(Sensor):
    """Shared plumbing: a start time, a seeded RNG, and an injectable clock.

    `time_source` exists so tests can drive a whole ride in milliseconds
    instead of waiting for it in real time, and so a recorded ride can be
    replayed faster than it happened.
    """

    def __init__(
        self,
        simulator: RideSimulator,
        seed: int,
        time_source: Callable[[], float] | None = None,
    ) -> None:
        self.simulator = simulator
        self._rng = random.Random(seed)
        self._time = time_source or clock.now
        self._start = 0.0
        self._open = False

    def open(self) -> None:
        self._start = self._time()
        self._open = True

    def close(self) -> None:
        self._open = False

    @property
    def start_time(self) -> float:
        """Epoch time at which this sensor was opened."""
        return self._start

    @property
    def elapsed(self) -> float:
        return self._time() - self._start


class SimulatedImu(_SimulatedSensor):
    """Noisy, biased view of the true motion, in the helmet body frame.

    Body axes: x forward, y left, z up. At rest the accelerometer reads +1 g on
    z, matching a real MPU6050 lying flat.
    """

    source = SensorSource.IMU

    # Deliberate offsets for the calibrator to discover.
    ACCEL_BIAS = (0.18, -0.12, 0.25)
    GYRO_BIAS = (1.4, -0.8, 0.6)
    ACCEL_NOISE = 0.06
    GYRO_NOISE = 0.15

    def __init__(
        self,
        config: ImuConfig,
        simulator: RideSimulator,
        seed: int = 11,
        time_source: Callable[[], float] | None = None,
    ) -> None:
        super().__init__(simulator, seed, time_source)
        self.config = config
        self.rate_hz = config.rate_hz

    def read(self) -> SensorReading | None:
        t = self.elapsed
        st = self.simulator.state_at(t)

        lean_rad = radians(st.lean_deg)
        pitch_rad = radians(st.pitch_deg)

        # Specific force, not acceleration: at rest the sensor reports the
        # reaction to gravity along its own up axis.
        ax = st.accel_forward_mps2 - GRAVITY * sin(pitch_rad)
        ay = 0.0  # a coordinated turn leaves no lateral force in the bike frame
        az = GRAVITY * cos(pitch_rad) / max(cos(lean_rad), 0.3)

        # Road vibration grows with speed and is what the DLPF fights on real
        # hardware, so the simulator produces it too.
        vibration = 0.015 * st.speed_mps

        gx, gy, gz = st.roll_rate_dps, st.pitch_rate_dps, st.yaw_rate_dps
        impact_x, impact_z, agitation = self._crash_forces(t)
        if not agitation:
            hole_x, hole_z = self.simulator.pothole_force()
            impact_x += hole_x
            impact_z += hole_z
        ax += impact_x
        az += impact_z
        if agitation:
            gx += self._rng.gauss(0.0, 180.0)
            gy += self._rng.gauss(0.0, 180.0)
            gz += self._rng.gauss(0.0, 180.0)

        return SensorReading(
            timestamp=self._time(),
            source=SensorSource.IMU,
            payload={
                "ax_mps2": self._noisy(ax, self.ACCEL_BIAS[0], self.ACCEL_NOISE + vibration),
                "ay_mps2": self._noisy(ay, self.ACCEL_BIAS[1], self.ACCEL_NOISE + vibration),
                "az_mps2": self._noisy(az, self.ACCEL_BIAS[2], self.ACCEL_NOISE + vibration),
                "gx_dps": self._noisy(gx, self.GYRO_BIAS[0], self.GYRO_NOISE),
                "gy_dps": self._noisy(gy, self.GYRO_BIAS[1], self.GYRO_NOISE),
                "gz_dps": self._noisy(gz, self.GYRO_BIAS[2], self.GYRO_NOISE),
                "temperature_c": 31.5 + self._rng.gauss(0.0, 0.1),
            },
            quality=1.0,
        )

    def _crash_forces(self, t: float) -> tuple[float, float, bool]:
        """Impact transient and tumble, added on top of the normal motion.

        The deceleration implied by `speed_at` alone is only about 3 g, which
        is not what an impact feels like. A real collision is a short spike an
        order of magnitude above the average, so it is modelled separately.
        """
        since = self.simulator.crash_elapsed(t)
        if since is None:
            return 0.0, 0.0, False

        peak = self.simulator.profile.crash_peak_g * GRAVITY

        if since < CRASH_IMPACT_S:
            # Half-sine impulse: rises and falls inside about a tenth of a
            # second, which is roughly the duration of a real impact pulse.
            shape = sin(pi * since / CRASH_IMPACT_S)
            return -peak * shape, peak * 0.45 * shape, True

        if since < CRASH_TUMBLE_S:
            # Sliding and tumbling: violent but no longer a single direction.
            return self._rng.gauss(0.0, 0.7 * GRAVITY), self._rng.gauss(0.0, 0.7 * GRAVITY), True

        # Down and still. The normal formula already yields 1 g on z with the
        # bike stopped and level, which is exactly what the detector wants.
        return 0.0, 0.0, False

    def _noisy(self, value: float, bias: float, sigma: float) -> float:
        return value + bias + self._rng.gauss(0.0, sigma)


class SimulatedGps(_SimulatedSensor):
    """Noisy position fixes with a cold start and a mid-ride dropout."""

    source = SensorSource.GPS

    def __init__(
        self,
        config: GpsConfig,
        simulator: RideSimulator,
        seed: int = 23,
        time_source: Callable[[], float] | None = None,
    ) -> None:
        super().__init__(simulator, seed, time_source)
        self.config = config
        self.rate_hz = config.rate_hz

    def read(self) -> SensorReading | None:
        t = self.elapsed
        st = self.simulator.state_at(t)
        timestamp = self._time()

        if not self.simulator.gps_available_at(t):
            # Real modules keep emitting sentences while searching, so we do too.
            return SensorReading(
                timestamp=timestamp,
                source=SensorSource.GPS,
                payload={
                    "latitude": 0.0,
                    "longitude": 0.0,
                    "altitude_m": 0.0,
                    "speed_mps": 0.0,
                    "course_deg": 0.0,
                    "satellites": self._rng.randint(0, 3),
                    "hdop": 99.0,
                    "valid": False,
                },
                quality=0.0,
            )

        hdop = max(0.7, self._rng.gauss(1.1, 0.2))
        position_sigma_m = hdop * 2.6
        origin = GeoOrigin(st.latitude, st.longitude)
        east_err = self._rng.gauss(0.0, position_sigma_m)
        north_err = self._rng.gauss(0.0, position_sigma_m)
        latitude, longitude = origin.to_geodetic(east_err, north_err)

        return SensorReading(
            timestamp=timestamp,
            source=SensorSource.GPS,
            payload={
                "latitude": latitude,
                "longitude": longitude,
                "altitude_m": st.altitude_m + self._rng.gauss(0.0, 4.0),
                "speed_mps": max(0.0, st.speed_mps + self._rng.gauss(0.0, 0.3)),
                "course_deg": (st.heading_deg + self._rng.gauss(0.0, 2.0)) % 360.0,
                "satellites": self._rng.randint(8, 12),
                "hdop": hdop,
                "valid": True,
            },
            quality=max(0.0, min(1.0, 1.5 / hdop)),
        )


class SimulatedMic(_SimulatedSensor):
    """Low-level noise floor, so the voice pipeline can run without hardware.

    Blocks for the chunk duration to imitate a real capture, otherwise the
    acquisition thread would spin at full speed on a self-paced sensor.
    """

    source = SensorSource.MIC
    rate_hz = 0.0

    def __init__(
        self,
        config: MicConfig,
        simulator: RideSimulator,
        seed: int = 37,
        time_source: Callable[[], float] | None = None,
    ) -> None:
        super().__init__(simulator, seed, time_source)
        self.config = config
        self._injected: list[bytes] = []

    def inject(self, pcm: bytes) -> None:
        """Queue PCM to be returned by the next read, for tests and demos."""
        self._injected.append(pcm)

    @property
    def chunk_duration_s(self) -> float:
        return self.config.chunk / float(self.config.sample_rate)

    def _noise_floor(self) -> bytes:
        """Quiet 16-bit mono hiss, clamped to the signed range."""
        buffer = bytearray()
        for _ in range(self.config.chunk * self.config.channels):
            value = int(max(-32768.0, min(32767.0, self._rng.gauss(0.0, 180.0))))
            buffer += value.to_bytes(SAMPLE_WIDTH_BYTES, "little", signed=True)
        return bytes(buffer)

    def read(self) -> SensorReading | None:
        time.sleep(self.chunk_duration_s)

        pcm = self._injected.pop(0) if self._injected else self._noise_floor()

        return SensorReading(
            timestamp=self._time(),
            source=SensorSource.MIC,
            payload={
                "pcm": pcm,
                "frames": len(pcm) // (self.config.channels * SAMPLE_WIDTH_BYTES),
                "sample_rate": self.config.sample_rate,
            },
            quality=1.0,
        )
