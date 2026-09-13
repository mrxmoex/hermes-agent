"""``/api/display/ws`` — raw RFB over WebSocket for the Bot Desktop viewer.

The Desktop renderer calls ``display.observe`` on its authenticated ``/api/ws`` connection, gets a
single-use 30 s ticket pinned to that profile's RFB socket, then opens this route with
``?display_ticket=``. No websockify, no new port: the bridge splices the profile's 0600 Unix socket
into the WebSocket as binary frames with backpressure both ways, and runs the client stream through
:class:`tools.bot_desktop.rfb_filter.RfbClientFilter` so keyboard, pointer and clipboard reach Xvnc
only from the viewer that currently holds the lease. noVNC's ``viewOnly`` is UX; this is the gate.

A lease change closes the evicted viewer's socket with 4000 ``control-taken`` so its UI drops back to
Watch mode and reconnects.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Optional

from fastapi import APIRouter, WebSocket, WebSocketDisconnect

from hermes_cli.web_server_chat import _ws_request_is_allowed

_log = logging.getLogger(__name__)
router = APIRouter()

_READ_CHUNK = 64 * 1024
_CLOSE_CONTROL_TAKEN = 4000
# 1000/1001 = the viewer closed the pane on purpose. 4002 = this window replaced
# its own stream (Reconnect). A code-less close is 1005; a dropped link is 1006.
# Only the first pair hands the screen back to the agent.
_CLEAN_CLOSE = frozenset({1000, 1001})
_CLOSE_DESKTOP_GONE = 4001
_CLOSE_STREAM_REPLACE = 4002
_CLOSE_BAD_TICKET = 4401
_CLOSE_NOT_ALLOWED = 4403
_CLOSE_PROTOCOL = 1003
_LEASE_REFRESH_S = 0.25
# One live RFB socket per (profile home, viewer_id). observe remints a ticket
# for the same viewer; without this a second accept shares the lease input gate.
_live_streams: dict[tuple[str, str], object] = {}


def _live_stream_key(hermes_home: str, viewer_id: str) -> tuple[str, str]:
    return (hermes_home, viewer_id)


async def _replace_live_stream(ws, hermes_home: str, viewer_id: str) -> tuple[str, str]:
    """Register *ws* as the live stream for this viewer; evict a previous one with 4002
    (same viewer replacing itself — keep the lease; 4000 would paint control-taken)."""
    key = _live_stream_key(hermes_home, viewer_id)
    previous = _live_streams.get(key)
    _live_streams[key] = ws
    if previous is not None and previous is not ws:
        try:
            await previous.close(code=_CLOSE_STREAM_REPLACE, reason="stream-replaced")
        except Exception:
            pass
    return key


def _forget_live_stream(key: tuple[str, str], ws) -> None:
    if _live_streams.get(key) is ws:
        del _live_streams[key]


def _reset_live_streams_for_tests() -> None:
    _live_streams.clear()


def _should_evict(held: dict, lease, viewer_id: str) -> bool:
    """A viewer that held control during this connection and lost it to ANOTHER human is kicked so its
    UI repaints; a plain hand-back to the agent, pure watchers and the new holder stay connected. The
    hand-back also forgets that this viewer ever held: after it, they are a plain watcher again and a
    later takeover by someone else must not evict them. ``held`` is the per-connection memory."""
    from tools.bot_desktop import lease as _lease
    if lease.holder != _lease.HUMAN:
        held["ever"] = False
        return False
    if lease.viewer_id == viewer_id:
        held["ever"] = True
        return False
    return bool(held["ever"])


def _consume_display_ticket(ws: WebSocket) -> Optional[dict]:
    from hermes_cli.dashboard_auth.ws_tickets import TicketInvalid, consume_ticket
    ticket = ws.query_params.get("display_ticket", "")
    if not ticket:
        return None
    try:
        info = consume_ticket(ticket)
    except TicketInvalid:
        return None
    if info.get("provider") != "bot-desktop" or not info.get("hermes_home"):
        return None
    return info


@router.websocket("/api/display/ws")
async def display_ws(ws: WebSocket) -> None:
    if not _ws_request_is_allowed(ws):
        await ws.close(code=_CLOSE_NOT_ALLOWED)
        return
    info = _consume_display_ticket(ws)
    if info is None:
        await ws.close(code=_CLOSE_BAD_TICKET, reason="display ticket missing, expired or used")
        return
    await _bridge(ws, info)


async def _bridge(ws: WebSocket, info: dict) -> None:
    """Pump RFB bytes between the viewer socket and THIS profile's Xvnc, gated by the lease."""
    from hermes_constants import hermes_home_key
    from tools.bot_desktop import lease as _lease
    from tools.bot_desktop.rfb_filter import RfbClientFilter
    from pathlib import Path

    sock = Path(info["hermes_home"]) / "bot-desktop" / "rfb.sock"
    profile_home = str(info["hermes_home"])
    profile_key = hermes_home_key(profile_home)
    viewer_id = str(info.get("viewer_id") or info.get("user_id") or "viewer")
    if not sock.exists():
        await ws.close(code=_CLOSE_DESKTOP_GONE, reason="Bot Desktop is not running")
        return
    try:
        reader, writer = await asyncio.open_unix_connection(str(sock))
    except OSError as exc:
        _log.warning("display ws: cannot reach RFB socket %s: %s", sock, exc)
        await ws.close(code=_CLOSE_DESKTOP_GONE, reason="Bot Desktop socket unreachable")
        return

    await ws.accept()
    live_key = await _replace_live_stream(ws, profile_home, viewer_id)
    loop = asyncio.get_running_loop()
    evicted = asyncio.Event()
    held = {"ever": _lease.viewer_may_send_input(viewer_id, profile_key=profile_home)}
    # Input gate cache: reading lease.json per client message (a stat + read on the event loop for
    # every pointer move) is replaced by a decision refreshed on this process's on_change callback
    # and by a file re-read at most every _LEASE_REFRESH_S, so another process's takeover still lands.
    allowed = {"input": held["ever"], "at": loop.time()}

    def _refresh_allowed(lease=None) -> None:
        if lease is None:
            lease = _lease.get(profile_key=profile_home)
        allowed["input"] = lease.holder == _lease.HUMAN and lease.viewer_id == viewer_id
        allowed["at"] = loop.time()
        # Eviction used to run only on in-process on_change. A takeover written by
        # another process (CLI, gateway, second serve) never fired that callback, so
        # the previous holder kept the RFB stream and watched the new human type.
        if _should_evict(held, lease, viewer_id):
            evicted.set()

    def _may_send_input() -> bool:
        if loop.time() - allowed["at"] > _LEASE_REFRESH_S:
            _refresh_allowed()
        return allowed["input"]

    def _on_lease(key: str, lease) -> None:
        if key != profile_key:
            return
        loop.call_soon_threadsafe(_refresh_allowed, lease)
    unsubscribe = _lease.on_change(_on_lease)

    rfb_filter = RfbClientFilter(_may_send_input)

    viewer_closed = asyncio.Event()

    async def rfb_to_ws() -> None:
        while True:
            chunk = await reader.read(_READ_CHUNK)
            if not chunk:
                return
            await ws.send_bytes(chunk)  # awaiting the send is the backpressure toward Xvnc

    async def ws_to_rfb() -> None:
        while True:
            message = await ws.receive()
            if message.get("type") == "websocket.disconnect":
                # 1000/1001 = the viewer closed the window; anything else is a dropped link.
                if message.get("code") in _CLEAN_CLOSE:
                    viewer_closed.set()
                return
            data = message.get("bytes")
            if data is None:
                await ws.close(code=_CLOSE_PROTOCOL, reason="RFB is binary")
                return
            try:
                allowed = rfb_filter.feed(data)
            except ValueError as exc:
                await ws.close(code=_CLOSE_PROTOCOL, reason=str(exc)[:100])
                return
            if allowed:
                writer.write(allowed)
                await writer.drain()  # backpressure toward the browser

    async def watch_eviction() -> None:
        await evicted.wait()
        await ws.close(code=_CLOSE_CONTROL_TAKEN, reason="control-taken")

    async def poll_lease() -> None:
        # _refresh_allowed used to run only on in-process on_change or when the
        # viewer sent input (the filter consults allow_input for Key/Pointer only).
        # A cross-process takeover plus an idle or viewOnly holder never hit
        # either path, so the previous viewer kept the framebuffer. Drive the
        # same disk refresh on a timer so eviction does not depend on input.
        while True:
            await asyncio.sleep(_LEASE_REFRESH_S)
            _refresh_allowed()

    tasks = [asyncio.create_task(rfb_to_ws()), asyncio.create_task(ws_to_rfb()),
             asyncio.create_task(watch_eviction()), asyncio.create_task(poll_lease())]
    try:
        done, pending = await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
        for t in pending:
            t.cancel()
        for t in done:
            exc = t.exception()
            if exc and not isinstance(exc, (WebSocketDisconnect, ConnectionError)):
                _log.debug("display ws ended: %r", exc)
    finally:
        _forget_live_stream(live_key, ws)
        unsubscribe()
        writer.close()
        # Closing the viewer window hands control back. A DROPPED link (laptop lid, Wi-Fi, 1006)
        # keeps the human's exclusion: they may be mid-login on that screen and the agent must not
        # resume into it. The Desktop reconnects into the same lease, or the human hands back.
        if viewer_closed.is_set() and _lease.viewer_may_send_input(viewer_id, profile_key=profile_home):
            _lease.release(viewer_id, profile_key=profile_home)
        try:
            await ws.close()
        except Exception:  # already closed by the peer or by an eviction
            pass
