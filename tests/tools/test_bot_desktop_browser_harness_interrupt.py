"""Leftover browser-use harness CDP after Take over must be dropped, not waited out.

``browser_exec`` kills the in-flight CLI (finding 42) then discards the result.
The harness daemon ``start_new_session``s out of that group and stays
CDP-connected after the command returns — leftover ``Target.setAutoAttach`` /
Playwright input in the field a human just took over. Same class as leftover
CDP ``ws.send`` and attach-only agent-browser (finding 40). Tree-kill is
refused: a session-leader daemon must not take the dock Chromium with it.
"""

from __future__ import annotations

import json
import os
import signal
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


def test_takeover_kills_registered_dock_harness():
    from tools.browser_tool_session import register_reserved_dock_harness

    killed = []
    register_reserved_dock_harness("review", pid=4242, kill=killed.append)
    lease.acquire("human")
    assert 4242 in killed


def test_interrupt_spares_a_sibling_profile_harness(tmp_path: Path):
    from hermes_constants import reset_hermes_home_override, set_hermes_home_override
    from tools.browser_tool_session import (
        interrupt_reserved_browser_harness,
        register_reserved_dock_harness,
    )

    home_a = tmp_path / "profile-a"
    home_b = tmp_path / "profile-b"
    home_a.mkdir()
    home_b.mkdir()
    killed_a, killed_b = [], []
    register_reserved_dock_harness("review", str(home_a), pid=11, kill=killed_a.append)
    register_reserved_dock_harness("review", str(home_b), pid=22, kill=killed_b.append)

    token = set_hermes_home_override(home_a)
    try:
        lease.acquire("human")
        interrupt_reserved_browser_harness(home=str(home_a))
    finally:
        lease.release("human")
        reset_hermes_home_override(token)

    assert 11 in killed_a
    assert killed_b == []


def test_interrupt_spares_an_unregistered_cloud_harness():
    from tools.browser_tool_session import interrupt_reserved_browser_harness

    killed = []
    lease.acquire("human")
    interrupt_reserved_browser_harness()
    assert killed == []


def test_interrupt_is_noop_while_agent_holds():
    from tools.browser_tool_session import (
        interrupt_reserved_browser_harness,
        register_reserved_dock_harness,
    )

    killed = []
    register_reserved_dock_harness("review", pid=4242, kill=killed.append)
    interrupt_reserved_browser_harness()
    assert killed == []


def test_default_harness_kill_is_pid_only_not_process_group(monkeypatch):
    from tools import browser_tool_session as session

    killed, groups = [], []
    monkeypatch.setattr(session, "_verify_harness_daemon", lambda pid: True)
    monkeypatch.setattr(os, "kill", lambda pid, sig: killed.append((pid, sig)))
    monkeypatch.setattr(os, "killpg", lambda pid, sig: groups.append((pid, sig)))
    session._kill_harness_daemon_pid(4242)
    assert killed == [(4242, signal.SIGKILL)]
    assert groups == []


def test_planted_unrelated_pid_is_not_killed(monkeypatch):
    from tools import browser_tool_session as session

    killed = []
    monkeypatch.setattr(session, "_verify_harness_daemon", lambda pid: False)
    monkeypatch.setattr(os, "kill", lambda pid, sig: killed.append(pid))
    session._kill_harness_daemon_pid(1)
    assert killed == []


def test_harness_name_traversal_is_not_resolved(tmp_path: Path, monkeypatch):
    from tools import browser_tool_session as session

    monkeypatch.chdir(tmp_path)
    assert session._harness_pid_file_candidates("../evil") == []
    assert session._resolve_harness_daemon_pid("../evil") is None
    session.register_reserved_dock_harness("../evil", pid=9, kill=lambda *_a: None)
    lease.acquire("human")
    # Invalid names never enter the leftover table.
    from tools.browser_tool_session import _reserved_dock_harness
    assert _reserved_dock_harness == []


def test_browser_exec_registers_dock_harness_and_takeover_kills_it(monkeypatch):
    from tools import browser_use_cli as bu
    from tools.browser_tool_session import _stamp_admitted

    killed = []

    class _Done:
        returncode = 0
        stdout = "ok"
        stderr = ""

    monkeypatch.setattr(bu, "_find_cli", lambda: ["browser-use"])
    monkeypatch.setattr(bu, "_blocked_url_in_code", lambda code: None)
    monkeypatch.setattr(bu, "_route_backend", lambda env, *a, **k: env.__setitem__(
        "BU_CDP_WS", "ws://127.0.0.1:9333/devtools/browser/x") or None)
    monkeypatch.setattr(
        "tools.browser_tool_session._admit_shared_browser",
        lambda *a, **k: _stamp_admitted(lease.assert_agent_may_act()),
    )
    monkeypatch.setattr(bu, "_attach_vault_supervisor", lambda *a, **k: None)
    monkeypatch.setattr(bu, "_run_cli_killing_process_group", lambda *a, **k: _Done())
    monkeypatch.setattr(
        "tools.browser_tool_session._resolve_harness_daemon_pid",
        lambda name: 555 if name == "default" else None,
    )
    monkeypatch.setattr(
        "tools.browser_tool_session._kill_harness_daemon_pid",
        lambda pid: killed.append(pid),
    )

    payload = json.loads(bu.browser_exec("print(page_info())", task_id="review"))
    assert payload.get("success") is True
    lease.acquire("human")
    assert 555 in killed


def test_browser_exec_cloud_session_does_not_register_a_dock_harness(monkeypatch):
    from tools import browser_use_cli as bu
    from tools.browser_tool_session import interrupt_reserved_browser_harness

    killed = []

    class _Done:
        returncode = 0
        stdout = "ok"
        stderr = ""

    monkeypatch.setattr(bu, "_find_cli", lambda: ["browser-use"])
    monkeypatch.setattr(bu, "_blocked_url_in_code", lambda code: None)
    monkeypatch.setattr(bu, "_route_backend", lambda env, *a, **k: env.__setitem__(
        "BU_CDP_WS", "wss://browserbase.example/cdp") or None)
    monkeypatch.setattr(
        "tools.browser_tool_session._admit_shared_browser",
        lambda *a, **k: None,
    )
    monkeypatch.setattr(bu, "_attach_vault_supervisor", lambda *a, **k: None)
    monkeypatch.setattr(bu, "_run_cli_killing_process_group", lambda *a, **k: _Done())
    monkeypatch.setattr(
        "tools.browser_tool_session._kill_harness_daemon_pid",
        lambda pid: killed.append(pid),
    )

    payload = json.loads(bu.browser_exec("print(page_info())", session="cloud"))
    assert payload.get("success") is True
    lease.acquire("human")
    interrupt_reserved_browser_harness()
    assert killed == []
