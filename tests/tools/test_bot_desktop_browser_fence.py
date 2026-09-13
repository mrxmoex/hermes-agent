"""The bot's browser tools obey the screen lease: while a human holds the shared browser, nothing is
dispatched, and a command whose run crossed a takeover loses its result."""

from __future__ import annotations

import json
import os
import subprocess

import pytest

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
    monkeypatch.setattr(runtime, "published_env", lambda: {})
    bu, ran = _wire_browser_exec(monkeypatch, session_info={
        "session_name": "cloud",
        "cdp_url": "wss://cloud.example/devtools/browser/x",
        "features": {"local": False},
    })
    lease.acquire("human-viewer")
    result = json.loads(bu.browser_exec("print(1)", task_id="cloud"))
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
