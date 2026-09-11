"""SOS escalation: countdown, cancellation, and distress dispatch.

The countdown exists because crash detection is a probabilistic judgement and
the rider is the only party who knows the truth. Thirty seconds of "SOS in
28... 27..." costs nothing when the detector is right — the rider is
unconscious and cannot object — and costs nothing but a spoken "cancel" when
it is wrong. Dispatching immediately would mean every false positive becomes
a real emergency call, and after two of those the rider disables the feature.

Cancellation is only possible during the countdown. Once the message is out it
cannot be unsent, so the state machine refuses late cancels rather than
pretending to honour them.
"""

from __future__ import annotations

import asyncio
import contextlib
import inspect
import json
import logging
import time
import urllib.error
import urllib.request
from collections.abc import Awaitable, Callable
from enum import StrEnum
from typing import Any, Protocol

from app import clock
from app.config import SafetyConfig
from app.models import CrashEvent

logger = logging.getLogger(__name__)

DISPATCH_ATTEMPTS = 3
WEBHOOK_TIMEOUT_S = 10.0

StateCallback = Callable[["SosState", CrashEvent | None], Awaitable[None] | None]


class SosState(StrEnum):
    IDLE = "idle"
    COUNTDOWN = "countdown"
    DISPATCHING = "dispatching"
    SENT = "sent"
    FAILED = "failed"
    CANCELLED = "cancelled"


class Notifier(Protocol):
    """Anything that can get a distress payload off the helmet."""

    async def send(self, payload: dict[str, Any]) -> None:
        """Deliver the payload. Raise on failure so the caller can retry."""
        ...


class LoggingNotifier:
    """Fallback when no webhook is configured.

    A node with nowhere to send an SOS still records it loudly, so the
    incident is recoverable from the journal after the fact.
    """

    def __init__(self) -> None:
        self.sent: list[dict[str, Any]] = []

    async def send(self, payload: dict[str, Any]) -> None:
        self.sent.append(payload)
        logger.critical("SOS (no webhook configured): %s", json.dumps(payload))


class WebhookNotifier:
    """POSTs JSON to a configured URL.

    Uses `urllib` on a worker thread rather than adding an async HTTP client to
    the runtime dependencies. One request every few months does not justify the
    extra package on a 512 MB device.
    """

    def __init__(self, url: str, timeout_s: float = WEBHOOK_TIMEOUT_S) -> None:
        self.url = url
        self.timeout_s = timeout_s

    async def send(self, payload: dict[str, Any]) -> None:
        await asyncio.to_thread(self._post, payload)

    def _post(self, payload: dict[str, Any]) -> None:
        body = json.dumps(payload).encode()
        request = urllib.request.Request(  # noqa: S310 - URL comes from operator config
            self.url,
            data=body,
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        with urllib.request.urlopen(request, timeout=self.timeout_s) as response:  # noqa: S310
            if response.status >= 400:
                raise urllib.error.HTTPError(
                    self.url, response.status, "SOS webhook rejected", response.headers, None
                )


class SosController:
    """Cancellable countdown followed by a retried distress dispatch."""

    def __init__(
        self,
        config: SafetyConfig,
        helmet_id: str,
        *,
        notifier: Notifier | None = None,
        retry_backoff_s: float = 2.0,
    ) -> None:
        self.config = config
        self.helmet_id = helmet_id
        self.notifier = notifier or _default_notifier(config)
        self.retry_backoff_s = retry_backoff_s

        self._state = SosState.IDLE
        self._event: CrashEvent | None = None
        self._deadline = 0.0
        self._cancelled = asyncio.Event()
        self._task: asyncio.Task[None] | None = None
        self._subscribers: list[StateCallback] = []
        self._last_error = ""
        self._attempts = 0

    # --- state ------------------------------------------------------------

    @property
    def state(self) -> SosState:
        return self._state

    @property
    def event(self) -> CrashEvent | None:
        return self._event

    @property
    def active(self) -> bool:
        """True from arming until the outcome is known."""
        return self._state in (SosState.COUNTDOWN, SosState.DISPATCHING)

    @property
    def remaining_s(self) -> float:
        """Seconds left to cancel. Zero once the countdown has run out."""
        if self._state is not SosState.COUNTDOWN:
            return 0.0
        return max(0.0, self._deadline - clock.monotonic())

    def subscribe(self, callback: StateCallback) -> None:
        self._subscribers.append(callback)

    def stats(self) -> dict[str, Any]:
        return {
            "state": self._state.value,
            "remaining_s": round(self.remaining_s, 1),
            "attempts": self._attempts,
            "last_error": self._last_error,
            "event": _event_summary(self._event),
        }

    # --- control ----------------------------------------------------------

    def arm(self, event: CrashEvent) -> bool:
        """Begin the countdown. False if an escalation is already running."""
        if self.active:
            logger.info("SOS already %s; ignoring duplicate trigger", self._state.value)
            return False

        self._event = event
        self._cancelled.clear()
        self._last_error = ""
        self._attempts = 0
        self._deadline = clock.monotonic() + max(0.0, self.config.sos_countdown_s)
        # Enter COUNTDOWN here rather than inside the task. The task does not
        # run until the next loop iteration, and a rider who cancels in that
        # gap must not be told the countdown had not started yet.
        self._state = SosState.COUNTDOWN
        self._task = asyncio.create_task(self._run(), name="apex-sos")
        return True

    def cancel(self, reason: str = "rider cancelled") -> bool:
        """Stop a countdown in progress. False if it is too late or not running."""
        if self._state is not SosState.COUNTDOWN:
            logger.info("SOS cancel ignored in state %s", self._state.value)
            return False
        logger.warning("SOS cancelled: %s", reason)
        self._cancelled.set()
        return True

    async def wait(self) -> None:
        """Block until the current escalation reaches a terminal state."""
        if self._task is not None:
            await asyncio.shield(self._task)

    async def close(self) -> None:
        """Abandon any countdown. Called on shutdown, not by the rider."""
        if self._task is not None and not self._task.done():
            self._task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._task
        self._task = None

    # --- escalation -------------------------------------------------------

    async def _run(self) -> None:
        await self._notify(SosState.COUNTDOWN)

        try:
            await asyncio.wait_for(self._cancelled.wait(), timeout=self.remaining_s)
        except TimeoutError:
            pass  # countdown expired without a cancel, which is the escalation path
        else:
            await self._set_state(SosState.CANCELLED)
            return

        await self._set_state(SosState.DISPATCHING)
        payload = self._payload()

        for attempt in range(1, DISPATCH_ATTEMPTS + 1):
            self._attempts = attempt
            try:
                await self.notifier.send(payload)
            except Exception as exc:  # noqa: BLE001 - every failure mode is retryable here
                self._last_error = f"{type(exc).__name__}: {exc}"
                logger.error(
                    "SOS dispatch attempt %d/%d failed: %s", attempt, DISPATCH_ATTEMPTS, exc
                )
                if attempt < DISPATCH_ATTEMPTS:
                    await asyncio.sleep(self.retry_backoff_s * attempt)
            else:
                logger.critical("SOS dispatched on attempt %d", attempt)
                await self._set_state(SosState.SENT)
                return

        # Out of attempts. The state stays FAILED rather than resetting, so the
        # dashboard and the LED keep showing that help was never reached.
        await self._set_state(SosState.FAILED)

    def _payload(self) -> dict[str, Any]:
        event = self._event
        body: dict[str, Any] = {
            "alert": "crash",
            "helmet_id": self.helmet_id,
            "emergency_contact": self.config.emergency_contact,
            "sent_at": time.strftime("%Y-%m-%dT%H:%M:%S%z", time.localtime()),
        }
        if event is None:
            return body

        body.update(
            {
                "ride_id": event.ride_id,
                "occurred_at": time.strftime(
                    "%Y-%m-%dT%H:%M:%S%z", time.localtime(event.timestamp)
                ),
                "latitude": event.latitude,
                "longitude": event.longitude,
                # Responders need somewhere to tap, not a pair of floats.
                "map_url": f"https://maps.google.com/?q={event.latitude:.6f},{event.longitude:.6f}",
                "peak_g": event.peak_g,
                "speed_before_kmh": event.speed_before_kmh,
                "speed_after_kmh": event.speed_after_kmh,
                "detection": event.reason,
            }
        )
        return body

    async def _set_state(self, state: SosState) -> None:
        self._state = state
        await self._notify(state)

    async def _notify(self, state: SosState) -> None:
        logger.info("SOS state -> %s", state.value)
        for callback in list(self._subscribers):
            try:
                result = callback(state, self._event)
                if inspect.isawaitable(result):
                    await result
            except Exception:  # noqa: BLE001 - a listener must not derail an SOS
                logger.exception("SOS subscriber %r failed", callback)


def _default_notifier(config: SafetyConfig) -> Notifier:
    if config.sos_webhook_url:
        return WebhookNotifier(config.sos_webhook_url)
    return LoggingNotifier()


def _event_summary(event: CrashEvent | None) -> dict[str, Any] | None:
    if event is None:
        return None
    return {
        "timestamp": event.timestamp,
        "peak_g": event.peak_g,
        "speed_before_kmh": event.speed_before_kmh,
        "speed_after_kmh": event.speed_after_kmh,
        "latitude": event.latitude,
        "longitude": event.longitude,
        "reason": event.reason,
    }
