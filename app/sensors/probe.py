"""See whether the helmet sensors are actually attached.

This runs before any sensor thread starts. A missing bus is a normal laptop,
not an error: the chooser uses it to refuse a hardware ride.
"""

from __future__ import annotations

from pathlib import Path

from app.config import AppConfig

HARDWARE_REQUIRED = "This action requires hardware which is not connected."


def probe_hardware(config: AppConfig) -> dict[str, bool | str]:
    """True only when both the IMU and the GPS port answer."""
    imu_ok = _imu_present(config.sensors.imu.i2c_bus, config.sensors.imu.i2c_address)
    gps_ok = Path(config.sensors.gps.port).exists()
    available = imu_ok and gps_ok
    return {
        "available": available,
        "reason": "" if available else HARDWARE_REQUIRED,
    }


def _imu_present(bus_number: int, address: int) -> bool:
    device = Path(f"/dev/i2c-{bus_number}")
    if not device.exists():
        return False
    try:
        from smbus2 import SMBus
    except ImportError:
        return False
    try:
        bus = SMBus(bus_number)
    except OSError:
        return False
    try:
        # WHO_AM_I. A missing chip raises; we only care that something is there.
        bus.read_byte_data(address, 0x75)
    except OSError:
        return False
    finally:
        bus.close()
    return True
