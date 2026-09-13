"""Bot Desktop leftover CDP supervisors obey the screen lease."""

from __future__ import annotations

import asyncio
from unittest.mock import MagicMock

import pytest

from tools.bot_desktop import lease, runtime


@pytest.fixture(autouse=True)
def _fresh(monkeypatch):
    lease._reset_for_tests()
    monkeypatch.setattr(runtime, "published_env", lambda: {"DISPLAY": ":37"})
    yield
    lease._reset_for_tests()


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
    assert supervisor_may_touch_page(sup.cdp_url) is True
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
