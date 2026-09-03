"""System orchestrator for sensor ingestion, fusion, and downstream hooks."""

from __future__ import annotations

import argparse
import csv
import queue
import random
import signal
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Dict, Optional

from kalman import KalmanEngine, MotionState, SensorObservation


@dataclass
class SensorFrame:
    """Structured transport frame for daemon-internal communication."""

    timestamp: float
    source: str
    payload: Dict[str, float] = field(default_factory=dict)
    quality: float = 1.0
    metadata: Dict[str, str] = field(default_factory=dict)


class TelemetryDaemon:
    """Coordinates sensor readers, fusion engine, and telemetry hooks."""

    def __init__(
        self,
        imu_reader: Callable[[], SensorFrame],
        gps_reader: Callable[[], SensorFrame],
        *,
        imu_hz: float = 50.0,
        gps_hz: float = 5.0,
        queue_size: int = 256,
        reorder_window_s: float = 0.3,
        csv_path: Optional[Path] = None,
    ) -> None:
        self.imu_reader = imu_reader
        self.gps_reader = gps_reader
        self.imu_period = 1.0 / max(imu_hz, 1e-3)
        self.gps_period = 1.0 / max(gps_hz, 1e-3)
        self.reorder_window_s = max(0.0, reorder_window_s)

        self._queue: "queue.Queue[SensorFrame]" = queue.Queue(maxsize=max(32, queue_size))
        self._heap: list[tuple[float, int, SensorFrame]] = []
        self._seq = 0
        self._heap_lock = threading.Lock()

        self._stop_event = threading.Event()
        self._engine = KalmanEngine()
        self._threads: list[threading.Thread] = []

        self._latest_state = MotionState()
        self._latest_seen_ts = 0.0
        self._drop_count = 0

        self._csv_path = csv_path
        self._csv_header_written = False

    def start(self, duration_s: Optional[float] = None) -> None:
        self._install_signal_handlers()

        self._threads = [
            threading.Thread(target=self._sensor_loop, args=("imu", self.imu_reader, self.imu_period), daemon=True),
            threading.Thread(target=self._sensor_loop, args=("gps", self.gps_reader, self.gps_period), daemon=True),
            threading.Thread(target=self._fusion_loop, daemon=True),
        ]

        for thread in self._threads:
            thread.start()

        start_ts = time.time()
        try:
            while not self._stop_event.is_set():
                if duration_s is not None and (time.time() - start_ts) >= duration_s:
                    self.stop()
                    break
                time.sleep(0.2)
        finally:
            self.stop()
            for thread in self._threads:
                thread.join(timeout=2.0)

    def stop(self) -> None:
        self._stop_event.set()

    def get_latest_state(self) -> MotionState:
        return self._latest_state

    def _sensor_loop(self, name: str, reader: Callable[[], SensorFrame], period_s: float) -> None:
        while not self._stop_event.is_set():
            tick_start = time.time()
            try:
                frame = reader()
                if not frame.source:
                    frame.source = name
                self._enqueue_frame(frame)
            except Exception as exc:  # noqa: BLE001
                self._log_event("sensor_error", {"sensor": name, "error": str(exc)})
                time.sleep(min(period_s * 2.0, 1.0))

            elapsed = time.time() - tick_start
            sleep_for = max(0.0, period_s - elapsed)
            time.sleep(sleep_for)

    def _enqueue_frame(self, frame: SensorFrame) -> None:
        try:
            self._queue.put_nowait(frame)
        except queue.Full:
            try:
                self._queue.get_nowait()
            except queue.Empty:
                pass
            self._drop_count += 1
            self._queue.put_nowait(frame)
            self._log_event("queue_drop", {"drops": self._drop_count})

    def _fusion_loop(self) -> None:
        import heapq

        while not self._stop_event.is_set() or not self._queue.empty() or self._heap:
            try:
                frame = self._queue.get(timeout=0.15)
                with self._heap_lock:
                    self._seq += 1
                    heapq.heappush(self._heap, (frame.timestamp, self._seq, frame))
                    self._latest_seen_ts = max(self._latest_seen_ts, frame.timestamp)
            except queue.Empty:
                pass

            self._drain_ordered_frames(flush=self._stop_event.is_set())

    def _drain_ordered_frames(self, *, flush: bool) -> None:
        import heapq

        watermark = self._latest_seen_ts - self.reorder_window_s
        with self._heap_lock:
            while self._heap:
                ts, _, frame = self._heap[0]
                if not flush and ts > watermark:
                    break
                heapq.heappop(self._heap)
                self._process_frame(frame)

    def _process_frame(self, frame: SensorFrame) -> None:
        observation = SensorObservation(
            timestamp=frame.timestamp,
            source=frame.source,
            data=frame.payload,
            quality=frame.quality,
            sequence=self._seq,
        )
        self._latest_state = self._engine.push_observation(observation)

        self._detect_crash(frame)
        self._persist_state(self._latest_state)

    def _detect_crash(self, frame: SensorFrame) -> None:
        if frame.source != "imu":
            return
        ax = abs(frame.payload.get("ax_mps2", 0.0))
        ay = abs(frame.payload.get("ay_mps2", 0.0))
        total = (ax * ax + ay * ay) ** 0.5
        if total >= (4.0 * 9.81):
            self._log_event(
                "crash_candidate",
                {
                    "timestamp": frame.timestamp,
                    "accel_mps2": round(total, 2),
                    "x_m": round(self._latest_state.x_m, 2),
                    "y_m": round(self._latest_state.y_m, 2),
                },
            )

    def _persist_state(self, state: MotionState) -> None:
        if self._csv_path is None:
            return

        self._csv_path.parent.mkdir(parents=True, exist_ok=True)
        row = {
            "timestamp": state.timestamp,
            "x_m": state.x_m,
            "y_m": state.y_m,
            "vx_mps": state.vx_mps,
            "vy_mps": state.vy_mps,
            "speed_mps": state.speed_mps,
            "heading_deg": state.heading_deg,
            "position_confidence": state.position_confidence,
            "velocity_confidence": state.velocity_confidence,
            "acceleration_confidence": state.acceleration_confidence,
            "mode": state.mode,
        }

        write_header = not self._csv_header_written and not self._csv_path.exists()
        with self._csv_path.open("a", newline="", encoding="utf-8") as csv_file:
            writer = csv.DictWriter(csv_file, fieldnames=list(row.keys()))
            if write_header:
                writer.writeheader()
                self._csv_header_written = True
            writer.writerow(row)

    @staticmethod
    def _log_event(event_type: str, payload: Dict[str, object]) -> None:
        print(f"[{event_type}] {payload}")

    def _install_signal_handlers(self) -> None:
        def _handle_signal(_signum: int, _frame: object) -> None:
            self.stop()

        signal.signal(signal.SIGINT, _handle_signal)
        signal.signal(signal.SIGTERM, _handle_signal)


def simulated_imu_reader() -> SensorFrame:
    now = time.time()
    ax = random.gauss(0.0, 0.8)
    ay = random.gauss(0.0, 0.8)
    return SensorFrame(
        timestamp=now,
        source="imu",
        payload={"ax_mps2": ax, "ay_mps2": ay},
        quality=0.8,
    )


def _gps_state() -> Dict[str, float]:
    if not hasattr(_gps_state, "state"):
        _gps_state.state = {"x_m": 0.0, "y_m": 0.0, "vx": 3.0, "vy": 0.7}
    return _gps_state.state  # type: ignore[attr-defined]


def simulated_gps_reader() -> SensorFrame:
    st = _gps_state()
    dt = 0.2
    st["x_m"] += st["vx"] * dt + random.gauss(0.0, 0.4)
    st["y_m"] += st["vy"] * dt + random.gauss(0.0, 0.4)

    return SensorFrame(
        timestamp=time.time(),
        source="gps",
        payload={
            "x_m": st["x_m"],
            "y_m": st["y_m"],
            "vx_mps": st["vx"] + random.gauss(0.0, 0.3),
            "vy_mps": st["vy"] + random.gauss(0.0, 0.3),
            "position_std_m": 3.5,
            "velocity_std_mps": 1.0,
        },
        quality=0.9,
    )


def main() -> None:
    parser = argparse.ArgumentParser(description="Run APEX telemetry daemon")
    parser.add_argument("--duration", type=float, default=10.0, help="run duration in seconds")
    parser.add_argument(
        "--csv",
        type=Path,
        default=Path("data/telemetry.csv"),
        help="path to telemetry CSV output",
    )
    args = parser.parse_args()

    daemon = TelemetryDaemon(
        imu_reader=simulated_imu_reader,
        gps_reader=simulated_gps_reader,
        imu_hz=50.0,
        gps_hz=5.0,
        queue_size=256,
        reorder_window_s=0.3,
        csv_path=args.csv,
    )

    daemon.start(duration_s=args.duration)
    final_state = daemon.get_latest_state()
    print(
        "Final state:",
        {
            "x_m": round(final_state.x_m, 2),
            "y_m": round(final_state.y_m, 2),
            "speed_mps": round(final_state.speed_mps, 2),
            "heading_deg": round(final_state.heading_deg, 1),
            "mode": final_state.mode,
        },
    )


if __name__ == "__main__":
    main()
