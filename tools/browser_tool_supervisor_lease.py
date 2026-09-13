"""Stop CDP supervisors aimed at a human-held Bot Desktop Chromium.

Leftover watchdogs and auto-dialog policies talk to the page over CDP with no
command fence. Take over from Desktop writes the lease in another process; this
module subscribes to in-process acquires and polls once a second for the rest.
"""

from __future__ import annotations

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


def supervisor_may_touch_page(cdp_url: str = "") -> bool:
    """False when a leftover supervisor must not talk to this Chromium.

    A human-held dock jar is the page they are typing into. Reconnect,
    ``Target.createTarget``, Fetch passthrough, and ``/json/version`` are all
    observation/action on that jar. Unrelated remote CDP is another browser.
    """
    try:
        from tools.bot_desktop.lease import HumanHasControl
        from tools.browser_tool_session import _admit_shared_browser
        _admit_shared_browser(cdp_url=cdp_url or "")
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
    if supervisor_may_touch_page(getattr(supervisor, "cdp_url", "") or ""):
        return False
    setattr(supervisor, "_stop_requested", True)
    return True


def install_supervisor_lease_hook() -> None:
    """Stop dock-aimed supervisors the moment *this* process writes HUMAN.

    Cross-process takeovers are picked up by the 1s watch thread. ``lease._reset_for_tests``
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


def _watch_loop() -> None:
    while True:
        try:
            from tools.bot_desktop import lease as _bd_lease
            if _bd_lease.human_holds():
                stop_reserved_supervisors()
        except Exception:
            pass
        time.sleep(LEASE_POLL_S)


def stop_reserved_supervisors(home: Optional[str] = None) -> None:
    """Detach CDP supervisors aimed at the Chromium a human is typing into.

    Closing the WS drops the Fetch interceptor so a leftover bridge XHR fails
    closed (dismiss) instead of auto-accepting. Cloud / unrelated CDP
    supervisors are left alone.
    """
    install_supervisor_lease_hook()
    try:
        from hermes_constants import hermes_home_key
        from tools.bot_desktop import lease as _bd_lease
        from tools.browser_supervisor import SUPERVISOR_REGISTRY
        from tools import browser_tool as _bt
        from tools import browser_tool_session as _session
        from tools.browser_tool_lifecycle import _session_owner_scope
    except Exception:
        return
    want = hermes_home_key(home) if home is not None else hermes_home_key()
    try:
        with SUPERVISOR_REGISTRY._lock:
            items = list(SUPERVISOR_REGISTRY._by_task.items())
    except Exception:
        return
    for task_id, sup in items:
        try:
            with _session_owner_scope(task_id):
                if hermes_home_key() != want:
                    continue
                if not _bd_lease.human_holds():
                    continue
                session_info = _bt._active_sessions.get(task_id)
                cdp_url = str(getattr(sup, "cdp_url", "") or "")
                if session_info is not None and not _session._is_shared_bot_desktop_session(session_info):
                    continue
                if session_info is None and not _session._cdp_url_is_bot_desktop_browser(cdp_url):
                    continue
                SUPERVISOR_REGISTRY.stop(task_id)
        except Exception:
            _bt.logger.debug("reserved supervisor stop failed for %s", task_id, exc_info=True)
