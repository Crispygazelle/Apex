"""u-blox NEO-M8N GPS driver: NMEA over UART.

Replaces the old root-level `read_gps.py`, which only handled RMC and therefore
threw away altitude, satellite count, and HDOP. Altitude is needed for the
climbing-gradient metric and HDOP is needed to weight the Kalman update, so
this parser merges RMC, GGA, and VTG into a single fix.

The sensor is self-paced: `read()` blocks on the serial port and returns None
for sentences that do not complete a fix.
"""

from __future__ import annotations

import contextlib
from typing import Any

from app import clock
from app.config import GpsConfig
from app.models import SensorReading, SensorSource
from app.sensors.base import Sensor, SensorError

KNOTS_TO_MPS = 0.514444
KMH_TO_MPS = 1.0 / 3.6


class NeoM8n(Sensor):
    """Accumulates NMEA sentences into position fixes.

    A fix is emitted when an RMC or GGA sentence arrives, carrying whatever
    altitude and accuracy data the most recent GGA/VTG provided.
    """

    source = SensorSource.GPS
    # Self-paced: the module pushes sentences at its own rate.
    rate_hz = 0.0

    def __init__(self, config: GpsConfig) -> None:
        self.config = config
        self._serial: Any = None
        self._nmea: Any = None

        # Sticky fields carried between sentences, since no single NMEA
        # sentence contains everything we need.
        self._altitude_m = 0.0
        self._satellites = 0
        self._hdop = 99.0
        self._speed_mps = 0.0
        self._course_deg = 0.0

    def open(self) -> None:
        try:
            import pynmea2
            import serial
        except ImportError as exc:  # pragma: no cover - hardware path
            raise SensorError(
                "pyserial/pynmea2 are not installed. Install the Pi extras: "
                "pip install -e '.[pi]'"
            ) from exc

        self._nmea = pynmea2
        try:
            self._serial = serial.Serial(
                self.config.port,
                baudrate=self.config.baudrate,
                timeout=self.config.read_timeout_s,
            )
        except Exception as exc:  # pragma: no cover - hardware path
            raise SensorError(
                f"Cannot open GPS at {self.config.port}. Is UART enabled and "
                f"the serial console disabled?"
            ) from exc

    def read(self) -> SensorReading | None:
        if self._serial is None:
            raise SensorError("read() before open()")

        try:
            raw = self._serial.readline()
        except Exception as exc:  # pragma: no cover - hardware path
            raise SensorError(f"GPS serial read failed: {exc}") from exc

        if not raw:
            return None  # Read timeout; no sentence this tick.

        line = raw.decode("ascii", errors="replace").strip()
        if not line.startswith("$"):
            return None

        try:
            msg = self._nmea.parse(line)
        except Exception:
            # Partial or corrupted sentence. Common on startup; just skip it.
            return None

        return self._consume(msg)

    def _consume(self, msg: Any) -> SensorReading | None:
        """Fold one sentence into the running fix, emitting when complete."""
        sentence = getattr(msg, "sentence_type", "")

        if sentence == "GGA":
            self._altitude_m = _as_float(getattr(msg, "altitude", None), self._altitude_m)
            self._satellites = int(_as_float(getattr(msg, "num_sats", None), self._satellites))
            self._hdop = _as_float(getattr(msg, "horizontal_dil", None), self._hdop)
            quality = int(_as_float(getattr(msg, "gps_qual", None), 0.0))
            if quality == 0:
                return self._pending_fix(valid=False)
            return self._fix_from_position(msg, valid=True)

        if sentence == "VTG":
            speed_kmh = _as_float(getattr(msg, "spd_over_grnd_kmph", None), None)
            if speed_kmh is not None:
                self._speed_mps = speed_kmh * KMH_TO_MPS
            course = _as_float(getattr(msg, "true_track", None), None)
            if course is not None:
                self._course_deg = course
            return None

        if sentence == "RMC":
            if getattr(msg, "status", "V") != "A":
                return self._pending_fix(valid=False)
            speed_knots = _as_float(getattr(msg, "spd_over_grnd", None), None)
            if speed_knots is not None:
                self._speed_mps = speed_knots * KNOTS_TO_MPS
            course = _as_float(getattr(msg, "true_course", None), None)
            if course is not None:
                self._course_deg = course
            return self._fix_from_position(msg, valid=True)

        if sentence == "GSA" or sentence == "GSV":
            sats = _as_float(getattr(msg, "num_sv_in_view", None), None)
            if sats is not None:
                self._satellites = int(sats)
            return None

        return None

    def _fix_from_position(self, msg: Any, *, valid: bool) -> SensorReading | None:
        latitude = _as_float(getattr(msg, "latitude", None), None)
        longitude = _as_float(getattr(msg, "longitude", None), None)
        if latitude is None or longitude is None or (latitude == 0.0 and longitude == 0.0):
            return self._pending_fix(valid=False)

        return SensorReading(
            timestamp=clock.now(),
            source=SensorSource.GPS,
            payload={
                "latitude": latitude,
                "longitude": longitude,
                "altitude_m": self._altitude_m,
                "speed_mps": self._speed_mps,
                "course_deg": self._course_deg,
                "satellites": self._satellites,
                "hdop": self._hdop,
                "valid": valid,
            },
            quality=_quality_from_hdop(self._hdop),
        )

    def _pending_fix(self, *, valid: bool) -> SensorReading:
        """Report 'still searching' so the pipeline can show acquiring state."""
        return SensorReading(
            timestamp=clock.now(),
            source=SensorSource.GPS,
            payload={
                "latitude": 0.0,
                "longitude": 0.0,
                "altitude_m": self._altitude_m,
                "speed_mps": 0.0,
                "course_deg": 0.0,
                "satellites": self._satellites,
                "hdop": self._hdop,
                "valid": valid,
            },
            quality=0.0,
        )

    def close(self) -> None:
        if self._serial is not None:
            with contextlib.suppress(Exception):  # pragma: no cover - hardware path
                self._serial.close()
            self._serial = None


def _as_float(value: Any, default: float | None) -> float | None:
    if value is None or value == "":
        return default
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def _quality_from_hdop(hdop: float) -> float:
    """Map HDOP onto 0..1. HDOP 1 is excellent, 10+ is unusable."""
    if hdop <= 0.0:
        return 0.0
    return max(0.0, min(1.0, 1.5 / hdop))
