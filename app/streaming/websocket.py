"""Live telemetry fan-out to connected browsers.

Kept deliberately free of any web framework: the hub talks to anything with an
async `send_json`, so it can be tested with a fake client and does not care
whether the transport is a FastAPI WebSocket or something else later.

Two things matter here. The feed is downsampled, because pushing 50 Hz to a
browser accomplishes nothing a human can see while costing the Pi Zero real
CPU on JSON serialisation. And a client that has stopped reading is dropped
rather than allowed to apply backpressure to the fusion loop.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any, Protocol

from app.config import StreamingConfig
from app.models import RideSample

logger = logging.getLogger(__name__)

# A slow client is given this long to accept a frame before being disconnected.
SEND_TIMEOUT_S = 2.0


class StreamClient(Protocol):
    """Minimal surface the hub needs from a transport."""

    async def send_json(self, data: Any) -> None: ...


class TelemetryHub:
    """Throttled broadcast to a bounded set of clients."""

    def __init__(self, config: StreamingConfig | None = None) -> None:
        self.config = config or StreamingConfig()
        self._clients: set[StreamClient] = set()
        self._last_broadcast = 0.0
        self._period = 1.0 / max(self.config.rate_hz, 0.1)

        self.frames_sent = 0
        self.frames_skipped = 0
        self.clients_dropped = 0

    # --- membership -------------------------------------------------------

    @property
    def client_count(self) -> int:
        return len(self._clients)

    @property
    def full(self) -> bool:
        return len(self._clients) >= self.config.max_clients

    def register(self, client: StreamClient) -> bool:
        """Add a client. Returns False if the hub is at capacity."""
        if self.full:
            logger.warning(
                "Refusing telemetry client: already at max_clients=%d",
                self.config.max_clients,
            )
            return False
        self._clients.add(client)
        logger.info("Telemetry client connected (%d total)", len(self._clients))
        return True

    def unregister(self, client: StreamClient) -> None:
        self._clients.discard(client)
        logger.info("Telemetry client disconnected (%d remaining)", len(self._clients))

    # --- broadcast --------------------------------------------------------

    async def broadcast(self, sample: RideSample) -> None:
        """Send a sample to every client, subject to the output rate."""
        if not self._clients:
            return

        if sample.timestamp - self._last_broadcast < self._period:
            self.frames_skipped += 1
            return
        self._last_broadcast = sample.timestamp

        await self.publish(sample.to_dict())

    async def publish(self, payload: dict[str, Any]) -> None:
        """Send an arbitrary payload immediately, bypassing the throttle.

        Used for events that must not be dropped by downsampling, such as a
        crash alert.
        """
        if not self._clients:
            return

        # Snapshot once: the set can change while we are awaiting, and pairing
        # results against a second iteration would attribute failures to the
        # wrong client.
        clients = tuple(self._clients)
        results = await asyncio.gather(
            *(self._send(client, payload) for client in clients),
            return_exceptions=True,
        )

        for client, result in zip(clients, results, strict=True):
            if isinstance(result, BaseException):
                self.clients_dropped += 1
                self.unregister(client)

    async def _send(self, client: StreamClient, payload: dict[str, Any]) -> None:
        # A browser on a flaky link can stop draining its socket. Without a
        # timeout this await would block the broadcast, and through it the
        # fusion loop, for as long as the client stayed silent.
        async with asyncio.timeout(SEND_TIMEOUT_S):
            await client.send_json(payload)
        self.frames_sent += 1

    async def close(self) -> None:
        self._clients.clear()

    def stats(self) -> dict[str, Any]:
        return {
            "clients": self.client_count,
            "max_clients": self.config.max_clients,
            "rate_hz": self.config.rate_hz,
            "frames_sent": self.frames_sent,
            "frames_skipped": self.frames_skipped,
            "clients_dropped": self.clients_dropped,
        }
