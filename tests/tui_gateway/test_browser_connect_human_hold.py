"""/browser connect and disconnect must not swap CDP while a human holds.

Both RPCs reap every in-process browser session around the override swap.
That used to pass force=True and tree-kill the shared Chromium a human is
mid-login in. Refuse the swap until they hand back.
"""

from __future__ import annotations

import os

import pytest

from tools.bot_desktop import lease
from tui_gateway import server


def test_browser_manage_connect_refuses_while_human_holds(monkeypatch):
    monkeypatch.delenv("BROWSER_CDP_URL", raising=False)
    lease.acquire("human-viewer")
    try:
        resp = server.handle_request(
            {
                "id": "1",
                "method": "browser.manage",
                "params": {"action": "connect", "url": "http://127.0.0.1:9222"},
            }
        )
        assert "error" in resp, resp
        assert "human holds" in resp["error"]["message"].lower()
        assert "BROWSER_CDP_URL" not in os.environ
    finally:
        lease._reset_for_tests()


def test_browser_manage_disconnect_refuses_while_human_holds(monkeypatch):
    monkeypatch.setenv("BROWSER_CDP_URL", "http://127.0.0.1:9222")
    lease.acquire("human-viewer")
    try:
        resp = server.handle_request(
            {"id": "1", "method": "browser.manage", "params": {"action": "disconnect"}}
        )
        assert "error" in resp, resp
        assert "human holds" in resp["error"]["message"].lower()
        assert os.environ.get("BROWSER_CDP_URL") == "http://127.0.0.1:9222"
    finally:
        lease._reset_for_tests()


def test_browser_manage_connect_persists_dock_port_before_devtools_miss(monkeypatch):
    """TUI connect saw a live dock over HTTP and never stamped the port.
    A DevTools miss before leftover watch / computer_use then admitted
    leftover CDP as another Chrome."""
    import tools.bot_desktop.browser as bdb
    from tools.bot_desktop.lease import HumanHasControl
    from tools.browser_tool_session import (
        _admit_resolved_cdp_for_attach,
        _admit_shared_browser,
        _cdp_url_is_bot_desktop_browser,
        _last_dock_cdp_port,
        _reset_dock_port_memory_for_tests,
    )

    monkeypatch.delenv("BROWSER_CDP_URL", raising=False)
    lease._reset_for_tests()
    _reset_dock_port_memory_for_tests()
    monkeypatch.setattr(bdb, "running_instance_cdp_port", lambda *a, **k: 9333)

    class _Resp:
        status = 200

        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

    monkeypatch.setattr("urllib.request.urlopen", lambda *a, **k: _Resp())
    monkeypatch.setattr("tools.browser_tool_lifecycle.cleanup_all_browsers", lambda: None)
    resp = server.handle_request(
        {
            "id": "1",
            "method": "browser.manage",
            "params": {"action": "connect", "url": "http://127.0.0.1:9333"},
        }
    )
    assert resp.get("result", {}).get("connected") is True, resp
    assert bdb.last_known_dock_cdp_port() == 9333

    dock = "ws://127.0.0.1:9333/devtools/browser/x"
    other = "ws://127.0.0.1:9222/devtools/browser/x"
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
    monkeypatch.delenv("BROWSER_CDP_URL", raising=False)
    lease._reset_for_tests()
    _reset_dock_port_memory_for_tests()


def test_cli_browser_connect_refuses_while_human_holds(monkeypatch, capsys):
    """Interactive CLI ``/browser connect`` used to swap CDP while a human
    holds — TUI ``browser.manage`` already refused that leftover attach."""
    from hermes_cli.cli_commands_mixin import _browser_connect

    monkeypatch.delenv("BROWSER_CDP_URL", raising=False)
    lease.acquire("human-viewer")
    try:
        _browser_connect(object(), "http://127.0.0.1:9333")
        out = capsys.readouterr().out.lower()
        assert "human holds" in out
        assert "BROWSER_CDP_URL" not in os.environ
    finally:
        lease._reset_for_tests()


def test_cli_browser_disconnect_refuses_while_human_holds(monkeypatch, capsys):
    from hermes_cli.cli_commands_mixin import _browser_disconnect

    monkeypatch.setenv("BROWSER_CDP_URL", "http://127.0.0.1:9222")
    lease.acquire("human-viewer")
    try:
        _browser_disconnect(object())
        out = capsys.readouterr().out.lower()
        assert "human holds" in out
        assert os.environ.get("BROWSER_CDP_URL") == "http://127.0.0.1:9222"
    finally:
        lease._reset_for_tests()
        monkeypatch.delenv("BROWSER_CDP_URL", raising=False)


def test_browser_manage_connect_allowed_when_agent_holds(monkeypatch):
    """The refuse is lease-gated, not a blanket block on /browser connect."""
    monkeypatch.delenv("BROWSER_CDP_URL", raising=False)
    lease._reset_for_tests()
    assert lease.human_holds() is False

    class _Resp:
        status = 200

        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

    monkeypatch.setattr(
        "urllib.request.urlopen", lambda *a, **k: _Resp()
    )
    monkeypatch.setattr(
        "tools.browser_tool_lifecycle.cleanup_all_browsers", lambda: None
    )
    resp = server.handle_request(
        {
            "id": "1",
            "method": "browser.manage",
            "params": {"action": "connect", "url": "http://127.0.0.1:9333"},
        }
    )
    assert resp.get("result", {}).get("connected") is True, resp
    assert os.environ.get("BROWSER_CDP_URL") == "http://127.0.0.1:9333"
    monkeypatch.delenv("BROWSER_CDP_URL", raising=False)
