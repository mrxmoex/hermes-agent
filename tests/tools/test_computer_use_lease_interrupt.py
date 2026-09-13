"""Leftover computer_use input after Take over must be interrupted, not waited out.

``_stop_backend`` takes the session call lock so an in-flight ``type_text``
finishes first. That leftover write lands in the field a human just took over —
the same class as leftover CDP ``ws.send``. ``interrupt_reserved_backends``
detaches the driver and drops the transport without that lock.
"""

from __future__ import annotations

import json
import threading
from pathlib import Path

import pytest

from tools.bot_desktop import lease
from tools.computer_use.backend import ActionResult


@pytest.fixture(autouse=True)
def _fresh(monkeypatch):
    from tools.computer_use import tool

    lease._reset_for_tests()
    tool.reset_backend_for_tests()
    monkeypatch.setattr(tool, "_request_approval", lambda *a, **k: None)
    yield
    lease._reset_for_tests()
    tool.reset_backend_for_tests()


class _BlockingBackend:
    """Driver whose ``type_text`` holds the call lock until ``interrupt``."""

    def __init__(self):
        self.started = threading.Event()
        self.released = threading.Event()
        self.typed: list[str] = []
        self.interrupt_calls = 0

    def start(self):
        return None

    def stop(self):
        self.interrupt()

    def interrupt(self):
        self.interrupt_calls += 1
        self.released.set()

    def is_available(self):
        return True

    def type_text(self, text, **_kw):
        self.started.set()
        if not self.released.wait(timeout=3.0):
            self.typed.append(text)
            return ActionResult(ok=True, action="type")
        raise RuntimeError("driver interrupted")


class _RecordingBackend:
    def __init__(self):
        self.interrupt_calls = 0

    def start(self):
        return None

    def stop(self):
        self.interrupt()

    def interrupt(self):
        self.interrupt_calls += 1

    def is_available(self):
        return True


def test_takeover_interrupts_inflight_type_without_delivering_it(monkeypatch):
    from tools.computer_use import tool

    backend = _BlockingBackend()
    monkeypatch.setattr(tool, "_new_backend", lambda permission_mode="standard": backend)

    result_box: list[dict] = []

    def _run():
        result_box.append(json.loads(tool.handle_computer_use(
            {"action": "type", "text": "secret-password"})))

    worker = threading.Thread(target=_run, name="cua-inflight-type")
    worker.start()
    assert backend.started.wait(timeout=3.0), "type_text never started"
    lease.acquire("human")
    worker.join(timeout=3.0)
    assert not worker.is_alive(), "in-flight type did not unblock after Take over"
    assert backend.interrupt_calls >= 1
    assert backend.typed == [], "leftover keystrokes were delivered after Take over"
    assert result_box and result_box[0].get("code") == "human_has_control"


def test_interrupt_spares_a_sibling_profile_backend(tmp_path: Path):
    from hermes_constants import hermes_home_key, reset_hermes_home_override, set_hermes_home_override
    from tools.computer_use import tool

    home_a = tmp_path / "profile-a"
    home_b = tmp_path / "profile-b"
    home_a.mkdir()
    home_b.mkdir()
    backend_a = _RecordingBackend()
    backend_b = _RecordingBackend()

    token = set_hermes_home_override(home_a)
    try:
        with tool._backend_lock:
            tool._install_backend("sess-a", backend_a, "standard")
    finally:
        reset_hermes_home_override(token)

    token = set_hermes_home_override(home_b)
    try:
        with tool._backend_lock:
            tool._install_backend("sess-b", backend_b, "standard")
    finally:
        reset_hermes_home_override(token)

    token = set_hermes_home_override(home_a)
    try:
        lease.acquire("human")
        tool.interrupt_reserved_backends(home=str(home_a))
    finally:
        lease.release("human")
        reset_hermes_home_override(token)

    assert backend_a.interrupt_calls == 1
    assert backend_b.interrupt_calls == 0
    assert hermes_home_key(home_b) != hermes_home_key(home_a)


def test_interrupt_is_noop_while_agent_holds():
    from tools.computer_use import tool

    backend = _RecordingBackend()
    with tool._backend_lock:
        tool._install_backend("sess", backend, "standard")
    tool.interrupt_reserved_backends()
    assert backend.interrupt_calls == 0
    assert "sess" in tool._backends


def test_cua_interrupt_skips_end_session():
    """``stop()`` sends end_session (waits behind type). ``interrupt()`` must not."""
    from types import SimpleNamespace

    from tools.computer_use.cua_backend import CuaDriverBackend

    backend = CuaDriverBackend()
    session = SimpleNamespace(_started=True, calls=[], stopped=False)

    def _call_tool(name, args, timeout=30.0):
        session.calls.append(name)
        return {}

    session.call_tool = _call_tool
    session.stop = lambda: setattr(session, "stopped", True)
    backend._session = session
    backend._bridge = SimpleNamespace(stop=lambda: None)
    backend._embedded_daemon = None

    backend.interrupt()
    assert session.calls == []
    assert session.stopped is True

    session.calls.clear()
    session.stopped = False
    backend.stop()
    assert session.calls == ["end_session"]
    assert session.stopped is True
