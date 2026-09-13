"""Stop CDP supervisors aimed at a human-held Bot Desktop Chromium.

Leftover watchdogs and auto-dialog policies talk to the page over CDP with no
command fence. Take over from Desktop writes the lease in another process; this
module subscribes to in-process acquires and polls served profile homes for the
rest.

A leftover supervisor is minted under a bot's ``HERMES_HOME`` and then outlives
that turn. The process home after a multiplex turn is the launch profile —
whose dock port and ``lease.json`` belong to a different bot. Every leftover
check therefore re-enters the home stamped on the supervisor (or recorded on
the session), not the ambient process home.
"""

from __future__ import annotations

import contextlib
import threading
import time
from typing import Optional

_listener = None
_watch_started = False
_watch_lock = threading.Lock()

# Same cadence as the RFB input gate. A leftover supervisor whose socket is
# still up only admitted before reconnect; this is how a live read loop /
# Fetch interceptor notices a cross-process Take over.
LEASE_POLL_S = 0.25


@contextlib.contextmanager
def _home_scope(home: Optional[str]):
    """Evaluate lease + dock identity on ``home``, or the current profile if unset."""
    if not home:
        yield
        return
    from hermes_constants import reset_hermes_home_override, set_hermes_home_override
    token = set_hermes_home_override(home)
    try:
        yield
    finally:
        reset_hermes_home_override(token)


def _supervisor_home(supervisor, task_id: Optional[str] = None) -> Optional[str]:
    """Profile that minted this leftover supervisor, else the session owner, else None."""
    home = getattr(supervisor, "hermes_home", None)
    if isinstance(home, str) and home:
        return home
    if not task_id:
        task_id = getattr(supervisor, "task_id", None)
    if not task_id:
        return None
    try:
        from tools import browser_tool as _bt
        owner = _bt._session_owner_homes.get(task_id)
        return str(owner) if owner else None
    except Exception:
        return None


def supervisor_may_touch_page(
    cdp_url: str = "",
    home: Optional[str] = None,
    targets_bot_desktop=None,
) -> bool:
    """False when a leftover supervisor must not talk to this Chromium.

    A human-held dock jar is the page they are typing into. Reconnect,
    ``Target.createTarget``, Fetch passthrough, and ``/json/version`` are all
    observation/action on that jar. Unrelated remote CDP is another browser.

    ``home`` is the HERMES_HOME that owns the dock. ``targets_bot_desktop`` is
    mint-time identity so a missing ``DevToolsActivePort`` cannot unfence a
    leftover WS that was the dock when it attached.
    """
    try:
        from tools.bot_desktop.lease import HumanHasControl
        from tools.browser_tool_session import _admit_shared_browser
        _admit_shared_browser(
            cdp_url=cdp_url or "",
            home=home,
            treat_as_dock=targets_bot_desktop is True,
        )
        return True
    except HumanHasControl:
        return False
    except Exception:
        return True


def request_leftover_stop(supervisor) -> bool:
    """Mark a leftover dock supervisor to drop the jar. Safe on its own thread.

    Does not ``join()`` — that deadlocks the supervisor loop. Callers close the
    WebSocket or break the read loop; ``_run`` then exits instead of reconnecting.
    """
    home = _supervisor_home(supervisor)
    targets = getattr(supervisor, "targets_bot_desktop", None)
    if supervisor_may_touch_page(
        getattr(supervisor, "cdp_url", "") or "",
        home=home,
        targets_bot_desktop=targets if isinstance(targets, bool) else None,
    ):
        return False
    setattr(supervisor, "_stop_requested", True)
    return True


def install_supervisor_lease_hook() -> None:
    """Stop dock-aimed supervisors the moment *this* process writes HUMAN.

    Cross-process takeovers are picked up by the watch thread, which walks
    every leftover supervisor under its own home. ``lease._reset_for_tests``
    drops listeners, so we re-subscribe when the callback is gone.
    """
    global _listener, _watch_started
    from tools.bot_desktop import lease as _bd_lease
    if _listener is None or _listener not in _bd_lease._listeners:
        def _on_lease(profile_key: str, lease) -> None:
            if lease.holder != _bd_lease.HUMAN:
                return
            stop_reserved_supervisors(home=profile_key)

        _listener = _on_lease
        _bd_lease.on_change(_on_lease)
    with _watch_lock:
        if not _watch_started:
            _watch_started = True
            threading.Thread(
                target=_watch_loop, daemon=True, name="bot-desktop-supervisor-lease",
            ).start()


def _watch_once() -> None:
    """Detach leftover supervisors on every served profile whose human holds.

    Must not pre-filter on the launch home's ``human_holds()`` — a sibling
    bot's Take over writes a different ``lease.json``.
    """
    stop_reserved_supervisors()


def _watch_loop() -> None:
    while True:
        try:
            _watch_once()
        except Exception:
            pass
        time.sleep(LEASE_POLL_S)


def stop_reserved_supervisors(home: Optional[str] = None) -> None:
    """Detach CDP supervisors aimed at the Chromium a human is typing into.

    Closing the WS drops the Fetch interceptor so a leftover bridge XHR fails
    closed (dismiss) instead of auto-accepting. Cloud / unrelated CDP
    supervisors are left alone.

    ``home`` limits the sweep to one profile (in-process acquire). ``None``
    walks every leftover supervisor under the home it was minted on — the
    cross-process / multiplex path.
    """
    install_supervisor_lease_hook()
    try:
        from hermes_constants import hermes_home_key
        from tools.bot_desktop import lease as _bd_lease
        from tools.browser_supervisor import SUPERVISOR_REGISTRY
        from tools import browser_tool as _bt
        from tools import browser_tool_session as _session
    except Exception:
        return
    want = hermes_home_key(home) if home is not None else None
    try:
        with SUPERVISOR_REGISTRY._lock:
            items = list(SUPERVISOR_REGISTRY._by_task.items())
    except Exception:
        return
    for task_id, sup in items:
        try:
            owner = _supervisor_home(sup, task_id)
            if want is not None and owner is not None and hermes_home_key(owner) != want:
                continue
            with _home_scope(owner):
                if want is not None and hermes_home_key() != want:
                    continue
                if not _bd_lease.human_holds():
                    continue
                session_info = _bt._active_sessions.get(task_id)
                cdp_url = str(getattr(sup, "cdp_url", "") or "")
                stamped_dock = getattr(sup, "targets_bot_desktop", None) is True
                if session_info is not None and not _session._is_shared_bot_desktop_session(session_info):
                    continue
                if (
                    session_info is None
                    and not stamped_dock
                    and not _session._cdp_url_is_bot_desktop_browser(cdp_url)
                ):
                    continue
                SUPERVISOR_REGISTRY.stop(task_id)
        except Exception:
            _bt.logger.debug("reserved supervisor stop failed for %s", task_id, exc_info=True)
