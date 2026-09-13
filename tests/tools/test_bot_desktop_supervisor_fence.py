"""Bot Desktop leftover CDP supervisors obey the screen lease."""

from __future__ import annotations

import asyncio
from unittest.mock import MagicMock

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


def test_ensure_cdp_supervisor_does_not_attach_to_dock_while_human_holds(monkeypatch):
    """_get_session_info used to start a supervisor on dock CDP before the command fence."""
    import tools.bot_desktop.browser as bdb
    from tools import browser_tool_cdp as cdp

    monkeypatch.setattr(bdb, "running_instance_cdp_port", lambda *a, **k: 9333)
    monkeypatch.setattr(cdp, "_get_cdp_override_raw", lambda: "http://127.0.0.1:9333")
    monkeypatch.setattr(cdp, "_get_cdp_override", lambda: "http://127.0.0.1:9333")
    started = []
    import tools.browser_supervisor as bs
    registry = MagicMock()
    registry.get_or_start.side_effect = lambda **k: started.append(k)
    monkeypatch.setattr(bs, "SUPERVISOR_REGISTRY", registry)
    lease.acquire("human-viewer")
    cdp._ensure_cdp_supervisor("review")
    assert started == []


def test_ensure_cdp_supervisor_does_not_probe_dock_while_human_holds(monkeypatch):
    """HTTP /json/version on the dock is observation of the page a human is typing into."""
    import tools.bot_desktop.browser as bdb
    from tools import browser_tool_cdp as cdp

    probed = []
    monkeypatch.setattr(bdb, "running_instance_cdp_port", lambda *a, **k: 9333)
    monkeypatch.setattr(cdp, "_get_cdp_override_raw", lambda: "http://127.0.0.1:9333")
    monkeypatch.setattr(cdp, "_get_cdp_override", lambda: probed.append("override") or "ws://127.0.0.1:9333/devtools/browser/x")
    monkeypatch.setattr(cdp, "_resolve_cdp_override", lambda url: probed.append(url) or url)
    started = []
    import tools.browser_supervisor as bs
    registry = MagicMock()
    registry.get_or_start.side_effect = lambda **k: started.append(k)
    monkeypatch.setattr(bs, "SUPERVISOR_REGISTRY", registry)
    lease.acquire("human-viewer")
    cdp._ensure_cdp_supervisor("review")
    assert probed == []
    assert started == []


def test_ensure_cdp_supervisor_still_attaches_when_agent_holds(monkeypatch):
    """Discovery and attach stay available while the agent owns the screen."""
    import tools.bot_desktop.browser as bdb
    from tools import browser_tool_cdp as cdp

    probed = []
    monkeypatch.setattr(bdb, "running_instance_cdp_port", lambda *a, **k: 9333)
    monkeypatch.setattr(cdp, "_get_cdp_override_raw", lambda: "http://127.0.0.1:9333")
    monkeypatch.setattr(cdp, "_get_cdp_override", lambda: probed.append("override") or "ws://127.0.0.1:9333/devtools/browser/x")
    started = []
    import tools.browser_supervisor as bs
    registry = MagicMock()
    registry.get_or_start.side_effect = lambda **k: started.append(k)
    monkeypatch.setattr(bs, "SUPERVISOR_REGISTRY", registry)
    cdp._ensure_cdp_supervisor("review")
    assert probed == ["override"]
    assert len(started) == 1


def test_supervisor_may_not_touch_dock_page_while_human_holds(monkeypatch):
    """Leftover reconnect / Fetch passthrough must stop, not talk to the dock jar."""
    import tools.bot_desktop.browser as bdb
    from tools.browser_tool_supervisor_lease import supervisor_may_touch_page

    monkeypatch.setattr(bdb, "running_instance_cdp_port", lambda *a, **k: 9333)
    dock = "ws://127.0.0.1:9333/devtools/browser/x"
    lease.acquire("human-viewer")
    assert supervisor_may_touch_page(dock) is False
    lease.release("human-viewer")
    assert supervisor_may_touch_page(dock) is True
    assert supervisor_may_touch_page("wss://browserbase.example/cdp") is True


def test_human_acquire_stops_leftover_dock_supervisor(monkeypatch):
    """A leftover supervisor's watchdog must not keep talking to the dock page after Take over."""
    import tools.bot_desktop.browser as bdb
    from tools.browser_tool_supervisor_lease import install_supervisor_lease_hook

    monkeypatch.setattr(bdb, "running_instance_cdp_port", lambda *a, **k: 9333)
    stopped = []
    sup = MagicMock()
    sup.cdp_url = "ws://127.0.0.1:9333/devtools/browser/x"
    import tools.browser_supervisor as bs
    registry = MagicMock()
    registry._lock = __import__("threading").Lock()
    registry._by_task = {"review": sup}
    registry.stop.side_effect = lambda tid: stopped.append(tid)
    monkeypatch.setattr(bs, "SUPERVISOR_REGISTRY", registry)
    install_supervisor_lease_hook()
    lease.acquire("human-viewer")
    assert stopped == ["review"]


def test_dialog_respond_does_not_talk_to_cdp_while_human_holds(monkeypatch):
    """Leftover auto-policy / watchdog must not accept or dismiss on the human's page."""
    import tools.bot_desktop.browser as bdb
    from tools.browser_supervisor_dialogs import DialogSupervisionMixin, PendingDialog

    class _Sup(DialogSupervisionMixin):
        task_id = "review"
        cdp_url = "ws://127.0.0.1:9333/devtools/browser/x"

        def __init__(self):
            self.called = False

        async def _cdp(self, *a, **k):
            self.called = True
            return {}

        async def _cdp_quiet(self, *a, **k):
            self.called = True

    monkeypatch.setattr(bdb, "running_instance_cdp_port", lambda *a, **k: 9333)
    lease.acquire("human-viewer")
    dialog = PendingDialog(
        id="d-1", type="prompt", message="password", default_prompt="",
        opened_at=0.0, cdp_session_id="s", bridge_request_id="req-1",
    )
    sup = _Sup()
    asyncio.run(sup._respond(dialog, accept=True, prompt_text="SECRET"))
    assert sup.called is False
    assert getattr(sup, "_stop_requested", False) is True


def test_fetch_passthrough_does_not_talk_to_cdp_while_human_holds(monkeypatch):
    """Leftover Fetch.enable still pauses requests; continueRequest is a CDP write."""
    import tools.bot_desktop.browser as bdb
    from tools.browser_supervisor_dialogs import DialogSupervisionMixin

    class _Sup(DialogSupervisionMixin):
        task_id = "review"
        cdp_url = "ws://127.0.0.1:9333/devtools/browser/x"

        def __init__(self):
            self.called = False

        async def _cdp(self, *a, **k):
            self.called = True
            return {}

        async def _cdp_quiet(self, *a, **k):
            self.called = True

    monkeypatch.setattr(bdb, "running_instance_cdp_port", lambda *a, **k: 9333)
    lease.acquire("human-viewer")
    sup = _Sup()
    asyncio.run(sup._on_fetch_paused(
        {"requestId": "r1", "request": {"url": "https://example.com/"}}, session_id="s",
    ))
    assert sup.called is False
    assert getattr(sup, "_stop_requested", False) is True


def test_request_leftover_stop_marks_dock_supervisor_when_human_holds(monkeypatch):
    """Safe on the supervisor thread: set the flag, never join."""
    import tools.bot_desktop.browser as bdb
    from tools.browser_tool_supervisor_lease import request_leftover_stop

    monkeypatch.setattr(bdb, "running_instance_cdp_port", lambda *a, **k: 9333)
    dock = MagicMock()
    dock.cdp_url = "ws://127.0.0.1:9333/devtools/browser/x"
    dock._stop_requested = False
    remote = MagicMock()
    remote.cdp_url = "wss://browserbase.example/cdp"
    remote._stop_requested = False
    lease.acquire("human-viewer")
    assert request_leftover_stop(dock) is True
    assert dock._stop_requested is True
    assert request_leftover_stop(remote) is False
    assert remote._stop_requested is False
    lease.release("human-viewer")
    dock._stop_requested = False
    assert request_leftover_stop(dock) is False
    assert dock._stop_requested is False


def test_read_loop_detaches_live_socket_when_human_holds(monkeypatch):
    """Reconnect admit is not enough — a still-up leftover WS must drop on the next frame."""
    import tools.bot_desktop.browser as bdb
    from tools.browser_supervisor import CDPSupervisor

    monkeypatch.setattr(bdb, "running_instance_cdp_port", lambda *a, **k: 9333)

    class _WS:
        def __aiter__(self):
            return self

        async def __anext__(self):
            return '{"method":"Page.frameNavigated","params":{}}'

    sup = CDPSupervisor(task_id="review", cdp_url="ws://127.0.0.1:9333/devtools/browser/x")
    sup._ws = _WS()
    lease.acquire("human-viewer")
    asyncio.run(sup._read_loop())
    assert sup._stop_requested is True


def test_read_loop_detaches_idle_socket_when_human_holds(monkeypatch):
    """A quiet page sends no CDP frames; leftover Fetch/dialog I/O must still drop."""
    import tools.bot_desktop.browser as bdb
    from tools.browser_supervisor import CDPSupervisor

    monkeypatch.setattr(bdb, "running_instance_cdp_port", lambda *a, **k: 9333)

    class _QuietWS:
        def __init__(self):
            self._closed = asyncio.Event()

        def __aiter__(self):
            return self

        async def __anext__(self):
            await self._closed.wait()
            raise StopAsyncIteration

        async def close(self):
            self._closed.set()

    sup = CDPSupervisor(task_id="review", cdp_url="ws://127.0.0.1:9333/devtools/browser/x")
    sup._ws = _QuietWS()
    lease.acquire("human-viewer")
    asyncio.run(sup._read_loop())
    assert sup._stop_requested is True


def _sibling_homes(tmp_path):
    launch = tmp_path / "launch"
    bot = tmp_path / "bot"
    launch.mkdir()
    bot.mkdir()
    return launch, bot


def _dock_port_for(bot_home, bot_port=9333, other_port=9444):
    from pathlib import Path

    bot_profile = (bot_home / "bot-desktop" / "browser-profile").resolve()

    def fake_port(user_data_dir, **_k):
        return bot_port if Path(user_data_dir).resolve() == bot_profile else other_port

    return fake_port


def test_leftover_supervisor_follows_the_profile_that_minted_it(monkeypatch, tmp_path):
    """After a multiplex turn the process home is the launch profile; leftover I/O is not."""
    import tools.bot_desktop.browser as bdb
    from hermes_constants import hermes_home_key, reset_hermes_home_override, set_hermes_home_override
    from tools.bot_desktop.lease import _path
    from tools.browser_supervisor import CDPSupervisor
    from tools.browser_tool_supervisor_lease import request_leftover_stop, supervisor_may_touch_page

    launch, bot = _sibling_homes(tmp_path)
    monkeypatch.setattr(bdb, "running_instance_cdp_port", _dock_port_for(bot))
    token_bot = set_hermes_home_override(str(bot))
    try:
        sup = CDPSupervisor(task_id="review", cdp_url="ws://127.0.0.1:9333/devtools/browser/x")
        lease.acquire("human-viewer")
        assert sup.hermes_home == hermes_home_key(bot)
    finally:
        reset_hermes_home_override(token_bot)
    token_launch = set_hermes_home_override(str(launch))
    try:
        assert supervisor_may_touch_page(sup.cdp_url) is True
        assert supervisor_may_touch_page(sup.cdp_url, home=sup.hermes_home) is False
        assert request_leftover_stop(sup) is True
        assert sup._stop_requested is True
    finally:
        reset_hermes_home_override(token_launch)
        for home in (launch, bot):
            for f in (_path(str(home)), _path(str(home)).with_suffix(".lock")):
                try:
                    f.unlink()
                except OSError:
                    pass


def test_cross_process_takeover_stops_sibling_profile_supervisor(monkeypatch, tmp_path):
    """Desktop writes another profile's lease.json; the watch sweep must still detach."""
    import tools.bot_desktop.browser as bdb
    from hermes_constants import reset_hermes_home_override, set_hermes_home_override
    from tools.bot_desktop.lease import HUMAN, Lease, _path, _write
    from tools.browser_supervisor import CDPSupervisor
    from tools.browser_tool_supervisor_lease import _watch_once

    launch, bot = _sibling_homes(tmp_path)
    monkeypatch.setattr(bdb, "running_instance_cdp_port", _dock_port_for(bot))
    token_bot = set_hermes_home_override(str(bot))
    try:
        sup = CDPSupervisor(task_id="review", cdp_url="ws://127.0.0.1:9333/devtools/browser/x")
    finally:
        reset_hermes_home_override(token_bot)

    stopped = []
    import tools.browser_supervisor as bs
    registry = __import__("unittest.mock", fromlist=["MagicMock"]).MagicMock()
    registry._lock = __import__("threading").Lock()
    registry._by_task = {"review": sup}
    registry.stop.side_effect = lambda tid: stopped.append(tid)
    monkeypatch.setattr(bs, "SUPERVISOR_REGISTRY", registry)

    token_launch = set_hermes_home_override(str(launch))
    try:
        _write(_path(str(bot)), Lease(holder=HUMAN, viewer_id="human-viewer"))
        _watch_once()
        assert stopped == ["review"]
    finally:
        reset_hermes_home_override(token_launch)
        for home in (launch, bot):
            for f in (_path(str(home)), _path(str(home)).with_suffix(".lock")):
                try:
                    f.unlink()
                except OSError:
                    pass


def test_minted_dock_identity_survives_a_missing_devtools_port(monkeypatch):
    """Take over can unlink DevToolsActivePort; leftover I/O is still the dock."""
    import tools.bot_desktop.browser as bdb
    from tools.bot_desktop.lease import HumanHasControl
    from tools.browser_supervisor import CDPSupervisor
    from tools.browser_tool_session import _admit_task_shared_browser
    from tools.browser_tool_supervisor_lease import request_leftover_stop, supervisor_may_touch_page

    monkeypatch.setattr(bdb, "running_instance_cdp_port", lambda *a, **k: 9333)
    sup = CDPSupervisor(task_id="review", cdp_url="ws://127.0.0.1:9333/devtools/browser/x")
    assert sup.targets_bot_desktop is True
    monkeypatch.setattr(bdb, "running_instance_cdp_port", lambda *a, **k: None)
    lease.acquire("human-viewer")
    # Mint remembered the dock port; a live DevTools miss must not unfence it.
    assert supervisor_may_touch_page(sup.cdp_url) is False
    assert supervisor_may_touch_page(
        sup.cdp_url, home=sup.hermes_home, targets_bot_desktop=True,
    ) is False
    assert request_leftover_stop(sup) is True
    assert sup._stop_requested is True
    import tools.browser_supervisor as bs
    registry = MagicMock()
    registry.get.return_value = sup
    monkeypatch.setattr(bs, "SUPERVISOR_REGISTRY", registry)
    with pytest.raises(HumanHasControl):
        _admit_task_shared_browser("review")


def test_missing_devtools_port_still_stops_stamped_dock_supervisor(monkeypatch):
    """Watch sweep must not skip a leftover dock just because the port file is gone."""
    import tools.bot_desktop.browser as bdb
    from tools.browser_supervisor import CDPSupervisor
    from tools.browser_tool_supervisor_lease import stop_reserved_supervisors

    monkeypatch.setattr(bdb, "running_instance_cdp_port", lambda *a, **k: 9333)
    sup = CDPSupervisor(task_id="review", cdp_url="ws://127.0.0.1:9333/devtools/browser/x")
    monkeypatch.setattr(bdb, "running_instance_cdp_port", lambda *a, **k: None)
    stopped = []
    import tools.browser_supervisor as bs
    registry = MagicMock()
    registry._lock = __import__("threading").Lock()
    registry._by_task = {"review": sup}
    registry.stop.side_effect = lambda tid: stopped.append(tid)
    monkeypatch.setattr(bs, "SUPERVISOR_REGISTRY", registry)
    lease.acquire("human-viewer")
    stop_reserved_supervisors()
    assert stopped == ["review"]


def test_stop_reserved_stops_stamped_dock_leftover_when_session_is_cloud(monkeypatch):
    """A leftover dock WS is the human's jar even if this task's session is Browserbase."""
    import tools.bot_desktop.browser as bdb
    from tools import browser_tool as bt
    from tools.browser_supervisor import CDPSupervisor
    from tools.browser_tool_supervisor_lease import stop_reserved_supervisors

    monkeypatch.setattr(bdb, "running_instance_cdp_port", lambda *a, **k: 9333)
    sup = CDPSupervisor(task_id="review", cdp_url="ws://127.0.0.1:9333/devtools/browser/x")
    assert sup.targets_bot_desktop is True
    stopped = []
    import tools.browser_supervisor as bs
    registry = MagicMock()
    registry._lock = __import__("threading").Lock()
    registry._by_task = {"review": sup}
    registry.stop.side_effect = lambda tid: stopped.append(tid)
    monkeypatch.setattr(bs, "SUPERVISOR_REGISTRY", registry)
    monkeypatch.setitem(
        bt._active_sessions,
        "review",
        {"cdp_url": "wss://browserbase.example/cdp", "features": {}},
    )
    lease.acquire("human-viewer")
    stop_reserved_supervisors()
    assert stopped == ["review"]


def test_run_does_not_attach_when_human_takes_over_during_connect(monkeypatch):
    """Pre-connect admit is not enough — Target.createTarget after a 10s connect
    is leftover action on the page the human is now typing into."""
    import sys
    import types

    import tools.bot_desktop.browser as bdb
    from tools.browser_supervisor import CDPSupervisor

    monkeypatch.setattr(bdb, "running_instance_cdp_port", lambda *a, **k: 9333)
    attached = []

    class _WS:
        def __init__(self):
            self._closed = asyncio.Event()

        async def close(self):
            self._closed.set()

        def __aiter__(self):
            return self

        async def __anext__(self):
            await self._closed.wait()
            raise StopAsyncIteration

    async def _connect(*_a, **_k):
        lease.acquire("human-viewer")
        return _WS()

    monkeypatch.setitem(sys.modules, "websockets", types.SimpleNamespace(connect=_connect))
    sup = CDPSupervisor(task_id="review", cdp_url="ws://127.0.0.1:9333/devtools/browser/x")

    async def _attach():
        attached.append(True)

    sup._attach_initial_page = _attach
    asyncio.run(sup._run())
    assert attached == []
    assert sup._stop_requested is True
    assert sup._ws is None


def test_supervisor_may_touch_page_fails_closed_when_admit_raises(monkeypatch):
    """Leftover I/O must stop if the lease helper cannot decide, not keep talking."""
    from tools.browser_tool_supervisor_lease import request_leftover_stop, supervisor_may_touch_page

    monkeypatch.setattr(
        "tools.browser_tool_session._admit_shared_browser",
        lambda *a, **k: (_ for _ in ()).throw(RuntimeError("lease helper exploded")),
    )
    assert supervisor_may_touch_page("ws://127.0.0.1:9333/devtools/browser/x") is False
    assert supervisor_may_touch_page("wss://browserbase.example/cdp") is False
    dock = MagicMock()
    dock.cdp_url = "ws://127.0.0.1:9333/devtools/browser/x"
    dock._stop_requested = False
    assert request_leftover_stop(dock) is True
    assert dock._stop_requested is True


def test_ensure_cdp_supervisor_does_not_probe_when_lease_check_raises(monkeypatch):
    """Discovery is observation; an exploded admit must not HTTP the candidate."""
    from tools import browser_tool_cdp as cdp

    probed = []
    started = []
    monkeypatch.setattr(cdp, "_get_cdp_override_raw", lambda: "http://127.0.0.1:9333")
    monkeypatch.setattr(
        cdp, "_get_cdp_override",
        lambda: probed.append("override") or "ws://127.0.0.1:9333/devtools/browser/x",
    )
    monkeypatch.setattr(
        "tools.browser_tool_supervisor_lease.supervisor_may_touch_page",
        lambda *a, **k: (_ for _ in ()).throw(RuntimeError("lease helper exploded")),
    )
    import tools.browser_supervisor as bs
    registry = MagicMock()
    registry.get_or_start.side_effect = lambda **k: started.append(k)
    monkeypatch.setattr(bs, "SUPERVISOR_REGISTRY", registry)
    cdp._ensure_cdp_supervisor("review")
    assert probed == []
    assert started == []


def test_dock_supervisor_does_not_reconnect_when_lease_check_raises(monkeypatch):
    """A stamped dock leftover must not open a new WS if admit cannot run."""
    import tools.bot_desktop.browser as bdb
    from tools.browser_supervisor import CDPSupervisor

    monkeypatch.setattr(bdb, "running_instance_cdp_port", lambda *a, **k: 9333)
    sup = CDPSupervisor(task_id="review", cdp_url="ws://127.0.0.1:9333/devtools/browser/x")
    assert sup.targets_bot_desktop is True
    monkeypatch.setattr(
        "tools.browser_tool_supervisor_lease.supervisor_may_touch_page",
        lambda *a, **k: (_ for _ in ()).throw(RuntimeError("lease helper exploded")),
    )
    asyncio.run(sup._run())
    assert sup._stop_requested is True
    assert sup._ws is None


def test_read_loop_detaches_idle_dock_when_lease_check_raises(monkeypatch):
    """A quiet dock leftover must drop if the idle poller cannot evaluate the lease."""
    import tools.bot_desktop.browser as bdb
    from tools.browser_supervisor import CDPSupervisor

    monkeypatch.setattr(bdb, "running_instance_cdp_port", lambda *a, **k: 9333)

    class _QuietWS:
        def __init__(self):
            self._closed = asyncio.Event()

        def __aiter__(self):
            return self

        async def __anext__(self):
            await self._closed.wait()
            raise StopAsyncIteration

        async def close(self):
            self._closed.set()

    sup = CDPSupervisor(task_id="review", cdp_url="ws://127.0.0.1:9333/devtools/browser/x")
    sup._ws = _QuietWS()
    monkeypatch.setattr(
        "tools.browser_tool_supervisor_lease.request_leftover_stop",
        lambda *_a, **_k: (_ for _ in ()).throw(RuntimeError("lease helper exploded")),
    )
    asyncio.run(sup._read_loop())
    assert sup._stop_requested is True


def test_admit_task_does_not_adopt_a_sibling_default_leftover(monkeypatch, tmp_path):
    """The registry is keyed by task_id; a sibling leftover stored as default is another bot."""
    import tools.bot_desktop.browser as bdb
    from hermes_constants import reset_hermes_home_override, set_hermes_home_override
    from tools.bot_desktop.lease import _path
    from tools.browser_supervisor import CDPSupervisor
    from tools.browser_tool_session import _admit_task_shared_browser

    launch, bot = _sibling_homes(tmp_path)
    monkeypatch.setattr(bdb, "running_instance_cdp_port", _dock_port_for(bot))
    token_bot = set_hermes_home_override(str(bot))
    try:
        sup = CDPSupervisor(task_id="default", cdp_url="ws://127.0.0.1:9333/devtools/browser/x")
        lease.acquire("human-viewer")
        assert sup.targets_bot_desktop is True
    finally:
        reset_hermes_home_override(token_bot)

    import tools.browser_supervisor as bs
    registry = MagicMock()
    registry.get.side_effect = lambda tid: sup if tid == "default" else None
    monkeypatch.setattr(bs, "SUPERVISOR_REGISTRY", registry)

    token_launch = set_hermes_home_override(str(launch))
    try:
        # Another task — and even task_id default — on the launch profile must
        # not inherit the sibling leftover (that would raise HumanHasControl
        # from the bot's lease, or worse, admit that jar).
        assert _admit_task_shared_browser("review") is None
        assert _admit_task_shared_browser(None) is None
        assert _admit_task_shared_browser("default") is None
    finally:
        reset_hermes_home_override(token_launch)
        for home in (launch, bot):
            for f in (_path(str(home)), _path(str(home)).with_suffix(".lock")):
                try:
                    f.unlink()
                except OSError:
                    pass


def test_lease_moved_result_uses_the_home_that_admitted(tmp_path):
    """A leftover admit under the bot home must see that home's take-over."""
    from hermes_constants import reset_hermes_home_override, set_hermes_home_override
    from tools.bot_desktop.lease import HUMAN, Lease, _path, _write
    from tools.browser_tool_session import _admit_shared_browser, _lease_moved_result

    launch, bot = _sibling_homes(tmp_path)
    token_bot = set_hermes_home_override(str(bot))
    try:
        admitted = _admit_shared_browser(treat_as_dock=True)
        assert admitted is not None
        assert admitted.epoch == 0
        assert getattr(admitted, "_hermes_home", None)
    finally:
        reset_hermes_home_override(token_bot)

    _write(_path(str(bot)), Lease(holder=HUMAN, viewer_id="human-viewer", epoch=1))
    token_launch = set_hermes_home_override(str(launch))
    try:
        moved = _lease_moved_result(admitted)
        assert moved is not None
        assert moved.get("code") == "human_has_control"
        assert lease.get().epoch == 0
    finally:
        reset_hermes_home_override(token_launch)
        for home in (launch, bot):
            for f in (_path(str(home)), _path(str(home)).with_suffix(".lock")):
                try:
                    f.unlink()
                except OSError:
                    pass


def test_registry_does_not_hand_out_a_sibling_leftover(monkeypatch, tmp_path):
    """Vault/dialog I/O uses SUPERVISOR_REGISTRY.get; a sibling default leftover is another jar."""
    import json
    import tools.bot_desktop.browser as bdb
    from hermes_constants import reset_hermes_home_override, set_hermes_home_override
    from tools.bot_desktop.lease import _path
    from tools.browser_supervisor import (
        CDPSupervisor, SUPERVISOR_REGISTRY, _supervisor_registry_key,
    )
    from tools import browser_dialog_tool as dialog
    from tools import browser_vault_tool as vault

    launch, bot = _sibling_homes(tmp_path)
    monkeypatch.setattr(bdb, "running_instance_cdp_port", _dock_port_for(bot))
    saved = dict(SUPERVISOR_REGISTRY._by_task)
    SUPERVISOR_REGISTRY._by_task.clear()
    token_bot = set_hermes_home_override(str(bot))
    try:
        sup = CDPSupervisor(task_id="default", cdp_url="ws://127.0.0.1:9333/devtools/browser/x")
        bot_key = _supervisor_registry_key("default")
        SUPERVISOR_REGISTRY._by_task[bot_key] = sup
        evaluated = []
        replied = []
        sup.evaluate_runtime = lambda *a, **k: evaluated.append(a) or {"ok": True, "result": "SECRET"}
        sup.respond_to_dialog = lambda **k: replied.append(k) or {"ok": True, "dialog": {}}
        assert SUPERVISOR_REGISTRY.get("default") is sup
    finally:
        reset_hermes_home_override(token_bot)

    monkeypatch.setattr(
        "tools.browser_tool_session._run_browser_command",
        lambda *a, **k: {"success": False, "error": "no session"},
    )
    token_launch = set_hermes_home_override(str(launch))
    try:
        assert SUPERVISOR_REGISTRY.get("default") is None
        SUPERVISOR_REGISTRY.stop("default")
        assert SUPERVISOR_REGISTRY._by_task.get(bot_key) is sup
        vault_out = vault._eval_js("default", "window.location.href")
        dialog_out = json.loads(dialog.browser_dialog("accept", task_id="default"))
        assert evaluated == []
        assert replied == []
        assert "SECRET" not in json.dumps(vault_out)
        assert dialog_out.get("success") is not True
    finally:
        reset_hermes_home_override(token_launch)
        SUPERVISOR_REGISTRY._by_task.clear()
        SUPERVISOR_REGISTRY._by_task.update(saved)
        for home in (launch, bot):
            for f in (_path(str(home)), _path(str(home)).with_suffix(".lock")):
                try:
                    f.unlink()
                except OSError:
                    pass


def test_get_or_start_does_not_stop_a_sibling_leftover(monkeypatch, tmp_path):
    """A launch-home attach on the same task_id must not tear down the other bot's leftover."""
    import tools.bot_desktop.browser as bdb
    from hermes_constants import reset_hermes_home_override, set_hermes_home_override
    from tools.browser_supervisor import (
        CDPSupervisor, SUPERVISOR_REGISTRY, _supervisor_registry_key,
    )

    launch, bot = _sibling_homes(tmp_path)
    monkeypatch.setattr(bdb, "running_instance_cdp_port", _dock_port_for(bot))
    monkeypatch.setattr(CDPSupervisor, "start", lambda self, timeout=15.0: None)
    saved = dict(SUPERVISOR_REGISTRY._by_task)
    SUPERVISOR_REGISTRY._by_task.clear()
    token_bot = set_hermes_home_override(str(bot))
    try:
        leftover = CDPSupervisor(task_id="default", cdp_url="ws://127.0.0.1:9333/devtools/browser/x")
        leftover.stop = lambda *a, **k: (_ for _ in ()).throw(AssertionError("sibling leftover stopped"))
        bot_key = _supervisor_registry_key("default")
        SUPERVISOR_REGISTRY._by_task[bot_key] = leftover
    finally:
        reset_hermes_home_override(token_bot)

    token_launch = set_hermes_home_override(str(launch))
    try:
        started = SUPERVISOR_REGISTRY.get_or_start(
            "default", "ws://127.0.0.1:9222/devtools/browser/x",
        )
        assert started is not leftover
        assert SUPERVISOR_REGISTRY._by_task.get(bot_key) is leftover
        assert SUPERVISOR_REGISTRY.get("default") is started
    finally:
        reset_hermes_home_override(token_launch)
        SUPERVISOR_REGISTRY._by_task.clear()
        SUPERVISOR_REGISTRY._by_task.update(saved)
