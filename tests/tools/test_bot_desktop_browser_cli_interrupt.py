"""Leftover browser CLI input after Take over must be interrupted, not waited out.

``_run_browser_command`` and ``browser_exec`` only discard the result after the
CLI finishes. Leftover ``fill`` / Playwright keystrokes still land in the field
a human just took over — the same class as leftover CDP ``ws.send`` and leftover
``computer_use`` ``type_text``.
"""

from __future__ import annotations

import json
import threading
from pathlib import Path

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


class _BlockingProc:
    """CLI whose ``wait`` holds until ``kill`` — leftover fill if we wait it out."""

    def __init__(self):
        self.started = threading.Event()
        self.released = threading.Event()
        self.killed = 0
        self.returncode = None

    def wait(self, timeout=None):
        self.started.set()
        if not self.released.wait(timeout=3.0):
            self.returncode = 0
            return 0
        self.returncode = -9
        return self.returncode

    def communicate(self, input=None, timeout=None):
        self.wait(timeout=timeout)
        return ("typed secret", "")

    def kill(self):
        self.killed += 1
        self.released.set()

    def poll(self):
        return self.returncode


def _wire_local_browser(monkeypatch, proc, commands=None):
    from tools import browser_tool as browser
    from tools import browser_tool_session as session

    monkeypatch.delenv("AGENT_BROWSER_PROFILE", raising=False)
    monkeypatch.setattr(browser, "_is_camofox_mode", lambda: False)
    monkeypatch.setattr(browser, "_blocked_private_page_action", lambda *a: None)
    monkeypatch.setattr(session, "_browser_command_preflight", lambda: {"browser_cmd": "agent-browser"})
    monkeypatch.setattr(session, "_get_session_info", lambda *a: {
        "session_name": "review", "cdp_url": None, "features": {"local": True},
    })
    monkeypatch.setattr(session._cloud, "_get_browser_engine", lambda: "chrome")
    monkeypatch.setattr(session._cloud, "_is_headed_mode", lambda: True)
    monkeypatch.setattr(session._cdp, "_ensure_cdp_supervisor", lambda *a, **k: None)

    def popen(argv, env, socket_dir, tag):
        if commands is not None:
            commands.append(tag)
        Path(socket_dir).mkdir(parents=True, exist_ok=True)
        Path(socket_dir, f"_stdout_{tag}").write_text('{"success":true,"data":{"typed":"secret"}}\n')
        Path(socket_dir, f"_stderr_{tag}").write_text("")
        return proc

    monkeypatch.setattr(session, "_popen_agent_browser", popen)
    return browser, session


def test_takeover_kills_inflight_agent_browser_fill_without_delivering_it(monkeypatch):
    proc = _BlockingProc()
    _browser, session = _wire_local_browser(monkeypatch, proc)
    result_box: list[dict] = []

    def _run():
        result_box.append(session._run_browser_command(
            "review", "fill", ["@e1", "secret-password"]))

    worker = threading.Thread(target=_run, name="ab-inflight-fill")
    worker.start()
    assert proc.started.wait(timeout=3.0), "agent-browser fill never started"
    lease.acquire("human")
    worker.join(timeout=3.0)
    assert not worker.is_alive(), "in-flight fill did not unblock after Take over"
    assert proc.killed >= 1
    assert result_box and result_box[0].get("code") == "human_has_control"
    assert "secret" not in json.dumps(result_box[0].get("data") or {})


def test_interrupt_spares_a_cloud_browser_cli(monkeypatch):
    from tools.browser_tool_session import interrupt_reserved_browser_cli, register_inflight_dock_cli

    dock = _BlockingProc()
    cloud = _BlockingProc()
    register_inflight_dock_cli(dock)
    # Cloud leftover is not registered — interrupt must not invent a kill.
    lease.acquire("human")
    interrupt_reserved_browser_cli()
    assert dock.killed >= 1
    assert cloud.killed == 0


def test_takeover_does_not_kill_inflight_cloud_cli(monkeypatch):
    """A Browserbase / other-Chrome session is not this screen's leftover writer."""
    proc = _BlockingProc()
    _browser, session = _wire_local_browser(monkeypatch, proc)
    monkeypatch.setattr(session, "_get_session_info", lambda *a: {
        "session_name": "cloud", "cdp_url": "wss://browserbase.example/cdp", "features": {},
    })
    result_box: list[dict] = []

    def _run():
        result_box.append(session._run_browser_command("review", "fill", ["@e1", "secret"]))

    worker = threading.Thread(target=_run, name="ab-cloud-fill")
    worker.start()
    assert proc.started.wait(timeout=3.0)
    lease.acquire("human")
    worker.join(timeout=0.4)
    assert worker.is_alive(), "cloud CLI was interrupted as if it were the dock"
    assert proc.killed == 0
    proc.kill()
    worker.join(timeout=3.0)
    assert not worker.is_alive()


def test_interrupt_spares_a_sibling_profile_cli(tmp_path: Path):
    from hermes_constants import reset_hermes_home_override, set_hermes_home_override
    from tools.browser_tool_session import interrupt_reserved_browser_cli, register_inflight_dock_cli

    home_a = tmp_path / "profile-a"
    home_b = tmp_path / "profile-b"
    home_a.mkdir()
    home_b.mkdir()
    proc_a = _BlockingProc()
    proc_b = _BlockingProc()
    register_inflight_dock_cli(proc_a, str(home_a))
    register_inflight_dock_cli(proc_b, str(home_b))

    token = set_hermes_home_override(home_a)
    try:
        lease.acquire("human")
        interrupt_reserved_browser_cli(home=str(home_a))
    finally:
        lease.release("human")
        reset_hermes_home_override(token)

    assert proc_a.killed == 1
    assert proc_b.killed == 0


def test_interrupt_is_noop_while_agent_holds():
    from tools.browser_tool_session import interrupt_reserved_browser_cli, register_inflight_dock_cli

    proc = _BlockingProc()
    register_inflight_dock_cli(proc)
    interrupt_reserved_browser_cli()
    assert proc.killed == 0


def test_takeover_kills_inflight_browser_exec_without_delivering_it(monkeypatch):
    from tools import browser_use_cli as bu
    from tools.browser_tool_session import _stamp_admitted

    proc = _BlockingProc()
    killed = []

    def fake_kill(p):
        killed.append(p)
        p.kill()

    monkeypatch.setattr(bu, "_find_cli", lambda: ["browser-use"])
    monkeypatch.setattr(bu, "_blocked_url_in_code", lambda code: None)
    monkeypatch.setattr(bu, "_route_backend", lambda env, *a, **k: env.__setitem__(
        "BU_CDP_WS", "ws://127.0.0.1:9333/devtools/browser/x") or None)
    monkeypatch.setattr(
        "tools.browser_tool_session._admit_shared_browser",
        lambda *a, **k: _stamp_admitted(lease.assert_agent_may_act()),
    )
    monkeypatch.setattr(bu, "_attach_vault_supervisor", lambda *a, **k: None)
    monkeypatch.setattr(bu.subprocess, "Popen", lambda *a, **k: proc)
    monkeypatch.setattr(bu, "_kill_cli_process_group", fake_kill)
    monkeypatch.setattr(bu, "_group_popen_kwargs", lambda: {})

    result_box: list[str] = []

    def _run():
        result_box.append(bu.browser_exec(
            "fill_input('#pw', 'secret-password')", task_id="review"))

    worker = threading.Thread(target=_run, name="bu-inflight-fill")
    worker.start()
    assert proc.started.wait(timeout=3.0), "browser_exec never started"
    lease.acquire("human")
    worker.join(timeout=3.0)
    assert not worker.is_alive(), "in-flight browser_exec did not unblock after Take over"
    assert killed and killed[0] is proc
    assert proc.killed >= 1
    payload = result_box[0] if result_box else ""
    assert "human_has_control" in payload
    assert "secret-password" not in payload
    assert "typed secret" not in payload
