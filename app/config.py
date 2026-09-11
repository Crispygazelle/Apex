"""Typed configuration, loaded from YAML with environment overrides.

Every tunable lives in `config/apex.yaml`. Secrets and per-device values can be
overridden without editing the file using double-underscore env paths, e.g.
`APEX__STORAGE__TOKEN=abc` or `APEX__NODE__SENSOR_BACKEND=hardware`.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field, is_dataclass
from pathlib import Path
from typing import Any, get_origin, get_type_hints

import yaml

ENV_PREFIX = "APEX__"
DEFAULT_CONFIG_PATH = Path("config/apex.yaml")


@dataclass
class NodeConfig:
    helmet_id: str = "helmet01"
    # "sim" runs the whole stack with no hardware. "hardware" talks to the Pi.
    sensor_backend: str = "sim"
    log_level: str = "INFO"
    data_dir: str = "data"


@dataclass
class ImuConfig:
    enabled: bool = True
    rate_hz: float = 100.0
    i2c_bus: int = 1
    i2c_address: int = 0x68
    accel_range_g: int = 8
    gyro_range_dps: int = 500


@dataclass
class GpsConfig:
    enabled: bool = True
    rate_hz: float = 10.0
    port: str = "/dev/ttyAMA0"
    baudrate: int = 9600
    read_timeout_s: float = 1.0


@dataclass
class MicConfig:
    enabled: bool = False
    sample_rate: int = 16000
    channels: int = 1
    chunk: int = 1024
    device_index: int | None = None


@dataclass
class SensorsConfig:
    imu: ImuConfig = field(default_factory=ImuConfig)
    gps: GpsConfig = field(default_factory=GpsConfig)
    mic: MicConfig = field(default_factory=MicConfig)


@dataclass
class CalibrationConfig:
    auto_calibrate_on_start: bool = True
    stationary_samples: int = 200
    stationary_gyro_threshold_dps: float = 2.0
    accel_bias_mps2: list[float] = field(default_factory=lambda: [0.0, 0.0, 0.0])
    gyro_bias_dps: list[float] = field(default_factory=lambda: [0.0, 0.0, 0.0])
    accel_scale: list[float] = field(default_factory=lambda: [1.0, 1.0, 1.0])
    gravity_mps2: float = 9.80665
    # Low-pass cutoff separating gravity from rider acceleration. This must be
    # far below the frequency of real riding: at 0.5 Hz the filter's time
    # constant is 0.32 s, so it follows a braking event and reports the braking
    # as a change in gravity. 0.05 Hz gives a 3.2 s constant, which passes
    # acceleration and absorbs only slow attitude changes such as a long climb.
    gravity_filter_hz: float = 0.05


@dataclass
class FusionConfig:
    process_noise: float = 0.8
    gps_position_std_m: float = 4.0
    gps_velocity_std_mps: float = 1.2
    # How long to hold late frames so out-of-order arrivals can be sorted.
    reorder_window_s: float = 0.3
    # Beyond this GPS silence the filter reports dead reckoning.
    max_gps_gap_s: float = 3.0


@dataclass
class PipelineConfig:
    buffer_seconds: float = 60.0
    queue_size: int = 2048
    output_rate_hz: float = 50.0


@dataclass
class StorageConfig:
    enabled: bool = False
    url: str = "http://localhost:8086"
    token: str = ""
    org: str = "apex"
    bucket: str = "telemetry"
    measurement: str = "helmet_telemetry"
    batch_size: int = 500
    flush_interval_s: float = 5.0
    spool_dir: str = "data/spool"
    max_spool_mb: float = 256.0


@dataclass
class StreamingConfig:
    enabled: bool = True
    rate_hz: float = 10.0
    max_clients: int = 8


@dataclass
class DashboardConfig:
    enabled: bool = True
    host: str = "0.0.0.0"
    port: int = 8000


@dataclass
class StatusLedConfig:
    enabled: bool = False
    red_pin: int = 17
    yellow_pin: int = 27
    green_pin: int = 22


@dataclass
class SafetyConfig:
    enabled: bool = True
    crash_g_threshold: float = 4.0
    # An impact must also shed this much speed to count.
    speed_drop_kmh: float = 20.0
    speed_lookback_s: float = 2.0
    # ...and the helmet must then go quiet, which a pothole never does.
    stillness_window_s: float = 3.0
    stillness_g_tolerance: float = 0.35
    sos_countdown_s: float = 30.0
    sos_webhook_url: str = ""
    emergency_contact: str = ""
    led: StatusLedConfig = field(default_factory=StatusLedConfig)


@dataclass
class VoiceConfig:
    enabled: bool = False
    wake_words: list[str] = field(default_factory=lambda: ["apex", "hey apex"])
    vosk_model_path: str = "models/vosk-model-small-en-us-0.15"
    piper_model_path: str = "models/en_US-amy-low.onnx"
    command_timeout_s: float = 6.0
    # Silence duration that ends an utterance.
    endpoint_silence_s: float = 0.8
    tts_output_device: str = ""
    speak_responses: bool = True


@dataclass
class AppConfig:
    node: NodeConfig = field(default_factory=NodeConfig)
    sensors: SensorsConfig = field(default_factory=SensorsConfig)
    calibration: CalibrationConfig = field(default_factory=CalibrationConfig)
    fusion: FusionConfig = field(default_factory=FusionConfig)
    pipeline: PipelineConfig = field(default_factory=PipelineConfig)
    storage: StorageConfig = field(default_factory=StorageConfig)
    streaming: StreamingConfig = field(default_factory=StreamingConfig)
    dashboard: DashboardConfig = field(default_factory=DashboardConfig)
    safety: SafetyConfig = field(default_factory=SafetyConfig)
    voice: VoiceConfig = field(default_factory=VoiceConfig)

    @property
    def use_hardware(self) -> bool:
        return self.node.sensor_backend.lower() == "hardware"


def _coerce(value: Any, target_type: Any) -> Any:
    """Best-effort conversion of a YAML/env scalar into the annotated type."""
    if target_type in (Any, None) or value is None:
        return value

    if get_origin(target_type) in (list, dict):
        return value

    args = getattr(target_type, "__args__", None)
    if args:  # Optional[X] and other unions: use the first non-None member.
        non_none = [t for t in args if t is not type(None)]
        if not non_none:
            return value
        target_type = non_none[0]

    if target_type is bool:
        if isinstance(value, str):
            return value.strip().lower() in {"1", "true", "yes", "on"}
        return bool(value)
    if target_type is int:
        return int(value, 0) if isinstance(value, str) else int(value)
    if target_type is float:
        return float(value)
    if target_type is str:
        return str(value)
    return value


def _apply(node: Any, data: dict[str, Any], path: str = "") -> None:
    """Recursively overlay a dict onto a dataclass instance."""
    if not is_dataclass(node):
        return

    # `from __future__ import annotations` makes dataclass field types strings,
    # so resolve them to real objects before coercing.
    hints = get_type_hints(type(node))
    for key, value in data.items():
        name = str(key).lower()
        if name not in hints:
            raise ValueError(f"Unknown config key: {path}{name}")

        current = getattr(node, name)
        if is_dataclass(current) and isinstance(value, dict):
            _apply(current, value, f"{path}{name}.")
        elif isinstance(value, list):
            setattr(node, name, list(value))
        else:
            setattr(node, name, _coerce(value, hints[name]))


def _env_overlay() -> dict[str, Any]:
    """Turn APEX__SECTION__KEY=value into a nested dict."""
    overlay: dict[str, Any] = {}
    for raw_key, raw_value in os.environ.items():
        if not raw_key.startswith(ENV_PREFIX):
            continue
        parts = [p.lower() for p in raw_key[len(ENV_PREFIX) :].split("__") if p]
        if not parts:
            continue
        cursor = overlay
        for part in parts[:-1]:
            cursor = cursor.setdefault(part, {})
        cursor[parts[-1]] = raw_value
    return overlay


def load_config(path: str | Path | None = None) -> AppConfig:
    """Build config from defaults, then the YAML file, then the environment."""
    config = AppConfig()

    config_path = Path(path) if path else DEFAULT_CONFIG_PATH
    if config_path.exists():
        raw = yaml.safe_load(config_path.read_text(encoding="utf-8")) or {}
        if not isinstance(raw, dict):
            raise ValueError(f"{config_path} must contain a YAML mapping")
        _apply(config, raw)

    env = _env_overlay()
    if env:
        _apply(config, env)

    return config
