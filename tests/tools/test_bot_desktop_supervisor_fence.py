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


def test_ensure_cdp_supervisor_does_not_attach_after_takeover_on_resolved_ws(monkeypatch):
    """A full dock WS candidate used to skip the post-resolve check
    (``cdp_url == candidate``) and still ``get_or_start`` after Take over."""
    import tools.bot_desktop.browser as bdb
    from tools import browser_tool_cdp as cdp

    ws = "ws://127.0.0.1:9333/devtools/browser/x"
    started = []
    monkeypatch.setattr(bdb, "running_instance_cdp_port", lambda *a, **k: 9333)
    monkeypatch.setattr(cdp, "_get_cdp_override_raw", lambda: ws)

    def _get():
        lease.acquire("human-viewer")
        return ws

    monkeypatch.setattr(cdp, "_get_cdp_override", _get)
    import tools.browser_supervisor as bs
    registry = MagicMock()
    registry.get_or_start.side_effect = lambda **k: started.append(k.get("cdp_url"))
    monkeypatch.setattr(bs, "SUPERVISOR_REGISTRY", registry)
    cdp._ensure_cdp_supervisor("review")
    assert started == []
    assert lease.human_holds() is True


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


def test_watch_once_persists_sibling_dock_under_supervisor_home(monkeypatch, tmp_path):
    """A leftover minted under a bot home must stamp that home's
    ``dock-cdp-port``, not invent a port on the launch profile.

    Mint happens with no live probe (no identify persist). The watch tick
    later sees DevTools on the bot jar only.
    """
    import tools.bot_desktop.browser as bdb
    from hermes_constants import reset_hermes_home_override, set_hermes_home_override
    from pathlib import Path
    from tools.browser_supervisor import CDPSupervisor
    from tools.browser_tool_supervisor_lease import _watch_once

    launch, bot = _sibling_homes(tmp_path)
    monkeypatch.setattr(bdb, "running_instance_cdp_port", lambda *a, **k: None)
    token_bot = set_hermes_home_override(str(bot))
    try:
        sup = CDPSupervisor(task_id="review", cdp_url="ws://127.0.0.1:9333/devtools/browser/x")
        assert bdb.last_known_dock_cdp_port() is None
        assert getattr(sup, "targets_bot_desktop", None) is False
    finally:
        reset_hermes_home_override(token_bot)

    bot_profile = (bot / "bot-desktop" / "browser-profile").resolve()

    def live_port(user_data_dir, **_k):
        try:
            return 9333 if Path(user_data_dir).resolve() == bot_profile else None
        except OSError:
            return None

    monkeypatch.setattr(bdb, "running_instance_cdp_port", live_port)
    monkeypatch.setattr(
        "tools.browser_tool_lifecycle._stop_reserved_recordings",
        lambda: None,
    )
    import tools.browser_supervisor as bs
    registry = __import__("unittest.mock", fromlist=["MagicMock"]).MagicMock()
    registry._lock = __import__("threading").Lock()
    registry._by_task = {"review": sup}
    monkeypatch.setattr(bs, "SUPERVISOR_REGISTRY", registry)

    token_launch = set_hermes_home_override(str(launch))
    try:
        assert bdb.last_known_dock_cdp_port() is None
        _watch_once()
        assert bdb.last_known_dock_cdp_port() is None
    finally:
        reset_hermes_home_override(token_launch)

    token_bot = set_hermes_home_override(str(bot))
    try:
        assert bdb.last_known_dock_cdp_port() == 9333
    finally:
        reset_hermes_home_override(token_bot)


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


def _record_cdp_until_takeover(supervisor, *, flip_on: str, replies: dict):
    """Real ``_cdp`` send path: flip the lease on ``flip_on``, record methods."""
    import json

    sent = []

    class _WS:
        async def send(self, payload):
            msg = json.loads(payload)
            method = msg["method"]
            sent.append(method)
            if method == flip_on:
                lease.acquire("human-viewer")
            reply = replies.get(method, {})
            fut = supervisor._pending_calls.get(msg["id"])
            if fut is not None and not fut.done():
                fut.set_result({"id": msg["id"], "result": reply})

    supervisor._ws = _WS()
    return sent


def test_attach_initial_page_does_not_write_after_human_takes_over_mid_attach(monkeypatch):
    """Post-connect admit is not enough — Target.getTargets / Page.enable can
    sit on the wire for seconds. Target.createTarget / Fetch.enable /
    Runtime.evaluate after that are leftover action on the jar the human
    is now typing into."""
    import tools.bot_desktop.browser as bdb
    from tools.browser_supervisor import CDPSupervisor

    monkeypatch.setattr(bdb, "running_instance_cdp_port", lambda *a, **k: 9333)
    sup = CDPSupervisor(task_id="review", cdp_url="ws://127.0.0.1:9333/devtools/browser/x")
    sent = _record_cdp_until_takeover(
        sup,
        flip_on="Target.getTargets",
        replies={
            "Target.getTargets": {"targetInfos": []},
            "Target.createTarget": {"targetId": "t1"},
            "Target.attachToTarget": {"sessionId": "s1"},
        },
    )

    async def _go():
        try:
            await sup._attach_initial_page()
        except Exception:
            pass

    asyncio.run(_go())
    assert "Target.createTarget" not in sent
    assert "Fetch.enable" not in sent
    assert "Runtime.evaluate" not in sent
    assert sent == ["Target.getTargets"]
    assert sup._stop_requested is True


def test_child_domain_install_does_not_write_after_human_takes_over(monkeypatch):
    """Target.attachedToTarget schedules leftover Page.enable + dialog-bridge
    inject. Those writes must stop once a human holds, not finish the chain."""
    import tools.bot_desktop.browser as bdb
    from tools.browser_supervisor import CDPSupervisor

    monkeypatch.setattr(bdb, "running_instance_cdp_port", lambda *a, **k: 9333)
    sup = CDPSupervisor(task_id="review", cdp_url="ws://127.0.0.1:9333/devtools/browser/x")
    sent = _record_cdp_until_takeover(
        sup,
        flip_on="Page.enable",
        replies={},
    )

    async def _go():
        await sup._enable_child_domains("sid-child")

    asyncio.run(_go())
    assert "Fetch.enable" not in sent
    assert "Runtime.evaluate" not in sent
    assert sent == ["Page.enable"]
    assert sup._stop_requested is True


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


def test_stop_reserved_kills_attach_only_daemon_not_dock_chromium(monkeypatch):
    """A --cdp attach to launcher-owned Chrome can die; the session row stays."""
    import tools.bot_desktop.browser as bdb
    from tools import browser_tool as bt
    from tools.browser_tool_supervisor_lease import stop_reserved_supervisors

    killed = []
    monkeypatch.setattr(bdb, "shared_chromium_owner_session", lambda *a, **k: None)
    monkeypatch.setattr(
        "tools.browser_tool_lifecycle._kill_verified_daemon",
        lambda socket_dir, name: killed.append((socket_dir, name)) or True,
    )
    saved = bt._active_sessions.copy()
    try:
        bt._active_sessions.clear()
        bt._active_sessions["review"] = {
            "session_name": "h_review", "features": {"local": True},
        }
        lease.acquire("human-viewer")
        stop_reserved_supervisors()
        assert [name for _dir, name in killed] == ["h_review"]
        assert bt._active_sessions["review"]["session_name"] == "h_review"
    finally:
        bt._active_sessions.clear()
        bt._active_sessions.update(saved)


def test_stop_reserved_does_not_kill_owner_daemon_when_lock_gone_leftover_helpers(
    monkeypatch, tmp_path,
):
    """Finding 187: leftover helpers hid chrome pid after lock unlink.

    ``shared_chromium_owner_session`` used unique leftover-file
    holders. Overlay-unlinked SingletonLock plus leftover DevTools
    helpers made chrome unknown, so Take over treated the daemon
    that spawned chrome as attach-only leftover and tree-killed
    the Browser a human is typing into. Unique holder of hidden
    chrome keeps that daemon reserved.
    """
    import tools.bot_desktop.browser as bdb
    from tools import browser_tool as bt
    from tools.browser_tool_supervisor_lease import stop_reserved_supervisors

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

    killed = []
    monkeypatch.setattr(
        "tools.browser_tool_lifecycle._kill_verified_daemon",
        lambda socket_dir, name: killed.append(name) or True,
    )
    saved = bt._active_sessions.copy()
    try:
        bt._active_sessions.clear()
        bt._active_sessions["review"] = {
            "session_name": "h_review", "features": {"local": True},
        }
        lease.acquire("human-viewer")
        stop_reserved_supervisors()
        assert killed == []
        assert "review" in bt._active_sessions
    finally:
        bt._active_sessions.clear()
        bt._active_sessions.update(saved)


def test_stop_reserved_does_not_kill_owner_when_leftover_inherited_chrome_listen(
    monkeypatch, tmp_path,
):
    """Finding 188: leftover inherited chrome's unique CDP listen.

    n==1 unique-holder recover missed chrome, so Take over treated
    the daemon that spawned chrome as attach-only leftover and
    tree-killed the Browser a human is typing into. Leftover-shared
    with chrome keeps that daemon reserved.
    """
    import tools.bot_desktop.browser as bdb
    from tools import browser_tool as bt
    from tools.browser_tool_supervisor_lease import stop_reserved_supervisors

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
    monkeypatch.setattr(bdb, "_configured_cdp_override_url", lambda: "")
    (tmp_path / "DevToolsActivePort").write_text(
        "40141\n/devtools/browser/abc\n", encoding="utf-8",
    )
    assert bdb._this_jar_chromium_pid(str(tmp_path)) == 4240
    assert bdb.shared_chromium_owner_session() == "h_review"

    killed = []
    monkeypatch.setattr(
        "tools.browser_tool_lifecycle._kill_verified_daemon",
        lambda socket_dir, name: killed.append(name) or True,
    )
    saved = bt._active_sessions.copy()
    try:
        bt._active_sessions.clear()
        bt._active_sessions["review"] = {
            "session_name": "h_review", "features": {"local": True},
        }
        lease.acquire("human-viewer")
        stop_reserved_supervisors()
        assert killed == []
        assert "review" in bt._active_sessions
    finally:
        bt._active_sessions.clear()
        bt._active_sessions.update(saved)


def test_stop_reserved_does_not_kill_owner_when_leftover_connect_only_chrome_zygote(
    monkeypatch, tmp_path,
):
    """Finding 189: leftover is connect-only; chrome + zygote hold CDP.

    Leftover-shared equality missed chrome, so Take over treated the
    daemon that spawned chrome as attach-only leftover and tree-killed
    the Browser a human is typing into. Chrome plus this-jar children
    of chrome keeps that daemon reserved.
    """
    import tools.bot_desktop.browser as bdb
    from tools import browser_tool as bt
    from tools.browser_tool_supervisor_lease import stop_reserved_supervisors

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
    monkeypatch.setattr(bdb, "_configured_cdp_override_url", lambda: "")
    (tmp_path / "DevToolsActivePort").write_text(
        "40141\n/devtools/browser/abc\n", encoding="utf-8",
    )
    assert bdb._this_jar_chromium_pid(str(tmp_path)) == 4240
    assert bdb.shared_chromium_owner_session() == "h_review"

    killed = []
    monkeypatch.setattr(
        "tools.browser_tool_lifecycle._kill_verified_daemon",
        lambda socket_dir, name: killed.append(name) or True,
    )
    saved = bt._active_sessions.copy()
    try:
        bt._active_sessions.clear()
        bt._active_sessions["review"] = {
            "session_name": "h_review", "features": {"local": True},
        }
        lease.acquire("human-viewer")
        stop_reserved_supervisors()
        assert killed == []
        assert "review" in bt._active_sessions
    finally:
        bt._active_sessions.clear()
        bt._active_sessions.update(saved)


def test_stop_reserved_does_not_kill_owner_when_leftover_connect_only_zygote_grandchild(
    monkeypatch, tmp_path,
):
    """Finding 190: leftover is connect-only; zygote grandchild holds CDP.

    Child-only family missed chrome, so Take over treated the daemon
    that spawned chrome as attach-only leftover and tree-killed the
    Browser a human is typing into. Chrome plus this-jar descendants
    of chrome keeps that daemon reserved.
    """
    import tools.bot_desktop.browser as bdb
    from tools import browser_tool as bt
    from tools.browser_tool_supervisor_lease import stop_reserved_supervisors

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
    monkeypatch.setattr(bdb, "_configured_cdp_override_url", lambda: "")
    (tmp_path / "DevToolsActivePort").write_text(
        "40141\n/devtools/browser/abc\n", encoding="utf-8",
    )
    assert bdb._this_jar_chromium_pid(str(tmp_path)) == 4240
    assert bdb.shared_chromium_owner_session() == "h_review"

    killed = []
    monkeypatch.setattr(
        "tools.browser_tool_lifecycle._kill_verified_daemon",
        lambda socket_dir, name: killed.append(name) or True,
    )
    saved = bt._active_sessions.copy()
    try:
        bt._active_sessions.clear()
        bt._active_sessions["review"] = {
            "session_name": "h_review", "features": {"local": True},
        }
        lease.acquire("human-viewer")
        stop_reserved_supervisors()
        assert killed == []
        assert "review" in bt._active_sessions
    finally:
        bt._active_sessions.clear()
        bt._active_sessions.update(saved)


def test_stop_reserved_does_not_kill_owner_when_leftover_daemon_parent_sibling_chrome(
    monkeypatch, tmp_path,
):
    """Finding 191: leftover helpers' parent is leftover daemon.

    Lock-gone used leftover daemon as chrome_pid, so Take over
    treated the daemon that spawned chrome as attach-only leftover
    and tree-killed the Browser a human is typing into. Unique
    browser-process root of extra holders keeps that daemon reserved.
    """
    import tools.bot_desktop.browser as bdb
    from tools import browser_tool as bt
    from tools.browser_tool_supervisor_lease import stop_reserved_supervisors

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
    assert bdb._this_jar_chromium_pid(str(tmp_path)) == 4240
    assert bdb.shared_chromium_owner_session() == "h_review"

    killed = []
    monkeypatch.setattr(
        "tools.browser_tool_lifecycle._kill_verified_daemon",
        lambda socket_dir, name: killed.append(name) or True,
    )
    saved = bt._active_sessions.copy()
    try:
        bt._active_sessions.clear()
        bt._active_sessions["review"] = {
            "session_name": "h_review", "features": {"local": True},
        }
        lease.acquire("human-viewer")
        stop_reserved_supervisors()
        assert killed == []
        assert "review" in bt._active_sessions
    finally:
        bt._active_sessions.clear()
        bt._active_sessions.update(saved)


def test_stop_reserved_does_not_kill_owner_when_leftover_memory_hides_sibling_chrome(
    monkeypatch, tmp_path,
):
    """Finding 192: leftover memory hid sibling chrome.

    Lock-gone 179 returned leftover helper hosts after a pre-191
    leftover stamp, so Take over treated the daemon that spawned
    chrome as attach-only leftover and tree-killed the Browser a
    human is typing into. Hidden chrome that differs from leftover
    memory keeps that daemon reserved.
    """
    import tools.bot_desktop.browser as bdb
    from hermes_constants import hermes_home_key
    from tools import browser_tool as bt
    from tools.browser_tool_session import _last_dock_cdp_port
    from tools.browser_tool_supervisor_lease import stop_reserved_supervisors

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
    _last_dock_cdp_port[hermes_home_key()] = 40141
    (tmp_path / "dock-cdp-port").write_text("40141\n", encoding="utf-8")
    assert bdb.lock_listed_persist_port() == 9333
    assert bdb._this_jar_chromium_pid(str(tmp_path)) == 4240
    assert bdb.shared_chromium_owner_session() == "h_review"

    killed = []
    monkeypatch.setattr(
        "tools.browser_tool_lifecycle._kill_verified_daemon",
        lambda socket_dir, name: killed.append(name) or True,
    )
    saved = bt._active_sessions.copy()
    try:
        bt._active_sessions.clear()
        bt._active_sessions["review"] = {
            "session_name": "h_review", "features": {"local": True},
        }
        lease.acquire("human-viewer")
        stop_reserved_supervisors()
        assert killed == []
        assert "review" in bt._active_sessions
    finally:
        bt._active_sessions.clear()
        bt._active_sessions.update(saved)


def test_stop_reserved_does_not_kill_owner_when_leftover_persist_unique_hides_sibling_chrome(
    monkeypatch, tmp_path,
):
    """Finding 193: leftover persist unique hid sibling chrome.

    Lock-gone 181 returned leftover CRI persist after a restart, so
    Take over treated the daemon that spawned chrome as attach-only
    leftover and tree-killed the Browser a human is typing into.
    Hidden chrome that differs from leftover persist keeps that
    daemon reserved.
    """
    import tools.bot_desktop.browser as bdb
    from hermes_constants import hermes_home_key
    from tools import browser_tool as bt
    from tools.browser_tool_session import _last_dock_cdp_port
    from tools.browser_tool_supervisor_lease import stop_reserved_supervisors

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
    (tmp_path / "dock-cdp-port").write_text("18888\n", encoding="utf-8")
    assert bdb.lock_listed_persist_port() == 9333
    assert bdb._this_jar_chromium_pid(str(tmp_path)) == 4240
    assert bdb.shared_chromium_owner_session() == "h_review"

    killed = []
    monkeypatch.setattr(
        "tools.browser_tool_lifecycle._kill_verified_daemon",
        lambda socket_dir, name: killed.append(name) or True,
    )
    saved = bt._active_sessions.copy()
    try:
        bt._active_sessions.clear()
        bt._active_sessions["review"] = {
            "session_name": "h_review", "features": {"local": True},
        }
        lease.acquire("human-viewer")
        stop_reserved_supervisors()
        assert killed == []
        assert "review" in bt._active_sessions
    finally:
        bt._active_sessions.clear()
        bt._active_sessions.update(saved)


def test_stop_reserved_does_not_kill_owner_when_leftover_persist_unique_hides_attach(
    monkeypatch, tmp_path,
):
    """Finding 194: leftover persist unique hid attach behind leftover DevTools.

    Live recover misses when leftover holds chrome's CDP socket (166).
    Finding 172 then preferred leftover DevTools, so Take over treated
    the daemon that spawned chrome as attach-only leftover and
    tree-killed the Browser a human is typing into. Hidden chrome that
    equals lock-listed keeps that daemon reserved.
    """
    import tools.bot_desktop.browser as bdb
    from hermes_constants import hermes_home_key
    from tools import browser_tool as bt
    from tools.browser_tool_session import (
        _last_dock_cdp_port,
        _remembered_dock_attach_port,
    )
    from tools.browser_tool_supervisor_lease import stop_reserved_supervisors

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
    (tmp_path / "dock-cdp-port").write_text("18888\n", encoding="utf-8")
    assert bdb.lock_listed_persist_port() == 9333
    assert bdb._this_jar_chromium_pid(str(tmp_path)) == 4240
    assert bdb.shared_chromium_owner_session() == "h_review"
    assert bdb.running_instance_cdp_port(str(tmp_path)) is None
    assert _remembered_dock_attach_port() == 9333
    assert (tmp_path / "dock-cdp-port").read_text(encoding="utf-8").strip() == "18888"
    assert _last_dock_cdp_port.get(hermes_home_key()) is None

    killed = []
    monkeypatch.setattr(
        "tools.browser_tool_lifecycle._kill_verified_daemon",
        lambda socket_dir, name: killed.append(name) or True,
    )
    saved = bt._active_sessions.copy()
    try:
        bt._active_sessions.clear()
        bt._active_sessions["review"] = {
            "session_name": "h_review", "features": {"local": True},
        }
        lease.acquire("human-viewer")
        stop_reserved_supervisors()
        assert killed == []
        assert "review" in bt._active_sessions
    finally:
        bt._active_sessions.clear()
        bt._active_sessions.update(saved)


def test_stop_reserved_does_not_kill_owner_when_leftover_daemon_unique_cri(
    monkeypatch, tmp_path,
):
    """Finding 195: leftover daemon unique CRI hid sibling chrome.

    Lock-gone n==1 treated leftover daemon that inherited leftover
    CRI as chrome, so Take over treated the daemon that spawned
    chrome as attach-only leftover and tree-killed the Browser a
    human is typing into. Leftover daemon is not chrome. Sibling
    chrome keeps that daemon reserved.
    """
    import tools.bot_desktop.browser as bdb
    from hermes_constants import hermes_home_key
    from tools import browser_tool as bt
    from tools.browser_tool_session import _last_dock_cdp_port
    from tools.browser_tool_supervisor_lease import stop_reserved_supervisors

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
        lambda port, hosts: port in (9333, 40141, 18888) and "::1" in hosts,
    )
    monkeypatch.setattr(bdb, "_configured_cdp_override_url", lambda: "")
    (tmp_path / "DevToolsActivePort").write_text(
        "40141\n/devtools/browser/abc\n", encoding="utf-8",
    )
    (tmp_path / "dock-cdp-port").write_text("18888\n", encoding="utf-8")
    assert bdb.lock_listed_persist_port() == 9333
    assert bdb._this_jar_chromium_pid(str(tmp_path)) == 4240
    assert bdb.shared_chromium_owner_session() == "h_review"
    assert _last_dock_cdp_port.get(hermes_home_key()) is None

    killed = []
    monkeypatch.setattr(
        "tools.browser_tool_lifecycle._kill_verified_daemon",
        lambda socket_dir, name: killed.append(name) or True,
    )
    saved = bt._active_sessions.copy()
    try:
        bt._active_sessions.clear()
        bt._active_sessions["review"] = {
            "session_name": "h_review", "features": {"local": True},
        }
        lease.acquire("human-viewer")
        stop_reserved_supervisors()
        assert killed == []
        assert "review" in bt._active_sessions
    finally:
        bt._active_sessions.clear()
        bt._active_sessions.update(saved)


def test_stop_reserved_does_not_kill_owner_when_leftover_shared_cri(
    monkeypatch, tmp_path,
):
    """Finding 196: leftover-shared CRI hid sibling chrome.

    Leftover daemon and chrome both inherited leftover CRI, so
    hidden missed and Take over treated the daemon that spawned
    chrome as attach-only leftover and tree-killed the Browser a
    human is typing into. Chrome's own listen keeps that daemon
    reserved.
    """
    import tools.bot_desktop.browser as bdb
    from hermes_constants import hermes_home_key
    from tools import browser_tool as bt
    from tools.browser_tool_session import _last_dock_cdp_port
    from tools.browser_tool_supervisor_lease import stop_reserved_supervisors

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
        lambda port, hosts: port in (9333, 40141, 18888) and "::1" in hosts,
    )
    monkeypatch.setattr(bdb, "_configured_cdp_override_url", lambda: "")
    (tmp_path / "DevToolsActivePort").write_text(
        "40141\n/devtools/browser/abc\n", encoding="utf-8",
    )
    (tmp_path / "dock-cdp-port").write_text("18888\n", encoding="utf-8")
    assert bdb.lock_listed_persist_port() == 9333
    assert bdb._this_jar_chromium_pid(str(tmp_path)) == 4240
    assert bdb.shared_chromium_owner_session() == "h_review"
    assert _last_dock_cdp_port.get(hermes_home_key()) is None

    killed = []
    monkeypatch.setattr(
        "tools.browser_tool_lifecycle._kill_verified_daemon",
        lambda socket_dir, name: killed.append(name) or True,
    )
    saved = bt._active_sessions.copy()
    try:
        bt._active_sessions.clear()
        bt._active_sessions["review"] = {
            "session_name": "h_review", "features": {"local": True},
        }
        lease.acquire("human-viewer")
        stop_reserved_supervisors()
        assert killed == []
        assert "review" in bt._active_sessions
    finally:
        bt._active_sessions.clear()
        bt._active_sessions.update(saved)


def test_stop_reserved_does_not_kill_owner_when_unique_leftover_daemon_devtools(
    monkeypatch, tmp_path,
):
    """Finding 197: unique leftover-daemon DevTools hid sibling chrome.

    Leftover fill clients exited and chrome dropped the inherited
    DevTools listen, so leftover daemon uniquely held stale
    ``DevToolsActivePort``. Take over treated leftover daemon as
    chrome (``owner`` None) and tree-killed the Browser a human
    is typing into. Sibling chrome keeps that daemon reserved.
    """
    import tools.bot_desktop.browser as bdb
    from hermes_constants import hermes_home_key
    from tools import browser_tool as bt
    from tools.browser_tool_session import _last_dock_cdp_port
    from tools.browser_tool_supervisor_lease import stop_reserved_supervisors

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
        lambda port, hosts: port in (9333, 40141, 18888) and "::1" in hosts,
    )
    monkeypatch.setattr(bdb, "_configured_cdp_override_url", lambda: "")
    (tmp_path / "DevToolsActivePort").write_text(
        "40141\n/devtools/browser/abc\n", encoding="utf-8",
    )
    (tmp_path / "dock-cdp-port").write_text("18888\n", encoding="utf-8")
    assert bdb.lock_listed_persist_port() == 9333
    assert bdb._this_jar_chromium_pid(str(tmp_path)) == 4240
    assert bdb.shared_chromium_owner_session() == "h_review"
    assert _last_dock_cdp_port.get(hermes_home_key()) is None

    killed = []
    monkeypatch.setattr(
        "tools.browser_tool_lifecycle._kill_verified_daemon",
        lambda socket_dir, name: killed.append(name) or True,
    )
    saved = bt._active_sessions.copy()
    try:
        bt._active_sessions.clear()
        bt._active_sessions["review"] = {
            "session_name": "h_review", "features": {"local": True},
        }
        lease.acquire("human-viewer")
        stop_reserved_supervisors()
        assert killed == []
        assert "review" in bt._active_sessions
    finally:
        bt._active_sessions.clear()
        bt._active_sessions.update(saved)


def test_stop_reserved_does_not_kill_owner_when_unique_leftover_fill_devtools(
    monkeypatch, tmp_path,
):
    """Finding 198: unique leftover-fill DevTools hid sibling chrome.

    Leftover fill uniquely held stale ``DevToolsActivePort`` after
    leftover daemon dropped that listen. Take over treated chrome
    pid None as leftover and tree-killed the Browser a human is
    typing into. Sibling chrome keeps that daemon reserved.
    """
    import tools.bot_desktop.browser as bdb
    from hermes_constants import hermes_home_key
    from tools import browser_tool as bt
    from tools.browser_tool_session import _last_dock_cdp_port
    from tools.browser_tool_supervisor_lease import stop_reserved_supervisors

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
        lambda port, hosts: port in (9333, 40141, 18888) and "::1" in hosts,
    )
    monkeypatch.setattr(bdb, "_configured_cdp_override_url", lambda: "")
    (tmp_path / "DevToolsActivePort").write_text(
        "40141\n/devtools/browser/abc\n", encoding="utf-8",
    )
    (tmp_path / "dock-cdp-port").write_text("18888\n", encoding="utf-8")
    assert bdb.lock_listed_persist_port() == 9333
    assert bdb._this_jar_chromium_pid(str(tmp_path)) == 4240
    assert bdb.shared_chromium_owner_session() == "h_review"
    assert _last_dock_cdp_port.get(hermes_home_key()) is None

    killed = []
    monkeypatch.setattr(
        "tools.browser_tool_lifecycle._kill_verified_daemon",
        lambda socket_dir, name: killed.append(name) or True,
    )
    saved = bt._active_sessions.copy()
    try:
        bt._active_sessions.clear()
        bt._active_sessions["review"] = {
            "session_name": "h_review", "features": {"local": True},
        }
        lease.acquire("human-viewer")
        stop_reserved_supervisors()
        assert killed == []
        assert "review" in bt._active_sessions
    finally:
        bt._active_sessions.clear()
        bt._active_sessions.update(saved)


def test_stop_reserved_does_not_kill_owner_when_leftover_fill_inherited_persist(
    monkeypatch, tmp_path,
):
    """Finding 199: leftover fill inherited leftover persist hid sibling chrome.

    Leftover fill uniquely held stale ``DevToolsActivePort`` and
    also inherited leftover persist with chrome. Take over treated
    chrome pid None as leftover and tree-killed the Browser a
    human is typing into. Sibling chrome keeps that daemon
    reserved.
    """
    import tools.bot_desktop.browser as bdb
    from hermes_constants import hermes_home_key
    from tools import browser_tool as bt
    from tools.browser_tool_session import _last_dock_cdp_port
    from tools.browser_tool_supervisor_lease import stop_reserved_supervisors

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
        lambda port, hosts: port in (9333, 40141, 18888) and "::1" in hosts,
    )
    monkeypatch.setattr(bdb, "_configured_cdp_override_url", lambda: "")
    (tmp_path / "DevToolsActivePort").write_text(
        "40141\n/devtools/browser/abc\n", encoding="utf-8",
    )
    (tmp_path / "dock-cdp-port").write_text("18888\n", encoding="utf-8")
    assert bdb.lock_listed_persist_port() == 9333
    assert bdb._this_jar_chromium_pid(str(tmp_path)) == 4240
    assert bdb.shared_chromium_owner_session() == "h_review"
    assert _last_dock_cdp_port.get(hermes_home_key()) is None

    killed = []
    monkeypatch.setattr(
        "tools.browser_tool_lifecycle._kill_verified_daemon",
        lambda socket_dir, name: killed.append(name) or True,
    )
    saved = bt._active_sessions.copy()
    try:
        bt._active_sessions.clear()
        bt._active_sessions["review"] = {
            "session_name": "h_review", "features": {"local": True},
        }
        lease.acquire("human-viewer")
        stop_reserved_supervisors()
        assert killed == []
        assert "review" in bt._active_sessions
    finally:
        bt._active_sessions.clear()
        bt._active_sessions.update(saved)


def test_stop_reserved_does_not_kill_owner_when_leftover_python_inherited_chrome(
    monkeypatch, tmp_path,
):
    """Finding 200: leftover python inherited chrome CDP hid sibling chrome.

    Leftover fill uniquely held stale ``DevToolsActivePort``.
    Leftover python inherited chrome's CDP with the chrome
    family. Take over treated chrome pid None as leftover
    and tree-killed the Browser a human is typing into.
    Sibling chrome keeps that daemon reserved.
    """
    import tools.bot_desktop.browser as bdb
    from hermes_constants import hermes_home_key
    from tools import browser_tool as bt
    from tools.browser_tool_session import _last_dock_cdp_port
    from tools.browser_tool_supervisor_lease import stop_reserved_supervisors

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
        lambda port, hosts: port in (9333, 40141) and "::1" in hosts,
    )
    monkeypatch.setattr(bdb, "_configured_cdp_override_url", lambda: "")
    (tmp_path / "DevToolsActivePort").write_text(
        "40141\n/devtools/browser/abc\n", encoding="utf-8",
    )
    (tmp_path / "dock-cdp-port").write_text("18888\n", encoding="utf-8")
    assert bdb.lock_listed_persist_port() == 9333
    assert bdb._this_jar_chromium_pid(str(tmp_path)) == 4240
    assert bdb.shared_chromium_owner_session() == "h_review"
    assert _last_dock_cdp_port.get(hermes_home_key()) is None

    killed = []
    monkeypatch.setattr(
        "tools.browser_tool_lifecycle._kill_verified_daemon",
        lambda socket_dir, name: killed.append(name) or True,
    )
    saved = bt._active_sessions.copy()
    try:
        bt._active_sessions.clear()
        bt._active_sessions["review"] = {
            "session_name": "h_review", "features": {"local": True},
        }
        lease.acquire("human-viewer")
        stop_reserved_supervisors()
        assert killed == []
        assert "review" in bt._active_sessions
    finally:
        bt._active_sessions.clear()
        bt._active_sessions.update(saved)


def test_stop_reserved_does_not_kill_daemon_that_spawned_dock_chromium(monkeypatch):
    """Tree-killing the parent daemon would kill the Browser the human is using."""
    import tools.bot_desktop.browser as bdb
    from tools import browser_tool as bt
    from tools.browser_tool_supervisor_lease import stop_reserved_supervisors

    killed = []
    monkeypatch.setattr(bdb, "shared_chromium_owner_session", lambda *a, **k: "h_review")
    monkeypatch.setattr(
        "tools.browser_tool_lifecycle._kill_verified_daemon",
        lambda socket_dir, name: killed.append(name) or True,
    )
    saved = bt._active_sessions.copy()
    try:
        bt._active_sessions.clear()
        bt._active_sessions["review"] = {
            "session_name": "h_review", "features": {"local": True},
        }
        lease.acquire("human-viewer")
        stop_reserved_supervisors()
        assert killed == []
        assert "review" in bt._active_sessions
    finally:
        bt._active_sessions.clear()
        bt._active_sessions.update(saved)


def test_stop_reserved_does_not_kill_unrelated_cloud_daemon(monkeypatch):
    """A Browserbase / other-Chrome session is not this screen's leftover CDP."""
    from tools import browser_tool as bt
    from tools.browser_tool_supervisor_lease import stop_reserved_supervisors

    killed = []
    monkeypatch.setattr(
        "tools.browser_tool_lifecycle._kill_verified_daemon",
        lambda socket_dir, name: killed.append(name) or True,
    )
    saved = bt._active_sessions.copy()
    try:
        bt._active_sessions.clear()
        bt._active_sessions["review"] = {
            "session_name": "bb", "bb_session_id": "x",
            "cdp_url": "wss://browserbase.example/cdp", "features": {},
        }
        lease.acquire("human-viewer")
        stop_reserved_supervisors()
        assert killed == []
    finally:
        bt._active_sessions.clear()
        bt._active_sessions.update(saved)
