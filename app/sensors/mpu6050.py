"""MPU6050 6-axis IMU driver over I2C.

Replaces the old root-level `mpu.py`, which used the `mpu6050` convenience
package and issued two separate I2C transactions per sample. Here all 14 bytes
of accel, temperature, and gyro are read in a single burst so the six axes come
from the same instant rather than smearing across ~2 ms at 100 Hz.
"""

from __future__ import annotations

from typing import Any

from app import clock
from app.config import ImuConfig
from app.models import SensorReading, SensorSource
from app.sensors.base import Sensor, SensorError

# Register map (InvenSense MPU-6000/6050 Register Map rev 4.2).
REG_SMPLRT_DIV = 0x19
REG_CONFIG = 0x1A
REG_GYRO_CONFIG = 0x1B
REG_ACCEL_CONFIG = 0x1C
REG_ACCEL_XOUT_H = 0x3B
REG_PWR_MGMT_1 = 0x6B
REG_WHO_AM_I = 0x75

# LSB per g, indexed by full-scale range in g.
ACCEL_SENSITIVITY = {2: 16384.0, 4: 8192.0, 8: 4096.0, 16: 2048.0}
# LSB per deg/s, indexed by full-scale range in deg/s.
GYRO_SENSITIVITY = {250: 131.0, 500: 65.5, 1000: 32.8, 2000: 16.4}

STANDARD_GRAVITY = 9.80665
# Gyro output rate when the digital low-pass filter is enabled.
GYRO_OUTPUT_RATE_HZ = 1000.0


def _to_signed(high: int, low: int) -> int:
    value = (high << 8) | low
    return value - 65536 if value >= 32768 else value


class Mpu6050(Sensor):
    """Reads calibrated-scale accelerometer and gyroscope data at a fixed rate.

    Output payload is in SI units: m/s^2 and deg/s, body frame, with
    x forward, y left, z up.
    """

    source = SensorSource.IMU

    def __init__(self, config: ImuConfig) -> None:
        self.config = config
        self.rate_hz = config.rate_hz
        self._bus: Any = None
        self._accel_lsb = ACCEL_SENSITIVITY.get(config.accel_range_g)
        self._gyro_lsb = GYRO_SENSITIVITY.get(config.gyro_range_dps)

        if self._accel_lsb is None:
            raise SensorError(
                f"accel_range_g must be one of {sorted(ACCEL_SENSITIVITY)}, "
                f"got {config.accel_range_g}"
            )
        if self._gyro_lsb is None:
            raise SensorError(
                f"gyro_range_dps must be one of {sorted(GYRO_SENSITIVITY)}, "
                f"got {config.gyro_range_dps}"
            )

    def open(self) -> None:
        try:
            from smbus2 import SMBus
        except ImportError as exc:  # pragma: no cover - hardware path
            raise SensorError(
                "smbus2 is not installed. Install the Pi extras: pip install -e '.[pi]'"
            ) from exc

        try:
            self._bus = SMBus(self.config.i2c_bus)
            who_am_i = self._bus.read_byte_data(self.config.i2c_address, REG_WHO_AM_I)
        except OSError as exc:  # pragma: no cover - hardware path
            raise SensorError(
                f"No I2C device at {hex(self.config.i2c_address)} on bus "
                f"{self.config.i2c_bus}. Is I2C enabled and the IMU wired?"
            ) from exc

        # 0x68 is the MPU6050; 0x70/0x71/0x73 are MPU6500/9250 clones that share
        # this register map closely enough to keep going with a warning.
        if who_am_i not in (0x68, 0x70, 0x71, 0x73, 0x98):
            raise SensorError(f"Unexpected WHO_AM_I 0x{who_am_i:02X}; not an MPU6050")

        self._configure()

    def _configure(self) -> None:
        bus = self._bus
        addr = self.config.i2c_address

        # Wake from sleep and use the X-axis gyro PLL as the clock source,
        # which is more stable than the internal 8 MHz oscillator.
        bus.write_byte_data(addr, REG_PWR_MGMT_1, 0x01)

        # DLPF at 44 Hz accel / 42 Hz gyro: keeps road vibration out of the
        # signal while passing real rider dynamics, and sets the gyro output
        # rate to 1 kHz so SMPLRT_DIV divides from there.
        bus.write_byte_data(addr, REG_CONFIG, 0x03)

        divider = max(0, min(255, round(GYRO_OUTPUT_RATE_HZ / max(self.rate_hz, 1.0)) - 1))
        bus.write_byte_data(addr, REG_SMPLRT_DIV, divider)

        accel_sel = {2: 0, 4: 1, 8: 2, 16: 3}[self.config.accel_range_g]
        gyro_sel = {250: 0, 500: 1, 1000: 2, 2000: 3}[self.config.gyro_range_dps]
        bus.write_byte_data(addr, REG_ACCEL_CONFIG, accel_sel << 3)
        bus.write_byte_data(addr, REG_GYRO_CONFIG, gyro_sel << 3)

    def read(self) -> SensorReading | None:
        if self._bus is None:
            raise SensorError("read() before open()")

        try:
            # ACCEL_XOUT_H through GYRO_ZOUT_L: 6 accel + 2 temp + 6 gyro.
            raw = self._bus.read_i2c_block_data(self.config.i2c_address, REG_ACCEL_XOUT_H, 14)
        except OSError as exc:  # pragma: no cover - hardware path
            raise SensorError(f"I2C read failed: {exc}") from exc

        timestamp = clock.now()

        ax = _to_signed(raw[0], raw[1]) / self._accel_lsb * STANDARD_GRAVITY
        ay = _to_signed(raw[2], raw[3]) / self._accel_lsb * STANDARD_GRAVITY
        az = _to_signed(raw[4], raw[5]) / self._accel_lsb * STANDARD_GRAVITY

        # Datasheet: temp_degC = raw / 340 + 36.53
        temperature = _to_signed(raw[6], raw[7]) / 340.0 + 36.53

        gx = _to_signed(raw[8], raw[9]) / self._gyro_lsb
        gy = _to_signed(raw[10], raw[11]) / self._gyro_lsb
        gz = _to_signed(raw[12], raw[13]) / self._gyro_lsb

        return SensorReading(
            timestamp=timestamp,
            source=SensorSource.IMU,
            payload={
                "ax_mps2": ax,
                "ay_mps2": ay,
                "az_mps2": az,
                "gx_dps": gx,
                "gy_dps": gy,
                "gz_dps": gz,
                "temperature_c": temperature,
            },
            quality=1.0,
        )

    def close(self) -> None:
        if self._bus is not None:
            try:
                # Return to sleep so the IMU stops drawing current.
                self._bus.write_byte_data(self.config.i2c_address, REG_PWR_MGMT_1, 0x40)
                self._bus.close()
            except OSError:  # pragma: no cover - hardware path
                pass
            self._bus = None
