"""Monotonic-anchored wall clock.

Sensor timestamps must never move backwards. `time.time()` can jump when NTP
corrects the Pi's clock (which it does shortly after boot, because the Zero has
no RTC), and a negative dt would corrupt the Kalman covariance. So we take one
epoch reading at import, then derive every later timestamp from the monotonic
counter.
"""

from __future__ import annotations

import time

_EPOCH_ANCHOR = time.time()
_MONO_ANCHOR = time.monotonic()


def now() -> float:
    """Epoch seconds that only ever increase."""
    return _EPOCH_ANCHOR + (time.monotonic() - _MONO_ANCHOR)


def monotonic() -> float:
    """Raw monotonic seconds, for measuring durations."""
    return time.monotonic()


def resync() -> float:
    """Re-anchor to wall clock. Call only when no dt is in flight.

    Returns the correction applied in seconds, which will be large and positive
    the first time NTP lands after boot.
    """
    global _EPOCH_ANCHOR, _MONO_ANCHOR
    correction = time.time() - now()
    _EPOCH_ANCHOR = time.time()
    _MONO_ANCHOR = time.monotonic()
    return correction
