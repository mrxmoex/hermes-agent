"""The bot's browser tools obey the screen lease: while a human holds the shared browser, nothing is
dispatched, and a command whose run crossed a takeover loses its result."""

from __future__ import annotations

import json
import threading

import pytest

from tools.bot_desktop import lease, runtime


def _drain_listen(listener):
    """Accept persist TCP probes so a later named-listen check is not stuck."""

    def _drain():
        while True:
            try:
                conn, _ = listener.accept()
            except OSError:
                return
            try:
                conn.close()
            except OSError:
                pass

    threading.Thread(target=_drain, daemon=True).start()


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


def test_debian_and_mapped_loopback_hosts_still_fence_after_devtools_miss(monkeypatch):
    """Identity used a closed host set (127.0.0.1 / localhost / ::1 / 0.0.0.0).
    Debian's 127.0.1.1 and IPv4-mapped ::ffff:127.0.0.1 never extracted a
    port, so persist could not match and leftover attach was another Chrome
    (admit None) on the jar a human is typing into. LAN hosts stay unfenced.
    """
    import tools.bot_desktop.browser as bdb
    from tools.bot_desktop.lease import HumanHasControl
    from tools.browser_tool_session import (
        _admit_resolved_cdp_for_attach,
        _admit_shared_browser,
        _cdp_url_is_bot_desktop_browser,
        _last_dock_cdp_port,
        _loopback_cdp_port,
    )

    debian = "ws://127.0.1.1:9333/devtools/browser/x"
    mapped = "ws://[::ffff:127.0.0.1]:9333/devtools/browser/x"
    other_debian = "ws://127.0.1.1:9222/devtools/browser/x"
    lan = "ws://192.168.1.5:9333/devtools/browser/x"

    assert _loopback_cdp_port(debian) == 9333
    assert _loopback_cdp_port(mapped) == 9333
    assert _loopback_cdp_port(lan) is None

    monkeypatch.setattr(bdb, "running_instance_cdp_port", lambda *a, **k: 9333)
    assert _cdp_url_is_bot_desktop_browser(debian) is True
    _last_dock_cdp_port.clear()
    monkeypatch.setattr(bdb, "running_instance_cdp_port", lambda *a, **k: None)
    lease.acquire("human-viewer")
    assert _cdp_url_is_bot_desktop_browser(debian) is True
    assert _cdp_url_is_bot_desktop_browser(mapped) is True
    assert _cdp_url_is_bot_desktop_browser(other_debian) is False
    assert _cdp_url_is_bot_desktop_browser(lan) is False
    with pytest.raises(HumanHasControl):
        _admit_shared_browser(cdp_url=debian)
    with pytest.raises(HumanHasControl):
        _admit_shared_browser(cdp_url=mapped)
    assert _admit_resolved_cdp_for_attach(debian) is False
    assert _admit_resolved_cdp_for_attach(mapped) is False
    assert _admit_shared_browser(cdp_url=other_debian) is None
    assert _admit_resolved_cdp_for_attach(other_debian) is True
    assert _admit_shared_browser(cdp_url=lan) is None
    assert _admit_resolved_cdp_for_attach(lan) is True


def test_whatwg_ipv4_shorthand_still_fences_after_devtools_miss(monkeypatch):
    """Identity used ``ipaddress``, which rejects Chromium / WHATWG shorthand.

    ``http://127.1:9333`` / ``http://0:9333`` / ``http://2130706433:9333``
    never extracted a port, so persist could not match and leftover
    attach was another Chrome (admit None) on the jar a human is typing
    into. LAN shorthand (``10.1``) and another loopback port stay
    unfenced.
    """
    import tools.bot_desktop.browser as bdb
    from tools.bot_desktop.lease import HumanHasControl
    from tools.browser_tool_session import (
        _admit_resolved_cdp_for_attach,
        _admit_shared_browser,
        _cdp_url_is_bot_desktop_browser,
        _last_dock_cdp_port,
        _loopback_cdp_port,
    )

    short = "ws://127.1:9333/devtools/browser/x"
    three = "ws://127.0.1:9333/devtools/browser/x"
    zero = "http://0:9333"
    packed = "http://2130706433:9333"
    mapped = "ws://[::ffff:127.1]:9333/devtools/browser/x"
    other = "ws://127.1:9222/devtools/browser/x"
    lan = "ws://10.1:9333/devtools/browser/x"

    assert _loopback_cdp_port(short) == 9333
    assert _loopback_cdp_port(three) == 9333
    assert _loopback_cdp_port(zero) == 9333
    assert _loopback_cdp_port(packed) == 9333
    assert _loopback_cdp_port(mapped) == 9333
    assert _loopback_cdp_port(other) == 9222
    assert _loopback_cdp_port(lan) is None

    monkeypatch.setattr(bdb, "running_instance_cdp_port", lambda *a, **k: 9333)
    assert _cdp_url_is_bot_desktop_browser(short) is True
    _last_dock_cdp_port.clear()
    monkeypatch.setattr(bdb, "running_instance_cdp_port", lambda *a, **k: None)
    lease.acquire("human-viewer")
    assert _cdp_url_is_bot_desktop_browser(short) is True
    assert _cdp_url_is_bot_desktop_browser(three) is True
    assert _cdp_url_is_bot_desktop_browser(zero) is True
    assert _cdp_url_is_bot_desktop_browser(packed) is True
    assert _cdp_url_is_bot_desktop_browser(mapped) is True
    assert _cdp_url_is_bot_desktop_browser(other) is False
    assert _cdp_url_is_bot_desktop_browser(lan) is False
    with pytest.raises(HumanHasControl):
        _admit_shared_browser(cdp_url=short)
    with pytest.raises(HumanHasControl):
        _admit_shared_browser(cdp_url=zero)
    assert _admit_resolved_cdp_for_attach(short) is False
    assert _admit_resolved_cdp_for_attach(mapped) is False
    assert _admit_shared_browser(cdp_url=other) is None
    assert _admit_resolved_cdp_for_attach(other) is True
    assert _admit_shared_browser(cdp_url=lan) is None
    assert _admit_resolved_cdp_for_attach(lan) is True


def test_percent_encoded_loopback_still_fences_after_devtools_miss(monkeypatch):
    """Node leftover decodes ``127%2e1``; urllib.parse does not.

    ``new URL('http://127%2e1:9333')`` is 127.0.0.1:9333. Persist then
    could not match and leftover attach was another Chrome (admit None)
    on the jar a human is typing into. LAN ``10%2e1`` and another
    loopback port stay unfenced. Decode once — ``%2531`` is not 1.
    """
    import tools.bot_desktop.browser as bdb
    from tools.bot_desktop.lease import HumanHasControl
    from tools.browser_tool_session import (
        _admit_resolved_cdp_for_attach,
        _admit_shared_browser,
        _cdp_url_is_bot_desktop_browser,
        _last_dock_cdp_port,
        _loopback_cdp_port,
    )

    dotted = "http://127%2e1:9333"
    full = "http://127%2e0%2e0%2e1:9333"
    digits = "http://%31%32%37.0.0.1:9333"
    zero = "http://0%2e0%2e0%2e0:9333"
    mapped = "ws://[::ffff:127%2e1]:9333/devtools/browser/x"
    other = "http://127%2e1:9222"
    lan = "http://10%2e1:9333"
    double = "http://%2531%2532%2537.0.0.1:9333"

    assert _loopback_cdp_port(dotted) == 9333
    assert _loopback_cdp_port(full) == 9333
    assert _loopback_cdp_port(digits) == 9333
    assert _loopback_cdp_port(zero) == 9333
    assert _loopback_cdp_port(mapped) == 9333
    assert _loopback_cdp_port(other) == 9222
    assert _loopback_cdp_port(lan) is None
    assert _loopback_cdp_port(double) is None

    monkeypatch.setattr(bdb, "running_instance_cdp_port", lambda *a, **k: 9333)
    assert _cdp_url_is_bot_desktop_browser(dotted) is True
    _last_dock_cdp_port.clear()
    monkeypatch.setattr(bdb, "running_instance_cdp_port", lambda *a, **k: None)
    lease.acquire("human-viewer")
    assert _cdp_url_is_bot_desktop_browser(dotted) is True
    assert _cdp_url_is_bot_desktop_browser(full) is True
    assert _cdp_url_is_bot_desktop_browser(digits) is True
    assert _cdp_url_is_bot_desktop_browser(zero) is True
    assert _cdp_url_is_bot_desktop_browser(mapped) is True
    assert _cdp_url_is_bot_desktop_browser(other) is False
    assert _cdp_url_is_bot_desktop_browser(lan) is False
    assert _cdp_url_is_bot_desktop_browser(double) is False
    with pytest.raises(HumanHasControl):
        _admit_shared_browser(cdp_url=dotted)
    with pytest.raises(HumanHasControl):
        _admit_shared_browser(cdp_url=full)
    assert _admit_resolved_cdp_for_attach(dotted) is False
    assert _admit_resolved_cdp_for_attach(digits) is False
    assert _admit_shared_browser(cdp_url=other) is None
    assert _admit_resolved_cdp_for_attach(other) is True
    assert _admit_shared_browser(cdp_url=lan) is None
    assert _admit_resolved_cdp_for_attach(lan) is True


def test_this_machine_hostname_still_fences_after_devtools_miss(monkeypatch):
    """Identity treated only IP / localhost-style hosts as the dock.
    Chromium, Debian ``127.0.1.1 <hostname>``, and
    ``--remote-debugging-address=0.0.0.0`` advertise ``ws://<hostname>:port``.
    Persist then could not match, leftover attach was another Chrome (admit
    None) on the jar a human is typing into. A different hostname or LAN IP
    stays another browser. Do not resolve DNS.
    """
    import os
    import socket

    import tools.bot_desktop.browser as bdb
    from tools.bot_desktop.lease import HumanHasControl
    from tools.browser_tool_session import (
        _admit_resolved_cdp_for_attach,
        _admit_shared_browser,
        _cdp_url_is_bot_desktop_browser,
        _last_dock_cdp_port,
        _loopback_cdp_port,
    )

    class _Uname:
        nodename = "testbox"

    monkeypatch.setattr(socket, "gethostname", lambda: "testbox")
    monkeypatch.setattr(os, "uname", lambda: _Uname)

    host = "ws://testbox:9333/devtools/browser/x"
    fqdn = "ws://testbox.example:9333/devtools/browser/x"
    other = "ws://otherbox:9333/devtools/browser/x"
    other_port = "ws://testbox:9222/devtools/browser/x"
    debian = "ws://127.0.1.1:9333/devtools/browser/x"
    lan = "ws://192.168.1.5:9333/devtools/browser/x"
    ip6 = "ws://ip6-localhost:9333/devtools/browser/x"

    assert _loopback_cdp_port(host) == 9333
    assert _loopback_cdp_port(fqdn) == 9333
    assert _loopback_cdp_port(other) is None
    assert _loopback_cdp_port(lan) is None
    assert _loopback_cdp_port(debian) == 9333
    assert _loopback_cdp_port(ip6) == 9333

    monkeypatch.setattr(socket, "gethostname", lambda: "testbox.example")
    assert _loopback_cdp_port(host) == 9333
    monkeypatch.setattr(socket, "gethostname", lambda: "testbox")

    monkeypatch.setattr(bdb, "running_instance_cdp_port", lambda *a, **k: 9333)
    assert _cdp_url_is_bot_desktop_browser(host) is True
    _last_dock_cdp_port.clear()
    monkeypatch.setattr(bdb, "running_instance_cdp_port", lambda *a, **k: None)
    lease.acquire("human-viewer")
    assert _cdp_url_is_bot_desktop_browser(host) is True
    assert _cdp_url_is_bot_desktop_browser(fqdn) is True
    assert _cdp_url_is_bot_desktop_browser(ip6) is True
    assert _cdp_url_is_bot_desktop_browser(other) is False
    assert _cdp_url_is_bot_desktop_browser(other_port) is False
    assert _cdp_url_is_bot_desktop_browser(lan) is False
    assert _cdp_url_is_bot_desktop_browser(debian) is True
    with pytest.raises(HumanHasControl):
        _admit_shared_browser(cdp_url=host)
    with pytest.raises(HumanHasControl):
        _admit_shared_browser(cdp_url=fqdn)
    assert _admit_resolved_cdp_for_attach(host) is False
    assert _admit_resolved_cdp_for_attach(fqdn) is False
    assert _admit_resolved_cdp_for_attach(ip6) is False
    assert _admit_shared_browser(cdp_url=other) is None
    assert _admit_resolved_cdp_for_attach(other) is True
    assert _admit_resolved_cdp_for_attach(other_port) is True
    assert _admit_resolved_cdp_for_attach(lan) is True
    assert _admit_resolved_cdp_for_attach(debian) is False


def test_hosts_file_loopback_alias_still_fences_after_devtools_miss(monkeypatch):
    """Identity treated only IP / localhost / this process's hostname as the dock.
    ``/etc/hosts`` ``127.0.0.1 dock-chrome`` (or Chromium advertising that
    alias) never extracted a port, so persist could not match and leftover
    attach was another Chrome (admit None) on the jar a human is typing
    into. A hosts name mapped to a LAN IP stays another browser. No DNS.
    """
    import tools.bot_desktop.browser as bdb
    from tools.bot_desktop.lease import HumanHasControl
    from tools.browser_tool_session import (
        _admit_resolved_cdp_for_attach,
        _admit_shared_browser,
        _cdp_url_is_bot_desktop_browser,
        _last_dock_cdp_port,
        _loopback_cdp_port,
        _parse_loopback_hosts_text,
    )

    parsed = _parse_loopback_hosts_text(
        "127.0.0.1 dock-chrome alias.example\n"
        "192.168.1.5 otherbox\n"
        "::1 ip6-localhost\n"
        "10.0.0.8 lanbox\n"
    )
    assert "dock-chrome" in parsed
    assert "alias.example" in parsed
    assert "alias" in parsed
    assert "ip6-localhost" in parsed
    assert "otherbox" not in parsed
    assert "lanbox" not in parsed

    monkeypatch.setattr(
        "tools.browser_tool_session._loopback_hosts_file_names",
        lambda: {"dock-chrome", "alias.example", "alias"},
    )

    alias = "ws://dock-chrome:9333/devtools/browser/x"
    fqdn = "ws://alias.example:9333/devtools/browser/x"
    other = "ws://otherbox:9333/devtools/browser/x"
    lan = "ws://192.168.1.5:9333/devtools/browser/x"
    debian = "ws://127.0.1.1:9333/devtools/browser/x"

    assert _loopback_cdp_port(alias) == 9333
    assert _loopback_cdp_port(fqdn) == 9333
    assert _loopback_cdp_port(other) is None
    assert _loopback_cdp_port(lan) is None

    monkeypatch.setattr(bdb, "running_instance_cdp_port", lambda *a, **k: 9333)
    assert _cdp_url_is_bot_desktop_browser(alias) is True
    _last_dock_cdp_port.clear()
    monkeypatch.setattr(bdb, "running_instance_cdp_port", lambda *a, **k: None)
    lease.acquire("human-viewer")
    assert _cdp_url_is_bot_desktop_browser(alias) is True
    assert _cdp_url_is_bot_desktop_browser(fqdn) is True
    assert _cdp_url_is_bot_desktop_browser(other) is False
    assert _cdp_url_is_bot_desktop_browser(lan) is False
    assert _cdp_url_is_bot_desktop_browser(debian) is True
    with pytest.raises(HumanHasControl):
        _admit_shared_browser(cdp_url=alias)
    with pytest.raises(HumanHasControl):
        _admit_shared_browser(cdp_url=fqdn)
    assert _admit_resolved_cdp_for_attach(alias) is False
    assert _admit_resolved_cdp_for_attach(fqdn) is False
    assert _admit_shared_browser(cdp_url=other) is None
    assert _admit_resolved_cdp_for_attach(other) is True
    assert _admit_resolved_cdp_for_attach(lan) is True
    assert _admit_resolved_cdp_for_attach(debian) is False


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


def test_cdp_swap_blocked_by_human_matches_tui_and_cli_fence():
    """Interactive CLI ``/browser connect`` used to swap CDP while a human
    holds. TUI ``browser.manage`` already refused; both now share this
    helper so leftover attach cannot mint after a DevTools miss."""
    from tools.browser_tool_cdp import cdp_swap_blocked_by_human

    lease._reset_for_tests()
    assert cdp_swap_blocked_by_human() is None
    lease.acquire("human-viewer")
    blocked = cdp_swap_blocked_by_human()
    assert blocked and "human holds" in blocked.lower()
    lease.release("human-viewer")
    assert cdp_swap_blocked_by_human() is None


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


def test_resolve_cdp_override_does_not_http_probe_dock_while_human_holds(monkeypatch):
    """HTTP /json/version is leftover observation. Callers that skipped the
    raw-URL admit (vault ``get cdp-url``, ``browser_cdp`` discovery) still
    talked to the jar, then admitted the resolved WS. Discovery now admits
    first. Other Chromes and the agent-held dock still resolve.
    """
    import tools.bot_desktop.browser as bdb
    from tools.browser_tool_cdp import _resolve_cdp_override

    probed = []

    class _Resp:
        def raise_for_status(self):
            return None

        def json(self):
            return {"webSocketDebuggerUrl": "ws://127.0.0.1:9333/devtools/browser/x"}

    monkeypatch.setattr("requests.get", lambda *a, **k: probed.append(a[0]) or _Resp())
    monkeypatch.setattr(bdb, "running_instance_cdp_port", lambda *a, **k: 9333)
    lease.acquire("human-viewer")
    assert _resolve_cdp_override("http://127.0.0.1:9333") == "http://127.0.0.1:9333"
    assert _resolve_cdp_override("http://127.0.1.1:9333") == "http://127.0.1.1:9333"
    assert _resolve_cdp_override("ws://127.0.0.1:9333") == "ws://127.0.0.1:9333"
    assert probed == []
    assert _resolve_cdp_override("http://127.0.0.1:9222") == "ws://127.0.0.1:9333/devtools/browser/x"
    assert probed == ["http://127.0.0.1:9222/json/version"]
    lease.release("human-viewer")
    probed.clear()
    assert _resolve_cdp_override("http://127.0.0.1:9333") == "ws://127.0.0.1:9333/devtools/browser/x"
    assert probed == ["http://127.0.0.1:9333/json/version"]


def test_resolve_cdp_override_does_not_http_probe_hostname_dock_while_human_holds(monkeypatch):
    """Finding 73 identifies hostname leftover URLs. Discovery must not HTTP
    them after persist + Take over just because the caller skipped raw admit.
    """
    import os
    import socket

    import tools.bot_desktop.browser as bdb
    from tools.browser_tool_cdp import _resolve_cdp_override
    from tools.browser_tool_session import _last_dock_cdp_port

    class _Uname:
        nodename = "testbox"

    monkeypatch.setattr(socket, "gethostname", lambda: "testbox")
    monkeypatch.setattr(os, "uname", lambda: _Uname)
    probed = []
    monkeypatch.setattr("requests.get", lambda *a, **k: probed.append(a[0]) or (_ for _ in ()).throw(AssertionError("probed dock")))
    monkeypatch.setattr(bdb, "running_instance_cdp_port", lambda *a, **k: 9333)
    from tools.browser_tool_session import _cdp_url_is_bot_desktop_browser
    assert _cdp_url_is_bot_desktop_browser("http://testbox:9333") is True
    _last_dock_cdp_port.clear()
    monkeypatch.setattr(bdb, "running_instance_cdp_port", lambda *a, **k: None)
    lease.acquire("human-viewer")
    assert _resolve_cdp_override("http://testbox:9333") == "http://testbox:9333"
    assert _resolve_cdp_override("http://testbox.example:9333") == "http://testbox.example:9333"
    assert probed == []


def test_resolve_cdp_override_does_not_http_probe_hosts_alias_dock_while_human_holds(monkeypatch):
    """Finding 74 identifies /etc/hosts loopback aliases. Discovery must not
    HTTP them after persist + Take over just because the caller skipped raw admit.
    """
    import tools.bot_desktop.browser as bdb
    from tools.browser_tool_cdp import _resolve_cdp_override
    from tools.browser_tool_session import _cdp_url_is_bot_desktop_browser, _last_dock_cdp_port

    monkeypatch.setattr(
        "tools.browser_tool_session._loopback_hosts_file_names",
        lambda: {"dock-chrome"},
    )
    probed = []
    monkeypatch.setattr(
        "requests.get",
        lambda *a, **k: probed.append(a[0]) or (_ for _ in ()).throw(AssertionError("probed dock")),
    )
    monkeypatch.setattr(bdb, "running_instance_cdp_port", lambda *a, **k: 9333)
    assert _cdp_url_is_bot_desktop_browser("http://dock-chrome:9333") is True
    _last_dock_cdp_port.clear()
    monkeypatch.setattr(bdb, "running_instance_cdp_port", lambda *a, **k: None)
    lease.acquire("human-viewer")
    assert _resolve_cdp_override("http://dock-chrome:9333") == "http://dock-chrome:9333"
    assert _resolve_cdp_override("ws://dock-chrome:9333") == "ws://dock-chrome:9333"
    assert probed == []


def test_resolve_cdp_override_does_not_http_probe_when_admit_explodes(monkeypatch):
    """Finding 2: a lease-helper explosion must not fall through to /json/version."""
    from tools.browser_tool_cdp import _resolve_cdp_override

    probed = []
    monkeypatch.setattr(
        "tools.browser_tool_session._admit_shared_browser",
        lambda *a, **k: (_ for _ in ()).throw(RuntimeError("lease helper exploded")),
    )
    monkeypatch.setattr(
        "requests.get",
        lambda *a, **k: probed.append(a[0]) or (_ for _ in ()).throw(AssertionError("probed")),
    )
    assert _resolve_cdp_override("http://127.0.0.1:9333") == "http://127.0.0.1:9333"
    assert _resolve_cdp_override("http://example-host:9223") == "http://example-host:9223"
    assert probed == []


def test_missing_devtools_recover_still_skips_http_while_human_holds(monkeypatch, tmp_path):
    """Finding 76: persist never ran and DevToolsActivePort is gone. Live
    SingletonLock recovery still identifies this jar, so discovery does not
    HTTP-probe the page a human holds. A second loopback listen stays unknown.
    """
    import os
    import socket

    import tools.bot_desktop.browser as bdb
    from tools.browser_tool_cdp import _resolve_cdp_override
    from tools.browser_tool_session import _cdp_url_is_bot_desktop_browser, _last_dock_cdp_port

    profile = tmp_path / "browser-profile"
    profile.mkdir()
    os.symlink(f"host-{os.getpid()}", profile / "SingletonLock")
    monkeypatch.setattr(bdb, "profile_dir", lambda: profile)
    monkeypatch.setattr(
        bdb,
        "_chromium_cmdline_tokens",
        lambda pid: ["chrome", f"--user-data-dir={profile}", "--remote-debugging-port=0"],
    )
    listener = socket.socket()
    listener.bind(("127.0.0.1", 0))
    listener.listen(1)
    port = listener.getsockname()[1]
    probed = []
    try:
        monkeypatch.setattr(bdb, "_loopback_listen_ports_for_pid", lambda pid: {port})
        _last_dock_cdp_port.clear()
        assert bdb.last_known_dock_cdp_port() is None
        assert bdb.running_instance_cdp_port(str(profile)) == port
        _last_dock_cdp_port.clear()
        lease.acquire("human-viewer")
        monkeypatch.setattr(
            "requests.get",
            lambda *a, **k: probed.append(a[0]) or (_ for _ in ()).throw(AssertionError("probed dock")),
        )
        assert _cdp_url_is_bot_desktop_browser(f"http://127.0.0.1:{port}") is True
        assert _resolve_cdp_override(f"http://127.0.0.1:{port}") == f"http://127.0.0.1:{port}"
        assert probed == []
        monkeypatch.setattr(bdb, "running_instance_cdp_port", lambda *a, **k: None)
        _last_dock_cdp_port.clear()
        # Recover stamped persist; a later live miss must still fence this port
        # and must not treat another loopback Chrome as the dock.
        assert _cdp_url_is_bot_desktop_browser(f"http://127.0.0.1:{port}") is True
        assert _cdp_url_is_bot_desktop_browser("http://127.0.0.1:9222") is False
    finally:
        listener.close()


def test_configured_listen_identifies_dock_when_recover_and_persist_miss(monkeypatch, tmp_path):
    """Finding 86: persist never ran and unique-listen recover is still
    ambiguous. The operator override names a port this jar listens on —
    leftover identity must treat that URL as the dock, not another Chrome
    (admit None → HTTP /json/version while a human holds). 9222 stays
    unknown. A config pointing at another Chrome must not become the dock.
    """
    import os
    import socket

    import tools.bot_desktop.browser as bdb
    from tools.bot_desktop.lease import HumanHasControl
    from tools.browser_tool_cdp import _resolve_cdp_override
    from tools.browser_tool_session import (
        _admit_resolved_cdp_for_attach,
        _admit_shared_browser,
        _cdp_url_is_bot_desktop_browser,
        _last_dock_cdp_port,
    )

    profile = tmp_path / "browser-profile"
    profile.mkdir()
    os.symlink(f"host-{os.getpid()}", profile / "SingletonLock")
    monkeypatch.setattr(runtime, "state_dir", lambda: tmp_path)
    monkeypatch.setattr(bdb, "profile_dir", lambda: profile)
    monkeypatch.setattr(bdb, "running_instance_cdp_port", lambda *a, **k: None)
    monkeypatch.setattr(
        bdb,
        "_chromium_cmdline_tokens",
        lambda pid: ["chrome", f"--user-data-dir={profile}"],
    )
    listener = socket.socket()
    listener.bind(("127.0.0.1", 0))
    listener.listen(8)
    _drain_listen(listener)
    port = listener.getsockname()[1]
    monkeypatch.setattr(bdb, "_loopback_listen_ports_for_pid", lambda pid: {port, port + 1})
    monkeypatch.setattr(
        bdb,
        "_loopback_listen_targets_for_pid",
        lambda pid: {("127.0.0.1", port), ("::1", port + 1)},
    )
    monkeypatch.setattr(
        bdb, "_configured_cdp_override_url", lambda: f"http://127.0.0.1:{port}",
    )
    dock = f"http://127.0.0.1:{port}"
    other = "http://127.0.0.1:9222"
    probed = []
    try:
        _last_dock_cdp_port.clear()
        assert bdb.last_known_dock_cdp_port() is None
        assert _cdp_url_is_bot_desktop_browser(dock) is True
        assert _cdp_url_is_bot_desktop_browser(other) is False
        assert bdb.last_known_dock_cdp_port() == port
        lease.acquire("human-viewer")
        monkeypatch.setattr(
            "requests.get",
            lambda *a, **k: probed.append(a[0]) or (_ for _ in ()).throw(AssertionError("probed dock")),
        )
        assert _resolve_cdp_override(dock) == dock
        assert probed == []
        with pytest.raises(HumanHasControl):
            _admit_shared_browser(cdp_url=dock)
        assert _admit_resolved_cdp_for_attach(dock) is False
        assert _admit_shared_browser(cdp_url=other) is None
        assert _admit_resolved_cdp_for_attach(other) is True

        _last_dock_cdp_port.clear()
        (tmp_path / "dock-cdp-port").unlink(missing_ok=True)
        monkeypatch.setattr(
            bdb, "_configured_cdp_override_url", lambda: "http://127.0.0.1:9222",
        )
        assert _cdp_url_is_bot_desktop_browser(other) is False
        # Finding 155: leftover / vault already named this jar's listen.
        # A wrong override must not hide that URL or stamp 9222.
        assert _cdp_url_is_bot_desktop_browser(dock) is True
        assert bdb.last_known_dock_cdp_port() == port
    finally:
        listener.close()


def test_named_listen_identifies_dock_when_recover_persist_and_override_miss(
    monkeypatch, tmp_path,
):
    """Finding 155: unique-listen recover stays unknown with two specific
    loopbacks. Persist never ran and the operator override is empty.
    Leftover / vault already named a port this jar inode-listens on —
    that is not a guess. Admit None used to HTTP-probe the jar a human
    holds. 9222, another jar, and persist-without-a-name stay unknown.
    """
    import os
    import socket

    import tools.bot_desktop.browser as bdb
    from tools.bot_desktop.lease import HumanHasControl
    from tools.browser_tool_cdp import _resolve_cdp_override
    from tools.browser_tool_session import (
        _admit_resolved_cdp_for_attach,
        _admit_shared_browser,
        _cdp_url_is_bot_desktop_browser,
        _last_dock_cdp_port,
    )

    profile = tmp_path / "browser-profile"
    profile.mkdir()
    os.symlink(f"host-{os.getpid()}", profile / "SingletonLock")
    monkeypatch.setattr(runtime, "state_dir", lambda: tmp_path)
    monkeypatch.setattr(bdb, "profile_dir", lambda: profile)
    monkeypatch.setattr(bdb, "running_instance_cdp_port", lambda *a, **k: None)
    monkeypatch.setattr(
        bdb,
        "_chromium_cmdline_tokens",
        lambda pid: ["chrome", f"--user-data-dir={profile}"],
    )
    listener = socket.socket()
    listener.bind(("127.0.0.1", 0))
    listener.listen(8)
    _drain_listen(listener)
    port = listener.getsockname()[1]
    monkeypatch.setattr(bdb, "_loopback_listen_ports_for_pid", lambda pid: {port, port + 1})
    monkeypatch.setattr(
        bdb,
        "_loopback_listen_targets_for_pid",
        lambda pid: {("127.0.0.1", port), ("::1", port + 1)},
    )
    monkeypatch.setattr(bdb, "_configured_cdp_override_url", lambda: "")
    dock = f"http://127.0.0.1:{port}"
    other = "http://127.0.0.1:9222"
    probed = []
    try:
        _last_dock_cdp_port.clear()
        assert bdb.last_known_dock_cdp_port() is None
        assert bdb.persist_live_dock_cdp_port() is None
        assert bdb.last_known_dock_cdp_port() is None
        assert _cdp_url_is_bot_desktop_browser(dock) is True
        assert _cdp_url_is_bot_desktop_browser(other) is False
        assert bdb.last_known_dock_cdp_port() == port
        lease.acquire("human-viewer")
        monkeypatch.setattr(
            "requests.get",
            lambda *a, **k: probed.append(a[0]) or (_ for _ in ()).throw(
                AssertionError("probed dock"),
            ),
        )
        assert _resolve_cdp_override(dock) == dock
        assert probed == []
        with pytest.raises(HumanHasControl):
            _admit_shared_browser(cdp_url=dock)
        assert _admit_resolved_cdp_for_attach(dock) is False
        assert _admit_shared_browser(cdp_url=other) is None
        assert _admit_resolved_cdp_for_attach(other) is True
    finally:
        listener.close()


def test_named_listen_host_port_aims_when_persist_and_override_miss(
    monkeypatch, tmp_path,
):
    """Finding 156: leftover ``--port`` / ``--host`` named this jar.

    Unique-listen recover stays unknown with two specific loopbacks.
    Persist never ran and the operator override is empty, so
    ``dock_port`` stayed None. Finding 155 only consulted leftover
    URLs; lighthouse / CRI ``--port`` required a stamp first.
    Persist still does not stamp an arbitrary listen.
    """
    import os
    import socket

    import tools.bot_desktop.browser as bdb
    from tools.browser_tool_session import (
        _leftover_host_port_aims_at_dock,
        _unregistered_cli_aims_at_dock,
    )

    profile = tmp_path / "browser-profile"
    profile.mkdir()
    os.symlink(f"host-{os.getpid()}", profile / "SingletonLock")
    monkeypatch.setattr(runtime, "state_dir", lambda: tmp_path)
    monkeypatch.setattr(bdb, "profile_dir", lambda: profile)
    monkeypatch.setattr(bdb, "running_instance_cdp_port", lambda *a, **k: None)
    monkeypatch.setattr(
        bdb,
        "_chromium_cmdline_tokens",
        lambda pid: ["chrome", f"--user-data-dir={profile}"],
    )
    listener = socket.socket()
    listener.bind(("127.0.0.1", 0))
    listener.listen(8)
    _drain_listen(listener)
    port = listener.getsockname()[1]
    monkeypatch.setattr(bdb, "_loopback_listen_ports_for_pid", lambda pid: {port, port + 1})
    monkeypatch.setattr(
        bdb,
        "_loopback_listen_targets_for_pid",
        lambda pid: {("127.0.0.1", port), ("::1", port + 1)},
    )
    monkeypatch.setattr(bdb, "_configured_cdp_override_url", lambda: "")
    try:
        assert bdb.last_known_dock_cdp_port() is None
        assert bdb.persist_live_dock_cdp_port() is None
        assert bdb.last_known_dock_cdp_port() is None
        assert _leftover_host_port_aims_at_dock(None, port, None) is True
        assert _leftover_host_port_aims_at_dock("127.0.0.1", port, None) is True
        assert _leftover_host_port_aims_at_dock(None, 9222, None) is False
        assert _leftover_host_port_aims_at_dock("127.0.0.1", port + 1, None) is False
        assert _unregistered_cli_aims_at_dock(
            ["npx", "lighthouse", "https://example.com", "--port", str(port)],
            {}, profile, None,
        ) is True
        assert _unregistered_cli_aims_at_dock(
            ["npx", "chrome-remote-interface", "--host", "127.0.0.1", "--port", str(port)],
            {}, profile, None,
        ) is True
        assert _unregistered_cli_aims_at_dock(
            ["npx", "lighthouse", "--hostname", "10.0.0.5", "--port", str(port)],
            {}, profile, None,
        ) is False
    finally:
        listener.close()


def test_named_listen_host_port_aims_when_persist_is_stale(
    monkeypatch, tmp_path,
):
    """Finding 171: stale persist ``dock_port`` hid leftover ``--port``.

    Finding 156 identified lighthouse / CRI ``--port`` when persist and
    the override both miss. Interrupt still stamps ``dock_port`` from
    ``last_known_dock_cdp_port`` after leftover holds CDP (finding
    165/166). An older persist then rejected ``port != dock_port``
    before named-listen identity, so Take over left writers aimed at
    the live listen. Identity first. 9222, the other family, and LAN
    stay unknown. Do not stamp persist.
    """
    import os
    import socket

    import tools.bot_desktop.browser as bdb
    from tools.browser_tool_session import (
        _leftover_cdp_aims_at_dock,
        _leftover_host_port_aims_at_dock,
        _unregistered_cli_aims_at_dock,
    )

    profile = tmp_path / "browser-profile"
    profile.mkdir()
    os.symlink(f"host-{os.getpid()}", profile / "SingletonLock")
    monkeypatch.setattr(runtime, "state_dir", lambda: tmp_path)
    monkeypatch.setattr(bdb, "profile_dir", lambda: profile)
    monkeypatch.setattr(bdb, "running_instance_cdp_port", lambda *a, **k: None)
    monkeypatch.setattr(
        bdb,
        "_chromium_cmdline_tokens",
        lambda pid: ["chrome", f"--user-data-dir={profile}"],
    )
    listener = socket.socket()
    listener.bind(("127.0.0.1", 0))
    listener.listen(8)
    _drain_listen(listener)
    port = listener.getsockname()[1]
    monkeypatch.setattr(bdb, "_loopback_listen_ports_for_pid", lambda pid: {port, port + 1})
    monkeypatch.setattr(
        bdb,
        "_loopback_listen_targets_for_pid",
        lambda pid: {("127.0.0.1", port), ("::1", port + 1)},
    )
    monkeypatch.setattr(bdb, "_configured_cdp_override_url", lambda: "")
    try:
        bdb.remember_dock_cdp_port(9333)
        assert bdb.last_known_dock_cdp_port() == 9333
        assert bdb.persist_live_dock_cdp_port() is None
        assert bdb.last_known_dock_cdp_port() == 9333
        assert _leftover_cdp_aims_at_dock(f"http://127.0.0.1:{port}", 9333) is True
        assert _leftover_host_port_aims_at_dock(None, port, 9333) is True
        assert _leftover_host_port_aims_at_dock("127.0.0.1", port, 9333) is True
        assert _leftover_host_port_aims_at_dock(None, 9222, 9333) is False
        assert _leftover_host_port_aims_at_dock("127.0.0.1", port + 1, 9333) is False
        assert _leftover_host_port_aims_at_dock("10.0.0.5", port, 9333) is False
        assert _unregistered_cli_aims_at_dock(
            ["npx", "lighthouse", "https://example.com", "--port", str(port)],
            {}, profile, 9333,
        ) is True
        assert _unregistered_cli_aims_at_dock(
            ["npx", "chrome-remote-interface", "--host", "127.0.0.1", "--port", str(port)],
            {}, profile, 9333,
        ) is True
        assert _unregistered_cli_aims_at_dock(
            ["npx", "lighthouse", "--port", "9222", "https://example.com"],
            {}, profile, 9333,
        ) is False
        assert _unregistered_cli_aims_at_dock(
            ["npx", "lighthouse", "--hostname", "10.0.0.5", "--port", str(port)],
            {}, profile, 9333,
        ) is False
    finally:
        listener.close()


def test_named_listen_other_port_aims_when_recover_is_live(
    monkeypatch, tmp_path,
):
    """Finding 173: live recover hid leftover CDP on another this-jar listen.

    ``running_instance_cdp_port`` returns the file-named port when that
    TCP probe works. Leftover ``--cdp`` / lighthouse ``--port`` aimed at
    the inherited other listen then looked like another Chrome — same
    jar, second fd. Named-listen identity for *want*. Do not stamp
    persist to that other listen. 9222 and the other family stay unknown.
    """
    import os
    import socket

    import tools.bot_desktop.browser as bdb
    from tools.browser_tool_session import (
        _cdp_url_is_bot_desktop_browser,
        _leftover_cdp_aims_at_dock,
        _leftover_host_port_aims_at_dock,
        _unregistered_cli_aims_at_dock,
    )

    profile = tmp_path / "browser-profile"
    profile.mkdir()
    os.symlink(f"host-{os.getpid()}", profile / "SingletonLock")
    monkeypatch.setattr(runtime, "state_dir", lambda: tmp_path)
    monkeypatch.setattr(bdb, "profile_dir", lambda: profile)
    monkeypatch.setattr(
        bdb,
        "_chromium_cmdline_tokens",
        lambda pid: ["chrome", f"--user-data-dir={profile}", "--remote-debugging-port=0"],
    )
    live6 = socket.socket(socket.AF_INET6, socket.SOCK_STREAM)
    live6.setsockopt(socket.IPPROTO_IPV6, socket.IPV6_V6ONLY, 1)
    live6.bind(("::1", 0))
    live6.listen(8)
    live = live6.getsockname()[1]
    live4 = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    live4.bind(("127.0.0.1", live))
    live4.listen(8)
    stale6 = socket.socket(socket.AF_INET6, socket.SOCK_STREAM)
    stale6.setsockopt(socket.IPPROTO_IPV6, socket.IPV6_V6ONLY, 1)
    stale6.bind(("::1", 0))
    stale6.listen(8)
    stale = stale6.getsockname()[1]
    stale4 = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    stale4.bind(("127.0.0.1", stale))
    stale4.listen(8)
    _drain_listen(live6)
    _drain_listen(live4)
    _drain_listen(stale6)
    _drain_listen(stale4)
    (profile / "DevToolsActivePort").write_text(
        f"{live}\n/devtools/browser/abc\n", encoding="utf-8",
    )
    monkeypatch.setattr(bdb, "_loopback_listen_ports_for_pid", lambda pid: {live, stale})
    monkeypatch.setattr(
        bdb,
        "_loopback_listen_targets_for_pid",
        lambda pid: {("::1", live), ("::1", stale)},
    )
    monkeypatch.setattr(bdb, "_configured_cdp_override_url", lambda: "")
    try:
        assert bdb.running_instance_cdp_port(str(profile)) == live
        assert bdb.last_known_dock_cdp_port() == live
        assert _cdp_url_is_bot_desktop_browser(f"http://[::1]:{stale}") is True
        assert _cdp_url_is_bot_desktop_browser(str(stale)) is True
        assert _cdp_url_is_bot_desktop_browser(f"http://127.0.0.1:{stale}") is False
        assert _cdp_url_is_bot_desktop_browser("9222") is False
        assert _leftover_cdp_aims_at_dock(f"http://[::1]:{stale}", live) is True
        assert _leftover_host_port_aims_at_dock(None, stale, live) is True
        assert _leftover_host_port_aims_at_dock("::1", stale, live) is True
        assert _leftover_host_port_aims_at_dock("127.0.0.1", stale, live) is False
        assert _leftover_host_port_aims_at_dock(None, 9222, live) is False
        assert _unregistered_cli_aims_at_dock(
            ["npx", "lighthouse", "https://example.com", "--port", str(stale)],
            {}, profile, live,
        ) is True
        assert _unregistered_cli_aims_at_dock(
            ["agent-browser", "--cdp", f"http://[::1]:{stale}", "fill"],
            {}, profile, live,
        ) is True
        assert _unregistered_cli_aims_at_dock(
            ["npx", "lighthouse", "--port", "9222", "https://example.com"],
            {}, profile, live,
        ) is False
        assert bdb.last_known_dock_cdp_port() == live
    finally:
        live6.close()
        live4.close()
        stale6.close()
        stale4.close()


def test_named_listen_does_not_stamp_over_lock_listed_persist(
    monkeypatch, tmp_path,
):
    """Finding 175: persist TCP miss must not let identity stamp file helpers.

    Finding 174 returns None when persist is still on the lock pid and
    only its TCP probe failed. Named-listen identity then treated that
    as a recover miss and stamped leftover ``--cdp`` / lighthouse
    ``--port`` aimed at stale ``DevToolsActivePort`` helpers,
    overwriting ``dock-cdp-port``. Identify the other listen; keep
    persist. 9222 and the other family stay unknown.
    """
    import tools.bot_desktop.browser as bdb
    from tools.browser_tool_session import (
        _cdp_url_is_bot_desktop_browser,
        _leftover_cdp_aims_at_dock,
        _leftover_host_port_aims_at_dock,
        _remembered_dock_attach_port,
        _reset_dock_port_memory_for_tests,
        _unregistered_cli_aims_at_dock,
    )

    monkeypatch.setattr(runtime, "state_dir", lambda: tmp_path)
    monkeypatch.setattr(bdb, "profile_dir", lambda: tmp_path)
    monkeypatch.setattr(bdb, "_lock_pid", lambda d: 4240)
    monkeypatch.setattr(
        bdb,
        "_chromium_cmdline_tokens",
        lambda pid: ["chrome", f"--user-data-dir={tmp_path}"],
    )
    monkeypatch.setattr(
        bdb, "_loopback_listen_ports_for_pid", lambda pid: {9333, 40142},
    )
    monkeypatch.setattr(
        bdb,
        "_loopback_listen_targets_for_pid",
        lambda pid: {("::1", 9333), ("::1", 40142)},
    )
    monkeypatch.setattr(bdb, "_recover_cdp_port_from_singleton", lambda *a, **k: None)
    monkeypatch.setattr(
        bdb,
        "_loopback_listen_inodes_for_port",
        lambda port: {7: {"::1"}} if port == 40141 else {},
    )
    monkeypatch.setattr(
        bdb,
        "_pids_holding_socket_inodes",
        lambda want: {4242: {7}, 4243: {7}} if 7 in want else {},
    )
    monkeypatch.setattr(
        bdb,
        "_cdp_port_reachable",
        lambda port, hosts: port == 40141 and "::1" in hosts,
    )
    monkeypatch.setattr(bdb, "_configured_cdp_override_url", lambda: "")
    _reset_dock_port_memory_for_tests()
    bdb.remember_dock_cdp_port(9333)
    (tmp_path / "DevToolsActivePort").write_text(
        "40141\n/devtools/browser/abc\n", encoding="utf-8",
    )
    assert bdb.lock_listed_persist_port() == 9333
    assert bdb.running_instance_cdp_port(str(tmp_path)) is None
    assert bdb.last_known_dock_cdp_port() == 9333
    assert _cdp_url_is_bot_desktop_browser("http://[::1]:40141") is True
    assert _cdp_url_is_bot_desktop_browser("40141") is True
    assert _cdp_url_is_bot_desktop_browser("http://[::1]:9333") is True
    assert _cdp_url_is_bot_desktop_browser("http://127.0.0.1:40141") is False
    assert _cdp_url_is_bot_desktop_browser("9222") is False
    assert _leftover_cdp_aims_at_dock("http://[::1]:40141", 9333) is True
    assert _leftover_host_port_aims_at_dock(None, 40141, 9333) is True
    assert _leftover_host_port_aims_at_dock("::1", 40141, 9333) is True
    assert _leftover_host_port_aims_at_dock("127.0.0.1", 40141, 9333) is False
    assert _leftover_host_port_aims_at_dock(None, 9222, 9333) is False
    assert _unregistered_cli_aims_at_dock(
        ["npx", "lighthouse", "https://example.com", "--port", "40141"],
        {}, tmp_path, 9333,
    ) is True
    assert _unregistered_cli_aims_at_dock(
        ["agent-browser", "--cdp", "http://[::1]:40141", "fill"],
        {}, tmp_path, 9333,
    ) is True
    assert _unregistered_cli_aims_at_dock(
        ["npx", "chrome-remote-interface", "--port", "40141", "inspect"],
        {}, tmp_path, 9333,
    ) is True
    assert _unregistered_cli_aims_at_dock(
        ["npx", "lighthouse", "--port", "9222", "https://example.com"],
        {}, tmp_path, 9333,
    ) is False
    assert bdb.last_known_dock_cdp_port() == 9333
    assert _remembered_dock_attach_port() == 9333

    # Finding 176: persist_live must not fall through to a configured
    # leftover file-helper listen and overwrite lock-listed persist.
    monkeypatch.setattr(
        bdb, "_configured_cdp_override_url", lambda: "http://[::1]:40141",
    )
    assert bdb._configured_listen_port_for_this_jar() is None
    assert bdb.persist_live_dock_cdp_port() is None
    assert bdb.last_known_dock_cdp_port() == 9333
    assert _remembered_dock_attach_port() == 9333
    assert _cdp_url_is_bot_desktop_browser("http://[::1]:40141") is True
    assert bdb.last_known_dock_cdp_port() == 9333


def test_memory_only_persist_does_not_shop_file_helpers(
    monkeypatch, tmp_path,
):
    """Finding 177: persist file miss must not let 164 stamp leftover helpers.

    Finding 169 already treats in-process memory as a persist candidate
    when ``dock-cdp-port`` is missing. ``lock_listed_persist_port``
    read the file only, so a remember miss made 164 / configured treat
    lock-listed chrome as empty and stamp leftover file helpers.
    Memory still on the lock is persist. Identity leftover on those
    helpers; keep memory. 9222 and the other family stay unknown.
    """
    import tools.bot_desktop.browser as bdb
    from hermes_constants import hermes_home_key
    from tools.browser_tool_session import (
        _cdp_url_is_bot_desktop_browser,
        _last_dock_cdp_port,
        _leftover_cdp_aims_at_dock,
        _leftover_host_port_aims_at_dock,
        _remembered_dock_attach_port,
        _reset_dock_port_memory_for_tests,
        _unregistered_cli_aims_at_dock,
    )

    monkeypatch.setattr(runtime, "state_dir", lambda: tmp_path)
    monkeypatch.setattr(bdb, "profile_dir", lambda: tmp_path)
    monkeypatch.setattr(bdb, "_lock_pid", lambda d: 4240)
    monkeypatch.setattr(
        bdb,
        "_chromium_cmdline_tokens",
        lambda pid: ["chrome", f"--user-data-dir={tmp_path}"],
    )
    monkeypatch.setattr(
        bdb, "_loopback_listen_ports_for_pid", lambda pid: {9333, 40142},
    )
    monkeypatch.setattr(
        bdb,
        "_loopback_listen_targets_for_pid",
        lambda pid: {("::1", 9333), ("::1", 40142)},
    )
    monkeypatch.setattr(bdb, "_recover_cdp_port_from_singleton", lambda *a, **k: None)
    monkeypatch.setattr(
        bdb,
        "_loopback_listen_inodes_for_port",
        lambda port: {7: {"::1"}} if port == 40141 else {},
    )
    monkeypatch.setattr(
        bdb,
        "_pids_holding_socket_inodes",
        lambda want: {4242: {7}, 4243: {7}} if 7 in want else {},
    )
    monkeypatch.setattr(
        bdb,
        "_cdp_port_reachable",
        lambda port, hosts: port == 40141 and "::1" in hosts,
    )
    monkeypatch.setattr(
        bdb, "_configured_cdp_override_url", lambda: "http://[::1]:40141",
    )
    _reset_dock_port_memory_for_tests()
    _last_dock_cdp_port[hermes_home_key()] = 9333
    (tmp_path / "DevToolsActivePort").write_text(
        "40141\n/devtools/browser/abc\n", encoding="utf-8",
    )
    assert bdb.last_known_dock_cdp_port() is None
    assert bdb.lock_listed_persist_port() == 9333
    assert bdb.running_instance_cdp_port(str(tmp_path)) is None
    assert bdb._configured_listen_port_for_this_jar() is None
    assert bdb.persist_live_dock_cdp_port() is None
    assert bdb.last_known_dock_cdp_port() is None
    assert _last_dock_cdp_port.get(hermes_home_key()) == 9333
    assert _remembered_dock_attach_port() == 9333
    assert _cdp_url_is_bot_desktop_browser("http://[::1]:40141") is True
    assert _cdp_url_is_bot_desktop_browser("40141") is True
    assert _cdp_url_is_bot_desktop_browser("http://[::1]:9333") is True
    assert _cdp_url_is_bot_desktop_browser("http://127.0.0.1:40141") is False
    assert _cdp_url_is_bot_desktop_browser("9222") is False
    assert _leftover_cdp_aims_at_dock("http://[::1]:40141", 9333) is True
    assert _leftover_host_port_aims_at_dock(None, 40141, 9333) is True
    assert _leftover_host_port_aims_at_dock("::1", 40141, 9333) is True
    assert _leftover_host_port_aims_at_dock("127.0.0.1", 40141, 9333) is False
    assert _leftover_host_port_aims_at_dock(None, 9222, 9333) is False
    assert _unregistered_cli_aims_at_dock(
        ["npx", "lighthouse", "https://example.com", "--port", "40141"],
        {}, tmp_path, 9333,
    ) is True
    assert _unregistered_cli_aims_at_dock(
        ["agent-browser", "--cdp", "http://[::1]:40141", "fill"],
        {}, tmp_path, 9333,
    ) is True
    assert _unregistered_cli_aims_at_dock(
        ["npx", "chrome-remote-interface", "--port", "40141", "inspect"],
        {}, tmp_path, 9333,
    ) is True
    assert _unregistered_cli_aims_at_dock(
        ["npx", "lighthouse", "--port", "9222", "https://example.com"],
        {}, tmp_path, 9333,
    ) is False
    assert bdb.last_known_dock_cdp_port() is None
    assert _last_dock_cdp_port.get(hermes_home_key()) == 9333
    assert _remembered_dock_attach_port() == 9333

    # File names leftover helpers (not on the lock). Memory still names
    # lock-listed chrome. File-first lock_listed used to miss and 164
    # restamped the leftover.
    bdb.remember_dock_cdp_port(40141)
    _last_dock_cdp_port[hermes_home_key()] = 9333
    assert bdb.last_known_dock_cdp_port() == 40141
    assert bdb.lock_listed_persist_port() == 9333
    assert bdb.running_instance_cdp_port(str(tmp_path)) is None
    assert bdb.persist_live_dock_cdp_port() is None
    assert bdb.last_known_dock_cdp_port() == 40141
    assert _last_dock_cdp_port.get(hermes_home_key()) == 9333
    assert _cdp_url_is_bot_desktop_browser("http://[::1]:40141") is True
    assert _last_dock_cdp_port.get(hermes_home_key()) == 9333


def test_poisoned_persist_file_does_not_hide_lock_listed_attach(
    monkeypatch, tmp_path,
):
    """Finding 178: poisoned persist file must not attach leftover helpers.

    Pre-177 persist_live left ``dock-cdp-port`` on leftover file
    helpers while memory still named lock-listed chrome. Finding
    177 stops new stamps; attach still preferred the file.
    Identify leftover on those helpers; attach chrome. 9222 and
    the other family stay unknown.
    """
    import tools.bot_desktop.browser as bdb
    from hermes_constants import hermes_home_key
    from tools.browser_tool_session import (
        _cdp_url_is_bot_desktop_browser,
        _last_dock_cdp_port,
        _leftover_cdp_aims_at_dock,
        _leftover_host_port_aims_at_dock,
        _remembered_dock_attach_port,
        _reset_dock_port_memory_for_tests,
        _unregistered_cli_aims_at_dock,
    )

    monkeypatch.setattr(runtime, "state_dir", lambda: tmp_path)
    monkeypatch.setattr(bdb, "profile_dir", lambda: tmp_path)
    monkeypatch.setattr(bdb, "_lock_pid", lambda d: 4240)
    monkeypatch.setattr(
        bdb,
        "_chromium_cmdline_tokens",
        lambda pid: ["chrome", f"--user-data-dir={tmp_path}"],
    )
    monkeypatch.setattr(
        bdb, "_loopback_listen_ports_for_pid", lambda pid: {9333, 40142},
    )
    monkeypatch.setattr(
        bdb,
        "_loopback_listen_targets_for_pid",
        lambda pid: {("::1", 9333), ("::1", 40142)},
    )
    monkeypatch.setattr(bdb, "_recover_cdp_port_from_singleton", lambda *a, **k: None)
    monkeypatch.setattr(
        bdb,
        "_loopback_listen_inodes_for_port",
        lambda port: {7: {"::1"}} if port == 40141 else {},
    )
    monkeypatch.setattr(
        bdb,
        "_pids_holding_socket_inodes",
        lambda want: {4242: {7}, 4243: {7}} if 7 in want else {},
    )
    monkeypatch.setattr(
        bdb,
        "_cdp_port_reachable",
        lambda port, hosts: port == 40141 and "::1" in hosts,
    )
    monkeypatch.setattr(bdb, "_configured_cdp_override_url", lambda: "")
    _reset_dock_port_memory_for_tests()
    bdb.remember_dock_cdp_port(40141)
    _last_dock_cdp_port[hermes_home_key()] = 9333
    (tmp_path / "DevToolsActivePort").write_text(
        "40141\n/devtools/browser/abc\n", encoding="utf-8",
    )
    assert bdb.last_known_dock_cdp_port() == 40141
    assert bdb.lock_listed_persist_port() == 9333
    assert bdb.running_instance_cdp_port(str(tmp_path)) is None
    assert bdb.persist_live_dock_cdp_port() is None
    assert bdb.last_known_dock_cdp_port() == 40141
    assert _last_dock_cdp_port.get(hermes_home_key()) == 9333
    assert _remembered_dock_attach_port() == 9333
    assert _cdp_url_is_bot_desktop_browser("http://[::1]:40141") is True
    assert _cdp_url_is_bot_desktop_browser("40141") is True
    assert _cdp_url_is_bot_desktop_browser("http://[::1]:9333") is True
    assert _cdp_url_is_bot_desktop_browser("http://127.0.0.1:40141") is False
    assert _cdp_url_is_bot_desktop_browser("9222") is False
    assert _leftover_cdp_aims_at_dock("http://[::1]:40141", 9333) is True
    assert _leftover_host_port_aims_at_dock(None, 40141, 9333) is True
    assert _leftover_host_port_aims_at_dock("::1", 40141, 9333) is True
    assert _leftover_host_port_aims_at_dock("127.0.0.1", 40141, 9333) is False
    assert _leftover_host_port_aims_at_dock(None, 9222, 9333) is False
    assert _unregistered_cli_aims_at_dock(
        ["npx", "lighthouse", "https://example.com", "--port", "40141"],
        {}, tmp_path, 9333,
    ) is True
    assert _unregistered_cli_aims_at_dock(
        ["agent-browser", "--cdp", "http://[::1]:40141", "fill"],
        {}, tmp_path, 9333,
    ) is True
    assert _unregistered_cli_aims_at_dock(
        ["npx", "chrome-remote-interface", "--port", "40141", "inspect"],
        {}, tmp_path, 9333,
    ) is True
    assert _unregistered_cli_aims_at_dock(
        ["npx", "lighthouse", "--port", "9222", "https://example.com"],
        {}, tmp_path, 9333,
    ) is False
    assert _remembered_dock_attach_port() == 9333
    assert bdb.last_known_dock_cdp_port() == 40141
    assert _last_dock_cdp_port.get(hermes_home_key()) == 9333


def test_missing_lock_does_not_shop_file_helpers_over_memory(monkeypatch, tmp_path):
    """Finding 179: missing SingletonLock must not hide lock-listed memory.

    Finding 161: Take over can unlink the lock while chrome still
    inode-listens. Finding 177 / 178 required that pid, so a poisoned
    persist file plus leftover ``DevToolsActivePort`` helpers made
    persist_live / attach treat memory-named chrome as empty.
    Identify leftover on those helpers; attach chrome. Finding 86
    still stamps helpers when memory is missing. 9222 and the other
    family stay unknown.
    """
    import tools.bot_desktop.browser as bdb
    from hermes_constants import hermes_home_key
    from tools.browser_tool_session import (
        _cdp_url_is_bot_desktop_browser,
        _last_dock_cdp_port,
        _leftover_cdp_aims_at_dock,
        _leftover_host_port_aims_at_dock,
        _remembered_dock_attach_port,
        _reset_dock_port_memory_for_tests,
        _unregistered_cli_aims_at_dock,
    )

    monkeypatch.setattr(runtime, "state_dir", lambda: tmp_path)
    monkeypatch.setattr(bdb, "profile_dir", lambda: tmp_path)
    monkeypatch.setattr(bdb, "_lock_pid", lambda d: None)
    monkeypatch.setattr(
        bdb,
        "_chromium_cmdline_tokens",
        lambda pid: ["chrome", f"--user-data-dir={tmp_path}"],
    )
    monkeypatch.setattr(
        bdb, "_loopback_listen_ports_for_pid", lambda pid: set(),
    )
    monkeypatch.setattr(
        bdb, "_loopback_listen_targets_for_pid", lambda pid: set(),
    )
    monkeypatch.setattr(bdb, "_recover_cdp_port_from_singleton", lambda *a, **k: None)

    def _inodes(port):
        if port == 40141:
            return {7: {"::1"}}
        if port == 9333:
            return {8: {"::1"}}
        return {}

    def _holders(want):
        out = {}
        if 7 in want:
            out[4242] = {7}
            out[4243] = {7}
        if 8 in want:
            out[4240] = {8}
        return out

    monkeypatch.setattr(bdb, "_loopback_listen_inodes_for_port", _inodes)
    monkeypatch.setattr(bdb, "_pids_holding_socket_inodes", _holders)
    monkeypatch.setattr(
        bdb,
        "_cdp_port_reachable",
        lambda port, hosts: port == 40141 and "::1" in hosts,
    )
    monkeypatch.setattr(
        bdb, "_configured_cdp_override_url",
        lambda: "http://[::1]:40141",
    )
    _reset_dock_port_memory_for_tests()
    bdb.remember_dock_cdp_port(40141)
    _last_dock_cdp_port[hermes_home_key()] = 9333
    (tmp_path / "DevToolsActivePort").write_text(
        "40141\n/devtools/browser/abc\n", encoding="utf-8",
    )
    assert bdb.last_known_dock_cdp_port() == 40141
    assert bdb.lock_listed_persist_port() == 9333
    assert bdb.file_named_dock_listen_port() == 40141
    assert bdb.running_instance_cdp_port(str(tmp_path)) is None
    assert bdb.persist_live_dock_cdp_port() is None
    assert bdb.last_known_dock_cdp_port() == 40141
    assert _last_dock_cdp_port.get(hermes_home_key()) == 9333
    assert _remembered_dock_attach_port() == 9333
    assert _cdp_url_is_bot_desktop_browser("http://[::1]:40141") is True
    assert _cdp_url_is_bot_desktop_browser("40141") is True
    assert _cdp_url_is_bot_desktop_browser("http://[::1]:9333") is True
    assert _cdp_url_is_bot_desktop_browser("http://127.0.0.1:40141") is False
    assert _cdp_url_is_bot_desktop_browser("9222") is False
    assert _leftover_cdp_aims_at_dock("http://[::1]:40141", 9333) is True
    assert _leftover_host_port_aims_at_dock(None, 40141, 9333) is True
    assert _leftover_host_port_aims_at_dock("::1", 40141, 9333) is True
    assert _leftover_host_port_aims_at_dock("127.0.0.1", 40141, 9333) is False
    assert _leftover_host_port_aims_at_dock(None, 9222, 9333) is False
    assert _unregistered_cli_aims_at_dock(
        ["npx", "lighthouse", "https://example.com", "--port", "40141"],
        {}, tmp_path, 9333,
    ) is True
    assert _unregistered_cli_aims_at_dock(
        ["agent-browser", "--cdp", "http://[::1]:40141", "fill"],
        {}, tmp_path, 9333,
    ) is True
    assert _unregistered_cli_aims_at_dock(
        ["npx", "chrome-remote-interface", "--port", "40141", "inspect"],
        {}, tmp_path, 9333,
    ) is True
    assert _unregistered_cli_aims_at_dock(
        ["npx", "lighthouse", "--port", "9222", "https://example.com"],
        {}, tmp_path, 9333,
    ) is False
    assert _remembered_dock_attach_port() == 9333
    assert bdb.last_known_dock_cdp_port() == 40141
    assert _last_dock_cdp_port.get(hermes_home_key()) == 9333


def test_persist_live_syncs_memory_so_missing_lock_keeps_chrome(
    monkeypatch, tmp_path,
):
    """Finding 180: persist_live must sync leftover memory before unlink.

    Finding 174 stamps chrome while the lock still lists persist and
    not leftover ``DevToolsActivePort`` helpers. Memory stayed on the
    leftover, so after Take over unlinked the lock, persist_live /
    attach / leftover identity followed those helpers. Sync memory
    on a live stamp. Identify leftover on the stale file; attach
    chrome. 9222 and the other family stay unknown.
    """
    import tools.bot_desktop.browser as bdb
    from hermes_constants import hermes_home_key
    from tools.browser_tool_session import (
        _cdp_url_is_bot_desktop_browser,
        _last_dock_cdp_port,
        _leftover_cdp_aims_at_dock,
        _leftover_host_port_aims_at_dock,
        _remembered_dock_attach_port,
        _reset_dock_port_memory_for_tests,
        _unregistered_cli_aims_at_dock,
    )

    monkeypatch.setattr(runtime, "state_dir", lambda: tmp_path)
    monkeypatch.setattr(bdb, "profile_dir", lambda: tmp_path)
    monkeypatch.setattr(bdb, "_lock_pid", lambda d: 4240)
    monkeypatch.setattr(
        bdb,
        "_chromium_cmdline_tokens",
        lambda pid: ["chrome", f"--user-data-dir={tmp_path}"],
    )
    monkeypatch.setattr(
        bdb, "_loopback_listen_ports_for_pid",
        lambda pid: {9333} if pid == 4240 else set(),
    )
    monkeypatch.setattr(
        bdb, "_loopback_listen_targets_for_pid",
        lambda pid: {("::1", 9333)} if pid == 4240 else set(),
    )
    monkeypatch.setattr(bdb, "_recover_cdp_port_from_singleton", lambda *a, **k: None)

    def _inodes(port):
        if port == 40141:
            return {7: {"::1"}}
        if port == 9333:
            return {8: {"::1"}}
        return {}

    def _holders(want):
        out = {}
        if 7 in want:
            out[4242] = {7}
            out[4243] = {7}
        if 8 in want:
            out[4240] = {8}
        return out

    monkeypatch.setattr(bdb, "_loopback_listen_inodes_for_port", _inodes)
    monkeypatch.setattr(bdb, "_pids_holding_socket_inodes", _holders)
    monkeypatch.setattr(
        bdb,
        "_cdp_port_reachable",
        lambda port, hosts: port == 9333 and "::1" in hosts,
    )
    monkeypatch.setattr(
        bdb, "_configured_cdp_override_url",
        lambda: "http://[::1]:40141",
    )
    _reset_dock_port_memory_for_tests()
    bdb.remember_dock_cdp_port(9333)
    _last_dock_cdp_port[hermes_home_key()] = 40141
    (tmp_path / "DevToolsActivePort").write_text(
        "40141\n/devtools/browser/abc\n", encoding="utf-8",
    )
    assert bdb.persist_live_dock_cdp_port() == 9333
    assert _last_dock_cdp_port.get(hermes_home_key()) == 9333

    monkeypatch.setattr(bdb, "_lock_pid", lambda d: None)
    monkeypatch.setattr(bdb, "_loopback_listen_ports_for_pid", lambda pid: set())
    monkeypatch.setattr(bdb, "_loopback_listen_targets_for_pid", lambda pid: set())
    assert bdb.lock_listed_persist_port() == 9333
    assert bdb.file_named_dock_listen_port() == 40141
    # Finding 185: chrome still inode-listens and TCP works.
    assert bdb.persist_live_dock_cdp_port() == 9333
    assert _remembered_dock_attach_port() == 9333
    assert _cdp_url_is_bot_desktop_browser("http://[::1]:40141") is True
    assert _cdp_url_is_bot_desktop_browser("40141") is True
    assert _cdp_url_is_bot_desktop_browser("http://[::1]:9333") is True
    assert _cdp_url_is_bot_desktop_browser("http://127.0.0.1:40141") is False
    assert _cdp_url_is_bot_desktop_browser("9222") is False
    assert _leftover_cdp_aims_at_dock("http://[::1]:40141", 9333) is True
    assert _leftover_host_port_aims_at_dock(None, 40141, 9333) is True
    assert _leftover_host_port_aims_at_dock("::1", 40141, 9333) is True
    assert _leftover_host_port_aims_at_dock("127.0.0.1", 40141, 9333) is False
    assert _leftover_host_port_aims_at_dock(None, 9222, 9333) is False
    assert _unregistered_cli_aims_at_dock(
        ["npx", "lighthouse", "https://example.com", "--port", "40141"],
        {}, tmp_path, 9333,
    ) is True
    assert _unregistered_cli_aims_at_dock(
        ["agent-browser", "--cdp", "http://[::1]:40141", "fill"],
        {}, tmp_path, 9333,
    ) is True
    assert _unregistered_cli_aims_at_dock(
        ["npx", "chrome-remote-interface", "--port", "40141", "inspect"],
        {}, tmp_path, 9333,
    ) is True
    assert _unregistered_cli_aims_at_dock(
        ["npx", "lighthouse", "--port", "9222", "https://example.com"],
        {}, tmp_path, 9333,
    ) is False
    assert _remembered_dock_attach_port() == 9333
    assert bdb.last_known_dock_cdp_port() == 9333
    assert _last_dock_cdp_port.get(hermes_home_key()) == 9333


def test_restart_empty_memory_does_not_stamp_helpers_over_persist_file(
    monkeypatch, tmp_path,
):
    """Finding 181: process restart must not hide persist-file chrome.

    A new process has empty ``_last_dock_cdp_port``. Finding 179 then
    treated file-only persist as empty, so persist_live / attach
    stamped leftover ``DevToolsActivePort`` helpers over chrome.
    Identify leftover on those helpers; attach chrome. Finding 86
    still stamps when persist equals DevTools. 9222 and the other
    family stay unknown.
    """
    import tools.bot_desktop.browser as bdb
    from hermes_constants import hermes_home_key
    from tools.browser_tool_session import (
        _cdp_url_is_bot_desktop_browser,
        _last_dock_cdp_port,
        _leftover_cdp_aims_at_dock,
        _leftover_host_port_aims_at_dock,
        _remembered_dock_attach_port,
        _reset_dock_port_memory_for_tests,
        _unregistered_cli_aims_at_dock,
    )

    monkeypatch.setattr(runtime, "state_dir", lambda: tmp_path)
    monkeypatch.setattr(bdb, "profile_dir", lambda: tmp_path)
    monkeypatch.setattr(bdb, "_lock_pid", lambda d: None)
    monkeypatch.setattr(
        bdb,
        "_chromium_cmdline_tokens",
        lambda pid: ["chrome", f"--user-data-dir={tmp_path}"],
    )
    monkeypatch.setattr(
        bdb, "_loopback_listen_ports_for_pid", lambda pid: set(),
    )
    monkeypatch.setattr(
        bdb, "_loopback_listen_targets_for_pid", lambda pid: set(),
    )
    monkeypatch.setattr(bdb, "_recover_cdp_port_from_singleton", lambda *a, **k: None)

    def _inodes(port):
        if port == 40141:
            return {7: {"::1"}}
        if port == 9333:
            return {8: {"::1"}}
        return {}

    def _holders(want):
        out = {}
        if 7 in want:
            out[4242] = {7}
            out[4243] = {7}
        if 8 in want:
            out[4240] = {8}
        return out

    monkeypatch.setattr(bdb, "_loopback_listen_inodes_for_port", _inodes)
    monkeypatch.setattr(bdb, "_pids_holding_socket_inodes", _holders)
    monkeypatch.setattr(
        bdb,
        "_cdp_port_reachable",
        lambda port, hosts: port == 40141 and "::1" in hosts,
    )
    monkeypatch.setattr(
        bdb, "_configured_cdp_override_url",
        lambda: "http://[::1]:40141",
    )
    _reset_dock_port_memory_for_tests()
    bdb.remember_dock_cdp_port(9333)
    (tmp_path / "DevToolsActivePort").write_text(
        "40141\n/devtools/browser/abc\n", encoding="utf-8",
    )
    assert _last_dock_cdp_port.get(hermes_home_key()) is None
    assert bdb.lock_listed_persist_port() == 9333
    assert bdb.file_named_dock_listen_port() == 40141
    assert bdb.running_instance_cdp_port(str(tmp_path)) is None
    assert bdb.persist_live_dock_cdp_port() is None
    assert bdb.last_known_dock_cdp_port() == 9333
    assert _last_dock_cdp_port.get(hermes_home_key()) is None
    assert _remembered_dock_attach_port() == 9333
    assert _cdp_url_is_bot_desktop_browser("http://[::1]:40141") is True
    assert _cdp_url_is_bot_desktop_browser("40141") is True
    assert _cdp_url_is_bot_desktop_browser("http://[::1]:9333") is True
    assert _cdp_url_is_bot_desktop_browser("http://127.0.0.1:40141") is False
    assert _cdp_url_is_bot_desktop_browser("9222") is False
    assert _leftover_cdp_aims_at_dock("http://[::1]:40141", 9333) is True
    assert _leftover_host_port_aims_at_dock(None, 40141, 9333) is True
    assert _leftover_host_port_aims_at_dock("::1", 40141, 9333) is True
    assert _leftover_host_port_aims_at_dock("127.0.0.1", 40141, 9333) is False
    assert _leftover_host_port_aims_at_dock(None, 9222, 9333) is False
    assert _unregistered_cli_aims_at_dock(
        ["npx", "lighthouse", "https://example.com", "--port", "40141"],
        {}, tmp_path, 9333,
    ) is True
    assert _unregistered_cli_aims_at_dock(
        ["agent-browser", "--cdp", "http://[::1]:40141", "fill"],
        {}, tmp_path, 9333,
    ) is True
    assert _unregistered_cli_aims_at_dock(
        ["npx", "chrome-remote-interface", "--port", "40141", "inspect"],
        {}, tmp_path, 9333,
    ) is True
    assert _unregistered_cli_aims_at_dock(
        ["npx", "lighthouse", "--port", "9222", "https://example.com"],
        {}, tmp_path, 9333,
    ) is False
    assert _remembered_dock_attach_port() == 9333
    assert bdb.last_known_dock_cdp_port() == 9333
    assert _last_dock_cdp_port.get(hermes_home_key()) is None


def test_running_instance_syncs_memory_so_missing_lock_keeps_chrome(
    monkeypatch, tmp_path,
):
    """Finding 182: running_instance must sync leftover memory before unlink.

    Finding 174 stamps chrome while the lock still lists persist and
    not leftover ``DevToolsActivePort`` helpers. Agent attach calls
    ``running_instance_cdp_port`` without persist_live. Memory stayed
    on the leftover, so after Take over unlinked the lock, persist_live
    / attach / leftover identity followed those helpers. Sync memory
    on a live stamp. Identify leftover on the stale file; attach
    chrome. 9222 and the other family stay unknown.
    """
    import tools.bot_desktop.browser as bdb
    from hermes_constants import hermes_home_key
    from tools.browser_tool_session import (
        _cdp_url_is_bot_desktop_browser,
        _last_dock_cdp_port,
        _leftover_cdp_aims_at_dock,
        _leftover_host_port_aims_at_dock,
        _remembered_dock_attach_port,
        _reset_dock_port_memory_for_tests,
        _unregistered_cli_aims_at_dock,
    )

    monkeypatch.setattr(runtime, "state_dir", lambda: tmp_path)
    monkeypatch.setattr(bdb, "profile_dir", lambda: tmp_path)
    monkeypatch.setattr(bdb, "_lock_pid", lambda d: 4240)
    monkeypatch.setattr(
        bdb,
        "_chromium_cmdline_tokens",
        lambda pid: ["chrome", f"--user-data-dir={tmp_path}"],
    )
    monkeypatch.setattr(
        bdb, "_loopback_listen_ports_for_pid",
        lambda pid: {9333} if pid == 4240 else set(),
    )
    monkeypatch.setattr(
        bdb, "_loopback_listen_targets_for_pid",
        lambda pid: {("::1", 9333)} if pid == 4240 else set(),
    )
    monkeypatch.setattr(bdb, "_recover_cdp_port_from_singleton", lambda *a, **k: None)

    def _inodes(port):
        if port == 40141:
            return {7: {"::1"}}
        if port == 9333:
            return {8: {"::1"}}
        return {}

    def _holders(want):
        out = {}
        if 7 in want:
            out[4242] = {7}
            out[4243] = {7}
        if 8 in want:
            out[4240] = {8}
        return out

    monkeypatch.setattr(bdb, "_loopback_listen_inodes_for_port", _inodes)
    monkeypatch.setattr(bdb, "_pids_holding_socket_inodes", _holders)
    monkeypatch.setattr(
        bdb,
        "_cdp_port_reachable",
        lambda port, hosts: port == 9333 and "::1" in hosts,
    )
    monkeypatch.setattr(
        bdb, "_configured_cdp_override_url",
        lambda: "http://[::1]:40141",
    )
    _reset_dock_port_memory_for_tests()
    bdb.remember_dock_cdp_port(9333)
    _last_dock_cdp_port[hermes_home_key()] = 40141
    (tmp_path / "DevToolsActivePort").write_text(
        "40141\n/devtools/browser/abc\n", encoding="utf-8",
    )
    assert bdb.running_instance_cdp_port(str(tmp_path)) == 9333
    assert _last_dock_cdp_port.get(hermes_home_key()) == 9333

    monkeypatch.setattr(bdb, "_lock_pid", lambda d: None)
    monkeypatch.setattr(bdb, "_loopback_listen_ports_for_pid", lambda pid: set())
    monkeypatch.setattr(bdb, "_loopback_listen_targets_for_pid", lambda pid: set())
    assert bdb.lock_listed_persist_port() == 9333
    assert bdb.file_named_dock_listen_port() == 40141
    # Finding 185: chrome still inode-listens and TCP works.
    assert bdb.persist_live_dock_cdp_port() == 9333
    assert _remembered_dock_attach_port() == 9333
    assert _cdp_url_is_bot_desktop_browser("http://[::1]:40141") is True
    assert _cdp_url_is_bot_desktop_browser("40141") is True
    assert _cdp_url_is_bot_desktop_browser("http://[::1]:9333") is True
    assert _cdp_url_is_bot_desktop_browser("http://127.0.0.1:40141") is False
    assert _cdp_url_is_bot_desktop_browser("9222") is False
    assert _leftover_cdp_aims_at_dock("http://[::1]:40141", 9333) is True
    assert _leftover_host_port_aims_at_dock(None, 40141, 9333) is True
    assert _leftover_host_port_aims_at_dock("::1", 40141, 9333) is True
    assert _leftover_host_port_aims_at_dock("127.0.0.1", 40141, 9333) is False
    assert _leftover_host_port_aims_at_dock(None, 9222, 9333) is False
    assert _unregistered_cli_aims_at_dock(
        ["npx", "lighthouse", "https://example.com", "--port", "40141"],
        {}, tmp_path, 9333,
    ) is True
    assert _unregistered_cli_aims_at_dock(
        ["agent-browser", "--cdp", "http://[::1]:40141", "fill"],
        {}, tmp_path, 9333,
    ) is True
    assert _unregistered_cli_aims_at_dock(
        ["npx", "chrome-remote-interface", "--port", "40141", "inspect"],
        {}, tmp_path, 9333,
    ) is True
    assert _unregistered_cli_aims_at_dock(
        ["npx", "lighthouse", "--port", "9222", "https://example.com"],
        {}, tmp_path, 9333,
    ) is False
    assert _remembered_dock_attach_port() == 9333
    assert bdb.last_known_dock_cdp_port() == 9333
    assert _last_dock_cdp_port.get(hermes_home_key()) == 9333


def test_lock_listed_leftover_file_does_not_hide_persist_chrome(
    monkeypatch, tmp_path,
):
    """Finding 183: lock listing leftover DevTools hid persist chrome.

    Chrome inherited leftover ``DevToolsActivePort`` fd, so the lock
    lists persist chrome and the leftover file. File TCP then stamped
    helpers over unique persist chrome, and attach 172 preferred the
    file. Identify leftover on the stale file; attach chrome. 9222
    and the other family stay unknown.
    """
    import tools.bot_desktop.browser as bdb
    from hermes_constants import hermes_home_key
    from tools.browser_tool_session import (
        _cdp_url_is_bot_desktop_browser,
        _last_dock_cdp_port,
        _leftover_cdp_aims_at_dock,
        _leftover_host_port_aims_at_dock,
        _remembered_dock_attach_port,
        _reset_dock_port_memory_for_tests,
        _unregistered_cli_aims_at_dock,
    )

    monkeypatch.setattr(runtime, "state_dir", lambda: tmp_path)
    monkeypatch.setattr(bdb, "profile_dir", lambda: tmp_path)
    monkeypatch.setattr(bdb, "_lock_pid", lambda d: 4240)
    monkeypatch.setattr(
        bdb,
        "_chromium_cmdline_tokens",
        lambda pid: ["chrome", f"--user-data-dir={tmp_path}"],
    )
    monkeypatch.setattr(
        bdb, "_loopback_listen_ports_for_pid",
        lambda pid: {9333, 40141} if pid == 4240 else set(),
    )
    monkeypatch.setattr(
        bdb, "_loopback_listen_targets_for_pid",
        lambda pid: {("::1", 9333), ("::1", 40141)} if pid == 4240 else set(),
    )
    monkeypatch.setattr(bdb, "_recover_cdp_port_from_singleton", lambda *a, **k: None)

    def _inodes(port):
        if port == 40141:
            return {7: {"::1"}}
        if port == 9333:
            return {8: {"::1"}}
        return {}

    def _holders(want):
        out = {}
        if 7 in want:
            out[4240] = {7}
            out[4242] = {7}
            out[4243] = {7}
        if 8 in want:
            out[4240] = {8}
        return out

    monkeypatch.setattr(bdb, "_loopback_listen_inodes_for_port", _inodes)
    monkeypatch.setattr(bdb, "_pids_holding_socket_inodes", _holders)
    monkeypatch.setattr(
        bdb,
        "_cdp_port_reachable",
        lambda port, hosts: port in (9333, 40141) and "::1" in hosts,
    )
    monkeypatch.setattr(
        bdb, "_configured_cdp_override_url",
        lambda: "http://[::1]:40141",
    )
    _reset_dock_port_memory_for_tests()
    bdb.remember_dock_cdp_port(9333)
    _last_dock_cdp_port[hermes_home_key()] = 9333
    (tmp_path / "DevToolsActivePort").write_text(
        "40141\n/devtools/browser/abc\n", encoding="utf-8",
    )
    assert bdb.running_instance_cdp_port(str(tmp_path)) == 9333
    assert bdb.last_known_dock_cdp_port() == 9333
    assert _last_dock_cdp_port.get(hermes_home_key()) == 9333
    assert _remembered_dock_attach_port() == 9333
    assert bdb.persist_live_dock_cdp_port() == 9333
    assert _cdp_url_is_bot_desktop_browser("http://[::1]:40141") is True
    assert _cdp_url_is_bot_desktop_browser("40141") is True
    assert _cdp_url_is_bot_desktop_browser("http://[::1]:9333") is True
    assert _cdp_url_is_bot_desktop_browser("http://127.0.0.1:40141") is False
    assert _cdp_url_is_bot_desktop_browser("9222") is False
    assert _leftover_cdp_aims_at_dock("http://[::1]:40141", 9333) is True
    assert _leftover_host_port_aims_at_dock(None, 40141, 9333) is True
    assert _leftover_host_port_aims_at_dock("::1", 40141, 9333) is True
    assert _leftover_host_port_aims_at_dock("127.0.0.1", 40141, 9333) is False
    assert _leftover_host_port_aims_at_dock(None, 9222, 9333) is False
    assert _unregistered_cli_aims_at_dock(
        ["npx", "lighthouse", "https://example.com", "--port", "40141"],
        {}, tmp_path, 9333,
    ) is True
    assert _unregistered_cli_aims_at_dock(
        ["agent-browser", "--cdp", "http://[::1]:40141", "fill"],
        {}, tmp_path, 9333,
    ) is True
    assert _unregistered_cli_aims_at_dock(
        ["npx", "chrome-remote-interface", "--port", "40141", "inspect"],
        {}, tmp_path, 9333,
    ) is True
    assert _unregistered_cli_aims_at_dock(
        ["npx", "lighthouse", "--port", "9222", "https://example.com"],
        {}, tmp_path, 9333,
    ) is False
    assert _remembered_dock_attach_port() == 9333
    assert bdb.last_known_dock_cdp_port() == 9333
    assert _last_dock_cdp_port.get(hermes_home_key()) == 9333

    monkeypatch.setattr(
        bdb,
        "_cdp_port_reachable",
        lambda port, hosts: port == 9333 and "::1" in hosts,
    )
    _reset_dock_port_memory_for_tests()
    bdb.remember_dock_cdp_port(9333)
    _last_dock_cdp_port[hermes_home_key()] = 9333
    assert bdb.running_instance_cdp_port(str(tmp_path)) == 9333
    assert _remembered_dock_attach_port() == 9333
    assert bdb.last_known_dock_cdp_port() == 9333


def test_first_persist_does_not_stamp_leftover_over_unique_lock_chrome(
    monkeypatch, tmp_path,
):
    """Finding 184: first persist must not stamp leftover over unique chrome.

    Persist may never have been stamped. File TCP / finding 164 then
    stamped leftover ``DevToolsActivePort`` helpers as the first
    persist. Identify leftover on the stale file; attach chrome.
    9222 and the other family stay unknown.
    """
    import tools.bot_desktop.browser as bdb
    from hermes_constants import hermes_home_key
    from tools.browser_tool_session import (
        _cdp_url_is_bot_desktop_browser,
        _last_dock_cdp_port,
        _leftover_cdp_aims_at_dock,
        _leftover_host_port_aims_at_dock,
        _remembered_dock_attach_port,
        _reset_dock_port_memory_for_tests,
        _unregistered_cli_aims_at_dock,
    )

    monkeypatch.setattr(runtime, "state_dir", lambda: tmp_path)
    monkeypatch.setattr(bdb, "profile_dir", lambda: tmp_path)
    monkeypatch.setattr(bdb, "_lock_pid", lambda d: 4240)
    monkeypatch.setattr(
        bdb,
        "_chromium_cmdline_tokens",
        lambda pid: ["chrome", f"--user-data-dir={tmp_path}"],
    )
    monkeypatch.setattr(
        bdb, "_loopback_listen_ports_for_pid",
        lambda pid: {9333, 40141} if pid == 4240 else set(),
    )
    monkeypatch.setattr(
        bdb, "_loopback_listen_targets_for_pid",
        lambda pid: {("::1", 9333), ("::1", 40141)} if pid == 4240 else set(),
    )
    monkeypatch.setattr(bdb, "_recover_cdp_port_from_singleton", lambda *a, **k: None)

    def _inodes(port):
        if port == 40141:
            return {7: {"::1"}}
        if port == 9333:
            return {8: {"::1"}}
        return {}

    def _holders(want):
        out = {}
        if 7 in want:
            out[4240] = {7}
            out[4242] = {7}
            out[4243] = {7}
        if 8 in want:
            out[4240] = {8}
        return out

    monkeypatch.setattr(bdb, "_loopback_listen_inodes_for_port", _inodes)
    monkeypatch.setattr(bdb, "_pids_holding_socket_inodes", _holders)
    monkeypatch.setattr(
        bdb,
        "_cdp_port_reachable",
        lambda port, hosts: port in (9333, 40141) and "::1" in hosts,
    )
    monkeypatch.setattr(
        bdb, "_configured_cdp_override_url",
        lambda: "http://[::1]:40141",
    )
    _reset_dock_port_memory_for_tests()
    (tmp_path / "DevToolsActivePort").write_text(
        "40141\n/devtools/browser/abc\n", encoding="utf-8",
    )
    assert bdb.last_known_dock_cdp_port() is None
    assert _last_dock_cdp_port.get(hermes_home_key()) is None
    assert bdb.running_instance_cdp_port(str(tmp_path)) == 9333
    assert bdb.last_known_dock_cdp_port() == 9333
    assert _last_dock_cdp_port.get(hermes_home_key()) == 9333
    assert _remembered_dock_attach_port() == 9333
    assert _cdp_url_is_bot_desktop_browser("http://[::1]:40141") is True
    assert _cdp_url_is_bot_desktop_browser("40141") is True
    assert _cdp_url_is_bot_desktop_browser("http://[::1]:9333") is True
    assert _cdp_url_is_bot_desktop_browser("http://127.0.0.1:40141") is False
    assert _cdp_url_is_bot_desktop_browser("9222") is False
    assert _leftover_cdp_aims_at_dock("http://[::1]:40141", 9333) is True
    assert _leftover_host_port_aims_at_dock(None, 40141, 9333) is True
    assert _leftover_host_port_aims_at_dock("::1", 40141, 9333) is True
    assert _leftover_host_port_aims_at_dock("127.0.0.1", 40141, 9333) is False
    assert _leftover_host_port_aims_at_dock(None, 9222, 9333) is False
    assert _unregistered_cli_aims_at_dock(
        ["npx", "lighthouse", "https://example.com", "--port", "40141"],
        {}, tmp_path, 9333,
    ) is True
    assert _unregistered_cli_aims_at_dock(
        ["agent-browser", "--cdp", "http://[::1]:40141", "fill"],
        {}, tmp_path, 9333,
    ) is True
    assert _unregistered_cli_aims_at_dock(
        ["npx", "chrome-remote-interface", "--port", "40141", "inspect"],
        {}, tmp_path, 9333,
    ) is True
    assert _unregistered_cli_aims_at_dock(
        ["npx", "lighthouse", "--port", "9222", "https://example.com"],
        {}, tmp_path, 9333,
    ) is False
    assert _remembered_dock_attach_port() == 9333
    assert bdb.last_known_dock_cdp_port() == 9333
    assert _last_dock_cdp_port.get(hermes_home_key()) == 9333


def test_missing_lock_first_persist_does_not_stamp_leftover_helpers(
    monkeypatch, tmp_path,
):
    """Finding 185: missing lock must not first-persist leftover helpers.

    Finding 184 needs the lock pid. Overlay can unlink it before any
    live stamp. File TCP / finding 164 then stamped leftover
    ``DevToolsActivePort`` helpers as the first persist. Identify
    leftover on the stale file; attach chrome. 9222 and the other
    family stay unknown.
    """
    import tools.bot_desktop.browser as bdb
    from hermes_constants import hermes_home_key
    from tools.browser_tool_session import (
        _cdp_url_is_bot_desktop_browser,
        _last_dock_cdp_port,
        _leftover_cdp_aims_at_dock,
        _leftover_host_port_aims_at_dock,
        _remembered_dock_attach_port,
        _reset_dock_port_memory_for_tests,
        _unregistered_cli_aims_at_dock,
    )

    monkeypatch.setattr(runtime, "state_dir", lambda: tmp_path)
    monkeypatch.setattr(bdb, "profile_dir", lambda: tmp_path)
    monkeypatch.setattr(bdb, "_lock_pid", lambda d: None)
    monkeypatch.setattr(
        bdb,
        "_chromium_cmdline_tokens",
        lambda pid: ["chrome", f"--user-data-dir={tmp_path}"],
    )
    monkeypatch.setattr(
        bdb, "_loopback_listen_ports_for_pid",
        lambda pid: {9333, 40141} if pid == 4240 else set(),
    )
    monkeypatch.setattr(
        bdb, "_loopback_listen_targets_for_pid",
        lambda pid: {("::1", 9333), ("::1", 40141)} if pid == 4240 else set(),
    )
    monkeypatch.setattr(bdb, "_recover_cdp_port_from_singleton", lambda *a, **k: None)

    def _inodes(port):
        if port == 40141:
            return {7: {"::1"}}
        if port == 9333:
            return {8: {"::1"}}
        return {}

    def _holders(want):
        out = {}
        if 7 in want:
            out[4240] = {7}
            out[4242] = {7}
            out[4243] = {7}
        if 8 in want:
            out[4240] = {8}
        return out

    monkeypatch.setattr(bdb, "_loopback_listen_inodes_for_port", _inodes)
    monkeypatch.setattr(bdb, "_pids_holding_socket_inodes", _holders)
    monkeypatch.setattr(
        bdb,
        "_cdp_port_reachable",
        lambda port, hosts: port in (9333, 40141) and "::1" in hosts,
    )
    monkeypatch.setattr(
        bdb, "_configured_cdp_override_url",
        lambda: "http://[::1]:40141",
    )
    _reset_dock_port_memory_for_tests()
    (tmp_path / "DevToolsActivePort").write_text(
        "40141\n/devtools/browser/abc\n", encoding="utf-8",
    )
    assert bdb.last_known_dock_cdp_port() is None
    assert _last_dock_cdp_port.get(hermes_home_key()) is None
    assert bdb.running_instance_cdp_port(str(tmp_path)) == 9333
    assert bdb.last_known_dock_cdp_port() == 9333
    assert _last_dock_cdp_port.get(hermes_home_key()) == 9333
    assert _remembered_dock_attach_port() == 9333
    assert _cdp_url_is_bot_desktop_browser("http://[::1]:40141") is True
    assert _cdp_url_is_bot_desktop_browser("40141") is True
    assert _cdp_url_is_bot_desktop_browser("http://[::1]:9333") is True
    assert _cdp_url_is_bot_desktop_browser("http://127.0.0.1:40141") is False
    assert _cdp_url_is_bot_desktop_browser("9222") is False
    assert _leftover_cdp_aims_at_dock("http://[::1]:40141", 9333) is True
    assert _leftover_host_port_aims_at_dock(None, 40141, 9333) is True
    assert _leftover_host_port_aims_at_dock("::1", 40141, 9333) is True
    assert _leftover_host_port_aims_at_dock("127.0.0.1", 40141, 9333) is False
    assert _leftover_host_port_aims_at_dock(None, 9222, 9333) is False
    assert _unregistered_cli_aims_at_dock(
        ["npx", "lighthouse", "https://example.com", "--port", "40141"],
        {}, tmp_path, 9333,
    ) is True
    assert _unregistered_cli_aims_at_dock(
        ["agent-browser", "--cdp", "http://[::1]:40141", "fill"],
        {}, tmp_path, 9333,
    ) is True
    assert _unregistered_cli_aims_at_dock(
        ["npx", "chrome-remote-interface", "--port", "40141", "inspect"],
        {}, tmp_path, 9333,
    ) is True
    assert _unregistered_cli_aims_at_dock(
        ["npx", "lighthouse", "--port", "9222", "https://example.com"],
        {}, tmp_path, 9333,
    ) is False
    assert _remembered_dock_attach_port() == 9333
    assert bdb.last_known_dock_cdp_port() == 9333
    assert _last_dock_cdp_port.get(hermes_home_key()) == 9333


def test_missing_lock_first_persist_does_not_stamp_parent_chrome_as_leftover(
    monkeypatch, tmp_path,
):
    """Finding 186: leftover helpers that dropped chrome's DevTools fd.

    Overlay can unlink SingletonLock before any live stamp. Finding
    185's leftover-holder listen scan misses when helpers dropped
    chrome's inherited fd. Those helpers still have chrome as PPID.
    Identify leftover on the stale file; attach chrome. 9222 and
    the other family stay unknown.
    """
    import tools.bot_desktop.browser as bdb
    from hermes_constants import hermes_home_key
    from tools.browser_tool_session import (
        _cdp_url_is_bot_desktop_browser,
        _last_dock_cdp_port,
        _leftover_cdp_aims_at_dock,
        _leftover_host_port_aims_at_dock,
        _remembered_dock_attach_port,
        _reset_dock_port_memory_for_tests,
        _unregistered_cli_aims_at_dock,
    )

    leftover_helpers = {4242, 4243}

    monkeypatch.setattr(runtime, "state_dir", lambda: tmp_path)
    monkeypatch.setattr(bdb, "profile_dir", lambda: tmp_path)
    monkeypatch.setattr(bdb, "_lock_pid", lambda d: None)
    monkeypatch.setattr(
        bdb,
        "_chromium_cmdline_tokens",
        lambda pid: ["chrome", f"--user-data-dir={tmp_path}"],
    )
    monkeypatch.setattr(
        bdb, "_loopback_listen_ports_for_pid",
        lambda pid: {9333} if pid == 4240 else (
            {40141} if pid in leftover_helpers else set()
        ),
    )
    monkeypatch.setattr(
        bdb, "_loopback_listen_targets_for_pid",
        lambda pid: {("::1", 9333)} if pid == 4240 else (
            {("::1", 40141)} if pid in leftover_helpers else set()
        ),
    )
    monkeypatch.setattr(bdb, "_recover_cdp_port_from_singleton", lambda *a, **k: None)
    monkeypatch.setattr(
        bdb,
        "_proc_ppid",
        lambda pid: 4240 if pid in leftover_helpers else None,
    )

    def _inodes(port):
        if port == 40141:
            return {7: {"::1"}}
        if port == 9333:
            return {8: {"::1"}}
        return {}

    def _holders(want):
        out = {}
        if 7 in want:
            out[4242] = {7}
            out[4243] = {7}
        if 8 in want:
            out[4240] = {8}
        return out

    monkeypatch.setattr(bdb, "_loopback_listen_inodes_for_port", _inodes)
    monkeypatch.setattr(bdb, "_pids_holding_socket_inodes", _holders)
    monkeypatch.setattr(
        bdb,
        "_cdp_port_reachable",
        lambda port, hosts: port in (9333, 40141) and "::1" in hosts,
    )
    monkeypatch.setattr(
        bdb, "_configured_cdp_override_url",
        lambda: "http://[::1]:40141",
    )
    _reset_dock_port_memory_for_tests()
    (tmp_path / "DevToolsActivePort").write_text(
        "40141\n/devtools/browser/abc\n", encoding="utf-8",
    )
    assert bdb.last_known_dock_cdp_port() is None
    assert _last_dock_cdp_port.get(hermes_home_key()) is None
    assert bdb.unique_lock_chrome_hidden_by_leftover_file(
        40141, str(tmp_path),
    ) == 9333
    assert bdb.lock_listed_persist_port() == 9333
    assert bdb._this_jar_chromium_pid(str(tmp_path)) == 4240
    assert bdb.running_instance_cdp_port(str(tmp_path)) == 9333
    assert bdb.last_known_dock_cdp_port() == 9333
    assert _last_dock_cdp_port.get(hermes_home_key()) == 9333
    assert _remembered_dock_attach_port() == 9333
    assert _cdp_url_is_bot_desktop_browser("http://[::1]:40141") is True
    assert _cdp_url_is_bot_desktop_browser("40141") is True
    assert _cdp_url_is_bot_desktop_browser("http://[::1]:9333") is True
    assert _cdp_url_is_bot_desktop_browser("http://127.0.0.1:40141") is False
    assert _cdp_url_is_bot_desktop_browser("9222") is False
    assert _leftover_cdp_aims_at_dock("http://[::1]:40141", 9333) is True
    assert _leftover_host_port_aims_at_dock(None, 40141, 9333) is True
    assert _leftover_host_port_aims_at_dock("::1", 40141, 9333) is True
    assert _leftover_host_port_aims_at_dock("127.0.0.1", 40141, 9333) is False
    assert _leftover_host_port_aims_at_dock(None, 9222, 9333) is False
    assert _unregistered_cli_aims_at_dock(
        ["npx", "lighthouse", "https://example.com", "--port", "40141"],
        {}, tmp_path, 9333,
    ) is True
    assert _unregistered_cli_aims_at_dock(
        ["agent-browser", "--cdp", "http://[::1]:40141", "fill"],
        {}, tmp_path, 9333,
    ) is True
    assert _unregistered_cli_aims_at_dock(
        ["npx", "chrome-remote-interface", "--port", "40141", "inspect"],
        {}, tmp_path, 9333,
    ) is True
    assert _unregistered_cli_aims_at_dock(
        ["npx", "lighthouse", "--port", "9222", "https://example.com"],
        {}, tmp_path, 9333,
    ) is False
    assert _remembered_dock_attach_port() == 9333
    assert bdb.last_known_dock_cdp_port() == 9333
    assert _last_dock_cdp_port.get(hermes_home_key()) == 9333


def test_missing_lock_does_not_hide_parent_chrome_pid_from_owner_session(
    monkeypatch, tmp_path,
):
    """Finding 187: leftover helpers hid chrome pid after lock unlink.

    Skip-kill / owner-session used unique leftover-file holders.
    Several leftover DevTools helpers made chrome unknown, so Take
    over tree-killed the daemon that spawned the Browser a human
    is typing into. Unique holder of hidden chrome is that pid.
    Leftover-only several stays unknown.
    """
    import tools.bot_desktop.browser as bdb
    from tools.browser_tool_session import _singleton_lock_pid

    leftover_helpers = {4242, 4243}

    monkeypatch.setattr(runtime, "state_dir", lambda: tmp_path)
    monkeypatch.setattr(bdb, "profile_dir", lambda: tmp_path)
    monkeypatch.setattr(bdb, "_lock_pid", lambda d: None)
    monkeypatch.setattr(
        bdb,
        "_chromium_cmdline_tokens",
        lambda pid: ["chrome", f"--user-data-dir={tmp_path}"],
    )
    monkeypatch.setattr(
        bdb, "_loopback_listen_ports_for_pid",
        lambda pid: {9333} if pid == 4240 else (
            {40141} if pid in leftover_helpers else set()
        ),
    )
    monkeypatch.setattr(
        bdb, "_loopback_listen_targets_for_pid",
        lambda pid: {("::1", 9333)} if pid == 4240 else (
            {("::1", 40141)} if pid in leftover_helpers else set()
        ),
    )
    monkeypatch.setattr(bdb, "_recover_cdp_port_from_singleton", lambda *a, **k: None)
    monkeypatch.setattr(
        bdb,
        "_proc_ppid",
        lambda pid: 4240 if pid in leftover_helpers else None,
    )
    monkeypatch.setattr(
        bdb,
        "_launched_by_session",
        lambda pid: "h_review" if pid == 4240 else None,
    )

    def _inodes(port):
        if port == 40141:
            return {7: {"::1"}}
        if port == 9333:
            return {8: {"::1"}}
        return {}

    def _holders(want):
        out = {}
        if 7 in want:
            out[4242] = {7}
            out[4243] = {7}
        if 8 in want:
            out[4240] = {8}
        return out

    monkeypatch.setattr(bdb, "_loopback_listen_inodes_for_port", _inodes)
    monkeypatch.setattr(bdb, "_pids_holding_socket_inodes", _holders)
    monkeypatch.setattr(
        bdb,
        "_cdp_port_reachable",
        lambda port, hosts: port in (9333, 40141) and "::1" in hosts,
    )
    monkeypatch.setattr(bdb, "_configured_cdp_override_url", lambda: "")
    (tmp_path / "DevToolsActivePort").write_text(
        "40141\n/devtools/browser/abc\n", encoding="utf-8",
    )
    assert bdb._this_jar_chromium_pid(str(tmp_path)) == 4240
    assert bdb.shared_chromium_owner_session() == "h_review"
    assert _singleton_lock_pid(str(tmp_path)) == 4240

    def _leftover_only(want):
        out = {}
        if 7 in want:
            out[4240] = {7}
            out[4242] = {7}
            out[4243] = {7}
        return out

    monkeypatch.setattr(bdb, "_pids_holding_socket_inodes", _leftover_only)
    monkeypatch.setattr(
        bdb, "_loopback_listen_ports_for_pid",
        lambda pid: {40141} if pid == 4240 else (
            {40141} if pid in leftover_helpers else set()
        ),
    )
    assert bdb._this_jar_chromium_pid(str(tmp_path)) is None
    assert bdb.shared_chromium_owner_session() is None
    assert _singleton_lock_pid(str(tmp_path)) is None


def test_leftover_inherited_chrome_listen_does_not_stamp_helpers(
    monkeypatch, tmp_path,
):
    """Finding 188: leftover inherited chrome's unique CDP listen.

    n==1 unique-holder recover misses when leftover helpers still
    name this jar and hold chrome's listen inode (finding 163).
    Identify leftover on the stale file; attach chrome. 9222 and
    the other family stay unknown.
    """
    import tools.bot_desktop.browser as bdb
    from hermes_constants import hermes_home_key
    from tools.browser_tool_session import (
        _cdp_url_is_bot_desktop_browser,
        _last_dock_cdp_port,
        _leftover_cdp_aims_at_dock,
        _leftover_host_port_aims_at_dock,
        _remembered_dock_attach_port,
        _reset_dock_port_memory_for_tests,
        _singleton_lock_pid,
        _unregistered_cli_aims_at_dock,
    )

    leftover_helpers = {4242, 4243}

    monkeypatch.setattr(runtime, "state_dir", lambda: tmp_path)
    monkeypatch.setattr(bdb, "profile_dir", lambda: tmp_path)
    monkeypatch.setattr(bdb, "_lock_pid", lambda d: None)
    monkeypatch.setattr(
        bdb,
        "_chromium_cmdline_tokens",
        lambda pid: ["chrome", f"--user-data-dir={tmp_path}"],
    )
    monkeypatch.setattr(
        bdb, "_loopback_listen_ports_for_pid",
        lambda pid: {9333, 40141} if pid in leftover_helpers else (
            {9333} if pid == 4240 else set()
        ),
    )
    monkeypatch.setattr(
        bdb, "_loopback_listen_targets_for_pid",
        lambda pid: {("::1", 9333), ("::1", 40141)} if pid in leftover_helpers else (
            {("::1", 9333)} if pid == 4240 else set()
        ),
    )
    monkeypatch.setattr(bdb, "_recover_cdp_port_from_singleton", lambda *a, **k: None)
    monkeypatch.setattr(
        bdb,
        "_proc_ppid",
        lambda pid: 4240 if pid in leftover_helpers else None,
    )
    monkeypatch.setattr(
        bdb,
        "_launched_by_session",
        lambda pid: "h_review" if pid == 4240 else None,
    )

    def _inodes(port):
        if port == 40141:
            return {7: {"::1"}}
        if port == 9333:
            return {8: {"::1"}}
        return {}

    def _holders(want):
        out = {}
        if 7 in want:
            out[4242] = {7}
            out[4243] = {7}
        if 8 in want:
            out[4240] = {8}
            out[4242] = {8}
            out[4243] = {8}
        return out

    monkeypatch.setattr(bdb, "_loopback_listen_inodes_for_port", _inodes)
    monkeypatch.setattr(bdb, "_pids_holding_socket_inodes", _holders)
    monkeypatch.setattr(
        bdb,
        "_cdp_port_reachable",
        lambda port, hosts: port in (9333, 40141) and "::1" in hosts,
    )
    monkeypatch.setattr(
        bdb, "_configured_cdp_override_url",
        lambda: "http://[::1]:40141",
    )
    _reset_dock_port_memory_for_tests()
    (tmp_path / "DevToolsActivePort").write_text(
        "40141\n/devtools/browser/abc\n", encoding="utf-8",
    )
    assert bdb.unique_lock_chrome_hidden_by_leftover_file(
        40141, str(tmp_path),
    ) == 9333
    assert bdb.lock_listed_persist_port() == 9333
    assert bdb._this_jar_chromium_pid(str(tmp_path)) == 4240
    assert bdb.shared_chromium_owner_session() == "h_review"
    assert _singleton_lock_pid(str(tmp_path)) == 4240
    assert bdb.running_instance_cdp_port(str(tmp_path)) == 9333
    assert bdb.last_known_dock_cdp_port() == 9333
    assert _last_dock_cdp_port.get(hermes_home_key()) == 9333
    assert _remembered_dock_attach_port() == 9333
    assert _cdp_url_is_bot_desktop_browser("http://[::1]:40141") is True
    assert _cdp_url_is_bot_desktop_browser("40141") is True
    assert _cdp_url_is_bot_desktop_browser("http://[::1]:9333") is True
    assert _cdp_url_is_bot_desktop_browser("http://127.0.0.1:40141") is False
    assert _cdp_url_is_bot_desktop_browser("9222") is False
    assert _leftover_cdp_aims_at_dock("http://[::1]:40141", 9333) is True
    assert _leftover_host_port_aims_at_dock(None, 40141, 9333) is True
    assert _leftover_host_port_aims_at_dock("::1", 40141, 9333) is True
    assert _leftover_host_port_aims_at_dock("127.0.0.1", 40141, 9333) is False
    assert _leftover_host_port_aims_at_dock(None, 9222, 9333) is False
    assert _unregistered_cli_aims_at_dock(
        ["npx", "lighthouse", "https://example.com", "--port", "40141"],
        {}, tmp_path, 9333,
    ) is True
    assert _unregistered_cli_aims_at_dock(
        ["agent-browser", "--cdp", "http://[::1]:40141", "fill"],
        {}, tmp_path, 9333,
    ) is True
    assert _unregistered_cli_aims_at_dock(
        ["npx", "chrome-remote-interface", "--port", "40141", "inspect"],
        {}, tmp_path, 9333,
    ) is True
    assert _unregistered_cli_aims_at_dock(
        ["npx", "lighthouse", "--port", "9222", "https://example.com"],
        {}, tmp_path, 9333,
    ) is False
    assert _remembered_dock_attach_port() == 9333
    assert bdb.last_known_dock_cdp_port() == 9333
    assert _last_dock_cdp_port.get(hermes_home_key()) == 9333


def test_leftover_connect_only_chrome_zygote_does_not_stamp_helpers(
    monkeypatch, tmp_path,
):
    """Finding 189: leftover is connect-only; chrome + zygote hold CDP.

    Leftover-shared equality missed chrome when leftover CRI /
    lighthouse only connect to DevTools and chrome's children
    inherit the listen. Identify leftover on the stale file;
    attach chrome. 9222 and the other family stay unknown.
    """
    import tools.bot_desktop.browser as bdb
    from hermes_constants import hermes_home_key
    from tools.browser_tool_session import (
        _cdp_url_is_bot_desktop_browser,
        _last_dock_cdp_port,
        _leftover_cdp_aims_at_dock,
        _leftover_host_port_aims_at_dock,
        _remembered_dock_attach_port,
        _reset_dock_port_memory_for_tests,
        _singleton_lock_pid,
        _unregistered_cli_aims_at_dock,
    )

    leftover_helpers = {4242, 4243}
    chrome_family = {4240, 4241}

    monkeypatch.setattr(runtime, "state_dir", lambda: tmp_path)
    monkeypatch.setattr(bdb, "profile_dir", lambda: tmp_path)
    monkeypatch.setattr(bdb, "_lock_pid", lambda d: None)
    monkeypatch.setattr(
        bdb,
        "_chromium_cmdline_tokens",
        lambda pid: ["chrome", f"--user-data-dir={tmp_path}"],
    )
    monkeypatch.setattr(
        bdb, "_loopback_listen_ports_for_pid",
        lambda pid: {40141} if pid in leftover_helpers else (
            {9333} if pid in chrome_family else set()
        ),
    )
    monkeypatch.setattr(
        bdb, "_loopback_listen_targets_for_pid",
        lambda pid: {("::1", 40141)} if pid in leftover_helpers else (
            {("::1", 9333)} if pid in chrome_family else set()
        ),
    )
    monkeypatch.setattr(bdb, "_recover_cdp_port_from_singleton", lambda *a, **k: None)
    monkeypatch.setattr(
        bdb,
        "_proc_ppid",
        lambda pid: 4240 if pid in (4241, 4242, 4243) else None,
    )
    monkeypatch.setattr(
        bdb,
        "_launched_by_session",
        lambda pid: "h_review" if pid == 4240 else None,
    )

    def _inodes(port):
        if port == 40141:
            return {7: {"::1"}}
        if port == 9333:
            return {8: {"::1"}}
        return {}

    def _holders(want):
        out = {}
        if 7 in want:
            out[4242] = {7}
            out[4243] = {7}
        if 8 in want:
            out[4240] = {8}
            out[4241] = {8}
        return out

    monkeypatch.setattr(bdb, "_loopback_listen_inodes_for_port", _inodes)
    monkeypatch.setattr(bdb, "_pids_holding_socket_inodes", _holders)
    monkeypatch.setattr(
        bdb,
        "_cdp_port_reachable",
        lambda port, hosts: port in (9333, 40141) and "::1" in hosts,
    )
    monkeypatch.setattr(
        bdb, "_configured_cdp_override_url",
        lambda: "http://[::1]:40141",
    )
    _reset_dock_port_memory_for_tests()
    (tmp_path / "DevToolsActivePort").write_text(
        "40141\n/devtools/browser/abc\n", encoding="utf-8",
    )
    assert bdb.unique_lock_chrome_hidden_by_leftover_file(
        40141, str(tmp_path),
    ) == 9333
    assert bdb.lock_listed_persist_port() == 9333
    assert bdb._this_jar_chromium_pid(str(tmp_path)) == 4240
    assert bdb.shared_chromium_owner_session() == "h_review"
    assert _singleton_lock_pid(str(tmp_path)) == 4240
    assert bdb.running_instance_cdp_port(str(tmp_path)) == 9333
    assert bdb.last_known_dock_cdp_port() == 9333
    assert _last_dock_cdp_port.get(hermes_home_key()) == 9333
    assert _remembered_dock_attach_port() == 9333
    assert _cdp_url_is_bot_desktop_browser("http://[::1]:40141") is True
    assert _cdp_url_is_bot_desktop_browser("40141") is True
    assert _cdp_url_is_bot_desktop_browser("http://[::1]:9333") is True
    assert _cdp_url_is_bot_desktop_browser("http://127.0.0.1:40141") is False
    assert _cdp_url_is_bot_desktop_browser("9222") is False
    assert _leftover_cdp_aims_at_dock("http://[::1]:40141", 9333) is True
    assert _leftover_host_port_aims_at_dock(None, 40141, 9333) is True
    assert _leftover_host_port_aims_at_dock("::1", 40141, 9333) is True
    assert _leftover_host_port_aims_at_dock("127.0.0.1", 40141, 9333) is False
    assert _leftover_host_port_aims_at_dock(None, 9222, 9333) is False
    assert _unregistered_cli_aims_at_dock(
        ["npx", "lighthouse", "https://example.com", "--port", "40141"],
        {}, tmp_path, 9333,
    ) is True
    assert _unregistered_cli_aims_at_dock(
        ["agent-browser", "--cdp", "http://[::1]:40141", "fill"],
        {}, tmp_path, 9333,
    ) is True
    assert _unregistered_cli_aims_at_dock(
        ["npx", "chrome-remote-interface", "--port", "40141", "inspect"],
        {}, tmp_path, 9333,
    ) is True
    assert _unregistered_cli_aims_at_dock(
        ["npx", "lighthouse", "--port", "9222", "https://example.com"],
        {}, tmp_path, 9333,
    ) is False
    assert _remembered_dock_attach_port() == 9333
    assert bdb.last_known_dock_cdp_port() == 9333
    assert _last_dock_cdp_port.get(hermes_home_key()) == 9333


def test_leftover_connect_only_chrome_zygote_grandchild_does_not_stamp_helpers(
    monkeypatch, tmp_path,
):
    """Finding 190: leftover is connect-only; zygote grandchild holds CDP.

    Child-only family missed chrome when a zygote-spawned utility
    inherited the listen. Identify leftover on the stale file;
    attach chrome. 9222 and the other family stay unknown.
    """
    import tools.bot_desktop.browser as bdb
    from hermes_constants import hermes_home_key
    from tools.browser_tool_session import (
        _cdp_url_is_bot_desktop_browser,
        _last_dock_cdp_port,
        _leftover_cdp_aims_at_dock,
        _leftover_host_port_aims_at_dock,
        _remembered_dock_attach_port,
        _reset_dock_port_memory_for_tests,
        _singleton_lock_pid,
        _unregistered_cli_aims_at_dock,
    )

    leftover_helpers = {4242, 4243}
    chrome_family = {4240, 4241, 4245}

    monkeypatch.setattr(runtime, "state_dir", lambda: tmp_path)
    monkeypatch.setattr(bdb, "profile_dir", lambda: tmp_path)
    monkeypatch.setattr(bdb, "_lock_pid", lambda d: None)
    monkeypatch.setattr(
        bdb,
        "_chromium_cmdline_tokens",
        lambda pid: ["chrome", f"--user-data-dir={tmp_path}"],
    )
    monkeypatch.setattr(
        bdb, "_loopback_listen_ports_for_pid",
        lambda pid: {40141} if pid in leftover_helpers else (
            {9333} if pid in chrome_family else set()
        ),
    )
    monkeypatch.setattr(
        bdb, "_loopback_listen_targets_for_pid",
        lambda pid: {("::1", 40141)} if pid in leftover_helpers else (
            {("::1", 9333)} if pid in chrome_family else set()
        ),
    )
    monkeypatch.setattr(bdb, "_recover_cdp_port_from_singleton", lambda *a, **k: None)
    monkeypatch.setattr(
        bdb,
        "_proc_ppid",
        lambda pid: {4241: 4240, 4245: 4241, 4242: 4240, 4243: 4240}.get(pid),
    )
    monkeypatch.setattr(
        bdb,
        "_launched_by_session",
        lambda pid: "h_review" if pid == 4240 else None,
    )

    def _inodes(port):
        if port == 40141:
            return {7: {"::1"}}
        if port == 9333:
            return {8: {"::1"}}
        return {}

    def _holders(want):
        out = {}
        if 7 in want:
            out[4242] = {7}
            out[4243] = {7}
        if 8 in want:
            out[4240] = {8}
            out[4241] = {8}
            out[4245] = {8}
        return out

    monkeypatch.setattr(bdb, "_loopback_listen_inodes_for_port", _inodes)
    monkeypatch.setattr(bdb, "_pids_holding_socket_inodes", _holders)
    monkeypatch.setattr(
        bdb,
        "_cdp_port_reachable",
        lambda port, hosts: port in (9333, 40141) and "::1" in hosts,
    )
    monkeypatch.setattr(
        bdb, "_configured_cdp_override_url",
        lambda: "http://[::1]:40141",
    )
    _reset_dock_port_memory_for_tests()
    (tmp_path / "DevToolsActivePort").write_text(
        "40141\n/devtools/browser/abc\n", encoding="utf-8",
    )
    assert bdb.unique_lock_chrome_hidden_by_leftover_file(
        40141, str(tmp_path),
    ) == 9333
    assert bdb.lock_listed_persist_port() == 9333
    assert bdb._this_jar_chromium_pid(str(tmp_path)) == 4240
    assert bdb.shared_chromium_owner_session() == "h_review"
    assert _singleton_lock_pid(str(tmp_path)) == 4240
    assert bdb.running_instance_cdp_port(str(tmp_path)) == 9333
    assert bdb.last_known_dock_cdp_port() == 9333
    assert _last_dock_cdp_port.get(hermes_home_key()) == 9333
    assert _remembered_dock_attach_port() == 9333
    assert _cdp_url_is_bot_desktop_browser("http://[::1]:40141") is True
    assert _cdp_url_is_bot_desktop_browser("http://[::1]:9333") is True
    assert _cdp_url_is_bot_desktop_browser("9222") is False
    assert _leftover_cdp_aims_at_dock("http://[::1]:40141", 9333) is True
    assert _leftover_host_port_aims_at_dock(None, 40141, 9333) is True
    assert _leftover_host_port_aims_at_dock("::1", 40141, 9333) is True
    assert _leftover_host_port_aims_at_dock("127.0.0.1", 40141, 9333) is False
    assert _unregistered_cli_aims_at_dock(
        ["npx", "lighthouse", "https://example.com", "--port", "40141"],
        {}, tmp_path, 9333,
    ) is True
    assert _unregistered_cli_aims_at_dock(
        ["npx", "chrome-remote-interface", "--port", "40141", "inspect"],
        {}, tmp_path, 9333,
    ) is True
    assert _unregistered_cli_aims_at_dock(
        ["npx", "lighthouse", "--port", "9222", "https://example.com"],
        {}, tmp_path, 9333,
    ) is False
    assert _remembered_dock_attach_port() == 9333


def test_leftover_daemon_parent_sibling_chrome_does_not_stamp_helpers(
    monkeypatch, tmp_path,
):
    """Finding 191: leftover helpers' parent is leftover daemon.

    Leftover-shared chrome_pid was leftover daemon, so persist
    stamped leftover DevTools. Identify leftover on the stale
    file; attach chrome. 9222 and the other family stay unknown.
    """
    import tools.bot_desktop.browser as bdb
    from hermes_constants import hermes_home_key
    from tools.browser_tool_session import (
        _cdp_url_is_bot_desktop_browser,
        _last_dock_cdp_port,
        _leftover_cdp_aims_at_dock,
        _leftover_host_port_aims_at_dock,
        _remembered_dock_attach_port,
        _reset_dock_port_memory_for_tests,
        _singleton_lock_pid,
        _unregistered_cli_aims_at_dock,
    )

    leftover_helpers = {4242, 4243}
    chrome_family = {4240, 4241, 4245}

    monkeypatch.setattr(runtime, "state_dir", lambda: tmp_path)
    monkeypatch.setattr(bdb, "profile_dir", lambda: tmp_path)
    monkeypatch.setattr(bdb, "_lock_pid", lambda d: None)
    monkeypatch.setattr(
        bdb,
        "_chromium_cmdline_tokens",
        lambda pid: (
            ["chrome", "--type=zygote", f"--user-data-dir={tmp_path}"]
            if pid == 4241 else
            ["chrome", "--type=utility", f"--user-data-dir={tmp_path}"]
            if pid == 4245 else
            ["agent-browser", "daemon", f"--user-data-dir={tmp_path}"]
            if pid == 4300 else
            ["chrome", f"--user-data-dir={tmp_path}"]
        ),
    )
    monkeypatch.setattr(
        bdb,
        "_proc_ppid",
        lambda pid: {
            4240: 4300, 4241: 4240, 4245: 4241, 4242: 4300, 4243: 4300,
        }.get(pid),
    )
    monkeypatch.setattr(
        bdb,
        "_launched_by_session",
        lambda pid: "h_review" if pid == 4240 else None,
    )
    monkeypatch.setattr(bdb, "_recover_cdp_port_from_singleton", lambda *a, **k: None)
    monkeypatch.setattr(
        bdb, "_this_jar_children",
        lambda parent, user_data_dir: (
            {4240, 4242, 4243} if parent == 4300 else set()
        ),
    )

    def _inodes(port):
        if port == 40141:
            return {7: {"::1"}}
        if port == 9333:
            return {8: {"::1"}}
        return {}

    def _holders(want):
        out = {}
        if 7 in want:
            out[4242] = {7}
            out[4243] = {7}
        if 8 in want:
            out[4240] = {8}
            out[4241] = {8}
            out[4245] = {8}
        return out

    monkeypatch.setattr(bdb, "_loopback_listen_inodes_for_port", _inodes)
    monkeypatch.setattr(bdb, "_pids_holding_socket_inodes", _holders)
    monkeypatch.setattr(
        bdb, "_loopback_listen_ports_for_pid",
        lambda pid: {40141} if pid in leftover_helpers or pid == 4300 else (
            {9333} if pid in chrome_family else set()
        ),
    )
    monkeypatch.setattr(
        bdb, "_loopback_listen_targets_for_pid",
        lambda pid: {("::1", 40141)} if pid in leftover_helpers or pid == 4300 else (
            {("::1", 9333)} if pid in chrome_family else set()
        ),
    )
    monkeypatch.setattr(
        bdb,
        "_cdp_port_reachable",
        lambda port, hosts: port in (9333, 40141) and "::1" in hosts,
    )
    monkeypatch.setattr(
        bdb, "_configured_cdp_override_url",
        lambda: "http://[::1]:40141",
    )
    _reset_dock_port_memory_for_tests()
    (tmp_path / "DevToolsActivePort").write_text(
        "40141\n/devtools/browser/abc\n", encoding="utf-8",
    )
    assert bdb.unique_lock_chrome_hidden_by_leftover_file(
        40141, str(tmp_path),
    ) == 9333
    assert bdb.lock_listed_persist_port() == 9333
    assert bdb._this_jar_chromium_pid(str(tmp_path)) == 4240
    assert bdb.shared_chromium_owner_session() == "h_review"
    assert _singleton_lock_pid(str(tmp_path)) == 4240
    assert bdb.running_instance_cdp_port(str(tmp_path)) == 9333
    assert _last_dock_cdp_port.get(hermes_home_key()) == 9333
    assert _remembered_dock_attach_port() == 9333
    assert _cdp_url_is_bot_desktop_browser("http://[::1]:40141") is True
    assert _cdp_url_is_bot_desktop_browser("http://[::1]:9333") is True
    assert _cdp_url_is_bot_desktop_browser("9222") is False
    assert _leftover_cdp_aims_at_dock("http://[::1]:40141", 9333) is True
    assert _leftover_host_port_aims_at_dock("::1", 40141, 9333) is True
    assert _leftover_host_port_aims_at_dock("127.0.0.1", 40141, 9333) is False
    assert _unregistered_cli_aims_at_dock(
        ["npx", "lighthouse", "https://example.com", "--port", "40141"],
        {}, tmp_path, 9333,
    ) is True
    assert _unregistered_cli_aims_at_dock(
        ["npx", "lighthouse", "--port", "9222", "https://example.com"],
        {}, tmp_path, 9333,
    ) is False
    assert _remembered_dock_attach_port() == 9333


def test_leftover_memory_hides_sibling_chrome_does_not_stamp_helpers(
    monkeypatch, tmp_path,
):
    """Finding 192: leftover memory hid sibling chrome.

    Lock-gone 179 returned leftover helper hosts. Identify leftover
    on the stale file; attach chrome. 9222 and the other family
    stay unknown.
    """
    import tools.bot_desktop.browser as bdb
    from hermes_constants import hermes_home_key
    from tools.browser_tool_session import (
        _cdp_url_is_bot_desktop_browser,
        _last_dock_cdp_port,
        _leftover_cdp_aims_at_dock,
        _leftover_host_port_aims_at_dock,
        _remembered_dock_attach_port,
        _reset_dock_port_memory_for_tests,
        _singleton_lock_pid,
        _unregistered_cli_aims_at_dock,
    )

    leftover_helpers = {4242, 4243}
    chrome_family = {4240, 4241, 4245}

    monkeypatch.setattr(runtime, "state_dir", lambda: tmp_path)
    monkeypatch.setattr(bdb, "profile_dir", lambda: tmp_path)
    monkeypatch.setattr(bdb, "_lock_pid", lambda d: None)
    monkeypatch.setattr(
        bdb,
        "_chromium_cmdline_tokens",
        lambda pid: (
            ["chrome", "--type=zygote", f"--user-data-dir={tmp_path}"]
            if pid == 4241 else
            ["chrome", "--type=utility", f"--user-data-dir={tmp_path}"]
            if pid == 4245 else
            ["agent-browser", "daemon", f"--user-data-dir={tmp_path}"]
            if pid == 4300 else
            ["chrome", f"--user-data-dir={tmp_path}"]
        ),
    )
    monkeypatch.setattr(
        bdb,
        "_proc_ppid",
        lambda pid: {
            4240: 4300, 4241: 4240, 4245: 4241, 4242: 4300, 4243: 4300,
        }.get(pid),
    )
    monkeypatch.setattr(
        bdb,
        "_launched_by_session",
        lambda pid: "h_review" if pid == 4240 else None,
    )
    monkeypatch.setattr(bdb, "_recover_cdp_port_from_singleton", lambda *a, **k: None)
    monkeypatch.setattr(
        bdb, "_this_jar_children",
        lambda parent, user_data_dir: (
            {4240, 4242, 4243} if parent == 4300 else set()
        ),
    )

    def _inodes(port):
        if port == 40141:
            return {7: {"::1"}}
        if port == 9333:
            return {8: {"::1"}}
        return {}

    def _holders(want):
        out = {}
        if 7 in want:
            out[4242] = {7}
            out[4243] = {7}
        if 8 in want:
            out[4240] = {8}
            out[4241] = {8}
            out[4245] = {8}
        return out

    monkeypatch.setattr(bdb, "_loopback_listen_inodes_for_port", _inodes)
    monkeypatch.setattr(bdb, "_pids_holding_socket_inodes", _holders)
    monkeypatch.setattr(
        bdb, "_loopback_listen_ports_for_pid",
        lambda pid: {40141} if pid in leftover_helpers or pid == 4300 else (
            {9333} if pid in chrome_family else set()
        ),
    )
    monkeypatch.setattr(
        bdb, "_loopback_listen_targets_for_pid",
        lambda pid: {("::1", 40141)} if pid in leftover_helpers or pid == 4300 else (
            {("::1", 9333)} if pid in chrome_family else set()
        ),
    )
    monkeypatch.setattr(
        bdb,
        "_cdp_port_reachable",
        lambda port, hosts: port in (9333, 40141) and "::1" in hosts,
    )
    monkeypatch.setattr(bdb, "_configured_cdp_override_url", lambda: "")
    (tmp_path / "DevToolsActivePort").write_text(
        "40141\n/devtools/browser/abc\n", encoding="utf-8",
    )
    _reset_dock_port_memory_for_tests()
    _last_dock_cdp_port[hermes_home_key()] = 40141
    (tmp_path / "dock-cdp-port").write_text("40141\n", encoding="utf-8")
    assert bdb.unique_lock_chrome_hidden_by_leftover_file(
        40141, str(tmp_path),
    ) == 9333
    assert bdb.lock_listed_persist_port() == 9333
    assert bdb._this_jar_chromium_pid(str(tmp_path)) == 4240
    assert bdb.shared_chromium_owner_session() == "h_review"
    assert _singleton_lock_pid(str(tmp_path)) == 4240
    assert bdb.running_instance_cdp_port(str(tmp_path)) == 9333
    assert _last_dock_cdp_port.get(hermes_home_key()) == 9333
    assert _remembered_dock_attach_port() == 9333
    assert _cdp_url_is_bot_desktop_browser("http://[::1]:40141") is True
    assert _cdp_url_is_bot_desktop_browser("http://[::1]:9333") is True
    assert _cdp_url_is_bot_desktop_browser("9222") is False
    assert _leftover_cdp_aims_at_dock("http://[::1]:40141", 9333) is True
    assert _leftover_host_port_aims_at_dock("::1", 40141, 9333) is True
    assert _leftover_host_port_aims_at_dock("127.0.0.1", 40141, 9333) is False
    assert _unregistered_cli_aims_at_dock(
        ["npx", "lighthouse", "https://example.com", "--port", "40141"],
        {}, tmp_path, 9333,
    ) is True
    assert _unregistered_cli_aims_at_dock(
        ["npx", "lighthouse", "--port", "9222", "https://example.com"],
        {}, tmp_path, 9333,
    ) is False
    assert _remembered_dock_attach_port() == 9333


def test_leftover_persist_unique_hides_sibling_chrome_does_not_stamp_helpers(
    monkeypatch, tmp_path,
):
    """Finding 193: leftover persist unique hid sibling chrome.

    Lock-gone 181 returned leftover CRI persist after a restart.
    Identify leftover on that stamp and on leftover DevTools;
    attach chrome. 9222 and the other family stay unknown.
    """
    import tools.bot_desktop.browser as bdb
    from hermes_constants import hermes_home_key
    from tools.browser_tool_session import (
        _cdp_url_is_bot_desktop_browser,
        _last_dock_cdp_port,
        _leftover_cdp_aims_at_dock,
        _leftover_host_port_aims_at_dock,
        _remembered_dock_attach_port,
        _reset_dock_port_memory_for_tests,
        _singleton_lock_pid,
        _unregistered_cli_aims_at_dock,
    )

    leftover_helpers = {4242, 4243}
    chrome_family = {4240, 4241, 4245}
    leftover_cri = 4244

    monkeypatch.setattr(runtime, "state_dir", lambda: tmp_path)
    monkeypatch.setattr(bdb, "profile_dir", lambda: tmp_path)
    monkeypatch.setattr(bdb, "_lock_pid", lambda d: None)
    monkeypatch.setattr(
        bdb,
        "_chromium_cmdline_tokens",
        lambda pid: (
            ["chrome", "--type=zygote", f"--user-data-dir={tmp_path}"]
            if pid == 4241 else
            ["chrome", "--type=utility", f"--user-data-dir={tmp_path}"]
            if pid == 4245 else
            ["agent-browser", "daemon", f"--user-data-dir={tmp_path}"]
            if pid == 4300 else
            ["agent-browser", "cri", f"--user-data-dir={tmp_path}"]
            if pid == leftover_cri else
            ["chrome", f"--user-data-dir={tmp_path}"]
        ),
    )
    monkeypatch.setattr(
        bdb,
        "_proc_ppid",
        lambda pid: {
            4240: 4300, 4241: 4240, 4245: 4241, 4242: 4300, 4243: 4300,
            4244: 4300,
        }.get(pid),
    )
    monkeypatch.setattr(
        bdb,
        "_launched_by_session",
        lambda pid: "h_review" if pid == 4240 else None,
    )
    monkeypatch.setattr(bdb, "_recover_cdp_port_from_singleton", lambda *a, **k: None)
    monkeypatch.setattr(
        bdb, "_this_jar_children",
        lambda parent, user_data_dir: (
            {4240, 4242, 4243, 4244} if parent == 4300 else set()
        ),
    )

    def _inodes(port):
        if port == 40141:
            return {7: {"::1"}}
        if port == 9333:
            return {8: {"::1"}}
        if port == 18888:
            return {9: {"::1"}}
        return {}

    def _holders(want):
        out = {}
        if 7 in want:
            out[4242] = {7}
            out[4243] = {7}
        if 8 in want:
            out[4240] = {8}
            out[4241] = {8}
            out[4245] = {8}
        if 9 in want:
            out[4244] = {9}
        return out

    monkeypatch.setattr(bdb, "_loopback_listen_inodes_for_port", _inodes)
    monkeypatch.setattr(bdb, "_pids_holding_socket_inodes", _holders)
    monkeypatch.setattr(
        bdb, "_loopback_listen_ports_for_pid",
        lambda pid: {40141} if pid in leftover_helpers or pid == 4300 else (
            {18888} if pid == leftover_cri else (
                {9333} if pid in chrome_family else set()
            )
        ),
    )
    monkeypatch.setattr(
        bdb, "_loopback_listen_targets_for_pid",
        lambda pid: {("::1", 40141)} if pid in leftover_helpers or pid == 4300 else (
            {("::1", 18888)} if pid == leftover_cri else (
                {("::1", 9333)} if pid in chrome_family else set()
            )
        ),
    )
    monkeypatch.setattr(
        bdb,
        "_cdp_port_reachable",
        lambda port, hosts: port in (9333, 40141, 18888) and "::1" in hosts,
    )
    monkeypatch.setattr(bdb, "_configured_cdp_override_url", lambda: "")
    (tmp_path / "DevToolsActivePort").write_text(
        "40141\n/devtools/browser/abc\n", encoding="utf-8",
    )
    _reset_dock_port_memory_for_tests()
    (tmp_path / "dock-cdp-port").write_text("18888\n", encoding="utf-8")
    assert bdb.unique_lock_chrome_hidden_by_leftover_file(
        40141, str(tmp_path),
    ) == 9333
    assert bdb.lock_listed_persist_port() == 9333
    assert bdb._this_jar_chromium_pid(str(tmp_path)) == 4240
    assert bdb.shared_chromium_owner_session() == "h_review"
    assert _singleton_lock_pid(str(tmp_path)) == 4240
    assert bdb.running_instance_cdp_port(str(tmp_path)) == 9333
    assert _last_dock_cdp_port.get(hermes_home_key()) == 9333
    assert _remembered_dock_attach_port() == 9333
    assert _cdp_url_is_bot_desktop_browser("http://[::1]:40141") is True
    assert _cdp_url_is_bot_desktop_browser("http://[::1]:18888") is True
    assert _cdp_url_is_bot_desktop_browser("http://[::1]:9333") is True
    assert _cdp_url_is_bot_desktop_browser("9222") is False
    assert _leftover_cdp_aims_at_dock("http://[::1]:40141", 9333) is True
    assert _leftover_cdp_aims_at_dock("http://[::1]:18888", 9333) is True
    assert _leftover_host_port_aims_at_dock("::1", 40141, 9333) is True
    assert _leftover_host_port_aims_at_dock("::1", 18888, 9333) is True
    assert _leftover_host_port_aims_at_dock("127.0.0.1", 40141, 9333) is False
    assert _unregistered_cli_aims_at_dock(
        ["npx", "lighthouse", "https://example.com", "--port", "40141"],
        {}, tmp_path, 9333,
    ) is True
    assert _unregistered_cli_aims_at_dock(
        ["npx", "chrome-remote-interface", "--port", "18888", "inspect"],
        {}, tmp_path, 9333,
    ) is True
    assert _unregistered_cli_aims_at_dock(
        ["npx", "lighthouse", "--port", "9222", "https://example.com"],
        {}, tmp_path, 9333,
    ) is False
    assert _remembered_dock_attach_port() == 9333


def test_leftover_persist_unique_does_not_hide_attach_behind_devtools(
    monkeypatch, tmp_path,
):
    """Finding 194: leftover persist unique hid attach behind leftover DevTools.

    Finding 193 prefers hidden chrome for lock-listed. Live recover
    misses when leftover holds chrome's CDP socket (166). Finding 172
    then preferred leftover DevTools because persist file was leftover
    CRI. Attach chrome; identify leftover on both leftover listens.
    9222 and the other family stay unknown.
    """
    import tools.bot_desktop.browser as bdb
    from hermes_constants import hermes_home_key
    from tools.browser_tool_session import (
        _cdp_url_is_bot_desktop_browser,
        _last_dock_cdp_port,
        _leftover_cdp_aims_at_dock,
        _leftover_host_port_aims_at_dock,
        _remembered_dock_attach_port,
        _reset_dock_port_memory_for_tests,
        _singleton_lock_pid,
        _unregistered_cli_aims_at_dock,
    )

    leftover_helpers = {4242, 4243}
    chrome_family = {4240, 4241, 4245}
    leftover_cri = 4244

    monkeypatch.setattr(runtime, "state_dir", lambda: tmp_path)
    monkeypatch.setattr(bdb, "profile_dir", lambda: tmp_path)
    monkeypatch.setattr(bdb, "_lock_pid", lambda d: None)
    monkeypatch.setattr(
        bdb,
        "_chromium_cmdline_tokens",
        lambda pid: (
            ["chrome", "--type=zygote", f"--user-data-dir={tmp_path}"]
            if pid == 4241 else
            ["chrome", "--type=utility", f"--user-data-dir={tmp_path}"]
            if pid == 4245 else
            ["agent-browser", "daemon", f"--user-data-dir={tmp_path}"]
            if pid == 4300 else
            ["agent-browser", "cri", f"--user-data-dir={tmp_path}"]
            if pid == leftover_cri else
            ["chrome", f"--user-data-dir={tmp_path}"]
        ),
    )
    monkeypatch.setattr(
        bdb,
        "_proc_ppid",
        lambda pid: {
            4240: 4300, 4241: 4240, 4245: 4241, 4242: 4300, 4243: 4300,
            4244: 4300,
        }.get(pid),
    )
    monkeypatch.setattr(
        bdb,
        "_launched_by_session",
        lambda pid: "h_review" if pid == 4240 else None,
    )
    monkeypatch.setattr(bdb, "_recover_cdp_port_from_singleton", lambda *a, **k: None)
    monkeypatch.setattr(
        bdb, "_this_jar_children",
        lambda parent, user_data_dir: (
            {4240, 4242, 4243, 4244} if parent == 4300 else set()
        ),
    )

    def _inodes(port):
        if port == 40141:
            return {7: {"::1"}}
        if port == 9333:
            return {8: {"::1"}}
        if port == 18888:
            return {9: {"::1"}}
        return {}

    def _holders(want):
        out = {}
        if 7 in want:
            out[4242] = {7}
            out[4243] = {7}
        if 8 in want:
            out[4240] = {8}
            out[4241] = {8}
            out[4245] = {8}
        if 9 in want:
            out[4244] = {9}
        return out

    monkeypatch.setattr(bdb, "_loopback_listen_inodes_for_port", _inodes)
    monkeypatch.setattr(bdb, "_pids_holding_socket_inodes", _holders)
    monkeypatch.setattr(
        bdb, "_loopback_listen_ports_for_pid",
        lambda pid: {40141} if pid in leftover_helpers or pid == 4300 else (
            {18888} if pid == leftover_cri else (
                {9333} if pid in chrome_family else set()
            )
        ),
    )
    monkeypatch.setattr(
        bdb, "_loopback_listen_targets_for_pid",
        lambda pid: {("::1", 40141)} if pid in leftover_helpers or pid == 4300 else (
            {("::1", 18888)} if pid == leftover_cri else (
                {("::1", 9333)} if pid in chrome_family else set()
            )
        ),
    )
    monkeypatch.setattr(
        bdb,
        "_cdp_port_reachable",
        lambda port, hosts: port in (40141, 18888) and "::1" in hosts,
    )
    monkeypatch.setattr(bdb, "_configured_cdp_override_url", lambda: "")
    (tmp_path / "DevToolsActivePort").write_text(
        "40141\n/devtools/browser/abc\n", encoding="utf-8",
    )
    _reset_dock_port_memory_for_tests()
    (tmp_path / "dock-cdp-port").write_text("18888\n", encoding="utf-8")
    assert bdb.lock_listed_persist_port() == 9333
    assert bdb._this_jar_chromium_pid(str(tmp_path)) == 4240
    assert bdb.shared_chromium_owner_session() == "h_review"
    assert _singleton_lock_pid(str(tmp_path)) == 4240
    assert bdb.running_instance_cdp_port(str(tmp_path)) is None
    assert _remembered_dock_attach_port() == 9333
    assert (tmp_path / "dock-cdp-port").read_text(encoding="utf-8").strip() == "18888"
    assert _last_dock_cdp_port.get(hermes_home_key()) is None
    assert _cdp_url_is_bot_desktop_browser("http://[::1]:40141") is True
    assert _cdp_url_is_bot_desktop_browser("http://[::1]:18888") is True
    assert _cdp_url_is_bot_desktop_browser("http://[::1]:9333") is True
    assert _cdp_url_is_bot_desktop_browser("9222") is False
    assert _leftover_cdp_aims_at_dock("http://[::1]:40141", 9333) is True
    assert _leftover_cdp_aims_at_dock("http://[::1]:18888", 9333) is True
    assert _leftover_host_port_aims_at_dock("::1", 40141, 9333) is True
    assert _leftover_host_port_aims_at_dock("::1", 18888, 9333) is True
    assert _leftover_host_port_aims_at_dock("127.0.0.1", 40141, 9333) is False
    assert _unregistered_cli_aims_at_dock(
        ["npx", "lighthouse", "https://example.com", "--port", "40141"],
        {}, tmp_path, 9333,
    ) is True
    assert _unregistered_cli_aims_at_dock(
        ["npx", "chrome-remote-interface", "--port", "18888", "inspect"],
        {}, tmp_path, 9333,
    ) is True
    assert _unregistered_cli_aims_at_dock(
        ["npx", "lighthouse", "--port", "9222", "https://example.com"],
        {}, tmp_path, 9333,
    ) is False
    assert _remembered_dock_attach_port() == 9333


def test_leftover_daemon_unique_cri_does_not_hide_sibling_chrome(
    monkeypatch, tmp_path,
):
    """Finding 195: leftover daemon unique CRI hid sibling chrome.

    Lock-gone n==1 treated leftover daemon that inherited leftover
    CRI as hidden chrome. Identify leftover on that stamp and on
    leftover DevTools; attach chrome. 9222 and the other family
    stay unknown. Leftover-only unique persist + leftover several
    stays 86.
    """
    import tools.bot_desktop.browser as bdb
    from hermes_constants import hermes_home_key
    from tools.browser_tool_session import (
        _cdp_url_is_bot_desktop_browser,
        _last_dock_cdp_port,
        _leftover_cdp_aims_at_dock,
        _leftover_host_port_aims_at_dock,
        _remembered_dock_attach_port,
        _reset_dock_port_memory_for_tests,
        _singleton_lock_pid,
        _unregistered_cli_aims_at_dock,
    )

    leftover_helpers = {4242, 4243}
    chrome_family = {4240, 4241, 4245}
    leftover_cri = 4244

    monkeypatch.setattr(runtime, "state_dir", lambda: tmp_path)
    monkeypatch.setattr(bdb, "profile_dir", lambda: tmp_path)
    monkeypatch.setattr(bdb, "_lock_pid", lambda d: None)
    monkeypatch.setattr(
        bdb,
        "_chromium_cmdline_tokens",
        lambda pid: (
            ["chrome", "--type=zygote", f"--user-data-dir={tmp_path}"]
            if pid == 4241 else
            ["chrome", "--type=utility", f"--user-data-dir={tmp_path}"]
            if pid == 4245 else
            ["agent-browser", "daemon", f"--user-data-dir={tmp_path}"]
            if pid == 4300 else
            ["agent-browser", "cri", f"--user-data-dir={tmp_path}"]
            if pid == leftover_cri else
            ["chrome", f"--user-data-dir={tmp_path}"]
        ),
    )
    monkeypatch.setattr(
        bdb,
        "_proc_ppid",
        lambda pid: {
            4240: 4300, 4241: 4240, 4245: 4241, 4242: 4300, 4243: 4300,
            4244: 4300,
        }.get(pid),
    )
    monkeypatch.setattr(
        bdb,
        "_launched_by_session",
        lambda pid: "h_review" if pid == 4240 else None,
    )
    monkeypatch.setattr(bdb, "_recover_cdp_port_from_singleton", lambda *a, **k: None)
    monkeypatch.setattr(
        bdb, "_this_jar_children",
        lambda parent, user_data_dir: (
            {4240, 4242, 4243, 4244} if parent == 4300 else set()
        ),
    )

    def _inodes(port):
        if port == 40141:
            return {7: {"::1"}}
        if port == 9333:
            return {8: {"::1"}}
        if port == 18888:
            return {9: {"::1"}}
        return {}

    def _holders(want):
        out = {}
        if 7 in want:
            out[4242] = {7}
            out[4243] = {7}
        if 8 in want:
            out[4240] = {8}
            out[4241] = {8}
            out[4245] = {8}
        if 9 in want:
            out[4300] = {9}
        return out

    monkeypatch.setattr(bdb, "_loopback_listen_inodes_for_port", _inodes)
    monkeypatch.setattr(bdb, "_pids_holding_socket_inodes", _holders)
    monkeypatch.setattr(
        bdb, "_loopback_listen_ports_for_pid",
        lambda pid: {40141, 18888} if pid == 4300 else (
            {40141} if pid in leftover_helpers else (
                {9333} if pid in chrome_family else set()
            )
        ),
    )
    monkeypatch.setattr(
        bdb, "_loopback_listen_targets_for_pid",
        lambda pid: {("::1", 40141), ("::1", 18888)} if pid == 4300 else (
            {("::1", 40141)} if pid in leftover_helpers else (
                {("::1", 9333)} if pid in chrome_family else set()
            )
        ),
    )
    monkeypatch.setattr(
        bdb,
        "_cdp_port_reachable",
        lambda port, hosts: port in (40141, 18888) and "::1" in hosts,
    )
    monkeypatch.setattr(bdb, "_configured_cdp_override_url", lambda: "")
    (tmp_path / "DevToolsActivePort").write_text(
        "40141\n/devtools/browser/abc\n", encoding="utf-8",
    )
    _reset_dock_port_memory_for_tests()
    (tmp_path / "dock-cdp-port").write_text("18888\n", encoding="utf-8")
    assert bdb.lock_listed_persist_port() == 9333
    assert bdb._this_jar_chromium_pid(str(tmp_path)) == 4240
    assert bdb.shared_chromium_owner_session() == "h_review"
    assert _singleton_lock_pid(str(tmp_path)) == 4240
    assert bdb.running_instance_cdp_port(str(tmp_path)) is None
    assert _remembered_dock_attach_port() == 9333
    assert (tmp_path / "dock-cdp-port").read_text(encoding="utf-8").strip() == "18888"
    assert _last_dock_cdp_port.get(hermes_home_key()) is None
    assert _cdp_url_is_bot_desktop_browser("http://[::1]:40141") is True
    assert _cdp_url_is_bot_desktop_browser("http://[::1]:18888") is True
    assert _cdp_url_is_bot_desktop_browser("http://[::1]:9333") is True
    assert _cdp_url_is_bot_desktop_browser("9222") is False
    assert _leftover_cdp_aims_at_dock("http://[::1]:40141", 9333) is True
    assert _leftover_cdp_aims_at_dock("http://[::1]:18888", 9333) is True
    assert _leftover_host_port_aims_at_dock("::1", 40141, 9333) is True
    assert _leftover_host_port_aims_at_dock("::1", 18888, 9333) is True
    assert _leftover_host_port_aims_at_dock("127.0.0.1", 40141, 9333) is False
    assert _unregistered_cli_aims_at_dock(
        ["npx", "lighthouse", "https://example.com", "--port", "40141"],
        {}, tmp_path, 9333,
    ) is True
    assert _unregistered_cli_aims_at_dock(
        ["npx", "chrome-remote-interface", "--port", "18888", "inspect"],
        {}, tmp_path, 9333,
    ) is True
    assert _unregistered_cli_aims_at_dock(
        ["npx", "lighthouse", "--port", "9222", "https://example.com"],
        {}, tmp_path, 9333,
    ) is False
    assert _remembered_dock_attach_port() == 9333


def test_leftover_shared_cri_does_not_hide_sibling_chrome(
    monkeypatch, tmp_path,
):
    """Finding 196: leftover-shared CRI hid sibling chrome.

    Leftover daemon and chrome both inherited leftover CRI, so
    leftover persist and chrome CDP both looked leftover-shared.
    Identify leftover on that stamp and on leftover DevTools;
    attach chrome. 9222 and the other family stay unknown.
    """
    import tools.bot_desktop.browser as bdb
    from hermes_constants import hermes_home_key
    from tools.browser_tool_session import (
        _cdp_url_is_bot_desktop_browser,
        _last_dock_cdp_port,
        _leftover_cdp_aims_at_dock,
        _leftover_host_port_aims_at_dock,
        _remembered_dock_attach_port,
        _reset_dock_port_memory_for_tests,
        _singleton_lock_pid,
        _unregistered_cli_aims_at_dock,
    )

    leftover_helpers = {4242, 4243}
    chrome_family = {4240, 4241, 4245}
    leftover_cri = 4244

    monkeypatch.setattr(runtime, "state_dir", lambda: tmp_path)
    monkeypatch.setattr(bdb, "profile_dir", lambda: tmp_path)
    monkeypatch.setattr(bdb, "_lock_pid", lambda d: None)
    monkeypatch.setattr(
        bdb,
        "_chromium_cmdline_tokens",
        lambda pid: (
            ["chrome", "--type=zygote", f"--user-data-dir={tmp_path}"]
            if pid == 4241 else
            ["chrome", "--type=utility", f"--user-data-dir={tmp_path}"]
            if pid == 4245 else
            ["agent-browser", "daemon", f"--user-data-dir={tmp_path}"]
            if pid == 4300 else
            ["agent-browser", "cri", f"--user-data-dir={tmp_path}"]
            if pid == leftover_cri else
            ["chrome", f"--user-data-dir={tmp_path}"]
        ),
    )
    monkeypatch.setattr(
        bdb,
        "_proc_ppid",
        lambda pid: {
            4240: 4300, 4241: 4240, 4245: 4241, 4242: 4300, 4243: 4300,
            4244: 4300,
        }.get(pid),
    )
    monkeypatch.setattr(
        bdb,
        "_launched_by_session",
        lambda pid: "h_review" if pid == 4240 else None,
    )
    monkeypatch.setattr(bdb, "_recover_cdp_port_from_singleton", lambda *a, **k: None)
    monkeypatch.setattr(
        bdb, "_this_jar_children",
        lambda parent, user_data_dir: (
            {4240, 4242, 4243, 4244} if parent == 4300 else set()
        ),
    )

    def _inodes(port):
        if port == 40141:
            return {7: {"::1"}}
        if port == 9333:
            return {8: {"::1"}}
        if port == 18888:
            return {9: {"::1"}}
        return {}

    def _holders(want):
        out = {}
        if 7 in want:
            out[4242] = {7}
            out[4243] = {7}
        if 8 in want:
            out[4240] = {8}
            out[4241] = {8}
            out[4245] = {8}
        if 9 in want:
            out[4300] = {9}
            out[4240] = {9}
        return out

    monkeypatch.setattr(bdb, "_loopback_listen_inodes_for_port", _inodes)
    monkeypatch.setattr(bdb, "_pids_holding_socket_inodes", _holders)
    monkeypatch.setattr(
        bdb, "_loopback_listen_ports_for_pid",
        lambda pid: {40141, 18888} if pid == 4300 else (
            {40141} if pid in leftover_helpers else (
                {9333, 18888} if pid == 4240 else (
                    {9333} if pid in chrome_family else set()
                )
            )
        ),
    )
    monkeypatch.setattr(
        bdb, "_loopback_listen_targets_for_pid",
        lambda pid: {("::1", 40141), ("::1", 18888)} if pid == 4300 else (
            {("::1", 40141)} if pid in leftover_helpers else (
                {("::1", 9333), ("::1", 18888)} if pid == 4240 else (
                    {("::1", 9333)} if pid in chrome_family else set()
                )
            )
        ),
    )
    monkeypatch.setattr(
        bdb,
        "_cdp_port_reachable",
        lambda port, hosts: port in (40141, 18888) and "::1" in hosts,
    )
    monkeypatch.setattr(bdb, "_configured_cdp_override_url", lambda: "")
    (tmp_path / "DevToolsActivePort").write_text(
        "40141\n/devtools/browser/abc\n", encoding="utf-8",
    )
    _reset_dock_port_memory_for_tests()
    (tmp_path / "dock-cdp-port").write_text("18888\n", encoding="utf-8")
    assert bdb.lock_listed_persist_port() == 9333
    assert bdb._this_jar_chromium_pid(str(tmp_path)) == 4240
    assert bdb.shared_chromium_owner_session() == "h_review"
    assert _singleton_lock_pid(str(tmp_path)) == 4240
    assert bdb.running_instance_cdp_port(str(tmp_path)) is None
    assert _remembered_dock_attach_port() == 9333
    assert (tmp_path / "dock-cdp-port").read_text(encoding="utf-8").strip() == "18888"
    assert _last_dock_cdp_port.get(hermes_home_key()) is None
    assert _cdp_url_is_bot_desktop_browser("http://[::1]:40141") is True
    assert _cdp_url_is_bot_desktop_browser("http://[::1]:18888") is True
    assert _cdp_url_is_bot_desktop_browser("http://[::1]:9333") is True
    assert _cdp_url_is_bot_desktop_browser("9222") is False
    assert _leftover_cdp_aims_at_dock("http://[::1]:40141", 9333) is True
    assert _leftover_cdp_aims_at_dock("http://[::1]:18888", 9333) is True
    assert _leftover_host_port_aims_at_dock("::1", 40141, 9333) is True
    assert _leftover_host_port_aims_at_dock("::1", 18888, 9333) is True
    assert _leftover_host_port_aims_at_dock("127.0.0.1", 40141, 9333) is False
    assert _unregistered_cli_aims_at_dock(
        ["npx", "lighthouse", "https://example.com", "--port", "40141"],
        {}, tmp_path, 9333,
    ) is True
    assert _unregistered_cli_aims_at_dock(
        ["npx", "chrome-remote-interface", "--port", "18888", "inspect"],
        {}, tmp_path, 9333,
    ) is True
    assert _unregistered_cli_aims_at_dock(
        ["npx", "lighthouse", "--port", "9222", "https://example.com"],
        {}, tmp_path, 9333,
    ) is False
    assert _remembered_dock_attach_port() == 9333


def test_unique_leftover_daemon_devtools_does_not_hide_sibling_chrome(
    monkeypatch, tmp_path,
):
    """Finding 197: unique leftover-daemon DevTools hid sibling chrome.

    Leftover fill clients exited and chrome dropped the inherited
    DevTools listen, so leftover daemon uniquely holds stale
    ``DevToolsActivePort``. Identify leftover on that stamp and
    on leftover persist; attach chrome. 9222 and the other family
    stay unknown.
    """
    import tools.bot_desktop.browser as bdb
    from hermes_constants import hermes_home_key
    from tools.browser_tool_session import (
        _cdp_url_is_bot_desktop_browser,
        _last_dock_cdp_port,
        _leftover_cdp_aims_at_dock,
        _leftover_host_port_aims_at_dock,
        _remembered_dock_attach_port,
        _reset_dock_port_memory_for_tests,
        _singleton_lock_pid,
        _unregistered_cli_aims_at_dock,
    )

    chrome_family = {4240, 4241, 4245}
    leftover_cri = 4244

    monkeypatch.setattr(runtime, "state_dir", lambda: tmp_path)
    monkeypatch.setattr(bdb, "profile_dir", lambda: tmp_path)
    monkeypatch.setattr(bdb, "_lock_pid", lambda d: None)
    monkeypatch.setattr(
        bdb,
        "_chromium_cmdline_tokens",
        lambda pid: (
            ["chrome", "--type=zygote", f"--user-data-dir={tmp_path}"]
            if pid == 4241 else
            ["chrome", "--type=utility", f"--user-data-dir={tmp_path}"]
            if pid == 4245 else
            ["agent-browser", "daemon", f"--user-data-dir={tmp_path}"]
            if pid == 4300 else
            ["agent-browser", "cri", f"--user-data-dir={tmp_path}"]
            if pid == leftover_cri else
            ["chrome", f"--user-data-dir={tmp_path}"]
        ),
    )
    monkeypatch.setattr(
        bdb,
        "_proc_ppid",
        lambda pid: {
            4240: 4300, 4241: 4240, 4245: 4241, 4244: 4300,
        }.get(pid),
    )
    monkeypatch.setattr(
        bdb,
        "_launched_by_session",
        lambda pid: "h_review" if pid == 4240 else None,
    )
    monkeypatch.setattr(bdb, "_recover_cdp_port_from_singleton", lambda *a, **k: None)
    monkeypatch.setattr(
        bdb, "_this_jar_children",
        lambda parent, user_data_dir: (
            {4240, 4244} if parent == 4300 else set()
        ),
    )

    def _inodes(port):
        if port == 40141:
            return {7: {"::1"}}
        if port == 9333:
            return {8: {"::1"}}
        if port == 18888:
            return {9: {"::1"}}
        return {}

    def _holders(want):
        out = {}
        if 7 in want:
            out[4300] = {7}
        if 8 in want:
            out[4240] = {8}
            out[4241] = {8}
            out[4245] = {8}
        if 9 in want:
            out[4244] = {9}
        return out

    monkeypatch.setattr(bdb, "_loopback_listen_inodes_for_port", _inodes)
    monkeypatch.setattr(bdb, "_pids_holding_socket_inodes", _holders)
    monkeypatch.setattr(
        bdb, "_loopback_listen_ports_for_pid",
        lambda pid: {40141} if pid == 4300 else (
            {18888} if pid == leftover_cri else (
                {9333} if pid in chrome_family else set()
            )
        ),
    )
    monkeypatch.setattr(
        bdb, "_loopback_listen_targets_for_pid",
        lambda pid: {("::1", 40141)} if pid == 4300 else (
            {("::1", 18888)} if pid == leftover_cri else (
                {("::1", 9333)} if pid in chrome_family else set()
            )
        ),
    )
    monkeypatch.setattr(
        bdb,
        "_cdp_port_reachable",
        lambda port, hosts: port in (40141, 18888) and "::1" in hosts,
    )
    monkeypatch.setattr(bdb, "_configured_cdp_override_url", lambda: "")
    (tmp_path / "DevToolsActivePort").write_text(
        "40141\n/devtools/browser/abc\n", encoding="utf-8",
    )
    _reset_dock_port_memory_for_tests()
    (tmp_path / "dock-cdp-port").write_text("18888\n", encoding="utf-8")
    assert bdb.lock_listed_persist_port() == 9333
    assert bdb._this_jar_chromium_pid(str(tmp_path)) == 4240
    assert bdb.shared_chromium_owner_session() == "h_review"
    assert _singleton_lock_pid(str(tmp_path)) == 4240
    assert bdb.running_instance_cdp_port(str(tmp_path)) is None
    assert _remembered_dock_attach_port() == 9333
    assert (tmp_path / "dock-cdp-port").read_text(encoding="utf-8").strip() == "18888"
    assert _last_dock_cdp_port.get(hermes_home_key()) is None
    assert _cdp_url_is_bot_desktop_browser("http://[::1]:40141") is True
    assert _cdp_url_is_bot_desktop_browser("http://[::1]:18888") is True
    assert _cdp_url_is_bot_desktop_browser("http://[::1]:9333") is True
    assert _cdp_url_is_bot_desktop_browser("9222") is False
    assert _leftover_cdp_aims_at_dock("http://[::1]:40141", 9333) is True
    assert _leftover_cdp_aims_at_dock("http://[::1]:18888", 9333) is True
    assert _leftover_host_port_aims_at_dock("::1", 40141, 9333) is True
    assert _leftover_host_port_aims_at_dock("::1", 18888, 9333) is True
    assert _leftover_host_port_aims_at_dock("127.0.0.1", 40141, 9333) is False
    assert _unregistered_cli_aims_at_dock(
        ["npx", "lighthouse", "https://example.com", "--port", "40141"],
        {}, tmp_path, 9333,
    ) is True
    assert _unregistered_cli_aims_at_dock(
        ["npx", "chrome-remote-interface", "--port", "18888", "inspect"],
        {}, tmp_path, 9333,
    ) is True
    assert _unregistered_cli_aims_at_dock(
        ["npx", "lighthouse", "--port", "9222", "https://example.com"],
        {}, tmp_path, 9333,
    ) is False
    assert _remembered_dock_attach_port() == 9333


def test_unique_leftover_fill_devtools_does_not_hide_sibling_chrome(
    monkeypatch, tmp_path,
):
    """Finding 198: unique leftover-fill DevTools hid sibling chrome.

    Leftover fill uniquely holds stale ``DevToolsActivePort``.
    Leftover daemon dropped that listen. Identify leftover on
    that stamp and on leftover persist; attach chrome. 9222
    and the other family stay unknown.
    """
    import tools.bot_desktop.browser as bdb
    from hermes_constants import hermes_home_key
    from tools.browser_tool_session import (
        _cdp_url_is_bot_desktop_browser,
        _last_dock_cdp_port,
        _leftover_cdp_aims_at_dock,
        _leftover_host_port_aims_at_dock,
        _remembered_dock_attach_port,
        _reset_dock_port_memory_for_tests,
        _singleton_lock_pid,
        _unregistered_cli_aims_at_dock,
    )

    chrome_family = {4240, 4241, 4245}
    leftover_fill = 4242
    leftover_cri = 4244

    monkeypatch.setattr(runtime, "state_dir", lambda: tmp_path)
    monkeypatch.setattr(bdb, "profile_dir", lambda: tmp_path)
    monkeypatch.setattr(bdb, "_lock_pid", lambda d: None)
    monkeypatch.setattr(
        bdb,
        "_chromium_cmdline_tokens",
        lambda pid: (
            ["chrome", "--type=zygote", f"--user-data-dir={tmp_path}"]
            if pid == 4241 else
            ["chrome", "--type=utility", f"--user-data-dir={tmp_path}"]
            if pid == 4245 else
            ["agent-browser", "daemon", f"--user-data-dir={tmp_path}"]
            if pid == 4300 else
            ["agent-browser", "fill", f"--user-data-dir={tmp_path}"]
            if pid == leftover_fill else
            ["agent-browser", "cri", f"--user-data-dir={tmp_path}"]
            if pid == leftover_cri else
            ["chrome", f"--user-data-dir={tmp_path}"]
        ),
    )
    monkeypatch.setattr(
        bdb,
        "_proc_ppid",
        lambda pid: {
            4240: 4300, 4241: 4240, 4245: 4241,
            leftover_fill: 4300, leftover_cri: 4300,
        }.get(pid),
    )
    monkeypatch.setattr(
        bdb,
        "_launched_by_session",
        lambda pid: "h_review" if pid == 4240 else None,
    )
    monkeypatch.setattr(bdb, "_recover_cdp_port_from_singleton", lambda *a, **k: None)
    monkeypatch.setattr(
        bdb, "_this_jar_children",
        lambda parent, user_data_dir: (
            {4240, leftover_fill, leftover_cri} if parent == 4300 else set()
        ),
    )

    def _inodes(port):
        if port == 40141:
            return {7: {"::1"}}
        if port == 9333:
            return {8: {"::1"}}
        if port == 18888:
            return {9: {"::1"}}
        return {}

    def _holders(want):
        out = {}
        if 7 in want:
            out[leftover_fill] = {7}
        if 8 in want:
            out[4240] = {8}
            out[4241] = {8}
            out[4245] = {8}
        if 9 in want:
            out[leftover_cri] = {9}
        return out

    monkeypatch.setattr(bdb, "_loopback_listen_inodes_for_port", _inodes)
    monkeypatch.setattr(bdb, "_pids_holding_socket_inodes", _holders)
    monkeypatch.setattr(
        bdb, "_loopback_listen_ports_for_pid",
        lambda pid: {40141} if pid == leftover_fill else (
            {18888} if pid == leftover_cri else (
                {9333} if pid in chrome_family else set()
            )
        ),
    )
    monkeypatch.setattr(
        bdb, "_loopback_listen_targets_for_pid",
        lambda pid: {("::1", 40141)} if pid == leftover_fill else (
            {("::1", 18888)} if pid == leftover_cri else (
                {("::1", 9333)} if pid in chrome_family else set()
            )
        ),
    )
    monkeypatch.setattr(
        bdb,
        "_cdp_port_reachable",
        lambda port, hosts: port in (40141, 18888) and "::1" in hosts,
    )
    monkeypatch.setattr(bdb, "_configured_cdp_override_url", lambda: "")
    (tmp_path / "DevToolsActivePort").write_text(
        "40141\n/devtools/browser/abc\n", encoding="utf-8",
    )
    _reset_dock_port_memory_for_tests()
    (tmp_path / "dock-cdp-port").write_text("18888\n", encoding="utf-8")
    assert bdb.lock_listed_persist_port() == 9333
    assert bdb._this_jar_chromium_pid(str(tmp_path)) == 4240
    assert bdb.shared_chromium_owner_session() == "h_review"
    assert _singleton_lock_pid(str(tmp_path)) == 4240
    assert bdb.running_instance_cdp_port(str(tmp_path)) is None
    assert _remembered_dock_attach_port() == 9333
    assert (tmp_path / "dock-cdp-port").read_text(encoding="utf-8").strip() == "18888"
    assert _last_dock_cdp_port.get(hermes_home_key()) is None
    assert _cdp_url_is_bot_desktop_browser("http://[::1]:40141") is True
    assert _cdp_url_is_bot_desktop_browser("http://[::1]:18888") is True
    assert _cdp_url_is_bot_desktop_browser("http://[::1]:9333") is True
    assert _cdp_url_is_bot_desktop_browser("9222") is False
    assert _leftover_cdp_aims_at_dock("http://[::1]:40141", 9333) is True
    assert _leftover_cdp_aims_at_dock("http://[::1]:18888", 9333) is True
    assert _leftover_host_port_aims_at_dock("::1", 40141, 9333) is True
    assert _leftover_host_port_aims_at_dock("::1", 18888, 9333) is True
    assert _leftover_host_port_aims_at_dock("127.0.0.1", 40141, 9333) is False
    assert _unregistered_cli_aims_at_dock(
        ["npx", "lighthouse", "https://example.com", "--port", "40141"],
        {}, tmp_path, 9333,
    ) is True
    assert _unregistered_cli_aims_at_dock(
        ["npx", "chrome-remote-interface", "--port", "18888", "inspect"],
        {}, tmp_path, 9333,
    ) is True
    assert _unregistered_cli_aims_at_dock(
        ["npx", "lighthouse", "--port", "9222", "https://example.com"],
        {}, tmp_path, 9333,
    ) is False
    assert _remembered_dock_attach_port() == 9333


def test_leftover_fill_inherited_persist_does_not_hide_sibling_chrome(
    monkeypatch, tmp_path,
):
    """Finding 199: leftover fill inherited leftover persist hid sibling chrome.

    Leftover fill uniquely holds stale ``DevToolsActivePort`` and
    also inherited leftover persist with chrome. Identify leftover
    on that stamp and on leftover persist; attach chrome. 9222
    and the other family stay unknown.
    """
    import tools.bot_desktop.browser as bdb
    from hermes_constants import hermes_home_key
    from tools.browser_tool_session import (
        _cdp_url_is_bot_desktop_browser,
        _last_dock_cdp_port,
        _leftover_cdp_aims_at_dock,
        _leftover_host_port_aims_at_dock,
        _remembered_dock_attach_port,
        _reset_dock_port_memory_for_tests,
        _singleton_lock_pid,
        _unregistered_cli_aims_at_dock,
    )

    leftover_fill = 4242
    leftover_cri = 4244

    monkeypatch.setattr(runtime, "state_dir", lambda: tmp_path)
    monkeypatch.setattr(bdb, "profile_dir", lambda: tmp_path)
    monkeypatch.setattr(bdb, "_lock_pid", lambda d: None)
    monkeypatch.setattr(
        bdb,
        "_chromium_cmdline_tokens",
        lambda pid: (
            ["chrome", "--type=zygote", f"--user-data-dir={tmp_path}"]
            if pid == 4241 else
            ["chrome", "--type=utility", f"--user-data-dir={tmp_path}"]
            if pid == 4245 else
            ["agent-browser", "daemon", f"--user-data-dir={tmp_path}"]
            if pid == 4300 else
            ["agent-browser", "fill", f"--user-data-dir={tmp_path}"]
            if pid == leftover_fill else
            ["agent-browser", "cri", f"--user-data-dir={tmp_path}"]
            if pid == leftover_cri else
            ["chrome", f"--user-data-dir={tmp_path}"]
        ),
    )
    monkeypatch.setattr(
        bdb,
        "_proc_ppid",
        lambda pid: {
            4240: 4300, 4241: 4240, 4245: 4241,
            leftover_fill: 4300, leftover_cri: 4300,
        }.get(pid),
    )
    monkeypatch.setattr(
        bdb,
        "_launched_by_session",
        lambda pid: "h_review" if pid == 4240 else None,
    )
    monkeypatch.setattr(bdb, "_recover_cdp_port_from_singleton", lambda *a, **k: None)
    monkeypatch.setattr(
        bdb, "_this_jar_children",
        lambda parent, user_data_dir: (
            {4240, leftover_fill, leftover_cri} if parent == 4300 else set()
        ),
    )

    def _inodes(port):
        if port == 40141:
            return {7: {"::1"}}
        if port == 9333:
            return {8: {"::1"}}
        if port == 18888:
            return {9: {"::1"}}
        return {}

    def _holders(want):
        out = {}
        if 7 in want:
            out[leftover_fill] = {7}
        if 8 in want:
            out[4240] = {8}
            out[4241] = {8}
            out[4245] = {8}
        if 9 in want:
            out[leftover_fill] = {9}
            out[4240] = {9}
        return out

    monkeypatch.setattr(bdb, "_loopback_listen_inodes_for_port", _inodes)
    monkeypatch.setattr(bdb, "_pids_holding_socket_inodes", _holders)
    monkeypatch.setattr(
        bdb, "_loopback_listen_ports_for_pid",
        lambda pid: {40141, 18888} if pid == leftover_fill else (
            {9333, 18888} if pid == 4240 else (
                {9333} if pid in {4241, 4245} else set()
            )
        ),
    )
    monkeypatch.setattr(
        bdb, "_loopback_listen_targets_for_pid",
        lambda pid: {("::1", 40141), ("::1", 18888)} if pid == leftover_fill else (
            {("::1", 9333), ("::1", 18888)} if pid == 4240 else (
                {("::1", 9333)} if pid in {4241, 4245} else set()
            )
        ),
    )
    monkeypatch.setattr(
        bdb,
        "_cdp_port_reachable",
        lambda port, hosts: port in (40141, 18888) and "::1" in hosts,
    )
    monkeypatch.setattr(bdb, "_configured_cdp_override_url", lambda: "")
    (tmp_path / "DevToolsActivePort").write_text(
        "40141\n/devtools/browser/abc\n", encoding="utf-8",
    )
    _reset_dock_port_memory_for_tests()
    (tmp_path / "dock-cdp-port").write_text("18888\n", encoding="utf-8")
    assert bdb.lock_listed_persist_port() == 9333
    assert bdb._this_jar_chromium_pid(str(tmp_path)) == 4240
    assert bdb.shared_chromium_owner_session() == "h_review"
    assert _singleton_lock_pid(str(tmp_path)) == 4240
    assert bdb.running_instance_cdp_port(str(tmp_path)) is None
    assert _remembered_dock_attach_port() == 9333
    assert (tmp_path / "dock-cdp-port").read_text(encoding="utf-8").strip() == "18888"
    assert _last_dock_cdp_port.get(hermes_home_key()) is None
    assert _cdp_url_is_bot_desktop_browser("http://[::1]:40141") is True
    assert _cdp_url_is_bot_desktop_browser("http://[::1]:18888") is True
    assert _cdp_url_is_bot_desktop_browser("http://[::1]:9333") is True
    assert _cdp_url_is_bot_desktop_browser("9222") is False
    assert _leftover_cdp_aims_at_dock("http://[::1]:40141", 9333) is True
    assert _leftover_cdp_aims_at_dock("http://[::1]:18888", 9333) is True
    assert _leftover_host_port_aims_at_dock("::1", 40141, 9333) is True
    assert _leftover_host_port_aims_at_dock("::1", 18888, 9333) is True
    assert _leftover_host_port_aims_at_dock("127.0.0.1", 40141, 9333) is False
    assert _unregistered_cli_aims_at_dock(
        ["npx", "lighthouse", "https://example.com", "--port", "40141"],
        {}, tmp_path, 9333,
    ) is True
    assert _unregistered_cli_aims_at_dock(
        ["npx", "chrome-remote-interface", "--port", "18888", "inspect"],
        {}, tmp_path, 9333,
    ) is True
    assert _unregistered_cli_aims_at_dock(
        ["npx", "lighthouse", "--port", "9222", "https://example.com"],
        {}, tmp_path, 9333,
    ) is False
    assert _remembered_dock_attach_port() == 9333


def test_leftover_python_inherited_chrome_cdp_does_not_hide_sibling_chrome(
    monkeypatch, tmp_path,
):
    """Finding 200: leftover python inherited chrome CDP hid sibling chrome.

    Leftover fill uniquely holds stale ``DevToolsActivePort``.
    Leftover python inherited chrome's CDP with the chrome
    family. Identify leftover on leftover DevTools; attach
    chrome. 9222 and the other family stay unknown.
    """
    import tools.bot_desktop.browser as bdb
    from hermes_constants import hermes_home_key
    from tools.browser_tool_session import (
        _cdp_url_is_bot_desktop_browser,
        _last_dock_cdp_port,
        _leftover_cdp_aims_at_dock,
        _leftover_host_port_aims_at_dock,
        _remembered_dock_attach_port,
        _reset_dock_port_memory_for_tests,
        _singleton_lock_pid,
        _unregistered_cli_aims_at_dock,
    )

    leftover_fill = 4242
    leftover_cri = 4244
    leftover_py = 4246

    monkeypatch.setattr(runtime, "state_dir", lambda: tmp_path)
    monkeypatch.setattr(bdb, "profile_dir", lambda: tmp_path)
    monkeypatch.setattr(bdb, "_lock_pid", lambda d: None)
    monkeypatch.setattr(
        bdb,
        "_chromium_cmdline_tokens",
        lambda pid: (
            ["chrome", "--type=zygote", f"--user-data-dir={tmp_path}"]
            if pid == 4241 else
            ["chrome", "--type=utility", f"--user-data-dir={tmp_path}"]
            if pid == 4245 else
            ["agent-browser", "daemon", f"--user-data-dir={tmp_path}"]
            if pid == 4300 else
            ["agent-browser", "fill", f"--user-data-dir={tmp_path}"]
            if pid == leftover_fill else
            ["agent-browser", "cri", f"--user-data-dir={tmp_path}"]
            if pid == leftover_cri else
            ["agent-browser", "python", f"--user-data-dir={tmp_path}"]
            if pid == leftover_py else
            ["chrome", f"--user-data-dir={tmp_path}"]
        ),
    )
    monkeypatch.setattr(
        bdb,
        "_proc_ppid",
        lambda pid: {
            4240: 4300, 4241: 4240, 4245: 4241,
            leftover_fill: 4300, leftover_cri: 4300, leftover_py: 4300,
        }.get(pid),
    )
    monkeypatch.setattr(
        bdb,
        "_launched_by_session",
        lambda pid: "h_review" if pid == 4240 else None,
    )
    monkeypatch.setattr(bdb, "_recover_cdp_port_from_singleton", lambda *a, **k: None)
    monkeypatch.setattr(
        bdb, "_this_jar_children",
        lambda parent, user_data_dir: (
            {4240, leftover_fill, leftover_cri, leftover_py}
            if parent == 4300 else set()
        ),
    )

    def _inodes(port):
        if port == 40141:
            return {7: {"::1"}}
        if port == 9333:
            return {8: {"::1"}}
        if port == 18888:
            return {9: {"::1"}}
        return {}

    def _holders(want):
        out = {}
        if 7 in want:
            out[leftover_fill] = {7}
        if 8 in want:
            out[4240] = {8}
            out[4241] = {8}
            out[4245] = {8}
            out[leftover_py] = {8}
        return out

    monkeypatch.setattr(bdb, "_loopback_listen_inodes_for_port", _inodes)
    monkeypatch.setattr(bdb, "_pids_holding_socket_inodes", _holders)
    monkeypatch.setattr(
        bdb, "_loopback_listen_ports_for_pid",
        lambda pid: {40141} if pid == leftover_fill else (
            {9333} if pid == leftover_py else (
                {9333} if pid in {4240, 4241, 4245} else set()
            )
        ),
    )
    monkeypatch.setattr(
        bdb, "_loopback_listen_targets_for_pid",
        lambda pid: {("::1", 40141)} if pid == leftover_fill else (
            {("::1", 9333)} if pid in {4240, 4241, 4245, leftover_py} else set()
        ),
    )
    monkeypatch.setattr(
        bdb,
        "_cdp_port_reachable",
        lambda port, hosts: port == 40141 and "::1" in hosts,
    )
    monkeypatch.setattr(bdb, "_configured_cdp_override_url", lambda: "")
    (tmp_path / "DevToolsActivePort").write_text(
        "40141\n/devtools/browser/abc\n", encoding="utf-8",
    )
    _reset_dock_port_memory_for_tests()
    (tmp_path / "dock-cdp-port").write_text("18888\n", encoding="utf-8")
    assert bdb.lock_listed_persist_port() == 9333
    assert bdb._this_jar_chromium_pid(str(tmp_path)) == 4240
    assert bdb.shared_chromium_owner_session() == "h_review"
    assert _singleton_lock_pid(str(tmp_path)) == 4240
    assert bdb.running_instance_cdp_port(str(tmp_path)) is None
    assert _remembered_dock_attach_port() == 9333
    assert (tmp_path / "dock-cdp-port").read_text(encoding="utf-8").strip() == "18888"
    assert _last_dock_cdp_port.get(hermes_home_key()) is None
    assert _cdp_url_is_bot_desktop_browser("http://[::1]:40141") is True
    assert _cdp_url_is_bot_desktop_browser("http://[::1]:9333") is True
    assert _cdp_url_is_bot_desktop_browser("9222") is False
    assert _leftover_cdp_aims_at_dock("http://[::1]:40141", 9333) is True
    assert _leftover_host_port_aims_at_dock("::1", 40141, 9333) is True
    assert _leftover_host_port_aims_at_dock("127.0.0.1", 40141, 9333) is False
    assert _unregistered_cli_aims_at_dock(
        ["npx", "lighthouse", "https://example.com", "--port", "40141"],
        {}, tmp_path, 9333,
    ) is True
    assert _unregistered_cli_aims_at_dock(
        ["npx", "lighthouse", "--port", "9222", "https://example.com"],
        {}, tmp_path, 9333,
    ) is False
    assert _remembered_dock_attach_port() == 9333


def test_leftover_python_inherited_persist_does_not_hide_sibling_chrome(
    monkeypatch, tmp_path,
):
    """Finding 201: leftover python inherited leftover persist hid sibling chrome.

    Leftover fill uniquely holds stale ``DevToolsActivePort``.
    Leftover python inherited leftover persist with chrome.
    Identify leftover on leftover DevTools and leftover
    persist; attach chrome. 9222 and the other family stay
    unknown.
    """
    import tools.bot_desktop.browser as bdb
    from hermes_constants import hermes_home_key
    from tools.browser_tool_session import (
        _cdp_url_is_bot_desktop_browser,
        _last_dock_cdp_port,
        _leftover_cdp_aims_at_dock,
        _leftover_host_port_aims_at_dock,
        _remembered_dock_attach_port,
        _reset_dock_port_memory_for_tests,
        _singleton_lock_pid,
        _unregistered_cli_aims_at_dock,
    )

    leftover_fill = 4242
    leftover_cri = 4244
    leftover_py = 4246

    monkeypatch.setattr(runtime, "state_dir", lambda: tmp_path)
    monkeypatch.setattr(bdb, "profile_dir", lambda: tmp_path)
    monkeypatch.setattr(bdb, "_lock_pid", lambda d: None)
    monkeypatch.setattr(
        bdb,
        "_chromium_cmdline_tokens",
        lambda pid: (
            ["chrome", "--type=zygote", f"--user-data-dir={tmp_path}"]
            if pid == 4241 else
            ["chrome", "--type=utility", f"--user-data-dir={tmp_path}"]
            if pid == 4245 else
            ["agent-browser", "daemon", f"--user-data-dir={tmp_path}"]
            if pid == 4300 else
            ["agent-browser", "fill", f"--user-data-dir={tmp_path}"]
            if pid == leftover_fill else
            ["agent-browser", "cri", f"--user-data-dir={tmp_path}"]
            if pid == leftover_cri else
            ["agent-browser", "python", f"--user-data-dir={tmp_path}"]
            if pid == leftover_py else
            ["chrome", f"--user-data-dir={tmp_path}"]
        ),
    )
    monkeypatch.setattr(
        bdb,
        "_proc_ppid",
        lambda pid: {
            4240: 4300, 4241: 4240, 4245: 4241,
            leftover_fill: 4300, leftover_cri: 4300, leftover_py: 4300,
        }.get(pid),
    )
    monkeypatch.setattr(
        bdb,
        "_launched_by_session",
        lambda pid: "h_review" if pid == 4240 else None,
    )
    monkeypatch.setattr(bdb, "_recover_cdp_port_from_singleton", lambda *a, **k: None)
    monkeypatch.setattr(
        bdb, "_this_jar_children",
        lambda parent, user_data_dir: (
            {4240, leftover_fill, leftover_cri, leftover_py}
            if parent == 4300 else set()
        ),
    )

    def _inodes(port):
        if port == 40141:
            return {7: {"::1"}}
        if port == 9333:
            return {8: {"::1"}}
        if port == 18888:
            return {9: {"::1"}}
        return {}

    def _holders(want):
        out = {}
        if 7 in want:
            out[leftover_fill] = {7}
        if 8 in want:
            out[4240] = {8}
            out[4241] = {8}
            out[4245] = {8}
        if 9 in want:
            out[leftover_py] = {9}
            out[4240] = out.get(4240, set()) | {9}
        return out

    monkeypatch.setattr(bdb, "_loopback_listen_inodes_for_port", _inodes)
    monkeypatch.setattr(bdb, "_pids_holding_socket_inodes", _holders)
    monkeypatch.setattr(
        bdb, "_loopback_listen_ports_for_pid",
        lambda pid: {40141} if pid == leftover_fill else (
            {18888} if pid == leftover_py else (
                {9333, 18888} if pid == 4240 else (
                    {9333} if pid in {4241, 4245} else set()
                )
            )
        ),
    )
    monkeypatch.setattr(
        bdb, "_loopback_listen_targets_for_pid",
        lambda pid: {("::1", 40141)} if pid == leftover_fill else (
            {("::1", 18888)} if pid == leftover_py else (
                {("::1", 9333), ("::1", 18888)} if pid == 4240 else (
                    {("::1", 9333)} if pid in {4241, 4245} else set()
                )
            )
        ),
    )
    monkeypatch.setattr(
        bdb,
        "_cdp_port_reachable",
        lambda port, hosts: port in (40141, 18888) and "::1" in hosts,
    )
    monkeypatch.setattr(bdb, "_configured_cdp_override_url", lambda: "")
    (tmp_path / "DevToolsActivePort").write_text(
        "40141\n/devtools/browser/abc\n", encoding="utf-8",
    )
    _reset_dock_port_memory_for_tests()
    (tmp_path / "dock-cdp-port").write_text("18888\n", encoding="utf-8")
    assert bdb.lock_listed_persist_port() == 9333
    assert bdb._this_jar_chromium_pid(str(tmp_path)) == 4240
    assert bdb.shared_chromium_owner_session() == "h_review"
    assert _singleton_lock_pid(str(tmp_path)) == 4240
    assert bdb.running_instance_cdp_port(str(tmp_path)) is None
    assert _remembered_dock_attach_port() == 9333
    assert (tmp_path / "dock-cdp-port").read_text(encoding="utf-8").strip() == "18888"
    assert _last_dock_cdp_port.get(hermes_home_key()) is None
    assert _cdp_url_is_bot_desktop_browser("http://[::1]:40141") is True
    assert _cdp_url_is_bot_desktop_browser("http://[::1]:18888") is True
    assert _cdp_url_is_bot_desktop_browser("http://[::1]:9333") is True
    assert _cdp_url_is_bot_desktop_browser("9222") is False
    assert _leftover_cdp_aims_at_dock("http://[::1]:40141", 9333) is True
    assert _leftover_cdp_aims_at_dock("http://[::1]:18888", 9333) is True
    assert _leftover_host_port_aims_at_dock("::1", 40141, 9333) is True
    assert _leftover_host_port_aims_at_dock("::1", 18888, 9333) is True
    assert _leftover_host_port_aims_at_dock("127.0.0.1", 40141, 9333) is False
    assert _unregistered_cli_aims_at_dock(
        ["npx", "lighthouse", "https://example.com", "--port", "40141"],
        {}, tmp_path, 9333,
    ) is True
    assert _unregistered_cli_aims_at_dock(
        ["npx", "chrome-remote-interface", "--port", "18888", "inspect"],
        {}, tmp_path, 9333,
    ) is True
    assert _unregistered_cli_aims_at_dock(
        ["npx", "lighthouse", "--port", "9222", "https://example.com"],
        {}, tmp_path, 9333,
    ) is False
    assert _remembered_dock_attach_port() == 9333


def test_leftover_fill_child_python_inherited_persist_does_not_hide_sibling_chrome(
    monkeypatch, tmp_path,
):
    """Finding 202: leftover fill's leftover python hid sibling chrome.

    Leftover fill uniquely holds stale ``DevToolsActivePort``.
    Leftover python spawned by leftover fill inherited leftover
    persist with chrome. Finding 201 leftover-sibling-miss only
    walked leftover daemon's children. Identify leftover on
    leftover DevTools and leftover persist; attach chrome.
    9222 and the other family stay unknown. Attach must not
    stamp persist.
    """
    import tools.bot_desktop.browser as bdb
    from hermes_constants import hermes_home_key
    from tools.browser_tool_session import (
        _cdp_url_is_bot_desktop_browser,
        _last_dock_cdp_port,
        _leftover_cdp_aims_at_dock,
        _leftover_host_port_aims_at_dock,
        _remembered_dock_attach_port,
        _reset_dock_port_memory_for_tests,
        _singleton_lock_pid,
        _unregistered_cli_aims_at_dock,
    )

    leftover_fill = 4242
    leftover_cri = 4244
    leftover_py = 4246

    monkeypatch.setattr(runtime, "state_dir", lambda: tmp_path)
    monkeypatch.setattr(bdb, "profile_dir", lambda: tmp_path)
    monkeypatch.setattr(bdb, "_lock_pid", lambda d: None)
    monkeypatch.setattr(
        bdb,
        "_chromium_cmdline_tokens",
        lambda pid: (
            ["chrome", "--type=zygote", f"--user-data-dir={tmp_path}"]
            if pid == 4241 else
            ["chrome", "--type=utility", f"--user-data-dir={tmp_path}"]
            if pid == 4245 else
            ["agent-browser", "daemon", f"--user-data-dir={tmp_path}"]
            if pid == 4300 else
            ["agent-browser", "fill", f"--user-data-dir={tmp_path}"]
            if pid == leftover_fill else
            ["agent-browser", "cri", f"--user-data-dir={tmp_path}"]
            if pid == leftover_cri else
            ["agent-browser", "python", f"--user-data-dir={tmp_path}"]
            if pid == leftover_py else
            ["chrome", f"--user-data-dir={tmp_path}"]
        ),
    )
    monkeypatch.setattr(
        bdb,
        "_proc_ppid",
        lambda pid: {
            4240: 4300, 4241: 4240, 4245: 4241,
            leftover_fill: 4300, leftover_cri: 4300, leftover_py: leftover_fill,
        }.get(pid),
    )
    monkeypatch.setattr(
        bdb,
        "_launched_by_session",
        lambda pid: "h_review" if pid == 4240 else None,
    )
    monkeypatch.setattr(bdb, "_recover_cdp_port_from_singleton", lambda *a, **k: None)
    monkeypatch.setattr(
        bdb, "_this_jar_children",
        lambda parent, user_data_dir: (
            {4240, leftover_fill, leftover_cri} if parent == 4300 else (
                {leftover_py} if parent == leftover_fill else set()
            )
        ),
    )

    def _inodes(port):
        if port == 40141:
            return {7: {"::1"}}
        if port == 9333:
            return {8: {"::1"}}
        if port == 18888:
            return {9: {"::1"}}
        return {}

    def _holders(want):
        out = {}
        if 7 in want:
            out[leftover_fill] = {7}
        if 8 in want:
            out[4240] = {8}
            out[4241] = {8}
            out[4245] = {8}
        if 9 in want:
            out[leftover_py] = {9}
            out[4240] = out.get(4240, set()) | {9}
        return out

    monkeypatch.setattr(bdb, "_loopback_listen_inodes_for_port", _inodes)
    monkeypatch.setattr(bdb, "_pids_holding_socket_inodes", _holders)
    monkeypatch.setattr(
        bdb, "_loopback_listen_ports_for_pid",
        lambda pid: {40141} if pid == leftover_fill else (
            {18888} if pid == leftover_py else (
                {9333, 18888} if pid == 4240 else (
                    {9333} if pid in {4241, 4245} else set()
                )
            )
        ),
    )
    monkeypatch.setattr(
        bdb, "_loopback_listen_targets_for_pid",
        lambda pid: {("::1", 40141)} if pid == leftover_fill else (
            {("::1", 18888)} if pid == leftover_py else (
                {("::1", 9333), ("::1", 18888)} if pid == 4240 else (
                    {("::1", 9333)} if pid in {4241, 4245} else set()
                )
            )
        ),
    )
    monkeypatch.setattr(
        bdb,
        "_cdp_port_reachable",
        lambda port, hosts: port in (40141, 18888) and "::1" in hosts,
    )
    monkeypatch.setattr(bdb, "_configured_cdp_override_url", lambda: "")
    (tmp_path / "DevToolsActivePort").write_text(
        "40141\n/devtools/browser/abc\n", encoding="utf-8",
    )
    _reset_dock_port_memory_for_tests()
    (tmp_path / "dock-cdp-port").write_text("18888\n", encoding="utf-8")
    assert bdb.lock_listed_persist_port() == 9333
    assert bdb._this_jar_chromium_pid(str(tmp_path)) == 4240
    assert bdb.shared_chromium_owner_session() == "h_review"
    assert _singleton_lock_pid(str(tmp_path)) == 4240
    assert bdb.running_instance_cdp_port(str(tmp_path)) is None
    assert _remembered_dock_attach_port() == 9333
    assert (tmp_path / "dock-cdp-port").read_text(encoding="utf-8").strip() == "18888"
    assert _last_dock_cdp_port.get(hermes_home_key()) is None
    assert _cdp_url_is_bot_desktop_browser("http://[::1]:40141") is True
    assert _cdp_url_is_bot_desktop_browser("http://[::1]:18888") is True
    assert _cdp_url_is_bot_desktop_browser("http://[::1]:9333") is True
    assert _cdp_url_is_bot_desktop_browser("9222") is False
    assert _leftover_cdp_aims_at_dock("http://[::1]:40141", 9333) is True
    assert _leftover_cdp_aims_at_dock("http://[::1]:18888", 9333) is True
    assert _leftover_host_port_aims_at_dock("::1", 40141, 9333) is True
    assert _leftover_host_port_aims_at_dock("::1", 18888, 9333) is True
    assert _leftover_host_port_aims_at_dock("127.0.0.1", 40141, 9333) is False
    assert _unregistered_cli_aims_at_dock(
        ["npx", "lighthouse", "https://example.com", "--port", "40141"],
        {}, tmp_path, 9333,
    ) is True
    assert _unregistered_cli_aims_at_dock(
        ["npx", "chrome-remote-interface", "--port", "18888", "inspect"],
        {}, tmp_path, 9333,
    ) is True
    assert _unregistered_cli_aims_at_dock(
        ["npx", "lighthouse", "--port", "9222", "https://example.com"],
        {}, tmp_path, 9333,
    ) is False
    assert _remembered_dock_attach_port() == 9333


def test_leftover_fill_grandchild_python_inherited_persist_does_not_hide_sibling_chrome(
    monkeypatch, tmp_path,
):
    """Finding 203: leftover fill's leftover grandchild hid sibling chrome.

    Leftover fill uniquely holds stale ``DevToolsActivePort``.
    Leftover fill spawned leftover CRI which spawned leftover
    python that inherited leftover persist with chrome.
    Finding 202 leftover-siblings'-children hop is leftover
    CRI, not leftover python. Identify leftover on leftover
    DevTools and leftover persist; attach chrome. 9222 and
    the other family stay unknown. Attach must not stamp
    persist.
    """
    import tools.bot_desktop.browser as bdb
    from hermes_constants import hermes_home_key
    from tools.browser_tool_session import (
        _cdp_url_is_bot_desktop_browser,
        _last_dock_cdp_port,
        _leftover_cdp_aims_at_dock,
        _leftover_host_port_aims_at_dock,
        _remembered_dock_attach_port,
        _reset_dock_port_memory_for_tests,
        _singleton_lock_pid,
        _unregistered_cli_aims_at_dock,
    )

    leftover_fill = 4242
    leftover_cri = 4244
    leftover_py = 4246

    monkeypatch.setattr(runtime, "state_dir", lambda: tmp_path)
    monkeypatch.setattr(bdb, "profile_dir", lambda: tmp_path)
    monkeypatch.setattr(bdb, "_lock_pid", lambda d: None)
    monkeypatch.setattr(
        bdb,
        "_chromium_cmdline_tokens",
        lambda pid: (
            ["chrome", "--type=zygote", f"--user-data-dir={tmp_path}"]
            if pid == 4241 else
            ["chrome", "--type=utility", f"--user-data-dir={tmp_path}"]
            if pid == 4245 else
            ["agent-browser", "daemon", f"--user-data-dir={tmp_path}"]
            if pid == 4300 else
            ["agent-browser", "fill", f"--user-data-dir={tmp_path}"]
            if pid == leftover_fill else
            ["agent-browser", "cri", f"--user-data-dir={tmp_path}"]
            if pid == leftover_cri else
            ["agent-browser", "python", f"--user-data-dir={tmp_path}"]
            if pid == leftover_py else
            ["chrome", f"--user-data-dir={tmp_path}"]
        ),
    )
    monkeypatch.setattr(
        bdb,
        "_proc_ppid",
        lambda pid: {
            4240: 4300, 4241: 4240, 4245: 4241,
            leftover_fill: 4300, leftover_cri: leftover_fill, leftover_py: leftover_cri,
        }.get(pid),
    )
    monkeypatch.setattr(
        bdb,
        "_launched_by_session",
        lambda pid: "h_review" if pid == 4240 else None,
    )
    monkeypatch.setattr(bdb, "_recover_cdp_port_from_singleton", lambda *a, **k: None)
    monkeypatch.setattr(
        bdb, "_this_jar_children",
        lambda parent, user_data_dir: (
            {4240, leftover_fill} if parent == 4300 else (
                {leftover_cri} if parent == leftover_fill else (
                    {leftover_py} if parent == leftover_cri else set()
                )
            )
        ),
    )

    def _inodes(port):
        if port == 40141:
            return {7: {"::1"}}
        if port == 9333:
            return {8: {"::1"}}
        if port == 18888:
            return {9: {"::1"}}
        return {}

    def _holders(want):
        out = {}
        if 7 in want:
            out[leftover_fill] = {7}
        if 8 in want:
            out[4240] = {8}
            out[4241] = {8}
            out[4245] = {8}
        if 9 in want:
            out[leftover_py] = {9}
            out[4240] = out.get(4240, set()) | {9}
        return out

    monkeypatch.setattr(bdb, "_loopback_listen_inodes_for_port", _inodes)
    monkeypatch.setattr(bdb, "_pids_holding_socket_inodes", _holders)
    monkeypatch.setattr(
        bdb, "_loopback_listen_ports_for_pid",
        lambda pid: {40141} if pid == leftover_fill else (
            {18888} if pid == leftover_py else (
                {9333, 18888} if pid == 4240 else (
                    {9333} if pid in {4241, 4245} else set()
                )
            )
        ),
    )
    monkeypatch.setattr(
        bdb, "_loopback_listen_targets_for_pid",
        lambda pid: {("::1", 40141)} if pid == leftover_fill else (
            {("::1", 18888)} if pid == leftover_py else (
                {("::1", 9333), ("::1", 18888)} if pid == 4240 else (
                    {("::1", 9333)} if pid in {4241, 4245} else set()
                )
            )
        ),
    )
    monkeypatch.setattr(
        bdb,
        "_cdp_port_reachable",
        lambda port, hosts: port in (40141, 18888) and "::1" in hosts,
    )
    monkeypatch.setattr(bdb, "_configured_cdp_override_url", lambda: "")
    (tmp_path / "DevToolsActivePort").write_text(
        "40141\n/devtools/browser/abc\n", encoding="utf-8",
    )
    _reset_dock_port_memory_for_tests()
    (tmp_path / "dock-cdp-port").write_text("18888\n", encoding="utf-8")
    assert bdb.lock_listed_persist_port() == 9333
    assert bdb._this_jar_chromium_pid(str(tmp_path)) == 4240
    assert bdb.shared_chromium_owner_session() == "h_review"
    assert _singleton_lock_pid(str(tmp_path)) == 4240
    assert bdb.running_instance_cdp_port(str(tmp_path)) is None
    assert _remembered_dock_attach_port() == 9333
    assert (tmp_path / "dock-cdp-port").read_text(encoding="utf-8").strip() == "18888"
    assert _last_dock_cdp_port.get(hermes_home_key()) is None
    assert _cdp_url_is_bot_desktop_browser("http://[::1]:40141") is True
    assert _cdp_url_is_bot_desktop_browser("http://[::1]:18888") is True
    assert _cdp_url_is_bot_desktop_browser("http://[::1]:9333") is True
    assert _cdp_url_is_bot_desktop_browser("9222") is False
    assert _leftover_cdp_aims_at_dock("http://[::1]:40141", 9333) is True
    assert _leftover_cdp_aims_at_dock("http://[::1]:18888", 9333) is True
    assert _leftover_host_port_aims_at_dock("::1", 40141, 9333) is True
    assert _leftover_host_port_aims_at_dock("::1", 18888, 9333) is True
    assert _leftover_host_port_aims_at_dock("127.0.0.1", 40141, 9333) is False
    assert _unregistered_cli_aims_at_dock(
        ["npx", "lighthouse", "https://example.com", "--port", "40141"],
        {}, tmp_path, 9333,
    ) is True
    assert _unregistered_cli_aims_at_dock(
        ["npx", "chrome-remote-interface", "--port", "18888", "inspect"],
        {}, tmp_path, 9333,
    ) is True
    assert _unregistered_cli_aims_at_dock(
        ["npx", "lighthouse", "--port", "9222", "https://example.com"],
        {}, tmp_path, 9333,
    ) is False
    assert _remembered_dock_attach_port() == 9333


def test_leftover_fill_and_cri_devtools_grandchild_persist_does_not_hide_sibling_chrome(
    monkeypatch, tmp_path,
):
    """Finding 204: leftover fill + leftover CRI leftover DevTools hid sibling chrome.

    Leftover fill and leftover fill's leftover CRI both hold stale
    ``DevToolsActivePort``. leftover CRI's leftover parent leftover
    fill is leftover file holder, so unique leftover-file parent
    was several and leftover-inherited never ran. Leftover python
    leftover CRI's leftover child inherited leftover persist with
    chrome. Identify leftover on leftover DevTools and leftover
    persist; attach chrome. 9222 and the other family stay
    unknown. Attach must not stamp persist.
    """
    import tools.bot_desktop.browser as bdb
    from hermes_constants import hermes_home_key
    from tools.browser_tool_session import (
        _cdp_url_is_bot_desktop_browser,
        _last_dock_cdp_port,
        _leftover_cdp_aims_at_dock,
        _leftover_host_port_aims_at_dock,
        _remembered_dock_attach_port,
        _reset_dock_port_memory_for_tests,
        _singleton_lock_pid,
        _unregistered_cli_aims_at_dock,
    )

    leftover_fill = 4242
    leftover_cri = 4244
    leftover_py = 4246

    monkeypatch.setattr(runtime, "state_dir", lambda: tmp_path)
    monkeypatch.setattr(bdb, "profile_dir", lambda: tmp_path)
    monkeypatch.setattr(bdb, "_lock_pid", lambda d: None)
    monkeypatch.setattr(
        bdb,
        "_chromium_cmdline_tokens",
        lambda pid: (
            ["chrome", "--type=zygote", f"--user-data-dir={tmp_path}"]
            if pid == 4241 else
            ["chrome", "--type=utility", f"--user-data-dir={tmp_path}"]
            if pid == 4245 else
            ["agent-browser", "daemon", f"--user-data-dir={tmp_path}"]
            if pid == 4300 else
            ["agent-browser", "fill", f"--user-data-dir={tmp_path}"]
            if pid == leftover_fill else
            ["agent-browser", "cri", f"--user-data-dir={tmp_path}"]
            if pid == leftover_cri else
            ["agent-browser", "python", f"--user-data-dir={tmp_path}"]
            if pid == leftover_py else
            ["chrome", f"--user-data-dir={tmp_path}"]
        ),
    )
    monkeypatch.setattr(
        bdb,
        "_proc_ppid",
        lambda pid: {
            4240: 4300, 4241: 4240, 4245: 4241,
            leftover_fill: 4300, leftover_cri: leftover_fill, leftover_py: leftover_cri,
        }.get(pid),
    )
    monkeypatch.setattr(
        bdb,
        "_launched_by_session",
        lambda pid: "h_review" if pid == 4240 else None,
    )
    monkeypatch.setattr(bdb, "_recover_cdp_port_from_singleton", lambda *a, **k: None)
    monkeypatch.setattr(
        bdb, "_this_jar_children",
        lambda parent, user_data_dir: (
            {4240, leftover_fill} if parent == 4300 else (
                {leftover_cri} if parent == leftover_fill else (
                    {leftover_py} if parent == leftover_cri else set()
                )
            )
        ),
    )

    def _inodes(port):
        if port == 40141:
            return {7: {"::1"}}
        if port == 9333:
            return {8: {"::1"}}
        if port == 18888:
            return {9: {"::1"}}
        return {}

    def _holders(want):
        out = {}
        if 7 in want:
            out[leftover_fill] = {7}
            out[leftover_cri] = {7}
        if 8 in want:
            out[4240] = {8}
            out[4241] = {8}
            out[4245] = {8}
        if 9 in want:
            out[leftover_py] = {9}
            out[4240] = out.get(4240, set()) | {9}
        return out

    monkeypatch.setattr(bdb, "_loopback_listen_inodes_for_port", _inodes)
    monkeypatch.setattr(bdb, "_pids_holding_socket_inodes", _holders)
    monkeypatch.setattr(
        bdb, "_loopback_listen_ports_for_pid",
        lambda pid: {40141} if pid in {leftover_fill, leftover_cri} else (
            {18888} if pid == leftover_py else (
                {9333, 18888} if pid == 4240 else (
                    {9333} if pid in {4241, 4245} else set()
                )
            )
        ),
    )
    monkeypatch.setattr(
        bdb, "_loopback_listen_targets_for_pid",
        lambda pid: {("::1", 40141)} if pid in {leftover_fill, leftover_cri} else (
            {("::1", 18888)} if pid == leftover_py else (
                {("::1", 9333), ("::1", 18888)} if pid == 4240 else (
                    {("::1", 9333)} if pid in {4241, 4245} else set()
                )
            )
        ),
    )
    monkeypatch.setattr(
        bdb,
        "_cdp_port_reachable",
        lambda port, hosts: port in (40141, 18888) and "::1" in hosts,
    )
    monkeypatch.setattr(bdb, "_configured_cdp_override_url", lambda: "")
    (tmp_path / "DevToolsActivePort").write_text(
        "40141\n/devtools/browser/abc\n", encoding="utf-8",
    )
    _reset_dock_port_memory_for_tests()
    (tmp_path / "dock-cdp-port").write_text("18888\n", encoding="utf-8")
    assert bdb._unique_this_jar_parent(
        {leftover_fill, leftover_cri}, str(tmp_path),
    ) == 4300
    assert bdb.lock_listed_persist_port() == 9333
    assert bdb._this_jar_chromium_pid(str(tmp_path)) == 4240
    assert bdb.shared_chromium_owner_session() == "h_review"
    assert _singleton_lock_pid(str(tmp_path)) == 4240
    assert bdb.running_instance_cdp_port(str(tmp_path)) is None
    assert _remembered_dock_attach_port() == 9333
    assert (tmp_path / "dock-cdp-port").read_text(encoding="utf-8").strip() == "18888"
    assert _last_dock_cdp_port.get(hermes_home_key()) is None
    assert _cdp_url_is_bot_desktop_browser("http://[::1]:40141") is True
    assert _cdp_url_is_bot_desktop_browser("http://[::1]:18888") is True
    assert _cdp_url_is_bot_desktop_browser("http://[::1]:9333") is True
    assert _cdp_url_is_bot_desktop_browser("9222") is False
    assert _leftover_cdp_aims_at_dock("http://[::1]:40141", 9333) is True
    assert _leftover_cdp_aims_at_dock("http://[::1]:18888", 9333) is True
    assert _leftover_host_port_aims_at_dock("::1", 40141, 9333) is True
    assert _leftover_host_port_aims_at_dock("::1", 18888, 9333) is True
    assert _leftover_host_port_aims_at_dock("127.0.0.1", 40141, 9333) is False
    assert _unregistered_cli_aims_at_dock(
        ["npx", "lighthouse", "https://example.com", "--port", "40141"],
        {}, tmp_path, 9333,
    ) is True
    assert _unregistered_cli_aims_at_dock(
        ["npx", "chrome-remote-interface", "--port", "18888", "inspect"],
        {}, tmp_path, 9333,
    ) is True
    assert _unregistered_cli_aims_at_dock(
        ["npx", "lighthouse", "--port", "9222", "https://example.com"],
        {}, tmp_path, 9333,
    ) is False
    assert _remembered_dock_attach_port() == 9333


def test_leftover_daemon_fill_and_cri_devtools_grandchild_persist_does_not_hide_sibling_chrome(
    monkeypatch, tmp_path,
):
    """Finding 205: leftover daemon + leftover fill + leftover CRI leftover DevTools hid sibling chrome.

    Leftover daemon, leftover fill, and leftover fill's leftover CRI
    all hold stale ``DevToolsActivePort``. leftover fill's leftover
    parent leftover daemon is leftover file holder, so leftover-
    outside is empty and leftover-inherited never ran. Leftover
    python leftover CRI's leftover child inherited leftover persist
    with chrome. Identify leftover on leftover DevTools and leftover
    persist; attach chrome. 9222 and the other family stay
    unknown. Attach must not stamp persist.
    """
    import tools.bot_desktop.browser as bdb
    from hermes_constants import hermes_home_key
    from tools.browser_tool_session import (
        _cdp_url_is_bot_desktop_browser,
        _last_dock_cdp_port,
        _leftover_cdp_aims_at_dock,
        _leftover_host_port_aims_at_dock,
        _remembered_dock_attach_port,
        _reset_dock_port_memory_for_tests,
        _singleton_lock_pid,
        _unregistered_cli_aims_at_dock,
    )

    leftover_fill = 4242
    leftover_cri = 4244
    leftover_py = 4246

    monkeypatch.setattr(runtime, "state_dir", lambda: tmp_path)
    monkeypatch.setattr(bdb, "profile_dir", lambda: tmp_path)
    monkeypatch.setattr(bdb, "_lock_pid", lambda d: None)
    monkeypatch.setattr(
        bdb,
        "_chromium_cmdline_tokens",
        lambda pid: (
            ["chrome", "--type=zygote", f"--user-data-dir={tmp_path}"]
            if pid == 4241 else
            ["chrome", "--type=utility", f"--user-data-dir={tmp_path}"]
            if pid == 4245 else
            ["agent-browser", "daemon", f"--user-data-dir={tmp_path}"]
            if pid == 4300 else
            ["agent-browser", "fill", f"--user-data-dir={tmp_path}"]
            if pid == leftover_fill else
            ["agent-browser", "cri", f"--user-data-dir={tmp_path}"]
            if pid == leftover_cri else
            ["agent-browser", "python", f"--user-data-dir={tmp_path}"]
            if pid == leftover_py else
            ["chrome", f"--user-data-dir={tmp_path}"]
        ),
    )
    monkeypatch.setattr(
        bdb,
        "_proc_ppid",
        lambda pid: {
            4240: 4300, 4241: 4240, 4245: 4241,
            leftover_fill: 4300, leftover_cri: leftover_fill, leftover_py: leftover_cri,
        }.get(pid),
    )
    monkeypatch.setattr(
        bdb,
        "_launched_by_session",
        lambda pid: "h_review" if pid == 4240 else None,
    )
    monkeypatch.setattr(bdb, "_recover_cdp_port_from_singleton", lambda *a, **k: None)
    monkeypatch.setattr(
        bdb, "_this_jar_children",
        lambda parent, user_data_dir: (
            {4240, leftover_fill} if parent == 4300 else (
                {leftover_cri} if parent == leftover_fill else (
                    {leftover_py} if parent == leftover_cri else set()
                )
            )
        ),
    )

    def _inodes(port):
        if port == 40141:
            return {7: {"::1"}}
        if port == 9333:
            return {8: {"::1"}}
        if port == 18888:
            return {9: {"::1"}}
        return {}

    def _holders(want):
        out = {}
        if 7 in want:
            out[4300] = {7}
            out[leftover_fill] = {7}
            out[leftover_cri] = {7}
        if 8 in want:
            out[4240] = {8}
            out[4241] = {8}
            out[4245] = {8}
        if 9 in want:
            out[leftover_py] = {9}
            out[4240] = out.get(4240, set()) | {9}
        return out

    monkeypatch.setattr(bdb, "_loopback_listen_inodes_for_port", _inodes)
    monkeypatch.setattr(bdb, "_pids_holding_socket_inodes", _holders)
    monkeypatch.setattr(
        bdb, "_loopback_listen_ports_for_pid",
        lambda pid: {40141} if pid in {4300, leftover_fill, leftover_cri} else (
            {18888} if pid == leftover_py else (
                {9333, 18888} if pid == 4240 else (
                    {9333} if pid in {4241, 4245} else set()
                )
            )
        ),
    )
    monkeypatch.setattr(
        bdb, "_loopback_listen_targets_for_pid",
        lambda pid: {("::1", 40141)} if pid in {4300, leftover_fill, leftover_cri} else (
            {("::1", 18888)} if pid == leftover_py else (
                {("::1", 9333), ("::1", 18888)} if pid == 4240 else (
                    {("::1", 9333)} if pid in {4241, 4245} else set()
                )
            )
        ),
    )
    monkeypatch.setattr(
        bdb,
        "_cdp_port_reachable",
        lambda port, hosts: port in (40141, 18888) and "::1" in hosts,
    )
    monkeypatch.setattr(bdb, "_configured_cdp_override_url", lambda: "")
    (tmp_path / "DevToolsActivePort").write_text(
        "40141\n/devtools/browser/abc\n", encoding="utf-8",
    )
    _reset_dock_port_memory_for_tests()
    (tmp_path / "dock-cdp-port").write_text("18888\n", encoding="utf-8")
    assert bdb._unique_this_jar_parent(
        {4300, leftover_fill, leftover_cri}, str(tmp_path),
    ) == 4300
    assert bdb.lock_listed_persist_port() == 9333
    assert bdb._this_jar_chromium_pid(str(tmp_path)) == 4240
    assert bdb.shared_chromium_owner_session() == "h_review"
    assert _singleton_lock_pid(str(tmp_path)) == 4240
    assert bdb.running_instance_cdp_port(str(tmp_path)) is None
    assert _remembered_dock_attach_port() == 9333
    assert (tmp_path / "dock-cdp-port").read_text(encoding="utf-8").strip() == "18888"
    assert _last_dock_cdp_port.get(hermes_home_key()) is None
    assert _cdp_url_is_bot_desktop_browser("http://[::1]:40141") is True
    assert _cdp_url_is_bot_desktop_browser("http://[::1]:18888") is True
    assert _cdp_url_is_bot_desktop_browser("http://[::1]:9333") is True
    assert _cdp_url_is_bot_desktop_browser("9222") is False
    assert _leftover_cdp_aims_at_dock("http://[::1]:40141", 9333) is True
    assert _leftover_cdp_aims_at_dock("http://[::1]:18888", 9333) is True
    assert _leftover_host_port_aims_at_dock("::1", 40141, 9333) is True
    assert _leftover_host_port_aims_at_dock("::1", 18888, 9333) is True
    assert _leftover_host_port_aims_at_dock("127.0.0.1", 40141, 9333) is False
    assert _unregistered_cli_aims_at_dock(
        ["npx", "lighthouse", "https://example.com", "--port", "40141"],
        {}, tmp_path, 9333,
    ) is True
    assert _unregistered_cli_aims_at_dock(
        ["npx", "chrome-remote-interface", "--port", "18888", "inspect"],
        {}, tmp_path, 9333,
    ) is True
    assert _unregistered_cli_aims_at_dock(
        ["npx", "lighthouse", "--port", "9222", "https://example.com"],
        {}, tmp_path, 9333,
    ) is False
    assert _remembered_dock_attach_port() == 9333


def test_leftover_supervisor_parent_devtools_grandchild_persist_does_not_hide_sibling_chrome(
    monkeypatch, tmp_path,
):
    """Finding 206: leftover daemon leftover parent leftover supervisor hid sibling chrome.

    leftover daemon leftover parent leftover supervisor names this
    jar. leftover daemon, leftover fill, and leftover CRI leftover
    DevTools. leftover-outside leftover supervisor. leftover-
    inherited leftover supervisor leftover children leftover daemon
    missed leftover daemon leftover children (chrome). Identify
    leftover on leftover DevTools and leftover persist; attach
    chrome. 9222 and the other family stay unknown. Attach must
    not stamp persist.
    """
    import tools.bot_desktop.browser as bdb
    from hermes_constants import hermes_home_key
    from tools.browser_tool_session import (
        _cdp_url_is_bot_desktop_browser,
        _last_dock_cdp_port,
        _leftover_cdp_aims_at_dock,
        _leftover_host_port_aims_at_dock,
        _remembered_dock_attach_port,
        _reset_dock_port_memory_for_tests,
        _singleton_lock_pid,
        _unregistered_cli_aims_at_dock,
    )

    leftover_fill = 4242
    leftover_cri = 4244
    leftover_py = 4246
    leftover_sup = 4290

    monkeypatch.setattr(runtime, "state_dir", lambda: tmp_path)
    monkeypatch.setattr(bdb, "profile_dir", lambda: tmp_path)
    monkeypatch.setattr(bdb, "_lock_pid", lambda d: None)
    monkeypatch.setattr(
        bdb,
        "_chromium_cmdline_tokens",
        lambda pid: (
            ["chrome", "--type=zygote", f"--user-data-dir={tmp_path}"]
            if pid == 4241 else
            ["chrome", "--type=utility", f"--user-data-dir={tmp_path}"]
            if pid == 4245 else
            ["agent-browser", "supervisor", f"--user-data-dir={tmp_path}"]
            if pid == leftover_sup else
            ["agent-browser", "daemon", f"--user-data-dir={tmp_path}"]
            if pid == 4300 else
            ["agent-browser", "fill", f"--user-data-dir={tmp_path}"]
            if pid == leftover_fill else
            ["agent-browser", "cri", f"--user-data-dir={tmp_path}"]
            if pid == leftover_cri else
            ["agent-browser", "python", f"--user-data-dir={tmp_path}"]
            if pid == leftover_py else
            ["chrome", f"--user-data-dir={tmp_path}"]
        ),
    )
    monkeypatch.setattr(
        bdb,
        "_proc_ppid",
        lambda pid: {
            4240: 4300, 4241: 4240, 4245: 4241, 4300: leftover_sup,
            leftover_fill: 4300, leftover_cri: leftover_fill, leftover_py: leftover_cri,
        }.get(pid),
    )
    monkeypatch.setattr(
        bdb,
        "_launched_by_session",
        lambda pid: "h_review" if pid == 4240 else None,
    )
    monkeypatch.setattr(bdb, "_recover_cdp_port_from_singleton", lambda *a, **k: None)
    monkeypatch.setattr(
        bdb, "_this_jar_children",
        lambda parent, user_data_dir: (
            {4300} if parent == leftover_sup else (
                {4240, leftover_fill} if parent == 4300 else (
                    {leftover_cri} if parent == leftover_fill else (
                        {leftover_py} if parent == leftover_cri else set()
                    )
                )
            )
        ),
    )

    def _inodes(port):
        if port == 40141:
            return {7: {"::1"}}
        if port == 9333:
            return {8: {"::1"}}
        if port == 18888:
            return {9: {"::1"}}
        return {}

    def _holders(want):
        out = {}
        if 7 in want:
            out[4300] = {7}
            out[leftover_fill] = {7}
            out[leftover_cri] = {7}
        if 8 in want:
            out[4240] = {8}
            out[4241] = {8}
            out[4245] = {8}
        if 9 in want:
            out[leftover_py] = {9}
            out[4240] = out.get(4240, set()) | {9}
        return out

    monkeypatch.setattr(bdb, "_loopback_listen_inodes_for_port", _inodes)
    monkeypatch.setattr(bdb, "_pids_holding_socket_inodes", _holders)
    monkeypatch.setattr(
        bdb, "_loopback_listen_ports_for_pid",
        lambda pid: {40141} if pid in {4300, leftover_fill, leftover_cri} else (
            {18888} if pid == leftover_py else (
                {9333, 18888} if pid == 4240 else (
                    {9333} if pid in {4241, 4245} else set()
                )
            )
        ),
    )
    monkeypatch.setattr(
        bdb, "_loopback_listen_targets_for_pid",
        lambda pid: {("::1", 40141)} if pid in {4300, leftover_fill, leftover_cri} else (
            {("::1", 18888)} if pid == leftover_py else (
                {("::1", 9333), ("::1", 18888)} if pid == 4240 else (
                    {("::1", 9333)} if pid in {4241, 4245} else set()
                )
            )
        ),
    )
    monkeypatch.setattr(
        bdb,
        "_cdp_port_reachable",
        lambda port, hosts: port in (40141, 18888) and "::1" in hosts,
    )
    monkeypatch.setattr(bdb, "_configured_cdp_override_url", lambda: "")
    (tmp_path / "DevToolsActivePort").write_text(
        "40141\n/devtools/browser/abc\n", encoding="utf-8",
    )
    _reset_dock_port_memory_for_tests()
    (tmp_path / "dock-cdp-port").write_text("18888\n", encoding="utf-8")
    assert bdb._unique_this_jar_parent(
        {4300, leftover_fill, leftover_cri}, str(tmp_path),
    ) == leftover_sup
    assert bdb.lock_listed_persist_port() == 9333
    assert bdb._this_jar_chromium_pid(str(tmp_path)) == 4240
    assert bdb.shared_chromium_owner_session() == "h_review"
    assert _singleton_lock_pid(str(tmp_path)) == 4240
    assert bdb.running_instance_cdp_port(str(tmp_path)) is None
    assert _remembered_dock_attach_port() == 9333
    assert (tmp_path / "dock-cdp-port").read_text(encoding="utf-8").strip() == "18888"
    assert _last_dock_cdp_port.get(hermes_home_key()) is None
    assert _cdp_url_is_bot_desktop_browser("http://[::1]:40141") is True
    assert _cdp_url_is_bot_desktop_browser("http://[::1]:18888") is True
    assert _cdp_url_is_bot_desktop_browser("http://[::1]:9333") is True
    assert _cdp_url_is_bot_desktop_browser("9222") is False
    assert _leftover_cdp_aims_at_dock("http://[::1]:40141", 9333) is True
    assert _leftover_cdp_aims_at_dock("http://[::1]:18888", 9333) is True
    assert _leftover_host_port_aims_at_dock("::1", 40141, 9333) is True
    assert _leftover_host_port_aims_at_dock("::1", 18888, 9333) is True
    assert _leftover_host_port_aims_at_dock("127.0.0.1", 40141, 9333) is False
    assert _unregistered_cli_aims_at_dock(
        ["npx", "lighthouse", "https://example.com", "--port", "40141"],
        {}, tmp_path, 9333,
    ) is True
    assert _unregistered_cli_aims_at_dock(
        ["npx", "chrome-remote-interface", "--port", "18888", "inspect"],
        {}, tmp_path, 9333,
    ) is True
    assert _unregistered_cli_aims_at_dock(
        ["npx", "lighthouse", "--port", "9222", "https://example.com"],
        {}, tmp_path, 9333,
    ) is False
    assert _remembered_dock_attach_port() == 9333


def test_leftover_supervisor_root_devtools_great_grandchild_persist_does_not_hide_sibling_chrome(
    monkeypatch, tmp_path,
):
    """Finding 207: leftover daemon leftover parent leftover supervisor hid sibling chrome.

    leftover daemon leftover parent leftover supervisor names this
    jar. leftover daemon, leftover fill, and leftover CRI leftover
    DevTools. leftover-outside leftover supervisor. leftover-
    inherited leftover supervisor leftover children leftover daemon
    missed leftover daemon leftover children (chrome). Identify
    leftover on leftover DevTools and leftover persist; attach
    chrome. 9222 and the other family stay unknown. Attach must
    not stamp persist.
    """
    import tools.bot_desktop.browser as bdb
    from hermes_constants import hermes_home_key
    from tools.browser_tool_session import (
        _cdp_url_is_bot_desktop_browser,
        _last_dock_cdp_port,
        _leftover_cdp_aims_at_dock,
        _leftover_host_port_aims_at_dock,
        _remembered_dock_attach_port,
        _reset_dock_port_memory_for_tests,
        _singleton_lock_pid,
        _unregistered_cli_aims_at_dock,
    )

    leftover_fill = 4242
    leftover_cri = 4244
    leftover_py = 4246
    leftover_sup = 4290
    leftover_root = 4280

    monkeypatch.setattr(runtime, "state_dir", lambda: tmp_path)
    monkeypatch.setattr(bdb, "profile_dir", lambda: tmp_path)
    monkeypatch.setattr(bdb, "_lock_pid", lambda d: None)
    monkeypatch.setattr(
        bdb,
        "_chromium_cmdline_tokens",
        lambda pid: (
            ["chrome", "--type=zygote", f"--user-data-dir={tmp_path}"]
            if pid == 4241 else
            ["chrome", "--type=utility", f"--user-data-dir={tmp_path}"]
            if pid == 4245 else
            ["agent-browser", "supervisor", f"--user-data-dir={tmp_path}"]
            if pid == leftover_root else
            ["agent-browser", "supervisor", f"--user-data-dir={tmp_path}"]
            if pid == leftover_sup else
            ["agent-browser", "daemon", f"--user-data-dir={tmp_path}"]
            if pid == 4300 else
            ["agent-browser", "fill", f"--user-data-dir={tmp_path}"]
            if pid == leftover_fill else
            ["agent-browser", "cri", f"--user-data-dir={tmp_path}"]
            if pid == leftover_cri else
            ["agent-browser", "python", f"--user-data-dir={tmp_path}"]
            if pid == leftover_py else
            ["chrome", f"--user-data-dir={tmp_path}"]
        ),
    )
    monkeypatch.setattr(
        bdb,
        "_proc_ppid",
        lambda pid: {
            4240: 4300, 4241: 4240, 4245: 4241, leftover_sup: leftover_root, 4300: leftover_sup,
            leftover_fill: 4300, leftover_cri: leftover_fill, leftover_py: leftover_cri,
        }.get(pid),
    )
    monkeypatch.setattr(
        bdb,
        "_launched_by_session",
        lambda pid: "h_review" if pid == 4240 else None,
    )
    monkeypatch.setattr(bdb, "_recover_cdp_port_from_singleton", lambda *a, **k: None)
    monkeypatch.setattr(
        bdb, "_this_jar_children",
        lambda parent, user_data_dir: (
            {leftover_sup} if parent == leftover_root else (
                {4300} if parent == leftover_sup else (
                    {4240, leftover_fill} if parent == 4300 else (
                        {leftover_cri} if parent == leftover_fill else (
                            {leftover_py} if parent == leftover_cri else set()
                        )
                    )
                )
            )
        ),
    )

    def _inodes(port):
        if port == 40141:
            return {7: {"::1"}}
        if port == 9333:
            return {8: {"::1"}}
        if port == 18888:
            return {9: {"::1"}}
        return {}

    def _holders(want):
        out = {}
        if 7 in want:
            out[leftover_sup] = {7}
            out[4300] = {7}
            out[leftover_fill] = {7}
            out[leftover_cri] = {7}
        if 8 in want:
            out[4240] = {8}
            out[4241] = {8}
            out[4245] = {8}
        if 9 in want:
            out[leftover_py] = {9}
            out[4240] = out.get(4240, set()) | {9}
        return out

    monkeypatch.setattr(bdb, "_loopback_listen_inodes_for_port", _inodes)
    monkeypatch.setattr(bdb, "_pids_holding_socket_inodes", _holders)
    monkeypatch.setattr(
        bdb, "_loopback_listen_ports_for_pid",
        lambda pid: {40141} if pid in {leftover_sup, 4300, leftover_fill, leftover_cri} else (
            {18888} if pid == leftover_py else (
                {9333, 18888} if pid == 4240 else (
                    {9333} if pid in {4241, 4245} else set()
                )
            )
        ),
    )
    monkeypatch.setattr(
        bdb, "_loopback_listen_targets_for_pid",
        lambda pid: {("::1", 40141)} if pid in {leftover_sup, 4300, leftover_fill, leftover_cri} else (
            {("::1", 18888)} if pid == leftover_py else (
                {("::1", 9333), ("::1", 18888)} if pid == 4240 else (
                    {("::1", 9333)} if pid in {4241, 4245} else set()
                )
            )
        ),
    )
    monkeypatch.setattr(
        bdb,
        "_cdp_port_reachable",
        lambda port, hosts: port in (40141, 18888) and "::1" in hosts,
    )
    monkeypatch.setattr(bdb, "_configured_cdp_override_url", lambda: "")
    (tmp_path / "DevToolsActivePort").write_text(
        "40141\n/devtools/browser/abc\n", encoding="utf-8",
    )
    _reset_dock_port_memory_for_tests()
    (tmp_path / "dock-cdp-port").write_text("18888\n", encoding="utf-8")
    assert bdb._unique_this_jar_parent(
        {leftover_sup, 4300, leftover_fill, leftover_cri}, str(tmp_path),
    ) == leftover_root
    assert bdb.lock_listed_persist_port() == 9333
    assert bdb._this_jar_chromium_pid(str(tmp_path)) == 4240
    assert bdb.shared_chromium_owner_session() == "h_review"
    assert _singleton_lock_pid(str(tmp_path)) == 4240
    assert bdb.running_instance_cdp_port(str(tmp_path)) is None
    assert _remembered_dock_attach_port() == 9333
    assert (tmp_path / "dock-cdp-port").read_text(encoding="utf-8").strip() == "18888"
    assert _last_dock_cdp_port.get(hermes_home_key()) is None
    assert _cdp_url_is_bot_desktop_browser("http://[::1]:40141") is True
    assert _cdp_url_is_bot_desktop_browser("http://[::1]:18888") is True
    assert _cdp_url_is_bot_desktop_browser("http://[::1]:9333") is True
    assert _cdp_url_is_bot_desktop_browser("9222") is False
    assert _leftover_cdp_aims_at_dock("http://[::1]:40141", 9333) is True
    assert _leftover_cdp_aims_at_dock("http://[::1]:18888", 9333) is True
    assert _leftover_host_port_aims_at_dock("::1", 40141, 9333) is True
    assert _leftover_host_port_aims_at_dock("::1", 18888, 9333) is True
    assert _leftover_host_port_aims_at_dock("127.0.0.1", 40141, 9333) is False
    assert _unregistered_cli_aims_at_dock(
        ["npx", "lighthouse", "https://example.com", "--port", "40141"],
        {}, tmp_path, 9333,
    ) is True
    assert _unregistered_cli_aims_at_dock(
        ["npx", "chrome-remote-interface", "--port", "18888", "inspect"],
        {}, tmp_path, 9333,
    ) is True
    assert _unregistered_cli_aims_at_dock(
        ["npx", "lighthouse", "--port", "9222", "https://example.com"],
        {}, tmp_path, 9333,
    ) is False
    assert _remembered_dock_attach_port() == 9333



def test_leftover_supervisor_parent_root_devtools_great_great_grandchild_persist_does_not_hide_sibling_chrome(
    monkeypatch, tmp_path,
):
    """Finding 208: leftover supervisor leftover parent leftover supervisor hid sibling chrome.

    leftover supervisor leftover parent leftover supervisor leftover
    parent leftover supervisor names this jar. leftover daemon,
    leftover fill, leftover CRI, leftover supervisor, and leftover
    supervisor leftover parent leftover supervisor leftover DevTools.
    leftover-outside leftover supervisor leftover parent leftover
    parent. leftover unique leftover parent leftover children leftover
    children leftover children is leftover supervisor leftover children
    leftover supervisor leftover children leftover daemon, missed
    leftover daemon leftover children (chrome). Identify leftover on
    leftover DevTools and leftover persist; attach chrome. 9222 and
    the other family stay unknown. Attach must not stamp persist.
    """
    import tools.bot_desktop.browser as bdb
    from hermes_constants import hermes_home_key
    from tools.browser_tool_session import (
        _cdp_url_is_bot_desktop_browser,
        _last_dock_cdp_port,
        _leftover_cdp_aims_at_dock,
        _leftover_host_port_aims_at_dock,
        _remembered_dock_attach_port,
        _reset_dock_port_memory_for_tests,
        _singleton_lock_pid,
        _unregistered_cli_aims_at_dock,
    )

    leftover_fill = 4242
    leftover_cri = 4244
    leftover_py = 4246
    leftover_sup = 4290
    leftover_root = 4280
    leftover_parent = 4270

    monkeypatch.setattr(runtime, "state_dir", lambda: tmp_path)
    monkeypatch.setattr(bdb, "profile_dir", lambda: tmp_path)
    monkeypatch.setattr(bdb, "_lock_pid", lambda d: None)
    monkeypatch.setattr(
        bdb,
        "_chromium_cmdline_tokens",
        lambda pid: (
            ["chrome", "--type=zygote", f"--user-data-dir={tmp_path}"]
            if pid == 4241 else
            ["chrome", "--type=utility", f"--user-data-dir={tmp_path}"]
            if pid == 4245 else
            ["agent-browser", "supervisor", f"--user-data-dir={tmp_path}"]
            if pid == leftover_parent else
            ["agent-browser", "supervisor", f"--user-data-dir={tmp_path}"]
            if pid == leftover_root else
            ["agent-browser", "supervisor", f"--user-data-dir={tmp_path}"]
            if pid == leftover_sup else
            ["agent-browser", "daemon", f"--user-data-dir={tmp_path}"]
            if pid == 4300 else
            ["agent-browser", "fill", f"--user-data-dir={tmp_path}"]
            if pid == leftover_fill else
            ["agent-browser", "cri", f"--user-data-dir={tmp_path}"]
            if pid == leftover_cri else
            ["agent-browser", "python", f"--user-data-dir={tmp_path}"]
            if pid == leftover_py else
            ["chrome", f"--user-data-dir={tmp_path}"]
        ),
    )
    monkeypatch.setattr(
        bdb,
        "_proc_ppid",
        lambda pid: {
            4240: 4300, 4241: 4240, 4245: 4241,
            leftover_root: leftover_parent, leftover_sup: leftover_root, 4300: leftover_sup,
            leftover_fill: 4300, leftover_cri: leftover_fill, leftover_py: leftover_cri,
        }.get(pid),
    )
    monkeypatch.setattr(
        bdb,
        "_launched_by_session",
        lambda pid: "h_review" if pid == 4240 else None,
    )
    monkeypatch.setattr(bdb, "_recover_cdp_port_from_singleton", lambda *a, **k: None)
    monkeypatch.setattr(
        bdb, "_this_jar_children",
        lambda parent, user_data_dir: (
            {leftover_root} if parent == leftover_parent else (
                {leftover_sup} if parent == leftover_root else (
                    {4300} if parent == leftover_sup else (
                        {4240, leftover_fill} if parent == 4300 else (
                            {leftover_cri} if parent == leftover_fill else (
                                {leftover_py} if parent == leftover_cri else set()
                            )
                        )
                    )
                )
            )
        ),
    )

    def _inodes(port):
        if port == 40141:
            return {7: {"::1"}}
        if port == 9333:
            return {8: {"::1"}}
        if port == 18888:
            return {9: {"::1"}}
        return {}

    def _holders(want):
        out = {}
        if 7 in want:
            out[leftover_root] = {7}
            out[leftover_sup] = {7}
            out[4300] = {7}
            out[leftover_fill] = {7}
            out[leftover_cri] = {7}
        if 8 in want:
            out[4240] = {8}
            out[4241] = {8}
            out[4245] = {8}
        if 9 in want:
            out[leftover_py] = {9}
            out[4240] = out.get(4240, set()) | {9}
        return out

    monkeypatch.setattr(bdb, "_loopback_listen_inodes_for_port", _inodes)
    monkeypatch.setattr(bdb, "_pids_holding_socket_inodes", _holders)
    monkeypatch.setattr(
        bdb, "_loopback_listen_ports_for_pid",
        lambda pid: {40141} if pid in {leftover_root, leftover_sup, 4300, leftover_fill, leftover_cri} else (
            {18888} if pid == leftover_py else (
                {9333, 18888} if pid == 4240 else (
                    {9333} if pid in {4241, 4245} else set()
                )
            )
        ),
    )
    monkeypatch.setattr(
        bdb, "_loopback_listen_targets_for_pid",
        lambda pid: {("::1", 40141)} if pid in {leftover_root, leftover_sup, 4300, leftover_fill, leftover_cri} else (
            {("::1", 18888)} if pid == leftover_py else (
                {("::1", 9333), ("::1", 18888)} if pid == 4240 else (
                    {("::1", 9333)} if pid in {4241, 4245} else set()
                )
            )
        ),
    )
    monkeypatch.setattr(
        bdb,
        "_cdp_port_reachable",
        lambda port, hosts: port in (40141, 18888) and "::1" in hosts,
    )
    monkeypatch.setattr(bdb, "_configured_cdp_override_url", lambda: "")
    (tmp_path / "DevToolsActivePort").write_text(
        "40141\n/devtools/browser/abc\n", encoding="utf-8",
    )
    _reset_dock_port_memory_for_tests()
    (tmp_path / "dock-cdp-port").write_text("18888\n", encoding="utf-8")
    assert bdb._unique_this_jar_parent(
        {leftover_root, leftover_sup, 4300, leftover_fill, leftover_cri}, str(tmp_path),
    ) == leftover_parent
    assert bdb.lock_listed_persist_port() == 9333
    assert bdb._this_jar_chromium_pid(str(tmp_path)) == 4240
    assert bdb.shared_chromium_owner_session() == "h_review"
    assert _singleton_lock_pid(str(tmp_path)) == 4240
    assert bdb.running_instance_cdp_port(str(tmp_path)) is None
    assert _remembered_dock_attach_port() == 9333
    assert (tmp_path / "dock-cdp-port").read_text(encoding="utf-8").strip() == "18888"
    assert _last_dock_cdp_port.get(hermes_home_key()) is None
    assert _cdp_url_is_bot_desktop_browser("http://[::1]:40141") is True
    assert _cdp_url_is_bot_desktop_browser("http://[::1]:18888") is True
    assert _cdp_url_is_bot_desktop_browser("http://[::1]:9333") is True
    assert _cdp_url_is_bot_desktop_browser("9222") is False
    assert _leftover_cdp_aims_at_dock("http://[::1]:40141", 9333) is True
    assert _leftover_cdp_aims_at_dock("http://[::1]:18888", 9333) is True
    assert _leftover_host_port_aims_at_dock("::1", 40141, 9333) is True
    assert _leftover_host_port_aims_at_dock("::1", 18888, 9333) is True
    assert _leftover_host_port_aims_at_dock("127.0.0.1", 40141, 9333) is False
    assert _unregistered_cli_aims_at_dock(
        ["npx", "lighthouse", "https://example.com", "--port", "40141"],
        {}, tmp_path, 9333,
    ) is True
    assert _unregistered_cli_aims_at_dock(
        ["npx", "chrome-remote-interface", "--port", "18888", "inspect"],
        {}, tmp_path, 9333,
    ) is True
    assert _unregistered_cli_aims_at_dock(
        ["npx", "lighthouse", "--port", "9222", "https://example.com"],
        {}, tmp_path, 9333,
    ) is False
    assert _remembered_dock_attach_port() == 9333


def test_leftover_supervisor_parent_grand_devtools_great_great_great_grandchild_persist_does_not_hide_sibling_chrome(
    monkeypatch, tmp_path,
):
    """Finding 209: leftover supervisor leftover parent leftover parent leftover supervisor hid sibling chrome.

    leftover supervisor leftover parent leftover supervisor leftover
    parent leftover supervisor names this jar. leftover daemon,
    leftover fill, leftover CRI, leftover supervisor, and leftover
    supervisor leftover parent leftover supervisor leftover DevTools.
    leftover-outside leftover supervisor leftover parent leftover
    parent. leftover unique leftover parent leftover children leftover
    children leftover children is leftover supervisor leftover children
    leftover supervisor leftover children leftover daemon, missed
    leftover daemon leftover children (chrome). Identify leftover on
    leftover DevTools and leftover persist; attach chrome. 9222 and
    the other family stay unknown. Attach must not stamp persist.
    """
    import tools.bot_desktop.browser as bdb
    from hermes_constants import hermes_home_key
    from tools.browser_tool_session import (
        _cdp_url_is_bot_desktop_browser,
        _last_dock_cdp_port,
        _leftover_cdp_aims_at_dock,
        _leftover_host_port_aims_at_dock,
        _remembered_dock_attach_port,
        _reset_dock_port_memory_for_tests,
        _singleton_lock_pid,
        _unregistered_cli_aims_at_dock,
    )

    leftover_fill = 4242
    leftover_cri = 4244
    leftover_py = 4246
    leftover_sup = 4290
    leftover_root = 4280
    leftover_parent = 4270
    leftover_grand = 4260

    monkeypatch.setattr(runtime, "state_dir", lambda: tmp_path)
    monkeypatch.setattr(bdb, "profile_dir", lambda: tmp_path)
    monkeypatch.setattr(bdb, "_lock_pid", lambda d: None)
    monkeypatch.setattr(
        bdb,
        "_chromium_cmdline_tokens",
        lambda pid: (
            ["chrome", "--type=zygote", f"--user-data-dir={tmp_path}"]
            if pid == 4241 else
            ["chrome", "--type=utility", f"--user-data-dir={tmp_path}"]
            if pid == 4245 else
            ["agent-browser", "supervisor", f"--user-data-dir={tmp_path}"]
            if pid == leftover_grand else
            ["agent-browser", "supervisor", f"--user-data-dir={tmp_path}"]
            if pid == leftover_parent else
            ["agent-browser", "supervisor", f"--user-data-dir={tmp_path}"]
            if pid == leftover_root else
            ["agent-browser", "supervisor", f"--user-data-dir={tmp_path}"]
            if pid == leftover_sup else
            ["agent-browser", "daemon", f"--user-data-dir={tmp_path}"]
            if pid == 4300 else
            ["agent-browser", "fill", f"--user-data-dir={tmp_path}"]
            if pid == leftover_fill else
            ["agent-browser", "cri", f"--user-data-dir={tmp_path}"]
            if pid == leftover_cri else
            ["agent-browser", "python", f"--user-data-dir={tmp_path}"]
            if pid == leftover_py else
            ["chrome", f"--user-data-dir={tmp_path}"]
        ),
    )
    monkeypatch.setattr(
        bdb,
        "_proc_ppid",
        lambda pid: {
            4240: 4300, 4241: 4240, 4245: 4241,
            leftover_parent: leftover_grand, leftover_root: leftover_parent, leftover_sup: leftover_root, 4300: leftover_sup,
            leftover_fill: 4300, leftover_cri: leftover_fill, leftover_py: leftover_cri,
        }.get(pid),
    )
    monkeypatch.setattr(
        bdb,
        "_launched_by_session",
        lambda pid: "h_review" if pid == 4240 else None,
    )
    monkeypatch.setattr(bdb, "_recover_cdp_port_from_singleton", lambda *a, **k: None)
    monkeypatch.setattr(
        bdb, "_this_jar_children",
        lambda parent, user_data_dir: (
            {leftover_parent} if parent == leftover_grand else (
            {leftover_root} if parent == leftover_parent else (
                {leftover_sup} if parent == leftover_root else (
                    {4300} if parent == leftover_sup else (
                        {4240, leftover_fill} if parent == 4300 else (
                            {leftover_cri} if parent == leftover_fill else (
                                {leftover_py} if parent == leftover_cri else set()
                            )
                        )
                    )
                )
            )
            )
        ),
    )

    def _inodes(port):
        if port == 40141:
            return {7: {"::1"}}
        if port == 9333:
            return {8: {"::1"}}
        if port == 18888:
            return {9: {"::1"}}
        return {}

    def _holders(want):
        out = {}
        if 7 in want:
            out[leftover_parent] = {7}
            out[leftover_root] = {7}
            out[leftover_sup] = {7}
            out[4300] = {7}
            out[leftover_fill] = {7}
            out[leftover_cri] = {7}
        if 8 in want:
            out[4240] = {8}
            out[4241] = {8}
            out[4245] = {8}
        if 9 in want:
            out[leftover_py] = {9}
            out[4240] = out.get(4240, set()) | {9}
        return out

    monkeypatch.setattr(bdb, "_loopback_listen_inodes_for_port", _inodes)
    monkeypatch.setattr(bdb, "_pids_holding_socket_inodes", _holders)
    monkeypatch.setattr(
        bdb, "_loopback_listen_ports_for_pid",
        lambda pid: {40141} if pid in {leftover_parent, leftover_root, leftover_sup, 4300, leftover_fill, leftover_cri} else (
            {18888} if pid == leftover_py else (
                {9333, 18888} if pid == 4240 else (
                    {9333} if pid in {4241, 4245} else set()
                )
            )
        ),
    )
    monkeypatch.setattr(
        bdb, "_loopback_listen_targets_for_pid",
        lambda pid: {("::1", 40141)} if pid in {leftover_parent, leftover_root, leftover_sup, 4300, leftover_fill, leftover_cri} else (
            {("::1", 18888)} if pid == leftover_py else (
                {("::1", 9333), ("::1", 18888)} if pid == 4240 else (
                    {("::1", 9333)} if pid in {4241, 4245} else set()
                )
            )
        ),
    )
    monkeypatch.setattr(
        bdb,
        "_cdp_port_reachable",
        lambda port, hosts: port in (40141, 18888) and "::1" in hosts,
    )
    monkeypatch.setattr(bdb, "_configured_cdp_override_url", lambda: "")
    (tmp_path / "DevToolsActivePort").write_text(
        "40141\n/devtools/browser/abc\n", encoding="utf-8",
    )
    _reset_dock_port_memory_for_tests()
    (tmp_path / "dock-cdp-port").write_text("18888\n", encoding="utf-8")
    assert bdb._unique_this_jar_parent(
        {leftover_parent, leftover_root, leftover_sup, 4300, leftover_fill, leftover_cri}, str(tmp_path),
    ) == leftover_grand
    assert bdb.lock_listed_persist_port() == 9333
    assert bdb._this_jar_chromium_pid(str(tmp_path)) == 4240
    assert bdb.shared_chromium_owner_session() == "h_review"
    assert _singleton_lock_pid(str(tmp_path)) == 4240
    assert bdb.running_instance_cdp_port(str(tmp_path)) is None
    assert _remembered_dock_attach_port() == 9333
    assert (tmp_path / "dock-cdp-port").read_text(encoding="utf-8").strip() == "18888"
    assert _last_dock_cdp_port.get(hermes_home_key()) is None
    assert _cdp_url_is_bot_desktop_browser("http://[::1]:40141") is True
    assert _cdp_url_is_bot_desktop_browser("http://[::1]:18888") is True
    assert _cdp_url_is_bot_desktop_browser("http://[::1]:9333") is True
    assert _cdp_url_is_bot_desktop_browser("9222") is False
    assert _leftover_cdp_aims_at_dock("http://[::1]:40141", 9333) is True
    assert _leftover_cdp_aims_at_dock("http://[::1]:18888", 9333) is True
    assert _leftover_host_port_aims_at_dock("::1", 40141, 9333) is True
    assert _leftover_host_port_aims_at_dock("::1", 18888, 9333) is True
    assert _leftover_host_port_aims_at_dock("127.0.0.1", 40141, 9333) is False
    assert _unregistered_cli_aims_at_dock(
        ["npx", "lighthouse", "https://example.com", "--port", "40141"],
        {}, tmp_path, 9333,
    ) is True
    assert _unregistered_cli_aims_at_dock(
        ["npx", "chrome-remote-interface", "--port", "18888", "inspect"],
        {}, tmp_path, 9333,
    ) is True
    assert _unregistered_cli_aims_at_dock(
        ["npx", "lighthouse", "--port", "9222", "https://example.com"],
        {}, tmp_path, 9333,
    ) is False
    assert _remembered_dock_attach_port() == 9333


def test_leftover_supervisor_parent_great_devtools_great_great_great_great_grandchild_persist_does_not_hide_sibling_chrome(
    monkeypatch, tmp_path,
):
    """Finding 210: leftover supervisor leftover parent leftover parent leftover parent leftover supervisor hid sibling chrome.

    leftover supervisor leftover parent leftover supervisor leftover
    parent leftover supervisor names this jar. leftover daemon,
    leftover fill, leftover CRI, leftover supervisor, and leftover
    supervisor leftover parent leftover supervisor leftover DevTools.
    leftover-outside leftover supervisor leftover parent leftover
    parent. leftover unique leftover parent leftover children leftover
    children leftover children is leftover supervisor leftover children
    leftover supervisor leftover children leftover daemon, missed
    leftover daemon leftover children (chrome). Identify leftover on
    leftover DevTools and leftover persist; attach chrome. 9222 and
    the other family stay unknown. Attach must not stamp persist.
    """
    import tools.bot_desktop.browser as bdb
    from hermes_constants import hermes_home_key
    from tools.browser_tool_session import (
        _cdp_url_is_bot_desktop_browser,
        _last_dock_cdp_port,
        _leftover_cdp_aims_at_dock,
        _leftover_host_port_aims_at_dock,
        _remembered_dock_attach_port,
        _reset_dock_port_memory_for_tests,
        _singleton_lock_pid,
        _unregistered_cli_aims_at_dock,
    )

    leftover_fill = 4242
    leftover_cri = 4244
    leftover_py = 4246
    leftover_sup = 4290
    leftover_root = 4280
    leftover_parent = 4270
    leftover_grand = 4260
    leftover_great = 4250

    monkeypatch.setattr(runtime, "state_dir", lambda: tmp_path)
    monkeypatch.setattr(bdb, "profile_dir", lambda: tmp_path)
    monkeypatch.setattr(bdb, "_lock_pid", lambda d: None)
    monkeypatch.setattr(
        bdb,
        "_chromium_cmdline_tokens",
        lambda pid: (
            ["chrome", "--type=zygote", f"--user-data-dir={tmp_path}"]
            if pid == 4241 else
            ["chrome", "--type=utility", f"--user-data-dir={tmp_path}"]
            if pid == 4245 else
            ["agent-browser", "supervisor", f"--user-data-dir={tmp_path}"]
            if pid == leftover_great else
            ["agent-browser", "supervisor", f"--user-data-dir={tmp_path}"]
            if pid == leftover_grand else
            ["agent-browser", "supervisor", f"--user-data-dir={tmp_path}"]
            if pid == leftover_parent else
            ["agent-browser", "supervisor", f"--user-data-dir={tmp_path}"]
            if pid == leftover_root else
            ["agent-browser", "supervisor", f"--user-data-dir={tmp_path}"]
            if pid == leftover_sup else
            ["agent-browser", "daemon", f"--user-data-dir={tmp_path}"]
            if pid == 4300 else
            ["agent-browser", "fill", f"--user-data-dir={tmp_path}"]
            if pid == leftover_fill else
            ["agent-browser", "cri", f"--user-data-dir={tmp_path}"]
            if pid == leftover_cri else
            ["agent-browser", "python", f"--user-data-dir={tmp_path}"]
            if pid == leftover_py else
            ["chrome", f"--user-data-dir={tmp_path}"]
        ),
    )
    monkeypatch.setattr(
        bdb,
        "_proc_ppid",
        lambda pid: {
            4240: 4300, 4241: 4240, 4245: 4241,
            leftover_grand: leftover_great, leftover_parent: leftover_grand, leftover_root: leftover_parent, leftover_sup: leftover_root, 4300: leftover_sup,
            leftover_fill: 4300, leftover_cri: leftover_fill, leftover_py: leftover_cri,
        }.get(pid),
    )
    monkeypatch.setattr(
        bdb,
        "_launched_by_session",
        lambda pid: "h_review" if pid == 4240 else None,
    )
    monkeypatch.setattr(bdb, "_recover_cdp_port_from_singleton", lambda *a, **k: None)
    monkeypatch.setattr(
        bdb, "_this_jar_children",
        lambda parent, user_data_dir: (
            {leftover_grand} if parent == leftover_great else (
            {leftover_parent} if parent == leftover_grand else (
            {leftover_root} if parent == leftover_parent else (
                {leftover_sup} if parent == leftover_root else (
                    {4300} if parent == leftover_sup else (
                        {4240, leftover_fill} if parent == 4300 else (
                            {leftover_cri} if parent == leftover_fill else (
                                {leftover_py} if parent == leftover_cri else set()
                            )
                        )
                    )
                )
            )
            )
            )
        ),
    )

    def _inodes(port):
        if port == 40141:
            return {7: {"::1"}}
        if port == 9333:
            return {8: {"::1"}}
        if port == 18888:
            return {9: {"::1"}}
        return {}

    def _holders(want):
        out = {}
        if 7 in want:
            out[leftover_grand] = {7}
            out[leftover_parent] = {7}
            out[leftover_root] = {7}
            out[leftover_sup] = {7}
            out[4300] = {7}
            out[leftover_fill] = {7}
            out[leftover_cri] = {7}
        if 8 in want:
            out[4240] = {8}
            out[4241] = {8}
            out[4245] = {8}
        if 9 in want:
            out[leftover_py] = {9}
            out[4240] = out.get(4240, set()) | {9}
        return out

    monkeypatch.setattr(bdb, "_loopback_listen_inodes_for_port", _inodes)
    monkeypatch.setattr(bdb, "_pids_holding_socket_inodes", _holders)
    monkeypatch.setattr(
        bdb, "_loopback_listen_ports_for_pid",
        lambda pid: {40141} if pid in {leftover_grand, leftover_parent, leftover_root, leftover_sup, 4300, leftover_fill, leftover_cri} else (
            {18888} if pid == leftover_py else (
                {9333, 18888} if pid == 4240 else (
                    {9333} if pid in {4241, 4245} else set()
                )
            )
        ),
    )
    monkeypatch.setattr(
        bdb, "_loopback_listen_targets_for_pid",
        lambda pid: {("::1", 40141)} if pid in {leftover_grand, leftover_parent, leftover_root, leftover_sup, 4300, leftover_fill, leftover_cri} else (
            {("::1", 18888)} if pid == leftover_py else (
                {("::1", 9333), ("::1", 18888)} if pid == 4240 else (
                    {("::1", 9333)} if pid in {4241, 4245} else set()
                )
            )
        ),
    )
    monkeypatch.setattr(
        bdb,
        "_cdp_port_reachable",
        lambda port, hosts: port in (40141, 18888) and "::1" in hosts,
    )
    monkeypatch.setattr(bdb, "_configured_cdp_override_url", lambda: "")
    (tmp_path / "DevToolsActivePort").write_text(
        "40141\n/devtools/browser/abc\n", encoding="utf-8",
    )
    _reset_dock_port_memory_for_tests()
    (tmp_path / "dock-cdp-port").write_text("18888\n", encoding="utf-8")
    assert bdb._unique_this_jar_parent(
        {leftover_grand, leftover_parent, leftover_root, leftover_sup, 4300, leftover_fill, leftover_cri}, str(tmp_path),
    ) == leftover_great
    assert bdb.lock_listed_persist_port() == 9333
    assert bdb._this_jar_chromium_pid(str(tmp_path)) == 4240
    assert bdb.shared_chromium_owner_session() == "h_review"
    assert _singleton_lock_pid(str(tmp_path)) == 4240
    assert bdb.running_instance_cdp_port(str(tmp_path)) is None
    assert _remembered_dock_attach_port() == 9333
    assert (tmp_path / "dock-cdp-port").read_text(encoding="utf-8").strip() == "18888"
    assert _last_dock_cdp_port.get(hermes_home_key()) is None
    assert _cdp_url_is_bot_desktop_browser("http://[::1]:40141") is True
    assert _cdp_url_is_bot_desktop_browser("http://[::1]:18888") is True
    assert _cdp_url_is_bot_desktop_browser("http://[::1]:9333") is True
    assert _cdp_url_is_bot_desktop_browser("9222") is False
    assert _leftover_cdp_aims_at_dock("http://[::1]:40141", 9333) is True
    assert _leftover_cdp_aims_at_dock("http://[::1]:18888", 9333) is True
    assert _leftover_host_port_aims_at_dock("::1", 40141, 9333) is True
    assert _leftover_host_port_aims_at_dock("::1", 18888, 9333) is True
    assert _leftover_host_port_aims_at_dock("127.0.0.1", 40141, 9333) is False
    assert _unregistered_cli_aims_at_dock(
        ["npx", "lighthouse", "https://example.com", "--port", "40141"],
        {}, tmp_path, 9333,
    ) is True
    assert _unregistered_cli_aims_at_dock(
        ["npx", "chrome-remote-interface", "--port", "18888", "inspect"],
        {}, tmp_path, 9333,
    ) is True
    assert _unregistered_cli_aims_at_dock(
        ["npx", "lighthouse", "--port", "9222", "https://example.com"],
        {}, tmp_path, 9333,
    ) is False
    assert _remembered_dock_attach_port() == 9333

def test_leftover_supervisor_parent_ggreat_devtools_great_great_great_great_great_grandchild_persist_does_not_hide_sibling_chrome(
    monkeypatch, tmp_path,
):
    """Finding 211: leftover supervisor leftover parent leftover parent leftover parent leftover supervisor hid sibling chrome.

    leftover supervisor leftover parent leftover supervisor leftover
    parent leftover supervisor names this jar. leftover daemon,
    leftover fill, leftover CRI, leftover supervisor, and leftover
    supervisor leftover parent leftover supervisor leftover DevTools.
    leftover-outside leftover supervisor leftover parent leftover
    parent. leftover unique leftover parent leftover children leftover
    children leftover children is leftover supervisor leftover children
    leftover supervisor leftover children leftover daemon, missed
    leftover daemon leftover children (chrome). Identify leftover on
    leftover DevTools and leftover persist; attach chrome. 9222 and
    the other family stay unknown. Attach must not stamp persist.
    """
    import tools.bot_desktop.browser as bdb
    from hermes_constants import hermes_home_key
    from tools.browser_tool_session import (
        _cdp_url_is_bot_desktop_browser,
        _last_dock_cdp_port,
        _leftover_cdp_aims_at_dock,
        _leftover_host_port_aims_at_dock,
        _remembered_dock_attach_port,
        _reset_dock_port_memory_for_tests,
        _singleton_lock_pid,
        _unregistered_cli_aims_at_dock,
    )

    leftover_fill = 4242
    leftover_cri = 4244
    leftover_py = 4246
    leftover_sup = 4290
    leftover_root = 4280
    leftover_parent = 4270
    leftover_grand = 4260
    leftover_ggreat = 4230
    leftover_great = 4250

    monkeypatch.setattr(runtime, "state_dir", lambda: tmp_path)
    monkeypatch.setattr(bdb, "profile_dir", lambda: tmp_path)
    monkeypatch.setattr(bdb, "_lock_pid", lambda d: None)
    monkeypatch.setattr(
        bdb,
        "_chromium_cmdline_tokens",
        lambda pid: (
            ["chrome", "--type=zygote", f"--user-data-dir={tmp_path}"]
            if pid == 4241 else
            ["chrome", "--type=utility", f"--user-data-dir={tmp_path}"]
            if pid == 4245 else
            ["agent-browser", "supervisor", f"--user-data-dir={tmp_path}"]
            if pid == leftover_ggreat else
            ["agent-browser", "supervisor", f"--user-data-dir={tmp_path}"]
            if pid == leftover_great else
            ["agent-browser", "supervisor", f"--user-data-dir={tmp_path}"]
            if pid == leftover_grand else
            ["agent-browser", "supervisor", f"--user-data-dir={tmp_path}"]
            if pid == leftover_parent else
            ["agent-browser", "supervisor", f"--user-data-dir={tmp_path}"]
            if pid == leftover_root else
            ["agent-browser", "supervisor", f"--user-data-dir={tmp_path}"]
            if pid == leftover_sup else
            ["agent-browser", "daemon", f"--user-data-dir={tmp_path}"]
            if pid == 4300 else
            ["agent-browser", "fill", f"--user-data-dir={tmp_path}"]
            if pid == leftover_fill else
            ["agent-browser", "cri", f"--user-data-dir={tmp_path}"]
            if pid == leftover_cri else
            ["agent-browser", "python", f"--user-data-dir={tmp_path}"]
            if pid == leftover_py else
            ["chrome", f"--user-data-dir={tmp_path}"]
        ),
    )
    monkeypatch.setattr(
        bdb,
        "_proc_ppid",
        lambda pid: {
            4240: 4300, 4241: 4240, 4245: 4241,
            leftover_great: leftover_ggreat, leftover_grand: leftover_great, leftover_parent: leftover_grand, leftover_root: leftover_parent, leftover_sup: leftover_root, 4300: leftover_sup,
            leftover_fill: 4300, leftover_cri: leftover_fill, leftover_py: leftover_cri,
        }.get(pid),
    )
    monkeypatch.setattr(
        bdb,
        "_launched_by_session",
        lambda pid: "h_review" if pid == 4240 else None,
    )
    monkeypatch.setattr(bdb, "_recover_cdp_port_from_singleton", lambda *a, **k: None)
    monkeypatch.setattr(
        bdb, "_this_jar_children",
        lambda parent, user_data_dir: (
            {leftover_great} if parent == leftover_ggreat else (
            {leftover_grand} if parent == leftover_great else (
            {leftover_parent} if parent == leftover_grand else (
            {leftover_root} if parent == leftover_parent else (
                {leftover_sup} if parent == leftover_root else (
                    {4300} if parent == leftover_sup else (
                        {4240, leftover_fill} if parent == 4300 else (
                            {leftover_cri} if parent == leftover_fill else (
                                {leftover_py} if parent == leftover_cri else set()
                            )
                        )
                    )
                )
            )
            )
            )
            )
        ),
    )

    def _inodes(port):
        if port == 40141:
            return {7: {"::1"}}
        if port == 9333:
            return {8: {"::1"}}
        if port == 18888:
            return {9: {"::1"}}
        return {}

    def _holders(want):
        out = {}
        if 7 in want:
            out[leftover_great] = {7}
            out[leftover_grand] = {7}
            out[leftover_parent] = {7}
            out[leftover_root] = {7}
            out[leftover_sup] = {7}
            out[4300] = {7}
            out[leftover_fill] = {7}
            out[leftover_cri] = {7}
        if 8 in want:
            out[4240] = {8}
            out[4241] = {8}
            out[4245] = {8}
        if 9 in want:
            out[leftover_py] = {9}
            out[4240] = out.get(4240, set()) | {9}
        return out

    monkeypatch.setattr(bdb, "_loopback_listen_inodes_for_port", _inodes)
    monkeypatch.setattr(bdb, "_pids_holding_socket_inodes", _holders)
    monkeypatch.setattr(
        bdb, "_loopback_listen_ports_for_pid",
        lambda pid: {40141} if pid in {leftover_great, leftover_grand, leftover_parent, leftover_root, leftover_sup, 4300, leftover_fill, leftover_cri} else (
            {18888} if pid == leftover_py else (
                {9333, 18888} if pid == 4240 else (
                    {9333} if pid in {4241, 4245} else set()
                )
            )
        ),
    )
    monkeypatch.setattr(
        bdb, "_loopback_listen_targets_for_pid",
        lambda pid: {("::1", 40141)} if pid in {leftover_great, leftover_grand, leftover_parent, leftover_root, leftover_sup, 4300, leftover_fill, leftover_cri} else (
            {("::1", 18888)} if pid == leftover_py else (
                {("::1", 9333), ("::1", 18888)} if pid == 4240 else (
                    {("::1", 9333)} if pid in {4241, 4245} else set()
                )
            )
        ),
    )
    monkeypatch.setattr(
        bdb,
        "_cdp_port_reachable",
        lambda port, hosts: port in (40141, 18888) and "::1" in hosts,
    )
    monkeypatch.setattr(bdb, "_configured_cdp_override_url", lambda: "")
    (tmp_path / "DevToolsActivePort").write_text(
        "40141\n/devtools/browser/abc\n", encoding="utf-8",
    )
    _reset_dock_port_memory_for_tests()
    (tmp_path / "dock-cdp-port").write_text("18888\n", encoding="utf-8")
    assert bdb._unique_this_jar_parent(
        {leftover_great, leftover_grand, leftover_parent, leftover_root, leftover_sup, 4300, leftover_fill, leftover_cri}, str(tmp_path),
    ) == leftover_ggreat
    assert bdb.lock_listed_persist_port() == 9333
    assert bdb._this_jar_chromium_pid(str(tmp_path)) == 4240
    assert bdb.shared_chromium_owner_session() == "h_review"
    assert _singleton_lock_pid(str(tmp_path)) == 4240
    assert bdb.running_instance_cdp_port(str(tmp_path)) is None
    assert _remembered_dock_attach_port() == 9333
    assert (tmp_path / "dock-cdp-port").read_text(encoding="utf-8").strip() == "18888"
    assert _last_dock_cdp_port.get(hermes_home_key()) is None
    assert _cdp_url_is_bot_desktop_browser("http://[::1]:40141") is True
    assert _cdp_url_is_bot_desktop_browser("http://[::1]:18888") is True
    assert _cdp_url_is_bot_desktop_browser("http://[::1]:9333") is True
    assert _cdp_url_is_bot_desktop_browser("9222") is False
    assert _leftover_cdp_aims_at_dock("http://[::1]:40141", 9333) is True
    assert _leftover_cdp_aims_at_dock("http://[::1]:18888", 9333) is True
    assert _leftover_host_port_aims_at_dock("::1", 40141, 9333) is True
    assert _leftover_host_port_aims_at_dock("::1", 18888, 9333) is True
    assert _leftover_host_port_aims_at_dock("127.0.0.1", 40141, 9333) is False
    assert _unregistered_cli_aims_at_dock(
        ["npx", "lighthouse", "https://example.com", "--port", "40141"],
        {}, tmp_path, 9333,
    ) is True
    assert _unregistered_cli_aims_at_dock(
        ["npx", "chrome-remote-interface", "--port", "18888", "inspect"],
        {}, tmp_path, 9333,
    ) is True
    assert _unregistered_cli_aims_at_dock(
        ["npx", "lighthouse", "--port", "9222", "https://example.com"],
        {}, tmp_path, 9333,
    ) is False
    assert _remembered_dock_attach_port() == 9333



def test_leftover_supervisor_parent_gggreat_devtools_great_great_great_great_great_great_grandchild_persist_does_not_hide_sibling_chrome(
    monkeypatch, tmp_path,
):
    """Finding 212: leftover supervisor leftover parent leftover parent leftover parent leftover supervisor hid sibling chrome.

    leftover supervisor leftover parent leftover supervisor leftover
    parent leftover supervisor names this jar. leftover daemon,
    leftover fill, leftover CRI, leftover supervisor, and leftover
    supervisor leftover parent leftover supervisor leftover DevTools.
    leftover-outside leftover supervisor leftover parent leftover
    parent. leftover unique leftover parent leftover children leftover
    children leftover children is leftover supervisor leftover children
    leftover supervisor leftover children leftover daemon, missed
    leftover daemon leftover children (chrome). Identify leftover on
    leftover DevTools and leftover persist; attach chrome. 9222 and
    the other family stay unknown. Attach must not stamp persist.
    """
    import tools.bot_desktop.browser as bdb
    from hermes_constants import hermes_home_key
    from tools.browser_tool_session import (
        _cdp_url_is_bot_desktop_browser,
        _last_dock_cdp_port,
        _leftover_cdp_aims_at_dock,
        _leftover_host_port_aims_at_dock,
        _remembered_dock_attach_port,
        _reset_dock_port_memory_for_tests,
        _singleton_lock_pid,
        _unregistered_cli_aims_at_dock,
    )

    leftover_fill = 4242
    leftover_cri = 4244
    leftover_py = 4246
    leftover_sup = 4290
    leftover_root = 4280
    leftover_parent = 4270
    leftover_grand = 4260
    leftover_gggreat = 4220
    leftover_ggreat = 4230
    leftover_great = 4250

    monkeypatch.setattr(runtime, "state_dir", lambda: tmp_path)
    monkeypatch.setattr(bdb, "profile_dir", lambda: tmp_path)
    monkeypatch.setattr(bdb, "_lock_pid", lambda d: None)
    monkeypatch.setattr(
        bdb,
        "_chromium_cmdline_tokens",
        lambda pid: (
            ["chrome", "--type=zygote", f"--user-data-dir={tmp_path}"]
            if pid == 4241 else
            ["chrome", "--type=utility", f"--user-data-dir={tmp_path}"]
            if pid == 4245 else
            ["agent-browser", "supervisor", f"--user-data-dir={tmp_path}"]
            if pid == leftover_gggreat else
            ["agent-browser", "supervisor", f"--user-data-dir={tmp_path}"]
            if pid == leftover_ggreat else
            ["agent-browser", "supervisor", f"--user-data-dir={tmp_path}"]
            if pid == leftover_great else
            ["agent-browser", "supervisor", f"--user-data-dir={tmp_path}"]
            if pid == leftover_grand else
            ["agent-browser", "supervisor", f"--user-data-dir={tmp_path}"]
            if pid == leftover_parent else
            ["agent-browser", "supervisor", f"--user-data-dir={tmp_path}"]
            if pid == leftover_root else
            ["agent-browser", "supervisor", f"--user-data-dir={tmp_path}"]
            if pid == leftover_sup else
            ["agent-browser", "daemon", f"--user-data-dir={tmp_path}"]
            if pid == 4300 else
            ["agent-browser", "fill", f"--user-data-dir={tmp_path}"]
            if pid == leftover_fill else
            ["agent-browser", "cri", f"--user-data-dir={tmp_path}"]
            if pid == leftover_cri else
            ["agent-browser", "python", f"--user-data-dir={tmp_path}"]
            if pid == leftover_py else
            ["chrome", f"--user-data-dir={tmp_path}"]
        ),
    )
    monkeypatch.setattr(
        bdb,
        "_proc_ppid",
        lambda pid: {
            4240: 4300, 4241: 4240, 4245: 4241,
            leftover_ggreat: leftover_gggreat, leftover_great: leftover_ggreat, leftover_grand: leftover_great, leftover_parent: leftover_grand, leftover_root: leftover_parent, leftover_sup: leftover_root, 4300: leftover_sup,
            leftover_fill: 4300, leftover_cri: leftover_fill, leftover_py: leftover_cri,
        }.get(pid),
    )
    monkeypatch.setattr(
        bdb,
        "_launched_by_session",
        lambda pid: "h_review" if pid == 4240 else None,
    )
    monkeypatch.setattr(bdb, "_recover_cdp_port_from_singleton", lambda *a, **k: None)
    monkeypatch.setattr(
        bdb, "_this_jar_children",
        lambda parent, user_data_dir: (
            {leftover_ggreat} if parent == leftover_gggreat else (
            {leftover_great} if parent == leftover_ggreat else (
            {leftover_grand} if parent == leftover_great else (
            {leftover_parent} if parent == leftover_grand else (
            {leftover_root} if parent == leftover_parent else (
                {leftover_sup} if parent == leftover_root else (
                    {4300} if parent == leftover_sup else (
                        {4240, leftover_fill} if parent == 4300 else (
                            {leftover_cri} if parent == leftover_fill else (
                                {leftover_py} if parent == leftover_cri else set()
                            )
                        )
                    )
                )
            )
            )
            )
            )
            )
        ),
    )

    def _inodes(port):
        if port == 40141:
            return {7: {"::1"}}
        if port == 9333:
            return {8: {"::1"}}
        if port == 18888:
            return {9: {"::1"}}
        return {}

    def _holders(want):
        out = {}
        if 7 in want:
            out[leftover_ggreat] = {7}
            out[leftover_great] = {7}
            out[leftover_grand] = {7}
            out[leftover_parent] = {7}
            out[leftover_root] = {7}
            out[leftover_sup] = {7}
            out[4300] = {7}
            out[leftover_fill] = {7}
            out[leftover_cri] = {7}
        if 8 in want:
            out[4240] = {8}
            out[4241] = {8}
            out[4245] = {8}
        if 9 in want:
            out[leftover_py] = {9}
            out[4240] = out.get(4240, set()) | {9}
        return out

    monkeypatch.setattr(bdb, "_loopback_listen_inodes_for_port", _inodes)
    monkeypatch.setattr(bdb, "_pids_holding_socket_inodes", _holders)
    monkeypatch.setattr(
        bdb, "_loopback_listen_ports_for_pid",
        lambda pid: {40141} if pid in {leftover_ggreat, leftover_great, leftover_grand, leftover_parent, leftover_root, leftover_sup, 4300, leftover_fill, leftover_cri} else (
            {18888} if pid == leftover_py else (
                {9333, 18888} if pid == 4240 else (
                    {9333} if pid in {4241, 4245} else set()
                )
            )
        ),
    )
    monkeypatch.setattr(
        bdb, "_loopback_listen_targets_for_pid",
        lambda pid: {("::1", 40141)} if pid in {leftover_ggreat, leftover_great, leftover_grand, leftover_parent, leftover_root, leftover_sup, 4300, leftover_fill, leftover_cri} else (
            {("::1", 18888)} if pid == leftover_py else (
                {("::1", 9333), ("::1", 18888)} if pid == 4240 else (
                    {("::1", 9333)} if pid in {4241, 4245} else set()
                )
            )
        ),
    )
    monkeypatch.setattr(
        bdb,
        "_cdp_port_reachable",
        lambda port, hosts: port in (40141, 18888) and "::1" in hosts,
    )
    monkeypatch.setattr(bdb, "_configured_cdp_override_url", lambda: "")
    (tmp_path / "DevToolsActivePort").write_text(
        "40141\n/devtools/browser/abc\n", encoding="utf-8",
    )
    _reset_dock_port_memory_for_tests()
    (tmp_path / "dock-cdp-port").write_text("18888\n", encoding="utf-8")
    assert bdb._unique_this_jar_parent(
        {leftover_ggreat, leftover_great, leftover_grand, leftover_parent, leftover_root, leftover_sup, 4300, leftover_fill, leftover_cri}, str(tmp_path),
    ) == leftover_gggreat
    assert bdb.lock_listed_persist_port() == 9333
    assert bdb._this_jar_chromium_pid(str(tmp_path)) == 4240
    assert bdb.shared_chromium_owner_session() == "h_review"
    assert _singleton_lock_pid(str(tmp_path)) == 4240
    assert bdb.running_instance_cdp_port(str(tmp_path)) is None
    assert _remembered_dock_attach_port() == 9333
    assert (tmp_path / "dock-cdp-port").read_text(encoding="utf-8").strip() == "18888"
    assert _last_dock_cdp_port.get(hermes_home_key()) is None
    assert _cdp_url_is_bot_desktop_browser("http://[::1]:40141") is True
    assert _cdp_url_is_bot_desktop_browser("http://[::1]:18888") is True
    assert _cdp_url_is_bot_desktop_browser("http://[::1]:9333") is True
    assert _cdp_url_is_bot_desktop_browser("9222") is False
    assert _leftover_cdp_aims_at_dock("http://[::1]:40141", 9333) is True
    assert _leftover_cdp_aims_at_dock("http://[::1]:18888", 9333) is True
    assert _leftover_host_port_aims_at_dock("::1", 40141, 9333) is True
    assert _leftover_host_port_aims_at_dock("::1", 18888, 9333) is True
    assert _leftover_host_port_aims_at_dock("127.0.0.1", 40141, 9333) is False
    assert _unregistered_cli_aims_at_dock(
        ["npx", "lighthouse", "https://example.com", "--port", "40141"],
        {}, tmp_path, 9333,
    ) is True
    assert _unregistered_cli_aims_at_dock(
        ["npx", "chrome-remote-interface", "--port", "18888", "inspect"],
        {}, tmp_path, 9333,
    ) is True
    assert _unregistered_cli_aims_at_dock(
        ["npx", "lighthouse", "--port", "9222", "https://example.com"],
        {}, tmp_path, 9333,
    ) is False
    assert _remembered_dock_attach_port() == 9333



def test_leftover_supervisor_parent_ggggreat_devtools_great_great_great_great_great_great_great_grandchild_persist_does_not_hide_sibling_chrome(
    monkeypatch, tmp_path,
):
    """Finding 213: leftover supervisor leftover parent leftover parent leftover parent leftover supervisor hid sibling chrome.

    leftover supervisor leftover parent leftover supervisor leftover
    parent leftover supervisor names this jar. leftover daemon,
    leftover fill, leftover CRI, leftover supervisor, and leftover
    supervisor leftover parent leftover supervisor leftover DevTools.
    leftover-outside leftover supervisor leftover parent leftover
    parent. leftover unique leftover parent leftover children leftover
    children leftover children is leftover supervisor leftover children
    leftover supervisor leftover children leftover daemon, missed
    leftover daemon leftover children (chrome). Identify leftover on
    leftover DevTools and leftover persist; attach chrome. 9222 and
    the other family stay unknown. Attach must not stamp persist.
    """
    import tools.bot_desktop.browser as bdb
    from hermes_constants import hermes_home_key
    from tools.browser_tool_session import (
        _cdp_url_is_bot_desktop_browser,
        _last_dock_cdp_port,
        _leftover_cdp_aims_at_dock,
        _leftover_host_port_aims_at_dock,
        _remembered_dock_attach_port,
        _reset_dock_port_memory_for_tests,
        _singleton_lock_pid,
        _unregistered_cli_aims_at_dock,
    )

    leftover_fill = 4242
    leftover_cri = 4244
    leftover_py = 4246
    leftover_sup = 4290
    leftover_root = 4280
    leftover_parent = 4270
    leftover_grand = 4260
    leftover_ggggreat = 4210
    leftover_gggreat = 4220
    leftover_ggreat = 4230
    leftover_great = 4250

    monkeypatch.setattr(runtime, "state_dir", lambda: tmp_path)
    monkeypatch.setattr(bdb, "profile_dir", lambda: tmp_path)
    monkeypatch.setattr(bdb, "_lock_pid", lambda d: None)
    monkeypatch.setattr(
        bdb,
        "_chromium_cmdline_tokens",
        lambda pid: (
            ["chrome", "--type=zygote", f"--user-data-dir={tmp_path}"]
            if pid == 4241 else
            ["chrome", "--type=utility", f"--user-data-dir={tmp_path}"]
            if pid == 4245 else
            ["agent-browser", "supervisor", f"--user-data-dir={tmp_path}"]
            if pid == leftover_ggggreat else
            ["agent-browser", "supervisor", f"--user-data-dir={tmp_path}"]
            if pid == leftover_gggreat else
            ["agent-browser", "supervisor", f"--user-data-dir={tmp_path}"]
            if pid == leftover_ggreat else
            ["agent-browser", "supervisor", f"--user-data-dir={tmp_path}"]
            if pid == leftover_great else
            ["agent-browser", "supervisor", f"--user-data-dir={tmp_path}"]
            if pid == leftover_grand else
            ["agent-browser", "supervisor", f"--user-data-dir={tmp_path}"]
            if pid == leftover_parent else
            ["agent-browser", "supervisor", f"--user-data-dir={tmp_path}"]
            if pid == leftover_root else
            ["agent-browser", "supervisor", f"--user-data-dir={tmp_path}"]
            if pid == leftover_sup else
            ["agent-browser", "daemon", f"--user-data-dir={tmp_path}"]
            if pid == 4300 else
            ["agent-browser", "fill", f"--user-data-dir={tmp_path}"]
            if pid == leftover_fill else
            ["agent-browser", "cri", f"--user-data-dir={tmp_path}"]
            if pid == leftover_cri else
            ["agent-browser", "python", f"--user-data-dir={tmp_path}"]
            if pid == leftover_py else
            ["chrome", f"--user-data-dir={tmp_path}"]
        ),
    )
    monkeypatch.setattr(
        bdb,
        "_proc_ppid",
        lambda pid: {
            4240: 4300, 4241: 4240, 4245: 4241,
            leftover_gggreat: leftover_ggggreat, leftover_ggreat: leftover_gggreat, leftover_great: leftover_ggreat, leftover_grand: leftover_great, leftover_parent: leftover_grand, leftover_root: leftover_parent, leftover_sup: leftover_root, 4300: leftover_sup,
            leftover_fill: 4300, leftover_cri: leftover_fill, leftover_py: leftover_cri,
        }.get(pid),
    )
    monkeypatch.setattr(
        bdb,
        "_launched_by_session",
        lambda pid: "h_review" if pid == 4240 else None,
    )
    monkeypatch.setattr(bdb, "_recover_cdp_port_from_singleton", lambda *a, **k: None)
    monkeypatch.setattr(
        bdb, "_this_jar_children",
        lambda parent, user_data_dir: (
            {leftover_gggreat} if parent == leftover_ggggreat else (
            {leftover_ggreat} if parent == leftover_gggreat else (
            {leftover_great} if parent == leftover_ggreat else (
            {leftover_grand} if parent == leftover_great else (
            {leftover_parent} if parent == leftover_grand else (
            {leftover_root} if parent == leftover_parent else (
                {leftover_sup} if parent == leftover_root else (
                    {4300} if parent == leftover_sup else (
                        {4240, leftover_fill} if parent == 4300 else (
                            {leftover_cri} if parent == leftover_fill else (
                                {leftover_py} if parent == leftover_cri else set()
                            )
                        )
                    )
                )
            )
            )
            )
            )
            )
            )
        ),
    )

    def _inodes(port):
        if port == 40141:
            return {7: {"::1"}}
        if port == 9333:
            return {8: {"::1"}}
        if port == 18888:
            return {9: {"::1"}}
        return {}

    def _holders(want):
        out = {}
        if 7 in want:
            out[leftover_gggreat] = {7}
            out[leftover_ggreat] = {7}
            out[leftover_great] = {7}
            out[leftover_grand] = {7}
            out[leftover_parent] = {7}
            out[leftover_root] = {7}
            out[leftover_sup] = {7}
            out[4300] = {7}
            out[leftover_fill] = {7}
            out[leftover_cri] = {7}
        if 8 in want:
            out[4240] = {8}
            out[4241] = {8}
            out[4245] = {8}
        if 9 in want:
            out[leftover_py] = {9}
            out[4240] = out.get(4240, set()) | {9}
        return out

    monkeypatch.setattr(bdb, "_loopback_listen_inodes_for_port", _inodes)
    monkeypatch.setattr(bdb, "_pids_holding_socket_inodes", _holders)
    monkeypatch.setattr(
        bdb, "_loopback_listen_ports_for_pid",
        lambda pid: {40141} if pid in {leftover_gggreat, leftover_ggreat, leftover_great, leftover_grand, leftover_parent, leftover_root, leftover_sup, 4300, leftover_fill, leftover_cri} else (
            {18888} if pid == leftover_py else (
                {9333, 18888} if pid == 4240 else (
                    {9333} if pid in {4241, 4245} else set()
                )
            )
        ),
    )
    monkeypatch.setattr(
        bdb, "_loopback_listen_targets_for_pid",
        lambda pid: {("::1", 40141)} if pid in {leftover_gggreat, leftover_ggreat, leftover_great, leftover_grand, leftover_parent, leftover_root, leftover_sup, 4300, leftover_fill, leftover_cri} else (
            {("::1", 18888)} if pid == leftover_py else (
                {("::1", 9333), ("::1", 18888)} if pid == 4240 else (
                    {("::1", 9333)} if pid in {4241, 4245} else set()
                )
            )
        ),
    )
    monkeypatch.setattr(
        bdb,
        "_cdp_port_reachable",
        lambda port, hosts: port in (40141, 18888) and "::1" in hosts,
    )
    monkeypatch.setattr(bdb, "_configured_cdp_override_url", lambda: "")
    (tmp_path / "DevToolsActivePort").write_text(
        "40141\n/devtools/browser/abc\n", encoding="utf-8",
    )
    _reset_dock_port_memory_for_tests()
    (tmp_path / "dock-cdp-port").write_text("18888\n", encoding="utf-8")
    assert bdb._unique_this_jar_parent(
        {leftover_gggreat, leftover_ggreat, leftover_great, leftover_grand, leftover_parent, leftover_root, leftover_sup, 4300, leftover_fill, leftover_cri}, str(tmp_path),
    ) == leftover_ggggreat
    assert bdb.lock_listed_persist_port() == 9333
    assert bdb._this_jar_chromium_pid(str(tmp_path)) == 4240
    assert bdb.shared_chromium_owner_session() == "h_review"
    assert _singleton_lock_pid(str(tmp_path)) == 4240
    assert bdb.running_instance_cdp_port(str(tmp_path)) is None
    assert _remembered_dock_attach_port() == 9333
    assert (tmp_path / "dock-cdp-port").read_text(encoding="utf-8").strip() == "18888"
    assert _last_dock_cdp_port.get(hermes_home_key()) is None
    assert _cdp_url_is_bot_desktop_browser("http://[::1]:40141") is True
    assert _cdp_url_is_bot_desktop_browser("http://[::1]:18888") is True
    assert _cdp_url_is_bot_desktop_browser("http://[::1]:9333") is True
    assert _cdp_url_is_bot_desktop_browser("9222") is False
    assert _leftover_cdp_aims_at_dock("http://[::1]:40141", 9333) is True
    assert _leftover_cdp_aims_at_dock("http://[::1]:18888", 9333) is True
    assert _leftover_host_port_aims_at_dock("::1", 40141, 9333) is True
    assert _leftover_host_port_aims_at_dock("::1", 18888, 9333) is True
    assert _leftover_host_port_aims_at_dock("127.0.0.1", 40141, 9333) is False
    assert _unregistered_cli_aims_at_dock(
        ["npx", "lighthouse", "https://example.com", "--port", "40141"],
        {}, tmp_path, 9333,
    ) is True
    assert _unregistered_cli_aims_at_dock(
        ["npx", "chrome-remote-interface", "--port", "18888", "inspect"],
        {}, tmp_path, 9333,
    ) is True
    assert _unregistered_cli_aims_at_dock(
        ["npx", "lighthouse", "--port", "9222", "https://example.com"],
        {}, tmp_path, 9333,
    ) is False
    assert _remembered_dock_attach_port() == 9333



def test_leftover_supervisor_parent_gggggreat_devtools_great_great_great_great_great_great_great_great_grandchild_persist_does_not_hide_sibling_chrome(
    monkeypatch, tmp_path,
):
    """Finding 214: leftover supervisor leftover parent leftover parent leftover parent leftover supervisor hid sibling chrome.

    leftover supervisor leftover parent leftover supervisor leftover
    parent leftover supervisor names this jar. leftover daemon,
    leftover fill, leftover CRI, leftover supervisor, and leftover
    supervisor leftover parent leftover supervisor leftover DevTools.
    leftover-outside leftover supervisor leftover parent leftover
    parent. leftover unique leftover parent leftover children leftover
    children leftover children is leftover supervisor leftover children
    leftover supervisor leftover children leftover daemon, missed
    leftover daemon leftover children (chrome). Identify leftover on
    leftover DevTools and leftover persist; attach chrome. 9222 and
    the other family stay unknown. Attach must not stamp persist.
    """
    import tools.bot_desktop.browser as bdb
    from hermes_constants import hermes_home_key
    from tools.browser_tool_session import (
        _cdp_url_is_bot_desktop_browser,
        _last_dock_cdp_port,
        _leftover_cdp_aims_at_dock,
        _leftover_host_port_aims_at_dock,
        _remembered_dock_attach_port,
        _reset_dock_port_memory_for_tests,
        _singleton_lock_pid,
        _unregistered_cli_aims_at_dock,
    )

    leftover_fill = 4242
    leftover_cri = 4244
    leftover_py = 4246
    leftover_sup = 4290
    leftover_root = 4280
    leftover_parent = 4270
    leftover_grand = 4260
    leftover_gggggreat = 4200
    leftover_ggggreat = 4210
    leftover_gggreat = 4220
    leftover_ggreat = 4230
    leftover_great = 4250

    monkeypatch.setattr(runtime, "state_dir", lambda: tmp_path)
    monkeypatch.setattr(bdb, "profile_dir", lambda: tmp_path)
    monkeypatch.setattr(bdb, "_lock_pid", lambda d: None)
    monkeypatch.setattr(
        bdb,
        "_chromium_cmdline_tokens",
        lambda pid: (
            ["chrome", "--type=zygote", f"--user-data-dir={tmp_path}"]
            if pid == 4241 else
            ["chrome", "--type=utility", f"--user-data-dir={tmp_path}"]
            if pid == 4245 else
            ["agent-browser", "supervisor", f"--user-data-dir={tmp_path}"]
            if pid == leftover_gggggreat else
            ["agent-browser", "supervisor", f"--user-data-dir={tmp_path}"]
            if pid == leftover_ggggreat else
            ["agent-browser", "supervisor", f"--user-data-dir={tmp_path}"]
            if pid == leftover_gggreat else
            ["agent-browser", "supervisor", f"--user-data-dir={tmp_path}"]
            if pid == leftover_ggreat else
            ["agent-browser", "supervisor", f"--user-data-dir={tmp_path}"]
            if pid == leftover_great else
            ["agent-browser", "supervisor", f"--user-data-dir={tmp_path}"]
            if pid == leftover_grand else
            ["agent-browser", "supervisor", f"--user-data-dir={tmp_path}"]
            if pid == leftover_parent else
            ["agent-browser", "supervisor", f"--user-data-dir={tmp_path}"]
            if pid == leftover_root else
            ["agent-browser", "supervisor", f"--user-data-dir={tmp_path}"]
            if pid == leftover_sup else
            ["agent-browser", "daemon", f"--user-data-dir={tmp_path}"]
            if pid == 4300 else
            ["agent-browser", "fill", f"--user-data-dir={tmp_path}"]
            if pid == leftover_fill else
            ["agent-browser", "cri", f"--user-data-dir={tmp_path}"]
            if pid == leftover_cri else
            ["agent-browser", "python", f"--user-data-dir={tmp_path}"]
            if pid == leftover_py else
            ["chrome", f"--user-data-dir={tmp_path}"]
        ),
    )
    monkeypatch.setattr(
        bdb,
        "_proc_ppid",
        lambda pid: {
            4240: 4300, 4241: 4240, 4245: 4241,
            leftover_ggggreat: leftover_gggggreat, leftover_gggreat: leftover_ggggreat, leftover_ggreat: leftover_gggreat, leftover_great: leftover_ggreat, leftover_grand: leftover_great, leftover_parent: leftover_grand, leftover_root: leftover_parent, leftover_sup: leftover_root, 4300: leftover_sup,
            leftover_fill: 4300, leftover_cri: leftover_fill, leftover_py: leftover_cri,
        }.get(pid),
    )
    monkeypatch.setattr(
        bdb,
        "_launched_by_session",
        lambda pid: "h_review" if pid == 4240 else None,
    )
    monkeypatch.setattr(bdb, "_recover_cdp_port_from_singleton", lambda *a, **k: None)
    monkeypatch.setattr(
        bdb, "_this_jar_children",
        lambda parent, user_data_dir: (
            {leftover_ggggreat} if parent == leftover_gggggreat else (
            {leftover_gggreat} if parent == leftover_ggggreat else (
            {leftover_ggreat} if parent == leftover_gggreat else (
            {leftover_great} if parent == leftover_ggreat else (
            {leftover_grand} if parent == leftover_great else (
            {leftover_parent} if parent == leftover_grand else (
            {leftover_root} if parent == leftover_parent else (
                {leftover_sup} if parent == leftover_root else (
                    {4300} if parent == leftover_sup else (
                        {4240, leftover_fill} if parent == 4300 else (
                            {leftover_cri} if parent == leftover_fill else (
                                {leftover_py} if parent == leftover_cri else set()
                            )
                        )
                    )
                )
            )
            )
            )
            )
            )
            )
            )
        ),
    )

    def _inodes(port):
        if port == 40141:
            return {7: {"::1"}}
        if port == 9333:
            return {8: {"::1"}}
        if port == 18888:
            return {9: {"::1"}}
        return {}

    def _holders(want):
        out = {}
        if 7 in want:
            out[leftover_ggggreat] = {7}
            out[leftover_gggreat] = {7}
            out[leftover_ggreat] = {7}
            out[leftover_great] = {7}
            out[leftover_grand] = {7}
            out[leftover_parent] = {7}
            out[leftover_root] = {7}
            out[leftover_sup] = {7}
            out[4300] = {7}
            out[leftover_fill] = {7}
            out[leftover_cri] = {7}
        if 8 in want:
            out[4240] = {8}
            out[4241] = {8}
            out[4245] = {8}
        if 9 in want:
            out[leftover_py] = {9}
            out[4240] = out.get(4240, set()) | {9}
        return out

    monkeypatch.setattr(bdb, "_loopback_listen_inodes_for_port", _inodes)
    monkeypatch.setattr(bdb, "_pids_holding_socket_inodes", _holders)
    monkeypatch.setattr(
        bdb, "_loopback_listen_ports_for_pid",
        lambda pid: {40141} if pid in {leftover_ggggreat, leftover_gggreat, leftover_ggreat, leftover_great, leftover_grand, leftover_parent, leftover_root, leftover_sup, 4300, leftover_fill, leftover_cri} else (
            {18888} if pid == leftover_py else (
                {9333, 18888} if pid == 4240 else (
                    {9333} if pid in {4241, 4245} else set()
                )
            )
        ),
    )
    monkeypatch.setattr(
        bdb, "_loopback_listen_targets_for_pid",
        lambda pid: {("::1", 40141)} if pid in {leftover_ggggreat, leftover_gggreat, leftover_ggreat, leftover_great, leftover_grand, leftover_parent, leftover_root, leftover_sup, 4300, leftover_fill, leftover_cri} else (
            {("::1", 18888)} if pid == leftover_py else (
                {("::1", 9333), ("::1", 18888)} if pid == 4240 else (
                    {("::1", 9333)} if pid in {4241, 4245} else set()
                )
            )
        ),
    )
    monkeypatch.setattr(
        bdb,
        "_cdp_port_reachable",
        lambda port, hosts: port in (40141, 18888) and "::1" in hosts,
    )
    monkeypatch.setattr(bdb, "_configured_cdp_override_url", lambda: "")
    (tmp_path / "DevToolsActivePort").write_text(
        "40141\n/devtools/browser/abc\n", encoding="utf-8",
    )
    _reset_dock_port_memory_for_tests()
    (tmp_path / "dock-cdp-port").write_text("18888\n", encoding="utf-8")
    assert bdb._unique_this_jar_parent(
        {leftover_ggggreat, leftover_gggreat, leftover_ggreat, leftover_great, leftover_grand, leftover_parent, leftover_root, leftover_sup, 4300, leftover_fill, leftover_cri}, str(tmp_path),
    ) == leftover_gggggreat
    assert bdb.lock_listed_persist_port() == 9333
    assert bdb._this_jar_chromium_pid(str(tmp_path)) == 4240
    assert bdb.shared_chromium_owner_session() == "h_review"
    assert _singleton_lock_pid(str(tmp_path)) == 4240
    assert bdb.running_instance_cdp_port(str(tmp_path)) is None
    assert _remembered_dock_attach_port() == 9333
    assert (tmp_path / "dock-cdp-port").read_text(encoding="utf-8").strip() == "18888"
    assert _last_dock_cdp_port.get(hermes_home_key()) is None
    assert _cdp_url_is_bot_desktop_browser("http://[::1]:40141") is True
    assert _cdp_url_is_bot_desktop_browser("http://[::1]:18888") is True
    assert _cdp_url_is_bot_desktop_browser("http://[::1]:9333") is True
    assert _cdp_url_is_bot_desktop_browser("9222") is False
    assert _leftover_cdp_aims_at_dock("http://[::1]:40141", 9333) is True
    assert _leftover_cdp_aims_at_dock("http://[::1]:18888", 9333) is True
    assert _leftover_host_port_aims_at_dock("::1", 40141, 9333) is True
    assert _leftover_host_port_aims_at_dock("::1", 18888, 9333) is True
    assert _leftover_host_port_aims_at_dock("127.0.0.1", 40141, 9333) is False
    assert _unregistered_cli_aims_at_dock(
        ["npx", "lighthouse", "https://example.com", "--port", "40141"],
        {}, tmp_path, 9333,
    ) is True
    assert _unregistered_cli_aims_at_dock(
        ["npx", "chrome-remote-interface", "--port", "18888", "inspect"],
        {}, tmp_path, 9333,
    ) is True
    assert _unregistered_cli_aims_at_dock(
        ["npx", "lighthouse", "--port", "9222", "https://example.com"],
        {}, tmp_path, 9333,
    ) is False
    assert _remembered_dock_attach_port() == 9333



def test_leftover_supervisor_parent_ggggggreat_devtools_great_great_great_great_great_great_great_great_great_grandchild_persist_does_not_hide_sibling_chrome(
    monkeypatch, tmp_path,
):
    """Finding 215: leftover supervisor leftover parent leftover parent leftover parent leftover supervisor hid sibling chrome.

    leftover supervisor leftover parent leftover supervisor leftover
    parent leftover supervisor names this jar. leftover daemon,
    leftover fill, leftover CRI, leftover supervisor, and leftover
    supervisor leftover parent leftover supervisor leftover DevTools.
    leftover-outside leftover supervisor leftover parent leftover
    parent. leftover unique leftover parent leftover children leftover
    children leftover children is leftover supervisor leftover children
    leftover supervisor leftover children leftover daemon, missed
    leftover daemon leftover children (chrome). Identify leftover on
    leftover DevTools and leftover persist; attach chrome. 9222 and
    the other family stay unknown. Attach must not stamp persist.
    """
    import tools.bot_desktop.browser as bdb
    from hermes_constants import hermes_home_key
    from tools.browser_tool_session import (
        _cdp_url_is_bot_desktop_browser,
        _last_dock_cdp_port,
        _leftover_cdp_aims_at_dock,
        _leftover_host_port_aims_at_dock,
        _remembered_dock_attach_port,
        _reset_dock_port_memory_for_tests,
        _singleton_lock_pid,
        _unregistered_cli_aims_at_dock,
    )

    leftover_fill = 4242
    leftover_cri = 4244
    leftover_py = 4246
    leftover_sup = 4290
    leftover_root = 4280
    leftover_parent = 4270
    leftover_grand = 4260
    leftover_ggggggreat = 4190
    leftover_gggggreat = 4200
    leftover_ggggreat = 4210
    leftover_gggreat = 4220
    leftover_ggreat = 4230
    leftover_great = 4250

    monkeypatch.setattr(runtime, "state_dir", lambda: tmp_path)
    monkeypatch.setattr(bdb, "profile_dir", lambda: tmp_path)
    monkeypatch.setattr(bdb, "_lock_pid", lambda d: None)
    monkeypatch.setattr(
        bdb,
        "_chromium_cmdline_tokens",
        lambda pid: (
            ["chrome", "--type=zygote", f"--user-data-dir={tmp_path}"]
            if pid == 4241 else
            ["chrome", "--type=utility", f"--user-data-dir={tmp_path}"]
            if pid == 4245 else
            ["agent-browser", "supervisor", f"--user-data-dir={tmp_path}"]
            if pid == leftover_ggggggreat else
            ["agent-browser", "supervisor", f"--user-data-dir={tmp_path}"]
            if pid == leftover_gggggreat else
            ["agent-browser", "supervisor", f"--user-data-dir={tmp_path}"]
            if pid == leftover_ggggreat else
            ["agent-browser", "supervisor", f"--user-data-dir={tmp_path}"]
            if pid == leftover_gggreat else
            ["agent-browser", "supervisor", f"--user-data-dir={tmp_path}"]
            if pid == leftover_ggreat else
            ["agent-browser", "supervisor", f"--user-data-dir={tmp_path}"]
            if pid == leftover_great else
            ["agent-browser", "supervisor", f"--user-data-dir={tmp_path}"]
            if pid == leftover_grand else
            ["agent-browser", "supervisor", f"--user-data-dir={tmp_path}"]
            if pid == leftover_parent else
            ["agent-browser", "supervisor", f"--user-data-dir={tmp_path}"]
            if pid == leftover_root else
            ["agent-browser", "supervisor", f"--user-data-dir={tmp_path}"]
            if pid == leftover_sup else
            ["agent-browser", "daemon", f"--user-data-dir={tmp_path}"]
            if pid == 4300 else
            ["agent-browser", "fill", f"--user-data-dir={tmp_path}"]
            if pid == leftover_fill else
            ["agent-browser", "cri", f"--user-data-dir={tmp_path}"]
            if pid == leftover_cri else
            ["agent-browser", "python", f"--user-data-dir={tmp_path}"]
            if pid == leftover_py else
            ["chrome", f"--user-data-dir={tmp_path}"]
        ),
    )
    monkeypatch.setattr(
        bdb,
        "_proc_ppid",
        lambda pid: {
            4240: 4300, 4241: 4240, 4245: 4241,
            leftover_gggggreat: leftover_ggggggreat, leftover_ggggreat: leftover_gggggreat, leftover_gggreat: leftover_ggggreat, leftover_ggreat: leftover_gggreat, leftover_great: leftover_ggreat, leftover_grand: leftover_great, leftover_parent: leftover_grand, leftover_root: leftover_parent, leftover_sup: leftover_root, 4300: leftover_sup,
            leftover_fill: 4300, leftover_cri: leftover_fill, leftover_py: leftover_cri,
        }.get(pid),
    )
    monkeypatch.setattr(
        bdb,
        "_launched_by_session",
        lambda pid: "h_review" if pid == 4240 else None,
    )
    monkeypatch.setattr(bdb, "_recover_cdp_port_from_singleton", lambda *a, **k: None)
    monkeypatch.setattr(
        bdb, "_this_jar_children",
        lambda parent, user_data_dir: (
            {leftover_gggggreat} if parent == leftover_ggggggreat else (
            {leftover_ggggreat} if parent == leftover_gggggreat else (
            {leftover_gggreat} if parent == leftover_ggggreat else (
            {leftover_ggreat} if parent == leftover_gggreat else (
            {leftover_great} if parent == leftover_ggreat else (
            {leftover_grand} if parent == leftover_great else (
            {leftover_parent} if parent == leftover_grand else (
            {leftover_root} if parent == leftover_parent else (
                {leftover_sup} if parent == leftover_root else (
                    {4300} if parent == leftover_sup else (
                        {4240, leftover_fill} if parent == 4300 else (
                            {leftover_cri} if parent == leftover_fill else (
                                {leftover_py} if parent == leftover_cri else set()
                            )
                        )
                    )
                )
            )
            )
            )
            )
            )
            )
            )
            )
        ),
    )

    def _inodes(port):
        if port == 40141:
            return {7: {"::1"}}
        if port == 9333:
            return {8: {"::1"}}
        if port == 18888:
            return {9: {"::1"}}
        return {}

    def _holders(want):
        out = {}
        if 7 in want:
            out[leftover_gggggreat] = {7}
            out[leftover_ggggreat] = {7}
            out[leftover_gggreat] = {7}
            out[leftover_ggreat] = {7}
            out[leftover_great] = {7}
            out[leftover_grand] = {7}
            out[leftover_parent] = {7}
            out[leftover_root] = {7}
            out[leftover_sup] = {7}
            out[4300] = {7}
            out[leftover_fill] = {7}
            out[leftover_cri] = {7}
        if 8 in want:
            out[4240] = {8}
            out[4241] = {8}
            out[4245] = {8}
        if 9 in want:
            out[leftover_py] = {9}
            out[4240] = out.get(4240, set()) | {9}
        return out

    monkeypatch.setattr(bdb, "_loopback_listen_inodes_for_port", _inodes)
    monkeypatch.setattr(bdb, "_pids_holding_socket_inodes", _holders)
    monkeypatch.setattr(
        bdb, "_loopback_listen_ports_for_pid",
        lambda pid: {40141} if pid in {leftover_gggggreat, leftover_ggggreat, leftover_gggreat, leftover_ggreat, leftover_great, leftover_grand, leftover_parent, leftover_root, leftover_sup, 4300, leftover_fill, leftover_cri} else (
            {18888} if pid == leftover_py else (
                {9333, 18888} if pid == 4240 else (
                    {9333} if pid in {4241, 4245} else set()
                )
            )
        ),
    )
    monkeypatch.setattr(
        bdb, "_loopback_listen_targets_for_pid",
        lambda pid: {("::1", 40141)} if pid in {leftover_gggggreat, leftover_ggggreat, leftover_gggreat, leftover_ggreat, leftover_great, leftover_grand, leftover_parent, leftover_root, leftover_sup, 4300, leftover_fill, leftover_cri} else (
            {("::1", 18888)} if pid == leftover_py else (
                {("::1", 9333), ("::1", 18888)} if pid == 4240 else (
                    {("::1", 9333)} if pid in {4241, 4245} else set()
                )
            )
        ),
    )
    monkeypatch.setattr(
        bdb,
        "_cdp_port_reachable",
        lambda port, hosts: port in (40141, 18888) and "::1" in hosts,
    )
    monkeypatch.setattr(bdb, "_configured_cdp_override_url", lambda: "")
    (tmp_path / "DevToolsActivePort").write_text(
        "40141\n/devtools/browser/abc\n", encoding="utf-8",
    )
    _reset_dock_port_memory_for_tests()
    (tmp_path / "dock-cdp-port").write_text("18888\n", encoding="utf-8")
    assert bdb._unique_this_jar_parent(
        {leftover_gggggreat, leftover_ggggreat, leftover_gggreat, leftover_ggreat, leftover_great, leftover_grand, leftover_parent, leftover_root, leftover_sup, 4300, leftover_fill, leftover_cri}, str(tmp_path),
    ) == leftover_ggggggreat
    assert bdb.lock_listed_persist_port() == 9333
    assert bdb._this_jar_chromium_pid(str(tmp_path)) == 4240
    assert bdb.shared_chromium_owner_session() == "h_review"
    assert _singleton_lock_pid(str(tmp_path)) == 4240
    assert bdb.running_instance_cdp_port(str(tmp_path)) is None
    assert _remembered_dock_attach_port() == 9333
    assert (tmp_path / "dock-cdp-port").read_text(encoding="utf-8").strip() == "18888"
    assert _last_dock_cdp_port.get(hermes_home_key()) is None
    assert _cdp_url_is_bot_desktop_browser("http://[::1]:40141") is True
    assert _cdp_url_is_bot_desktop_browser("http://[::1]:18888") is True
    assert _cdp_url_is_bot_desktop_browser("http://[::1]:9333") is True
    assert _cdp_url_is_bot_desktop_browser("9222") is False
    assert _leftover_cdp_aims_at_dock("http://[::1]:40141", 9333) is True
    assert _leftover_cdp_aims_at_dock("http://[::1]:18888", 9333) is True
    assert _leftover_host_port_aims_at_dock("::1", 40141, 9333) is True
    assert _leftover_host_port_aims_at_dock("::1", 18888, 9333) is True
    assert _leftover_host_port_aims_at_dock("127.0.0.1", 40141, 9333) is False
    assert _unregistered_cli_aims_at_dock(
        ["npx", "lighthouse", "https://example.com", "--port", "40141"],
        {}, tmp_path, 9333,
    ) is True
    assert _unregistered_cli_aims_at_dock(
        ["npx", "chrome-remote-interface", "--port", "18888", "inspect"],
        {}, tmp_path, 9333,
    ) is True
    assert _unregistered_cli_aims_at_dock(
        ["npx", "lighthouse", "--port", "9222", "https://example.com"],
        {}, tmp_path, 9333,
    ) is False
    assert _remembered_dock_attach_port() == 9333



def test_leftover_supervisor_parent_gggggggreat_devtools_great_great_great_great_great_great_great_great_great_great_grandchild_persist_does_not_hide_sibling_chrome(
    monkeypatch, tmp_path,
):
    """Finding 216: leftover supervisor leftover parent leftover parent leftover parent leftover supervisor hid sibling chrome.

    leftover supervisor leftover parent leftover supervisor leftover
    parent leftover supervisor names this jar. leftover daemon,
    leftover fill, leftover CRI, leftover supervisor, and leftover
    supervisor leftover parent leftover supervisor leftover DevTools.
    leftover-outside leftover supervisor leftover parent leftover
    parent. leftover unique leftover parent leftover children leftover
    children leftover children is leftover supervisor leftover children
    leftover supervisor leftover children leftover daemon, missed
    leftover daemon leftover children (chrome). Identify leftover on
    leftover DevTools and leftover persist; attach chrome. 9222 and
    the other family stay unknown. Attach must not stamp persist.
    """
    import tools.bot_desktop.browser as bdb
    from hermes_constants import hermes_home_key
    from tools.browser_tool_session import (
        _cdp_url_is_bot_desktop_browser,
        _last_dock_cdp_port,
        _leftover_cdp_aims_at_dock,
        _leftover_host_port_aims_at_dock,
        _remembered_dock_attach_port,
        _reset_dock_port_memory_for_tests,
        _singleton_lock_pid,
        _unregistered_cli_aims_at_dock,
    )

    leftover_fill = 4242
    leftover_cri = 4244
    leftover_py = 4246
    leftover_sup = 4290
    leftover_root = 4280
    leftover_parent = 4270
    leftover_grand = 4260
    leftover_gggggggreat = 4180
    leftover_ggggggreat = 4190
    leftover_gggggreat = 4200
    leftover_ggggreat = 4210
    leftover_gggreat = 4220
    leftover_ggreat = 4230
    leftover_great = 4250

    monkeypatch.setattr(runtime, "state_dir", lambda: tmp_path)
    monkeypatch.setattr(bdb, "profile_dir", lambda: tmp_path)
    monkeypatch.setattr(bdb, "_lock_pid", lambda d: None)
    monkeypatch.setattr(
        bdb,
        "_chromium_cmdline_tokens",
        lambda pid: (
            ["chrome", "--type=zygote", f"--user-data-dir={tmp_path}"]
            if pid == 4241 else
            ["chrome", "--type=utility", f"--user-data-dir={tmp_path}"]
            if pid == 4245 else
            ["agent-browser", "supervisor", f"--user-data-dir={tmp_path}"]
            if pid == leftover_gggggggreat else
            ["agent-browser", "supervisor", f"--user-data-dir={tmp_path}"]
            if pid == leftover_ggggggreat else
            ["agent-browser", "supervisor", f"--user-data-dir={tmp_path}"]
            if pid == leftover_gggggreat else
            ["agent-browser", "supervisor", f"--user-data-dir={tmp_path}"]
            if pid == leftover_ggggreat else
            ["agent-browser", "supervisor", f"--user-data-dir={tmp_path}"]
            if pid == leftover_gggreat else
            ["agent-browser", "supervisor", f"--user-data-dir={tmp_path}"]
            if pid == leftover_ggreat else
            ["agent-browser", "supervisor", f"--user-data-dir={tmp_path}"]
            if pid == leftover_great else
            ["agent-browser", "supervisor", f"--user-data-dir={tmp_path}"]
            if pid == leftover_grand else
            ["agent-browser", "supervisor", f"--user-data-dir={tmp_path}"]
            if pid == leftover_parent else
            ["agent-browser", "supervisor", f"--user-data-dir={tmp_path}"]
            if pid == leftover_root else
            ["agent-browser", "supervisor", f"--user-data-dir={tmp_path}"]
            if pid == leftover_sup else
            ["agent-browser", "daemon", f"--user-data-dir={tmp_path}"]
            if pid == 4300 else
            ["agent-browser", "fill", f"--user-data-dir={tmp_path}"]
            if pid == leftover_fill else
            ["agent-browser", "cri", f"--user-data-dir={tmp_path}"]
            if pid == leftover_cri else
            ["agent-browser", "python", f"--user-data-dir={tmp_path}"]
            if pid == leftover_py else
            ["chrome", f"--user-data-dir={tmp_path}"]
        ),
    )
    monkeypatch.setattr(
        bdb,
        "_proc_ppid",
        lambda pid: {
            4240: 4300, 4241: 4240, 4245: 4241,
            leftover_ggggggreat: leftover_gggggggreat, leftover_gggggreat: leftover_ggggggreat, leftover_ggggreat: leftover_gggggreat, leftover_gggreat: leftover_ggggreat, leftover_ggreat: leftover_gggreat, leftover_great: leftover_ggreat, leftover_grand: leftover_great, leftover_parent: leftover_grand, leftover_root: leftover_parent, leftover_sup: leftover_root, 4300: leftover_sup,
            leftover_fill: 4300, leftover_cri: leftover_fill, leftover_py: leftover_cri,
        }.get(pid),
    )
    monkeypatch.setattr(
        bdb,
        "_launched_by_session",
        lambda pid: "h_review" if pid == 4240 else None,
    )
    monkeypatch.setattr(bdb, "_recover_cdp_port_from_singleton", lambda *a, **k: None)
    monkeypatch.setattr(
        bdb, "_this_jar_children",
        lambda parent, user_data_dir: (
            {leftover_ggggggreat} if parent == leftover_gggggggreat else (
            {leftover_gggggreat} if parent == leftover_ggggggreat else (
            {leftover_ggggreat} if parent == leftover_gggggreat else (
            {leftover_gggreat} if parent == leftover_ggggreat else (
            {leftover_ggreat} if parent == leftover_gggreat else (
            {leftover_great} if parent == leftover_ggreat else (
            {leftover_grand} if parent == leftover_great else (
            {leftover_parent} if parent == leftover_grand else (
            {leftover_root} if parent == leftover_parent else (
                {leftover_sup} if parent == leftover_root else (
                    {4300} if parent == leftover_sup else (
                        {4240, leftover_fill} if parent == 4300 else (
                            {leftover_cri} if parent == leftover_fill else (
                                {leftover_py} if parent == leftover_cri else set()
                            )
                        )
                    )
                )
            )
            )
            )
            )
            )
            )
            )
            )
            )
        ),
    )

    def _inodes(port):
        if port == 40141:
            return {7: {"::1"}}
        if port == 9333:
            return {8: {"::1"}}
        if port == 18888:
            return {9: {"::1"}}
        return {}

    def _holders(want):
        out = {}
        if 7 in want:
            out[leftover_ggggggreat] = {7}
            out[leftover_gggggreat] = {7}
            out[leftover_ggggreat] = {7}
            out[leftover_gggreat] = {7}
            out[leftover_ggreat] = {7}
            out[leftover_great] = {7}
            out[leftover_grand] = {7}
            out[leftover_parent] = {7}
            out[leftover_root] = {7}
            out[leftover_sup] = {7}
            out[4300] = {7}
            out[leftover_fill] = {7}
            out[leftover_cri] = {7}
        if 8 in want:
            out[4240] = {8}
            out[4241] = {8}
            out[4245] = {8}
        if 9 in want:
            out[leftover_py] = {9}
            out[4240] = out.get(4240, set()) | {9}
        return out

    monkeypatch.setattr(bdb, "_loopback_listen_inodes_for_port", _inodes)
    monkeypatch.setattr(bdb, "_pids_holding_socket_inodes", _holders)
    monkeypatch.setattr(
        bdb, "_loopback_listen_ports_for_pid",
        lambda pid: {40141} if pid in {leftover_ggggggreat, leftover_gggggreat, leftover_ggggreat, leftover_gggreat, leftover_ggreat, leftover_great, leftover_grand, leftover_parent, leftover_root, leftover_sup, 4300, leftover_fill, leftover_cri} else (
            {18888} if pid == leftover_py else (
                {9333, 18888} if pid == 4240 else (
                    {9333} if pid in {4241, 4245} else set()
                )
            )
        ),
    )
    monkeypatch.setattr(
        bdb, "_loopback_listen_targets_for_pid",
        lambda pid: {("::1", 40141)} if pid in {leftover_ggggggreat, leftover_gggggreat, leftover_ggggreat, leftover_gggreat, leftover_ggreat, leftover_great, leftover_grand, leftover_parent, leftover_root, leftover_sup, 4300, leftover_fill, leftover_cri} else (
            {("::1", 18888)} if pid == leftover_py else (
                {("::1", 9333), ("::1", 18888)} if pid == 4240 else (
                    {("::1", 9333)} if pid in {4241, 4245} else set()
                )
            )
        ),
    )
    monkeypatch.setattr(
        bdb,
        "_cdp_port_reachable",
        lambda port, hosts: port in (40141, 18888) and "::1" in hosts,
    )
    monkeypatch.setattr(bdb, "_configured_cdp_override_url", lambda: "")
    (tmp_path / "DevToolsActivePort").write_text(
        "40141\n/devtools/browser/abc\n", encoding="utf-8",
    )
    _reset_dock_port_memory_for_tests()
    (tmp_path / "dock-cdp-port").write_text("18888\n", encoding="utf-8")
    assert bdb._unique_this_jar_parent(
        {leftover_ggggggreat, leftover_gggggreat, leftover_ggggreat, leftover_gggreat, leftover_ggreat, leftover_great, leftover_grand, leftover_parent, leftover_root, leftover_sup, 4300, leftover_fill, leftover_cri}, str(tmp_path),
    ) == leftover_gggggggreat
    assert bdb.lock_listed_persist_port() == 9333
    assert bdb._this_jar_chromium_pid(str(tmp_path)) == 4240
    assert bdb.shared_chromium_owner_session() == "h_review"
    assert _singleton_lock_pid(str(tmp_path)) == 4240
    assert bdb.running_instance_cdp_port(str(tmp_path)) is None
    assert _remembered_dock_attach_port() == 9333
    assert (tmp_path / "dock-cdp-port").read_text(encoding="utf-8").strip() == "18888"
    assert _last_dock_cdp_port.get(hermes_home_key()) is None
    assert _cdp_url_is_bot_desktop_browser("http://[::1]:40141") is True
    assert _cdp_url_is_bot_desktop_browser("http://[::1]:18888") is True
    assert _cdp_url_is_bot_desktop_browser("http://[::1]:9333") is True
    assert _cdp_url_is_bot_desktop_browser("9222") is False
    assert _leftover_cdp_aims_at_dock("http://[::1]:40141", 9333) is True
    assert _leftover_cdp_aims_at_dock("http://[::1]:18888", 9333) is True
    assert _leftover_host_port_aims_at_dock("::1", 40141, 9333) is True
    assert _leftover_host_port_aims_at_dock("::1", 18888, 9333) is True
    assert _leftover_host_port_aims_at_dock("127.0.0.1", 40141, 9333) is False
    assert _unregistered_cli_aims_at_dock(
        ["npx", "lighthouse", "https://example.com", "--port", "40141"],
        {}, tmp_path, 9333,
    ) is True
    assert _unregistered_cli_aims_at_dock(
        ["npx", "chrome-remote-interface", "--port", "18888", "inspect"],
        {}, tmp_path, 9333,
    ) is True
    assert _unregistered_cli_aims_at_dock(
        ["npx", "lighthouse", "--port", "9222", "https://example.com"],
        {}, tmp_path, 9333,
    ) is False
    assert _remembered_dock_attach_port() == 9333



def test_leftover_supervisor_parent_ggggggggreat_devtools_great_great_great_great_great_great_great_great_great_great_great_grandchild_persist_does_not_hide_sibling_chrome(
    monkeypatch, tmp_path,
):
    """Finding 217: leftover supervisor leftover parent leftover parent leftover parent leftover supervisor hid sibling chrome.

    leftover supervisor leftover parent leftover supervisor leftover
    parent leftover supervisor names this jar. leftover daemon,
    leftover fill, leftover CRI, leftover supervisor, and leftover
    supervisor leftover parent leftover supervisor leftover DevTools.
    leftover-outside leftover supervisor leftover parent leftover
    parent. leftover unique leftover parent leftover children leftover
    children leftover children is leftover supervisor leftover children
    leftover supervisor leftover children leftover daemon, missed
    leftover daemon leftover children (chrome). Identify leftover on
    leftover DevTools and leftover persist; attach chrome. 9222 and
    the other family stay unknown. Attach must not stamp persist.
    """
    import tools.bot_desktop.browser as bdb
    from hermes_constants import hermes_home_key
    from tools.browser_tool_session import (
        _cdp_url_is_bot_desktop_browser,
        _last_dock_cdp_port,
        _leftover_cdp_aims_at_dock,
        _leftover_host_port_aims_at_dock,
        _remembered_dock_attach_port,
        _reset_dock_port_memory_for_tests,
        _singleton_lock_pid,
        _unregistered_cli_aims_at_dock,
    )

    leftover_fill = 4242
    leftover_cri = 4244
    leftover_py = 4246
    leftover_sup = 4290
    leftover_root = 4280
    leftover_parent = 4270
    leftover_grand = 4260
    leftover_ggggggggreat = 4170
    leftover_gggggggreat = 4180
    leftover_ggggggreat = 4190
    leftover_gggggreat = 4200
    leftover_ggggreat = 4210
    leftover_gggreat = 4220
    leftover_ggreat = 4230
    leftover_great = 4250

    monkeypatch.setattr(runtime, "state_dir", lambda: tmp_path)
    monkeypatch.setattr(bdb, "profile_dir", lambda: tmp_path)
    monkeypatch.setattr(bdb, "_lock_pid", lambda d: None)
    monkeypatch.setattr(
        bdb,
        "_chromium_cmdline_tokens",
        lambda pid: (
            ["chrome", "--type=zygote", f"--user-data-dir={tmp_path}"]
            if pid == 4241 else
            ["chrome", "--type=utility", f"--user-data-dir={tmp_path}"]
            if pid == 4245 else
            ["agent-browser", "supervisor", f"--user-data-dir={tmp_path}"]
            if pid == leftover_ggggggggreat else
            ["agent-browser", "supervisor", f"--user-data-dir={tmp_path}"]
            if pid == leftover_gggggggreat else
            ["agent-browser", "supervisor", f"--user-data-dir={tmp_path}"]
            if pid == leftover_ggggggreat else
            ["agent-browser", "supervisor", f"--user-data-dir={tmp_path}"]
            if pid == leftover_gggggreat else
            ["agent-browser", "supervisor", f"--user-data-dir={tmp_path}"]
            if pid == leftover_ggggreat else
            ["agent-browser", "supervisor", f"--user-data-dir={tmp_path}"]
            if pid == leftover_gggreat else
            ["agent-browser", "supervisor", f"--user-data-dir={tmp_path}"]
            if pid == leftover_ggreat else
            ["agent-browser", "supervisor", f"--user-data-dir={tmp_path}"]
            if pid == leftover_great else
            ["agent-browser", "supervisor", f"--user-data-dir={tmp_path}"]
            if pid == leftover_grand else
            ["agent-browser", "supervisor", f"--user-data-dir={tmp_path}"]
            if pid == leftover_parent else
            ["agent-browser", "supervisor", f"--user-data-dir={tmp_path}"]
            if pid == leftover_root else
            ["agent-browser", "supervisor", f"--user-data-dir={tmp_path}"]
            if pid == leftover_sup else
            ["agent-browser", "daemon", f"--user-data-dir={tmp_path}"]
            if pid == 4300 else
            ["agent-browser", "fill", f"--user-data-dir={tmp_path}"]
            if pid == leftover_fill else
            ["agent-browser", "cri", f"--user-data-dir={tmp_path}"]
            if pid == leftover_cri else
            ["agent-browser", "python", f"--user-data-dir={tmp_path}"]
            if pid == leftover_py else
            ["chrome", f"--user-data-dir={tmp_path}"]
        ),
    )
    monkeypatch.setattr(
        bdb,
        "_proc_ppid",
        lambda pid: {
            4240: 4300, 4241: 4240, 4245: 4241,
            leftover_gggggggreat: leftover_ggggggggreat, leftover_ggggggreat: leftover_gggggggreat, leftover_gggggreat: leftover_ggggggreat, leftover_ggggreat: leftover_gggggreat, leftover_gggreat: leftover_ggggreat, leftover_ggreat: leftover_gggreat, leftover_great: leftover_ggreat, leftover_grand: leftover_great, leftover_parent: leftover_grand, leftover_root: leftover_parent, leftover_sup: leftover_root, 4300: leftover_sup,
            leftover_fill: 4300, leftover_cri: leftover_fill, leftover_py: leftover_cri,
        }.get(pid),
    )
    monkeypatch.setattr(
        bdb,
        "_launched_by_session",
        lambda pid: "h_review" if pid == 4240 else None,
    )
    monkeypatch.setattr(bdb, "_recover_cdp_port_from_singleton", lambda *a, **k: None)
    monkeypatch.setattr(
        bdb, "_this_jar_children",
        lambda parent, user_data_dir: (
            {leftover_gggggggreat} if parent == leftover_ggggggggreat else (
            {leftover_ggggggreat} if parent == leftover_gggggggreat else (
            {leftover_gggggreat} if parent == leftover_ggggggreat else (
            {leftover_ggggreat} if parent == leftover_gggggreat else (
            {leftover_gggreat} if parent == leftover_ggggreat else (
            {leftover_ggreat} if parent == leftover_gggreat else (
            {leftover_great} if parent == leftover_ggreat else (
            {leftover_grand} if parent == leftover_great else (
            {leftover_parent} if parent == leftover_grand else (
            {leftover_root} if parent == leftover_parent else (
                {leftover_sup} if parent == leftover_root else (
                    {4300} if parent == leftover_sup else (
                        {4240, leftover_fill} if parent == 4300 else (
                            {leftover_cri} if parent == leftover_fill else (
                                {leftover_py} if parent == leftover_cri else set()
                            )
                            )
                        )
                    )
                )
            )
            )
            )
            )
            )
            )
            )
            )
            )
        ),
    )

    def _inodes(port):
        if port == 40141:
            return {7: {"::1"}}
        if port == 9333:
            return {8: {"::1"}}
        if port == 18888:
            return {9: {"::1"}}
        return {}

    def _holders(want):
        out = {}
        if 7 in want:
            out[leftover_gggggggreat] = {7}
            out[leftover_ggggggreat] = {7}
            out[leftover_gggggreat] = {7}
            out[leftover_ggggreat] = {7}
            out[leftover_gggreat] = {7}
            out[leftover_ggreat] = {7}
            out[leftover_great] = {7}
            out[leftover_grand] = {7}
            out[leftover_parent] = {7}
            out[leftover_root] = {7}
            out[leftover_sup] = {7}
            out[4300] = {7}
            out[leftover_fill] = {7}
            out[leftover_cri] = {7}
        if 8 in want:
            out[4240] = {8}
            out[4241] = {8}
            out[4245] = {8}
        if 9 in want:
            out[leftover_py] = {9}
            out[4240] = out.get(4240, set()) | {9}
        return out

    monkeypatch.setattr(bdb, "_loopback_listen_inodes_for_port", _inodes)
    monkeypatch.setattr(bdb, "_pids_holding_socket_inodes", _holders)
    monkeypatch.setattr(
        bdb, "_loopback_listen_ports_for_pid",
        lambda pid: {40141} if pid in {leftover_gggggggreat, leftover_ggggggreat, leftover_gggggreat, leftover_ggggreat, leftover_gggreat, leftover_ggreat, leftover_great, leftover_grand, leftover_parent, leftover_root, leftover_sup, 4300, leftover_fill, leftover_cri} else (
            {18888} if pid == leftover_py else (
                {9333, 18888} if pid == 4240 else (
                    {9333} if pid in {4241, 4245} else set()
                )
            )
        ),
    )
    monkeypatch.setattr(
        bdb, "_loopback_listen_targets_for_pid",
        lambda pid: {("::1", 40141)} if pid in {leftover_gggggggreat, leftover_ggggggreat, leftover_gggggreat, leftover_ggggreat, leftover_gggreat, leftover_ggreat, leftover_great, leftover_grand, leftover_parent, leftover_root, leftover_sup, 4300, leftover_fill, leftover_cri} else (
            {("::1", 18888)} if pid == leftover_py else (
                {("::1", 9333), ("::1", 18888)} if pid == 4240 else (
                    {("::1", 9333)} if pid in {4241, 4245} else set()
                )
            )
        ),
    )
    monkeypatch.setattr(
        bdb,
        "_cdp_port_reachable",
        lambda port, hosts: port in (40141, 18888) and "::1" in hosts,
    )
    monkeypatch.setattr(bdb, "_configured_cdp_override_url", lambda: "")
    (tmp_path / "DevToolsActivePort").write_text(
        "40141\n/devtools/browser/abc\n", encoding="utf-8",
    )
    _reset_dock_port_memory_for_tests()
    (tmp_path / "dock-cdp-port").write_text("18888\n", encoding="utf-8")
    assert bdb._unique_this_jar_parent(
        {leftover_gggggggreat, leftover_ggggggreat, leftover_gggggreat, leftover_ggggreat, leftover_gggreat, leftover_ggreat, leftover_great, leftover_grand, leftover_parent, leftover_root, leftover_sup, 4300, leftover_fill, leftover_cri}, str(tmp_path),
    ) == leftover_ggggggggreat
    assert bdb.lock_listed_persist_port() == 9333
    assert bdb._this_jar_chromium_pid(str(tmp_path)) == 4240
    assert bdb.shared_chromium_owner_session() == "h_review"
    assert _singleton_lock_pid(str(tmp_path)) == 4240
    assert bdb.running_instance_cdp_port(str(tmp_path)) is None
    assert _remembered_dock_attach_port() == 9333
    assert (tmp_path / "dock-cdp-port").read_text(encoding="utf-8").strip() == "18888"
    assert _last_dock_cdp_port.get(hermes_home_key()) is None
    assert _cdp_url_is_bot_desktop_browser("http://[::1]:40141") is True
    assert _cdp_url_is_bot_desktop_browser("http://[::1]:18888") is True
    assert _cdp_url_is_bot_desktop_browser("http://[::1]:9333") is True
    assert _cdp_url_is_bot_desktop_browser("9222") is False
    assert _leftover_cdp_aims_at_dock("http://[::1]:40141", 9333) is True
    assert _leftover_cdp_aims_at_dock("http://[::1]:18888", 9333) is True
    assert _leftover_host_port_aims_at_dock("::1", 40141, 9333) is True
    assert _leftover_host_port_aims_at_dock("::1", 18888, 9333) is True
    assert _leftover_host_port_aims_at_dock("127.0.0.1", 40141, 9333) is False
    assert _unregistered_cli_aims_at_dock(
        ["npx", "lighthouse", "https://example.com", "--port", "40141"],
        {}, tmp_path, 9333,
    ) is True
    assert _unregistered_cli_aims_at_dock(
        ["npx", "chrome-remote-interface", "--port", "18888", "inspect"],
        {}, tmp_path, 9333,
    ) is True
    assert _unregistered_cli_aims_at_dock(
        ["npx", "lighthouse", "--port", "9222", "https://example.com"],
        {}, tmp_path, 9333,
    ) is False
    assert _remembered_dock_attach_port() == 9333


def test_leftover_supervisor_parent_gggggggggreat_devtools_great_great_great_great_great_great_great_great_great_great_great_great_grandchild_persist_does_not_hide_sibling_chrome(
    monkeypatch, tmp_path,
):
    """Finding 218: leftover supervisor leftover parent leftover parent leftover parent leftover supervisor hid sibling chrome.

    leftover supervisor leftover parent leftover supervisor leftover
    parent leftover supervisor names this jar. leftover daemon,
    leftover fill, leftover CRI, leftover supervisor, and leftover
    supervisor leftover parent leftover supervisor leftover DevTools.
    leftover-outside leftover supervisor leftover parent leftover
    parent. leftover unique leftover parent leftover children leftover
    children leftover children is leftover supervisor leftover children
    leftover supervisor leftover children leftover daemon, missed
    leftover daemon leftover children (chrome). Identify leftover on
    leftover DevTools and leftover persist; attach chrome. 9222 and
    the other family stay unknown. Attach must not stamp persist.
    """
    import tools.bot_desktop.browser as bdb
    from hermes_constants import hermes_home_key
    from tools.browser_tool_session import (
        _cdp_url_is_bot_desktop_browser,
        _last_dock_cdp_port,
        _leftover_cdp_aims_at_dock,
        _leftover_host_port_aims_at_dock,
        _remembered_dock_attach_port,
        _reset_dock_port_memory_for_tests,
        _singleton_lock_pid,
        _unregistered_cli_aims_at_dock,
    )

    leftover_fill = 4242
    leftover_cri = 4244
    leftover_py = 4246
    leftover_sup = 4290
    leftover_root = 4280
    leftover_parent = 4270
    leftover_grand = 4260
    leftover_gggggggggreat = 4160
    leftover_ggggggggreat = 4170
    leftover_gggggggreat = 4180
    leftover_ggggggreat = 4190
    leftover_gggggreat = 4200
    leftover_ggggreat = 4210
    leftover_gggreat = 4220
    leftover_ggreat = 4230
    leftover_great = 4250

    monkeypatch.setattr(runtime, "state_dir", lambda: tmp_path)
    monkeypatch.setattr(bdb, "profile_dir", lambda: tmp_path)
    monkeypatch.setattr(bdb, "_lock_pid", lambda d: None)
    monkeypatch.setattr(
        bdb,
        "_chromium_cmdline_tokens",
        lambda pid: (
            ["chrome", "--type=zygote", f"--user-data-dir={tmp_path}"]
            if pid == 4241 else
            ["chrome", "--type=utility", f"--user-data-dir={tmp_path}"]
            if pid == 4245 else
            ["agent-browser", "supervisor", f"--user-data-dir={tmp_path}"]
            if pid == leftover_gggggggggreat else
            ["agent-browser", "supervisor", f"--user-data-dir={tmp_path}"]
            if pid == leftover_ggggggggreat else
            ["agent-browser", "supervisor", f"--user-data-dir={tmp_path}"]
            if pid == leftover_gggggggreat else
            ["agent-browser", "supervisor", f"--user-data-dir={tmp_path}"]
            if pid == leftover_ggggggreat else
            ["agent-browser", "supervisor", f"--user-data-dir={tmp_path}"]
            if pid == leftover_gggggreat else
            ["agent-browser", "supervisor", f"--user-data-dir={tmp_path}"]
            if pid == leftover_ggggreat else
            ["agent-browser", "supervisor", f"--user-data-dir={tmp_path}"]
            if pid == leftover_gggreat else
            ["agent-browser", "supervisor", f"--user-data-dir={tmp_path}"]
            if pid == leftover_ggreat else
            ["agent-browser", "supervisor", f"--user-data-dir={tmp_path}"]
            if pid == leftover_great else
            ["agent-browser", "supervisor", f"--user-data-dir={tmp_path}"]
            if pid == leftover_grand else
            ["agent-browser", "supervisor", f"--user-data-dir={tmp_path}"]
            if pid == leftover_parent else
            ["agent-browser", "supervisor", f"--user-data-dir={tmp_path}"]
            if pid == leftover_root else
            ["agent-browser", "supervisor", f"--user-data-dir={tmp_path}"]
            if pid == leftover_sup else
            ["agent-browser", "daemon", f"--user-data-dir={tmp_path}"]
            if pid == 4300 else
            ["agent-browser", "fill", f"--user-data-dir={tmp_path}"]
            if pid == leftover_fill else
            ["agent-browser", "cri", f"--user-data-dir={tmp_path}"]
            if pid == leftover_cri else
            ["agent-browser", "python", f"--user-data-dir={tmp_path}"]
            if pid == leftover_py else
            ["chrome", f"--user-data-dir={tmp_path}"]
        ),
    )
    monkeypatch.setattr(
        bdb,
        "_proc_ppid",
        lambda pid: {
            4240: 4300, 4241: 4240, 4245: 4241,
            leftover_ggggggggreat: leftover_gggggggggreat, leftover_gggggggreat: leftover_ggggggggreat, leftover_ggggggreat: leftover_gggggggreat, leftover_gggggreat: leftover_ggggggreat, leftover_ggggreat: leftover_gggggreat, leftover_gggreat: leftover_ggggreat, leftover_ggreat: leftover_gggreat, leftover_great: leftover_ggreat, leftover_grand: leftover_great, leftover_parent: leftover_grand, leftover_root: leftover_parent, leftover_sup: leftover_root, 4300: leftover_sup,
            leftover_fill: 4300, leftover_cri: leftover_fill, leftover_py: leftover_cri,
        }.get(pid),
    )
    monkeypatch.setattr(
        bdb,
        "_launched_by_session",
        lambda pid: "h_review" if pid == 4240 else None,
    )
    monkeypatch.setattr(bdb, "_recover_cdp_port_from_singleton", lambda *a, **k: None)
    monkeypatch.setattr(
        bdb, "_this_jar_children",
        lambda parent, user_data_dir: (
            {leftover_ggggggggreat} if parent == leftover_gggggggggreat else (
            {leftover_gggggggreat} if parent == leftover_ggggggggreat else (
            {leftover_ggggggreat} if parent == leftover_gggggggreat else (
            {leftover_gggggreat} if parent == leftover_ggggggreat else (
            {leftover_ggggreat} if parent == leftover_gggggreat else (
            {leftover_gggreat} if parent == leftover_ggggreat else (
            {leftover_ggreat} if parent == leftover_gggreat else (
            {leftover_great} if parent == leftover_ggreat else (
            {leftover_grand} if parent == leftover_great else (
            {leftover_parent} if parent == leftover_grand else (
            {leftover_root} if parent == leftover_parent else (
                {leftover_sup} if parent == leftover_root else (
                    {4300} if parent == leftover_sup else (
                        {4240, leftover_fill} if parent == 4300 else (
                            {leftover_cri} if parent == leftover_fill else (
                                {leftover_py} if parent == leftover_cri else set()
                            )
                            )
                        )
                    )
                )
            )
            )
            )
            )
            )
            )
            )
            )
            )
            )
        ),
    )

    def _inodes(port):
        if port == 40141:
            return {7: {"::1"}}
        if port == 9333:
            return {8: {"::1"}}
        if port == 18888:
            return {9: {"::1"}}
        return {}

    def _holders(want):
        out = {}
        if 7 in want:
            out[leftover_ggggggggreat] = {7}
            out[leftover_gggggggreat] = {7}
            out[leftover_ggggggreat] = {7}
            out[leftover_gggggreat] = {7}
            out[leftover_ggggreat] = {7}
            out[leftover_gggreat] = {7}
            out[leftover_ggreat] = {7}
            out[leftover_great] = {7}
            out[leftover_grand] = {7}
            out[leftover_parent] = {7}
            out[leftover_root] = {7}
            out[leftover_sup] = {7}
            out[4300] = {7}
            out[leftover_fill] = {7}
            out[leftover_cri] = {7}
        if 8 in want:
            out[4240] = {8}
            out[4241] = {8}
            out[4245] = {8}
        if 9 in want:
            out[leftover_py] = {9}
            out[4240] = out.get(4240, set()) | {9}
        return out

    monkeypatch.setattr(bdb, "_loopback_listen_inodes_for_port", _inodes)
    monkeypatch.setattr(bdb, "_pids_holding_socket_inodes", _holders)
    monkeypatch.setattr(
        bdb, "_loopback_listen_ports_for_pid",
        lambda pid: {40141} if pid in {leftover_ggggggggreat, leftover_gggggggreat, leftover_ggggggreat, leftover_gggggreat, leftover_ggggreat, leftover_gggreat, leftover_ggreat, leftover_great, leftover_grand, leftover_parent, leftover_root, leftover_sup, 4300, leftover_fill, leftover_cri} else (
            {18888} if pid == leftover_py else (
                {9333, 18888} if pid == 4240 else (
                    {9333} if pid in {4241, 4245} else set()
                )
            )
        ),
    )
    monkeypatch.setattr(
        bdb, "_loopback_listen_targets_for_pid",
        lambda pid: {("::1", 40141)} if pid in {leftover_ggggggggreat, leftover_gggggggreat, leftover_ggggggreat, leftover_gggggreat, leftover_ggggreat, leftover_gggreat, leftover_ggreat, leftover_great, leftover_grand, leftover_parent, leftover_root, leftover_sup, 4300, leftover_fill, leftover_cri} else (
            {("::1", 18888)} if pid == leftover_py else (
                {("::1", 9333), ("::1", 18888)} if pid == 4240 else (
                    {("::1", 9333)} if pid in {4241, 4245} else set()
                )
            )
        ),
    )
    monkeypatch.setattr(
        bdb,
        "_cdp_port_reachable",
        lambda port, hosts: port in (40141, 18888) and "::1" in hosts,
    )
    monkeypatch.setattr(bdb, "_configured_cdp_override_url", lambda: "")
    (tmp_path / "DevToolsActivePort").write_text(
        "40141\n/devtools/browser/abc\n", encoding="utf-8",
    )
    _reset_dock_port_memory_for_tests()
    (tmp_path / "dock-cdp-port").write_text("18888\n", encoding="utf-8")
    assert bdb._unique_this_jar_parent(
        {leftover_ggggggggreat, leftover_gggggggreat, leftover_ggggggreat, leftover_gggggreat, leftover_ggggreat, leftover_gggreat, leftover_ggreat, leftover_great, leftover_grand, leftover_parent, leftover_root, leftover_sup, 4300, leftover_fill, leftover_cri}, str(tmp_path),
    ) == leftover_gggggggggreat
    assert bdb.lock_listed_persist_port() == 9333
    assert bdb._this_jar_chromium_pid(str(tmp_path)) == 4240
    assert bdb.shared_chromium_owner_session() == "h_review"
    assert _singleton_lock_pid(str(tmp_path)) == 4240
    assert bdb.running_instance_cdp_port(str(tmp_path)) is None
    assert _remembered_dock_attach_port() == 9333
    assert (tmp_path / "dock-cdp-port").read_text(encoding="utf-8").strip() == "18888"
    assert _last_dock_cdp_port.get(hermes_home_key()) is None
    assert _cdp_url_is_bot_desktop_browser("http://[::1]:40141") is True
    assert _cdp_url_is_bot_desktop_browser("http://[::1]:18888") is True
    assert _cdp_url_is_bot_desktop_browser("http://[::1]:9333") is True
    assert _cdp_url_is_bot_desktop_browser("9222") is False
    assert _leftover_cdp_aims_at_dock("http://[::1]:40141", 9333) is True
    assert _leftover_cdp_aims_at_dock("http://[::1]:18888", 9333) is True
    assert _leftover_host_port_aims_at_dock("::1", 40141, 9333) is True
    assert _leftover_host_port_aims_at_dock("::1", 18888, 9333) is True
    assert _leftover_host_port_aims_at_dock("127.0.0.1", 40141, 9333) is False
    assert _unregistered_cli_aims_at_dock(
        ["npx", "lighthouse", "https://example.com", "--port", "40141"],
        {}, tmp_path, 9333,
    ) is True
    assert _unregistered_cli_aims_at_dock(
        ["npx", "chrome-remote-interface", "--port", "18888", "inspect"],
        {}, tmp_path, 9333,
    ) is True
    assert _unregistered_cli_aims_at_dock(
        ["npx", "lighthouse", "--port", "9222", "https://example.com"],
        {}, tmp_path, 9333,
    ) is False
    assert _remembered_dock_attach_port() == 9333


def test_stale_lock_pid_is_not_this_jar_chromium(monkeypatch, tmp_path):
    """Finding 157: a leftover pid on SingletonLock is not the dock.

    Recover already refuses a recycled lock pid whose cmdline does not
    name this jar. Take over used to skip the raw symlink target.
    A dead pid, a live pid that is not this Chromium, and another
    profile stay unknown. A live pid that still names this jar is.
    """
    import os

    import tools.bot_desktop.browser as bdb
    from tools.browser_tool_session import _singleton_lock_pid

    profile = tmp_path / "browser-profile"
    profile.mkdir()
    os.symlink("host-11221", profile / "SingletonLock")
    monkeypatch.setattr(runtime, "state_dir", lambda: tmp_path)
    monkeypatch.setattr(bdb, "profile_dir", lambda: profile)
    assert bdb._lock_pid(str(profile)) is None
    assert bdb._this_jar_chromium_pid(str(profile)) is None
    assert _singleton_lock_pid(str(profile)) is None

    os.unlink(profile / "SingletonLock")
    os.symlink("host-11225", profile / "SingletonLock")
    monkeypatch.setattr(bdb, "_pid_alive", lambda pid: pid == 11225)
    monkeypatch.setattr(
        bdb,
        "_listed_user_data_dir",
        lambda pid, tokens=None: str(profile) if pid == 11225 else None,
    )
    monkeypatch.setattr(bdb, "_proc_cwd", lambda pid: profile.parent)
    assert bdb._this_jar_chromium_pid(str(profile)) == 11225
    assert _singleton_lock_pid(str(profile)) == 11225

    monkeypatch.setattr(bdb, "_listed_user_data_dir", lambda pid, tokens=None: None)
    assert bdb._this_jar_chromium_pid(str(profile)) is None
    assert _singleton_lock_pid(str(profile)) is None


def test_devtools_file_recycled_lock_is_not_this_jar(monkeypatch, tmp_path):
    """Finding 158: stale DevTools + recycled lock pid is another Chrome.

    Unique-listen recover already required cmdline identity. The file
    port did not, so persist stamped a sibling listen and leftover
    ``--cdp`` to that sibling looked like the dock. 9222 stays unknown.
    Same-jar cmdline still identifies.
    """
    import os
    import socket

    import tools.bot_desktop.browser as bdb
    from tools.bot_desktop.lease import HumanHasControl
    from tools.browser_tool_session import (
        _admit_resolved_cdp_for_attach,
        _admit_shared_browser,
        _cdp_url_is_bot_desktop_browser,
        _last_dock_cdp_port,
    )

    profile = tmp_path / "browser-profile"
    profile.mkdir()
    listener = socket.socket()
    listener.bind(("127.0.0.1", 0))
    listener.listen(8)
    _drain_listen(listener)
    port = listener.getsockname()[1]
    (profile / "DevToolsActivePort").write_text(
        f"{port}\n/devtools/browser/abc\n", encoding="utf-8",
    )
    os.symlink(f"host-{os.getpid()}", profile / "SingletonLock")
    monkeypatch.setattr(runtime, "state_dir", lambda: tmp_path)
    monkeypatch.setattr(bdb, "profile_dir", lambda: profile)
    monkeypatch.setattr(bdb, "_configured_cdp_override_url", lambda: "")
    monkeypatch.setattr(
        bdb,
        "_chromium_cmdline_tokens",
        lambda pid: ["chrome", "--user-data-dir=/other/profile"],
    )
    dock = f"http://127.0.0.1:{port}"
    other = "http://127.0.0.1:9222"
    try:
        _last_dock_cdp_port.clear()
        assert bdb.running_instance_cdp_port(str(profile)) is None
        assert bdb.persist_live_dock_cdp_port() is None
        assert bdb.last_known_dock_cdp_port() is None
        assert _cdp_url_is_bot_desktop_browser(dock) is False
        assert _cdp_url_is_bot_desktop_browser(other) is False
        assert _admit_shared_browser(cdp_url=dock) is None
        assert _admit_resolved_cdp_for_attach(dock) is True

        monkeypatch.setattr(
            bdb,
            "_chromium_cmdline_tokens",
            lambda pid: ["chrome", f"--user-data-dir={profile}"],
        )
        _last_dock_cdp_port.clear()
        (tmp_path / "dock-cdp-port").unlink(missing_ok=True)
        assert bdb.running_instance_cdp_port(str(profile)) == port
        lease.acquire("human-viewer")
        with pytest.raises(HumanHasControl):
            _admit_shared_browser(cdp_url=dock)
        assert _admit_resolved_cdp_for_attach(dock) is False
        assert _admit_shared_browser(cdp_url=other) is None
    finally:
        listener.close()


def test_recycled_lock_family_does_not_hide_persisted_dock(monkeypatch, tmp_path):
    """Finding 159: recycled lock pid inverted finding 145.

    Family check used the raw SingletonLock pid. A live pid that does
    not name this jar and listens only on ``::1`` made leftover
    ``--cdp http://127.0.0.1:<persist>`` look like a sibling squat,
    so Take over left the writer. Dead / recycled lock stays
    port-only. Same-jar ``::1`` still rejects leftover IPv4.
    """
    import os

    import tools.bot_desktop.browser as bdb
    from tools.browser_tool_session import (
        _cdp_url_is_bot_desktop_browser,
        _leftover_cdp_host_matches_this_jar,
        _last_dock_cdp_port,
    )

    profile = tmp_path / "browser-profile"
    profile.mkdir()
    os.symlink(f"host-{os.getpid()}", profile / "SingletonLock")
    monkeypatch.setattr(runtime, "state_dir", lambda: tmp_path)
    monkeypatch.setattr(bdb, "profile_dir", lambda: profile)
    monkeypatch.setattr(bdb, "running_instance_cdp_port", lambda *a, **k: None)
    monkeypatch.setattr(bdb, "_configured_cdp_override_url", lambda: "")
    monkeypatch.setattr(
        bdb, "_loopback_listen_ports_for_pid", lambda pid: {9333},
    )
    monkeypatch.setattr(
        bdb, "_loopback_listen_targets_for_pid", lambda pid: {("::1", 9333)},
    )
    dock = "http://127.0.0.1:9333"
    v6 = "http://[::1]:9333"
    other = "http://127.0.0.1:9222"

    monkeypatch.setattr(
        bdb,
        "_chromium_cmdline_tokens",
        lambda pid: ["chrome", "--user-data-dir=/other/profile"],
    )
    _last_dock_cdp_port.clear()
    bdb.remember_dock_cdp_port(9333)
    assert bdb._this_jar_chromium_pid(str(profile)) is None
    assert _leftover_cdp_host_matches_this_jar(dock, 9333) is True
    assert _cdp_url_is_bot_desktop_browser(dock) is True
    assert _cdp_url_is_bot_desktop_browser(v6) is True
    assert _cdp_url_is_bot_desktop_browser(other) is False

    monkeypatch.setattr(
        bdb,
        "_chromium_cmdline_tokens",
        lambda pid: ["chrome", f"--user-data-dir={profile}"],
    )
    _last_dock_cdp_port.clear()
    (tmp_path / "dock-cdp-port").unlink(missing_ok=True)
    bdb.remember_dock_cdp_port(9333)
    assert bdb._this_jar_chromium_pid(str(profile)) == os.getpid()
    assert _leftover_cdp_host_matches_this_jar(dock, 9333) is False
    assert _cdp_url_is_bot_desktop_browser(dock) is False
    assert _cdp_url_is_bot_desktop_browser(v6) is True
    assert _cdp_url_is_bot_desktop_browser(other) is False


def test_materialized_lock_file_is_this_jar(monkeypatch, tmp_path):
    """Finding 160: leftover identity must see a materialized lock file.

    Persist / named-listen used ``readlink`` only. A regular-file
    ``host-pid`` left leftover ``--cdp`` looking like another Chrome,
    so Take over left the writer. Recycled file text stays unknown.
    9222 stays unknown. Same-jar cmdline still identifies.
    """
    import os
    import socket

    import tools.bot_desktop.browser as bdb
    from tools.bot_desktop.lease import HumanHasControl
    from tools.browser_tool_session import (
        _admit_resolved_cdp_for_attach,
        _admit_shared_browser,
        _cdp_url_is_bot_desktop_browser,
        _last_dock_cdp_port,
    )

    profile = tmp_path / "browser-profile"
    profile.mkdir()
    listener = socket.socket()
    listener.bind(("127.0.0.1", 0))
    listener.listen(8)
    _drain_listen(listener)
    port = listener.getsockname()[1]
    (profile / "DevToolsActivePort").write_text(
        f"{port}\n/devtools/browser/abc\n", encoding="utf-8",
    )
    (profile / "SingletonLock").write_text(f"host-{os.getpid()}\n", encoding="utf-8")
    monkeypatch.setattr(runtime, "state_dir", lambda: tmp_path)
    monkeypatch.setattr(bdb, "profile_dir", lambda: profile)
    monkeypatch.setattr(bdb, "_configured_cdp_override_url", lambda: "")
    monkeypatch.setattr(
        bdb,
        "_chromium_cmdline_tokens",
        lambda pid: ["chrome", "--user-data-dir=/other/profile"],
    )
    dock = f"http://127.0.0.1:{port}"
    other = "http://127.0.0.1:9222"
    try:
        _last_dock_cdp_port.clear()
        assert not (profile / "SingletonLock").is_symlink()
        assert bdb.running_instance_cdp_port(str(profile)) is None
        assert bdb.persist_live_dock_cdp_port() is None
        assert _cdp_url_is_bot_desktop_browser(dock) is False
        assert _cdp_url_is_bot_desktop_browser(other) is False
        assert _admit_shared_browser(cdp_url=dock) is None

        monkeypatch.setattr(
            bdb,
            "_chromium_cmdline_tokens",
            lambda pid: ["chrome", f"--user-data-dir={profile}"],
        )
        _last_dock_cdp_port.clear()
        (tmp_path / "dock-cdp-port").unlink(missing_ok=True)
        assert bdb._lock_pid(str(profile)) == os.getpid()
        assert bdb.running_instance_cdp_port(str(profile)) == port
        assert bdb.persist_live_dock_cdp_port() == port
        assert _cdp_url_is_bot_desktop_browser(dock) is True
        assert _cdp_url_is_bot_desktop_browser(other) is False
        lease.acquire("human-viewer")
        with pytest.raises(HumanHasControl):
            _admit_shared_browser(cdp_url=dock)
        assert _admit_resolved_cdp_for_attach(dock) is False
        assert _admit_shared_browser(cdp_url=other) is None
    finally:
        listener.close()


def test_missing_lock_named_listen_is_this_jar(monkeypatch, tmp_path):
    """Finding 161: leftover identity must see a missing SingletonLock.

    Persist / named-listen required the lock pid. Take over / overlay
    can unlink it while Chromium still holds DevTools, so leftover
    ``--cdp`` looked like another Chrome. Recycled cmdline stays
    unknown. 9222 stays unknown. Same-jar cmdline still identifies.
    """
    import os
    import socket

    import tools.bot_desktop.browser as bdb
    from tools.bot_desktop.lease import HumanHasControl
    from tools.browser_tool_session import (
        _admit_resolved_cdp_for_attach,
        _admit_shared_browser,
        _cdp_url_is_bot_desktop_browser,
        _last_dock_cdp_port,
    )

    profile = tmp_path / "browser-profile"
    profile.mkdir()
    listener = socket.socket()
    listener.bind(("127.0.0.1", 0))
    listener.listen(8)
    _drain_listen(listener)
    port = listener.getsockname()[1]
    (profile / "DevToolsActivePort").write_text(
        f"{port}\n/devtools/browser/abc\n", encoding="utf-8",
    )
    monkeypatch.setattr(runtime, "state_dir", lambda: tmp_path)
    monkeypatch.setattr(bdb, "profile_dir", lambda: profile)
    monkeypatch.setattr(bdb, "_configured_cdp_override_url", lambda: "")
    monkeypatch.setattr(
        bdb,
        "_chromium_cmdline_tokens",
        lambda pid: ["chrome", "--user-data-dir=/other/profile"],
    )
    dock = f"http://127.0.0.1:{port}"
    other = "http://127.0.0.1:9222"
    try:
        _last_dock_cdp_port.clear()
        assert not (profile / "SingletonLock").exists()
        assert bdb._lock_pid(str(profile)) is None
        assert bdb.running_instance_cdp_port(str(profile)) is None
        assert bdb.persist_live_dock_cdp_port() is None
        assert _cdp_url_is_bot_desktop_browser(dock) is False
        assert _cdp_url_is_bot_desktop_browser(other) is False
        assert _admit_shared_browser(cdp_url=dock) is None

        monkeypatch.setattr(
            bdb,
            "_chromium_cmdline_tokens",
            lambda pid: ["chrome", f"--user-data-dir={profile}"],
        )
        _last_dock_cdp_port.clear()
        (tmp_path / "dock-cdp-port").unlink(missing_ok=True)
        assert bdb.running_instance_cdp_port(str(profile)) == port
        assert bdb.persist_live_dock_cdp_port() == port
        assert _cdp_url_is_bot_desktop_browser(dock) is True
        assert _cdp_url_is_bot_desktop_browser(other) is False
        lease.acquire("human-viewer")
        with pytest.raises(HumanHasControl):
            _admit_shared_browser(cdp_url=dock)
        assert _admit_resolved_cdp_for_attach(dock) is False
        assert _admit_shared_browser(cdp_url=other) is None
    finally:
        listener.close()


def test_missing_lock_and_file_other_family_is_not_this_jar(monkeypatch, tmp_path):
    """Finding 162: leftover IPv4 must not skip family when files are gone.

    Named listen still works without SingletonLock / DevToolsActivePort.
    Family used ``_this_jar_chromium_pid``, which needs one of those
    files, so leftover ``--cdp http://127.0.0.1:<port>`` aimed at a
    sibling squat looked like a ``::1``-only dock. Persist still does
    not guess. 9222 stays unknown.
    """
    import socket

    import tools.bot_desktop.browser as bdb
    from tools.bot_desktop.lease import HumanHasControl
    from tools.browser_tool_session import (
        _admit_resolved_cdp_for_attach,
        _admit_shared_browser,
        _cdp_url_is_bot_desktop_browser,
        _leftover_cdp_host_matches_this_jar,
        _last_dock_cdp_port,
    )

    profile = tmp_path / "browser-profile"
    profile.mkdir()
    listener = socket.socket(socket.AF_INET6, socket.SOCK_STREAM)
    listener.setsockopt(socket.IPPROTO_IPV6, socket.IPV6_V6ONLY, 1)
    listener.bind(("::1", 0))
    listener.listen(8)
    _drain_listen(listener)
    port = listener.getsockname()[1]
    monkeypatch.setattr(runtime, "state_dir", lambda: tmp_path)
    monkeypatch.setattr(bdb, "profile_dir", lambda: profile)
    monkeypatch.setattr(bdb, "_configured_cdp_override_url", lambda: "")
    monkeypatch.setattr(
        bdb,
        "_chromium_cmdline_tokens",
        lambda pid: ["chrome", f"--user-data-dir={profile}"],
    )
    squat = f"http://127.0.0.1:{port}"
    dock = f"http://[::1]:{port}"
    other = "http://127.0.0.1:9222"
    try:
        _last_dock_cdp_port.clear()
        assert not (profile / "SingletonLock").exists()
        assert not (profile / "DevToolsActivePort").exists()
        assert bdb._this_jar_chromium_pid(str(profile)) is None
        assert bdb.persist_live_dock_cdp_port() is None
        assert bdb._this_jar_listens_on_port(port) is True
        assert _leftover_cdp_host_matches_this_jar(squat, port) is False
        assert _leftover_cdp_host_matches_this_jar(dock, port) is True
        assert _cdp_url_is_bot_desktop_browser(squat) is False
        assert _cdp_url_is_bot_desktop_browser(dock) is True
        assert _cdp_url_is_bot_desktop_browser(other) is False
        lease.acquire("human-viewer")
        with pytest.raises(HumanHasControl):
            _admit_shared_browser(cdp_url=dock)
        assert _admit_resolved_cdp_for_attach(dock) is False
        assert _admit_shared_browser(cdp_url=squat) is None
        assert _admit_shared_browser(cdp_url=other) is None
    finally:
        listener.close()


def test_several_this_jar_listen_holders_other_family_is_not_this_jar(monkeypatch, tmp_path):
    """Finding 163: leftover IPv4 must not skip family when holders are several.

    Unique-holder skip-kill stays unknown. Leftover ``--cdp`` already named
    the listen — union hosts still fence ``::1`` and reject a sibling
    squat. 9222 stays unknown. Without the port file persist still does
    not guess.
    """
    import tools.bot_desktop.browser as bdb
    from tools.bot_desktop.lease import HumanHasControl
    from tools.browser_tool_session import (
        _admit_resolved_cdp_for_attach,
        _admit_shared_browser,
        _cdp_url_is_bot_desktop_browser,
        _leftover_cdp_host_matches_this_jar,
        _last_dock_cdp_port,
    )

    profile = tmp_path / "browser-profile"
    profile.mkdir()
    monkeypatch.setattr(runtime, "state_dir", lambda: tmp_path)
    monkeypatch.setattr(bdb, "profile_dir", lambda: profile)
    monkeypatch.setattr(bdb, "_configured_cdp_override_url", lambda: "")
    monkeypatch.setattr(bdb, "_lock_pid", lambda d: None)
    monkeypatch.setattr(
        bdb,
        "_chromium_cmdline_tokens",
        lambda pid: ["chrome", f"--user-data-dir={profile}"],
    )
    monkeypatch.setattr(
        bdb,
        "_loopback_listen_inodes_for_port",
        lambda port: {7: {"::1"}} if port == 40141 else {},
    )
    monkeypatch.setattr(
        bdb,
        "_pids_holding_socket_inodes",
        lambda want: {4242: {7}, 4243: {7}} if 7 in want else {},
    )
    monkeypatch.setattr(
        bdb,
        "_cdp_port_reachable",
        lambda port, hosts: port == 40141 and "::1" in hosts,
    )
    squat = "http://127.0.0.1:40141"
    dock = "http://[::1]:40141"
    other = "http://127.0.0.1:9222"
    _last_dock_cdp_port.clear()
    assert bdb._scan_this_jar_listen_holder(40141, str(profile)) is None
    assert bdb.persist_live_dock_cdp_port() is None
    assert bdb._this_jar_chromium_pid(str(profile)) is None
    assert bdb._this_jar_listens_on_port(40141) is True
    assert _leftover_cdp_host_matches_this_jar(squat, 40141) is False
    assert _leftover_cdp_host_matches_this_jar(dock, 40141) is True
    assert _cdp_url_is_bot_desktop_browser(squat) is False
    assert _cdp_url_is_bot_desktop_browser(dock) is True
    assert _cdp_url_is_bot_desktop_browser(other) is False
    lease.acquire("human-viewer")
    with pytest.raises(HumanHasControl):
        _admit_shared_browser(cdp_url=dock)
    assert _admit_resolved_cdp_for_attach(dock) is False
    assert _admit_shared_browser(cdp_url=squat) is None
    assert _admit_shared_browser(cdp_url=other) is None


def test_file_named_port_persists_when_several_this_jar_holders(monkeypatch, tmp_path):
    """Finding 164: DevToolsActivePort plus several holders is this jar.

    Persist used to require a unique pid, so a file-named listen that
    several this-jar helpers inherited left ``dock-cdp-port`` empty.
    Skip-kill stays unknown. Leftover IPv4 to a ``::1``-only jar, 9222,
    and no file still do not guess.
    """
    import tools.bot_desktop.browser as bdb
    from tools.bot_desktop.lease import HumanHasControl
    from tools.browser_tool_session import (
        _admit_resolved_cdp_for_attach,
        _admit_shared_browser,
        _cdp_url_is_bot_desktop_browser,
        _leftover_cdp_host_matches_this_jar,
        _last_dock_cdp_port,
    )

    profile = tmp_path / "browser-profile"
    profile.mkdir()
    (profile / "DevToolsActivePort").write_text(
        "40141\n/devtools/browser/abc\n", encoding="utf-8",
    )
    monkeypatch.setattr(runtime, "state_dir", lambda: tmp_path)
    monkeypatch.setattr(bdb, "profile_dir", lambda: profile)
    monkeypatch.setattr(bdb, "_configured_cdp_override_url", lambda: "")
    monkeypatch.setattr(bdb, "_lock_pid", lambda d: None)
    monkeypatch.setattr(
        bdb,
        "_chromium_cmdline_tokens",
        lambda pid: ["chrome", f"--user-data-dir={profile}"],
    )
    monkeypatch.setattr(
        bdb,
        "_loopback_listen_inodes_for_port",
        lambda port: {7: {"::1"}} if port == 40141 else {},
    )
    monkeypatch.setattr(
        bdb,
        "_pids_holding_socket_inodes",
        lambda want: {4242: {7}, 4243: {7}} if 7 in want else {},
    )
    monkeypatch.setattr(
        bdb,
        "_cdp_port_reachable",
        lambda port, hosts: port == 40141 and "::1" in hosts,
    )
    squat = "http://127.0.0.1:40141"
    dock = "http://[::1]:40141"
    other = "http://127.0.0.1:9222"
    _last_dock_cdp_port.clear()
    assert bdb._scan_this_jar_listen_holder(40141, str(profile)) is None
    assert bdb.running_instance_cdp_port(str(profile)) == 40141
    assert bdb.persist_live_dock_cdp_port() == 40141
    assert bdb.last_known_dock_cdp_port() == 40141
    assert bdb._this_jar_chromium_pid(str(profile)) is None
    assert _leftover_cdp_host_matches_this_jar(squat, 40141) is False
    assert _leftover_cdp_host_matches_this_jar(dock, 40141) is True
    assert _cdp_url_is_bot_desktop_browser(squat) is False
    assert _cdp_url_is_bot_desktop_browser(dock) is True
    assert _cdp_url_is_bot_desktop_browser(other) is False
    lease.acquire("human-viewer")
    with pytest.raises(HumanHasControl):
        _admit_shared_browser(cdp_url=dock)
    assert _admit_resolved_cdp_for_attach(dock) is False
    assert _admit_shared_browser(cdp_url=squat) is None
    assert _admit_shared_browser(cdp_url=other) is None


def test_lock_pid_dead_family_leftover_identity_does_not_stamp_persist(monkeypatch, tmp_path):
    """Finding 166: leftover identity must see the lock pid's listed family.

    Finding 165 left persist empty when that family failed TCP, so inode
    holders on ``127.0.0.1:same`` were not stamped. Named-listen identity
    used the same probe, so leftover ``--cdp http://[::1]:<port>`` that
    already held the CDP socket looked like another Chrome and admit
    HTTP-probed the jar a human holds. Identify from inode hosts +
    family without stamping persist. Leftover IPv4 to the squat stays
    another Chrome. 9222 stays unknown.
    """
    import tools.bot_desktop.browser as bdb
    from tools.bot_desktop.lease import HumanHasControl
    from tools.browser_tool_session import (
        _admit_resolved_cdp_for_attach,
        _admit_shared_browser,
        _cdp_url_is_bot_desktop_browser,
        _leftover_cdp_host_matches_this_jar,
        _last_dock_cdp_port,
    )

    profile = tmp_path / "browser-profile"
    profile.mkdir()
    (profile / "DevToolsActivePort").write_text(
        "40141\n/devtools/browser/abc\n", encoding="utf-8",
    )
    monkeypatch.setattr(runtime, "state_dir", lambda: tmp_path)
    monkeypatch.setattr(bdb, "profile_dir", lambda: profile)
    monkeypatch.setattr(bdb, "_configured_cdp_override_url", lambda: "")
    monkeypatch.setattr(bdb, "_lock_pid", lambda d: 4240)
    monkeypatch.setattr(
        bdb,
        "_chromium_cmdline_tokens",
        lambda pid: ["chrome", f"--user-data-dir={profile}"],
    )
    monkeypatch.setattr(bdb, "_loopback_listen_ports_for_pid", lambda pid: {40141})
    monkeypatch.setattr(
        bdb, "_loopback_listen_targets_for_pid", lambda pid: {("::1", 40141)},
    )
    monkeypatch.setattr(bdb, "_recover_cdp_port_from_singleton", lambda *a, **k: None)
    monkeypatch.setattr(
        bdb,
        "_loopback_listen_inodes_for_port",
        lambda port: {7: {"127.0.0.1"}} if port == 40141 else {},
    )
    monkeypatch.setattr(
        bdb,
        "_pids_holding_socket_inodes",
        lambda want: {4242: {7}} if 7 in want else {},
    )
    monkeypatch.setattr(
        bdb,
        "_cdp_port_reachable",
        lambda port, hosts: port == 40141 and "127.0.0.1" in hosts,
    )
    squat = "http://127.0.0.1:40141"
    dock = "http://[::1]:40141"
    other = "http://127.0.0.1:9222"
    _last_dock_cdp_port.clear()
    assert bdb.running_instance_cdp_port(str(profile)) is None
    assert bdb.persist_live_dock_cdp_port() is None
    assert bdb.last_known_dock_cdp_port() is None
    assert bdb._this_jar_chromium_pid(str(profile)) == 4240
    assert bdb._this_jar_listen_connect_hosts(40141) == ("::1",)
    assert bdb._this_jar_listens_on_port(40141) is False
    assert _leftover_cdp_host_matches_this_jar(squat, 40141) is False
    assert _leftover_cdp_host_matches_this_jar(dock, 40141) is True
    assert _cdp_url_is_bot_desktop_browser(squat) is False
    assert _cdp_url_is_bot_desktop_browser(dock) is True
    assert _cdp_url_is_bot_desktop_browser(other) is False
    assert bdb.last_known_dock_cdp_port() is None
    lease.acquire("human-viewer")
    with pytest.raises(HumanHasControl):
        _admit_shared_browser(cdp_url=dock)
    assert _admit_resolved_cdp_for_attach(dock) is False
    assert _admit_shared_browser(cdp_url=squat) is None
    assert _admit_shared_browser(cdp_url=other) is None


def test_vault_ensure_does_not_probe_raw_dock_url_while_human_holds(monkeypatch):
    """Session admit can be a no-op while ``get cdp-url`` names the dock."""
    import tools.bot_desktop.browser as bdb
    from tools import browser_vault_tool as vault

    probed = []
    started = []
    monkeypatch.setattr(bdb, "running_instance_cdp_port", lambda *a, **k: 9333)
    lease.acquire("human-viewer")
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
    monkeypatch.setattr(
        "tools.browser_tool_cdp._resolve_cdp_override",
        lambda url: probed.append(url) or url,
    )
    import tools.browser_supervisor as bs
    registry = __import__("unittest.mock", fromlist=["MagicMock"]).MagicMock()
    registry.get.return_value = None
    registry.get_or_start.side_effect = lambda **k: started.append(k.get("cdp_url"))
    monkeypatch.setattr(bs, "SUPERVISOR_REGISTRY", registry)
    assert vault._ensure_supervisor("review") is None
    assert probed == []
    assert started == []


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


def _sibling_homes(tmp_path):
    launch = tmp_path / "launch"
    bot = tmp_path / "bot"
    launch.mkdir()
    bot.mkdir()
    return launch, bot


def test_cleanup_all_browsers_defers_owner_session_after_multiplex_turn(
    monkeypatch, tmp_path,
):
    """``cleanup_all_browsers`` used ambient ``human_holds()``. After a
    multiplex turn / finding 118's session wrap that is the launch
    lease — tree-kill of the Chromium a human holds on the bot.
    ``/browser connect`` and gateway shutdown walk every session.
    """
    from hermes_constants import reset_hermes_home_override, set_hermes_home_override
    from tools.bot_desktop.lease import _path
    from tools import browser_tool as bt
    from tools import browser_tool_lifecycle as life

    launch, bot = _sibling_homes(tmp_path)
    released: list = []
    saved = _session_state()[1]
    monkeypatch.setattr(
        life, "_release_session_resources", lambda *a, **k: released.append("release")
    )
    monkeypatch.setattr(
        "tools.browser_tool_session._run_browser_command",
        lambda *a, **k: released.append("close"),
    )
    token_bot = set_hermes_home_override(str(bot))
    try:
        for name in saved:
            getattr(bt, name).clear()
        bt._active_sessions["review"] = {
            "session_name": "h_review",
            "features": {"local": True},
            "bb_session_id": None,
        }
        bt._session_owner_homes["review"] = str(bot)
        lease.acquire("human-viewer")
    finally:
        reset_hermes_home_override(token_bot)

    token_launch = set_hermes_home_override(str(launch))
    try:
        assert lease.human_holds() is False
        life.cleanup_all_browsers()
        assert "review" in bt._active_sessions
        assert released == []
    finally:
        reset_hermes_home_override(token_launch)
        for home in (launch, bot):
            for f in (_path(str(home)), _path(str(home)).with_suffix(".lock")):
                try:
                    f.unlink()
                except OSError:
                    pass
        _restore_session_state(bt, saved)


def test_cleanup_all_browsers_does_not_defer_on_the_launch_profile_lease(
    monkeypatch, tmp_path,
):
    """A human on the launch bot must not keep a sibling session reserved."""
    from hermes_constants import reset_hermes_home_override, set_hermes_home_override
    from tools.bot_desktop.lease import _path
    from tools import browser_tool as bt
    from tools import browser_tool_lifecycle as life

    launch, bot = _sibling_homes(tmp_path)
    released: list = []
    saved = _session_state()[1]

    def _release(task_id, session_info):
        released.append("release")
        bt._active_sessions.pop(task_id, None)

    monkeypatch.setattr(life, "_release_session_resources", _release)
    monkeypatch.setattr(
        "tools.browser_tool_session._run_browser_command",
        lambda *a, **k: {"success": True},
    )
    token_launch = set_hermes_home_override(str(launch))
    try:
        lease.acquire("human-viewer")
    finally:
        reset_hermes_home_override(token_launch)

    token_bot = set_hermes_home_override(str(bot))
    try:
        for name in saved:
            getattr(bt, name).clear()
        bt._active_sessions["review"] = {
            "session_name": "h_review",
            "features": {"local": True},
            "bb_session_id": None,
        }
        bt._session_owner_homes["review"] = str(bot)
    finally:
        reset_hermes_home_override(token_bot)

    token_launch = set_hermes_home_override(str(launch))
    try:
        assert lease.human_holds() is True
        life.cleanup_all_browsers()
        assert "review" not in bt._active_sessions
        assert "release" in released
    finally:
        reset_hermes_home_override(token_launch)
        for home in (launch, bot):
            for f in (_path(str(home)), _path(str(home)).with_suffix(".lock")):
                try:
                    f.unlink()
                except OSError:
                    pass
        _restore_session_state(bt, saved)


def test_admit_task_shared_browser_uses_session_owner_lease_after_multiplex_turn(
    monkeypatch, tmp_path,
):
    """Finding 101 scoped ``_run_browser_command``. Vault / dialog /
    ``browser_cdp`` / vision leftover admit still read ambient launch
    ``lease.json`` (agent, missing file) and talked to the bot jar a
    human was typing into.
    """
    from hermes_constants import reset_hermes_home_override, set_hermes_home_override
    from tools.bot_desktop.lease import HumanHasControl, _path
    from tools import browser_tool as bt
    from tools.browser_tool_session import _admit_task_shared_browser

    launch, bot = _sibling_homes(tmp_path)
    saved = _session_state()[1]
    token_bot = set_hermes_home_override(str(bot))
    try:
        for name in saved:
            getattr(bt, name).clear()
        bt._active_sessions["review"] = {
            "session_name": "h_review", "features": {"local": True},
        }
        bt._session_owner_homes["review"] = str(bot)
        lease.acquire("human-viewer")
    finally:
        reset_hermes_home_override(token_bot)

    token_launch = set_hermes_home_override(str(launch))
    try:
        with pytest.raises(HumanHasControl):
            _admit_task_shared_browser("review")
    finally:
        reset_hermes_home_override(token_launch)
        for home in (launch, bot):
            for f in (_path(str(home)), _path(str(home)).with_suffix(".lock")):
                try:
                    f.unlink()
                except OSError:
                    pass
        _restore_session_state(bt, saved)


def test_admit_task_shared_browser_does_not_fence_on_the_launch_profile_lease(
    monkeypatch, tmp_path,
):
    """A human on the launch bot must not void a sibling leftover admit."""
    from hermes_constants import reset_hermes_home_override, set_hermes_home_override
    from tools.bot_desktop.lease import _path
    from tools import browser_tool as bt
    from tools.browser_tool_session import _admit_task_shared_browser

    launch, bot = _sibling_homes(tmp_path)
    saved = _session_state()[1]
    token_launch = set_hermes_home_override(str(launch))
    try:
        lease.acquire("human-viewer")
    finally:
        reset_hermes_home_override(token_launch)

    token_launch = set_hermes_home_override(str(launch))
    try:
        for name in saved:
            getattr(bt, name).clear()
        bt._active_sessions["review"] = {
            "session_name": "h_review", "features": {"local": True},
        }
        bt._session_owner_homes["review"] = str(bot)
        admitted = _admit_task_shared_browser("review")
        assert admitted is not None
        assert admitted.holder == lease.AGENT
    finally:
        reset_hermes_home_override(token_launch)
        for home in (launch, bot):
            for f in (_path(str(home)), _path(str(home)).with_suffix(".lock")):
                try:
                    f.unlink()
                except OSError:
                    pass
        _restore_session_state(bt, saved)


def test_run_browser_command_uses_session_owner_lease_after_multiplex_turn(monkeypatch, tmp_path):
    """After a multiplex turn the process home is the launch profile.

    Ambient ``assert_agent_may_act`` / ``get()`` then read launch's
    ``lease.json`` (agent, missing file) and clicked the bot jar a human
    was typing into. ``record stop`` from Desktop Take over is this path:
    ``_maybe_stop_recording`` does not wrap owner scope itself.
    """
    from hermes_constants import reset_hermes_home_override, set_hermes_home_override
    from tools.bot_desktop.lease import _path
    from tools import browser_tool as bt
    from tools import browser_tool_session as session

    launch, bot = _sibling_homes(tmp_path)
    commands: list = []
    saved = _session_state()[1]
    monkeypatch.setattr(session, "_browser_command_preflight", lambda: {"browser_cmd": "agent-browser"})
    monkeypatch.setattr(session._lifecycle, "_start_browser_cleanup_thread", lambda: None)
    monkeypatch.setattr(session._cloud, "_get_browser_engine", lambda: "chrome")
    monkeypatch.setattr(session._cloud, "_is_headed_mode", lambda: True)
    monkeypatch.setattr(session._cdp, "_ensure_cdp_supervisor", lambda *a, **k: None)
    monkeypatch.setattr(
        session, "_spawn_and_collect",
        lambda *a, **k: commands.append(a[2] if len(a) > 2 else "cmd") or {
            "success": True, "data": {"secret": "WHAT-THE-HUMAN-TYPED"},
        },
    )
    token_bot = set_hermes_home_override(str(bot))
    try:
        for name in saved:
            getattr(bt, name).clear()
        bt._active_sessions["review"] = {
            "session_name": "h_review", "features": {"local": True},
        }
        bt._session_owner_homes["review"] = str(bot)
        lease.acquire("human-viewer")
    finally:
        reset_hermes_home_override(token_bot)

    token_launch = set_hermes_home_override(str(launch))
    try:
        result = session._run_browser_command("review", "click", ["e1"])
        assert result.get("code") == "human_has_control"
        assert commands == []
        assert "WHAT-THE-HUMAN-TYPED" not in json.dumps(result)

        stopped = session._run_browser_command("review", "record", ["stop"])
        assert stopped.get("success") is True
        assert commands, "record stop must still reach the daemon after the multiplex turn"
    finally:
        reset_hermes_home_override(token_launch)
        for home in (launch, bot):
            for f in (_path(str(home)), _path(str(home)).with_suffix(".lock")):
                try:
                    f.unlink()
                except OSError:
                    pass
        _restore_session_state(bt, saved)


def test_run_browser_command_does_not_fence_on_the_launch_profile_lease(monkeypatch, tmp_path):
    """A human on the launch bot must not void a sibling session's click."""
    from hermes_constants import reset_hermes_home_override, set_hermes_home_override
    from tools.bot_desktop.lease import _path
    from tools import browser_tool as bt
    from tools import browser_tool_session as session

    launch, bot = _sibling_homes(tmp_path)
    commands: list = []
    saved = _session_state()[1]

    class _Healthy:
        def ensure_healthy(self):
            return True

    monkeypatch.setattr(session, "_browser_command_preflight", lambda: {"browser_cmd": "agent-browser"})
    monkeypatch.setattr(session._lifecycle, "_start_browser_cleanup_thread", lambda: None)
    monkeypatch.setattr(session._lifecycle, "_session_has_expired", lambda _s: False)
    monkeypatch.setattr(bt, "_browser_session_backend", lambda *_a, **_k: _Healthy())
    monkeypatch.setattr(session._cloud, "_get_browser_engine", lambda: "chrome")
    monkeypatch.setattr(session._cloud, "_is_headed_mode", lambda: True)
    monkeypatch.setattr(session._cdp, "_ensure_cdp_supervisor", lambda *a, **k: None)
    monkeypatch.setattr(
        session, "_spawn_and_collect",
        lambda *a, **k: commands.append("click") or {"success": True, "data": {"ok": True}},
    )
    token_launch = set_hermes_home_override(str(launch))
    try:
        lease.acquire("human-viewer")
    finally:
        reset_hermes_home_override(token_launch)

    token_launch = set_hermes_home_override(str(launch))
    try:
        for name in saved:
            getattr(bt, name).clear()
        bt._active_sessions["review"] = {
            "session_name": "h_review", "features": {"local": True},
        }
        bt._session_owner_homes["review"] = str(bot)
        result = session._run_browser_command("review", "click", ["e1"])
        assert result.get("success") is True
        assert commands == ["click"]
        assert result.get("code") != "human_has_control"
    finally:
        reset_hermes_home_override(token_launch)
        for home in (launch, bot):
            for f in (_path(str(home)), _path(str(home)).with_suffix(".lock")):
                try:
                    f.unlink()
                except OSError:
                    pass
        _restore_session_state(bt, saved)


def test_run_browser_command_epoch_discard_follows_the_home_that_admitted(monkeypatch, tmp_path):
    """Take over the owner home mid-command; ambient launch ``get()`` must not keep the frame."""
    from hermes_constants import reset_hermes_home_override, set_hermes_home_override
    from tools.bot_desktop.lease import HUMAN, Lease, _path, _write
    from tools import browser_tool as bt
    from tools import browser_tool_session as session

    launch, bot = _sibling_homes(tmp_path)
    saved = _session_state()[1]

    class _Healthy:
        def ensure_healthy(self):
            return True

    def spawn_then_takeover(*_a, **_k):
        _write(_path(str(bot)), Lease(holder=HUMAN, viewer_id="human-viewer", epoch=1))
        return {"success": True, "data": {"secret": "WHAT-THE-HUMAN-TYPED"}}

    monkeypatch.setattr(session, "_browser_command_preflight", lambda: {"browser_cmd": "agent-browser"})
    monkeypatch.setattr(session._lifecycle, "_start_browser_cleanup_thread", lambda: None)
    monkeypatch.setattr(session._lifecycle, "_session_has_expired", lambda _s: False)
    monkeypatch.setattr(bt, "_browser_session_backend", lambda *_a, **_k: _Healthy())
    monkeypatch.setattr(session._cloud, "_get_browser_engine", lambda: "chrome")
    monkeypatch.setattr(session._cloud, "_is_headed_mode", lambda: True)
    monkeypatch.setattr(session._cdp, "_ensure_cdp_supervisor", lambda *a, **k: None)
    monkeypatch.setattr(session, "_spawn_and_collect", spawn_then_takeover)

    token_launch = set_hermes_home_override(str(launch))
    try:
        for name in saved:
            getattr(bt, name).clear()
        bt._active_sessions["review"] = {
            "session_name": "h_review", "features": {"local": True},
        }
        bt._session_owner_homes["review"] = str(bot)
        result = session._run_browser_command("review", "click", ["e1"])
        assert result.get("code") == "human_has_control"
        assert "WHAT-THE-HUMAN-TYPED" not in json.dumps(result)
    finally:
        reset_hermes_home_override(token_launch)
        for home in (launch, bot):
            for f in (_path(str(home)), _path(str(home)).with_suffix(".lock")):
                try:
                    f.unlink()
                except OSError:
                    pass
        _restore_session_state(bt, saved)


def test_get_session_info_does_not_recycle_owner_session_after_multiplex_turn(monkeypatch, tmp_path):
    """Expired recycle used ambient ``human_holds()``. After the turn that is
    the launch lease — tree-kill of the Chromium a human holds on the bot."""
    from hermes_constants import reset_hermes_home_override, set_hermes_home_override
    from tools.bot_desktop.lease import _path
    from tools import browser_tool as bt
    from tools import browser_tool_session as session

    launch, bot = _sibling_homes(tmp_path)
    cleaned: list = []
    saved = _session_state()[1]
    monkeypatch.setattr(session._lifecycle, "_start_browser_cleanup_thread", lambda: None)
    monkeypatch.setattr(
        session._lifecycle, "_cleanup_single_browser_session",
        lambda task_id, **k: cleaned.append(task_id),
    )
    monkeypatch.setattr(session._lifecycle, "_session_has_expired", lambda _s: True)
    monkeypatch.setattr(
        session, "_create_session_for_key",
        lambda *a, **k: {"session_name": "fresh", "features": {"local": True}},
    )
    token_bot = set_hermes_home_override(str(bot))
    try:
        for name in saved:
            getattr(bt, name).clear()
        existing = {"session_name": "h_review", "features": {"local": True}, "session_key": "review"}
        bt._active_sessions["review"] = existing
        bt._session_owner_homes["review"] = str(bot)
        bt._suspect_browser_sessions["review"] = "timeout"
        lease.acquire("human-viewer")
    finally:
        reset_hermes_home_override(token_bot)

    token_launch = set_hermes_home_override(str(launch))
    try:
        got = session._get_session_info("review")
        assert got is existing
        assert cleaned == []
        assert bt._suspect_browser_sessions.get("review") == "timeout"
    finally:
        reset_hermes_home_override(token_launch)
        for home in (launch, bot):
            for f in (_path(str(home)), _path(str(home)).with_suffix(".lock")):
                try:
                    f.unlink()
                except OSError:
                    pass
        _restore_session_state(bt, saved)


def test_admit_resolved_cdp_for_attach_uses_session_owner_lease_after_multiplex_turn(
    monkeypatch, tmp_path,
):
    """Finding 114 scoped start-of-call leftover admit. Mid-resolve attach
    (``_admit_resolved_cdp_for_attach`` / ``browser_cdp`` resolved WS) still
    read ambient launch ``lease.json`` (agent, missing file) and leftover
    ``get_or_start`` / ``ws.send`` talked to the bot jar a human held.
    """
    import tools.bot_desktop.browser as bdb
    from hermes_constants import reset_hermes_home_override, set_hermes_home_override
    from tools.bot_desktop.lease import _path
    from tools import browser_tool as bt
    from tools.browser_cdp_tool import _admit_resolved_cdp_endpoint
    from tools.browser_tool_session import _admit_resolved_cdp_for_attach

    launch, bot = _sibling_homes(tmp_path)
    saved = _session_state()[1]
    monkeypatch.setattr(bdb, "running_instance_cdp_port", lambda *a, **k: 9333)
    dock = "http://127.0.0.1:9333"
    token_bot = set_hermes_home_override(str(bot))
    try:
        for name in saved:
            getattr(bt, name).clear()
        bt._session_owner_homes["review"] = str(bot)
        lease.acquire("human-viewer")
    finally:
        reset_hermes_home_override(token_bot)

    token_launch = set_hermes_home_override(str(launch))
    try:
        assert _admit_resolved_cdp_for_attach(dock, task_id="review") is False
        admitted, refused = _admit_resolved_cdp_endpoint(dock, task_id="review")
        assert admitted is None
        assert refused and "human_has_control" in refused
    finally:
        reset_hermes_home_override(token_launch)
        for home in (launch, bot):
            for f in (_path(str(home)), _path(str(home)).with_suffix(".lock")):
                try:
                    f.unlink()
                except OSError:
                    pass
        _restore_session_state(bt, saved)


def test_admit_resolved_cdp_for_attach_does_not_fence_on_the_launch_profile_lease(
    monkeypatch, tmp_path,
):
    """A human on the launch bot must not void a sibling resolved attach."""
    import tools.bot_desktop.browser as bdb
    from hermes_constants import reset_hermes_home_override, set_hermes_home_override
    from tools.bot_desktop.lease import _path
    from tools import browser_tool as bt
    from tools.browser_cdp_tool import _admit_resolved_cdp_endpoint
    from tools.browser_tool_session import _admit_resolved_cdp_for_attach

    launch, bot = _sibling_homes(tmp_path)
    saved = _session_state()[1]
    monkeypatch.setattr(bdb, "running_instance_cdp_port", lambda *a, **k: 9333)
    dock = "http://127.0.0.1:9333"
    token_launch = set_hermes_home_override(str(launch))
    try:
        lease.acquire("human-viewer")
    finally:
        reset_hermes_home_override(token_launch)

    token_launch = set_hermes_home_override(str(launch))
    try:
        for name in saved:
            getattr(bt, name).clear()
        bt._session_owner_homes["review"] = str(bot)
        assert _admit_resolved_cdp_for_attach(dock, task_id="review") is True
        admitted, refused = _admit_resolved_cdp_endpoint(dock, task_id="review")
        assert refused is None
        assert admitted is not None
        assert admitted.holder == lease.AGENT
    finally:
        reset_hermes_home_override(token_launch)
        for home in (launch, bot):
            for f in (_path(str(home)), _path(str(home)).with_suffix(".lock")):
                try:
                    f.unlink()
                except OSError:
                    pass
        _restore_session_state(bt, saved)


def test_resolve_cdp_override_does_not_http_probe_owner_dock_after_multiplex_turn(
    monkeypatch, tmp_path,
):
    """Finding 75 admits before HTTP. After multiplex that admit used launch
    ``lease.json``, so ``/json/version`` still observed the sibling jar.
    """
    import tools.bot_desktop.browser as bdb
    from hermes_constants import reset_hermes_home_override, set_hermes_home_override
    from tools.bot_desktop.lease import _path
    from tools import browser_tool as bt
    from tools.browser_tool_cdp import _resolve_cdp_override

    launch, bot = _sibling_homes(tmp_path)
    saved = _session_state()[1]
    probed = []

    class _Resp:
        def raise_for_status(self):
            return None

        def json(self):
            return {"webSocketDebuggerUrl": "ws://127.0.0.1:9333/devtools/browser/x"}

    monkeypatch.setattr("requests.get", lambda *a, **k: probed.append(a[0]) or _Resp())
    monkeypatch.setattr(bdb, "running_instance_cdp_port", lambda *a, **k: 9333)
    token_bot = set_hermes_home_override(str(bot))
    try:
        for name in saved:
            getattr(bt, name).clear()
        bt._session_owner_homes["review"] = str(bot)
        lease.acquire("human-viewer")
    finally:
        reset_hermes_home_override(token_bot)

    token_launch = set_hermes_home_override(str(launch))
    try:
        dock = "http://127.0.0.1:9333"
        assert _resolve_cdp_override(dock, home=str(bot)) == dock
        assert probed == []
        other = "http://127.0.0.1:9222"
        assert _resolve_cdp_override(other, home=str(bot)) == (
            "ws://127.0.0.1:9333/devtools/browser/x"
        )
        assert probed == ["http://127.0.0.1:9222/json/version"]
    finally:
        reset_hermes_home_override(token_launch)
        for home in (launch, bot):
            for f in (_path(str(home)), _path(str(home)).with_suffix(".lock")):
                try:
                    f.unlink()
                except OSError:
                    pass
        _restore_session_state(bt, saved)


def test_browser_exec_uses_session_owner_lease_after_multiplex_turn(monkeypatch, tmp_path):
    """Finding 115 scoped vault attach / discovery. ``browser_exec`` still
    admitted the dock with no home, stamped launch ``lease.json``, spawned
    leftover browser-use on the sibling jar, and registered the harness
    under the launch home so Take over of the owner never killed it.
    """
    import tools.bot_desktop.browser as bdb
    from hermes_constants import reset_hermes_home_override, set_hermes_home_override
    from tools.bot_desktop.lease import _path
    from tools import browser_tool as bt
    from tools import browser_use_cli as bu

    launch, bot = _sibling_homes(tmp_path)
    saved = _session_state()[1]
    spawned: list = []

    class _Done:
        returncode = 0
        stdout = "SECRET"
        stderr = ""

    monkeypatch.setattr(bdb, "running_instance_cdp_port", lambda *a, **k: 9333)
    monkeypatch.setattr(bu, "_find_cli", lambda: ["browser-use"])
    monkeypatch.setattr(bu, "_blocked_url_in_code", lambda code: None)
    monkeypatch.setattr(bu, "_route_backend", lambda env, *a, **k: env.__setitem__(
        "BU_CDP_WS", "ws://127.0.0.1:9333/devtools/browser/x") or None)
    monkeypatch.setattr(bu, "_attach_vault_supervisor", lambda *a, **k: None)
    monkeypatch.setattr(
        bu, "_run_cli_killing_process_group",
        lambda *a, **k: spawned.append("cli") or _Done(),
    )
    token_bot = set_hermes_home_override(str(bot))
    try:
        for name in saved:
            getattr(bt, name).clear()
        bt._session_owner_homes["review"] = str(bot)
        lease.acquire("human-viewer")
    finally:
        reset_hermes_home_override(token_bot)

    token_launch = set_hermes_home_override(str(launch))
    try:
        out = json.loads(bu.browser_exec("print(page_info())", task_id="review"))
        assert spawned == []
        assert out.get("code") == "human_has_control"
        assert "SECRET" not in json.dumps(out)
    finally:
        reset_hermes_home_override(token_launch)
        for home in (launch, bot):
            for f in (_path(str(home)), _path(str(home)).with_suffix(".lock")):
                try:
                    f.unlink()
                except OSError:
                    pass
        _restore_session_state(bt, saved)


def test_browser_exec_does_not_fence_on_the_launch_profile_lease(monkeypatch, tmp_path):
    """A human on the launch bot must not void a sibling browser_exec."""
    import tools.bot_desktop.browser as bdb
    from hermes_constants import reset_hermes_home_override, set_hermes_home_override
    from tools.bot_desktop.lease import _path
    from tools import browser_tool as bt
    from tools import browser_use_cli as bu

    launch, bot = _sibling_homes(tmp_path)
    saved = _session_state()[1]
    spawned: list = []

    class _Done:
        returncode = 0
        stdout = "ok"
        stderr = ""

    monkeypatch.setattr(bdb, "running_instance_cdp_port", lambda *a, **k: 9333)
    monkeypatch.setattr(bu, "_find_cli", lambda: ["browser-use"])
    monkeypatch.setattr(bu, "_blocked_url_in_code", lambda code: None)
    monkeypatch.setattr(bu, "_route_backend", lambda env, *a, **k: env.__setitem__(
        "BU_CDP_WS", "ws://127.0.0.1:9333/devtools/browser/x") or None)
    monkeypatch.setattr(bu, "_attach_vault_supervisor", lambda *a, **k: None)
    monkeypatch.setattr(
        bu, "_run_cli_killing_process_group",
        lambda *a, **k: spawned.append("cli") or _Done(),
    )
    token_launch = set_hermes_home_override(str(launch))
    try:
        lease.acquire("human-viewer")
    finally:
        reset_hermes_home_override(token_launch)

    token_launch = set_hermes_home_override(str(launch))
    try:
        for name in saved:
            getattr(bt, name).clear()
        bt._session_owner_homes["review"] = str(bot)
        out = json.loads(bu.browser_exec("print(page_info())", task_id="review"))
        assert spawned == ["cli"]
        assert out.get("success") is True
    finally:
        reset_hermes_home_override(token_launch)
        for home in (launch, bot):
            for f in (_path(str(home)), _path(str(home)).with_suffix(".lock")):
                try:
                    f.unlink()
                except OSError:
                    pass
        _restore_session_state(bt, saved)

