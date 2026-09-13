"""The bot's browser tools obey the screen lease: while a human holds the shared browser, nothing is
dispatched, and a command whose run crossed a takeover loses its result."""

from __future__ import annotations

import json

import pytest

from tools.bot_desktop import lease, runtime


@pytest.fixture(autouse=True)
def _fresh(monkeypatch):
    lease._reset_for_tests()
    monkeypatch.setattr(runtime, "published_env", lambda: {"DISPLAY": ":37"})
    yield
    lease._reset_for_tests()


def _wire(monkeypatch, commands):
    from tools import browser_tool as browser
    from tools import browser_tool_session as session

    monkeypatch.delenv("AGENT_BROWSER_PROFILE", raising=False)
    monkeypatch.setattr(browser, "_is_camofox_mode", lambda: False)
    monkeypatch.setattr(browser, "_blocked_private_page_action", lambda *a: None)
    monkeypatch.setattr(session, "_browser_command_preflight", lambda: {"browser_cmd": "agent-browser"})
    monkeypatch.setattr(session, "_get_session_info", lambda *a: {"session_name": "review", "cdp_url": None, "features": {"local": True}})
    monkeypatch.setattr(session._cloud, "_get_browser_engine", lambda: "chrome")
    monkeypatch.setattr(session._cloud, "_is_headed_mode", lambda: True)

    def spawn(*args):
        commands.append(args[2])
        return {"success": True, "data": {"secret": "WHAT-THE-HUMAN-TYPED"}}

    monkeypatch.setattr(session, "_spawn_and_collect", spawn)
    return browser, session


def test_browser_click_is_fenced_while_human_controls_shared_browser(monkeypatch):
    commands: list = []
    browser, _ = _wire(monkeypatch, commands)
    lease.acquire("human-viewer")
    result = json.loads(browser.browser_click("e1", task_id="review"))
    assert commands == [], f"human holds the lease, yet a browser command was dispatched: {commands}"
    assert result.get("code") == "human_has_control"


def test_browser_result_crossing_a_takeover_is_discarded(monkeypatch):
    commands: list = []
    browser, session = _wire(monkeypatch, commands)

    def spawn_then_takeover(*args):
        lease.acquire("human-viewer")
        lease.release("human-viewer")  # a full cycle, control is back — the frame is still theirs
        return {"success": True, "data": {"secret": "WHAT-THE-HUMAN-TYPED"}}

    monkeypatch.setattr(session, "_spawn_and_collect", spawn_then_takeover)
    result = browser.browser_click("e1", task_id="review")
    assert "WHAT-THE-HUMAN-TYPED" not in result


def test_real_profile_local_browser_is_fenced_by_provenance_even_without_a_live_display(monkeypatch):
    """A real-profile session attaches over a loopback cdp_url but is launched with the Bot Desktop
    DISPLAY, so it IS the human's browser: the fence keys on the ``local`` feature, not on the
    transport. And a stranded human lease with the screen already down must still fence (computer_use
    does), not silently unfence the browser."""
    commands: list = []
    browser, session = _wire(monkeypatch, commands)
    monkeypatch.setattr(session, "_get_session_info", lambda *a: {
        "session_name": "rp_1", "cdp_url": "ws://127.0.0.1:9222/devtools/browser/x",
        "features": {"local": True, "real_profile": True}})
    monkeypatch.setattr(runtime, "published_env", lambda: {})
    lease.acquire("human-viewer")
    result = json.loads(browser.browser_click("e1", task_id="review"))
    assert commands == [], f"human holds the lease, yet a real-profile browser command was dispatched: {commands}"
    assert result.get("code") == "human_has_control"


def _session_state():
    from tools import browser_tool as bt
    return bt, {
        name: getattr(bt, name).copy()
        for name in (
            "_active_sessions", "_session_last_activity",
            "_session_owner_homes", "_cleanup_failures", "_suspect_browser_sessions",
        )
    }


def _restore_session_state(bt, saved):
    for name, snapshot in saved.items():
        live = getattr(bt, name)
        live.clear()
        live.update(snapshot)


def test_inactivity_janitor_does_not_kill_shared_browser_while_human_holds(monkeypatch):
    """Default inactivity is 120s. wait_for_human + a 2FA login routinely exceed that.
    close is already fenced; the janitor still tree-killed the daemon afterwards."""
    from tools import browser_tool_lifecycle as life

    bt, saved = _session_state()
    orig_timeout = bt.BROWSER_SESSION_INACTIVITY_TIMEOUT
    bt.BROWSER_SESSION_INACTIVITY_TIMEOUT = 0
    released: list = []
    closed: list = []
    monkeypatch.setattr(life, "_release_session_resources", lambda *a, **k: released.append(a))
    monkeypatch.setattr(
        "tools.browser_tool_session._run_browser_command",
        lambda *a, **k: closed.append(a) or {"success": True},
    )
    try:
        for name in saved:
            getattr(bt, name).clear()
        existing = {"session_name": "h_review", "bb_session_id": None, "features": {"local": True}}
        bt._active_sessions["review"] = existing
        bt._session_last_activity["review"] = 0.0
        lease.acquire("human-viewer")
        life._cleanup_inactive_browser_sessions()
        assert released == []
        assert closed == []
        assert bt._active_sessions["review"] is existing
        assert "review" in bt._session_last_activity

        lease.release("human-viewer")
        life._cleanup_inactive_browser_sessions()
        assert released == [("review", existing)]
    finally:
        bt.BROWSER_SESSION_INACTIVITY_TIMEOUT = orig_timeout
        _restore_session_state(bt, saved)


def test_get_session_info_does_not_recycle_shared_browser_while_human_holds(monkeypatch):
    """browser_navigate calls _get_session_info before the command fence. An expired
    or suspect session used to teardown Chromium first, then refuse the click."""
    from tools import browser_tool_session as session

    bt, saved = _session_state()
    cleaned: list = []
    monkeypatch.setattr(
        session._lifecycle, "_cleanup_single_browser_session",
        lambda task_id, **k: cleaned.append(task_id),
    )
    monkeypatch.setattr(session._lifecycle, "_session_has_expired", lambda _s: True)
    monkeypatch.setattr(
        session, "_create_session_for_key",
        lambda *a, **k: {"session_name": "fresh", "features": {"local": True}},
    )
    try:
        for name in saved:
            getattr(bt, name).clear()
        existing = {"session_name": "h_review", "features": {"local": True}, "session_key": "review"}
        bt._active_sessions["review"] = existing
        bt._suspect_browser_sessions["review"] = "timeout"
        lease.acquire("human-viewer")
        got = session._get_session_info("review")
        assert got is existing
        assert cleaned == []
        assert bt._suspect_browser_sessions.get("review") == "timeout"

        lease.release("human-viewer")
        session._get_session_info("review")
        assert cleaned == ["review"]
    finally:
        _restore_session_state(bt, saved)


def test_cloud_browser_session_is_not_reserved_by_a_human_lease():
    """A remote cloud session is another browser; the janitor must still reap it."""
    from tools import browser_tool_session as session

    lease.acquire("human-viewer")
    assert session._local_browser_reserved_by_human(
        {"session_name": "bb", "bb_session_id": "x", "features": {"local": False}}
    ) is False
    assert session._local_browser_reserved_by_human(
        {"session_name": "h_review", "features": {"local": True}}
    ) is True
