"""The bot's browser tools obey the screen lease: while a human holds the shared browser, nothing is
dispatched, and a command whose run crossed a takeover loses its result."""

from __future__ import annotations

import asyncio
import json
import os
import subprocess
import threading
from pathlib import Path
from unittest.mock import patch

import pytest

from tools import browser_cdp_tool
from tools import browser_tool_session as session_mod
from tools import browser_use_cli as bu_cli
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


def test_reminted_screenshot_is_unlinked_from_this_profile_home(monkeypatch):
    """computer_use fences before persist; browser screenshot writes the PNG
    first. A remint must unlink that frame so a later read cannot recover it."""
    from hermes_constants import get_hermes_home

    commands: list = []
    _browser, session = _wire(monkeypatch, commands)
    shots = get_hermes_home() / "cache" / "screenshots"
    shots.mkdir(parents=True, exist_ok=True)
    path = shots / "browser_screenshot_handoff.png"

    def spawn_then_takeover(*args):
        path.write_bytes(b"HUMAN_PRIVATE_FRAME")
        lease.acquire("human-viewer")
        lease.release("human-viewer")
        return {"success": True, "data": {"path": str(path), "secret": "WHAT-THE-HUMAN-TYPED"}}

    monkeypatch.setattr(session, "_spawn_and_collect", spawn_then_takeover)
    result = session._run_browser_command("review", "screenshot", ["--full", str(path)])
    assert result.get("code") == "human_has_control"
    assert "WHAT-THE-HUMAN-TYPED" not in json.dumps(result)
    assert not path.exists(), "human frame left on disk after remint"


def test_reminted_screenshot_outside_hermes_home_is_left_alone(monkeypatch, tmp_path):
    """Unlink is scoped to this profile home — do not delete a caller path elsewhere."""
    commands: list = []
    _browser, session = _wire(monkeypatch, commands)
    path = tmp_path / "user-capture.png"
    path.write_bytes(b"keep-me")

    def spawn_then_takeover(*args):
        lease.acquire("human-viewer")
        lease.release("human-viewer")
        return {"success": True, "data": {"path": str(path)}}

    monkeypatch.setattr(session, "_spawn_and_collect", spawn_then_takeover)
    result = session._run_browser_command("review", "screenshot", ["--full", str(path)])
    assert result.get("code") == "human_has_control"
    assert path.exists() and path.read_bytes() == b"keep-me"


def test_bracket_unlinks_a_reminted_adopted_screenshot(monkeypatch):
    from hermes_constants import get_hermes_home

    shots = get_hermes_home() / "cache" / "screenshots"
    shots.mkdir(parents=True, exist_ok=True)
    path = shots / "browser_screenshot_preroute.png"
    path.write_bytes(b"HUMAN_PRIVATE_FRAME")

    def adopt():
        lease.acquire("human-viewer")
        lease.release("human-viewer")
        return {"success": True, "data": {"path": str(path)}}

    result = session_mod._bracket_bot_desktop_browser({"features": {"local": True}}, adopt)
    assert result.get("code") == "human_has_control"
    assert not path.exists(), "adopted preroute frame left on disk after remint"


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


def test_lightpanda_does_not_retry_chrome_after_a_handoff_refuse(monkeypatch):
    """A reminted Lightpanda result is not an engine failure. Retrying Chrome
    would run another command on the shared desktop after the human's turn."""
    commands: list = []
    _browser, session = _wire(monkeypatch, commands)
    monkeypatch.setattr(session._cloud, "_get_browser_engine", lambda: "lightpanda")
    fallback: list = []
    monkeypatch.setattr(
        session._lp, "_run_chrome_fallback_command",
        lambda *a, **k: fallback.append(a) or {
            "success": True, "data": {"secret": "WHAT-THE-HUMAN-TYPED"}},
    )
    monkeypatch.setattr(
        session._lp, "_chrome_fallback_screenshot",
        lambda *a, **k: fallback.append(("screenshot",) + a) or {
            "success": True, "data": {"secret": "WHAT-THE-HUMAN-TYPED"}},
    )

    def spawn(*args):
        commands.append(args[2])
        return {
            "success": False, "code": "human_has_control",
            "error": "A human has control of this bot's screen.",
        }

    monkeypatch.setattr(session, "_spawn_and_collect", spawn)
    result = session._run_browser_command("review", "click", ["@e1"])
    assert fallback == [], f"handoff refuse retried Chrome: {fallback}; result={result}"
    assert result.get("code") == "human_has_control"
    assert "WHAT-THE-HUMAN-TYPED" not in json.dumps(result)


def test_chrome_fallback_url_probe_preserves_human_has_control(monkeypatch):
    """Independently-fenced get-url remints; do not rewrite as a missing-URL error."""
    from tools import browser_tool_lightpanda_fallback as lp
    from tools import browser_tool_session as session

    spawned: list = []
    monkeypatch.setattr(session, "_get_session_info", lambda *a: {
        "session_name": "review", "cdp_url": None, "features": {"local": True}})
    monkeypatch.setattr(
        session, "_popen_agent_browser",
        lambda *a, **k: spawned.append(a) or (_ for _ in ()).throw(AssertionError("unfenced")),
    )
    monkeypatch.setattr(session, "_run_browser_command", lambda *a, **k: {
        "success": False, "code": "human_has_control",
        "error": "A human has control of this bot's screen.",
    })
    result = lp._run_chrome_fallback_command("review", "screenshot", [], timeout=10)
    assert spawned == [], f"reminted get-url still launched temp Chrome: {spawned}"
    assert result.get("code") == "human_has_control"
    assert result.get("success") is not True
    assert "could not determine" not in (result.get("error") or "").lower()


def test_chrome_fallback_is_fenced_when_cached_session_is_cloud(monkeypatch):
    """Temp Chrome is this screen. A cached cloud row used to skip the bracket."""
    from tools import browser_tool as browser
    from tools import browser_tool_lightpanda_fallback as lp
    from tools import browser_tool_session as session

    spawned: list = []
    prior = browser._active_sessions.get("review")
    browser._active_sessions["review"] = {
        "session_name": "cloud", "cdp_url": "wss://cloud.example/devtools/browser/x",
        "features": {"local": False},
    }
    monkeypatch.setattr(session, "_popen_agent_browser", lambda *a, **k: spawned.append(a) or (_ for _ in ()).throw(AssertionError("unfenced")))
    monkeypatch.setattr(session, "_run_browser_command", lambda *a, **k: spawned.append(("get-url",)) or {"success": True, "data": {"url": "https://example.com/"}})
    lease.acquire("human-viewer")
    try:
        result = lp._run_chrome_fallback_command("review", "screenshot", [], timeout=10)
    finally:
        if prior is None:
            browser._active_sessions.pop("review", None)
        else:
            browser._active_sessions["review"] = prior
    assert spawned == [], f"cached cloud skipped the local Chrome-fallback fence: {spawned}"
    assert result.get("code") == "human_has_control"


def test_lightpanda_does_not_retry_chrome_after_cached_cloud_failure(monkeypatch):
    """A failed cloud command plus ``browser.engine: lightpanda`` used to launch temp Chrome."""
    from tools import browser_tool as browser

    fallback: list = []
    prior = browser._active_sessions.get("review")
    browser._active_sessions["review"] = {
        "session_name": "cloud", "cdp_url": "wss://cloud.example/devtools/browser/x",
        "features": {"local": False},
    }
    monkeypatch.setattr(session_mod, "_browser_command_preflight", lambda: {"browser_cmd": "agent-browser"})
    monkeypatch.setattr(session_mod._cloud, "_get_browser_engine", lambda: "lightpanda")
    monkeypatch.setattr(session_mod._cdp, "_get_cdp_override_raw", lambda: "")
    monkeypatch.setattr(
        session_mod._lp, "_run_chrome_fallback_command",
        lambda *a, **k: fallback.append(a) or {"success": True, "data": {"secret": "WHAT-THE-HUMAN-TYPED"}},
    )
    monkeypatch.setattr(
        session_mod._lp, "_chrome_fallback_screenshot",
        lambda *a, **k: fallback.append(("screenshot",) + a) or {
            "success": True, "data": {"secret": "WHAT-THE-HUMAN-TYPED"}},
    )
    monkeypatch.setattr(
        session_mod, "_spawn_and_collect",
        lambda *a, **k: {"success": False, "error": "cloud snapshot empty"},
    )
    lease.acquire("human-viewer")
    try:
        result = session_mod._run_browser_command("review", "snapshot", [])
    finally:
        if prior is None:
            browser._active_sessions.pop("review", None)
        else:
            browser._active_sessions["review"] = prior
    assert fallback == [], f"cached cloud failure retried Chrome on this screen: {fallback}; result={result}"
    assert result.get("success") is False
    assert "WHAT-THE-HUMAN-TYPED" not in json.dumps(result)


def _wire_browser_exec(monkeypatch, *, session_info=None, run_cli=None):
    """Admit/discard the harness without launching a real browser-use CLI."""
    ran: list = []
    info = session_info or {
        "session_name": "review", "cdp_url": None, "features": {"local": True}}
    monkeypatch.setattr(session_mod, "_get_session_info", lambda *a, **k: info)
    monkeypatch.setattr(bu_cli, "_find_cli", lambda: ["/usr/bin/browser-use"])
    monkeypatch.setattr(bu_cli, "_route_backend", lambda *a, **k: None)
    monkeypatch.setattr(bu_cli, "_attach_vault_supervisor", lambda *a, **k: None)

    def _default_run(*args, **kwargs):
        ran.append(args)
        return subprocess.CompletedProcess(["browser-use"], 0, "ok\n", "")

    monkeypatch.setattr(bu_cli, "_run_cli_killing_process_group", run_cli or _default_run)
    return bu_cli, ran


def test_browser_exec_is_fenced_while_human_controls_shared_browser(monkeypatch):
    """``get cdp-url`` is fenced, but the harness then talks CDP directly."""
    bu, ran = _wire_browser_exec(monkeypatch)
    lease.acquire("human-viewer")
    result = json.loads(bu.browser_exec("print(page_info())", task_id="review"))
    assert ran == [], f"human holds the lease, yet browser_exec launched the harness: {ran}"
    assert result.get("code") == "human_has_control"


def test_browser_exec_result_crossing_a_takeover_is_discarded(monkeypatch, tmp_path):
    """A screenshot captured after acquire→release must not reach the model."""
    shot = tmp_path / "HUMAN_PRIVATE_FRAME.png"
    shot.write_bytes(b"\x89PNGHUMAN_PRIVATE_FRAME")

    def run_then_takeover(*_a, **_k):
        lease.acquire("human-viewer")
        lease.release("human-viewer")
        return subprocess.CompletedProcess(["browser-use"], 0, f"{shot}\n", "")

    bu, _ = _wire_browser_exec(monkeypatch, run_cli=run_then_takeover)
    monkeypatch.setattr(
        "tools.vision_tools._should_use_native_vision_fast_path", lambda: True)
    monkeypatch.setattr(
        "tools.vision_tools._resize_image_for_vision",
        lambda p, **kw: "data:image/png;base64,HUMAN_PRIVATE_FRAME",
    )
    raw = bu.browser_exec("print(capture_screenshot())", task_id="review")
    text = raw if isinstance(raw, str) else json.dumps(raw)
    parsed = json.loads(text) if isinstance(raw, str) else raw
    assert "HUMAN_PRIVATE_FRAME" not in text
    assert parsed.get("code") == "human_has_control"
    assert parsed.get("success") is not True


def test_browser_exec_reminted_screenshot_is_unlinked_from_this_profile_home(monkeypatch):
    """The harness finds the PNG after the CLI exits; a remint must unlink it."""
    from hermes_constants import get_hermes_home

    shots = get_hermes_home() / "cache" / "screenshots"
    shots.mkdir(parents=True, exist_ok=True)
    shot = shots / "browser_screenshot_exec.png"

    def run_then_takeover(*_a, **_k):
        shot.write_bytes(b"\x89PNGHUMAN_PRIVATE_FRAME")
        lease.acquire("human-viewer")
        lease.release("human-viewer")
        return subprocess.CompletedProcess(["browser-use"], 0, f"{shot}\n", "")

    bu, _ = _wire_browser_exec(monkeypatch, run_cli=run_then_takeover)
    raw = bu.browser_exec("print(capture_screenshot())", task_id="review")
    parsed = json.loads(raw) if isinstance(raw, str) else raw
    assert parsed.get("code") == "human_has_control"
    assert not shot.exists(), "harness screenshot left on disk after remint"


def test_browser_exec_terminal_remint_after_screenshot_assembly(monkeypatch, tmp_path):
    """A takeover while the PNG is being attached must not deliver the frame."""
    shot = tmp_path / "HUMAN_PRIVATE_FRAME.png"
    shot.write_bytes(b"\x89PNGHUMAN_PRIVATE_FRAME")

    def run_ok(*_a, **_k):
        return subprocess.CompletedProcess(["browser-use"], 0, f"{shot}\n", "")

    def find_then_takeover(stdout, since):
        lease.acquire("human-viewer")
        lease.release("human-viewer")
        return str(shot)

    bu, _ = _wire_browser_exec(monkeypatch, run_cli=run_ok)
    monkeypatch.setattr(bu_cli, "_find_screenshot", find_then_takeover)
    monkeypatch.setattr(
        "tools.vision_tools._should_use_native_vision_fast_path", lambda: True)
    monkeypatch.setattr(
        "tools.vision_tools._resize_image_for_vision",
        lambda p, **kw: "data:image/png;base64,HUMAN_PRIVATE_FRAME",
    )
    raw = bu.browser_exec("print(capture_screenshot())", task_id="review")
    text = raw if isinstance(raw, str) else json.dumps(raw)
    parsed = json.loads(text) if isinstance(raw, str) else raw
    assert "HUMAN_PRIVATE_FRAME" not in text
    assert parsed.get("code") == "human_has_control"
    assert parsed.get("success") is not True


def test_browser_exec_terminal_remint_after_native_encode(monkeypatch, tmp_path):
    """Encode is the last observation step; remint there still unlinks the PNG."""
    from hermes_constants import get_hermes_home

    shots = get_hermes_home() / "cache" / "screenshots"
    shots.mkdir(parents=True, exist_ok=True)
    shot = shots / "browser_screenshot_exec_encode.png"
    shot.write_bytes(b"\x89PNGHUMAN_PRIVATE_FRAME")

    def run_ok(*_a, **_k):
        return subprocess.CompletedProcess(["browser-use"], 0, f"{shot}\n", "")

    def encode_then_takeover(result, path):
        lease.acquire("human-viewer")
        lease.release("human-viewer")
        return {
            "_multimodal": True,
            "text_summary": "HUMAN_PRIVATE_FRAME",
            "content": [{"type": "text", "text": "HUMAN_PRIVATE_FRAME"}],
        }

    bu, _ = _wire_browser_exec(monkeypatch, run_cli=run_ok)
    monkeypatch.setattr(bu_cli, "_native_screenshot_result", encode_then_takeover)
    raw = bu.browser_exec("print(capture_screenshot())", task_id="review")
    text = raw if isinstance(raw, str) else json.dumps(raw)
    parsed = json.loads(text) if isinstance(raw, str) else raw
    assert "HUMAN_PRIVATE_FRAME" not in text
    assert parsed.get("code") == "human_has_control"
    assert not shot.exists(), "encoded screenshot left on disk after terminal remint"


def test_browser_exec_timeout_remints_instead_of_generic_timeout(monkeypatch):
    """A takeover during a hung harness is wait_for_human, not a retryable timeout."""

    def hang(*_a, **_k):
        lease.acquire("human-viewer")
        lease.release("human-viewer")
        raise subprocess.TimeoutExpired(cmd=["browser-use"], timeout=1)

    bu, _ = _wire_browser_exec(monkeypatch, run_cli=hang)
    raw = bu.browser_exec("print(page_info())", task_id="review")
    parsed = json.loads(raw) if isinstance(raw, str) else raw
    assert parsed.get("code") == "human_has_control"
    assert "timed out" not in (parsed.get("error") or "").lower()


def test_browser_exec_lookup_miss_fails_closed_while_human_holds(monkeypatch):
    ran: list = []
    monkeypatch.setattr(
        session_mod, "_get_session_info",
        lambda *a, **k: (_ for _ in ()).throw(RuntimeError("no session")))
    monkeypatch.setattr(bu_cli, "_find_cli", lambda: ["/usr/bin/browser-use"])
    monkeypatch.setattr(bu_cli, "_route_backend", lambda *a, **k: ran.append("route") or None)
    monkeypatch.setattr(bu_cli, "_run_cli_killing_process_group", lambda *a, **k: ran.append("cli"))
    lease.acquire("human-viewer")
    result = json.loads(bu_cli.browser_exec("print(1)", task_id="review"))
    assert ran == [], f"lookup failed open and browser_exec ran: {ran}"
    assert result.get("code") == "human_has_control"


def test_browser_exec_without_screen_skips_session_lookup(monkeypatch):
    """No DISPLAY and no human lease: do not spawn a browser daemon just to fence."""
    monkeypatch.setattr(runtime, "published_env", lambda: {})
    bu, ran = _wire_browser_exec(monkeypatch)
    looked: list = []
    monkeypatch.setattr(
        session_mod, "_get_session_info",
        lambda *a, **k: looked.append(a) or {"features": {"local": True}})
    result = json.loads(bu.browser_exec("print(1)", task_id="review"))
    assert looked == [], f"fence lookup ran with no screen and no human: {looked}"
    assert ran
    assert result.get("success") is True


def test_managed_chromium_cdp_remint_is_not_a_startup_failure(monkeypatch):
    """Independently-fenced get cdp-url remints; do not rewrite as a launch error."""
    monkeypatch.setattr(session_mod, "_run_browser_command", lambda *a, **k: {
        "success": False, "code": "human_has_control",
        "error": "A human has control of this bot's screen.",
    })
    with pytest.raises(lease.HumanHasControl, match="human has control"):
        err = bu_cli._resolve_managed_chromium_cdp({}, "review")
        raise AssertionError(f"remint was rewritten as a startup error: {err}")


def test_browser_exec_readmits_when_unadmitted_route_lands_on_dock(monkeypatch):
    """Predicted-cloud skip + a successful dock CDP resolve left ``admitted=None``.
    The harness then talked CDP directly; a takeover during CLI had no ticket."""
    ran: list = []
    _predict_cloud(monkeypatch)
    monkeypatch.setattr(bu_cli, "_find_cli", lambda: ["/usr/bin/browser-use"])
    monkeypatch.setattr(bu_cli, "_attach_vault_supervisor", lambda *a, **k: None)

    def route(env, *_a, **_k):
        env["BU_CDP_WS"] = _DOCK_CDP
        return None

    monkeypatch.setattr(bu_cli, "_route_backend", route)
    monkeypatch.setattr("tools.bot_desktop.browser.cdp_url_is_running_instance", _is_dock_cdp)

    def run_then_takeover(*_a, **_k):
        ran.append("cli")
        lease.acquire("human-viewer")
        lease.release("human-viewer")
        return subprocess.CompletedProcess(["browser-use"], 0, "WHAT-THE-HUMAN-TYPED\n", "")

    monkeypatch.setattr(bu_cli, "_run_cli_killing_process_group", run_then_takeover)
    result = json.loads(bu_cli.browser_exec("print(1)", task_id="review"))
    assert "WHAT-THE-HUMAN-TYPED" not in json.dumps(result)
    assert result.get("code") == "human_has_control"
    assert result.get("success") is not True


def test_browser_exec_refuses_unadmitted_dock_resolve_while_human_holds(monkeypatch):
    """Human already holds: a dock resolve after a predicted-cloud skip must not run."""
    ran: list = []
    _predict_cloud(monkeypatch)
    monkeypatch.setattr(bu_cli, "_find_cli", lambda: ["/usr/bin/browser-use"])
    monkeypatch.setattr(bu_cli, "_attach_vault_supervisor", lambda *a, **k: None)

    def route(env, *_a, **_k):
        env["BU_CDP_WS"] = _DOCK_CDP
        return None

    monkeypatch.setattr(bu_cli, "_route_backend", route)
    monkeypatch.setattr("tools.bot_desktop.browser.cdp_url_is_running_instance", _is_dock_cdp)
    monkeypatch.setattr(
        bu_cli, "_run_cli_killing_process_group",
        lambda *a, **k: ran.append("cli") or subprocess.CompletedProcess(["browser-use"], 0, "ok\n", ""),
    )
    lease.acquire("human-viewer")
    result = json.loads(bu_cli.browser_exec("print(1)", task_id="review"))
    assert ran == [], f"human holds the lease, yet dock-resolved exec ran: {ran}"
    assert result.get("code") == "human_has_control"
    assert result.get("success") is not True


def test_browser_exec_readmits_local_session_resolved_after_cloud_skip(monkeypatch):
    """Predicted-cloud skip + a newly launched local Chromium (not the dock icon)
    still put a headed browser on this screen. CDP-identity-only re-admit missed it."""
    from tools import browser_tool as browser

    ran: list = []
    local_cdp = "ws://127.0.0.1:9333/devtools/browser/new-local"
    key = bu_cli._backend_cache_key("review", "")
    _predict_cloud(monkeypatch)
    monkeypatch.setattr(bu_cli, "_find_cli", lambda: ["/usr/bin/browser-use"])
    monkeypatch.setattr(bu_cli, "_attach_vault_supervisor", lambda *a, **k: None)

    def route(env, *_a, **_k):
        env["BU_CDP_WS"] = local_cdp
        browser._active_sessions[key] = {
            "session_name": "review",
            "cdp_url": local_cdp,
            "features": {"local": True},
        }
        return None

    monkeypatch.setattr(bu_cli, "_route_backend", route)
    monkeypatch.setattr("tools.bot_desktop.browser.cdp_url_is_running_instance", _is_dock_cdp)

    def run_then_takeover(*_a, **_k):
        ran.append("cli")
        lease.acquire("human-viewer")
        lease.release("human-viewer")
        return subprocess.CompletedProcess(["browser-use"], 0, "WHAT-THE-HUMAN-TYPED\n", "")

    monkeypatch.setattr(bu_cli, "_run_cli_killing_process_group", run_then_takeover)
    try:
        result = json.loads(bu_cli.browser_exec("print(1)", task_id="review"))
    finally:
        browser._active_sessions.pop(key, None)
    assert "WHAT-THE-HUMAN-TYPED" not in json.dumps(result)
    assert result.get("code") == "human_has_control"
    assert result.get("success") is not True


def test_browser_exec_refuses_local_session_resolved_after_cloud_skip_while_human_holds(monkeypatch):
    """Human already holds: a local Chromium resolve after a predicted-cloud skip must not run."""
    from tools import browser_tool as browser

    ran: list = []
    local_cdp = "ws://127.0.0.1:9333/devtools/browser/new-local"
    key = bu_cli._backend_cache_key("review", "")
    _predict_cloud(monkeypatch)
    monkeypatch.setattr(bu_cli, "_find_cli", lambda: ["/usr/bin/browser-use"])
    monkeypatch.setattr(bu_cli, "_attach_vault_supervisor", lambda *a, **k: None)

    def route(env, *_a, **_k):
        env["BU_CDP_WS"] = local_cdp
        browser._active_sessions[key] = {
            "session_name": "review",
            "cdp_url": local_cdp,
            "features": {"local": True},
        }
        return None

    monkeypatch.setattr(bu_cli, "_route_backend", route)
    monkeypatch.setattr("tools.bot_desktop.browser.cdp_url_is_running_instance", _is_dock_cdp)
    monkeypatch.setattr(
        bu_cli, "_run_cli_killing_process_group",
        lambda *a, **k: ran.append("cli") or subprocess.CompletedProcess(["browser-use"], 0, "ok\n", ""),
    )
    lease.acquire("human-viewer")
    try:
        result = json.loads(bu_cli.browser_exec("print(1)", task_id="review"))
    finally:
        browser._active_sessions.pop(key, None)
    assert ran == [], f"human holds the lease, yet local-resolved exec ran: {ran}"
    assert result.get("code") == "human_has_control"
    assert result.get("success") is not True


def test_browser_exec_does_not_overlay_dock_supervisor_on_cloud_route(monkeypatch):
    """A leftover dock supervisor must not fence a harness that routed to cloud."""
    from tools import browser_tool as browser

    ran: list = []
    cloud_cdp = "wss://cloud.example/devtools/browser/x"
    key = bu_cli._backend_cache_key("review", "")
    _predict_cloud(monkeypatch)
    monkeypatch.setattr(bu_cli, "_find_cli", lambda: ["/usr/bin/browser-use"])
    monkeypatch.setattr(bu_cli, "_attach_vault_supervisor", lambda *a, **k: None)

    def route(env, *_a, **_k):
        env["BU_CDP_WS"] = cloud_cdp
        browser._active_sessions[key] = {
            "session_name": "review",
            "cdp_url": cloud_cdp,
            "features": {"local": False},
        }
        # Supervisor appears only after route, the way a leftover dock attach would.
        # Re-calling ``_shared_browser_fence`` here would overlay it and refuse.
        _install_supervisor(monkeypatch, _DOCK_CDP, task_id="review")
        return None

    monkeypatch.setattr(bu_cli, "_route_backend", route)
    monkeypatch.setattr(
        bu_cli, "_run_cli_killing_process_group",
        lambda *a, **k: ran.append("cli") or subprocess.CompletedProcess(["browser-use"], 0, "ok\n", ""),
    )
    lease.acquire("human-viewer")
    try:
        result = json.loads(bu_cli.browser_exec("print(1)", task_id="review"))
    finally:
        browser._active_sessions.pop(key, None)
    assert ran, "cloud-routed exec was fenced by a leftover dock supervisor"
    assert result.get("success") is True


def test_browser_exec_readmits_surviving_real_profile_copy_after_cloud_skip(monkeypatch):
    """Cache-empty real-profile resolve still lands on the surviving copy CDP."""
    token = _without_in_process_real_profile()
    _live_real_profile_copy(monkeypatch)
    ran: list = []
    rp_cdp = "http://127.0.0.1:9334"
    _predict_cloud(monkeypatch)
    monkeypatch.setattr(bu_cli, "_find_cli", lambda: ["/usr/bin/browser-use"])
    monkeypatch.setattr(bu_cli, "_attach_vault_supervisor", lambda *a, **k: None)

    def route(env, *_a, **_k):
        env["BU_CDP_URL"] = rp_cdp
        return None

    monkeypatch.setattr(bu_cli, "_route_backend", route)

    def run_then_takeover(*_a, **_k):
        ran.append("cli")
        lease.acquire("human-viewer")
        lease.release("human-viewer")
        return subprocess.CompletedProcess(["browser-use"], 0, "WHAT-THE-HUMAN-TYPED\n", "")

    monkeypatch.setattr(bu_cli, "_run_cli_killing_process_group", run_then_takeover)
    try:
        result = json.loads(bu_cli.browser_exec("print(1)", task_id="review", local=True))
    finally:
        _restore_in_process_real_profile(token)
    assert "WHAT-THE-HUMAN-TYPED" not in json.dumps(result)
    assert result.get("code") == "human_has_control"
    assert result.get("success") is not True


def test_browser_exec_readmits_scheme_less_surviving_copy_after_cloud_skip(monkeypatch):
    """``/browser connect`` often exports scheme-less ``127.0.0.1:PORT`` as ``BU_CDP_WS``."""
    token = _without_in_process_real_profile()
    _live_real_profile_copy(monkeypatch)
    ran: list = []
    _predict_cloud(monkeypatch)
    monkeypatch.setattr(bu_cli, "_find_cli", lambda: ["/usr/bin/browser-use"])
    monkeypatch.setattr(bu_cli, "_attach_vault_supervisor", lambda *a, **k: None)

    def route(env, *_a, **_k):
        env["BU_CDP_WS"] = "127.0.0.1:9334"
        return None

    monkeypatch.setattr(bu_cli, "_route_backend", route)

    def run_then_takeover(*_a, **_k):
        ran.append("cli")
        lease.acquire("human-viewer")
        lease.release("human-viewer")
        return subprocess.CompletedProcess(["browser-use"], 0, "WHAT-THE-HUMAN-TYPED\n", "")

    monkeypatch.setattr(bu_cli, "_run_cli_killing_process_group", run_then_takeover)
    try:
        result = json.loads(bu_cli.browser_exec("print(1)", task_id="review", local=True))
    finally:
        _restore_in_process_real_profile(token)
    assert "WHAT-THE-HUMAN-TYPED" not in json.dumps(result)
    assert result.get("code") == "human_has_control"
    assert result.get("success") is not True


def test_browser_exec_readmits_real_profile_cache_hit_after_cloud_skip(monkeypatch):
    """``local=true`` real-profile cache hit never writes ``_active_sessions``.
    Predicted-cloud skip + that CDP left admitted=None; takeover during CLI leaked."""
    from tools import browser_tool as browser

    ran: list = []
    rp_cdp = "http://127.0.0.1:9334"
    _predict_cloud(monkeypatch)
    browser._real_profile_cdp_cache["cdp"] = rp_cdp
    monkeypatch.setattr(bu_cli, "_find_cli", lambda: ["/usr/bin/browser-use"])
    monkeypatch.setattr(bu_cli, "_attach_vault_supervisor", lambda *a, **k: None)

    def route(env, *_a, **_k):
        env["BU_CDP_URL"] = rp_cdp
        return None

    monkeypatch.setattr(bu_cli, "_route_backend", route)
    monkeypatch.setattr("tools.bot_desktop.browser.cdp_url_is_running_instance", _is_dock_cdp)

    def run_then_takeover(*_a, **_k):
        ran.append("cli")
        lease.acquire("human-viewer")
        lease.release("human-viewer")
        return subprocess.CompletedProcess(["browser-use"], 0, "WHAT-THE-HUMAN-TYPED\n", "")

    monkeypatch.setattr(bu_cli, "_run_cli_killing_process_group", run_then_takeover)
    try:
        result = json.loads(bu_cli.browser_exec("print(1)", task_id="review", local=True))
    finally:
        browser._real_profile_cdp_cache.pop("cdp", None)
    assert "WHAT-THE-HUMAN-TYPED" not in json.dumps(result)
    assert result.get("code") == "human_has_control"
    assert result.get("success") is not True


def test_browser_exec_stale_real_profile_cache_does_not_fence_cloud_route(monkeypatch):
    """A leftover real-profile CDP cache must not fence a harness that routed to cloud."""
    from tools import browser_tool as browser

    ran: list = []
    cloud_cdp = "wss://cloud.example/devtools/browser/x"
    _predict_cloud(monkeypatch)
    browser._real_profile_cdp_cache["cdp"] = "http://127.0.0.1:9334"
    monkeypatch.setattr(bu_cli, "_find_cli", lambda: ["/usr/bin/browser-use"])
    monkeypatch.setattr(bu_cli, "_attach_vault_supervisor", lambda *a, **k: None)

    def route(env, *_a, **_k):
        env["BU_CDP_WS"] = cloud_cdp
        return None

    monkeypatch.setattr(bu_cli, "_route_backend", route)
    monkeypatch.setattr(
        bu_cli, "_run_cli_killing_process_group",
        lambda *a, **k: ran.append("cli") or subprocess.CompletedProcess(["browser-use"], 0, "ok\n", ""),
    )
    lease.acquire("human-viewer")
    try:
        result = json.loads(bu_cli.browser_exec("print(1)", task_id="review"))
    finally:
        browser._real_profile_cdp_cache.pop("cdp", None)
    assert ran, "cloud-routed exec was fenced by a stale real-profile CDP cache"
    assert result.get("success") is True


def test_browser_exec_leftover_real_profile_session_does_not_fence_cloud_route(monkeypatch):
    """``hermes-real-profile`` is process-global; a prior attach must not fence cloud."""
    from tools import browser_tool as browser

    ran: list = []
    cloud_cdp = "wss://cloud.example/devtools/browser/x"
    key = browser._REAL_PROFILE_SESSION
    _predict_cloud(monkeypatch)
    browser._active_sessions[key] = {
        "session_name": key,
        "cdp_url": "http://127.0.0.1:9334",
        "features": {"local": True, "real_profile": True},
    }
    monkeypatch.setattr(bu_cli, "_find_cli", lambda: ["/usr/bin/browser-use"])
    monkeypatch.setattr(bu_cli, "_attach_vault_supervisor", lambda *a, **k: None)

    def route(env, *_a, **_k):
        env["BU_CDP_WS"] = cloud_cdp
        return None

    monkeypatch.setattr(bu_cli, "_route_backend", route)
    monkeypatch.setattr(
        bu_cli, "_run_cli_killing_process_group",
        lambda *a, **k: ran.append("cli") or subprocess.CompletedProcess(["browser-use"], 0, "ok\n", ""),
    )
    lease.acquire("human-viewer")
    try:
        result = json.loads(bu_cli.browser_exec("print(1)", task_id="review"))
    finally:
        browser._active_sessions.pop(key, None)
    assert ran, "cloud-routed exec was fenced by a leftover real-profile session"
    assert result.get("success") is True


def test_browser_exec_keeps_human_has_control_when_unadmitted_cdp_resolve_remints(monkeypatch):
    """Predicted-other-browser skip leaves admitted=None, so ``_lease_moved_error``
    cannot recover a reminted get. The resolve must raise, not stringify."""
    ran: list = []
    monkeypatch.setattr(session_mod, "_session_info_for_shared_browser_fence", lambda *_a, **_k: {})
    monkeypatch.setattr(bu_cli, "_find_cli", lambda: ["/usr/bin/browser-use"])
    monkeypatch.setattr(bu_cli, "_attach_vault_supervisor", lambda *a, **k: None)
    monkeypatch.setattr(bu_cli, "_resolve_real_profile_cdp", lambda *a, **k: None)
    monkeypatch.setattr(bu_cli, "_resolve_lightpanda_cdp", lambda *a, **k: None)
    monkeypatch.setattr(bu_cli, "_has_cdp_env", lambda _env: False)
    monkeypatch.setattr("tools.browser_tool_cdp._get_cdp_override", lambda: "")
    monkeypatch.setattr("tools.browser_tool_cloud._get_cloud_provider", lambda: None)
    monkeypatch.setattr(session_mod, "_run_browser_command", lambda *a, **k: {
        "success": False, "code": "human_has_control",
        "error": "A human has control of this bot's screen.",
    })
    monkeypatch.setattr(
        bu_cli, "_run_cli_killing_process_group",
        lambda *a, **k: ran.append(a) or subprocess.CompletedProcess(["browser-use"], 0, "ok\n", ""),
    )
    result = json.loads(bu_cli.browser_exec("print(1)", task_id="review"))
    assert ran == [], f"reminted CDP resolve still launched the harness: {ran}"
    assert result.get("code") == "human_has_control"
    assert result.get("success") is not True
    assert "could not be started" not in (result.get("error") or "").lower()


def test_browser_exec_cloud_session_is_not_fenced(monkeypatch):
    """Cloud / user CDP is another browser; a lease on this screen must not block it."""
    from tools import browser_tool as browser

    monkeypatch.setattr(runtime, "published_env", lambda: {})
    cloud_info = {
        "session_name": "cloud",
        "cdp_url": "wss://cloud.example/devtools/browser/x",
        "features": {"local": False},
    }
    bu, ran = _wire_browser_exec(monkeypatch, session_info=cloud_info)
    browser._active_sessions["cloud"] = cloud_info
    lease.acquire("human-viewer")
    try:
        result = json.loads(bu.browser_exec("print(1)", task_id="cloud"))
    finally:
        browser._active_sessions.pop("cloud", None)
    assert ran, "cloud browser_exec was fenced by a Bot Desktop lease"
    assert result.get("success") is True
    assert result.get("code") != "human_has_control"


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


def test_vision_preroute_remint_does_not_take_a_second_screenshot(monkeypatch, tmp_path):
    """A reminted Chrome-fallback preroute must not fail-open to another capture."""
    commands: list = []
    browser, session = _wire(monkeypatch, commands)
    monkeypatch.setattr(session._cloud, "_get_browser_engine", lambda: "lightpanda")
    monkeypatch.setattr(session._cloud, "_should_inject_engine", lambda *a, **k: True)
    monkeypatch.setattr(
        "tools.browser_tool_lightpanda_fallback._chrome_fallback_screenshot",
        lambda *a, **k: {
            "success": False, "code": "human_has_control",
            "error": "A human has control of this bot's screen.",
        },
    )
    second: list = []

    def run(*a, **k):
        second.append(a)
        return {
            "success": True,
            "data": {"path": str(tmp_path / "SECOND.png"), "secret": "WHAT-THE-HUMAN-TYPED"},
        }

    monkeypatch.setattr(session, "_run_browser_command", run)
    result = json.loads(browser.browser_vision("what is on the page?", task_id="review"))
    assert second == [], f"reminted preroute took a second screenshot: {second}"
    assert result.get("code") == "human_has_control"
    assert result.get("success") is not True
    assert "WHAT-THE-HUMAN-TYPED" not in json.dumps(result)


def test_vision_preroute_adopt_is_fenced_when_cached_session_is_cloud(monkeypatch):
    """The prerouted PNG is this screen; a cached cloud row used to skip the adopt bracket."""
    from hermes_constants import get_hermes_home
    from tools import browser_tool as browser

    shots = get_hermes_home() / "cache" / "screenshots"
    shots.mkdir(parents=True, exist_ok=True)
    shot = shots / "HUMAN_PRIVATE_FRAME.png"
    shot.write_bytes(b"\x89PNGHUMAN_PRIVATE_FRAME")
    prior = browser._active_sessions.get("review")
    browser._active_sessions["review"] = {
        "session_name": "cloud", "cdp_url": "wss://cloud.example/devtools/browser/x",
        "features": {"local": False},
    }
    monkeypatch.setattr(browser, "_is_camofox_mode", lambda: False)
    monkeypatch.setattr(browser, "_blocked_private_page_content", lambda *_a, **_k: None)
    monkeypatch.setattr(
        "tools.browser_tool_vision._lightpanda_vision_preroute",
        lambda *_a, **_k: (True, "chrome fallback", shot, None),
    )
    lease.acquire("human-viewer")
    try:
        raw = browser.browser_vision("what is on the page?", task_id="review")
    finally:
        if prior is None:
            browser._active_sessions.pop("review", None)
        else:
            browser._active_sessions["review"] = prior
    text = raw if isinstance(raw, str) else json.dumps(raw)
    parsed = json.loads(text)
    assert "HUMAN_PRIVATE_FRAME" not in text
    assert parsed.get("code") == "human_has_control"
    assert parsed.get("success") is not True
    assert not shot.exists(), "reminted adopt left the prerouted PNG on disk"


def test_vision_preroute_adopts_onto_dest_and_unlinks_fallback_original(monkeypatch):
    """Chrome fallback used to pick its own PNG; preroute copied it and left
    the original. Adopt onto the tool dest and drop the sibling."""
    from hermes_constants import get_hermes_home
    from tools import browser_tool_vision as vision

    shots = get_hermes_home() / "cache" / "screenshots"
    shots.mkdir(parents=True, exist_ok=True)
    dest = shots / "browser_screenshot_dest.png"
    leftover = shots / "browser_screenshot_fallback_orig.png"
    leftover.write_bytes(b"HUMAN_PRIVATE_FRAME")
    monkeypatch.setattr(vision._cloud, "_get_browser_engine", lambda: "lightpanda")
    monkeypatch.setattr(vision._cloud, "_should_inject_engine", lambda *a, **k: True)

    def fallback(_task, args, _timeout):
        assert str(dest) in args, f"fallback must write the tool dest, got {args}"
        return {"success": True, "data": {"path": str(leftover)}}

    monkeypatch.setattr(
        "tools.browser_tool_lightpanda_fallback._chrome_fallback_screenshot",
        fallback,
    )
    prerouted, _warning, path, remint = vision._lightpanda_vision_preroute(
        "review", False, dest,
    )
    assert prerouted is True
    assert remint is None
    assert path == dest
    assert dest.exists() and dest.read_bytes() == b"HUMAN_PRIVATE_FRAME"
    assert not leftover.exists(), "fallback original left beside the dest copy"


def test_vision_preroute_leaves_fallback_original_outside_profile_home(monkeypatch, tmp_path):
    """Unlink is scoped to this profile home — a caller path elsewhere stays."""
    from hermes_constants import get_hermes_home
    from tools import browser_tool_vision as vision

    shots = get_hermes_home() / "cache" / "screenshots"
    shots.mkdir(parents=True, exist_ok=True)
    dest = shots / "browser_screenshot_dest.png"
    outside = tmp_path / "user-capture.png"
    outside.write_bytes(b"keep-me")
    monkeypatch.setattr(vision._cloud, "_get_browser_engine", lambda: "lightpanda")
    monkeypatch.setattr(vision._cloud, "_should_inject_engine", lambda *a, **k: True)
    monkeypatch.setattr(
        "tools.browser_tool_lightpanda_fallback._chrome_fallback_screenshot",
        lambda *_a, **_k: {"success": True, "data": {"path": str(outside)}},
    )
    prerouted, _warning, path, remint = vision._lightpanda_vision_preroute(
        "review", False, dest,
    )
    assert prerouted is True and remint is None and path == dest
    assert dest.exists() and dest.read_bytes() == b"keep-me"
    assert outside.exists() and outside.read_bytes() == b"keep-me"


def test_chrome_fallback_remint_unlinks_screenshot_arg_without_data_path(monkeypatch):
    """Fallback screenshot writes the argv path; a remint must unlink it even
    when the JSON has no data.path (timeout / partial collect)."""
    from hermes_constants import get_hermes_home
    from tools import browser_tool_lightpanda_fallback as lp

    shots = get_hermes_home() / "cache" / "screenshots"
    shots.mkdir(parents=True, exist_ok=True)
    path = shots / "browser_screenshot_fallback.png"

    def run(*_a, **_k):
        path.write_bytes(b"HUMAN_PRIVATE_FRAME")
        lease.acquire("human-viewer")
        lease.release("human-viewer")
        return {"success": True}

    monkeypatch.setattr(lp, "_run_chrome_fallback_command_unfenced", run)
    result = lp._run_chrome_fallback_command(
        "review", "screenshot", ["--full", str(path)], 30,
    )
    assert result.get("code") == "human_has_control"
    assert "HUMAN_PRIVATE_FRAME" not in json.dumps(result)
    assert not path.exists(), "fallback argv PNG left on disk after remint"


def test_reminted_vision_after_preroute_unlinks_fallback_sibling(monkeypatch):
    """A completed preroute used to keep the fallback original; adopt remint
    must drop both the dest and that sibling."""
    from hermes_constants import get_hermes_home
    from tools import browser_tool as browser
    from tools import browser_tool_vision as vision

    shots = get_hermes_home() / "cache" / "screenshots"
    shots.mkdir(parents=True, exist_ok=True)
    leftover = shots / "browser_screenshot_fallback_orig.png"
    leftover.write_bytes(b"HUMAN_PRIVATE_FRAME")
    written: list[str] = []
    monkeypatch.setattr(browser, "_is_camofox_mode", lambda: False)
    monkeypatch.setattr(browser, "_blocked_private_page_content", lambda *_a, **_k: None)
    monkeypatch.setattr(vision._cloud, "_get_browser_engine", lambda: "lightpanda")
    monkeypatch.setattr(vision._cloud, "_should_inject_engine", lambda *a, **k: True)
    monkeypatch.setattr(vision, "_analyze_screenshot_with_aux_llm", lambda *_a, **_k: "analysis")
    monkeypatch.setattr(
        "tools.vision_tools._should_use_native_vision_fast_path",
        lambda: False,
    )

    def fallback(_task, args, _timeout):
        dest = next(a for a in args if str(a).endswith(".png"))
        Path(dest).write_bytes(b"HUMAN_PRIVATE_FRAME")
        written.append(dest)
        return {"success": True, "data": {"path": str(leftover)}}

    monkeypatch.setattr(
        "tools.browser_tool_lightpanda_fallback._chrome_fallback_screenshot",
        fallback,
    )

    def adopt_then_takeover(info, run, **_kw):
        result = run()
        lease.acquire("human-viewer")
        lease.release("human-viewer")
        return result

    monkeypatch.setattr(browser._session, "_bracket_bot_desktop_browser", adopt_then_takeover)
    raw = browser.browser_vision("what is on the page?", task_id="review")
    text = raw if isinstance(raw, str) else json.dumps(raw)
    parsed = json.loads(text)
    assert parsed.get("code") == "human_has_control"
    assert "HUMAN_PRIVATE_FRAME" not in text
    assert written, "fallback never wrote the tool dest"
    assert not Path(written[0]).exists(), "preroute dest left on disk after remint"
    assert not leftover.exists(), "fallback sibling left on disk after remint"


class _EvalSupervisor:
    def __init__(self, ran, result="WHAT-THE-HUMAN-TYPED"):
        self.ran = ran
        self.result = result

    def evaluate_runtime(self, expr):
        self.ran.append(expr)
        return {"ok": True, "result": self.result}


def test_browser_eval_supervisor_is_fenced_while_human_controls(monkeypatch):
    """Supervisor Runtime.evaluate talks CDP directly, bypassing ``_run_browser_command``."""
    commands: list = []
    browser, _ = _wire(monkeypatch, commands)
    ran: list = []
    monkeypatch.setattr(
        "tools.browser_supervisor.SUPERVISOR_REGISTRY",
        type("R", (), {"get": staticmethod(lambda _tid: _EvalSupervisor(ran))})(),
    )
    monkeypatch.setattr(browser._eval_policy, "_eval_ssrf_guard_active", lambda *_a: False)
    lease.acquire("human-viewer")
    result = json.loads(browser._browser_eval("document.body.innerText", task_id="review"))
    assert ran == [], f"human holds the lease, yet supervisor eval ran: {ran}"
    assert result.get("code") == "human_has_control"


def test_browser_eval_supervisor_result_crossing_a_takeover_is_discarded(monkeypatch):
    commands: list = []
    browser, _ = _wire(monkeypatch, commands)
    ran: list = []

    class _Takeover(_EvalSupervisor):
        def evaluate_runtime(self, expr):
            lease.acquire("human-viewer")
            lease.release("human-viewer")
            return super().evaluate_runtime(expr)

    monkeypatch.setattr(
        "tools.browser_supervisor.SUPERVISOR_REGISTRY",
        type("R", (), {"get": staticmethod(lambda _tid: _Takeover(ran))})(),
    )
    monkeypatch.setattr(browser._eval_policy, "_eval_ssrf_guard_active", lambda *_a: False)
    raw = browser._browser_eval("document.body.innerText", task_id="review")
    text = raw if isinstance(raw, str) else json.dumps(raw)
    parsed = json.loads(text)
    assert "WHAT-THE-HUMAN-TYPED" not in text
    assert parsed.get("code") == "human_has_control"


def test_vault_eval_preserves_human_has_control_code(monkeypatch):
    """CLI fallback used to strip ``code`` from a reminted refuse."""
    from tools import browser_vault_tool as vault
    from tools import browser_tool_session as session

    monkeypatch.setattr(
        "tools.browser_supervisor.SUPERVISOR_REGISTRY",
        type("R", (), {"get": staticmethod(lambda *_a: None)})(),
    )
    monkeypatch.setattr("tools.browser_tool._last_session_key", lambda key: key)
    monkeypatch.setattr(session, "_run_browser_command", lambda *_a, **_k: {
        "success": False, "code": "human_has_control",
        "error": "A human has control of this bot's screen.",
    })
    result = vault._eval_js("review", "window.location.href")
    assert result.get("code") == "human_has_control"
    assert result.get("success") is not True


def test_vault_save_login_does_not_wrap_a_reminted_fill_as_success(monkeypatch, tmp_path):
    """save_login stores the credential then calls fill. Fill is independently
    fenced and can remint; the outer success wrapper must not hide the handoff."""
    from agent.vault_backends import unlock as unlock_mod
    from agent.vault_store import VaultStore
    from tools import browser_vault_tool as vault

    store = VaultStore(base_dir=tmp_path / "vault")
    unlock_mod.set_save_login_prompt_callback(
        lambda origin, site: {"identifier": "tek@acme.test", "password": "hunter2"}
    )
    monkeypatch.setattr(vault, "_focus_bound_origin", lambda *_a, **_k: None)
    monkeypatch.setattr(vault, "_origin_probe", lambda *_a, **_k: ("https://acme.test", None))
    monkeypatch.setattr(vault, "browser_vault_fill", lambda *_a, **_k: json.dumps({
        "success": False, "code": "human_has_control",
        "error": "A human has control of this bot's screen.",
    }))
    try:
        with patch("agent.vault_store.get_vault_store", return_value=store), \
             patch("agent.vault_backends.unlock.can_prompt_here", return_value=True):
            out = json.loads(vault.browser_vault_save_login(task_id="review"))
    finally:
        unlock_mod.set_save_login_prompt_callback(None)
    assert out.get("success") is not True, f"reminted fill was wrapped as success: {out}"
    assert out.get("code") == "human_has_control"
    assert store.list_items(), "the login was saved; only the fill reminted"


def test_vault_fill_inspect_failure_preserves_human_has_control(monkeypatch, tmp_path):
    """Inspect failure wrappers must not rewrite a handoff as a generic inspect error."""
    from agent.vault_store import VaultStore
    from tools import browser_vault_tool as vault

    store = VaultStore(base_dir=tmp_path / "vault")
    meta = store.add_item(
        kind="login",
        label="Example login",
        origin="https://example.com",
        secret={
            "identifier_type": "email",
            "identifier": "user@example.com",
            "password": "s3cret-pw",
            "origin": "https://example.com",
        },
    )
    monkeypatch.setattr(session_mod, "_get_session_info", lambda *a, **k: {
        "session_name": "review", "cdp_url": None, "features": {"local": True}})
    monkeypatch.setattr("agent.vault_store.get_vault_store", lambda: store)
    monkeypatch.setattr(vault, "_focus_bound_origin", lambda *a, **k: "https://example.com")

    injected: list = []

    def fake_eval(_task, expression):
        return {"success": False, "code": "human_has_control",
                "error": "A human has control of this bot's screen."}

    def fake_secret(_task, expression, admitted=None):
        injected.append(expression)
        return {"success": True, "result": json.dumps({"filled": 1})}

    monkeypatch.setattr(vault, "_eval_js", fake_eval)
    monkeypatch.setattr(vault, "_eval_js_secret", fake_secret)
    result = json.loads(vault.browser_vault_fill(meta.id, task_id="review"))
    assert injected == [], f"password was injected after a handoff refuse: {injected}"
    assert result.get("code") == "human_has_control"
    assert result.get("success") is not True


def _vault_origin_remint(expression):
    """Independently-fenced eval remints without moving the tool-level ticket."""
    return {
        "success": False,
        "code": "human_has_control",
        "error": "A human has control of this bot's screen.",
    }


def test_vault_save_login_origin_probe_preserves_human_has_control(monkeypatch):
    """save_login used to rewrite a reminted origin eval as 'open the login page'."""
    from tools import browser_vault_tool as vault

    monkeypatch.setattr(session_mod, "_get_session_info", lambda *a, **k: {
        "session_name": "review", "cdp_url": None, "features": {"local": True}})
    monkeypatch.setattr(vault, "_focus_bound_origin", lambda *a, **k: None)
    monkeypatch.setattr(vault, "_eval_js", lambda *_a, **_k: _vault_origin_remint("href"))
    result = json.loads(vault.browser_vault_save_login(task_id="review"))
    assert result.get("code") == "human_has_control"
    assert result.get("success") is not True
    assert "login page" not in (result.get("error") or "").lower()


def test_vault_enter_code_origin_probe_preserves_human_has_control(monkeypatch):
    """enter_code used to rewrite a reminted origin eval as 'no page with a code field'."""
    from tools import browser_vault_tool as vault

    monkeypatch.setattr(session_mod, "_get_session_info", lambda *a, **k: {
        "session_name": "review", "cdp_url": None, "features": {"local": True}})
    monkeypatch.setattr(vault, "_focus_bound_origin", lambda *a, **k: None)
    monkeypatch.setattr(vault, "_eval_js", lambda *_a, **_k: _vault_origin_remint("href"))
    result = json.loads(vault.browser_vault_enter_code(task_id="review"))
    assert result.get("code") == "human_has_control"
    assert result.get("success") is not True
    assert "code field" not in (result.get("error") or "").lower()


def test_vault_enter_code_inspect_preserves_human_has_control(monkeypatch):
    """enter_code inspect used to rewrite a reminted eval as no_code_field."""
    from tools import browser_vault_tool as vault

    monkeypatch.setattr(session_mod, "_get_session_info", lambda *a, **k: {
        "session_name": "review", "cdp_url": None, "features": {"local": True}})
    monkeypatch.setattr(vault, "_focus_bound_origin", lambda *a, **k: None)

    def fake_eval(_task, expression):
        if "location.href" in expression:
            return {"success": True, "result": "https://example.com/2fa"}
        return _vault_origin_remint(expression)

    monkeypatch.setattr(vault, "_eval_js", fake_eval)
    result = json.loads(vault.browser_vault_enter_code(task_id="review"))
    assert result.get("code") == "human_has_control"
    assert result.get("success") is not True
    assert result.get("error_type") != "no_code_field"


def test_vault_fill_origin_probe_preserves_human_has_control(monkeypatch, tmp_path):
    """Fill used to treat a reminted origin eval as a missing-origin error."""
    from agent.vault_store import VaultStore
    from tools import browser_vault_tool as vault

    store = VaultStore(base_dir=tmp_path / "vault")
    meta = store.add_item(
        kind="login",
        label="Example login",
        origin="https://example.com",
        secret={
            "identifier_type": "email",
            "identifier": "user@example.com",
            "password": "s3cret-pw",
            "origin": "https://example.com",
        },
    )
    monkeypatch.setattr(session_mod, "_get_session_info", lambda *a, **k: {
        "session_name": "review", "cdp_url": None, "features": {"local": True}})
    monkeypatch.setattr("agent.vault_store.get_vault_store", lambda: store)
    monkeypatch.setattr(vault, "_focus_bound_origin", lambda *a, **k: None)
    injected: list = []
    monkeypatch.setattr(vault, "_eval_js", lambda *_a, **_k: _vault_origin_remint("href"))
    monkeypatch.setattr(
        vault, "_eval_js_secret",
        lambda *_a, **_k: injected.append("secret") or {"success": True, "result": "{}"},
    )
    result = json.loads(vault.browser_vault_fill(meta.id, task_id="review"))
    assert injected == [], f"password was injected after a handoff refuse: {injected}"
    assert result.get("code") == "human_has_control"
    assert result.get("success") is not True
    assert "could not determine" not in (result.get("error") or "").lower()


def test_vault_eval_and_save_login_are_fenced_while_human_controls(monkeypatch):
    from tools import browser_vault_tool as vault

    monkeypatch.setattr(session_mod, "_get_session_info", lambda *a, **k: {
        "session_name": "review", "cdp_url": None, "features": {"local": True}})
    ran: list = []
    monkeypatch.setattr(
        "tools.browser_supervisor.SUPERVISOR_REGISTRY",
        type("R", (), {"get": staticmethod(lambda _tid: _EvalSupervisor(ran))})(),
    )
    lease.acquire("human-viewer")
    eval_result = vault._eval_js("review", "window.location.href")
    assert ran == [], f"human holds the lease, yet vault eval ran: {ran}"
    assert eval_result.get("code") == "human_has_control"
    save = json.loads(vault.browser_vault_save_login(task_id="review"))
    assert ran == []
    assert save.get("code") == "human_has_control"


def test_browser_dialog_is_fenced_while_human_controls(monkeypatch):
    from tools import browser_dialog_tool as dialog

    monkeypatch.setattr(session_mod, "_get_session_info", lambda *a, **k: {
        "session_name": "review", "cdp_url": None, "features": {"local": True}})
    ran: list = []

    class _Sup:
        def respond_to_dialog(self, **kwargs):
            ran.append(kwargs)
            return {"ok": True, "dialog": {"message": "WHAT-THE-HUMAN-TYPED"}}

    monkeypatch.setattr(
        dialog, "SUPERVISOR_REGISTRY",
        type("R", (), {"get": staticmethod(lambda _tid: _Sup())})(),
    )
    lease.acquire("human-viewer")
    result = json.loads(dialog.browser_dialog("accept", task_id="review"))
    assert ran == [], f"human holds the lease, yet dialog ran: {ran}"
    assert result.get("code") == "human_has_control"


def test_browser_snapshot_is_fenced_while_human_controls(monkeypatch):
    commands: list = []
    browser, session = _wire(monkeypatch, commands)
    ran: list = []
    monkeypatch.setattr(session, "_run_browser_command", lambda *a, **k: ran.append(a) or {
        "success": True, "data": {"snapshot": "ok", "refs": {}}})
    lease.acquire("human-viewer")
    result = json.loads(browser.browser_snapshot(task_id="review"))
    assert ran == [], f"human holds the lease, yet snapshot ran: {ran}"
    assert result.get("code") == "human_has_control"


def test_snapshot_does_not_merge_dialogs_from_a_later_epoch(monkeypatch):
    """CLI tree + supervisor merge are one ownership epoch."""
    commands: list = []
    browser, session = _wire(monkeypatch, commands)
    ran: list = []

    def run_cmd(*_a, **_k):
        lease.acquire("human-viewer")
        lease.release("human-viewer")
        return {"success": True, "data": {"snapshot": "ok", "refs": {}}}

    monkeypatch.setattr(session, "_run_browser_command", run_cmd)
    monkeypatch.setattr(browser, "_blocked_private_page_content", lambda *_a: None)

    class _Sup:
        def snapshot(self):
            ran.append("merge")
            return type("S", (), {
                "active": True,
                "to_dict": staticmethod(lambda: {"pending_dialogs": [{"message": "WHAT-THE-HUMAN-TYPED"}]}),
            })()

    monkeypatch.setattr(
        "tools.browser_supervisor.SUPERVISOR_REGISTRY",
        type("R", (), {"get": staticmethod(lambda _tid: _Sup())})(),
    )
    raw = browser.browser_snapshot(task_id="review")
    text = raw if isinstance(raw, str) else json.dumps(raw)
    parsed = json.loads(text)
    assert ran == [], f"supervisor merge ran after a completed takeover: {ran}"
    assert "WHAT-THE-HUMAN-TYPED" not in text
    assert parsed.get("code") == "human_has_control"


def test_snapshot_supervisor_merge_is_fenced_while_human_controls(monkeypatch):
    """A successful CLI snapshot must not merge human dialog text after takeover."""
    commands: list = []
    browser, session = _wire(monkeypatch, commands)
    monkeypatch.setattr(session, "_run_browser_command", lambda *a, **k: {
        "success": True, "data": {"snapshot": "ok", "refs": {}}})
    monkeypatch.setattr(browser, "_blocked_private_page_content", lambda *_a: None)

    class _Snap:
        active = True

        def to_dict(self):
            return {"pending_dialogs": [{"message": "WHAT-THE-HUMAN-TYPED"}]}

    class _Sup:
        def snapshot(self):
            lease.acquire("human-viewer")
            lease.release("human-viewer")
            return _Snap()

    monkeypatch.setattr(
        "tools.browser_supervisor.SUPERVISOR_REGISTRY",
        type("R", (), {"get": staticmethod(lambda _tid: _Sup())})(),
    )
    raw = browser.browser_snapshot(task_id="review")
    text = raw if isinstance(raw, str) else json.dumps(raw)
    parsed = json.loads(text)
    assert "WHAT-THE-HUMAN-TYPED" not in text
    assert parsed.get("code") == "human_has_control"


def _oversized_snapshot_tree(marker: str = "HUMAN_PRIVATE_DOM") -> str:
    return marker + "\n" + ("line\n" * 80)


def _cache_web_snapshots():
    from hermes_constants import get_hermes_home

    cache = get_hermes_home() / "cache" / "web"
    return list(cache.glob("browser-snapshot-*.txt")) if cache.exists() else []


def test_reminted_oversized_snapshot_is_unlinked_from_this_profile_home(monkeypatch):
    """Oversized snapshots spill the full tree to cache/web before the remint
    check. A discarded takeover must not leave that file for later read_file."""
    commands: list = []
    browser, session = _wire(monkeypatch, commands)
    tree = _oversized_snapshot_tree()
    monkeypatch.setattr(browser, "get_browser_snapshot_threshold", lambda: 80)
    monkeypatch.setattr(session, "_run_browser_command", lambda *a, **k: {
        "success": True, "data": {"snapshot": tree, "refs": {"e1": {}}}})
    monkeypatch.setattr(browser, "_blocked_private_page_content", lambda *_a: None)

    class _Snap:
        active = True

        def to_dict(self):
            return {"pending_dialogs": []}

    class _Sup:
        def snapshot(self):
            lease.acquire("human-viewer")
            lease.release("human-viewer")
            return _Snap()

    monkeypatch.setattr(
        "tools.browser_supervisor.SUPERVISOR_REGISTRY",
        type("R", (), {"get": staticmethod(lambda _tid: _Sup())})(),
    )
    result = json.loads(browser.browser_snapshot(task_id="review"))
    assert result.get("code") == "human_has_control"
    assert "HUMAN_PRIVATE_DOM" not in json.dumps(result)
    leftovers = _cache_web_snapshots()
    assert leftovers == [], f"human snapshot left on disk after remint: {leftovers}"


def test_successful_oversized_snapshot_keeps_spill_and_hides_host_path(monkeypatch):
    """A completed agent snapshot still pages via read_file; the host path
    must not leak in the tool JSON."""
    commands: list = []
    browser, session = _wire(monkeypatch, commands)
    tree = _oversized_snapshot_tree("AGENT_TREE")
    monkeypatch.setattr(browser, "get_browser_snapshot_threshold", lambda: 80)
    monkeypatch.setattr(session, "_run_browser_command", lambda *a, **k: {
        "success": True, "data": {"snapshot": tree, "refs": {"e1": {}}}})
    monkeypatch.setattr(browser, "_blocked_private_page_content", lambda *_a: None)
    monkeypatch.setattr(
        "tools.browser_supervisor.SUPERVISOR_REGISTRY",
        type("R", (), {"get": staticmethod(lambda _tid: None)})(),
    )
    result = json.loads(browser.browser_snapshot(task_id="review"))
    assert result.get("success") is True
    assert "_stored_snapshot_paths" not in result
    assert "read_file" in result.get("snapshot", "")
    leftovers = _cache_web_snapshots()
    assert leftovers, "successful oversized snapshot must keep the spill for paging"


def test_reminted_snapshot_spill_outside_hermes_home_is_left_alone(tmp_path):
    """Unlink is scoped to this profile home — same class as screenshot PNGs."""
    path = tmp_path / "browser-snapshot-deadbeef01.txt"
    path.write_text("keep-me", encoding="utf-8")
    session_mod._discard_shared_browser_captures(
        result={"_stored_snapshot_paths": [str(path)]},
    )
    assert path.exists() and path.read_text(encoding="utf-8") == "keep-me"


def test_reminted_navigate_auto_snapshot_is_unlinked_from_this_profile_home(monkeypatch):
    """Navigate stores the compact snapshot after open. A remint after that
    spill must unlink it the same way browser_snapshot does."""
    commands: list = []
    browser, session = _wire(monkeypatch, commands)
    tree = _oversized_snapshot_tree()
    monkeypatch.setattr(browser, "get_browser_snapshot_threshold", lambda: 80)
    monkeypatch.setattr(session, "_get_session_info", lambda *a, **k: {
        "session_name": "review", "cdp_url": None, "features": {"local": True}, "_first_nav": False})

    def run_cmd(_task, command, args=None, **_k):
        if command == "open":
            return {"success": True, "data": {"url": "https://example.com/", "title": "Example"}}
        return {"success": True, "data": {"snapshot": tree, "refs": {"e1": {}}}}

    monkeypatch.setattr(session, "_run_browser_command", run_cmd)
    monkeypatch.setattr(browser, "_post_redirect_block", lambda *_a, **_k: None)
    orig = browser._snapshot_fields

    def fields_then_takeover(snap):
        out = orig(snap)
        lease.acquire("human-viewer")
        lease.release("human-viewer")
        return out

    monkeypatch.setattr(browser, "_snapshot_fields", fields_then_takeover)
    result = json.loads(browser.browser_navigate("https://example.com", task_id="review"))
    assert result.get("code") == "human_has_control"
    assert "HUMAN_PRIVATE_DOM" not in json.dumps(result)
    leftovers = _cache_web_snapshots()
    assert leftovers == [], f"navigate auto-snapshot left on disk after remint: {leftovers}"


def test_vault_fill_does_not_inject_after_inspect_crossed_a_takeover(monkeypatch, tmp_path):
    """Inspect + fill are one ownership epoch: a completed takeover must not write."""
    from agent.vault_store import VaultStore
    from tools import browser_vault_tool as vault

    store = VaultStore(base_dir=tmp_path / "vault")
    meta = store.add_item(
        kind="login",
        label="Example login",
        origin="https://example.com",
        secret={
            "identifier_type": "email",
            "identifier": "user@example.com",
            "password": "s3cret-pw",
            "origin": "https://example.com",
        },
    )
    monkeypatch.setattr(session_mod, "_get_session_info", lambda *a, **k: {
        "session_name": "review", "cdp_url": None, "features": {"local": True}})
    monkeypatch.setattr("agent.vault_store.get_vault_store", lambda: store)
    monkeypatch.setattr(vault, "_focus_bound_origin", lambda *a, **k: "https://example.com")

    injected: list = []
    controls = [
        {"autocomplete": "email", "formIndex": 0, "index": 0, "label": "", "name": "email", "type": "email"},
        {"autocomplete": "current-password", "formIndex": 0, "index": 1, "label": "", "name": "pw", "type": "password"},
    ]

    def fake_eval(_task, expression):
        if "location.href" in expression:
            return {"success": True, "result": "https://example.com/login"}
        lease.acquire("human-viewer")
        lease.release("human-viewer")
        return {"success": True, "result": json.dumps(controls)}

    def fake_secret(_task, expression, admitted=None):
        injected.append(expression)
        return {"success": True, "result": json.dumps({"filled": 1})}

    monkeypatch.setattr(vault, "_eval_js", fake_eval)
    monkeypatch.setattr(vault, "_eval_js_secret", fake_secret)
    result = json.loads(vault.browser_vault_fill(meta.id, task_id="review"))
    assert injected == [], f"password was injected after a completed takeover: {injected}"
    assert result.get("code") == "human_has_control"


def test_vault_fill_does_not_inject_after_classify_crossed_a_takeover(monkeypatch, tmp_path):
    """Inspect succeeding is not a new admit: takeover before the write must not remint."""
    from agent.vault_login_classifier import select_password_fill as real_select
    from agent.vault_store import VaultStore
    from tools import browser_vault_tool as vault

    store = VaultStore(base_dir=tmp_path / "vault")
    meta = store.add_item(
        kind="login",
        label="Example login",
        origin="https://example.com",
        secret={
            "identifier_type": "email",
            "identifier": "user@example.com",
            "password": "s3cret-pw",
            "origin": "https://example.com",
        },
    )
    monkeypatch.setattr(session_mod, "_get_session_info", lambda *a, **k: {
        "session_name": "review", "cdp_url": None, "features": {"local": True}})
    monkeypatch.setattr("agent.vault_store.get_vault_store", lambda: store)
    monkeypatch.setattr(vault, "_focus_bound_origin", lambda *a, **k: "https://example.com")

    injected: list = []
    controls = [
        {"autocomplete": "email", "formIndex": 0, "index": 0, "label": "", "name": "email", "type": "email"},
        {"autocomplete": "current-password", "formIndex": 0, "index": 1, "label": "", "name": "pw", "type": "password"},
    ]

    def fake_eval(_task, expression):
        if "location.href" in expression:
            return {"success": True, "result": "https://example.com/login"}
        return {"success": True, "result": json.dumps(controls)}

    def fake_secret(_task, expression, admitted=None):
        injected.append(expression)
        return {"success": True, "result": json.dumps({"filled": 1})}

    def select_then_takeover(*args, **kwargs):
        fills = real_select(*args, **kwargs)
        lease.acquire("human-viewer")
        lease.release("human-viewer")
        return fills

    monkeypatch.setattr(vault, "_eval_js", fake_eval)
    monkeypatch.setattr(vault, "_eval_js_secret", fake_secret)
    monkeypatch.setattr("agent.vault_login_classifier.select_password_fill", select_then_takeover)
    result = json.loads(vault.browser_vault_fill(meta.id, task_id="review"))
    assert injected == [], f"password was injected after a completed takeover: {injected}"
    assert result.get("code") == "human_has_control"


def test_vault_secret_eval_does_not_inject_after_attach_crossed_a_takeover(monkeypatch):
    """``_ensure_supervisor`` (get cdp-url) and the secret eval are one epoch."""
    from tools import browser_vault_tool as vault

    monkeypatch.setattr(session_mod, "_get_session_info", lambda *a, **k: {
        "session_name": "review", "cdp_url": None, "features": {"local": True}})
    ran: list = []

    class _Sup:
        def evaluate_runtime(self, expr):
            ran.append(expr)
            return {"ok": True, "result": "WHAT-THE-HUMAN-TYPED"}

    def ensure(_tid):
        lease.acquire("human-viewer")
        lease.release("human-viewer")
        return _Sup()

    monkeypatch.setattr(vault, "_ensure_supervisor", ensure)
    result = vault._eval_js_secret("review", "document.querySelector('input').value='s3cret-pw'")
    assert ran == [], f"password JS ran after a completed takeover: {ran}"
    assert result.get("code") == "human_has_control"
    assert "WHAT-THE-HUMAN-TYPED" not in json.dumps(result)


def test_vault_secret_eval_preserves_handoff_when_cdp_url_probe_remints(monkeypatch):
    """get cdp-url remints; do not rewrite that as supervisor_required."""
    from tools import browser_vault_tool as vault

    monkeypatch.setattr(session_mod, "_get_session_info", lambda *a, **k: {
        "session_name": "review", "cdp_url": None, "features": {"local": True}})
    monkeypatch.setattr(
        "tools.browser_supervisor.SUPERVISOR_REGISTRY",
        type("R", (), {"get": staticmethod(lambda *_a: None)})(),
    )
    monkeypatch.setattr(session_mod, "_run_browser_command", lambda *a, **k: {
        "success": False, "code": "human_has_control",
        "error": "A human has control of this bot's screen.",
    })
    result = vault._eval_js_secret("review", "document.querySelector('input').value='s3cret-pw'")
    assert result.get("code") == "human_has_control"
    assert result.get("success") is not True
    assert result.get("error_type") != "supervisor_required"


def test_vault_secret_eval_keeps_caller_epoch(monkeypatch):
    """A write hop must not remint; the caller's ticket is the ownership epoch."""
    from tools import browser_vault_tool as vault

    monkeypatch.setattr(session_mod, "_get_session_info", lambda *a, **k: {
        "session_name": "review", "cdp_url": None, "features": {"local": True}})
    admitted, refuse = session_mod._shared_browser_fence("review")
    assert refuse is None and admitted is not None
    lease.acquire("human-viewer")
    lease.release("human-viewer")
    ran: list = []

    class _Sup:
        def evaluate_runtime(self, expr):
            ran.append(expr)
            return {"ok": True, "result": "WHAT-THE-HUMAN-TYPED"}

    monkeypatch.setattr(vault, "_ensure_supervisor", lambda *_a, **_k: _Sup())
    result = vault._eval_js_secret(
        "review", "document.querySelector('input').value='s3cret-pw'", admitted=admitted)
    assert ran == [], f"password JS ran after the caller's epoch moved: {ran}"
    assert result.get("code") == "human_has_control"
    assert "WHAT-THE-HUMAN-TYPED" not in json.dumps(result)


def test_browser_console_does_not_return_errors_from_a_later_epoch(monkeypatch):
    """console + errors are one ownership epoch; a hand-back must not attach human-period errors."""
    commands: list = []
    browser, session = _wire(monkeypatch, commands)
    calls = {"n": 0}

    def run_cmd(_task, command, args=None, **_k):
        calls["n"] += 1
        if command == "console":
            lease.acquire("human-viewer")
            lease.release("human-viewer")
            return {"success": True, "data": {"messages": []}}
        return {"success": True, "data": {"errors": [{"message": "WHAT-THE-HUMAN-TYPED"}]}}

    monkeypatch.setattr(session, "_run_browser_command", run_cmd)
    monkeypatch.setattr(browser, "_blocked_private_page_content", lambda *_a: None)
    raw = browser.browser_console(task_id="review")
    text = raw if isinstance(raw, str) else json.dumps(raw)
    parsed = json.loads(text)
    assert "WHAT-THE-HUMAN-TYPED" not in text
    assert parsed.get("code") == "human_has_control"
    assert calls["n"] == 1, f"errors read ran after a completed takeover: {calls}"


def test_browser_console_terminal_remint_after_assembly(monkeypatch):
    """console+errors already finished; a remint while merging must not ship the log."""
    browser, session = _wire(monkeypatch, [])
    monkeypatch.setattr(session, "_run_browser_command", lambda *_a, **_k: {
        "success": True,
        "data": {
            "messages": [{"type": "log", "text": "WHAT-THE-HUMAN-TYPED"}],
            "errors": [],
        },
    })
    monkeypatch.setattr(browser, "_blocked_private_page_content", lambda *_a: None)

    def merge_then_takeover(response, _result):
        lease.acquire("human-viewer")
        lease.release("human-viewer")
        return response

    monkeypatch.setattr(browser._lp, "_copy_fallback_warning", merge_then_takeover)
    raw = browser.browser_console(task_id="review")
    text = raw if isinstance(raw, str) else json.dumps(raw)
    parsed = json.loads(text)
    assert "WHAT-THE-HUMAN-TYPED" not in text
    assert parsed.get("code") == "human_has_control"


def test_browser_navigate_is_fenced_while_human_controls(monkeypatch):
    commands: list = []
    browser, _ = _wire(monkeypatch, commands)
    lease.acquire("human-viewer")
    result = json.loads(browser.browser_navigate("https://example.com", task_id="review"))
    assert commands == [], f"human holds the lease, yet navigate dispatched: {commands}"
    assert result.get("code") == "human_has_control"


def test_browser_navigate_does_not_return_snapshot_from_a_later_epoch(monkeypatch):
    """open + auto-snapshot are one ownership epoch; a hand-back must not attach the human's DOM."""
    commands: list = []
    browser, session = _wire(monkeypatch, commands)
    monkeypatch.setattr(session, "_get_session_info", lambda *a, **k: {
        "session_name": "review", "cdp_url": None, "features": {"local": True}, "_first_nav": False})
    calls: list = []

    def run_cmd(_task, command, args=None, **_k):
        calls.append(command)
        if command == "open":
            lease.acquire("human-viewer")
            lease.release("human-viewer")
            return {"success": True, "data": {"url": "https://example.com/", "title": "Example"}}
        return {"success": True, "data": {"snapshot": "WHAT-THE-HUMAN-TYPED", "refs": {"e1": {}}}}

    monkeypatch.setattr(session, "_run_browser_command", run_cmd)
    raw = browser.browser_navigate("https://example.com", task_id="review")
    text = raw if isinstance(raw, str) else json.dumps(raw)
    parsed = json.loads(text)
    assert "WHAT-THE-HUMAN-TYPED" not in text
    assert parsed.get("code") == "human_has_control"
    assert "snapshot" not in calls, f"auto-snapshot ran after a completed takeover: {calls}"


def test_browser_navigate_discards_open_payload_when_epoch_moves_after_open(monkeypatch):
    """open can finish on the agent's ticket; url/title still belong to that epoch.

    The post-open discard used to return the success payload (skipping only
    auto-snapshot). A take-over / hand-back after open must refuse the same
    way click/console do, not report success.
    """
    browser, session = _wire(monkeypatch, [])
    monkeypatch.setattr(session, "_get_session_info", lambda *a, **k: {
        "session_name": "review", "cdp_url": None, "features": {"local": True}, "_first_nav": False})
    monkeypatch.setattr(session, "_run_browser_command", lambda *_a, **_k: {
        "success": True, "data": {"url": "https://example.com/", "title": "Example"}})

    def steal_after_open(*_a, **_k):
        lease.acquire("human-viewer")
        lease.release("human-viewer")

    monkeypatch.setattr(browser, "_add_navigate_warnings", steal_after_open)
    monkeypatch.setattr(browser, "_last_active_session_key", {})
    result = json.loads(browser.browser_navigate("https://example.com", task_id="review"))
    assert result.get("code") == "human_has_control"
    assert result.get("success") is not True
    assert "review" not in browser._last_active_session_key


def test_browser_navigate_discards_open_payload_when_epoch_moves_after_redirect_check(monkeypatch):
    """The blank-on-SSRF window is the same epoch as open — not a new success."""
    browser, session = _wire(monkeypatch, [])
    monkeypatch.setattr(session, "_get_session_info", lambda *a, **k: {
        "session_name": "review", "cdp_url": None, "features": {"local": True}, "_first_nav": False})
    monkeypatch.setattr(session, "_run_browser_command", lambda *_a, **_k: {
        "success": True, "data": {"url": "https://example.com/", "title": "Example"}})

    def steal_after_redirect(*_a, **_k):
        lease.acquire("human-viewer")
        lease.release("human-viewer")
        return None

    monkeypatch.setattr(browser, "_post_redirect_block", steal_after_redirect)
    result = json.loads(browser.browser_navigate("https://example.com", task_id="review"))
    assert result.get("code") == "human_has_control"
    assert result.get("success") is not True


def test_browser_navigate_discards_ssrf_block_when_epoch_moves_during_blank(monkeypatch):
    """about:blank on a blocked redirect is still the open's epoch.

    The blank-on-SSRF window used to return ``Blocked: redirect landed on…``
    even after a completed take-over / hand-back, and it retargeted follow-ups
    onto that session. Same class as returning url/title after open.
    """
    browser, session = _wire(monkeypatch, [])
    monkeypatch.setattr(session, "_get_session_info", lambda *a, **k: {
        "session_name": "review", "cdp_url": None, "features": {"local": True}, "_first_nav": False})
    monkeypatch.setattr(browser, "_last_active_session_key", {})

    def run_cmd(_task, command, args=None, **_k):
        if command == "open" and list(args or []) == ["about:blank"]:
            lease.acquire("human-viewer")
            lease.release("human-viewer")
            return {"success": True, "data": {"url": "about:blank", "title": ""}}
        return {"success": True, "data": {"url": "http://169.254.169.254/", "title": "IMDS"}}

    monkeypatch.setattr(session, "_run_browser_command", run_cmd)
    result = json.loads(browser.browser_navigate("https://example.com", task_id="review"))
    assert result.get("code") == "human_has_control"
    assert result.get("success") is not True
    assert "redirect landed" not in (result.get("error") or "")
    assert "review" not in browser._last_active_session_key


def test_browser_navigate_propagates_human_has_control_from_ssrf_blank(monkeypatch):
    """If about:blank itself was refused, do not rewrite it as an SSRF block."""
    browser, session = _wire(monkeypatch, [])
    monkeypatch.setattr(session, "_get_session_info", lambda *a, **k: {
        "session_name": "review", "cdp_url": None, "features": {"local": True}, "_first_nav": False})
    monkeypatch.setattr(browser, "_last_active_session_key", {})

    def run_cmd(_task, command, args=None, **_k):
        if command == "open" and list(args or []) == ["about:blank"]:
            return {"success": False, "code": "human_has_control",
                    "error": "A human has control of this bot's screen."}
        return {"success": True, "data": {"url": "http://169.254.169.254/", "title": "IMDS"}}

    monkeypatch.setattr(session, "_run_browser_command", run_cmd)
    result = json.loads(browser.browser_navigate("https://example.com", task_id="review"))
    assert result.get("code") == "human_has_control"
    assert result.get("success") is not True
    assert "redirect landed" not in (result.get("error") or "")
    assert "review" not in browser._last_active_session_key


def test_browser_navigate_does_not_retarget_on_blocked_metadata_redirect(monkeypatch):
    """A blocked redirect is not a successful navigation — do not own the task."""
    browser, session = _wire(monkeypatch, [])
    monkeypatch.setattr(session, "_get_session_info", lambda *a, **k: {
        "session_name": "review", "cdp_url": None, "features": {"local": True}, "_first_nav": False})
    monkeypatch.setattr(browser, "_last_active_session_key", {})

    def run_cmd(_task, command, args=None, **_k):
        if command == "open" and list(args or []) == ["about:blank"]:
            return {"success": True, "data": {"url": "about:blank", "title": ""}}
        return {"success": True, "data": {"url": "http://169.254.169.254/", "title": "IMDS"}}

    monkeypatch.setattr(session, "_run_browser_command", run_cmd)
    result = json.loads(browser.browser_navigate("https://example.com", task_id="review"))
    assert result.get("success") is False
    assert "redirect landed on a cloud metadata endpoint" in (result.get("error") or "")
    assert "review" not in browser._last_active_session_key


def test_browser_get_images_discards_payload_when_epoch_moves_after_eval(monkeypatch):
    """The SSRF recheck remints. Images from the earlier eval must not ship after hand-back."""
    browser, session = _wire(monkeypatch, [])
    monkeypatch.setattr(session, "_run_browser_command", lambda *_a, **_k: {
        "success": True,
        "data": {"result": json.dumps([
            {"src": "https://human.example/secret.png", "alt": "WHAT-THE-HUMAN-TYPED"},
        ])},
    })

    def steal(*_a, **_k):
        lease.acquire("human-viewer")
        lease.release("human-viewer")
        return None

    monkeypatch.setattr(browser, "_blocked_private_page_content", steal)
    result = json.loads(browser.browser_get_images(task_id="review"))
    assert result.get("code") == "human_has_control"
    assert result.get("success") is not True
    assert "WHAT-THE-HUMAN-TYPED" not in json.dumps(result)


def test_browser_get_images_terminal_remint_after_assembly(monkeypatch):
    """Image JSON is built after the SSRF remint check; a later epoch must not ship it."""
    browser, session = _wire(monkeypatch, [])
    monkeypatch.setattr(session, "_run_browser_command", lambda *_a, **_k: {
        "success": True,
        "data": {"result": json.dumps([
            {"src": "https://human.example/secret.png", "alt": "WHAT-THE-HUMAN-TYPED"},
        ])},
    })
    monkeypatch.setattr(browser, "_blocked_private_page_content", lambda *_a: None)

    def redact_then_takeover(images):
        lease.acquire("human-viewer")
        lease.release("human-viewer")
        return images

    monkeypatch.setattr(browser._snapshot, "_redact_browser_output", redact_then_takeover)
    result = json.loads(browser.browser_get_images(task_id="review"))
    assert result.get("code") == "human_has_control"
    assert result.get("success") is not True
    assert "WHAT-THE-HUMAN-TYPED" not in json.dumps(result)


def test_browser_back_discards_url_when_epoch_moves_after_back(monkeypatch):
    """History URL from the agent's ``back`` belongs to that epoch, not a later one."""
    browser, session = _wire(monkeypatch, [])
    monkeypatch.setattr(session, "_run_browser_command", lambda *_a, **_k: {
        "success": True, "data": {"url": "https://human-private.example/"}})

    def steal(*_a, **_k):
        lease.acquire("human-viewer")
        lease.release("human-viewer")
        return None

    monkeypatch.setattr(browser, "_blocked_private_page", steal)
    result = json.loads(browser.browser_back(task_id="review"))
    assert result.get("code") == "human_has_control"
    assert result.get("success") is not True
    assert "human-private.example" not in json.dumps(result)


def test_browser_eval_subprocess_discards_when_epoch_moves_during_ssrf_recheck(monkeypatch):
    """Subprocess eval discarded after the command, then reminted the URL probe.

    Supervisor already discards after ``_eval_result_or_blocked``; the CLI path
    used to return the eval payload if the probe itself crossed a take-over.
    """
    browser, session = _wire(monkeypatch, [])
    monkeypatch.setattr(browser._eval_policy, "_eval_ssrf_guard_active", lambda *_a: False)
    monkeypatch.setattr(browser, "_eval_supervisor_fast_path", lambda *_a, **_k: None)
    monkeypatch.setattr(session, "_run_browser_command", lambda *_a, **_k: {
        "success": True, "data": {"result": "WHAT-THE-HUMAN-TYPED"}})

    def steal(*_a, **_k):
        lease.acquire("human-viewer")
        lease.release("human-viewer")
        return None

    monkeypatch.setattr(browser, "_blocked_private_page_content", steal)
    raw = browser._browser_eval("document.body.innerText", task_id="review")
    text = raw if isinstance(raw, str) else json.dumps(raw)
    parsed = json.loads(text)
    assert parsed.get("code") == "human_has_control"
    assert "WHAT-THE-HUMAN-TYPED" not in text


def test_browser_eval_failure_preserves_human_has_control_code(monkeypatch):
    """The CLI eval failure wrapper rewrites backend errors and used to drop ``code``.

    ``_run_browser_command`` can refuse with human_has_control while the tool-level
    ticket is still current (or None). The agent must still see the handoff code.
    """
    browser, session = _wire(monkeypatch, [])
    monkeypatch.setattr(browser._eval_policy, "_eval_ssrf_guard_active", lambda *_a: False)
    monkeypatch.setattr(browser, "_eval_supervisor_fast_path", lambda *_a, **_k: None)
    monkeypatch.setattr(session, "_run_browser_command", lambda *_a, **_k: {
        "success": False, "code": "human_has_control",
        "error": "A human has control of this bot's screen.",
    })
    raw = browser._browser_eval("1+1", task_id="review")
    parsed = json.loads(raw if isinstance(raw, str) else json.dumps(raw))
    assert parsed.get("code") == "human_has_control"
    assert parsed.get("success") is not True


def test_browser_type_preserves_human_has_control_code(monkeypatch):
    """fill's refuse/discard carries ``code``; type must not rewrite it as a generic error.

    Click/press go through ``_tool_response``; type builds its own payload for
    redaction and used to drop the machine-readable handoff code.
    """
    commands: list = []
    browser, _ = _wire(monkeypatch, commands)
    lease.acquire("human-viewer")
    result = json.loads(browser.browser_type("e1", "secret-token", task_id="review"))
    assert commands == [], f"human holds the lease, yet type was dispatched: {commands}"
    assert result.get("code") == "human_has_control"
    assert result.get("success") is not True


def test_browser_type_preserves_human_has_control_when_fill_was_discarded(monkeypatch):
    """A reminted fill discard must still tell the agent to wait_for_human."""
    browser, session = _wire(monkeypatch, [])
    monkeypatch.setattr(session, "_run_browser_command", lambda *_a, **_k: {
        "success": False, "code": "human_has_control",
        "error": "A human took over the bot's screen while this browser command ran; its result was discarded.",
    })
    result = json.loads(browser.browser_type("e1", "secret-token", task_id="review"))
    assert result.get("code") == "human_has_control"
    assert result.get("success") is not True


@pytest.mark.parametrize("invoke", [
    lambda browser: browser.browser_click("e1", task_id="review"),
    lambda browser: browser.browser_navigate("https://example.com", task_id="review"),
    lambda browser: browser.browser_get_images(task_id="review"),
    lambda browser: browser.browser_back(task_id="review"),
    lambda browser: browser.browser_type("e1", "x", task_id="review"),
])
def test_human_hold_does_not_create_or_recycle_shared_session(monkeypatch, invoke):
    """Admit before session create: a human lease must not launch or tear down Chromium.

    ``browser_navigate`` used to call ``_get_session_info`` for ``_first_nav`` *before*
    the fence, so a human-held screen still spawned the shared browser.
    """
    looked: list = []
    created: list = []
    browser, session = _wire(monkeypatch, [])
    monkeypatch.setattr(
        session, "_get_session_info",
        lambda *a, **k: looked.append(a) or {
            "session_name": "review", "cdp_url": None, "features": {"local": True}})
    monkeypatch.setattr(
        session, "_create_local_session",
        lambda *a, **k: created.append(a) or {"session_name": "spawned", "features": {"local": True}})
    lease.acquire("human-viewer")
    result = json.loads(invoke(browser))
    assert looked == [], f"session lookup ran while human holds: {looked}"
    assert created == [], f"local session was created while human holds: {created}"
    assert result.get("code") == "human_has_control"


def test_create_local_session_refuses_while_human_holds():
    lease.acquire("human-viewer")
    with pytest.raises(RuntimeError, match="human holds"):
        session_mod._create_local_session("review")


def test_fence_peeks_cache_and_does_not_create(monkeypatch):
    from tools import browser_tool as browser

    looked: list = []
    monkeypatch.setattr(
        session_mod, "_get_session_info",
        lambda *a, **k: looked.append("get") or {"features": {"local": True}})
    browser._active_sessions["review"] = {
        "session_name": "review", "cdp_url": None, "features": {"local": True}}
    try:
        info = session_mod._session_info_for_shared_browser_fence("review")
        assert info.get("session_name") == "review"
        assert looked == [], f"fence created a session instead of peeking: {looked}"
    finally:
        browser._active_sessions.pop("review", None)


def test_predicted_cloud_backend_is_not_treated_as_shared(monkeypatch):
    """A configured cloud provider is another browser even before the session cache exists."""
    monkeypatch.setattr(session_mod._cdp, "_get_cdp_override_raw", lambda: "")
    monkeypatch.setattr(session_mod._cloud, "_get_cloud_provider", lambda: object())
    assert session_mod._predicted_local_shared_browser("cloud-task") is False
    lease.acquire("human-viewer")
    admitted, refuse = session_mod._shared_browser_fence("cloud-task")
    assert refuse is None
    assert admitted is None


_DOCK_CDP = "ws://127.0.0.1:45555/devtools/browser/dock"
_FOREIGN_CDP = "ws://127.0.0.1:9222/devtools/browser/x"


def _is_dock_cdp(url, **_k):
    return "45555" in (url or "")


def _without_in_process_real_profile():
    """Drop process-local real-profile identity; return a restore token."""
    from tools import browser_tool as browser

    key = browser._REAL_PROFILE_SESSION
    prior_session = browser._active_sessions.pop(key, None)
    prior_cache = browser._real_profile_cdp_cache.pop("cdp", None)
    return key, prior_session, prior_cache


def _restore_in_process_real_profile(token):
    from tools import browser_tool as browser

    key, prior_session, prior_cache = token
    if prior_session is None:
        browser._active_sessions.pop(key, None)
    else:
        browser._active_sessions[key] = prior_session
    if prior_cache is None:
        browser._real_profile_cdp_cache.pop("cdp", None)
    else:
        browser._real_profile_cdp_cache["cdp"] = prior_cache


def _live_real_profile_copy(monkeypatch, port=9334, browser="chrome"):
    """A surviving snapshot Chrome on ``{HERMES_HOME}/browser-profile/<browser>``.

    Cache / leftover ``hermes-real-profile`` rows are gone (process restart).
    Identity is the copy dir's live DevTools port — same probe as the dock.
    """
    from hermes_constants import get_hermes_home

    copy = get_hermes_home() / "browser-profile" / browser
    copy.mkdir(parents=True, exist_ok=True)

    def _port(user_data_dir, **_k):
        try:
            if Path(user_data_dir).resolve() == copy.resolve():
                return port
        except OSError:
            return None
        return None

    monkeypatch.setattr("tools.bot_desktop.browser.running_instance_cdp_port", _port)
    monkeypatch.setattr("tools.bot_desktop.browser.cdp_url_is_running_instance", lambda url, **k: False)
    return copy


def test_predicted_dock_cdp_override_is_shared(monkeypatch):
    """``/browser connect`` to the live dock port is this screen, not 'another browser'."""
    monkeypatch.setattr(session_mod._cdp, "_get_cdp_override_raw", lambda: _DOCK_CDP)
    monkeypatch.setattr("tools.bot_desktop.browser.cdp_url_is_running_instance", _is_dock_cdp)
    assert session_mod._predicted_local_shared_browser("review") is True
    lease.acquire("human-viewer")
    _admitted, refuse = session_mod._shared_browser_fence("review")
    assert refuse is not None
    assert refuse.get("code") == "human_has_control"


def test_predicted_foreign_cdp_override_is_not_shared(monkeypatch):
    """Default ``/browser connect`` to 9222 stays another browser while the dock is elsewhere."""
    monkeypatch.setattr(session_mod._cdp, "_get_cdp_override_raw", lambda: _FOREIGN_CDP)
    monkeypatch.setattr("tools.bot_desktop.browser.cdp_url_is_running_instance", lambda url, **k: False)
    assert session_mod._predicted_local_shared_browser("review") is False
    lease.acquire("human-viewer")
    admitted, refuse = session_mod._shared_browser_fence("review")
    assert refuse is None
    assert admitted is None


def test_predicted_real_profile_cdp_override_is_shared(monkeypatch):
    """``/browser connect`` to this profile's real-profile Chrome is this screen.

    Real-profile cache stores HTTP; the override is the rewritten WebSocket.
    Same loopback port = same Chromium launched on this Bot Desktop DISPLAY.
    Dock-identity-only predict treated that attach as another browser, so
    the outer fence skipped and ``_create_cdp_session`` attached while the
    human held.
    """
    from tools import browser_tool as browser

    rp_http = "http://127.0.0.1:9334"
    rp_ws = "ws://127.0.0.1:9334/devtools/browser/real"
    browser._real_profile_cdp_cache["cdp"] = rp_http
    monkeypatch.setattr(session_mod._cdp, "_get_cdp_override_raw", lambda: rp_ws)
    monkeypatch.setattr("tools.bot_desktop.browser.cdp_url_is_running_instance", _is_dock_cdp)
    try:
        assert session_mod._predicted_local_shared_browser("review") is True
        lease.acquire("human-viewer")
        _admitted, refuse = session_mod._shared_browser_fence("review")
        assert refuse is not None
        assert refuse.get("code") == "human_has_control"
    finally:
        browser._real_profile_cdp_cache.pop("cdp", None)


def _install_supervisor(monkeypatch, cdp_url, task_id="cloud-task"):
    supervisor = type("S", (), {"cdp_url": cdp_url})()
    monkeypatch.setattr(
        "tools.browser_supervisor.SUPERVISOR_REGISTRY",
        type("R", (), {"get": staticmethod(lambda tid: supervisor if tid == task_id else None)})(),
    )
    return supervisor


def _predict_cloud(monkeypatch):
    monkeypatch.setattr(session_mod._cdp, "_get_cdp_override_raw", lambda: "")
    monkeypatch.setattr(session_mod._cloud, "_get_cloud_provider", lambda: object())
    monkeypatch.setattr("tools.bot_desktop.browser.cdp_url_is_running_instance", _is_dock_cdp)


def test_dock_supervisor_is_shared_when_cloud_is_predicted(monkeypatch):
    """Supervisor-only paths never re-admit; a dock supervisor is this screen even
    when a configured cloud provider would make a *new* session another browser."""
    _predict_cloud(monkeypatch)
    _install_supervisor(monkeypatch, _DOCK_CDP)
    assert session_mod._predicted_local_shared_browser("cloud-task") is False
    lease.acquire("human-viewer")
    _admitted, refuse = session_mod._shared_browser_fence("cloud-task")
    assert refuse is not None
    assert refuse.get("code") == "human_has_control"


def test_real_profile_supervisor_is_shared_when_cloud_is_predicted(monkeypatch):
    """A live supervisor on this profile's real-profile Chrome is this screen.

    ``_dock_supervisor_session_info`` used to key only on dock identity, so a
    predicted-cloud miss left a real-profile supervisor unfenced.
    """
    from tools import browser_tool as browser

    rp_http = "http://127.0.0.1:9334"
    rp_ws = "ws://127.0.0.1:9334/devtools/browser/real"
    browser._real_profile_cdp_cache["cdp"] = rp_http
    _predict_cloud(monkeypatch)
    _install_supervisor(monkeypatch, rp_ws)
    try:
        assert session_mod._predicted_local_shared_browser("cloud-task") is False
        lease.acquire("human-viewer")
        _admitted, refuse = session_mod._shared_browser_fence("cloud-task")
        assert refuse is not None
        assert refuse.get("code") == "human_has_control"
    finally:
        browser._real_profile_cdp_cache.pop("cdp", None)


def test_foreign_supervisor_is_not_shared_when_cloud_is_predicted(monkeypatch):
    """A leftover supervisor on the user's Chrome / a cloud CDP stays another browser."""
    _predict_cloud(monkeypatch)
    _install_supervisor(monkeypatch, _FOREIGN_CDP)
    lease.acquire("human-viewer")
    admitted, refuse = session_mod._shared_browser_fence("cloud-task")
    assert refuse is None
    assert admitted is None


def test_dock_supervisor_wins_over_cached_cloud_label(monkeypatch):
    """Cache can still say cloud while evaluate/dialog talk the dock supervisor."""
    from tools import browser_tool as browser

    _predict_cloud(monkeypatch)
    _install_supervisor(monkeypatch, _DOCK_CDP, task_id="review")
    prior = dict(browser._active_sessions)
    browser._active_sessions["review"] = {
        "session_name": "cloud_1", "cdp_url": "wss://browserbase.example/s",
        "features": {"cloud": True},
    }
    try:
        lease.acquire("human-viewer")
        _admitted, refuse = session_mod._shared_browser_fence("review")
        assert refuse is not None
        assert refuse.get("code") == "human_has_control"
    finally:
        browser._active_sessions.clear()
        browser._active_sessions.update(prior)


def test_browser_eval_supervisor_fenced_when_cloud_is_predicted(monkeypatch):
    """``_eval_supervisor_fast_path`` never re-admits; predicted cloud must not
    fail-open a dock supervisor while the human holds."""
    from tools import browser_dialog_tool as dialog
    from tools import browser_vault_tool as vault

    ran: list = []

    class _Sup:
        cdp_url = _DOCK_CDP

        def evaluate_runtime(self, expr):
            ran.append(("eval", expr))
            return {"ok": True, "result": "WHAT-THE-HUMAN-TYPED"}

        def respond_to_dialog(self, **_k):
            ran.append("dialog")
            return {"ok": True, "dialog": {}}

    _predict_cloud(monkeypatch)
    monkeypatch.setattr(
        "tools.browser_supervisor.SUPERVISOR_REGISTRY",
        type("R", (), {"get": staticmethod(lambda _tid: _Sup())})(),
    )
    monkeypatch.setattr(dialog, "SUPERVISOR_REGISTRY",
                        type("R", (), {"get": staticmethod(lambda _tid: _Sup())})())
    from tools import browser_tool as browser
    monkeypatch.setattr(browser, "_is_camofox_mode", lambda: False)
    monkeypatch.setattr(browser._eval_policy, "_eval_ssrf_guard_active", lambda *_a: False)
    lease.acquire("human-viewer")

    eval_result = json.loads(browser._browser_eval("document.body.innerText", task_id="review"))
    assert ("eval", "document.body.innerText") not in ran
    assert eval_result.get("code") == "human_has_control"

    dialog_result = json.loads(dialog.browser_dialog(action="accept", task_id="review"))
    assert "dialog" not in ran
    assert dialog_result.get("code") == "human_has_control"

    vault_result = vault._eval_js("review", "window.location.href")
    assert ("eval", "window.location.href") not in ran
    assert vault_result.get("code") == "human_has_control"
    assert "WHAT-THE-HUMAN-TYPED" not in json.dumps(vault_result)


def test_cdp_override_session_is_fenced_when_url_is_dock(monkeypatch):
    """Session label is ``cdp_override``; identity is the dock port."""
    commands: list = []
    browser, session = _wire(monkeypatch, commands)
    monkeypatch.setattr(session._cdp, "_get_cdp_override_raw", lambda: _DOCK_CDP)
    monkeypatch.setattr("tools.bot_desktop.browser.cdp_url_is_running_instance", _is_dock_cdp)
    monkeypatch.setattr(session, "_get_session_info", lambda *a: {
        "session_name": "cdp_1", "cdp_url": _DOCK_CDP, "features": {"cdp_override": True}})
    lease.acquire("human-viewer")
    result = json.loads(browser.browser_click("e1", task_id="review"))
    assert commands == [], f"human holds the lease, yet dock CDP was dispatched: {commands}"
    assert result.get("code") == "human_has_control"


def test_cdp_override_session_is_fenced_when_url_is_real_profile(monkeypatch):
    """Session label is ``cdp_override``; identity is this profile's Chrome."""
    from tools import browser_tool as browser_mod

    commands: list = []
    browser, session = _wire(monkeypatch, commands)
    rp_http = "http://127.0.0.1:9334"
    rp_ws = "ws://127.0.0.1:9334/devtools/browser/real"
    browser_mod._real_profile_cdp_cache["cdp"] = rp_http
    monkeypatch.setattr(session._cdp, "_get_cdp_override_raw", lambda: rp_ws)
    monkeypatch.setattr("tools.bot_desktop.browser.cdp_url_is_running_instance", _is_dock_cdp)
    monkeypatch.setattr(session, "_get_session_info", lambda *a: {
        "session_name": "cdp_1", "cdp_url": rp_ws, "features": {"cdp_override": True}})
    lease.acquire("human-viewer")
    try:
        result = json.loads(browser.browser_click("e1", task_id="review"))
    finally:
        browser_mod._real_profile_cdp_cache.pop("cdp", None)
    assert commands == [], f"human holds the lease, yet real-profile CDP was dispatched: {commands}"
    assert result.get("code") == "human_has_control"


def test_cdp_override_session_is_fenced_when_url_is_surviving_real_profile_copy(monkeypatch):
    """Cache-empty ``cdp_override`` session whose URL is the surviving copy."""
    token = _without_in_process_real_profile()
    _live_real_profile_copy(monkeypatch)
    commands: list = []
    browser, session = _wire(monkeypatch, commands)
    rp_ws = "ws://127.0.0.1:9334/devtools/browser/real"
    monkeypatch.setattr(session._cdp, "_get_cdp_override_raw", lambda: rp_ws)
    monkeypatch.setattr(session, "_get_session_info", lambda *a: {
        "session_name": "cdp_1", "cdp_url": rp_ws, "features": {"cdp_override": True}})
    lease.acquire("human-viewer")
    try:
        result = json.loads(browser.browser_click("e1", task_id="review"))
    finally:
        _restore_in_process_real_profile(token)
    assert commands == [], f"human holds the lease, yet surviving copy CDP was dispatched: {commands}"
    assert result.get("code") == "human_has_control"


def test_cdp_override_session_is_not_fenced_when_url_is_foreign(monkeypatch):
    commands: list = []
    browser, session = _wire(monkeypatch, commands)
    monkeypatch.setattr(session._cdp, "_get_cdp_override_raw", lambda: _FOREIGN_CDP)
    monkeypatch.setattr("tools.bot_desktop.browser.cdp_url_is_running_instance", lambda url, **k: False)
    monkeypatch.setattr(session, "_get_session_info", lambda *a: {
        "session_name": "cdp_1", "cdp_url": _FOREIGN_CDP, "features": {"cdp_override": True}})
    lease.acquire("human-viewer")
    result = json.loads(browser.browser_click("e1", task_id="review"))
    assert commands, f"foreign CDP was refused as if it were the dock: {result}"
    assert result.get("success") is True


def test_create_cdp_session_refuses_dock_while_human_holds(monkeypatch):
    monkeypatch.setattr("tools.bot_desktop.browser.cdp_url_is_running_instance", lambda url, **k: True)
    lease.acquire("human-viewer")
    with pytest.raises(lease.HumanHasControl, match="human holds"):
        session_mod._create_cdp_session("review", _DOCK_CDP)


def test_create_cdp_session_refuses_real_profile_while_human_holds(monkeypatch):
    """``_create_cdp_session`` used to refuse only a live dock URL.

    A ``cdp_override`` pointing at this profile's real-profile Chrome is the
    same DISPLAY Chromium. Creating the session while the human holds would
    then let later clicks run through a ``cdp_override`` label the old
    predicate treated as foreign.
    """
    from tools import browser_tool as browser

    rp_http = "http://127.0.0.1:9334"
    rp_ws = "ws://127.0.0.1:9334/devtools/browser/real"
    browser._real_profile_cdp_cache["cdp"] = rp_http
    monkeypatch.setattr("tools.bot_desktop.browser.cdp_url_is_running_instance", _is_dock_cdp)
    lease.acquire("human-viewer")
    try:
        with pytest.raises(lease.HumanHasControl, match="human holds"):
            session_mod._create_cdp_session("review", rp_ws)
    finally:
        browser._real_profile_cdp_cache.pop("cdp", None)


def test_create_cdp_session_refuses_leftover_real_profile_session_url(monkeypatch):
    """Cache miss; identity is the process-global ``hermes-real-profile`` row."""
    from tools import browser_tool as browser

    key = browser._REAL_PROFILE_SESSION
    prior = browser._active_sessions.get(key)
    rp_ws = "ws://127.0.0.1:9334/devtools/browser/real"
    browser._active_sessions[key] = {
        "session_name": key,
        "cdp_url": rp_ws,
        "features": {"local": True, "real_profile": True},
    }
    monkeypatch.setattr("tools.bot_desktop.browser.cdp_url_is_running_instance", _is_dock_cdp)
    lease.acquire("human-viewer")
    try:
        with pytest.raises(lease.HumanHasControl, match="human holds"):
            session_mod._create_cdp_session("review", "http://127.0.0.1:9334")
    finally:
        if prior is None:
            browser._active_sessions.pop(key, None)
        else:
            browser._active_sessions[key] = prior


def test_create_cdp_session_allows_foreign_endpoint_while_human_holds(monkeypatch):
    monkeypatch.setattr("tools.bot_desktop.browser.cdp_url_is_running_instance", lambda url, **k: False)
    lease.acquire("human-viewer")
    info = session_mod._create_cdp_session("review", _FOREIGN_CDP)
    assert info["cdp_url"] == _FOREIGN_CDP
    assert info["features"]["cdp_override"] is True


def test_create_cdp_session_allows_foreign_endpoint_with_leftover_real_profile(monkeypatch):
    """A leftover real-profile cache must not fence a foreign CDP attach."""
    from tools import browser_tool as browser

    browser._real_profile_cdp_cache["cdp"] = "http://127.0.0.1:9334"
    monkeypatch.setattr("tools.bot_desktop.browser.cdp_url_is_running_instance", lambda url, **k: False)
    lease.acquire("human-viewer")
    try:
        info = session_mod._create_cdp_session("review", _FOREIGN_CDP)
        assert info["cdp_url"] == _FOREIGN_CDP
        assert info["features"]["cdp_override"] is True
    finally:
        browser._real_profile_cdp_cache.pop("cdp", None)


def test_surviving_real_profile_copy_is_shared_without_cache(monkeypatch):
    """Copy-dir Chrome survives a process restart; cache / leftover row do not.

    ``/browser connect`` then labels that attach ``cdp_override``. Dock- and
    cache-only identity treated it as user Chrome.
    """
    token = _without_in_process_real_profile()
    _live_real_profile_copy(monkeypatch)
    rp_ws = "ws://127.0.0.1:9334/devtools/browser/real"
    try:
        assert session_mod._shares_bot_desktop_browser({"cdp_url": rp_ws})
        assert session_mod._shares_bot_desktop_browser({"cdp_url": "http://127.0.0.1:9334"})
        assert not session_mod._shares_bot_desktop_browser({"cdp_url": _FOREIGN_CDP})
    finally:
        _restore_in_process_real_profile(token)


def test_predicted_surviving_real_profile_override_is_shared(monkeypatch):
    """Cache-empty ``/browser connect`` to the surviving copy is this screen."""
    token = _without_in_process_real_profile()
    _live_real_profile_copy(monkeypatch)
    rp_ws = "ws://127.0.0.1:9334/devtools/browser/real"
    monkeypatch.setattr(session_mod._cdp, "_get_cdp_override_raw", lambda: rp_ws)
    try:
        assert session_mod._predicted_local_shared_browser("review") is True
        lease.acquire("human-viewer")
        _admitted, refuse = session_mod._shared_browser_fence("review")
        assert refuse is not None
        assert refuse.get("code") == "human_has_control"
    finally:
        _restore_in_process_real_profile(token)


def test_surviving_real_profile_supervisor_is_shared_when_cloud_is_predicted(monkeypatch):
    """Predicted-cloud skip + cache-empty supervisor on the surviving copy."""
    token = _without_in_process_real_profile()
    _live_real_profile_copy(monkeypatch)
    rp_ws = "ws://127.0.0.1:9334/devtools/browser/real"
    _predict_cloud(monkeypatch)
    _install_supervisor(monkeypatch, rp_ws)
    try:
        assert session_mod._predicted_local_shared_browser("cloud-task") is False
        lease.acquire("human-viewer")
        _admitted, refuse = session_mod._shared_browser_fence("cloud-task")
        assert refuse is not None
        assert refuse.get("code") == "human_has_control"
    finally:
        _restore_in_process_real_profile(token)


def test_create_cdp_session_refuses_surviving_real_profile_copy(monkeypatch):
    """``_create_cdp_session`` must refuse the surviving copy, not only cache."""
    token = _without_in_process_real_profile()
    _live_real_profile_copy(monkeypatch)
    rp_ws = "ws://127.0.0.1:9334/devtools/browser/real"
    lease.acquire("human-viewer")
    try:
        with pytest.raises(lease.HumanHasControl, match="human holds"):
            session_mod._create_cdp_session("review", rp_ws)
        info = session_mod._create_cdp_session("review", _FOREIGN_CDP)
        assert info["cdp_url"] == _FOREIGN_CDP
        assert info["features"]["cdp_override"] is True
    finally:
        _restore_in_process_real_profile(token)


def test_cdp_loopback_port_matches_scheme_less_host_port():
    """``browser.cdp_url`` / ``BROWSER_CDP_URL`` are often stored as ``127.0.0.1:PORT``.

    ``urlparse`` without a scheme puts the whole string in ``path``, so hostname
    and port are ``None`` and real-profile identity treated that attach as
    foreign Chrome.
    """
    assert session_mod._cdp_loopback_port("127.0.0.1:9334") == 9334
    assert session_mod._cdp_loopback_port("localhost:9334") == 9334
    assert session_mod._cdp_loopback_port("http://127.0.0.1:9334") == 9334
    assert session_mod._cdp_loopback_port("ws://127.0.0.1:9334/devtools/browser/real") == 9334
    assert session_mod._cdp_loopback_port("127.0.0.1:9222") == 9222
    assert session_mod._cdp_loopback_port("192.168.1.9:9334") is None
    assert session_mod._cdp_endpoints_match("127.0.0.1:9334", "http://127.0.0.1:9334")
    assert session_mod._cdp_endpoints_match("127.0.0.1:9334", "ws://127.0.0.1:9334/devtools/browser/real")
    assert not session_mod._cdp_endpoints_match("127.0.0.1:9334", "127.0.0.1:9222")
    assert not session_mod._cdp_endpoints_match("127.0.0.1:9334", _FOREIGN_CDP)


def test_scheme_less_override_matches_leftover_real_profile_cache(monkeypatch):
    """Cache is ``http://…``; ``/browser connect`` raw is often ``host:port``."""
    from tools import browser_tool as browser

    token = _without_in_process_real_profile()
    browser._real_profile_cdp_cache["cdp"] = "http://127.0.0.1:9334"
    monkeypatch.setattr(session_mod._cdp, "_get_cdp_override_raw", lambda: "127.0.0.1:9334")
    monkeypatch.setattr("tools.bot_desktop.browser.cdp_url_is_running_instance", lambda url, **k: False)
    try:
        assert session_mod._shares_bot_desktop_browser({"cdp_url": "127.0.0.1:9334"})
        assert session_mod._predicted_local_shared_browser("review") is True
        lease.acquire("human-viewer")
        _admitted, refuse = session_mod._shared_browser_fence("review")
        assert refuse is not None
        assert refuse.get("code") == "human_has_control"
    finally:
        _restore_in_process_real_profile(token)


def test_scheme_less_foreign_override_does_not_match_real_profile_cache(monkeypatch):
    """A leftover cache must not fence a later scheme-less user CDP."""
    from tools import browser_tool as browser

    token = _without_in_process_real_profile()
    browser._real_profile_cdp_cache["cdp"] = "http://127.0.0.1:9334"
    monkeypatch.setattr(session_mod._cdp, "_get_cdp_override_raw", lambda: "127.0.0.1:9222")
    monkeypatch.setattr("tools.bot_desktop.browser.cdp_url_is_running_instance", lambda url, **k: False)
    try:
        assert not session_mod._shares_bot_desktop_browser({"cdp_url": "127.0.0.1:9222"})
        assert session_mod._predicted_local_shared_browser("review") is False
        lease.acquire("human-viewer")
        admitted, refuse = session_mod._shared_browser_fence("review")
        assert refuse is None
        assert admitted is None
    finally:
        _restore_in_process_real_profile(token)


def test_surviving_real_profile_copy_is_shared_via_scheme_less_url(monkeypatch):
    """Copy-dir identity must parse scheme-less ``127.0.0.1:PORT``, not only HTTP/WS."""
    token = _without_in_process_real_profile()
    _live_real_profile_copy(monkeypatch)
    try:
        assert session_mod._shares_bot_desktop_browser({"cdp_url": "127.0.0.1:9334"})
        assert not session_mod._shares_bot_desktop_browser({"cdp_url": "127.0.0.1:9222"})
    finally:
        _restore_in_process_real_profile(token)


def test_predicted_scheme_less_surviving_real_profile_override_is_shared(monkeypatch):
    """Cache-empty ``/browser connect`` raw ``127.0.0.1:PORT`` is this screen."""
    token = _without_in_process_real_profile()
    _live_real_profile_copy(monkeypatch)
    monkeypatch.setattr(session_mod._cdp, "_get_cdp_override_raw", lambda: "127.0.0.1:9334")
    try:
        assert session_mod._predicted_local_shared_browser("review") is True
        lease.acquire("human-viewer")
        _admitted, refuse = session_mod._shared_browser_fence("review")
        assert refuse is not None
        assert refuse.get("code") == "human_has_control"
    finally:
        _restore_in_process_real_profile(token)


def test_create_cdp_session_refuses_scheme_less_surviving_real_profile_copy(monkeypatch):
    token = _without_in_process_real_profile()
    _live_real_profile_copy(monkeypatch)
    lease.acquire("human-viewer")
    try:
        with pytest.raises(lease.HumanHasControl, match="human holds"):
            session_mod._create_cdp_session("review", "127.0.0.1:9334")
        info = session_mod._create_cdp_session("review", "127.0.0.1:9222")
        assert info["cdp_url"] == "127.0.0.1:9222"
        assert info["features"]["cdp_override"] is True
    finally:
        _restore_in_process_real_profile(token)


def test_cdp_override_session_is_fenced_when_url_is_scheme_less_surviving_copy(monkeypatch):
    token = _without_in_process_real_profile()
    _live_real_profile_copy(monkeypatch)
    commands: list = []
    browser, session = _wire(monkeypatch, commands)
    monkeypatch.setattr(session._cdp, "_get_cdp_override_raw", lambda: "127.0.0.1:9334")
    monkeypatch.setattr(session, "_get_session_info", lambda *a: {
        "session_name": "cdp_1", "cdp_url": "127.0.0.1:9334", "features": {"cdp_override": True}})
    lease.acquire("human-viewer")
    try:
        result = json.loads(browser.browser_click("e1", task_id="review"))
    finally:
        _restore_in_process_real_profile(token)
    assert commands == [], f"human holds the lease, yet scheme-less surviving copy CDP was dispatched: {commands}"
    assert result.get("code") == "human_has_control"


def test_ensure_supervisor_does_not_probe_leftover_override_on_cached_cloud(monkeypatch):
    """Cached cloud skips the fence; supervisor attach used to prefer leftover connect."""
    from tools import browser_tool as browser
    from tools import browser_tool_cdp as cdp

    token = _without_in_process_real_profile()
    _live_real_profile_copy(monkeypatch)
    cloud = "wss://cloud.example/devtools/browser/x"
    prior = browser._active_sessions.get("review")
    browser._active_sessions["review"] = {
        "session_name": "cloud", "cdp_url": cloud, "features": {"local": False},
    }
    monkeypatch.setattr(cdp, "_get_cdp_override_raw", lambda: "127.0.0.1:9334")
    probed: list = []

    def probe(*_a, **_k):
        probed.append("get")
        raise AssertionError("leftover Chrome must not be probed while human holds")

    started: list = []
    monkeypatch.setattr("requests.get", probe)
    monkeypatch.setattr(
        "tools.browser_supervisor.SUPERVISOR_REGISTRY",
        type("R", (), {"get_or_start": staticmethod(lambda **kw: started.append(kw.get("cdp_url")))})(),
    )
    lease.acquire("human-viewer")
    try:
        cdp._ensure_cdp_supervisor("review")
    finally:
        if prior is None:
            browser._active_sessions.pop("review", None)
        else:
            browser._active_sessions["review"] = prior
        _restore_in_process_real_profile(token)
    assert probed == [], f"human holds the lease, yet leftover Chrome was probed: {probed}"
    assert started == [cloud]


def test_ensure_supervisor_does_not_probe_leftover_dock_on_cached_cloud(monkeypatch):
    """Same skip when leftover ``/browser connect`` is the live dock, not a copy."""
    from tools import browser_tool as browser
    from tools import browser_tool_cdp as cdp

    cloud = "wss://cloud.example/devtools/browser/x"
    prior = browser._active_sessions.get("review")
    browser._active_sessions["review"] = {
        "session_name": "cloud", "cdp_url": cloud, "features": {"local": False},
    }
    monkeypatch.setattr(cdp, "_get_cdp_override_raw", lambda: _DOCK_CDP)
    monkeypatch.setattr("tools.bot_desktop.browser.cdp_url_is_running_instance", _is_dock_cdp)
    probed: list = []

    def probe(*_a, **_k):
        probed.append("get")
        raise AssertionError("leftover dock must not be probed while human holds")

    started: list = []
    monkeypatch.setattr("requests.get", probe)
    monkeypatch.setattr(
        "tools.browser_supervisor.SUPERVISOR_REGISTRY",
        type("R", (), {"get_or_start": staticmethod(lambda **kw: started.append(kw.get("cdp_url")))})(),
    )
    lease.acquire("human-viewer")
    try:
        cdp._ensure_cdp_supervisor("review")
    finally:
        if prior is None:
            browser._active_sessions.pop("review", None)
        else:
            browser._active_sessions["review"] = prior
    assert probed == [], f"human holds the lease, yet leftover dock was probed: {probed}"
    assert started == [cloud]


def test_ensure_supervisor_does_not_steal_cloud_session_for_leftover_connect(monkeypatch):
    """Leftover ``/browser connect`` is this screen. A cached cloud session must
    keep its own supervisor even while the agent holds — preferring leftover
    mixed this screen's dialogs into cloud snapshots."""
    from tools import browser_tool as browser
    from tools import browser_tool_cdp as cdp

    cloud = "wss://cloud.example/devtools/browser/x"
    prior = browser._active_sessions.get("review")
    browser._active_sessions["review"] = {
        "session_name": "cloud", "cdp_url": cloud, "features": {"local": False},
    }
    monkeypatch.setattr(cdp, "_get_cdp_override", lambda: _DOCK_CDP)
    monkeypatch.setattr(cdp, "_get_cdp_override_raw", lambda: _DOCK_CDP)
    monkeypatch.setattr("tools.bot_desktop.browser.cdp_url_is_running_instance", _is_dock_cdp)
    probed: list = []

    def probe(*_a, **_k):
        probed.append("get")
        raise AssertionError("leftover dock must not be probed for a cloud session")

    started: list = []
    monkeypatch.setattr("requests.get", probe)
    monkeypatch.setattr(
        "tools.browser_supervisor.SUPERVISOR_REGISTRY",
        type("R", (), {"get_or_start": staticmethod(lambda **kw: started.append(kw.get("cdp_url")))})(),
    )
    try:
        cdp._ensure_cdp_supervisor("review")
    finally:
        if prior is None:
            browser._active_sessions.pop("review", None)
        else:
            browser._active_sessions["review"] = prior
    assert probed == [], f"cloud session attach probed leftover dock: {probed}"
    assert started == [cloud]


def test_ensure_supervisor_keeps_leftover_when_session_is_that_endpoint(monkeypatch):
    """Same-endpoint leftover connect is this session — attach it."""
    from tools import browser_tool as browser
    from tools import browser_tool_cdp as cdp

    prior = browser._active_sessions.get("review")
    browser._active_sessions["review"] = {
        "session_name": "cdp", "cdp_url": _DOCK_CDP, "features": {"cdp_override": True},
    }
    monkeypatch.setattr(cdp, "_get_cdp_override", lambda: _DOCK_CDP)
    monkeypatch.setattr(cdp, "_get_cdp_override_raw", lambda: _DOCK_CDP)
    monkeypatch.setattr("tools.bot_desktop.browser.cdp_url_is_running_instance", _is_dock_cdp)
    started: list = []
    monkeypatch.setattr(
        "tools.browser_supervisor.SUPERVISOR_REGISTRY",
        type("R", (), {"get_or_start": staticmethod(lambda **kw: started.append(kw.get("cdp_url")))})(),
    )
    try:
        cdp._ensure_cdp_supervisor("review")
    finally:
        if prior is None:
            browser._active_sessions.pop("review", None)
        else:
            browser._active_sessions["review"] = prior
    assert started == [_DOCK_CDP]


def test_snapshot_does_not_merge_leftover_dock_dialogs_into_a_cloud_tree(monkeypatch):
    """CLI snapshot talks the cached cloud session; leftover connect can keep
    a dock supervisor on the same task_id. Those dialogs are this screen."""
    from tools import browser_tool as browser
    from tools import browser_tool_session as session

    monkeypatch.setattr(browser, "_is_camofox_mode", lambda: False)
    monkeypatch.setattr(browser, "_blocked_private_page_content", lambda *_a: None)
    monkeypatch.setattr("tools.bot_desktop.browser.cdp_url_is_running_instance", _is_dock_cdp)
    prior = browser._active_sessions.get("review")
    browser._active_sessions["review"] = {
        "session_name": "cloud", "cdp_url": "wss://browserbase.example/s",
        "features": {"cloud": True},
    }
    monkeypatch.setattr(session, "_run_browser_command", lambda *_a, **_k: {
        "success": True, "data": {"snapshot": "CLOUD_TREE", "refs": {}},
    })
    merged: list = []

    class _Snap:
        active = True

        def to_dict(self):
            merged.append("dialogs")
            return {"pending_dialogs": [{"message": "WHAT-THE-HUMAN-TYPED"}]}

    class _Sup:
        cdp_url = _DOCK_CDP

        def snapshot(self):
            return _Snap()

    monkeypatch.setattr(
        "tools.browser_supervisor.SUPERVISOR_REGISTRY",
        type("R", (), {"get": staticmethod(lambda _tid: _Sup())})(),
    )
    try:
        parsed = json.loads(browser.browser_snapshot(task_id="review"))
    finally:
        if prior is None:
            browser._active_sessions.pop("review", None)
        else:
            browser._active_sessions["review"] = prior
    assert parsed.get("success") is True
    assert parsed.get("snapshot") == "CLOUD_TREE"
    assert merged == [], f"leftover dock dialogs were merged into the cloud tree: {merged}"
    assert "WHAT-THE-HUMAN-TYPED" not in json.dumps(parsed)
    assert not parsed.get("pending_dialogs")


def test_snapshot_merges_supervisor_dialogs_when_session_is_the_dock(monkeypatch):
    """Same-browser leftover connect still contributes pending_dialogs."""
    from tools import browser_tool as browser
    from tools import browser_tool_session as session

    monkeypatch.setattr(browser, "_is_camofox_mode", lambda: False)
    monkeypatch.setattr(browser, "_blocked_private_page_content", lambda *_a: None)
    monkeypatch.setattr("tools.bot_desktop.browser.cdp_url_is_running_instance", _is_dock_cdp)
    prior = browser._active_sessions.get("review")
    browser._active_sessions["review"] = {
        "session_name": "cdp", "cdp_url": _DOCK_CDP, "features": {"cdp_override": True},
    }
    monkeypatch.setattr(session, "_run_browser_command", lambda *_a, **_k: {
        "success": True, "data": {"snapshot": "DOCK_TREE", "refs": {}},
    })

    class _Snap:
        active = True

        def to_dict(self):
            return {"pending_dialogs": [{"message": "DOCK_DIALOG"}]}

    class _Sup:
        cdp_url = _DOCK_CDP

        def snapshot(self):
            return _Snap()

    monkeypatch.setattr(
        "tools.browser_supervisor.SUPERVISOR_REGISTRY",
        type("R", (), {"get": staticmethod(lambda _tid: _Sup())})(),
    )
    try:
        parsed = json.loads(browser.browser_snapshot(task_id="review"))
    finally:
        if prior is None:
            browser._active_sessions.pop("review", None)
        else:
            browser._active_sessions["review"] = prior
    assert parsed.get("success") is True
    assert parsed.get("snapshot") == "DOCK_TREE"
    assert parsed.get("pending_dialogs") == [{"message": "DOCK_DIALOG"}]


def test_eval_does_not_run_leftover_dock_supervisor_on_cached_cloud(monkeypatch):
    """Leftover dock ``evaluate_runtime`` must not run JS on this screen for a cloud session."""
    from tools import browser_tool as browser
    from tools import browser_tool_session as session

    monkeypatch.setattr(browser, "_is_camofox_mode", lambda: False)
    monkeypatch.setattr(browser, "_blocked_private_page_content", lambda *_a: None)
    monkeypatch.setattr(browser._eval_policy, "_eval_ssrf_guard_active", lambda *_a: False)
    monkeypatch.setattr("tools.bot_desktop.browser.cdp_url_is_running_instance", _is_dock_cdp)
    prior = browser._active_sessions.get("review")
    browser._active_sessions["review"] = {
        "session_name": "cloud", "cdp_url": "wss://browserbase.example/s",
        "features": {"cloud": True},
    }
    ran: list = []

    class _Sup:
        cdp_url = _DOCK_CDP

        def evaluate_runtime(self, expr):
            ran.append(expr)
            return {"ok": True, "result": "WHAT-THE-HUMAN-TYPED"}

    monkeypatch.setattr(
        "tools.browser_supervisor.SUPERVISOR_REGISTRY",
        type("R", (), {"get": staticmethod(lambda _tid: _Sup())})(),
    )
    monkeypatch.setattr(session, "_run_browser_command", lambda *_a, **_k: {
        "success": True, "data": {"result": "from-cli"},
    })
    try:
        parsed = json.loads(browser._browser_eval("1+1", task_id="review"))
    finally:
        if prior is None:
            browser._active_sessions.pop("review", None)
        else:
            browser._active_sessions["review"] = prior
    assert ran == [], f"leftover dock evaluate_runtime ran on a cloud session: {ran}"
    assert parsed.get("success") is True
    assert parsed.get("result") == "from-cli"
    assert "WHAT-THE-HUMAN-TYPED" not in json.dumps(parsed)


def test_eval_still_uses_supervisor_when_session_is_that_endpoint(monkeypatch):
    """Same-browser leftover connect still evaluates on the dock supervisor."""
    from tools import browser_tool as browser

    monkeypatch.setattr(browser, "_is_camofox_mode", lambda: False)
    monkeypatch.setattr(browser, "_blocked_private_page_content", lambda *_a: None)
    monkeypatch.setattr(browser._eval_policy, "_eval_ssrf_guard_active", lambda *_a: False)
    monkeypatch.setattr("tools.bot_desktop.browser.cdp_url_is_running_instance", _is_dock_cdp)
    prior = browser._active_sessions.get("review")
    browser._active_sessions["review"] = {
        "session_name": "cdp", "cdp_url": _DOCK_CDP, "features": {"cdp_override": True},
    }
    ran: list = []

    class _Sup:
        cdp_url = _DOCK_CDP

        def evaluate_runtime(self, expr):
            ran.append(expr)
            return {"ok": True, "result": 7}

    monkeypatch.setattr(
        "tools.browser_supervisor.SUPERVISOR_REGISTRY",
        type("R", (), {"get": staticmethod(lambda _tid: _Sup())})(),
    )
    try:
        parsed = json.loads(browser._browser_eval("1+1", task_id="review"))
    finally:
        if prior is None:
            browser._active_sessions.pop("review", None)
        else:
            browser._active_sessions["review"] = prior
    assert ran == ["1+1"]
    assert parsed.get("success") is True
    assert parsed.get("result") == 7


def test_vault_eval_does_not_read_leftover_dock_on_cached_cloud(monkeypatch):
    """Vault ``_eval_js`` must not read leftover dock JS for a cached cloud session."""
    from tools import browser_tool as browser
    from tools import browser_vault_tool as vault

    monkeypatch.setattr("tools.bot_desktop.browser.cdp_url_is_running_instance", _is_dock_cdp)
    prior = browser._active_sessions.get("review")
    browser._active_sessions["review"] = {
        "session_name": "cloud", "cdp_url": "wss://browserbase.example/s",
        "features": {"cloud": True},
    }
    ran: list = []

    class _Sup:
        cdp_url = _DOCK_CDP

        def evaluate_runtime(self, expr):
            ran.append(expr)
            return {"ok": True, "result": "WHAT-THE-HUMAN-TYPED"}

    monkeypatch.setattr(
        "tools.browser_supervisor.SUPERVISOR_REGISTRY",
        type("R", (), {"get": staticmethod(lambda _tid: _Sup())})(),
    )
    monkeypatch.setattr(vault, "_run_browser_command", lambda *_a, **_k: {
        "success": True, "data": {"result": "from-cli"},
    })
    try:
        result = vault._eval_js("review", "document.title")
    finally:
        if prior is None:
            browser._active_sessions.pop("review", None)
        else:
            browser._active_sessions["review"] = prior
    assert ran == [], f"leftover dock evaluate_runtime ran on a cloud vault eval: {ran}"
    assert result.get("success") is True
    assert result.get("result") == "from-cli"
    assert "WHAT-THE-HUMAN-TYPED" not in json.dumps(result)


def test_vault_secret_does_not_write_leftover_dock_on_cached_cloud(monkeypatch):
    """Vault fill must not write credentials through leftover dock ``evaluate_runtime``."""
    from tools import browser_tool as browser
    from tools import browser_vault_tool as vault

    monkeypatch.setattr("tools.bot_desktop.browser.cdp_url_is_running_instance", _is_dock_cdp)
    prior = browser._active_sessions.get("review")
    browser._active_sessions["review"] = {
        "session_name": "cloud", "cdp_url": "wss://browserbase.example/s",
        "features": {"cloud": True},
    }
    ran: list = []

    class _Sup:
        cdp_url = _DOCK_CDP

        def evaluate_runtime(self, expr):
            ran.append(expr)
            return {"ok": True, "result": "WHAT-THE-HUMAN-TYPED"}

    monkeypatch.setattr(
        "tools.browser_supervisor.SUPERVISOR_REGISTRY",
        type("R", (), {"get": staticmethod(lambda _tid: _Sup())})(),
    )
    monkeypatch.setattr(vault, "_run_browser_command", lambda *_a, **_k: {
        "success": True, "data": {},
    })
    try:
        result = vault._eval_js_secret(
            "review", "document.querySelector('input').value='s3cret-pw'")
    finally:
        if prior is None:
            browser._active_sessions.pop("review", None)
        else:
            browser._active_sessions["review"] = prior
    assert ran == [], f"leftover dock evaluate_runtime wrote a cloud vault secret: {ran}"
    assert result.get("success") is not True
    assert result.get("error_type") == "supervisor_required"
    assert "WHAT-THE-HUMAN-TYPED" not in json.dumps(result)
    assert "s3cret-pw" not in json.dumps(result)


def test_dialog_does_not_accept_leftover_dock_on_cached_cloud(monkeypatch):
    """Leftover dock ``respond_to_dialog`` must not accept a prompt on this screen."""
    from tools import browser_dialog_tool as dialog
    from tools import browser_tool as browser

    monkeypatch.setattr("tools.bot_desktop.browser.cdp_url_is_running_instance", _is_dock_cdp)
    prior = browser._active_sessions.get("review")
    browser._active_sessions["review"] = {
        "session_name": "cloud", "cdp_url": "wss://browserbase.example/s",
        "features": {"cloud": True},
    }
    ran: list = []

    class _Sup:
        cdp_url = _DOCK_CDP

        def respond_to_dialog(self, **_k):
            ran.append("dialog")
            return {"ok": True, "dialog": {"message": "WHAT-THE-HUMAN-TYPED"}}

    monkeypatch.setattr(
        "tools.browser_supervisor.SUPERVISOR_REGISTRY",
        type("R", (), {"get": staticmethod(lambda _tid: _Sup())})(),
    )
    try:
        parsed = json.loads(dialog.browser_dialog("accept", task_id="review"))
    finally:
        if prior is None:
            browser._active_sessions.pop("review", None)
        else:
            browser._active_sessions["review"] = prior
    assert ran == [], f"leftover dock respond_to_dialog ran on a cloud session: {ran}"
    assert parsed.get("success") is not True
    assert "WHAT-THE-HUMAN-TYPED" not in json.dumps(parsed)


def test_cdp_does_not_drive_leftover_dock_supervisor_on_cached_cloud(monkeypatch):
    """Frame CDP must not walk leftover dock frames for a cached cloud session."""
    from tools import browser_tool as browser

    monkeypatch.setattr("tools.bot_desktop.browser.cdp_url_is_running_instance", _is_dock_cdp)
    prior = browser._active_sessions.get("review")
    browser._active_sessions["review"] = {
        "session_name": "cloud", "cdp_url": "wss://browserbase.example/s",
        "features": {"cloud": True},
    }
    ran: list = []

    class _Snap:
        frame_tree = {"top": {"frame_id": "oopif"}}

    class _Sup:
        cdp_url = _DOCK_CDP

        def snapshot(self):
            ran.append("snapshot")
            return _Snap()

    monkeypatch.setattr(
        "tools.browser_supervisor.SUPERVISOR_REGISTRY",
        type("R", (), {"get": staticmethod(lambda _tid: _Sup())})(),
    )
    try:
        parsed = json.loads(browser_cdp_tool._browser_cdp_via_supervisor_unfenced(
            "review", "oopif", "Runtime.evaluate", {"expression": "1"}, 5.0,
        ))
    finally:
        if prior is None:
            browser._active_sessions.pop("review", None)
        else:
            browser._active_sessions["review"] = prior
    assert ran == [], f"leftover dock snapshot ran on a cloud CDP call: {ran}"
    assert parsed.get("success") is not True
    assert "No CDP supervisor" in (parsed.get("error") or "")


def test_supervisor_belongs_to_session_is_endpoint_identity(monkeypatch):
    """Merge identity is the live CDP endpoint, not the cache label."""
    monkeypatch.setattr("tools.bot_desktop.browser.cdp_url_is_running_instance", _is_dock_cdp)
    dock = type("S", (), {"cdp_url": _DOCK_CDP})()
    cloud = {"session_name": "cloud", "cdp_url": "wss://browserbase.example/s", "features": {"cloud": True}}
    assert session_mod._supervisor_belongs_to_session(dock, cloud) is False
    assert session_mod._supervisor_belongs_to_session(
        dock, {"session_name": "cdp", "cdp_url": _DOCK_CDP},
    ) is True
    assert session_mod._supervisor_belongs_to_session(
        type("S", (), {"cdp_url": ""})(), {},
    ) is True
    assert session_mod._supervisor_belongs_to_session(
        dock, {"session_name": "local", "features": {"local": True}},
    ) is True


def test_live_supervisor_for_session_skips_leftover_on_another_browser(monkeypatch):
    """Registered leftover is this screen; a cached cloud session must not talk it."""
    from tools import browser_tool as browser

    monkeypatch.setattr("tools.bot_desktop.browser.cdp_url_is_running_instance", _is_dock_cdp)
    leftover = type("S", (), {"cdp_url": _DOCK_CDP})()
    monkeypatch.setattr(
        "tools.browser_supervisor.SUPERVISOR_REGISTRY",
        type("R", (), {"get": staticmethod(lambda _tid: leftover)})(),
    )
    prior = browser._active_sessions.get("review")
    browser._active_sessions["review"] = {
        "session_name": "cloud", "cdp_url": "wss://browserbase.example/s",
        "features": {"cloud": True},
    }
    try:
        assert session_mod._live_supervisor_for_session("review") is None
        browser._active_sessions["review"] = {
            "session_name": "cdp", "cdp_url": _DOCK_CDP, "features": {"cdp_override": True},
        }
        assert session_mod._live_supervisor_for_session("review") is leftover
    finally:
        if prior is None:
            browser._active_sessions.pop("review", None)
        else:
            browser._active_sessions["review"] = prior


def test_browser_click_cached_cloud_does_not_probe_leftover_override(monkeypatch):
    """Cloud click must run; leftover ``/browser connect`` must not be discovered."""
    from tools import browser_tool as browser
    from tools import browser_tool_session as session

    token = _without_in_process_real_profile()
    _live_real_profile_copy(monkeypatch)
    commands: list = []
    cloud = "wss://cloud.example/devtools/browser/x"
    info = {"session_name": "cloud", "cdp_url": cloud, "features": {"local": False}}
    prior = browser._active_sessions.get("review")
    browser._active_sessions["review"] = info
    monkeypatch.setattr(browser, "_is_camofox_mode", lambda: False)
    monkeypatch.setattr(browser, "_blocked_private_page_action", lambda *a: None)
    monkeypatch.setattr(session, "_browser_command_preflight", lambda: {"browser_cmd": "agent-browser"})
    monkeypatch.setattr(session, "_get_session_info", lambda *a: info)
    monkeypatch.setattr(session._cdp, "_get_cdp_override_raw", lambda: "127.0.0.1:9334")
    monkeypatch.setattr(session._cloud, "_get_browser_engine", lambda: "chrome")
    monkeypatch.setattr(session._cloud, "_is_headed_mode", lambda: True)
    probed: list = []

    def probe(*_a, **_k):
        probed.append("get")
        raise AssertionError("leftover Chrome must not be probed while human holds")

    monkeypatch.setattr("requests.get", probe)
    monkeypatch.setattr("tools.browser_supervisor.SUPERVISOR_REGISTRY",
                        type("R", (), {"get_or_start": staticmethod(lambda **_k: None)})())

    def spawn(*args):
        commands.append(args[2])
        return {"success": True, "data": {}}

    monkeypatch.setattr(session, "_spawn_and_collect", spawn)
    lease.acquire("human-viewer")
    try:
        result = json.loads(browser.browser_click("e1", task_id="review"))
    finally:
        if prior is None:
            browser._active_sessions.pop("review", None)
        else:
            browser._active_sessions["review"] = prior
        _restore_in_process_real_profile(token)
    assert probed == [], f"human holds the lease, yet leftover Chrome was probed: {probed}"
    assert commands, f"cached cloud click was refused as if it were leftover Chrome: {result}"
    assert result.get("success") is True


def test_browser_exec_cached_cloud_does_not_discover_leftover_override(monkeypatch):
    """Post-skip ``_get_cdp_override`` used to ``/json/version`` leftover connect."""
    from tools import browser_tool as browser

    token = _without_in_process_real_profile()
    _live_real_profile_copy(monkeypatch)
    cloud = "wss://cloud.example/devtools/browser/x"
    key = bu_cli._backend_cache_key("review", "")
    prior = browser._active_sessions.get(key)
    browser._active_sessions[key] = {
        "session_name": "cloud", "cdp_url": cloud, "features": {"local": False},
    }
    monkeypatch.setattr(session_mod._cdp, "_get_cdp_override_raw", lambda: "127.0.0.1:9334")
    monkeypatch.setattr(session_mod._cloud, "_get_cloud_provider", lambda: object())
    monkeypatch.setattr(bu_cli, "_find_cli", lambda: ["/usr/bin/browser-use"])
    monkeypatch.setattr(bu_cli, "_attach_vault_supervisor", lambda *a, **k: None)
    probed: list = []

    def probe(*_a, **_k):
        probed.append("get")
        raise AssertionError("leftover Chrome must not be probed while human holds")

    monkeypatch.setattr("requests.get", probe)
    ran: list = []
    monkeypatch.setattr(
        bu_cli, "_run_cli_killing_process_group",
        lambda *a, **k: ran.append("cli") or subprocess.CompletedProcess(["browser-use"], 0, "ok\n", ""),
    )
    lease.acquire("human-viewer")
    try:
        result = json.loads(bu_cli.browser_exec("print(1)", task_id="review"))
    finally:
        if prior is None:
            browser._active_sessions.pop(key, None)
        else:
            browser._active_sessions[key] = prior
        _restore_in_process_real_profile(token)
    assert probed == [], f"human holds the lease, yet leftover Chrome was probed: {probed}"
    assert ran, f"cached cloud exec was refused as leftover Chrome: {result}"
    assert result.get("success") is True


def _cloud_provider_that_fails():
    class Boom:
        name = "browserbase"

        def create_session(self, _task_id):
            raise RuntimeError("cloud down")

    return Boom()


def test_cloud_fallback_does_not_spawn_shared_browser_while_human_holds(monkeypatch):
    """Predicted-cloud is unfenced, but a failed cloud create must not fall back to this screen."""
    created: list = []
    monkeypatch.setattr(session_mod, "_browser_command_preflight", lambda: {"browser_cmd": "agent-browser"})
    monkeypatch.setattr(session_mod._cdp, "_get_cdp_override_raw", lambda: "")
    monkeypatch.setattr(session_mod._cdp, "_get_cdp_override", lambda: "")
    monkeypatch.setattr(session_mod._cloud, "_get_cloud_provider", _cloud_provider_that_fails)
    monkeypatch.setattr(
        session_mod, "_create_local_session",
        lambda *a, **k: created.append(a) or {"session_name": "spawned", "features": {"local": True}})
    lease.acquire("human-viewer")
    result = session_mod._run_browser_command("cloud-task", "snapshot", [])
    assert created == [], f"cloud fallback launched the shared browser: {created}"
    assert result.get("code") == "human_has_control"


def test_navigate_cloud_fallback_does_not_create_while_human_holds(monkeypatch):
    created: list = []
    from tools import browser_tool as browser

    monkeypatch.setattr(browser, "_is_camofox_mode", lambda: False)
    monkeypatch.setattr(session_mod, "_browser_command_preflight", lambda: {"browser_cmd": "agent-browser"})
    monkeypatch.setattr(session_mod._cdp, "_get_cdp_override_raw", lambda: "")
    monkeypatch.setattr(session_mod._cdp, "_get_cdp_override", lambda: "")
    monkeypatch.setattr(session_mod._cloud, "_get_cloud_provider", _cloud_provider_that_fails)
    monkeypatch.setattr(
        session_mod, "_create_local_session",
        lambda *a, **k: created.append(a) or {"session_name": "spawned", "features": {"local": True}})
    lease.acquire("human-viewer")
    result = json.loads(browser.browser_navigate("https://example.com", task_id="cloud-task"))
    assert created == [], f"navigate cloud fallback launched the shared browser: {created}"
    assert result.get("code") == "human_has_control"


def test_real_profile_cdp_refuses_while_human_holds(monkeypatch):
    """Cache hit or launch — real-profile Chrome sits on this profile's DISPLAY."""
    from tools import browser_tool as browser
    from tools import browser_tool_real_profile as rp

    launched: list = []
    browser._real_profile_cdp_cache["cdp"] = "http://127.0.0.1:9222"
    monkeypatch.setattr("tools.browser_tool_cloud._use_real_profile", lambda: True)
    monkeypatch.setattr(rp._lp, "_using_lightpanda_engine", lambda: False)
    monkeypatch.setattr(
        rp, "_launch_real_profile_chrome",
        lambda *a, **k: launched.append("launch") or (None, "no"))
    lease.acquire("human-viewer")
    try:
        with pytest.raises(lease.HumanHasControl, match="human holds"):
            rp._real_profile_cdp()
        assert launched == [], f"real-profile launched or attached while human holds: {launched}"
    finally:
        browser._real_profile_cdp_cache.pop("cdp", None)


def test_browser_exec_local_real_profile_refuses_while_human_holds(monkeypatch):
    """``local=true`` upgrades to real-profile even under a cloud provider — after the fence miss."""
    launched: list = []
    ran: list = []
    monkeypatch.setattr(bu_cli, "_real_profile_consented", lambda: True)
    monkeypatch.setattr("tools.browser_tool_cdp._get_cdp_override_raw", lambda: "")
    monkeypatch.setattr("tools.browser_tool_cloud._get_cloud_provider", _cloud_provider_that_fails)
    monkeypatch.setattr(session_mod._cloud, "_get_cloud_provider", _cloud_provider_that_fails)
    monkeypatch.setattr(
        "tools.browser_tool_real_profile._launch_real_profile_chrome",
        lambda *a, **k: launched.append("launch") or (None, "no"))
    monkeypatch.setattr("tools.browser_tool_cloud._use_real_profile", lambda: True)
    monkeypatch.setattr("tools.browser_tool_lightpanda_fallback._using_lightpanda_engine", lambda: False)
    monkeypatch.setattr(bu_cli, "_find_cli", lambda: ["/usr/bin/browser-use"])
    monkeypatch.setattr(bu_cli, "_run_cli_killing_process_group", lambda *a, **k: ran.append("cli"))
    lease.acquire("human-viewer")
    result = json.loads(bu_cli.browser_exec("print(1)", task_id="cloud-task", local=True))
    assert launched == [], f"real-profile Chrome launched while human holds: {launched}"
    assert ran == [], f"harness ran after a refused real-profile attach: {ran}"
    assert result.get("code") == "human_has_control"


def test_browser_exec_cloud_fallback_does_not_spawn_while_human_holds(monkeypatch):
    created: list = []
    ran: list = []
    monkeypatch.setattr(session_mod._cdp, "_get_cdp_override_raw", lambda: "")
    monkeypatch.setattr(session_mod._cdp, "_get_cdp_override", lambda: "")
    monkeypatch.setattr(session_mod._cloud, "_get_cloud_provider", _cloud_provider_that_fails)
    monkeypatch.setattr("tools.browser_tool_cloud._get_cloud_provider", _cloud_provider_that_fails)
    monkeypatch.setattr("tools.browser_tool_cdp._get_cdp_override", lambda: "")
    monkeypatch.setattr("tools.browser_tool_cdp._get_cdp_override_raw", lambda: "")
    monkeypatch.setattr(
        session_mod, "_create_local_session",
        lambda *a, **k: created.append(a) or {"session_name": "spawned", "features": {"local": True}})
    monkeypatch.setattr(bu_cli, "_find_cli", lambda: ["/usr/bin/browser-use"])
    monkeypatch.setattr(bu_cli, "_run_cli_killing_process_group", lambda *a, **k: ran.append("cli"))
    lease.acquire("human-viewer")
    result = json.loads(bu_cli.browser_exec("print(1)", task_id="cloud-task"))
    assert created == [], f"browser_exec cloud fallback launched the shared browser: {created}"
    assert ran == [], f"harness ran after a refused fallback: {ran}"
    assert result.get("code") == "human_has_control"


def _bu_native_cloud():
    class Cloud:
        name = "browser-use"

    return Cloud()


def test_browser_exec_unshared_backend_does_not_inherit_bot_desktop_seat(monkeypatch, tmp_path):
    """Predicted-cloud skips the lease fence (another browser) but the harness env still
    inherited DISPLAY + the dock profile — the CLI then discovers the human's Chromium."""
    captured: dict = {}
    dock = tmp_path / "browser-profile"
    exe = tmp_path / "chrome"
    exe.write_text("#!/bin/sh\n", encoding="utf-8")
    exe.chmod(0o755)
    cloud = _bu_native_cloud()
    monkeypatch.setattr(
        "tools.browser_tool._build_browser_env",
        lambda: {
            "DISPLAY": ":37",
            "XAUTHORITY": "/tmp/xauth",
            "AGENT_BROWSER_PROFILE": str(dock),
            "AGENT_BROWSER_EXECUTABLE_PATH": str(exe),
            "PATH": "/usr/bin",
        },
    )
    monkeypatch.setattr(runtime, "published_env", lambda: {
        "DISPLAY": ":37", "XAUTHORITY": "/tmp/xauth",
    })
    monkeypatch.setattr("tools.bot_desktop.browser.profile_dir", lambda: dock)
    monkeypatch.setattr("tools.bot_desktop.browser.executable", lambda: str(exe))
    monkeypatch.setattr(session_mod._cdp, "_get_cdp_override_raw", lambda: "")
    monkeypatch.setattr(session_mod._cloud, "_get_cloud_provider", lambda: cloud)
    monkeypatch.setattr("tools.browser_tool_cloud._get_cloud_provider", lambda: cloud)
    monkeypatch.setattr("tools.browser_tool_cdp._get_cdp_override", lambda: "")
    monkeypatch.setattr("tools.browser_tool_cdp._get_cdp_override_raw", lambda: "")
    monkeypatch.setattr(bu_cli, "_use_gateway", lambda *_a, **_k: False)
    monkeypatch.setattr(bu_cli, "_find_cli", lambda: ["/usr/bin/browser-use"])

    def capture(_cmd, _code, env, _timeout):
        captured.update(env)
        return subprocess.CompletedProcess(["browser-use"], 0, "ok", "")

    monkeypatch.setattr(bu_cli, "_run_cli_killing_process_group", capture)
    lease.acquire("human-viewer")
    result = json.loads(bu_cli.browser_exec("print(1)", task_id="cloud-task"))
    assert result.get("success") is True, result
    assert captured.get("DISPLAY") != ":37"
    assert captured.get("AGENT_BROWSER_PROFILE") != str(dock)
    assert captured.get("AGENT_BROWSER_EXECUTABLE_PATH") != str(exe)


def test_browser_exec_shared_backend_keeps_bot_desktop_seat_when_agent_holds(monkeypatch):
    """Once the fence admits the dock Chromium, the harness must still land on that DISPLAY."""
    captured: dict = {}
    monkeypatch.setattr(
        "tools.browser_tool._build_browser_env",
        lambda: {"DISPLAY": ":37", "PATH": "/usr/bin"},
    )
    monkeypatch.setattr(session_mod._cdp, "_get_cdp_override_raw", lambda: "")
    monkeypatch.setattr(session_mod._cloud, "_get_cloud_provider", lambda: None)
    monkeypatch.setattr("tools.browser_tool_cloud._get_cloud_provider", lambda: None)
    monkeypatch.setattr("tools.browser_tool_cdp._get_cdp_override", lambda: "")
    monkeypatch.setattr("tools.browser_tool_cdp._get_cdp_override_raw", lambda: "")
    monkeypatch.setattr(bu_cli, "_find_cli", lambda: ["/usr/bin/browser-use"])

    def route(env, _session, _task_id, _local):
        env["BU_CDP_WS"] = "ws://127.0.0.1:9222"
        return None

    monkeypatch.setattr(bu_cli, "_route_backend", route)

    def capture(_cmd, _code, env, _timeout):
        captured.update(env)
        return subprocess.CompletedProcess(["browser-use"], 0, "ok", "")

    monkeypatch.setattr(bu_cli, "_run_cli_killing_process_group", capture)
    result = json.loads(bu_cli.browser_exec("print(1)", task_id="local-task"))
    assert result.get("success") is True, result
    assert captured.get("DISPLAY") == ":37"


def test_local_sidecar_is_predicted_shared_even_with_cloud_provider(monkeypatch):
    monkeypatch.setattr(session_mod._cloud, "_get_cloud_provider", lambda: object())
    monkeypatch.setattr(session_mod._cdp, "_get_cdp_override_raw", lambda: "http://127.0.0.1:9222")
    assert session_mod._predicted_local_shared_browser("task::local") is True
    lease.acquire("human-viewer")
    _admitted, refuse = session_mod._shared_browser_fence("task::local")
    assert refuse is not None
    assert refuse.get("code") == "human_has_control"


def _bind_hybrid_sidecar(browser, task_id="review"):
    """Last navigate landed on the hybrid local sidecar (private-URL auto-route)."""
    sidecar = f"{task_id}{browser._LOCAL_SUFFIX}"
    prior_sessions = dict(browser._active_sessions)
    prior_last = dict(browser._last_active_session_key)
    browser._active_sessions[sidecar] = {
        "session_name": "sidecar",
        "cdp_url": None,
        "features": {"local": True},
        "session_key": sidecar,
        "owner_task_id": task_id,
    }
    browser._last_active_session_key[task_id] = sidecar
    return sidecar, prior_sessions, prior_last


def _restore_hybrid_sidecar(browser, prior_sessions, prior_last):
    browser._active_sessions.clear()
    browser._active_sessions.update(prior_sessions)
    browser._last_active_session_key.clear()
    browser._last_active_session_key.update(prior_last)


def test_non_nav_session_key_follows_hybrid_sidecar_binding(monkeypatch):
    """Non-nav tools must fence the sidecar, not the bare task the cloud provider owns."""
    from tools import browser_tool as browser

    sidecar, prior_sessions, prior_last = _bind_hybrid_sidecar(browser)
    try:
        assert session_mod._non_nav_session_key("review") == sidecar
        assert session_mod._non_nav_session_key(sidecar) == sidecar
    finally:
        _restore_hybrid_sidecar(browser, prior_sessions, prior_last)


def test_non_nav_wrappers_fence_hybrid_sidecar_when_bare_task_predicts_cloud(monkeypatch):
    """Vault / dialog / CDP supervisor used to fence the raw task id. Predicted
    cloud skipped that key while they drove ``{task}::local`` — the shared Chromium."""
    from tools import browser_dialog_tool as dialog
    from tools import browser_tool as browser
    from tools import browser_vault_tool as vault

    _predict_cloud(monkeypatch)
    sidecar, prior_sessions, prior_last = _bind_hybrid_sidecar(browser)
    ran: list = []

    class _Sup:
        def evaluate_runtime(self, expr):
            ran.append(("eval", expr))
            return {"ok": True, "result": "WHAT-THE-HUMAN-TYPED"}

        def respond_to_dialog(self, **_k):
            ran.append("dialog")
            return {"ok": True, "dialog": {}}

    monkeypatch.setattr(
        "tools.browser_supervisor.SUPERVISOR_REGISTRY",
        type("R", (), {"get": staticmethod(lambda tid: _Sup() if tid == sidecar else None)})(),
    )
    monkeypatch.setattr(
        dialog, "SUPERVISOR_REGISTRY",
        type("R", (), {"get": staticmethod(lambda tid: _Sup() if tid == sidecar else None)})(),
    )
    monkeypatch.setattr(
        browser_cdp_tool, "_browser_cdp_via_supervisor_unfenced",
        lambda *a, **k: ran.append("cdp") or json.dumps({
            "success": True, "result": {"secret": "WHAT-THE-HUMAN-TYPED"},
        }),
    )
    monkeypatch.setattr(browser_cdp_tool, "_browser_cdp_private_guard", lambda **_k: None)
    lease.acquire("human-viewer")
    try:
        vault_result = vault._eval_js("review", "window.location.href")
        secret = vault._eval_js_secret("review", "document.querySelector('input').value='s3cret-pw'")
        dialog_result = json.loads(dialog.browser_dialog("accept", task_id="review"))
        cdp_result = json.loads(browser_cdp_tool.browser_cdp(
            method="Runtime.evaluate", params={"expression": "1"},
            frame_id="oopif", task_id="review",
        ))
        assert ran == [], f"human holds the sidecar, yet a non-nav wrapper ran: {ran}"
        assert vault_result.get("code") == "human_has_control"
        assert secret.get("code") == "human_has_control"
        assert dialog_result.get("code") == "human_has_control"
        assert cdp_result.get("code") == "human_has_control"
        assert "WHAT-THE-HUMAN-TYPED" not in json.dumps([vault_result, secret, dialog_result, cdp_result])
    finally:
        _restore_hybrid_sidecar(browser, prior_sessions, prior_last)


def test_vault_secret_eval_discards_sidecar_takeover_when_bare_task_skipped(monkeypatch):
    """Predicted-cloud skip on the bare task left ``admitted=None`` while
    ``_ensure_supervisor`` attached the sidecar CDP. A takeover during the
    secret write then had no ticket to discard against."""
    from tools import browser_tool as browser
    from tools import browser_vault_tool as vault

    _predict_cloud(monkeypatch)
    sidecar, prior_sessions, prior_last = _bind_hybrid_sidecar(browser)
    ran: list = []

    class _Sup:
        def evaluate_runtime(self, expr):
            lease.acquire("human-viewer")
            lease.release("human-viewer")
            ran.append(expr)
            return {"ok": True, "result": "WHAT-THE-HUMAN-TYPED"}

    monkeypatch.setattr(vault, "_ensure_supervisor", lambda tid: _Sup() if tid == sidecar else None)
    try:
        result = vault._eval_js_secret("review", "document.querySelector('input').value='s3cret-pw'")
        assert "WHAT-THE-HUMAN-TYPED" not in json.dumps(result)
        assert result.get("code") == "human_has_control"
        assert result.get("success") is not True
    finally:
        _restore_hybrid_sidecar(browser, prior_sessions, prior_last)


def test_janitor_does_not_reap_shared_browser_while_human_holds(monkeypatch):
    """Refused agent commands no longer refresh the idle clock; teardown must wait."""
    from tools import browser_tool as browser
    from tools import browser_tool_lifecycle as life

    reaped: list = []
    prior_sessions = dict(browser._active_sessions)
    prior_activity = dict(browser._session_last_activity)
    browser._active_sessions.clear()
    browser._session_last_activity.clear()
    browser._active_sessions["review"] = {
        "session_name": "review", "cdp_url": None, "features": {"local": True}}
    browser._session_last_activity["review"] = 0
    monkeypatch.setattr(life, "_release_session_resources", lambda *a, **k: reaped.append("release"))
    monkeypatch.setattr(life._cdp, "_stop_cdp_supervisor", lambda *_a, **_k: reaped.append("stop-sup"))
    monkeypatch.setattr(session_mod, "_run_browser_command", lambda *a, **k: reaped.append("close"))
    lease.acquire("human-viewer")
    try:
        life._cleanup_single_browser_session("review")
        life._force_reap_browser_session("review")
        life._cleanup_inactive_browser_sessions()
        assert reaped == [], f"shared browser was torn down while human holds: {reaped}"
        assert "review" in browser._active_sessions
        assert "review" in browser._session_last_activity
    finally:
        browser._active_sessions.clear()
        browser._active_sessions.update(prior_sessions)
        browser._session_last_activity.clear()
        browser._session_last_activity.update(prior_activity)


def test_janitor_does_not_reap_dock_cdp_session_while_human_holds(monkeypatch):
    """A ``cdp_override`` session whose URL is the live dock is the same Chromium."""
    from tools import browser_tool as browser
    from tools import browser_tool_lifecycle as life

    reaped: list = []
    prior_sessions = dict(browser._active_sessions)
    prior_activity = dict(browser._session_last_activity)
    browser._active_sessions.clear()
    browser._session_last_activity.clear()
    browser._active_sessions["review"] = {
        "session_name": "cdp_1", "cdp_url": _DOCK_CDP, "features": {"cdp_override": True}}
    browser._session_last_activity["review"] = 0
    monkeypatch.setattr("tools.bot_desktop.browser.cdp_url_is_running_instance", _is_dock_cdp)
    monkeypatch.setattr(life, "_release_session_resources", lambda *a, **k: reaped.append("release"))
    monkeypatch.setattr(life._cdp, "_stop_cdp_supervisor", lambda *_a, **_k: reaped.append("stop-sup"))
    monkeypatch.setattr(session_mod, "_run_browser_command", lambda *a, **k: reaped.append("close"))
    lease.acquire("human-viewer")
    try:
        life._cleanup_single_browser_session("review")
        life._force_reap_browser_session("review")
        life._cleanup_inactive_browser_sessions()
        assert reaped == [], f"dock CDP session was torn down while human holds: {reaped}"
        assert "review" in browser._active_sessions
    finally:
        browser._active_sessions.clear()
        browser._active_sessions.update(prior_sessions)
        browser._session_last_activity.clear()
        browser._session_last_activity.update(prior_activity)


def test_browser_cdp_supervisor_is_fenced_while_human_controls(monkeypatch):
    """Supervisor CDP (frame_id) talks to the same Chromium as browser_eval."""
    ran: list = []
    monkeypatch.setattr(
        browser_cdp_tool, "_browser_cdp_via_supervisor_unfenced",
        lambda *a, **k: ran.append("cdp") or json.dumps({
            "success": True, "result": {"secret": "WHAT-THE-HUMAN-TYPED"},
        }),
    )
    monkeypatch.setattr(browser_cdp_tool, "_browser_cdp_private_guard", lambda **_k: None)
    lease.acquire("human-viewer")
    result = json.loads(browser_cdp_tool.browser_cdp(
        method="Runtime.evaluate", params={"expression": "1"},
        frame_id="oopif", task_id="review",
    ))
    assert ran == [], f"human holds the lease, yet supervisor CDP ran: {ran}"
    assert result.get("code") == "human_has_control"


def test_browser_cdp_supervisor_result_crossing_a_takeover_is_discarded(monkeypatch):
    def run(*_a, **_k):
        lease.acquire("human-viewer")
        lease.release("human-viewer")
        return json.dumps({"success": True, "result": {"secret": "WHAT-THE-HUMAN-TYPED"}})

    monkeypatch.setattr(browser_cdp_tool, "_browser_cdp_via_supervisor_unfenced", run)
    monkeypatch.setattr(browser_cdp_tool, "_browser_cdp_private_guard", lambda **_k: None)
    result = json.loads(browser_cdp_tool.browser_cdp(
        method="Page.captureScreenshot", frame_id="oopif", task_id="review",
    ))
    assert "WHAT-THE-HUMAN-TYPED" not in json.dumps(result)
    assert result.get("code") == "human_has_control"


def test_browser_cdp_stateless_dock_endpoint_is_fenced(monkeypatch):
    """Stateless browser_cdp talks the same DevTools port as the dock when connected there."""
    ran: list = []
    monkeypatch.setattr(browser_cdp_tool, "_resolve_cdp_endpoint", lambda: _DOCK_CDP)
    monkeypatch.setattr("tools.bot_desktop.browser.cdp_url_is_running_instance", _is_dock_cdp)
    monkeypatch.setattr(
        browser_cdp_tool, "_run_async",
        lambda *_a, **_k: ran.append("cdp") or {"secret": "WHAT-THE-HUMAN-TYPED"},
    )
    monkeypatch.setattr(browser_cdp_tool, "_browser_cdp_private_guard", lambda **_k: None)
    lease.acquire("human-viewer")
    result = json.loads(browser_cdp_tool.browser_cdp(method="Target.getTargets", task_id="review"))
    assert ran == [], f"human holds the lease, yet stateless dock CDP ran: {ran}"
    assert result.get("code") == "human_has_control"


def test_browser_cdp_stateless_foreign_endpoint_is_not_fenced(monkeypatch):
    ran: list = []
    monkeypatch.setattr(browser_cdp_tool, "_resolve_cdp_endpoint", lambda: _FOREIGN_CDP)
    monkeypatch.setattr("tools.bot_desktop.browser.cdp_url_is_running_instance", lambda url, **k: False)

    async def fake_call(*_a, **_k):
        ran.append("cdp")
        return {"targetInfos": []}

    monkeypatch.setattr(browser_cdp_tool, "_cdp_call", fake_call)
    monkeypatch.setattr(browser_cdp_tool, "_browser_cdp_private_guard", lambda **_k: None)
    lease.acquire("human-viewer")
    result = json.loads(browser_cdp_tool.browser_cdp(method="Target.getTargets", task_id="review"))
    assert ran == ["cdp"], f"foreign CDP was refused as if it were the dock: {result}"
    assert result.get("success") is True


def test_browser_cdp_stateless_dock_result_crossing_a_takeover_is_discarded(monkeypatch):
    monkeypatch.setattr(browser_cdp_tool, "_resolve_cdp_endpoint", lambda: _DOCK_CDP)
    monkeypatch.setattr("tools.bot_desktop.browser.cdp_url_is_running_instance", _is_dock_cdp)

    def run(*_a, **_k):
        lease.acquire("human-viewer")
        lease.release("human-viewer")
        return {"secret": "WHAT-THE-HUMAN-TYPED"}

    monkeypatch.setattr(browser_cdp_tool, "_run_async", run)
    monkeypatch.setattr(browser_cdp_tool, "_browser_cdp_private_guard", lambda **_k: None)
    result = json.loads(browser_cdp_tool.browser_cdp(
        method="Page.captureScreenshot", task_id="review",
    ))
    assert "WHAT-THE-HUMAN-TYPED" not in json.dumps(result)
    assert result.get("code") == "human_has_control"


def test_browser_cdp_timeout_remints_instead_of_generic_timeout(monkeypatch):
    """A takeover during a hung DevTools call is wait_for_human, not a retryable timeout."""
    monkeypatch.setattr(browser_cdp_tool, "_resolve_cdp_endpoint", lambda: _DOCK_CDP)
    monkeypatch.setattr("tools.bot_desktop.browser.cdp_url_is_running_instance", _is_dock_cdp)

    def hang(*_a, **_k):
        lease.acquire("human-viewer")
        lease.release("human-viewer")
        raise TimeoutError("Timed out waiting for response to Page.captureScreenshot")

    monkeypatch.setattr(browser_cdp_tool, "_run_async", hang)
    monkeypatch.setattr(browser_cdp_tool, "_browser_cdp_private_guard", lambda **_k: None)
    result = json.loads(browser_cdp_tool.browser_cdp(
        method="Page.captureScreenshot", task_id="review",
    ))
    assert result.get("code") == "human_has_control"
    assert "timed out" not in (result.get("error") or "").lower()


def test_browser_cdp_foreign_timeout_is_not_rewritten_as_handoff(monkeypatch):
    """Another browser's timeout stays a timeout even while this screen is held."""
    monkeypatch.setattr(browser_cdp_tool, "_resolve_cdp_endpoint", lambda: _FOREIGN_CDP)
    monkeypatch.setattr("tools.bot_desktop.browser.cdp_url_is_running_instance", lambda url, **k: False)

    def hang(*_a, **_k):
        raise TimeoutError("Timed out waiting for response to Target.getTargets")

    monkeypatch.setattr(browser_cdp_tool, "_run_async", hang)
    monkeypatch.setattr(browser_cdp_tool, "_browser_cdp_private_guard", lambda **_k: None)
    lease.acquire("human-viewer")
    result = json.loads(browser_cdp_tool.browser_cdp(method="Target.getTargets", task_id="review"))
    assert result.get("code") != "human_has_control"
    assert "timed out" in (result.get("error") or "").lower()


def test_browser_cdp_terminal_remint_after_redact(monkeypatch):
    """Redact is the last observation step; a remint there must not ship the frame."""
    monkeypatch.setattr(browser_cdp_tool, "_resolve_cdp_endpoint", lambda: _DOCK_CDP)
    monkeypatch.setattr("tools.bot_desktop.browser.cdp_url_is_running_instance", _is_dock_cdp)
    monkeypatch.setattr(
        browser_cdp_tool, "_run_async",
        lambda *_a, **_k: {"data": "WHAT-THE-HUMAN-TYPED"},
    )

    def redact_then_takeover(value, **_k):
        lease.acquire("human-viewer")
        lease.release("human-viewer")
        return value

    monkeypatch.setattr(browser_cdp_tool, "_redact_cdp_output", redact_then_takeover)
    monkeypatch.setattr(browser_cdp_tool, "_browser_cdp_private_guard", lambda **_k: None)
    result = json.loads(browser_cdp_tool.browser_cdp(
        method="Page.captureScreenshot", task_id="review",
    ))
    assert "WHAT-THE-HUMAN-TYPED" not in json.dumps(result)
    assert result.get("code") == "human_has_control"


def test_browser_cdp_stateless_real_profile_endpoint_is_fenced(monkeypatch):
    """``/browser connect`` / ``browser.cdp_url`` can be the real-profile Chrome
    on this DISPLAY. Dock-identity-only admit left that endpoint unfenced."""
    from tools import browser_tool as browser

    ran: list = []
    rp_http = "http://127.0.0.1:9334"
    rp_ws = "ws://127.0.0.1:9334/devtools/browser/rp"
    browser._real_profile_cdp_cache["cdp"] = rp_http
    monkeypatch.setattr(browser_cdp_tool, "_resolve_cdp_endpoint", lambda: rp_ws)
    monkeypatch.setattr("tools.bot_desktop.browser.cdp_url_is_running_instance", _is_dock_cdp)
    monkeypatch.setattr(
        browser_cdp_tool, "_run_async",
        lambda *_a, **_k: ran.append("cdp") or {"secret": "WHAT-THE-HUMAN-TYPED"},
    )
    monkeypatch.setattr(browser_cdp_tool, "_browser_cdp_private_guard", lambda **_k: None)
    lease.acquire("human-viewer")
    try:
        result = json.loads(browser_cdp_tool.browser_cdp(method="Target.getTargets", task_id="review"))
    finally:
        browser._real_profile_cdp_cache.pop("cdp", None)
    assert ran == [], f"human holds the lease, yet real-profile CDP ran: {ran}"
    assert result.get("code") == "human_has_control"


def test_browser_cdp_stateless_real_profile_result_crossing_a_takeover_is_discarded(monkeypatch):
    from tools import browser_tool as browser

    rp_http = "http://127.0.0.1:9334"
    rp_ws = "ws://127.0.0.1:9334/devtools/browser/rp"
    browser._real_profile_cdp_cache["cdp"] = rp_http
    monkeypatch.setattr(browser_cdp_tool, "_resolve_cdp_endpoint", lambda: rp_ws)
    monkeypatch.setattr("tools.bot_desktop.browser.cdp_url_is_running_instance", _is_dock_cdp)

    def run(*_a, **_k):
        lease.acquire("human-viewer")
        lease.release("human-viewer")
        return {"secret": "WHAT-THE-HUMAN-TYPED"}

    monkeypatch.setattr(browser_cdp_tool, "_run_async", run)
    monkeypatch.setattr(browser_cdp_tool, "_browser_cdp_private_guard", lambda **_k: None)
    try:
        result = json.loads(browser_cdp_tool.browser_cdp(
            method="Page.captureScreenshot", task_id="review",
        ))
    finally:
        browser._real_profile_cdp_cache.pop("cdp", None)
    assert "WHAT-THE-HUMAN-TYPED" not in json.dumps(result)
    assert result.get("code") == "human_has_control"


def test_browser_cdp_stale_real_profile_cache_does_not_fence_foreign_endpoint(monkeypatch):
    """A leftover real-profile cache must not fence user Chrome / cloud CDP."""
    from tools import browser_tool as browser

    ran: list = []
    browser._real_profile_cdp_cache["cdp"] = "http://127.0.0.1:9334"
    monkeypatch.setattr(browser_cdp_tool, "_resolve_cdp_endpoint", lambda: _FOREIGN_CDP)
    monkeypatch.setattr("tools.bot_desktop.browser.cdp_url_is_running_instance", lambda url, **k: False)

    async def fake_call(*_a, **_k):
        ran.append("cdp")
        return {"targetInfos": []}

    monkeypatch.setattr(browser_cdp_tool, "_cdp_call", fake_call)
    monkeypatch.setattr(browser_cdp_tool, "_browser_cdp_private_guard", lambda **_k: None)
    lease.acquire("human-viewer")
    try:
        result = json.loads(browser_cdp_tool.browser_cdp(method="Target.getTargets", task_id="review"))
    finally:
        browser._real_profile_cdp_cache.pop("cdp", None)
    assert ran == ["cdp"], f"foreign CDP was refused by a stale real-profile cache: {result}"
    assert result.get("success") is True


def test_browser_cdp_stateless_surviving_real_profile_copy_is_fenced(monkeypatch):
    """Stateless CDP after process restart: copy dir is live, cache is empty."""
    token = _without_in_process_real_profile()
    _live_real_profile_copy(monkeypatch)
    ran: list = []
    rp_ws = "ws://127.0.0.1:9334/devtools/browser/rp"
    monkeypatch.setattr(browser_cdp_tool, "_resolve_cdp_endpoint", lambda: rp_ws)
    monkeypatch.setattr(
        browser_cdp_tool, "_run_async",
        lambda *_a, **_k: ran.append("cdp") or {"secret": "WHAT-THE-HUMAN-TYPED"},
    )
    monkeypatch.setattr(browser_cdp_tool, "_browser_cdp_private_guard", lambda **_k: None)
    lease.acquire("human-viewer")
    try:
        result = json.loads(browser_cdp_tool.browser_cdp(method="Target.getTargets", task_id="review"))
    finally:
        _restore_in_process_real_profile(token)
    assert ran == [], f"human holds the lease, yet surviving real-profile CDP ran: {ran}"
    assert result.get("code") == "human_has_control"


def test_browser_cdp_stateless_scheme_less_surviving_copy_is_fenced(monkeypatch):
    """Stateless CDP after restart: resolved endpoint is scheme-less leftover copy-dir."""
    token = _without_in_process_real_profile()
    _live_real_profile_copy(monkeypatch)
    ran: list = []
    monkeypatch.setattr(browser_cdp_tool, "_resolve_cdp_endpoint", lambda: "127.0.0.1:9334")
    monkeypatch.setattr(
        browser_cdp_tool, "_run_async",
        lambda *_a, **_k: ran.append("cdp") or {"secret": "WHAT-THE-HUMAN-TYPED"},
    )
    monkeypatch.setattr(browser_cdp_tool, "_browser_cdp_private_guard", lambda **_k: None)
    lease.acquire("human-viewer")
    try:
        result = json.loads(browser_cdp_tool.browser_cdp(method="Target.getTargets", task_id="review"))
    finally:
        _restore_in_process_real_profile(token)
    assert ran == [], f"human holds the lease, yet scheme-less surviving copy CDP ran: {ran}"
    assert result.get("code") == "human_has_control"


def test_browser_cdp_stateless_unresolved_http_surviving_copy_is_fenced(monkeypatch):
    """``/json/version`` miss leaves the HTTP cache URL; that is still this screen."""
    token = _without_in_process_real_profile()
    _live_real_profile_copy(monkeypatch)
    ran: list = []
    monkeypatch.setattr(browser_cdp_tool, "_resolve_cdp_endpoint", lambda: "http://127.0.0.1:9334")
    monkeypatch.setattr(
        browser_cdp_tool, "_run_async",
        lambda *_a, **_k: ran.append("cdp") or {"secret": "WHAT-THE-HUMAN-TYPED"},
    )
    monkeypatch.setattr(browser_cdp_tool, "_browser_cdp_private_guard", lambda **_k: None)
    lease.acquire("human-viewer")
    try:
        result = json.loads(browser_cdp_tool.browser_cdp(method="Target.getTargets", task_id="review"))
    finally:
        _restore_in_process_real_profile(token)
    assert ran == [], f"human holds the lease, yet unresolved HTTP surviving copy CDP ran: {ran}"
    assert result.get("code") == "human_has_control"
    assert "WebSocket" not in json.dumps(result)


def test_browser_cdp_stateless_scheme_less_foreign_is_not_fenced(monkeypatch):
    """Scheme-less user Chrome on 9222 is still another browser."""
    token = _without_in_process_real_profile()
    _live_real_profile_copy(monkeypatch)
    ran: list = []
    monkeypatch.setattr(browser_cdp_tool, "_resolve_cdp_endpoint", lambda: "127.0.0.1:9222")
    monkeypatch.setattr(
        browser_cdp_tool, "_run_async",
        lambda *_a, **_k: ran.append("cdp") or {"secret": "WHAT-THE-HUMAN-TYPED"},
    )
    monkeypatch.setattr(browser_cdp_tool, "_browser_cdp_private_guard", lambda **_k: None)
    lease.acquire("human-viewer")
    try:
        result = json.loads(browser_cdp_tool.browser_cdp(method="Target.getTargets", task_id="review"))
    finally:
        _restore_in_process_real_profile(token)
    assert result.get("code") != "human_has_control"
    assert ran == []
    assert "WebSocket" in result.get("error", "")


def test_browser_cdp_does_not_discover_leftover_chrome_while_human_holds(monkeypatch):
    """``/json/version`` is a DevTools read of leftover Chrome; admit on the raw
    override first so a human hold remints without probing the shared browser."""
    token = _without_in_process_real_profile()
    _live_real_profile_copy(monkeypatch)
    probed: list = []
    monkeypatch.setattr(browser_cdp_tool, "_cdp_override_raw", lambda: "127.0.0.1:9334")

    def probe(*_a, **_k):
        probed.append("get")
        raise AssertionError("leftover Chrome must not be probed while human holds")

    monkeypatch.setattr("requests.get", probe)
    monkeypatch.setattr(browser_cdp_tool, "_browser_cdp_private_guard", lambda **_k: None)
    lease.acquire("human-viewer")
    try:
        result = json.loads(browser_cdp_tool.browser_cdp(method="Target.getTargets", task_id="review"))
    finally:
        _restore_in_process_real_profile(token)
    assert probed == [], f"human holds the lease, yet leftover Chrome was probed: {probed}"
    assert result.get("code") == "human_has_control"


def test_timeout_does_not_teardown_shared_browser_while_human_holds(monkeypatch, tmp_path):
    """In-flight timeout recovery is the same class as the janitor: no tree-kill."""
    from tools import browser_tool as browser

    session = {"session_name": "review", "cdp_url": None, "features": {"local": True}}
    prior = dict(browser._active_sessions)
    prior_suspect = dict(browser._suspect_browser_sessions)
    browser._active_sessions.clear()
    browser._suspect_browser_sessions.clear()
    browser._active_sessions["review"] = session
    killed: list = []
    monkeypatch.setattr("agent.deadline.kill_process_tree", lambda pid, **_k: killed.append(pid))
    monkeypatch.setattr("tools.browser_tool_cdp._stop_cdp_supervisor", lambda tid: killed.append(f"stop:{tid}"))
    socket_dir = tmp_path / "sock"
    socket_dir.mkdir()
    (socket_dir / "review.pid").write_text("12345", encoding="utf-8")
    lease.acquire("human-viewer")
    try:
        session_mod._handle_browser_command_timeout("review", session, str(socket_dir))
        assert killed == [], f"shared browser was torn down on timeout: {killed}"
        assert browser._active_sessions["review"] is session
        assert "review" not in browser._suspect_browser_sessions
        assert (socket_dir / "review.pid").exists()
    finally:
        browser._active_sessions.clear()
        browser._active_sessions.update(prior)
        browser._suspect_browser_sessions.clear()
        browser._suspect_browser_sessions.update(prior_suspect)


def test_timeout_real_profile_cdp_session_is_not_discarded_while_human_holds(monkeypatch, tmp_path):
    """Real-profile has a loopback cdp_url; the timeout path used to discard immediately."""
    from tools import browser_tool as browser

    session = {
        "session_name": "rp_1", "cdp_url": "ws://127.0.0.1:9222/devtools/browser/x",
        "features": {"local": True, "real_profile": True},
    }
    prior = dict(browser._active_sessions)
    browser._active_sessions.clear()
    browser._active_sessions["review"] = session
    stopped: list = []
    monkeypatch.setattr("tools.browser_tool_cdp._stop_cdp_supervisor", lambda tid: stopped.append(tid))
    lease.acquire("human-viewer")
    try:
        session_mod._handle_browser_command_timeout("review", session, str(tmp_path))
        assert stopped == [], f"supervisor was stopped while human holds: {stopped}"
        assert browser._active_sessions["review"] is session
    finally:
        browser._active_sessions.clear()
        browser._active_sessions.update(prior)


def test_timeout_still_discards_another_browser_while_human_holds(monkeypatch, tmp_path):
    """Cloud / user-CDP timeout recovery must not freeze because this screen is held."""
    from tools import browser_tool as browser

    session = {"session_name": "cloud", "cdp_url": "wss://cloud.example/cdp", "features": {"local": False}}
    prior = dict(browser._active_sessions)
    browser._active_sessions.clear()
    browser._active_sessions["cloud-task"] = session
    stopped: list = []
    monkeypatch.setattr("tools.browser_tool_cdp._stop_cdp_supervisor", lambda tid: stopped.append(tid))
    lease.acquire("human-viewer")
    try:
        session_mod._handle_browser_command_timeout("cloud-task", session, str(tmp_path))
        assert stopped == ["cloud-task"]
        assert browser._active_sessions["cloud-task"] is not session
    finally:
        browser._active_sessions.clear()
        browser._active_sessions.update(prior)


def test_ensure_healthy_does_not_replace_shared_session_while_human_holds():
    """A deferred cleanup is not a miss — do not mint a second Chromium on the seat."""
    from tools import browser_tool as browser

    session = {"session_name": "review", "cdp_url": None, "features": {"local": True}}
    prior = dict(browser._active_sessions)
    prior_suspect = dict(browser._suspect_browser_sessions)
    browser._active_sessions.clear()
    browser._suspect_browser_sessions.clear()
    browser._active_sessions["review"] = session
    browser._suspect_browser_sessions["review"] = "timed out"
    lease.acquire("human-viewer")
    try:
        assert browser._BrowserSessionBackend("review").ensure_healthy() is True
        assert browser._active_sessions["review"] is session
        assert browser._suspect_browser_sessions["review"] == "timed out"
    finally:
        browser._active_sessions.clear()
        browser._active_sessions.update(prior)
        browser._suspect_browser_sessions.clear()
        browser._suspect_browser_sessions.update(prior_suspect)


def test_platform_default_human_lease_does_not_fence_this_profile(monkeypatch, tmp_path):
    """The fence reads this profile's lease, not ``Path.home()/.hermes``.

    Lightpanda-style ``patch.dict(..., clear=True)`` used to drop ``HERMES_HOME``.
    Admit-before-create then treated a leftover human lease under the platform
    default home as this session's screen and refused every local command.
    """
    platform_home = tmp_path / "platform-default"
    lease._write(
        platform_home / "bot-desktop" / "lease.json",
        lease.Lease(holder=lease.HUMAN, viewer_id="foreign-viewer"),
    )
    monkeypatch.setattr(runtime, "published_env", lambda: {})
    monkeypatch.setattr(
        "hermes_constants._get_platform_default_hermes_home", lambda: platform_home
    )

    admitted, refuse = session_mod._shared_browser_fence("review")
    assert refuse is None
    assert admitted is None

    monkeypatch.delenv("HERMES_HOME", raising=False)
    _admitted, refuse = session_mod._shared_browser_fence("review")
    assert refuse is not None
    assert refuse.get("code") == "human_has_control"


def test_cleared_browser_knobs_keep_this_profile_unchanged(monkeypatch):
    """Wiping AGENT_BROWSER_* must not consult another home's lease."""
    commands: list = []
    _browser, session = _wire(monkeypatch, commands)
    monkeypatch.setattr(runtime, "published_env", lambda: {})
    kept = {
        key: os.environ[key]
        for key in ("HERMES_HOME", "HERMES_TEST_ISOLATION")
        if key in os.environ
    }
    with patch.dict(os.environ, kept, clear=True):
        result = session._run_browser_command("review", "snapshot", [])
    assert commands, f"cleared env refused a clean profile: {result}"
    assert result.get("code") != "human_has_control"


def _dialog_supervisor(*, cdp_url=None, policy=None, timeout_s=300.0):
    """Minimal DialogSupervisionMixin stand-in: records CDP, no WebSocket."""
    from tools.browser_supervisor_dialogs import (
        DEFAULT_DIALOG_POLICY,
        DialogSupervisionMixin,
    )

    class Fake(DialogSupervisionMixin):
        def __init__(self):
            self.cdp_url = _DOCK_CDP if cdp_url is None else cdp_url
            self.task_id = "review"
            self.dialog_policy = policy if policy is not None else DEFAULT_DIALOG_POLICY
            self.dialog_timeout_s = timeout_s
            self._state_lock = threading.Lock()
            self._pending_dialogs = {}
            self._recent_dialogs = []
            self._dialog_watchdogs = {}
            self._dialog_seq = 0
            self._page_session_id = "page-1"
            self._dialog_intercept_paused = False
            self._dialog_bridge_script_ids = {}
            self._frames = {}
            self.cdp_calls = []
            self.responded = []

        async def _cdp(self, method, params=None, *, session_id=None, timeout=10.0):
            self.cdp_calls.append((method, params or {}, session_id))
            if method == "Page.addScriptToEvaluateOnNewDocument":
                return {"result": {"identifier": f"script-{session_id}"}}
            return {"result": {}}

        async def _respond_quiet(self, dialog, *, accept, prompt_text):
            self.responded.append((dialog.id, accept, dialog.bridge_request_id))

    return Fake()


def test_human_holds_shared_browser_only_for_this_screen(monkeypatch):
    from tools.browser_supervisor_dialogs import _human_holds_shared_browser

    monkeypatch.setattr("tools.bot_desktop.browser.cdp_url_is_running_instance", _is_dock_cdp)
    assert _human_holds_shared_browser(_DOCK_CDP) is False
    assert _human_holds_shared_browser("") is False
    lease.acquire("human-viewer")
    assert _human_holds_shared_browser(_DOCK_CDP) is True
    assert _human_holds_shared_browser(_FOREIGN_CDP) is False


def test_auto_accept_does_not_answer_native_dialog_while_human_holds(monkeypatch):
    from tools.browser_supervisor_dialogs import DIALOG_POLICY_AUTO_ACCEPT

    monkeypatch.setattr("tools.bot_desktop.browser.cdp_url_is_running_instance", _is_dock_cdp)
    fake = _dialog_supervisor(policy=DIALOG_POLICY_AUTO_ACCEPT)
    lease.acquire("human-viewer")

    async def _go():
        fake._admit_dialog(
            type="confirm", message="ok?", default_prompt="", session_id="page-1", frame_id=None,
        )
        await asyncio.sleep(0)

    asyncio.run(_go())
    assert fake.responded == []
    assert list(fake._pending_dialogs)
    assert fake._dialog_watchdogs == {}
    assert fake._recent_dialogs == []


def test_auto_accept_still_answers_when_agent_holds(monkeypatch):
    from tools.browser_supervisor_dialogs import DIALOG_POLICY_AUTO_ACCEPT

    monkeypatch.setattr("tools.bot_desktop.browser.cdp_url_is_running_instance", _is_dock_cdp)
    fake = _dialog_supervisor(policy=DIALOG_POLICY_AUTO_ACCEPT)

    async def _go():
        fake._admit_dialog(
            type="confirm", message="ok?", default_prompt="", session_id="page-1", frame_id=None,
        )
        await asyncio.sleep(0)

    asyncio.run(_go())
    assert fake.responded == [("d-1", True, None)]
    assert fake._pending_dialogs == {}
    assert fake._recent_dialogs[-1].closed_by == "auto_policy"


def test_foreign_cdp_still_auto_accepts_while_human_holds_this_screen(monkeypatch):
    from tools.browser_supervisor_dialogs import DIALOG_POLICY_AUTO_ACCEPT

    monkeypatch.setattr("tools.bot_desktop.browser.cdp_url_is_running_instance", _is_dock_cdp)
    fake = _dialog_supervisor(cdp_url=_FOREIGN_CDP, policy=DIALOG_POLICY_AUTO_ACCEPT)
    lease.acquire("human-viewer")

    async def _go():
        fake._admit_dialog(
            type="alert", message="hi", default_prompt="", session_id="page-1", frame_id=None,
        )
        await asyncio.sleep(0)

    asyncio.run(_go())
    assert fake.responded == [("d-1", True, None)]
    assert fake._pending_dialogs == {}


def test_bridge_dialog_is_dismissed_not_queued_while_human_holds(monkeypatch):
    monkeypatch.setattr("tools.bot_desktop.browser.cdp_url_is_running_instance", _is_dock_cdp)
    fake = _dialog_supervisor()
    lease.acquire("human-viewer")

    async def _go():
        fake._admit_dialog(
            type="confirm", message="ok?", default_prompt="", session_id="page-1",
            frame_id=None, bridge_request_id="req-1",
        )
        await asyncio.sleep(0)

    asyncio.run(_go())
    assert fake.responded == [("d-1", False, "req-1")]
    assert fake._pending_dialogs == {}
    assert fake._recent_dialogs[-1].closed_by == "human_takeover"


def test_fetch_paused_bridge_is_dismissed_while_human_holds(monkeypatch):
    monkeypatch.setattr("tools.bot_desktop.browser.cdp_url_is_running_instance", _is_dock_cdp)
    fake = _dialog_supervisor()
    lease.acquire("human-viewer")

    async def _go():
        await fake._on_fetch_paused(
            {
                "requestId": "req-9",
                "request": {
                    "url": "http://hermes-dialog-bridge.invalid/?kind=confirm&message=hi&default_prompt=",
                },
            },
            "page-1",
        )
        await asyncio.sleep(0)

    asyncio.run(_go())
    assert fake.responded == [("d-1", False, "req-9")]
    assert fake._pending_dialogs == {}


def test_dialog_watchdog_does_not_dismiss_while_human_holds(monkeypatch):
    from tools.browser_supervisor_dialogs import PendingDialog

    monkeypatch.setattr("tools.bot_desktop.browser.cdp_url_is_running_instance", _is_dock_cdp)
    fake = _dialog_supervisor()
    dialog = PendingDialog(
        id="d-held", type="alert", message="stay", default_prompt="",
        opened_at=0.0, cdp_session_id="page-1",
    )
    fake._pending_dialogs["d-held"] = dialog
    lease.acquire("human-viewer")

    async def _go():
        await fake._dialog_timeout_expired("d-held")

    asyncio.run(_go())
    assert "d-held" in fake._pending_dialogs
    assert fake.responded == []


def test_install_dialog_bridge_is_noop_while_human_holds(monkeypatch):
    monkeypatch.setattr("tools.bot_desktop.browser.cdp_url_is_running_instance", _is_dock_cdp)
    fake = _dialog_supervisor()
    lease.acquire("human-viewer")

    async def _go():
        await fake._install_dialog_bridge("page-1")

    asyncio.run(_go())
    assert fake.cdp_calls == []
    assert fake._dialog_bridge_script_ids == {}


def test_dialog_intercept_resumes_after_human_releases(monkeypatch):
    monkeypatch.setattr("tools.bot_desktop.browser.cdp_url_is_running_instance", _is_dock_cdp)
    fake = _dialog_supervisor()

    async def _go():
        await fake._install_dialog_bridge("page-1")
        assert fake._dialog_bridge_script_ids["page-1"]
        lease.acquire("human-viewer")
        await fake._sync_dialog_intercept_to_lease()
        methods = [name for name, _params, _sid in fake.cdp_calls]
        assert "Fetch.disable" in methods
        assert "Page.removeScriptToEvaluateOnNewDocument" in methods
        assert any(
            name == "Runtime.evaluate" and "delete window.alert" in (params.get("expression") or "")
            for name, params, _sid in fake.cdp_calls
        )
        assert fake._dialog_intercept_paused is True
        lease.release("human-viewer")
        await fake._sync_dialog_intercept_to_lease()
        assert fake._dialog_intercept_paused is False
        assert fake._dialog_bridge_script_ids.get("page-1")

    asyncio.run(_go())
