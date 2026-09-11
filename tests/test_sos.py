"""SOS escalation: the countdown, the cancel, and the dispatch retries."""

from __future__ import annotations

import asyncio
import json
from typing import Any

import pytest

from app.config import SafetyConfig
from app.models import CrashEvent
from app.safety.sos import LoggingNotifier, SosController, SosState, WebhookNotifier

EVENT = CrashEvent(
    timestamp=1_700_000_000.0,
    ride_id="ride-test",
    peak_g=8.4,
    speed_before_kmh=62.0,
    speed_after_kmh=0.0,
    latitude=12.9716,
    longitude=77.5946,
    confirmed=True,
    reason="8.4 g, 62.0 km/h lost, helmet at rest",
)


class StubNotifier:
    def __init__(self, fail_times: int = 0) -> None:
        self.fail_times = fail_times
        self.calls: list[dict[str, Any]] = []

    async def send(self, payload: dict[str, Any]) -> None:
        self.calls.append(payload)
        if len(self.calls) <= self.fail_times:
            raise ConnectionError("no network")


def controller(notifier: Any, countdown_s: float = 0.05, **kwargs: Any) -> SosController:
    config = SafetyConfig(sos_countdown_s=countdown_s, emergency_contact="+911234567890")
    return SosController(
        config, "helmet01", notifier=notifier, retry_backoff_s=kwargs.pop("backoff", 0.0)
    )


async def test_countdown_expires_and_dispatches() -> None:
    notifier = StubNotifier()
    sos = controller(notifier)

    assert sos.arm(EVENT)
    assert sos.state is SosState.COUNTDOWN
    await sos.wait()

    assert sos.state is SosState.SENT
    assert len(notifier.calls) == 1


async def test_cancelling_during_the_countdown_sends_nothing() -> None:
    notifier = StubNotifier()
    sos = controller(notifier, countdown_s=5.0)

    sos.arm(EVENT)
    assert sos.cancel("rider said they are fine")
    await sos.wait()

    assert sos.state is SosState.CANCELLED
    assert notifier.calls == [], "a cancelled SOS must never reach the notifier"


async def test_cancel_works_in_the_gap_before_the_task_runs() -> None:
    """arm() then cancel() with no await between must still cancel.

    The countdown task does not start until the next loop iteration, and a
    rider who reacts instantly must not be told there is nothing to cancel.
    """
    notifier = StubNotifier()
    sos = controller(notifier, countdown_s=5.0)

    sos.arm(EVENT)
    assert sos.cancel()

    await sos.wait()
    assert sos.state is SosState.CANCELLED
    assert notifier.calls == []


async def test_cancelling_after_dispatch_is_refused() -> None:
    """You cannot unsend a distress call, so the API must not pretend."""
    notifier = StubNotifier()
    sos = controller(notifier)

    sos.arm(EVENT)
    await sos.wait()
    assert sos.state is SosState.SENT

    assert not sos.cancel()
    assert sos.state is SosState.SENT


async def test_dispatch_retries_then_succeeds() -> None:
    notifier = StubNotifier(fail_times=2)
    sos = controller(notifier)

    sos.arm(EVENT)
    await sos.wait()

    assert sos.state is SosState.SENT
    assert len(notifier.calls) == 3


async def test_exhausted_retries_stay_visible_as_failed() -> None:
    """The node must keep saying help was never reached, not reset to idle."""
    notifier = StubNotifier(fail_times=99)
    sos = controller(notifier)

    sos.arm(EVENT)
    await sos.wait()

    assert sos.state is SosState.FAILED
    assert len(notifier.calls) == 3
    assert "ConnectionError" in sos.stats()["last_error"]


async def test_arming_twice_does_not_start_a_second_countdown() -> None:
    notifier = StubNotifier()
    sos = controller(notifier, countdown_s=5.0)

    assert sos.arm(EVENT)
    assert not sos.arm(EVENT)

    sos.cancel()
    await sos.wait()
    assert notifier.calls == []


async def test_remaining_seconds_count_down_and_stop_at_zero() -> None:
    sos = controller(StubNotifier(), countdown_s=30.0)
    sos.arm(EVENT)

    first = sos.remaining_s
    assert 29.0 < first <= 30.0
    await asyncio.sleep(0.05)
    assert sos.remaining_s < first

    sos.cancel()
    await sos.wait()
    assert sos.remaining_s == 0.0


async def test_the_payload_carries_what_a_responder_needs() -> None:
    notifier = StubNotifier()
    sos = controller(notifier)

    sos.arm(EVENT)
    await sos.wait()

    payload = notifier.calls[0]
    assert payload["alert"] == "crash"
    assert payload["helmet_id"] == "helmet01"
    assert payload["emergency_contact"] == "+911234567890"
    assert payload["latitude"] == EVENT.latitude
    assert "12.971600,77.594600" in payload["map_url"]
    assert payload["peak_g"] == 8.4
    assert payload["detection"] == EVENT.reason
    # Must survive the trip over a webhook.
    json.dumps(payload)


async def test_subscribers_see_every_transition() -> None:
    seen: list[SosState] = []
    sos = controller(StubNotifier())
    sos.subscribe(lambda state, _event: seen.append(state))

    sos.arm(EVENT)
    await sos.wait()

    assert seen == [SosState.COUNTDOWN, SosState.DISPATCHING, SosState.SENT]


async def test_a_broken_subscriber_does_not_derail_the_sos() -> None:
    notifier = StubNotifier()
    sos = controller(notifier)

    def explode(state: SosState, event: CrashEvent | None) -> None:
        raise RuntimeError("subscriber is broken")

    sos.subscribe(explode)
    sos.arm(EVENT)
    await sos.wait()

    assert sos.state is SosState.SENT
    assert len(notifier.calls) == 1


async def test_close_abandons_a_running_countdown() -> None:
    notifier = StubNotifier()
    sos = controller(notifier, countdown_s=60.0)

    sos.arm(EVENT)
    await sos.close()

    assert notifier.calls == []


async def test_logging_notifier_is_the_fallback_without_a_webhook() -> None:
    """A node with nowhere to send still records the incident."""
    sos = SosController(SafetyConfig(sos_countdown_s=0.0), "helmet01", retry_backoff_s=0.0)
    assert isinstance(sos.notifier, LoggingNotifier)

    sos.arm(EVENT)
    await sos.wait()

    assert sos.state is SosState.SENT
    assert len(sos.notifier.sent) == 1


def test_webhook_notifier_is_chosen_when_a_url_is_configured() -> None:
    config = SafetyConfig(sos_webhook_url="https://example.invalid/sos")
    sos = SosController(config, "helmet01")
    assert isinstance(sos.notifier, WebhookNotifier)
    assert sos.notifier.url == "https://example.invalid/sos"


async def test_webhook_failure_surfaces_as_a_retry(monkeypatch: pytest.MonkeyPatch) -> None:
    """The real notifier's errors must reach the retry loop, not escape it."""
    notifier = WebhookNotifier("https://example.invalid/sos", timeout_s=0.1)
    attempts = 0

    def boom(payload: dict[str, Any]) -> None:
        nonlocal attempts
        attempts += 1
        raise OSError("dns failure")

    monkeypatch.setattr(notifier, "_post", boom)
    config = SafetyConfig(sos_countdown_s=0.0)
    sos = SosController(config, "helmet01", notifier=notifier, retry_backoff_s=0.0)

    sos.arm(EVENT)
    await sos.wait()

    assert sos.state is SosState.FAILED
    assert attempts == 3
