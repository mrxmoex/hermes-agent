"""The bot's browser tools obey the screen lease: while a human holds the shared browser, nothing is
dispatched, and a command whose run crossed a takeover loses its result."""

from __future__ import annotations

import json
import os
import subprocess
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


def test_create_cdp_session_allows_foreign_endpoint_while_human_holds(monkeypatch):
    monkeypatch.setattr("tools.bot_desktop.browser.cdp_url_is_running_instance", lambda url, **k: False)
    lease.acquire("human-viewer")
    info = session_mod._create_cdp_session("review", _FOREIGN_CDP)
    assert info["cdp_url"] == _FOREIGN_CDP
    assert info["features"]["cdp_override"] is True


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
    monkeypatch.setattr(browser_cdp_tool, "cdp_url_is_running_instance", _is_dock_cdp)
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
    monkeypatch.setattr(browser_cdp_tool, "cdp_url_is_running_instance", lambda url, **k: False)

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
    monkeypatch.setattr(browser_cdp_tool, "cdp_url_is_running_instance", _is_dock_cdp)

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
