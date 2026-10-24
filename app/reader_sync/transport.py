"""WebSocket transport binding for dual-reader sync (#78 / S03).

`/ws/reader-sync` — authenticated via the existing signed-cookie session
(SessionMiddleware populates scope["session"] for websocket scopes too).
All pairing/session logic lives in sessions.py; this file only adapts the
socket: one drain task per link (bounded queue), a receive loop, and close
plumbing.
"""
from __future__ import annotations

import asyncio
import logging

from starlette.websockets import WebSocket, WebSocketDisconnect

from app.auth import user_from_connection
from app.reader_sync.sessions import PeerLink, SessionRegistry, WS_CLOSE_POLICY

logger = logging.getLogger(__name__)


class WebSocketLink(PeerLink):
    """PeerLink over a starlette WebSocket.

    Outbound frames go through the bounded outq and a drain task, so a slow
    peer never blocks the session registry — the queue bound disconnects it
    instead (sessions.PeerLink.send_obj).
    """

    def __init__(self, websocket: WebSocket, registry: SessionRegistry, user: dict):
        super().__init__(registry)
        self.user = user
        self._ws = websocket
        self._loop = asyncio.get_running_loop()
        self._drain: asyncio.Task | None = None
        self._close_started = False

    def start(self) -> None:
        self._drain = self._loop.create_task(
            self._drain_loop(), name=f"reader-sync-drain-{self.id}")
        self.registry.track_task(self._drain)

    def stop(self) -> None:
        if self._drain is not None and not self._drain.done():
            self._drain.cancel()

    async def _drain_loop(self) -> None:
        try:
            while True:
                raw = await self.outq.get()
                await self._ws.send_text(raw)
        except asyncio.CancelledError:
            raise
        except Exception:
            # socket died mid-send; the receive loop's finally tears down
            pass

    def request_transport_close(self, code: int, reason: str) -> None:
        if self._close_started:
            return
        self._close_started = True

        def _begin():
            self.stop()
            task = self._loop.create_task(self._safe_close(code, reason))
            self.registry.track_task(task)

        # Prefer a direct task: on-loop callers (tick/dispatch/aclose) get the
        # close tracked synchronously, so aclose()'s pending-task snapshot
        # can't miss it. Off-loop callers fall back to a thread-safe schedule.
        try:
            running = asyncio.get_running_loop()
        except RuntimeError:
            running = None
        if running is self._loop:
            _begin()
        else:
            try:
                self._loop.call_soon_threadsafe(_begin)
            except RuntimeError:
                pass  # loop gone; nothing to close

    async def _safe_close(self, code: int, reason: str) -> None:
        try:
            await self._ws.close(code=code, reason=reason)
        except Exception:
            pass


async def reader_sync_ws(websocket: WebSocket) -> None:
    """`GET /ws/reader-sync` — one link per connection.

    Unauthenticated handshakes are rejected (403); authenticated ones get a
    HELLO, then frames flow through SessionRegistry.handle_raw.
    """
    user = user_from_connection(websocket)
    if user is None:
        await websocket.close(code=4401)
        return
    await websocket.accept()

    registry: SessionRegistry = websocket.app.state.reader_sync
    link = WebSocketLink(websocket, registry, user)
    registry.register_link(link)
    link.start()
    try:
        while True:
            msg = await websocket.receive()
            kind = msg["type"]
            if kind == "websocket.disconnect":
                break
            if kind != "websocket.receive":
                continue
            data = msg.get("bytes")
            if data is None:
                data = msg.get("text")
            if data is None:
                continue
            registry.handle_raw(link, data)
    except WebSocketDisconnect:
        pass
    except Exception:
        logger.debug("reader-sync link=%s receive loop error", link.id)
    finally:
        registry.detach_link(link)
        link.stop()
