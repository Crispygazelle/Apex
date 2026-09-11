"""Filter behaviour: convergence, dead reckoning, outlier rejection, stability."""

from __future__ import annotations

import numpy as np
import pytest

from app.config import FusionConfig
from app.models import FusionMode
from app.processing.kalman import KalmanEngine


@pytest.fixture
def engine() -> KalmanEngine:
    return KalmanEngine(FusionConfig(max_gps_gap_s=3.0))


def test_starts_uninitialised() -> None:
    engine = KalmanEngine()
    assert not engine.initialised
    assert engine.state().mode is FusionMode.INIT


def test_first_fix_initialises_directly_without_easing_in(engine: KalmanEngine) -> None:
    engine.correct_gps(1.0, east_m=100.0, north_m=-50.0, v_east_mps=5.0, v_north_mps=0.0)

    state = engine.state()
    assert state.east_m == pytest.approx(100.0)
    assert state.north_m == pytest.approx(-50.0)
    assert engine.initialised


def test_converges_on_a_noisy_but_unbiased_position(engine: KalmanEngine) -> None:
    rng = np.random.default_rng(4)
    true_east, true_north = 40.0, 25.0

    t = 0.0
    for _ in range(200):
        t += 0.1
        engine.predict_to(t)
        engine.correct_gps(
            t,
            east_m=true_east + rng.normal(0, 4.0),
            north_m=true_north + rng.normal(0, 4.0),
            v_east_mps=rng.normal(0, 0.5),
            v_north_mps=rng.normal(0, 0.5),
        )

    state = engine.state()
    # Averaging 200 fixes with 4 m sigma should land well inside 2 m.
    assert abs(state.east_m - true_east) < 2.0
    assert abs(state.north_m - true_north) < 2.0
    assert state.position_confidence > 0.5


def test_dead_reckons_a_constant_velocity_through_a_gps_outage(
    engine: KalmanEngine,
) -> None:
    engine.correct_gps(0.0, east_m=0.0, north_m=0.0, v_east_mps=10.0, v_north_mps=0.0)

    # Ten seconds of IMU-only prediction at a steady 10 m/s east.
    t = 0.0
    for _ in range(1000):
        t += 0.01
        engine.predict_to(t)

    state = engine.state()
    assert state.mode is FusionMode.DEAD_RECKONING
    # No acceleration was applied, so the position should track v * t.
    assert state.east_m == pytest.approx(100.0, abs=1.0)
    # ...but confidence must decay, because nothing corroborated it.
    assert state.position_confidence < 0.5


def test_mode_returns_to_fused_when_gps_comes_back(engine: KalmanEngine) -> None:
    engine.correct_gps(0.0, 0.0, 0.0, 0.0, 0.0)
    engine.predict_to(5.0)
    assert engine.state().mode is FusionMode.DEAD_RECKONING

    engine.correct_gps(5.0, 0.0, 0.0, 0.0, 0.0)
    assert engine.state().mode is FusionMode.FUSED


def test_acceleration_input_moves_the_state(engine: KalmanEngine) -> None:
    engine.initialise(0.0, 0.0, 0.0)

    t = 0.0
    for _ in range(100):  # 1 s at 100 Hz, accelerating north at 2 m/s^2
        t += 0.01
        engine.predict_to(t, accel_east_mps2=0.0, accel_north_mps2=2.0)

    state = engine.state()
    assert state.v_north_mps == pytest.approx(2.0, abs=0.05)
    assert state.north_m == pytest.approx(1.0, abs=0.05)


def test_rejects_a_wild_fix_but_accepts_plausible_ones(engine: KalmanEngine) -> None:
    engine.correct_gps(0.0, 0.0, 0.0, 0.0, 0.0)
    t = 0.1
    engine.predict_to(t)

    # A multipath reflection off a building: kilometres away in one tick.
    accepted = engine.correct_gps(
        t, east_m=5000.0, north_m=5000.0, v_east_mps=0.0, v_north_mps=0.0, position_std_m=4.0
    )
    assert accepted is False
    assert engine.rejected_fixes == 1
    assert abs(engine.state().east_m) < 50.0

    # A believable fix still gets through.
    assert engine.correct_gps(0.2, 3.0, 2.0, 0.0, 0.0, position_std_m=4.0) is True


def test_covariance_stays_symmetric_and_positive_definite() -> None:
    """The Joseph-form update exists to make this hold over a long ride."""
    engine = KalmanEngine()
    rng = np.random.default_rng(9)
    engine.correct_gps(0.0, 0.0, 0.0, 0.0, 0.0)

    t = 0.0
    for _ in range(3000):
        t += 0.01
        engine.predict_to(t, rng.normal(0, 1.0), rng.normal(0, 1.0))
        if int(t * 100) % 10 == 0:
            engine.correct_gps(t, rng.normal(0, 3), rng.normal(0, 3), 0.0, 0.0)

    cov = engine._cov  # noqa: SLF001 - asserting an internal invariant
    assert np.allclose(cov, cov.T, atol=1e-9)
    assert np.all(np.linalg.eigvalsh(cov) > 0.0)


def test_backwards_time_is_ignored_rather_than_integrated(engine: KalmanEngine) -> None:
    engine.initialise(10.0, 0.0, 0.0, v_east_mps=5.0)
    engine.predict_to(11.0)
    before = engine.state()

    engine.predict_to(10.5)  # out of order, should be a no-op
    after = engine.state()

    assert after.east_m == pytest.approx(before.east_m)


def test_reset_returns_to_a_clean_slate(engine: KalmanEngine) -> None:
    engine.correct_gps(1.0, 500.0, 500.0, 10.0, 10.0)
    engine.reset()

    assert not engine.initialised
    assert engine.state().east_m == 0.0
    assert engine.rejected_fixes == 0
