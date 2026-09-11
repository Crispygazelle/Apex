"""Config loading, coercion, and environment overrides."""

from __future__ import annotations

from pathlib import Path

import pytest

from app.config import AppConfig, load_config


def test_defaults_are_laptop_safe() -> None:
    config = AppConfig()
    assert config.node.sensor_backend == "sim"
    assert not config.use_hardware
    assert not config.storage.enabled
    assert not config.voice.enabled


def test_shipped_config_file_loads() -> None:
    config = load_config(Path("config/apex.yaml"))
    assert config.node.helmet_id
    assert config.sensors.imu.rate_hz == 100.0
    # YAML 0x68 must arrive as the integer I2C address, not a string.
    assert config.sensors.imu.i2c_address == 0x68


def test_missing_file_falls_back_to_defaults(tmp_path: Path) -> None:
    config = load_config(tmp_path / "absent.yaml")
    assert config == AppConfig()


def test_yaml_overrides_defaults(tmp_path: Path) -> None:
    path = tmp_path / "apex.yaml"
    path.write_text(
        "node:\n"
        "  helmet_id: test-helmet\n"
        "  sensor_backend: hardware\n"
        "fusion:\n"
        "  process_noise: 1.5\n",
        encoding="utf-8",
    )

    config = load_config(path)
    assert config.node.helmet_id == "test-helmet"
    assert config.use_hardware
    assert config.fusion.process_noise == 1.5
    # Untouched sections keep their defaults.
    assert config.sensors.gps.baudrate == 9600


def test_env_overrides_yaml_and_coerces_types(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / "apex.yaml"
    path.write_text("node:\n  sensor_backend: sim\n", encoding="utf-8")

    monkeypatch.setenv("APEX__NODE__SENSOR_BACKEND", "hardware")
    monkeypatch.setenv("APEX__SENSORS__IMU__RATE_HZ", "200")
    monkeypatch.setenv("APEX__STORAGE__ENABLED", "true")
    monkeypatch.setenv("APEX__SAFETY__LED__RED_PIN", "5")

    config = load_config(path)
    assert config.use_hardware
    assert config.sensors.imu.rate_hz == 200.0
    assert isinstance(config.sensors.imu.rate_hz, float)
    assert config.storage.enabled is True
    assert config.safety.led.red_pin == 5


@pytest.mark.parametrize("value", ["1", "true", "TRUE", "yes", "on"])
def test_truthy_env_values(value: str, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("APEX__VOICE__ENABLED", value)
    assert load_config(Path("does-not-exist.yaml")).voice.enabled is True


@pytest.mark.parametrize("value", ["0", "false", "no", "off", ""])
def test_falsy_env_values(value: str, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("APEX__STREAMING__ENABLED", value)
    assert load_config(Path("does-not-exist.yaml")).streaming.enabled is False


def test_unknown_key_is_rejected_rather_than_ignored(tmp_path: Path) -> None:
    """A typo in the config must not silently do nothing."""
    path = tmp_path / "apex.yaml"
    path.write_text("node:\n  helmet_idd: oops\n", encoding="utf-8")

    with pytest.raises(ValueError, match="Unknown config key: node.helmet_idd"):
        load_config(path)


def test_list_values_survive_loading(tmp_path: Path) -> None:
    path = tmp_path / "apex.yaml"
    path.write_text(
        'voice:\n  wake_words: ["apex", "helmet"]\n'
        "calibration:\n  gyro_bias_dps: [1.0, 2.0, 3.0]\n",
        encoding="utf-8",
    )

    config = load_config(path)
    assert config.voice.wake_words == ["apex", "helmet"]
    assert config.calibration.gyro_bias_dps == [1.0, 2.0, 3.0]
