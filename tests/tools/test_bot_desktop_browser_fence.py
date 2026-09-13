"""The bot's browser tools obey the screen lease: while a human holds the shared browser, nothing is
dispatched, and a command whose run crossed a takeover loses its result."""

from __future__ import annotations

import json
import os

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


def test_chrome_fallback_is_fenced_while_human_controls_shared_browser(monkeypatch):
    """``_run_chrome_fallback_command`` pops temp Chrome outside ``_run_browser_command``.
    A human lease must refuse before that session is launched."""
    from tools import browser_tool_lightpanda_fallback as lp
    from tools import browser_tool_session as session

    spawned: list = []
    monkeypatch.setattr(session, "_get_session_info", lambda *a: {
        "session_name": "review", "cdp_url": None, "features": {"local": True}})
    monkeypatch.setattr(session, "_popen_agent_browser", lambda *a, **k: spawned.append(a) or (_ for _ in ()).throw(AssertionError("unfenced")))
    monkeypatch.setattr(session, "_run_browser_command", lambda *a, **k: spawned.append(("get-url",)) or {"success": True, "data": {"url": "https://example.com/"}})
    lease.acquire("human-viewer")
    result = lp._run_chrome_fallback_command("review", "screenshot", [], timeout=10)
    assert spawned == [], f"human holds the lease, yet chrome fallback ran: {spawned}"
    assert result.get("code") == "human_has_control"


def test_chrome_fallback_result_crossing_a_takeover_is_discarded(monkeypatch, tmp_path):
    """Takeover during the unfenced ``_run_tmp`` open/screenshot must drop the frame."""
    from tools import browser_tool_lightpanda_fallback as lp
    from tools import browser_tool_session as session

    monkeypatch.setattr(session, "_get_session_info", lambda *a: {
        "session_name": "review", "cdp_url": None, "features": {"local": True}})
    monkeypatch.setattr(session, "_run_browser_command", lambda *a, **k: {
        "success": True, "data": {"url": "https://example.com/"}})
    monkeypatch.setattr("tools.browser_tool._socket_safe_tmpdir", lambda: str(tmp_path))
    monkeypatch.setattr("tools.browser_tool_install._find_agent_browser", lambda: "/usr/bin/agent-browser")
    monkeypatch.setattr("tools.browser_tool_install._chromium_installed", lambda: True)

    class _Proc:
        def wait(self, timeout=None):
            return None

    def popen(_cmd, env, socket_dir, cmd):
        lease.acquire("human-viewer")
        lease.release("human-viewer")
        stdout = os.path.join(socket_dir, f"_stdout_{cmd}")
        with open(stdout, "w", encoding="utf-8") as fh:
            fh.write(json.dumps({"success": True, "data": {"path": str(tmp_path / "HUMAN_PRIVATE_FRAME.png"), "secret": "WHAT-THE-HUMAN-TYPED"}}))
        return _Proc()

    def prepare(name):
        dest = tmp_path / name
        dest.mkdir(exist_ok=True)
        return str(dest)

    monkeypatch.setattr(session, "_popen_agent_browser", popen)
    monkeypatch.setattr(session, "_prepare_session_socket_dir", prepare)
    result = lp._run_chrome_fallback_command("review", "screenshot", [], timeout=10)
    assert "WHAT-THE-HUMAN-TYPED" not in json.dumps(result)
    assert result.get("code") == "human_has_control"


def test_chrome_fallback_lookup_miss_fails_closed_while_human_holds(monkeypatch):
    """A broken session lookup must not unfence temp Chrome on a published DISPLAY."""
    from tools import browser_tool_lightpanda_fallback as lp
    from tools import browser_tool_session as session

    spawned: list = []
    monkeypatch.setattr(session, "_get_session_info", lambda *a, **k: (_ for _ in ()).throw(RuntimeError("no session")))
    monkeypatch.setattr(session, "_popen_agent_browser", lambda *a, **k: spawned.append(a) or (_ for _ in ()).throw(AssertionError("unfenced")))
    monkeypatch.setattr(session, "_run_browser_command", lambda *a, **k: spawned.append(("get-url",)) or {"success": True, "data": {"url": "https://example.com/"}})
    lease.acquire("human-viewer")
    result = lp._run_chrome_fallback_command("review", "screenshot", [], timeout=10)
    assert spawned == [], f"lookup failed open and chrome fallback ran: {spawned}"
    assert result.get("code") == "human_has_control"


def test_browser_vision_preroute_is_fenced_while_human_controls(monkeypatch, tmp_path):
    """Lightpanda vision preroute must not adopt a frame after the human takes over."""
    commands: list = []
    browser, session = _wire(monkeypatch, commands)
    monkeypatch.setattr(session._cloud, "_get_browser_engine", lambda: "lightpanda")
    monkeypatch.setattr(session._cloud, "_should_inject_engine", lambda *a, **k: True)

    shot = tmp_path / "HUMAN_PRIVATE_FRAME.png"
    shot.write_bytes(b"\x89PNGHUMAN_PRIVATE_FRAME")

    def preroute(_task, _annotate, screenshot_path):
        lease.acquire("human-viewer")
        lease.release("human-viewer")
        return True, "chrome fallback", shot

    monkeypatch.setattr("tools.browser_tool_vision._lightpanda_vision_preroute", preroute)
    raw = browser.browser_vision("what is on the page?", task_id="review")
    text = raw if isinstance(raw, str) else json.dumps(raw)
    parsed = json.loads(text)
    assert "HUMAN_PRIVATE_FRAME" not in text
    assert parsed.get("success") is False
    assert parsed.get("code") == "human_has_control"
