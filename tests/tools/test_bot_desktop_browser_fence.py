"""The bot's browser tools obey the screen lease: while a human holds the shared browser, nothing is
dispatched, and a command whose run crossed a takeover loses its result."""

from __future__ import annotations

import json

import pytest

from tools.bot_desktop import lease, runtime


@pytest.fixture(autouse=True)
def _fresh(monkeypatch):
    from tools.browser_tool_session import _reset_dock_port_memory_for_tests
    lease._reset_for_tests()
    _reset_dock_port_memory_for_tests()
    monkeypatch.setattr(runtime, "published_env", lambda: {"DISPLAY": ":37"})
    yield
    lease._reset_for_tests()
    _reset_dock_port_memory_for_tests()


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
            "_recording_sessions",
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


def test_get_session_info_does_not_mint_shared_browser_while_human_holds(monkeypatch):
    """No leftover row: create used to launch / join the dock jar, then the command fence refused."""
    from tools import browser_tool_session as session
    from tools.bot_desktop.lease import HumanHasControl

    bt, saved = _session_state()
    launched = []
    monkeypatch.setattr(session._lifecycle, "_start_browser_cleanup_thread", lambda: None)
    monkeypatch.setattr(session._cdp, "_get_cdp_override_raw", lambda: "")
    monkeypatch.setattr(session._cdp, "_get_cdp_override", lambda: launched.append("http") or "")
    monkeypatch.setattr(session, "_create_local_session", lambda *a, **k: launched.append("local") or {
        "session_name": "h_new", "features": {"local": True}})
    monkeypatch.setattr(session, "_create_cdp_session", lambda *a, **k: launched.append("cdp") or {
        "session_name": "cdp_new", "features": {"cdp_override": True}})
    monkeypatch.setattr(session._cloud, "_get_cloud_provider", lambda: None)
    monkeypatch.setattr(session, "_browser_command_preflight", lambda: {"browser_cmd": "agent-browser"})
    try:
        for name in saved:
            getattr(bt, name).clear()
        lease.acquire("human-viewer")
        with pytest.raises(HumanHasControl):
            session._get_session_info("review")
        assert launched == []
        assert "review" not in bt._active_sessions
        assert "review" not in bt._session_last_activity

        result = session._run_browser_command("review", "click", ["e1"])
        assert result.get("code") == "human_has_control"
        assert launched == []
        assert "review" not in bt._active_sessions
    finally:
        _restore_session_state(bt, saved)


def test_get_session_info_does_not_probe_dock_cdp_while_human_holds(monkeypatch):
    """``_get_cdp_override`` HTTP-discovers /json/version on the dock jar. Peek raw, then refuse."""
    import tools.bot_desktop.browser as bdb
    from tools import browser_tool_session as session
    from tools.bot_desktop.lease import HumanHasControl

    bt, saved = _session_state()
    probed = []
    monkeypatch.setattr(session._lifecycle, "_start_browser_cleanup_thread", lambda: None)
    monkeypatch.setattr(bdb, "running_instance_cdp_port", lambda *a, **k: 9333)
    monkeypatch.setattr(session._cdp, "_get_cdp_override_raw", lambda: "http://127.0.0.1:9333")
    monkeypatch.setattr(session._cdp, "_get_cdp_override", lambda: probed.append("http") or "http://127.0.0.1:9333")
    monkeypatch.setattr(session, "_create_cdp_session", lambda *a, **k: probed.append("cdp") or {
        "session_name": "cdp_x", "cdp_url": a[1], "features": {"cdp_override": True}})
    monkeypatch.setattr(session, "_create_local_session", lambda *a, **k: probed.append("local") or {
        "session_name": "h_x", "features": {"local": True}})
    try:
        for name in saved:
            getattr(bt, name).clear()
        lease.acquire("human-viewer")
        with pytest.raises(HumanHasControl):
            session._get_session_info("review")
        assert probed == []
        assert "review" not in bt._active_sessions

        with pytest.raises(HumanHasControl):
            session._get_session_info("review::local")
        assert probed == []
        assert "review::local" not in bt._active_sessions
    finally:
        _restore_session_state(bt, saved)


def test_get_session_info_still_mints_cloud_session_while_human_holds(monkeypatch):
    """A remote cloud browser is not the dock jar; takeover must not block minting it."""
    from tools import browser_tool_session as session

    bt, saved = _session_state()

    class Cloud:
        def create_session(self, task_id):
            return {"session_name": "cloud_1", "cdp_url": "wss://cloud.example/cdp",
                    "features": {"cloud": True}}

    monkeypatch.setattr(session._lifecycle, "_start_browser_cleanup_thread", lambda: None)
    monkeypatch.setattr(session._cdp, "_get_cdp_override_raw", lambda: "")
    monkeypatch.setattr(session._cdp, "_get_cdp_override", lambda: "")
    monkeypatch.setattr(session._cloud, "_get_cloud_provider", lambda: Cloud())
    monkeypatch.setattr(session._cdp, "_ensure_cdp_supervisor", lambda *a, **k: None)
    monkeypatch.setattr(session._cdp, "_resolve_cdp_override", lambda url: url)
    try:
        for name in saved:
            getattr(bt, name).clear()
        lease.acquire("human-viewer")
        got = session._get_session_info("review")
        assert got["session_name"] == "cloud_1"
        assert bt._active_sessions["review"]["session_name"] == "cloud_1"
    finally:
        _restore_session_state(bt, saved)


def test_cloud_fallback_does_not_wrap_human_has_control(monkeypatch):
    """A failed cloud mint must not wrap the local-path refuse into a generic RuntimeError."""
    from tools import browser_tool_session as session
    from tools.bot_desktop.lease import HumanHasControl

    bt, saved = _session_state()

    class Cloud:
        def create_session(self, task_id):
            raise RuntimeError("cloud down")

    monkeypatch.setattr(session._lifecycle, "_start_browser_cleanup_thread", lambda: None)
    monkeypatch.setattr(session._cdp, "_get_cdp_override_raw", lambda: "")
    monkeypatch.setattr(session._cdp, "_get_cdp_override", lambda: "")
    monkeypatch.setattr(session._cloud, "_get_cloud_provider", lambda: Cloud())
    monkeypatch.setattr(session._real_profile, "_real_profile_cdp", lambda: (_ for _ in ()).throw(
        AssertionError("real-profile launch")))
    try:
        for name in saved:
            getattr(bt, name).clear()
        lease.acquire("human-viewer")
        with pytest.raises(HumanHasControl):
            session._get_session_info("review")
        assert "review" not in bt._active_sessions
    finally:
        _restore_session_state(bt, saved)


def test_browser_navigate_does_not_start_recording_while_human_holds(monkeypatch):
    """Navigate used to mint (and optionally start a WebM) before the command fence."""
    from tools import browser_tool as browser
    from tools.bot_desktop.lease import HumanHasControl

    recorded = []
    monkeypatch.setattr(browser, "_is_camofox_mode", lambda: False)
    monkeypatch.setattr(
        browser._session, "_get_session_info",
        lambda *a, **k: (_ for _ in ()).throw(HumanHasControl("held")),
    )
    monkeypatch.setattr(browser, "_maybe_start_recording", lambda *a, **k: recorded.append(a))
    monkeypatch.setattr(browser, "_url_policy_error", lambda *a, **k: None)
    monkeypatch.setattr(browser, "_secret_url_error_normalized", lambda url: (url, None))
    lease.acquire("human-viewer")
    out = json.loads(browser.browser_navigate("https://example.com", task_id="review"))
    assert out.get("code") == "human_has_control"
    assert recorded == []


def test_record_stop_is_allowed_while_human_holds_so_capture_can_cease(monkeypatch):
    """An opted-in WebM must be stoppable during takeover; close stays refused."""
    commands: list = []
    _, session = _wire(monkeypatch, commands)
    lease.acquire("human-viewer")
    stopped = session._run_browser_command("review", "record", ["stop"])
    assert stopped.get("success") is True
    assert commands, "record stop must reach the daemon while a human holds"
    commands.clear()
    started = session._run_browser_command("review", "record", ["start", "/tmp/x.webm"])
    assert started.get("code") == "human_has_control"
    assert commands == []


def test_request_handoff_stops_local_session_recording(monkeypatch):
    """request_handoff runs in the agent process before Take over; stop the WebM then."""
    from tools import browser_tool as bt
    from tools.computer_use.handoff import handle_handoff

    bt_saved = list(bt._recording_sessions)
    sessions = bt._active_sessions.copy()
    stopped: list = []
    monkeypatch.setattr(bt, "_maybe_stop_recording", lambda tid: stopped.append(tid))
    try:
        bt._recording_sessions.clear()
        bt._recording_sessions.add("review")
        bt._active_sessions["review"] = {"session_name": "h_review", "features": {"local": True}}
        result = json.loads(handle_handoff("request_handoff", {"reason": "Finish 2FA"}))
        assert result["ok"] is True
        assert stopped == ["review"]
    finally:
        bt._recording_sessions.clear()
        bt._recording_sessions.update(bt_saved)
        bt._active_sessions.clear()
        bt._active_sessions.update(sessions)


def test_stop_local_recordings_skips_sibling_profile_sessions(monkeypatch, tmp_path):
    """A multiplex process holds sessions for several homes; takeover on A must not stop B."""
    from tools import browser_tool as bt
    from hermes_constants import hermes_home_key

    home_a = tmp_path / "a"
    home_b = tmp_path / "b"
    home_a.mkdir()
    home_b.mkdir()
    bt_saved = list(bt._recording_sessions)
    sessions = bt._active_sessions.copy()
    owners = bt._session_owner_homes.copy()
    stopped: list = []
    monkeypatch.setattr(bt, "_maybe_stop_recording", lambda tid: stopped.append(tid))
    try:
        bt._recording_sessions.clear()
        bt._recording_sessions.update({"bot-a", "bot-b"})
        bt._active_sessions["bot-a"] = {"session_name": "h_a", "features": {"local": True}}
        bt._active_sessions["bot-b"] = {"session_name": "h_b", "features": {"local": True}}
        bt._session_owner_homes["bot-a"] = str(home_a)
        bt._session_owner_homes["bot-b"] = str(home_b)
        bt.stop_local_browser_recordings(home=hermes_home_key(home_a))
        assert stopped == ["bot-a"]
    finally:
        bt._recording_sessions.clear()
        bt._recording_sessions.update(bt_saved)
        bt._active_sessions.clear()
        bt._active_sessions.update(sessions)
        bt._session_owner_homes.clear()
        bt._session_owner_homes.update(owners)


def test_reserved_recording_stops_while_session_is_still_active(monkeypatch):
    """Desktop Take over must not wait for the 120s inactivity reap to cease a WebM."""
    from tools import browser_tool_lifecycle as life

    bt, saved = _session_state()
    stopped: list = []
    monkeypatch.setattr(bt, "_maybe_stop_recording", lambda tid: stopped.append(tid) or bt._recording_sessions.discard(tid))
    try:
        for name in saved:
            getattr(bt, name).clear()
        bt._recording_sessions.clear()
        existing = {"session_name": "h_review", "features": {"local": True}}
        bt._active_sessions["review"] = existing
        bt._session_last_activity["review"] = 10**12  # far in the future: not idle
        bt._recording_sessions.add("review")
        lease.acquire("human-viewer")
        life._stop_reserved_recordings()
        # Acquire's in-process hook and the janitor scan can both fire; the
        # contract is that the WebM stopped and the Chromium session stayed up.
        assert "review" in stopped
        assert "review" not in bt._recording_sessions
        assert bt._active_sessions["review"] is existing
    finally:
        bt._recording_sessions.clear()
        _restore_session_state(bt, saved)


def test_watch_once_stops_recording_after_cross_process_lease_write(monkeypatch):
    """Desktop Take over writes lease.json from hermes serve. This process
    does not get on_change; the 0.25s supervisor watch must stop the WebM
    without waiting for the janitor's 1s scan.
    """
    from tools import browser_tool_supervisor_lease as sl
    from tools.bot_desktop import lease as bd_lease

    bt, saved = _session_state()
    stopped: list = []
    monkeypatch.setattr(bt, "_maybe_stop_recording", lambda tid: stopped.append(tid) or bt._recording_sessions.discard(tid))
    try:
        for name in saved:
            getattr(bt, name).clear()
        bt._active_sessions["review"] = {"session_name": "h_review", "features": {"local": True}}
        bt._recording_sessions.add("review")
        # Other process: write HUMAN to disk, do not notify this process.
        path = bd_lease._path(None)
        bd_lease._write(path, bd_lease.Lease(holder=bd_lease.HUMAN, viewer_id="other-host", epoch=9))
        sl._watch_once()
        assert "review" in stopped
        assert "review" not in bt._recording_sessions
    finally:
        bt._recording_sessions.clear()
        _restore_session_state(bt, saved)


def test_in_process_acquire_stops_recording_via_lease_hook(monkeypatch):
    """display.lease.acquire writes HUMAN in this process; the WebM must stop then, not on the next tool call."""
    from tools import browser_tool as bt

    bt_saved = list(bt._recording_sessions)
    sessions = bt._active_sessions.copy()
    stopped: list = []
    monkeypatch.setattr(bt, "_maybe_stop_recording", lambda tid: stopped.append(tid) or bt._recording_sessions.discard(tid))
    try:
        bt._recording_sessions.clear()
        bt._recording_sessions.add("review")
        bt._active_sessions["review"] = {"session_name": "h_review", "features": {"local": True}}
        bt._install_recording_lease_hook()
        lease.acquire("human-viewer")
        assert stopped == ["review"]
    finally:
        bt._recording_sessions.clear()
        bt._recording_sessions.update(bt_saved)
        bt._active_sessions.clear()
        bt._active_sessions.update(sessions)


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


def test_cdp_override_to_dock_browser_is_fenced(monkeypatch):
    """``/browser connect`` to the dock Chromium is the same jar; the lease must hold."""
    import tools.bot_desktop.browser as bdb

    commands: list = []
    browser, session = _wire(monkeypatch, commands)
    monkeypatch.setattr(session, "_get_session_info", lambda *a: {
        "session_name": "cdp_1", "cdp_url": "http://127.0.0.1:9333",
        "features": {"cdp_override": True}})
    monkeypatch.setattr(bdb, "running_instance_cdp_port", lambda *a, **k: 9333)
    lease.acquire("human-viewer")
    result = json.loads(browser.browser_click("e1", task_id="review"))
    assert commands == [], f"dock CDP override was dispatched while a human held: {commands}"
    assert result.get("code") == "human_has_control"


def test_cdp_override_to_unrelated_browser_is_not_the_dock_jar(monkeypatch):
    """A user-supplied CDP to some other Chrome is not reserved by this profile's lease."""
    import tools.bot_desktop.browser as bdb
    from tools import browser_tool_session as session

    monkeypatch.setattr(bdb, "running_instance_cdp_port", lambda *a, **k: 9333)
    lease.acquire("human-viewer")
    other = {"session_name": "cdp_other", "cdp_url": "http://127.0.0.1:9444",
             "features": {"cdp_override": True}}
    assert session._cdp_url_is_bot_desktop_browser(other["cdp_url"]) is False
    assert session._local_browser_reserved_by_human(other) is False
    remote = {"session_name": "cdp_remote", "cdp_url": "wss://browser.example/cdp",
              "features": {"cdp_override": True}}
    assert session._shares_bot_desktop_browser(remote) is False


def test_eval_supervisor_fast_path_is_fenced_while_human_holds(monkeypatch):
    """Runtime.evaluate on the live CDP socket skipped _run_browser_command entirely."""
    from unittest.mock import MagicMock
    from tools import browser_tool as bt
    from tools.bot_desktop import lease

    commands: list = []
    _wire(monkeypatch, commands)
    monkeypatch.setattr(bt, "_is_camofox_mode", lambda: False)
    monkeypatch.setattr(bt, "_last_session_key", lambda task_id: "review")
    saved = bt._active_sessions.copy()
    try:
        bt._active_sessions["review"] = {
            "session_name": "h_review", "features": {"local": True},
        }
        sup = MagicMock()
        sup.evaluate_runtime.return_value = {"ok": True, "result": "SECRET-FROM-PAGE"}
        import tools.browser_supervisor as bs
        registry = MagicMock()
        registry.get.return_value = sup
        monkeypatch.setattr(bs, "SUPERVISOR_REGISTRY", registry)
        lease.acquire("human-viewer")
        out = json.loads(bt._browser_eval("document.body.innerText", task_id="review"))
        assert out.get("code") == "human_has_control"
        assert "SECRET-FROM-PAGE" not in json.dumps(out)
        sup.evaluate_runtime.assert_not_called()
    finally:
        bt._active_sessions.clear()
        bt._active_sessions.update(saved)


def test_eval_supervisor_fast_path_fenced_via_cdp_override_without_session(monkeypatch):
    """A leftover supervisor on the dock jar must not skip the lease just because no session row exists."""
    from unittest.mock import MagicMock
    import tools.bot_desktop.browser as bdb
    from tools import browser_tool as bt
    from tools.bot_desktop import lease

    _wire(monkeypatch, [])
    monkeypatch.setattr(bt, "_is_camofox_mode", lambda: False)
    monkeypatch.setattr(bt, "_last_session_key", lambda task_id: "review")
    monkeypatch.setattr(bdb, "running_instance_cdp_port", lambda *a, **k: 9333)
    monkeypatch.setattr("tools.browser_tool_cdp._get_cdp_override_raw", lambda: "http://127.0.0.1:9333")
    saved = bt._active_sessions.copy()
    try:
        bt._active_sessions.pop("review", None)
        sup = MagicMock()
        sup.evaluate_runtime.return_value = {"ok": True, "result": "SECRET-FROM-PAGE"}
        import tools.browser_supervisor as bs
        registry = MagicMock()
        registry.get.return_value = sup
        monkeypatch.setattr(bs, "SUPERVISOR_REGISTRY", registry)
        lease.acquire("human-viewer")
        out = json.loads(bt._browser_eval("document.body.innerText", task_id="review"))
        assert out.get("code") == "human_has_control"
        assert "SECRET-FROM-PAGE" not in json.dumps(out)
        sup.evaluate_runtime.assert_not_called()
    finally:
        bt._active_sessions.clear()
        bt._active_sessions.update(saved)


def test_eval_supervisor_fast_path_fenced_via_leftover_supervisor_cdp(monkeypatch):
    """Session row gone, no /browser connect — the supervisor's own cdp_url still names the dock jar."""
    from unittest.mock import MagicMock
    import tools.bot_desktop.browser as bdb
    from tools import browser_tool as bt
    from tools.bot_desktop import lease

    _wire(monkeypatch, [])
    monkeypatch.setattr(bt, "_is_camofox_mode", lambda: False)
    monkeypatch.setattr(bt, "_last_session_key", lambda task_id: "review")
    monkeypatch.setattr(bdb, "running_instance_cdp_port", lambda *a, **k: 9333)
    monkeypatch.setattr("tools.browser_tool_cdp._get_cdp_override_raw", lambda: "")
    saved = bt._active_sessions.copy()
    try:
        bt._active_sessions.pop("review", None)
        sup = MagicMock()
        sup.cdp_url = "ws://127.0.0.1:9333/devtools/browser/x"
        sup.evaluate_runtime.return_value = {"ok": True, "result": "SECRET-FROM-PAGE"}
        import tools.browser_supervisor as bs
        registry = MagicMock()
        registry.get.return_value = sup
        monkeypatch.setattr(bs, "SUPERVISOR_REGISTRY", registry)
        lease.acquire("human-viewer")
        out = json.loads(bt._browser_eval("document.body.innerText", task_id="review"))
        assert out.get("code") == "human_has_control"
        assert "SECRET-FROM-PAGE" not in json.dumps(out)
        sup.evaluate_runtime.assert_not_called()
    finally:
        bt._active_sessions.clear()
        bt._active_sessions.update(saved)


def test_eval_supervisor_fast_path_discards_result_after_takeover(monkeypatch):
    from unittest.mock import MagicMock
    from tools import browser_tool as bt
    from tools.bot_desktop import lease

    _wire(monkeypatch, [])
    monkeypatch.setattr(bt, "_is_camofox_mode", lambda: False)
    monkeypatch.setattr(bt, "_last_session_key", lambda task_id: "review")
    saved = bt._active_sessions.copy()
    try:
        bt._active_sessions["review"] = {
            "session_name": "h_review", "features": {"local": True},
        }
        sup = MagicMock()

        def _eval(_expr):
            lease.acquire("human-viewer")
            lease.release("human-viewer")
            return {"ok": True, "result": "SECRET-FROM-PAGE"}

        sup.evaluate_runtime.side_effect = _eval
        import tools.browser_supervisor as bs
        registry = MagicMock()
        registry.get.return_value = sup
        monkeypatch.setattr(bs, "SUPERVISOR_REGISTRY", registry)
        out = json.loads(bt._browser_eval("document.body.innerText", task_id="review"))
        assert out.get("code") == "human_has_control"
        assert "SECRET-FROM-PAGE" not in json.dumps(out)
    finally:
        bt._active_sessions.clear()
        bt._active_sessions.update(saved)


def test_chrome_fallback_is_refused_while_human_holds(monkeypatch):
    """Lightpanda's Chrome retry bypasses _run_browser_command and used to spawn against the dock jar."""
    from tools import browser_tool as bt
    from tools import browser_tool_lightpanda_fallback as lp
    from tools.bot_desktop import lease

    launched = []
    monkeypatch.setattr(lp._session, "_run_browser_command", lambda *a, **k: {
        "success": True, "data": {"url": "https://example.com/login"}})
    monkeypatch.setattr(lp._install, "_find_agent_browser", lambda: launched.append("find") or "/bin/agent-browser")
    monkeypatch.setattr(lp._install, "_chromium_installed", lambda: True)
    monkeypatch.setattr(lp._session, "_popen_agent_browser", lambda *a, **k: launched.append(a) or (_ for _ in ()).throw(
        AssertionError("fallback chrome spawned")))
    saved = bt._active_sessions.copy()
    try:
        bt._active_sessions["review"] = {"session_name": "lp_1", "features": {"local": True, "lightpanda": True}}
        lease.acquire("human-viewer")
        out = lp._run_chrome_fallback_command("review", "screenshot", [], 5)
        assert out.get("code") == "human_has_control"
        assert launched == []
    finally:
        bt._active_sessions.clear()
        bt._active_sessions.update(saved)


def test_chrome_fallback_uses_throwaway_profile_not_the_dock_jar(monkeypatch, tmp_path):
    """A temp Chrome that inherits AGENT_BROWSER_PROFILE joins the dock singleton and can navigate it."""
    from tools import browser_tool_lightpanda_fallback as lp
    from tools.bot_desktop import browser as bdb

    dock = str(tmp_path / "bot-desktop" / "browser-profile")
    captured: list[str] = []

    class _Proc:
        def wait(self, timeout=None):
            return 0

        def kill(self):
            return None

    monkeypatch.setattr(lp._session, "_run_browser_command", lambda *a, **k: {
        "success": True, "data": {"url": "https://example.com"}})
    monkeypatch.setattr(lp._install, "_find_agent_browser", lambda: "/bin/agent-browser")
    monkeypatch.setattr(lp._install, "_chromium_installed", lambda: True)
    monkeypatch.setattr(lp._session, "_prepare_session_socket_dir", lambda _n: str(tmp_path / "sock"))
    monkeypatch.setattr(lp._session, "_agent_browser_argv", lambda _cmd: ["agent-browser"])
    monkeypatch.setattr(lp._session, "_agent_browser_command_env", lambda _d: {
        "AGENT_BROWSER_PROFILE": dock})
    monkeypatch.setattr(lp._session, "_apply_chromium_sandbox_args", lambda _e: None)
    monkeypatch.setattr(lp._session, "_unlink_command_output_files", lambda *a: None)
    monkeypatch.setattr(bdb, "profile_dir", lambda: tmp_path / "bot-desktop" / "browser-profile")

    def _popen(_argv, env, _sock, _tag):
        captured.append(env.get("AGENT_BROWSER_PROFILE", ""))
        stdout = tmp_path / "sock" / f"_stdout_{_tag}"
        stdout.parent.mkdir(parents=True, exist_ok=True)
        stdout.write_text('{"success": true, "data": {}}\n', encoding="utf-8")
        return _Proc()

    monkeypatch.setattr(lp._session, "_popen_agent_browser", _popen)
    out = lp._run_chrome_fallback_command("review", "screenshot", [], 5)
    assert out.get("success") is True
    assert captured, "fallback never spawned Chrome"
    assert all(p != dock and p.endswith("chrome-profile") for p in captured), captured


def test_lightpanda_vision_preroute_does_not_persist_while_human_holds(monkeypatch, tmp_path):
    """Chrome fallback for Lightpanda vision must not copy a PNG after Take over."""
    from tools import browser_tool_vision as vision
    from tools.bot_desktop import lease
    from tools import browser_tool as bt

    png = tmp_path / "shot.png"
    png.write_bytes(b"PNG")
    monkeypatch.setattr(vision._cloud, "_get_browser_engine", lambda: "lightpanda")
    monkeypatch.setattr(vision._cloud, "_should_inject_engine", lambda _e: True)
    monkeypatch.setattr(
        vision._lp, "_chrome_fallback_screenshot",
        lambda *a, **k: {"success": True, "data": {"path": str(png)}},
    )
    dest = tmp_path / "out.png"
    saved = bt._active_sessions.copy()
    try:
        bt._active_sessions["review"] = {"session_name": "lp_1", "features": {"local": True, "lightpanda": True}}
        lease.acquire("human-viewer")
        prerouted, _warn, path = vision._lightpanda_vision_preroute("review", False, dest)
        assert prerouted is False
        assert path == dest
        assert not dest.exists()
    finally:
        bt._active_sessions.clear()
        bt._active_sessions.update(saved)


def test_browser_cdp_discards_result_after_takeover(monkeypatch):
    import tools.bot_desktop.browser as bdb
    from tools.bot_desktop import lease
    from tools import browser_cdp_tool as cdp

    monkeypatch.setattr(bdb, "running_instance_cdp_port", lambda *a, **k: 9333)
    monkeypatch.setattr("tools.browser_tool_cdp._get_cdp_override_raw", lambda: "http://127.0.0.1:9333")
    monkeypatch.setattr(cdp, "_resolve_cdp_endpoint", lambda: "ws://127.0.0.1:9333/devtools/browser/x")
    monkeypatch.setattr(cdp, "_WS_AVAILABLE", True)
    monkeypatch.setattr(cdp, "_browser_cdp_private_guard", lambda **k: None)

    def _call(*_a, **_k):
        lease.acquire("human-viewer")
        lease.release("human-viewer")
        return {"data": "SECRET"}

    monkeypatch.setattr(cdp, "_run_async", _call)
    monkeypatch.setattr(cdp, "_cdp_call", lambda *a, **k: None)
    monkeypatch.setattr(runtime, "published_env", lambda: {"DISPLAY": ":37"})
    out = json.loads(cdp.browser_cdp("Runtime.evaluate", {"expression": "1"}))
    assert out.get("code") == "human_has_control"
    assert "SECRET" not in json.dumps(out)


def test_browser_cdp_does_not_resolve_dock_endpoint_while_human_holds(monkeypatch):
    """Admit before HTTP /json/version — discovery itself reads the human's jar."""
    import tools.bot_desktop.browser as bdb
    from tools.bot_desktop import lease
    from tools import browser_cdp_tool as cdp

    probed = []
    monkeypatch.setattr(bdb, "running_instance_cdp_port", lambda *a, **k: 9333)
    monkeypatch.setattr("tools.browser_tool_cdp._get_cdp_override_raw", lambda: "http://127.0.0.1:9333")
    monkeypatch.setattr(cdp, "_resolve_cdp_endpoint", lambda: probed.append("http") or "ws://127.0.0.1:9333/devtools/browser/x")
    monkeypatch.setattr(cdp, "_WS_AVAILABLE", True)
    monkeypatch.setattr(cdp, "_run_async", lambda *_a, **_k: {"data": "SECRET"})
    lease.acquire("human-viewer")
    out = json.loads(cdp.browser_cdp("Target.getTargets", {}))
    assert out.get("code") == "human_has_control"
    assert probed == []
    assert "SECRET" not in json.dumps(out)


def test_browser_cdp_does_not_send_to_dock_after_takeover_during_resolve(monkeypatch):
    """Take over during /json/version must not then Target.attach / Runtime.evaluate."""
    import tools.bot_desktop.browser as bdb
    from tools.bot_desktop import lease
    from tools import browser_cdp_tool as cdp

    sent = []
    monkeypatch.setattr(bdb, "running_instance_cdp_port", lambda *a, **k: 9333)
    monkeypatch.setattr("tools.browser_tool_cdp._get_cdp_override_raw", lambda: "http://127.0.0.1:9333")

    def _resolve():
        lease.acquire("human-viewer")
        return "ws://127.0.0.1:9333/devtools/browser/x"

    monkeypatch.setattr(cdp, "_resolve_cdp_endpoint", _resolve)
    monkeypatch.setattr(cdp, "_WS_AVAILABLE", True)
    monkeypatch.setattr(cdp, "_browser_cdp_private_guard", lambda **k: None)
    monkeypatch.setattr(cdp, "_run_async", lambda *_a, **_k: sent.append("send") or {"data": "SECRET"})
    out = json.loads(cdp.browser_cdp("Runtime.evaluate", {"expression": "1"}))
    assert out.get("code") == "human_has_control"
    assert sent == []
    assert "SECRET" not in json.dumps(out)


def test_browser_cdp_other_chrome_still_sends_after_takeover_during_resolve(monkeypatch):
    """A resolved unrelated Chrome is not muted because this profile's lease moved."""
    import tools.bot_desktop.browser as bdb
    from tools.bot_desktop import lease
    from tools import browser_cdp_tool as cdp

    sent = []
    monkeypatch.setattr(bdb, "running_instance_cdp_port", lambda *a, **k: 9333)
    monkeypatch.setattr("tools.browser_tool_cdp._get_cdp_override_raw", lambda: "http://127.0.0.1:9333")

    def _resolve():
        lease.acquire("human-viewer")
        return "ws://127.0.0.1:9222/devtools/browser/x"

    monkeypatch.setattr(cdp, "_resolve_cdp_endpoint", _resolve)
    monkeypatch.setattr(cdp, "_WS_AVAILABLE", True)
    monkeypatch.setattr(cdp, "_browser_cdp_private_guard", lambda **k: None)
    monkeypatch.setattr(cdp, "_run_async", lambda *_a, **_k: sent.append("send") or {"data": "OTHER"})
    out = json.loads(cdp.browser_cdp("Runtime.evaluate", {"expression": "1"}))
    assert sent == ["send"]
    # Start-of-call admit was the dock; discard after, but do not skip the other Chrome's send.
    assert out.get("code") == "human_has_control"
    assert "OTHER" not in json.dumps(out)


def test_browser_cdp_ws_send_skips_after_takeover_during_connect(monkeypatch):
    """ws.send must not run if Take over happens after connect, before the first CDP write."""
    import tools.bot_desktop.browser as bdb
    from tools.bot_desktop.lease import HumanHasControl
    from tools.browser_tool_session import _admit_shared_browser
    from tools import browser_cdp_tool as cdp

    monkeypatch.setattr(bdb, "running_instance_cdp_port", lambda *a, **k: 9333)
    endpoint = "ws://127.0.0.1:9333/devtools/browser/x"
    admitted = _admit_shared_browser(cdp_url=endpoint)
    assert admitted is not None

    class FakeWS:
        def __init__(self):
            self.sent = []

        async def __aenter__(self):
            from tools.bot_desktop import lease as _lease
            _lease.acquire("human-viewer")
            return self

        async def __aexit__(self, *_a):
            return False

        async def send(self, raw):
            self.sent.append(raw)

        async def recv(self):
            raise AssertionError("recv should not run after a refused send")

    fake = FakeWS()
    monkeypatch.setattr(cdp, "websockets", type("WS", (), {"connect": staticmethod(lambda *_a, **_k: fake)})())
    with pytest.raises(HumanHasControl):
        cdp._run_async(cdp._cdp_call(
            endpoint, "Runtime.evaluate", {"expression": "1"}, None, 5.0,
            endpoint_admitted=admitted,
        ))
    assert fake.sent == []


def test_remembered_dock_port_still_fences_when_devtools_file_is_gone(monkeypatch):
    """A live DevTools miss must not treat the port we already saw as another Chrome."""
    import tools.bot_desktop.browser as bdb
    from tools.bot_desktop.lease import HumanHasControl
    from tools.browser_tool_session import (
        _admit_shared_browser,
        _cdp_url_is_bot_desktop_browser,
        _refuse_shared_session_while_human_holds,
    )

    dock = "ws://127.0.0.1:9333/devtools/browser/x"
    other = "ws://127.0.0.1:9222/devtools/browser/x"
    monkeypatch.setattr(bdb, "running_instance_cdp_port", lambda *a, **k: 9333)
    assert _cdp_url_is_bot_desktop_browser(dock) is True
    monkeypatch.setattr(bdb, "running_instance_cdp_port", lambda *a, **k: None)
    lease.acquire("human-viewer")
    assert _cdp_url_is_bot_desktop_browser(dock) is True
    assert _cdp_url_is_bot_desktop_browser(other) is False
    with pytest.raises(HumanHasControl):
        _admit_shared_browser(cdp_url=dock)
    with pytest.raises(HumanHasControl):
        _refuse_shared_session_while_human_holds(cdp_url="http://127.0.0.1:9333")
    assert _admit_shared_browser(cdp_url=other) is None


def test_persisted_dock_port_fences_a_cold_process_after_devtools_miss(monkeypatch):
    """In-memory last-known dies with the process; the on-disk port must still fence."""
    import tools.bot_desktop.browser as bdb
    from tools.bot_desktop.lease import HumanHasControl
    from tools.browser_tool_session import (
        _admit_shared_browser,
        _cdp_url_is_bot_desktop_browser,
        _last_dock_cdp_port,
    )

    dock = "ws://127.0.0.1:9333/devtools/browser/x"
    other = "ws://127.0.0.1:9222/devtools/browser/x"
    monkeypatch.setattr(bdb, "running_instance_cdp_port", lambda *a, **k: 9333)
    assert _cdp_url_is_bot_desktop_browser(dock) is True
    _last_dock_cdp_port.clear()
    monkeypatch.setattr(bdb, "running_instance_cdp_port", lambda *a, **k: None)
    lease.acquire("human-viewer")
    assert _cdp_url_is_bot_desktop_browser(dock) is True
    assert _cdp_url_is_bot_desktop_browser(other) is False
    with pytest.raises(HumanHasControl):
        _admit_shared_browser(cdp_url=dock)
    assert _admit_shared_browser(cdp_url=other) is None


def test_persisted_dock_port_does_not_leak_to_a_sibling_profile(monkeypatch, tmp_path):
    """A dock port stamped under one HERMES_HOME must not fence another bot's Chrome."""
    from hermes_constants import reset_hermes_home_override, set_hermes_home_override
    import tools.bot_desktop.browser as bdb
    from tools.browser_tool_session import (
        _cdp_url_is_bot_desktop_browser,
        _last_dock_cdp_port,
    )

    home_a = tmp_path / "a"
    home_b = tmp_path / "b"
    home_a.mkdir()
    home_b.mkdir()
    dock = "ws://127.0.0.1:9333/devtools/browser/x"
    token_a = set_hermes_home_override(home_a)
    try:
        monkeypatch.setattr(bdb, "running_instance_cdp_port", lambda *a, **k: 9333)
        assert _cdp_url_is_bot_desktop_browser(dock) is True
        _last_dock_cdp_port.clear()
        monkeypatch.setattr(bdb, "running_instance_cdp_port", lambda *a, **k: None)
        assert _cdp_url_is_bot_desktop_browser(dock) is True
    finally:
        reset_hermes_home_override(token_a)
    _last_dock_cdp_port.clear()
    token_b = set_hermes_home_override(home_b)
    try:
        monkeypatch.setattr(bdb, "running_instance_cdp_port", lambda *a, **k: None)
        assert _cdp_url_is_bot_desktop_browser(dock) is False
    finally:
        reset_hermes_home_override(token_b)


def test_acquire_persists_dock_port_before_any_agent_probe(monkeypatch):
    """Human-first Take over used to leave ``dock-cdp-port`` missing. A later
    DevTools miss then treated the jar as another Chrome (admit None)."""
    import tools.bot_desktop.browser as bdb
    from tools.bot_desktop.lease import HumanHasControl
    from tools.browser_tool_session import (
        _admit_resolved_cdp_for_attach,
        _admit_shared_browser,
        _cdp_url_is_bot_desktop_browser,
        _last_dock_cdp_port,
    )

    dock = "ws://127.0.0.1:9333/devtools/browser/x"
    other = "ws://127.0.0.1:9222/devtools/browser/x"
    monkeypatch.setattr(bdb, "running_instance_cdp_port", lambda *a, **k: 9333)
    _last_dock_cdp_port.clear()
    assert bdb.last_known_dock_cdp_port() is None

    lease.acquire("human-viewer")
    assert bdb.last_known_dock_cdp_port() == 9333

    _last_dock_cdp_port.clear()
    monkeypatch.setattr(bdb, "running_instance_cdp_port", lambda *a, **k: None)
    assert _cdp_url_is_bot_desktop_browser(dock) is True
    assert _cdp_url_is_bot_desktop_browser(other) is False
    with pytest.raises(HumanHasControl):
        _admit_shared_browser(cdp_url=dock)
    assert _admit_resolved_cdp_for_attach(dock) is False
    assert _admit_shared_browser(cdp_url=other) is None
    assert _admit_resolved_cdp_for_attach(other) is True


def test_watch_once_persists_dock_port_before_devtools_miss(monkeypatch):
    """Leftover watch used to never stamp ``dock-cdp-port`` while DevTools
    was still readable. A miss *before* Take over then treated the jar as
    another Chrome (admit None), even after finding 65's acquire persist
    (acquire sees no live port)."""
    import tools.bot_desktop.browser as bdb
    from tools.bot_desktop.lease import HumanHasControl
    from tools.browser_tool_session import (
        _admit_resolved_cdp_for_attach,
        _admit_shared_browser,
        _cdp_url_is_bot_desktop_browser,
        _last_dock_cdp_port,
    )
    from tools.browser_tool_supervisor_lease import _watch_once

    dock = "ws://127.0.0.1:9333/devtools/browser/x"
    other = "ws://127.0.0.1:9222/devtools/browser/x"
    monkeypatch.setattr(bdb, "running_instance_cdp_port", lambda *a, **k: 9333)
    monkeypatch.setattr(
        "tools.browser_tool_lifecycle._stop_reserved_recordings",
        lambda: None,
    )
    _last_dock_cdp_port.clear()
    assert bdb.last_known_dock_cdp_port() is None

    _watch_once()
    assert bdb.last_known_dock_cdp_port() == 9333

    _last_dock_cdp_port.clear()
    monkeypatch.setattr(bdb, "running_instance_cdp_port", lambda *a, **k: None)
    lease.acquire("human-viewer")
    assert bdb.last_known_dock_cdp_port() == 9333
    assert _cdp_url_is_bot_desktop_browser(dock) is True
    assert _cdp_url_is_bot_desktop_browser(other) is False
    with pytest.raises(HumanHasControl):
        _admit_shared_browser(cdp_url=dock)
    assert _admit_resolved_cdp_for_attach(dock) is False
    assert _admit_shared_browser(cdp_url=other) is None
    assert _admit_resolved_cdp_for_attach(other) is True


def test_set_process_cdp_override_persists_dock_port_before_devtools_miss(monkeypatch):
    """TUI ``/browser connect`` probed the dock over HTTP and published
    ``BROWSER_CDP_URL`` without stamping ``dock-cdp-port``. A later
    DevTools miss then leftover-attached as another Chrome."""
    import tools.bot_desktop.browser as bdb
    from tools.bot_desktop.lease import HumanHasControl
    from tools.browser_tool_cdp import set_process_cdp_override
    from tools.browser_tool_session import (
        _admit_resolved_cdp_for_attach,
        _admit_shared_browser,
        _cdp_url_is_bot_desktop_browser,
        _last_dock_cdp_port,
    )

    dock = "ws://127.0.0.1:9333/devtools/browser/x"
    other = "ws://127.0.0.1:9222/devtools/browser/x"
    monkeypatch.setattr(bdb, "running_instance_cdp_port", lambda *a, **k: 9333)
    _last_dock_cdp_port.clear()
    assert bdb.last_known_dock_cdp_port() is None

    set_process_cdp_override("http://127.0.0.1:9333")
    assert bdb.last_known_dock_cdp_port() == 9333

    _last_dock_cdp_port.clear()
    monkeypatch.setattr(bdb, "running_instance_cdp_port", lambda *a, **k: None)
    lease.acquire("human-viewer")
    assert bdb.last_known_dock_cdp_port() == 9333
    assert _cdp_url_is_bot_desktop_browser(dock) is True
    assert _cdp_url_is_bot_desktop_browser(other) is False
    with pytest.raises(HumanHasControl):
        _admit_shared_browser(cdp_url=dock)
    assert _admit_resolved_cdp_for_attach(dock) is False
    assert _admit_shared_browser(cdp_url=other) is None
    assert _admit_resolved_cdp_for_attach(other) is True


def test_remember_dock_cdp_port_skips_rewrite_when_unchanged():
    """The 0.25s watch re-persists the same port; skip-if-same must not
    block the first write or a later different port."""
    import tools.bot_desktop.browser as bdb

    assert bdb.last_known_dock_cdp_port() is None
    bdb.remember_dock_cdp_port(9333)
    assert bdb.last_known_dock_cdp_port() == 9333
    bdb.remember_dock_cdp_port(9333)
    assert bdb.last_known_dock_cdp_port() == 9333
    bdb.remember_dock_cdp_port(9222)
    assert bdb.last_known_dock_cdp_port() == 9222


def test_acquire_without_live_dock_does_not_invent_a_port(monkeypatch):
    """No live probe at Take over must not stamp a loopback port and mute
    an unrelated Chrome."""
    import tools.bot_desktop.browser as bdb
    from tools.browser_tool_session import (
        _admit_shared_browser,
        _cdp_url_is_bot_desktop_browser,
        _last_dock_cdp_port,
    )

    other = "ws://127.0.0.1:9222/devtools/browser/x"
    monkeypatch.setattr(bdb, "running_instance_cdp_port", lambda *a, **k: None)
    _last_dock_cdp_port.clear()
    lease.acquire("human-viewer")
    assert bdb.last_known_dock_cdp_port() is None
    assert _cdp_url_is_bot_desktop_browser(other) is False
    assert _admit_shared_browser(cdp_url=other) is None


def test_vault_supervisor_attach_does_not_probe_when_admit_raises(monkeypatch):
    """An unexpected admit failure must not fall through to HTTP /json/version."""
    from tools import browser_use_cli as bu

    probed = []
    started = []
    monkeypatch.setattr(
        "tools.browser_tool_session._admit_shared_browser",
        lambda *a, **k: (_ for _ in ()).throw(RuntimeError("lease helper exploded")),
    )
    monkeypatch.setattr(
        "tools.browser_tool_cdp._resolve_cdp_override",
        lambda url: probed.append(url) or url,
    )
    import tools.browser_supervisor as bs
    registry = __import__("unittest.mock", fromlist=["MagicMock"]).MagicMock()
    registry.get_or_start.side_effect = lambda **k: started.append(k)
    monkeypatch.setattr(bs, "SUPERVISOR_REGISTRY", registry)
    bu._attach_vault_supervisor(
        {"BU_CDP_WS": "ws://127.0.0.1:9333/devtools/browser/x"}, "review",
    )
    assert probed == []
    assert started == []


def test_vault_supervisor_attach_does_not_probe_dock_while_human_holds(monkeypatch):
    """browser_exec leftover attach used to HTTP /json/version after admit returned None."""
    import tools.bot_desktop.browser as bdb
    from tools import browser_use_cli as bu

    probed = []
    started = []
    monkeypatch.setattr(bdb, "running_instance_cdp_port", lambda *a, **k: 9333)
    from tools.browser_tool_session import _cdp_url_is_bot_desktop_browser
    assert _cdp_url_is_bot_desktop_browser("ws://127.0.0.1:9333/devtools/browser/x") is True
    monkeypatch.setattr(bdb, "running_instance_cdp_port", lambda *a, **k: None)
    monkeypatch.setattr(
        "tools.browser_tool_cdp._resolve_cdp_override",
        lambda url: probed.append(url) or url,
    )
    import tools.browser_supervisor as bs
    registry = __import__("unittest.mock", fromlist=["MagicMock"]).MagicMock()
    registry.get_or_start.side_effect = lambda **k: started.append(k)
    monkeypatch.setattr(bs, "SUPERVISOR_REGISTRY", registry)
    lease.acquire("human-viewer")
    bu._attach_vault_supervisor(
        {"BU_CDP_WS": "ws://127.0.0.1:9333/devtools/browser/x"}, "review",
    )
    assert probed == []
    assert started == []


def test_vault_supervisor_attach_fences_never_probed_dock_after_acquire(monkeypatch):
    """Take over stamps the live port. A later DevTools miss must not HTTP
    the jar just because no agent call identified it first."""
    import tools.bot_desktop.browser as bdb
    from tools import browser_use_cli as bu
    from tools.browser_tool_session import _last_dock_cdp_port

    probed = []
    started = []
    monkeypatch.setattr(bdb, "running_instance_cdp_port", lambda *a, **k: 9333)
    lease.acquire("human-viewer")
    _last_dock_cdp_port.clear()
    monkeypatch.setattr(bdb, "running_instance_cdp_port", lambda *a, **k: None)
    monkeypatch.setattr(
        "tools.browser_tool_cdp._resolve_cdp_override",
        lambda url: probed.append(url) or url,
    )
    import tools.browser_supervisor as bs
    registry = __import__("unittest.mock", fromlist=["MagicMock"]).MagicMock()
    registry.get_or_start.side_effect = lambda **k: started.append(k)
    monkeypatch.setattr(bs, "SUPERVISOR_REGISTRY", registry)
    bu._attach_vault_supervisor(
        {"BU_CDP_WS": "ws://127.0.0.1:9333/devtools/browser/x"}, "review",
    )
    assert probed == []
    assert started == []


def test_vault_supervisor_attach_does_not_start_after_takeover_during_resolve(monkeypatch):
    """Take over during /json/version must not then get_or_start on the dock."""
    import tools.bot_desktop.browser as bdb
    from tools import browser_use_cli as bu

    started = []
    monkeypatch.setattr(bdb, "running_instance_cdp_port", lambda *a, **k: 9333)

    def _resolve(url):
        lease.acquire("human-viewer")
        return "ws://127.0.0.1:9333/devtools/browser/x"

    monkeypatch.setattr("tools.browser_tool_cdp._resolve_cdp_override", _resolve)
    import tools.browser_supervisor as bs
    registry = __import__("unittest.mock", fromlist=["MagicMock"]).MagicMock()
    registry.get_or_start.side_effect = lambda **k: started.append(k.get("cdp_url"))
    monkeypatch.setattr(bs, "SUPERVISOR_REGISTRY", registry)
    bu._attach_vault_supervisor({"BU_CDP_URL": "http://127.0.0.1:9333"}, "review")
    assert started == []
    assert lease.human_holds() is True


def test_vault_supervisor_attach_other_chrome_still_starts_after_takeover_during_resolve(monkeypatch):
    """A resolved unrelated Chrome is not muted because this profile's lease moved."""
    import tools.bot_desktop.browser as bdb
    from tools import browser_use_cli as bu

    started = []
    monkeypatch.setattr(bdb, "running_instance_cdp_port", lambda *a, **k: 9333)

    def _resolve(url):
        lease.acquire("human-viewer")
        return "ws://127.0.0.1:9222/devtools/browser/x"

    monkeypatch.setattr("tools.browser_tool_cdp._resolve_cdp_override", _resolve)
    import tools.browser_supervisor as bs
    registry = __import__("unittest.mock", fromlist=["MagicMock"]).MagicMock()
    registry.get_or_start.side_effect = lambda **k: started.append(k.get("cdp_url"))
    monkeypatch.setattr(bs, "SUPERVISOR_REGISTRY", registry)
    bu._attach_vault_supervisor({"BU_CDP_URL": "http://127.0.0.1:9222"}, "review")
    assert started == ["ws://127.0.0.1:9222/devtools/browser/x"]


def test_vault_ensure_supervisor_does_not_start_after_takeover_during_resolve(monkeypatch):
    """Local-session vault attach has the same mid-resolve leftover door."""
    import tools.bot_desktop.browser as bdb
    from tools import browser_vault_tool as vault

    started = []
    monkeypatch.setattr(bdb, "running_instance_cdp_port", lambda *a, **k: 9333)
    monkeypatch.setattr(
        "tools.browser_tool_session._admit_task_shared_browser",
        lambda *a, **k: None,
    )
    monkeypatch.setattr(
        "tools.browser_tool_session._run_browser_command",
        lambda *a, **k: {"success": True, "data": {"cdpUrl": "http://127.0.0.1:9333"}},
    )
    monkeypatch.setattr("tools.browser_tool._last_session_key", lambda tid: tid)
    monkeypatch.setattr(
        "tools.browser_tool_cdp._get_dialog_policy_config",
        lambda: ("accept", 1.0),
    )

    def _resolve(url):
        lease.acquire("human-viewer")
        return "ws://127.0.0.1:9333/devtools/browser/x"

    monkeypatch.setattr("tools.browser_tool_cdp._resolve_cdp_override", _resolve)
    import tools.browser_supervisor as bs
    registry = __import__("unittest.mock", fromlist=["MagicMock"]).MagicMock()
    registry.get.return_value = None
    registry.get_or_start.side_effect = lambda **k: started.append(k.get("cdp_url"))
    monkeypatch.setattr(bs, "SUPERVISOR_REGISTRY", registry)
    assert vault._ensure_supervisor("review") is None
    assert started == []
    assert lease.human_holds() is True


def test_browser_cdp_remembers_dock_port_when_devtools_file_is_gone(monkeypatch):
    """browser_cdp must not HTTP-probe the remembered dock after the port file vanishes."""
    import tools.bot_desktop.browser as bdb
    from tools import browser_cdp_tool as cdp
    from tools.browser_tool_session import _cdp_url_is_bot_desktop_browser

    probed = []
    monkeypatch.setattr(bdb, "running_instance_cdp_port", lambda *a, **k: 9333)
    assert _cdp_url_is_bot_desktop_browser("http://127.0.0.1:9333") is True
    monkeypatch.setattr(bdb, "running_instance_cdp_port", lambda *a, **k: None)
    monkeypatch.setattr("tools.browser_tool_cdp._get_cdp_override_raw", lambda: "http://127.0.0.1:9333")
    monkeypatch.setattr(cdp, "_resolve_cdp_endpoint", lambda: probed.append("http") or "ws://127.0.0.1:9333/devtools/browser/x")
    monkeypatch.setattr(cdp, "_WS_AVAILABLE", True)
    monkeypatch.setattr(cdp, "_run_async", lambda *_a, **_k: {"data": "SECRET"})
    lease.acquire("human-viewer")
    out = json.loads(cdp.browser_cdp("Target.getTargets", {}))
    assert out.get("code") == "human_has_control"
    assert probed == []
    assert "SECRET" not in json.dumps(out)


def test_sibling_profile_browser_connect_does_not_override_this_home(monkeypatch, tmp_path):
    """``/browser connect`` writes BROWSER_CDP_URL; a multiplex sibling must not inherit it."""
    from hermes_constants import hermes_home_key, reset_hermes_home_override, set_hermes_home_override
    from tools import browser_tool_cdp as cdp

    home_a = tmp_path / "a"
    home_b = tmp_path / "b"
    home_a.mkdir()
    home_b.mkdir()
    saved_by_home = dict(cdp._cdp_override_by_home)
    saved_env_home = cdp._cdp_override_env_home
    monkeypatch.delenv("BROWSER_CDP_URL", raising=False)
    token_a = set_hermes_home_override(home_a)
    try:
        cdp.set_process_cdp_override("http://127.0.0.1:9333")
        assert cdp._get_cdp_override_raw() == "http://127.0.0.1:9333"
        reset_hermes_home_override(token_a)
        token_b = set_hermes_home_override(home_b)
        try:
            assert hermes_home_key() != hermes_home_key(home_a)
            assert cdp._get_cdp_override_raw() == ""
        finally:
            reset_hermes_home_override(token_b)
    finally:
        cdp._cdp_override_by_home.clear()
        cdp._cdp_override_by_home.update(saved_by_home)
        cdp._cdp_override_env_home = saved_env_home
        monkeypatch.delenv("BROWSER_CDP_URL", raising=False)


def _shared_session(bt, task_id="review"):
    saved = bt._active_sessions.copy()
    bt._active_sessions[task_id] = {
        "session_name": "h_review", "features": {"local": True},
    }
    return saved


def test_vault_supervisor_eval_is_fenced_while_human_holds(monkeypatch):
    """browser_vault_* inspects the page over CDP without _run_browser_command."""
    from unittest.mock import MagicMock
    from tools import browser_tool as bt
    from tools import browser_vault_tool as vault
    from tools.bot_desktop import lease

    _wire(monkeypatch, [])
    monkeypatch.setattr(bt, "_last_session_key", lambda task_id: "review")
    saved = _shared_session(bt)
    try:
        sup = MagicMock()
        sup.evaluate_runtime.return_value = {"ok": True, "result": "https://bank.test/secret"}
        import tools.browser_supervisor as bs
        registry = MagicMock()
        registry.get.return_value = sup
        monkeypatch.setattr(bs, "SUPERVISOR_REGISTRY", registry)
        lease.acquire("human-viewer")
        out = vault._eval_js("review", "window.location.href")
        assert out.get("code") == "human_has_control"
        assert "bank.test" not in json.dumps(out)
        sup.evaluate_runtime.assert_not_called()
    finally:
        bt._active_sessions.clear()
        bt._active_sessions.update(saved)


def test_vault_secret_fill_is_fenced_while_human_holds(monkeypatch):
    """Password injection talks to the supervisor only — the lease must still bind."""
    from unittest.mock import MagicMock
    from tools import browser_tool as bt
    from tools import browser_vault_tool as vault
    from tools.bot_desktop import lease

    _wire(monkeypatch, [])
    monkeypatch.setattr(bt, "_last_session_key", lambda task_id: "review")
    saved = _shared_session(bt)
    try:
        sup = MagicMock()
        sup.evaluate_runtime.return_value = {"ok": True, "result": "filled"}
        import tools.browser_supervisor as bs
        registry = MagicMock()
        registry.get.return_value = sup
        monkeypatch.setattr(bs, "SUPERVISOR_REGISTRY", registry)
        lease.acquire("human-viewer")
        out = vault._eval_js_secret("review", "document.querySelector('input').value = 's3cret-pw'")
        assert out.get("code") == "human_has_control"
        assert "s3cret-pw" not in json.dumps(out)
        sup.evaluate_runtime.assert_not_called()
    finally:
        bt._active_sessions.clear()
        bt._active_sessions.update(saved)


def test_vault_secret_fill_discards_result_after_takeover(monkeypatch):
    from unittest.mock import MagicMock
    from tools import browser_tool as bt
    from tools import browser_vault_tool as vault
    from tools.bot_desktop import lease

    _wire(monkeypatch, [])
    monkeypatch.setattr(bt, "_last_session_key", lambda task_id: "review")
    saved = _shared_session(bt)
    try:
        sup = MagicMock()

        def _eval(_expr):
            lease.acquire("human-viewer")
            lease.release("human-viewer")
            return {"ok": True, "result": "FILLED-SECRET"}

        sup.evaluate_runtime.side_effect = _eval
        import tools.browser_supervisor as bs
        registry = MagicMock()
        registry.get.return_value = sup
        monkeypatch.setattr(bs, "SUPERVISOR_REGISTRY", registry)
        out = vault._eval_js_secret("review", "1")
        assert out.get("code") == "human_has_control"
        assert "FILLED-SECRET" not in json.dumps(out)
    finally:
        bt._active_sessions.clear()
        bt._active_sessions.update(saved)


def test_vault_focus_does_not_switch_tabs_while_human_holds(monkeypatch):
    from unittest.mock import MagicMock
    from tools import browser_tool as bt
    from tools import browser_vault_tool as vault
    from tools.bot_desktop import lease

    _wire(monkeypatch, [])
    monkeypatch.setattr(bt, "_last_session_key", lambda task_id: "review")
    saved = _shared_session(bt)
    try:
        sup = MagicMock()
        sup.focus_page.return_value = {"ok": True, "url": "https://bank.test"}
        import tools.browser_supervisor as bs
        registry = MagicMock()
        registry.get.return_value = sup
        monkeypatch.setattr(bs, "SUPERVISOR_REGISTRY", registry)
        lease.acquire("human-viewer")
        assert vault._focus_bound_origin("review", "https://bank.test", "login") is None
        sup.focus_page.assert_not_called()
    finally:
        bt._active_sessions.clear()
        bt._active_sessions.update(saved)


def test_browser_dialog_is_fenced_while_human_holds(monkeypatch):
    """Accept/dismiss on the dock Chromium skipped the command fence."""
    from unittest.mock import MagicMock
    from tools import browser_dialog_tool as dialog
    from tools import browser_tool as bt
    from tools.bot_desktop import lease

    _wire(monkeypatch, [])
    monkeypatch.setattr(bt, "_last_session_key", lambda task_id: "review")
    saved = _shared_session(bt)
    try:
        sup = MagicMock()
        sup.respond_to_dialog.return_value = {"ok": True, "dialog": {"message": "SECRET-PROMPT"}}
        import tools.browser_dialog_tool as dt
        monkeypatch.setattr(dt, "SUPERVISOR_REGISTRY", MagicMock(get=lambda *_a, **_k: sup))
        lease.acquire("human-viewer")
        out = json.loads(dialog.browser_dialog(action="accept", prompt_text="yes", task_id="review"))
        assert out.get("code") == "human_has_control"
        assert "SECRET-PROMPT" not in json.dumps(out)
        sup.respond_to_dialog.assert_not_called()
    finally:
        bt._active_sessions.clear()
        bt._active_sessions.update(saved)


def test_snapshot_supervisor_merge_skips_dialogs_after_takeover(monkeypatch):
    """The accessibility snapshot can succeed; live pending_dialogs must not ride along after Take over."""
    from unittest.mock import MagicMock
    from tools import browser_tool as bt
    from tools.bot_desktop import lease

    _wire(monkeypatch, [])
    monkeypatch.setattr(bt, "_is_camofox_mode", lambda: False)
    monkeypatch.setattr(bt, "_last_session_key", lambda task_id: "review")
    monkeypatch.setattr(bt, "_blocked_private_page_content", lambda *_a, **_k: None)
    monkeypatch.setattr(bt, "_snapshot_fields", lambda *_a, **_k: {"snapshot": "tree"})

    def _snapshot_ok(*_a, **_k):
        lease.acquire("human-viewer")
        return {"success": True, "data": {}}

    monkeypatch.setattr(bt._session, "_run_browser_command", _snapshot_ok)
    saved = _shared_session(bt)
    try:
        snap = MagicMock()
        snap.active = True
        snap.to_dict.return_value = {"pending_dialogs": [{"message": "SECRET-PROMPT"}]}
        sup = MagicMock()
        sup.snapshot.return_value = snap
        import tools.browser_supervisor as bs
        monkeypatch.setattr(bs, "SUPERVISOR_REGISTRY", MagicMock(get=lambda *_a, **_k: sup))
        out = json.loads(bt.browser_snapshot(task_id="review"))
        assert out.get("success") is True
        assert "SECRET-PROMPT" not in json.dumps(out)
        sup.snapshot.assert_not_called()
    finally:
        bt._active_sessions.clear()
        bt._active_sessions.update(saved)


def test_browser_dialog_discards_result_after_takeover(monkeypatch):
    from unittest.mock import MagicMock
    from tools import browser_dialog_tool as dialog
    from tools import browser_tool as bt
    from tools.bot_desktop import lease

    _wire(monkeypatch, [])
    monkeypatch.setattr(bt, "_last_session_key", lambda task_id: "review")
    saved = _shared_session(bt)
    try:
        sup = MagicMock()

        def _respond(**_k):
            lease.acquire("human-viewer")
            lease.release("human-viewer")
            return {"ok": True, "dialog": {"message": "SECRET-PROMPT"}}

        sup.respond_to_dialog.side_effect = _respond
        import tools.browser_dialog_tool as dt
        monkeypatch.setattr(dt, "SUPERVISOR_REGISTRY", MagicMock(get=lambda *_a, **_k: sup))
        out = json.loads(dialog.browser_dialog(action="dismiss", task_id="review"))
        assert out.get("code") == "human_has_control"
        assert "SECRET-PROMPT" not in json.dumps(out)
    finally:
        bt._active_sessions.clear()
        bt._active_sessions.update(saved)


def _cloud_session(bt, task_id="review"):
    """A Browserbase / other-jar row. Leftover dock I/O must not inherit this admit."""
    saved = bt._active_sessions.copy()
    bt._active_sessions[task_id] = {
        "session_name": "cloud",
        "cdp_url": "wss://browserbase.example/session",
        "features": {"local": False, "cdp_override": True},
    }
    return saved


def _dock_leftover():
    from unittest.mock import MagicMock

    sup = MagicMock()
    sup.targets_bot_desktop = True
    sup.cdp_url = "ws://127.0.0.1:9333/devtools/browser/x"
    sup.hermes_home = None
    return sup


def _wire_cloud_override(monkeypatch, bt):
    """Session + /browser connect both name another jar so leftover identity is skipped."""
    from tools import browser_tool_session as session

    cloud = {
        "session_name": "cloud",
        "cdp_url": "wss://browserbase.example/session",
        "features": {"local": False, "cdp_override": True},
    }
    monkeypatch.setattr(session, "_get_session_info", lambda *a: cloud)
    monkeypatch.setattr("tools.browser_tool_cdp._get_cdp_override_raw", lambda: "wss://browserbase.example/session")
    monkeypatch.setattr(bt, "_last_session_key", lambda task_id: "review")


def test_admit_supervisor_fences_dock_leftover_while_human_holds(monkeypatch):
    """The leftover jar is the dock even when no session row names it."""
    from tools.bot_desktop.lease import HumanHasControl
    from tools.browser_tool_session import _admit_supervisor

    _wire(monkeypatch, [])
    lease.acquire("human-viewer")
    with pytest.raises(HumanHasControl):
        _admit_supervisor(_dock_leftover())


def test_admit_supervisor_does_not_invent_a_dock_from_unstamped_loopback(monkeypatch):
    """An unstamped leftover on some other loopback port is not this profile's dock."""
    import tools.bot_desktop.browser as bdb
    from tools.browser_tool_session import _admit_supervisor

    _wire(monkeypatch, [])
    monkeypatch.setattr(bdb, "running_instance_cdp_port", lambda *a, **k: 9333)
    from unittest.mock import MagicMock

    sup = MagicMock()
    sup.targets_bot_desktop = False
    sup.cdp_url = "ws://127.0.0.1:9222/devtools/browser/x"
    lease.acquire("human-viewer")
    assert _admit_supervisor(sup) is None


def test_snapshot_leftover_dock_merge_skips_when_session_is_another_browser(monkeypatch):
    """pending_dialogs on a leftover dock WS must not ride along after Take over."""
    from tools import browser_tool as bt

    _wire(monkeypatch, [])
    _wire_cloud_override(monkeypatch, bt)
    monkeypatch.setattr(bt, "_is_camofox_mode", lambda: False)
    monkeypatch.setattr(bt, "_blocked_private_page_content", lambda *_a, **_k: None)
    monkeypatch.setattr(bt, "_snapshot_fields", lambda *_a, **_k: {"snapshot": "tree"})
    monkeypatch.setattr(bt._session, "_run_browser_command", lambda *_a, **_k: {"success": True, "data": {}})
    saved = _cloud_session(bt)
    try:
        snap = type("Snap", (), {})()
        snap.active = True
        snap.to_dict = lambda: {"pending_dialogs": [{"message": "SECRET-PROMPT"}]}
        sup = _dock_leftover()
        sup.snapshot.return_value = snap
        import tools.browser_supervisor as bs
        monkeypatch.setattr(bs, "SUPERVISOR_REGISTRY", type("R", (), {"get": staticmethod(lambda *_a, **_k: sup)})())
        lease.acquire("human-viewer")
        out = json.loads(bt.browser_snapshot(task_id="review"))
        assert out.get("success") is True
        assert "SECRET-PROMPT" not in json.dumps(out)
        sup.snapshot.assert_not_called()
    finally:
        bt._active_sessions.clear()
        bt._active_sessions.update(saved)


def test_eval_leftover_dock_is_fenced_when_session_is_another_browser(monkeypatch):
    from tools import browser_tool as bt

    _wire(monkeypatch, [])
    _wire_cloud_override(monkeypatch, bt)
    monkeypatch.setattr(bt, "_is_camofox_mode", lambda: False)
    saved = _cloud_session(bt)
    try:
        sup = _dock_leftover()
        sup.evaluate_runtime.return_value = {"ok": True, "result": "SECRET-FROM-PAGE"}
        import tools.browser_supervisor as bs
        monkeypatch.setattr(bs, "SUPERVISOR_REGISTRY", type("R", (), {"get": staticmethod(lambda *_a, **_k: sup)})())
        lease.acquire("human-viewer")
        out = json.loads(bt._browser_eval("document.body.innerText", task_id="review"))
        assert out.get("code") == "human_has_control"
        assert "SECRET-FROM-PAGE" not in json.dumps(out)
        sup.evaluate_runtime.assert_not_called()
    finally:
        bt._active_sessions.clear()
        bt._active_sessions.update(saved)


def test_dialog_leftover_dock_is_fenced_when_session_is_another_browser(monkeypatch):
    from tools import browser_dialog_tool as dialog
    from tools import browser_tool as bt

    _wire(monkeypatch, [])
    _wire_cloud_override(monkeypatch, bt)
    saved = _cloud_session(bt)
    try:
        sup = _dock_leftover()
        sup.respond_to_dialog.return_value = {"ok": True, "dialog": {"message": "SECRET-PROMPT"}}
        import tools.browser_dialog_tool as dt
        monkeypatch.setattr(dt, "SUPERVISOR_REGISTRY", type("R", (), {"get": staticmethod(lambda *_a, **_k: sup)})())
        lease.acquire("human-viewer")
        out = json.loads(dialog.browser_dialog(action="accept", prompt_text="yes", task_id="review"))
        assert out.get("code") == "human_has_control"
        assert "SECRET-PROMPT" not in json.dumps(out)
        sup.respond_to_dialog.assert_not_called()
    finally:
        bt._active_sessions.clear()
        bt._active_sessions.update(saved)


def test_vault_leftover_dock_eval_is_fenced_when_session_is_another_browser(monkeypatch):
    from tools import browser_tool as bt
    from tools import browser_vault_tool as vault

    _wire(monkeypatch, [])
    _wire_cloud_override(monkeypatch, bt)
    saved = _cloud_session(bt)
    try:
        sup = _dock_leftover()
        sup.evaluate_runtime.return_value = {"ok": True, "result": "https://bank.test/secret"}
        import tools.browser_supervisor as bs
        monkeypatch.setattr(bs, "SUPERVISOR_REGISTRY", type("R", (), {"get": staticmethod(lambda *_a, **_k: sup)})())
        lease.acquire("human-viewer")
        out = vault._eval_js("review", "window.location.href")
        assert out.get("code") == "human_has_control"
        assert "bank.test" not in json.dumps(out)
        sup.evaluate_runtime.assert_not_called()
        secret = vault._eval_js_secret("review", "document.querySelector('input').value = 's3cret-pw'")
        assert secret.get("code") == "human_has_control"
        assert "s3cret-pw" not in json.dumps(secret)
        assert secret.get("error_type") == "human_has_control"
    finally:
        bt._active_sessions.clear()
        bt._active_sessions.update(saved)


def test_cdp_via_leftover_dock_is_fenced_when_session_is_another_browser(monkeypatch):
    from tools import browser_cdp_tool as cdp
    from tools import browser_tool as bt

    _wire(monkeypatch, [])
    _wire_cloud_override(monkeypatch, bt)
    saved = _cloud_session(bt)
    try:
        sup = _dock_leftover()
        import tools.browser_supervisor as bs
        monkeypatch.setattr(bs, "SUPERVISOR_REGISTRY", type("R", (), {"get": staticmethod(lambda *_a, **_k: sup)})())
        lease.acquire("human-viewer")
        out = json.loads(cdp._browser_cdp_via_supervisor(
            "review", "frame-1", "Runtime.evaluate", {"expression": "1"}, 1.0,
        ))
        assert out.get("code") == "human_has_control"
        assert "SECRET" not in json.dumps(out)
        sup.snapshot.assert_not_called()
    finally:
        bt._active_sessions.clear()
        bt._active_sessions.update(saved)


def test_unstamped_cloud_leftover_eval_is_not_the_dock_jar(monkeypatch):
    """A leftover attached to another browser stays usable while a human holds this screen."""
    from unittest.mock import MagicMock
    from tools import browser_tool as bt

    _wire(monkeypatch, [])
    _wire_cloud_override(monkeypatch, bt)
    monkeypatch.setattr(bt, "_is_camofox_mode", lambda: False)
    saved = _cloud_session(bt)
    try:
        sup = MagicMock()
        sup.targets_bot_desktop = False
        sup.cdp_url = "wss://browserbase.example/session"
        sup.evaluate_runtime.return_value = {"ok": True, "result": "cloud-page"}
        import tools.browser_supervisor as bs
        monkeypatch.setattr(bs, "SUPERVISOR_REGISTRY", type("R", (), {"get": staticmethod(lambda *_a, **_k: sup)})())
        lease.acquire("human-viewer")
        out = json.loads(bt._browser_eval("document.title", task_id="review"))
        assert out.get("code") != "human_has_control"
        sup.evaluate_runtime.assert_called_once()
    finally:
        bt._active_sessions.clear()
        bt._active_sessions.update(saved)


def test_force_cleanup_defers_shared_chromium_while_human_holds(monkeypatch):
    """cleanup_all_browsers used force=True and skipped the human-hold deferral.

    /browser connect and gateway shutdown then tree-killed the dock Chromium
    a human was typing into. Force-reap already deferred; the regular
    force path must match.
    """
    from tools import browser_tool as bt
    from tools import browser_tool_lifecycle as life

    released: list = []
    saved = bt._active_sessions.copy()
    try:
        bt._active_sessions["review"] = {
            "session_name": "h_review",
            "features": {"local": True},
            "bb_session_id": None,
        }
        monkeypatch.setattr(
            life, "_release_session_resources", lambda *a, **k: released.append("release")
        )
        monkeypatch.setattr(
            "tools.browser_tool_session._run_browser_command",
            lambda *a, **k: released.append("close"),
        )
        lease.acquire("human-viewer")
        life.cleanup_browser("review", force=True)
        life.cleanup_all_browsers()
        assert "review" in bt._active_sessions
        assert released == []
    finally:
        bt._active_sessions.clear()
        bt._active_sessions.update(saved)


def test_force_cleanup_still_reaps_unrelated_cloud_session_while_human_holds(monkeypatch):
    """A cloud session is another browser; force cleanup must still reap it."""
    from tools import browser_tool as bt
    from tools import browser_tool_lifecycle as life

    saved = bt._active_sessions.copy()
    try:
        bt._active_sessions["cloud"] = {
            "session_name": "bb",
            "bb_session_id": None,
            "features": {"local": False},
        }
        monkeypatch.setattr(
            "tools.browser_tool_session._run_browser_command",
            lambda *a, **k: {"success": True},
        )
        monkeypatch.setattr(life.os.path, "exists", lambda *_a, **_k: False)
        lease.acquire("human-viewer")
        life.cleanup_browser("cloud", force=True)
        assert "cloud" not in bt._active_sessions
    finally:
        bt._active_sessions.clear()
        bt._active_sessions.update(saved)
