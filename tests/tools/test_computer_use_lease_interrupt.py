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


def test_approval_detection_import_defers_cwd_write_compile():
    """``_cua_permission_mode`` imports ``tools.approval`` on the first
    ``computer_use`` call. Compiling the cwd-aware write regexes at
    import held ``_backend_lock`` for ~10s — Take over could not
    interrupt a sibling, and in-flight type never started in time."""
    import tools.approval_detection as ad

    assert ad._relative_bot_desktop_write_re.cache_info().currsize == 0
    assert ad._oldpwd_bot_desktop_write_re.cache_info().currsize == 0
    assert ad._dirstack_bot_desktop_write_re.cache_info().currsize == 0
    assert ad._pwd_parent_write_re.cache_info().currsize == 0
    assert ad._oldpwd_parent_write_re.cache_info().currsize == 0


def test_permission_mode_warmup_does_not_hold_backend_lock(monkeypatch):
    """First ``_cua_permission_mode`` (approval import) must not hold
    the cache lock — ``interrupt_reserved_backends`` needs that lock."""
    from tools.computer_use import tool

    unlocked: list[bool] = []
    orig = tool._cua_permission_mode

    def wrapped(sid):
        got = tool._backend_lock.acquire(blocking=False)
        unlocked.append(got)
        if got:
            tool._backend_lock.release()
        return orig(sid)

    monkeypatch.setattr(tool, "_cua_permission_mode", wrapped)
    monkeypatch.setattr(tool, "_new_backend", lambda permission_mode="standard": _RecordingBackend())
    tool._get_backend()
    assert unlocked and unlocked[0] is True


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


def test_computer_use_dispatch_persists_dock_port_before_devtools_miss(monkeypatch):
    """Messaging-only ``computer_use`` used to never stamp ``dock-cdp-port``.
    A DevTools miss before Take over then treated leftover CDP as another
    Chrome (admit None), even after findings 65–67 (acquire / leftover
    watch / serve status)."""
    import tools.bot_desktop.browser as bdb
    from tools.bot_desktop.lease import HumanHasControl
    from tools.computer_use import tool
    from tools.browser_tool_session import (
        _admit_resolved_cdp_for_attach,
        _admit_shared_browser,
        _cdp_url_is_bot_desktop_browser,
        _last_dock_cdp_port,
        _reset_dock_port_memory_for_tests,
    )

    _reset_dock_port_memory_for_tests()
    dock = "ws://127.0.0.1:9333/devtools/browser/x"
    other = "ws://127.0.0.1:9222/devtools/browser/x"
    monkeypatch.setattr(bdb, "running_instance_cdp_port", lambda *a, **k: 9333)
    monkeypatch.setattr("tools.bot_desktop.runtime.ensure_started_for_tool", lambda: None)
    monkeypatch.setattr(
        tool, "_get_backend",
        lambda **k: (_ for _ in ()).throw(RuntimeError("no backend")),
    )
    assert bdb.last_known_dock_cdp_port() is None
    tool.handle_computer_use({"action": "list_apps"})
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
    _reset_dock_port_memory_for_tests()


def test_computer_use_request_handoff_persists_dock_port_before_devtools_miss(monkeypatch):
    """Finding 68 persisted on list_apps/click, then returned from
    request_handoff / wait_for_human first. Messaging-only handoff never
    stamped dock-cdp-port or started the leftover watch. A later DevTools
    miss leftover-attached as another Chrome (admit None)."""
    import tools.bot_desktop.browser as bdb
    from tools.bot_desktop.lease import HumanHasControl
    from tools.computer_use import tool
    from tools.browser_tool_session import (
        _admit_resolved_cdp_for_attach,
        _admit_shared_browser,
        _cdp_url_is_bot_desktop_browser,
        _last_dock_cdp_port,
        _reset_dock_port_memory_for_tests,
    )

    _reset_dock_port_memory_for_tests()
    dock = "ws://127.0.0.1:9333/devtools/browser/x"
    other = "ws://127.0.0.1:9222/devtools/browser/x"
    monkeypatch.setattr(bdb, "running_instance_cdp_port", lambda *a, **k: 9333)
    assert bdb.last_known_dock_cdp_port() is None
    asked = json.loads(tool.handle_computer_use(
        {"action": "request_handoff", "reason": "Finish 2FA"},
    ))
    assert asked["ok"] is True
    assert asked["state"]["pending_handoff"] == "Finish 2FA"
    assert bdb.last_known_dock_cdp_port() == 9333
    assert tool._watch_started is True

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
    _reset_dock_port_memory_for_tests()


def test_computer_use_wait_for_human_persists_dock_port_before_devtools_miss(monkeypatch):
    """wait_for_human shared request_handoff's early return and skipped
    persist. A messaging turn that only waits still has to stamp the live
    dock before DevTools can disappear."""
    import tools.bot_desktop.browser as bdb
    from tools.bot_desktop.lease import HumanHasControl
    from tools.computer_use import tool
    from tools.browser_tool_session import (
        _admit_resolved_cdp_for_attach,
        _admit_shared_browser,
        _cdp_url_is_bot_desktop_browser,
        _last_dock_cdp_port,
        _reset_dock_port_memory_for_tests,
    )

    _reset_dock_port_memory_for_tests()
    dock = "ws://127.0.0.1:9333/devtools/browser/x"
    other = "ws://127.0.0.1:9222/devtools/browser/x"
    monkeypatch.setattr(bdb, "running_instance_cdp_port", lambda *a, **k: 9333)
    json.loads(tool.handle_computer_use(
        {"action": "request_handoff", "reason": "Finish 2FA"},
    ))
    _reset_dock_port_memory_for_tests()
    monkeypatch.setattr(bdb, "running_instance_cdp_port", lambda *a, **k: 9333)
    assert bdb.last_known_dock_cdp_port() is None
    waited = json.loads(tool.handle_computer_use(
        {"action": "wait_for_human", "seconds": 1, "grace": 0.05},
    ))
    assert waited["ok"] is False
    assert waited["code"] == "no_takeover"
    assert bdb.last_known_dock_cdp_port() == 9333
    assert tool._watch_started is True

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
    _reset_dock_port_memory_for_tests()


def test_computer_use_watch_persists_sibling_dock_under_backend_home(monkeypatch, tmp_path: Path):
    """A cua backend minted under a bot home must stamp that home's
    ``dock-cdp-port``, not invent a port on the launch profile."""
    import tools.bot_desktop.browser as bdb
    from hermes_constants import reset_hermes_home_override, set_hermes_home_override
    from tools.computer_use import tool
    from tools.browser_tool_session import _reset_dock_port_memory_for_tests

    launch = tmp_path / "launch"
    bot = tmp_path / "bot"
    launch.mkdir()
    bot.mkdir()
    token_bot = set_hermes_home_override(str(bot))
    try:
        with tool._backend_lock:
            tool._install_backend("review", _RecordingBackend(), "standard")
        assert bdb.last_known_dock_cdp_port() is None
    finally:
        reset_hermes_home_override(token_bot)

    bot_profile = (bot / "bot-desktop" / "browser-profile").resolve()

    def live_port(user_data_dir, **_k):
        try:
            return 9333 if Path(user_data_dir).resolve() == bot_profile else None
        except OSError:
            return None

    monkeypatch.setattr(bdb, "running_instance_cdp_port", live_port)
    _reset_dock_port_memory_for_tests()

    token_launch = set_hermes_home_override(str(launch))
    try:
        assert bdb.last_known_dock_cdp_port() is None
        tool._persist_watched_dock_ports()
        assert bdb.last_known_dock_cdp_port() is None
    finally:
        reset_hermes_home_override(token_launch)

    token_bot = set_hermes_home_override(str(bot))
    try:
        assert bdb.last_known_dock_cdp_port() == 9333
    finally:
        reset_hermes_home_override(token_bot)
        _reset_dock_port_memory_for_tests()


def _sibling_homes(tmp_path: Path) -> tuple[Path, Path]:
    launch = tmp_path / "launch"
    bot = tmp_path / "bot"
    launch.mkdir()
    bot.mkdir()
    return launch, bot


class _AppsBackend(_RecordingBackend):
    def list_apps(self, **_kw):
        return []


def test_handle_computer_use_uses_session_backend_home_after_multiplex_turn(tmp_path: Path):
    """Finding 101 scoped browser leftover I/O. ``computer_use`` still
    admitted / persisted / resolved DISPLAY against ambient launch
    ``lease.json`` (agent, missing file) and typed on the bot screen a
    human was using — or replaced that session's cached cua-driver
    because launch DISPLAY looked stale.
    """
    from hermes_constants import reset_hermes_home_override, set_hermes_home_override
    from tools.bot_desktop.lease import _path
    from tools.computer_use import tool

    launch, bot = _sibling_homes(tmp_path)
    token_bot = set_hermes_home_override(str(bot))
    try:
        with tool._backend_lock:
            tool._install_backend("review", _AppsBackend(), "standard")
        lease.acquire("human-viewer")
    finally:
        reset_hermes_home_override(token_bot)

    token_launch = set_hermes_home_override(str(launch))
    try:
        refused = json.loads(tool.handle_computer_use(
            {"action": "list_apps"}, session_id="review",
        ))
        assert refused.get("code") == "human_has_control"
        assert "review" in tool._backends
    finally:
        reset_hermes_home_override(token_launch)
        for home in (launch, bot):
            for f in (_path(str(home)), _path(str(home)).with_suffix(".lock")):
                try:
                    f.unlink()
                except OSError:
                    pass


def test_handle_computer_use_does_not_fence_on_the_launch_profile_lease(monkeypatch, tmp_path: Path):
    """A human on the launch bot must not void a sibling computer_use."""
    from hermes_constants import reset_hermes_home_override, set_hermes_home_override
    from tools.bot_desktop.lease import _path
    from tools.computer_use import tool

    launch, bot = _sibling_homes(tmp_path)
    token_launch = set_hermes_home_override(str(launch))
    try:
        lease.acquire("human-viewer")
    finally:
        reset_hermes_home_override(token_launch)

    token_bot = set_hermes_home_override(str(bot))
    try:
        with tool._backend_lock:
            tool._install_backend("review", _AppsBackend(), "standard")
    finally:
        reset_hermes_home_override(token_bot)

    monkeypatch.setattr("tools.bot_desktop.runtime.ensure_started_for_tool", lambda: None)

    token_launch = set_hermes_home_override(str(launch))
    try:
        result = json.loads(tool.handle_computer_use(
            {"action": "list_apps"}, session_id="review",
        ))
        assert result.get("code") != "human_has_control"
        assert "apps" in result
    finally:
        reset_hermes_home_override(token_launch)
        for home in (launch, bot):
            for f in (_path(str(home)), _path(str(home)).with_suffix(".lock")):
                try:
                    f.unlink()
                except OSError:
                    pass


def test_handle_computer_use_persists_dock_under_backend_home_after_multiplex(
    monkeypatch, tmp_path: Path,
):
    """After a multiplex turn, persist must stamp the session backend's
    ``dock-cdp-port``, not invent a port on the launch profile.
    """
    import tools.bot_desktop.browser as bdb
    from hermes_constants import reset_hermes_home_override, set_hermes_home_override
    from tools.bot_desktop.lease import _path
    from tools.browser_tool_session import _reset_dock_port_memory_for_tests
    from tools.computer_use import tool

    launch, bot = _sibling_homes(tmp_path)
    token_bot = set_hermes_home_override(str(bot))
    try:
        with tool._backend_lock:
            tool._install_backend("review", _AppsBackend(), "standard")
        assert bdb.last_known_dock_cdp_port() is None
    finally:
        reset_hermes_home_override(token_bot)

    bot_profile = (bot / "bot-desktop" / "browser-profile").resolve()

    def live_port(user_data_dir, **_k):
        try:
            return 9333 if Path(user_data_dir).resolve() == bot_profile else None
        except OSError:
            return None

    monkeypatch.setattr(bdb, "running_instance_cdp_port", live_port)
    monkeypatch.setattr("tools.bot_desktop.runtime.ensure_started_for_tool", lambda: None)
    monkeypatch.setattr(
        tool, "_get_backend",
        lambda **k: (_ for _ in ()).throw(RuntimeError("no backend")),
    )
    _reset_dock_port_memory_for_tests()

    token_launch = set_hermes_home_override(str(launch))
    try:
        assert bdb.last_known_dock_cdp_port() is None
        tool.handle_computer_use({"action": "list_apps"}, session_id="review")
        assert bdb.last_known_dock_cdp_port() is None
    finally:
        reset_hermes_home_override(token_launch)

    token_bot = set_hermes_home_override(str(bot))
    try:
        assert bdb.last_known_dock_cdp_port() == 9333
    finally:
        reset_hermes_home_override(token_bot)
        _reset_dock_port_memory_for_tests()
        for home in (launch, bot):
            for f in (_path(str(home)), _path(str(home)).with_suffix(".lock")):
                try:
                    f.unlink()
                except OSError:
                    pass


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
