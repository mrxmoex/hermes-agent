"""Bot Desktop leftover CDP supervisors obey the screen lease."""

from __future__ import annotations

import asyncio
import json
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
    monkeypatch.setattr(cdp, "_get_cdp_override", lambda: "http://127.0.0.1:9333")
    started = []
    import tools.browser_supervisor as bs
    registry = MagicMock()
    registry.get_or_start.side_effect = lambda **k: started.append(k)
    monkeypatch.setattr(bs, "SUPERVISOR_REGISTRY", registry)
    lease.acquire("human-viewer")
    cdp._ensure_cdp_supervisor("review")
    assert started == []


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
