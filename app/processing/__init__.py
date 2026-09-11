"""Signal processing: clean the data, estimate state, derive rider metrics."""

from __future__ import annotations

from app.processing.calibration import CalibratedImu, Calibrator
from app.processing.kalman import FilterState, KalmanEngine, variance_to_confidence
from app.processing.metrics import MetricsCalculator
from app.processing.synchronization import FrameSynchronizer, SyncStats

__all__ = [
    "CalibratedImu",
    "Calibrator",
    "FilterState",
    "FrameSynchronizer",
    "KalmanEngine",
    "MetricsCalculator",
    "SyncStats",
    "variance_to_confidence",
]
