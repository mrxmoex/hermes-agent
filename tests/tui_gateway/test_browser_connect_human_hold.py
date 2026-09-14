"""/browser connect and disconnect must not swap CDP while a human holds.

Both RPCs reap every in-process browser session around the override swap.
That used to pass force=True and tree-kill the shared Chromium a human is
mid-login in. Refuse the swap until they hand back.
"""

from __future__ import annotations

import os
from pathlib import Path

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


def _sibling_homes(tmp_path):
    launch = Path(tmp_path) / "launch"
    bot = Path(tmp_path) / "bot"
    launch.mkdir()
    bot.mkdir()
    return launch, bot


def _clear_lease_files(*homes):
    from tools.bot_desktop.lease import _path

    for home in homes:
        for f in (_path(str(home)), _path(str(home)).with_suffix(".lock")):
            try:
                f.unlink()
            except OSError:
                pass


def _http_ok(monkeypatch):
    class _Resp:
        status = 200

        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

    monkeypatch.setattr("urllib.request.urlopen", lambda *a, **k: _Resp())
    monkeypatch.setattr("tools.browser_tool_lifecycle.cleanup_all_browsers", lambda: None)


def test_browser_manage_connect_uses_session_home_after_multiplex(monkeypatch, tmp_path):
    """TUI / Desktop send session_id, not profile. After a multiplex turn
    the process home is launch; missing lease fail-opens as agent. Connect
    must refuse on the session bot a human holds — not probe launch CDP."""
    from hermes_constants import reset_hermes_home_override, set_hermes_home_override

    launch, bot = _sibling_homes(tmp_path)
    sid = "review"
    server._sessions[sid] = {"profile_home": str(bot), "session_key": sid}
    monkeypatch.delenv("BROWSER_CDP_URL", raising=False)
    token_bot = set_hermes_home_override(str(bot))
    try:
        lease.acquire("human-viewer")
    finally:
        reset_hermes_home_override(token_bot)

    token_launch = set_hermes_home_override(str(launch))
    try:
        assert lease.human_holds() is False
        resp = server.handle_request(
            {
                "id": "1",
                "method": "browser.manage",
                "params": {
                    "action": "connect",
                    "session_id": sid,
                    "url": "http://127.0.0.1:9333",
                },
            }
        )
        assert "error" in resp, resp
        assert "human holds" in resp["error"]["message"].lower()
        assert "BROWSER_CDP_URL" not in os.environ
    finally:
        reset_hermes_home_override(token_launch)
        server._sessions.pop(sid, None)
        _clear_lease_files(launch, bot)


def test_browser_manage_does_not_fence_on_the_launch_profile_lease(monkeypatch, tmp_path):
    """A human on the launch bot must not void /browser connect for a sibling session."""
    from hermes_constants import reset_hermes_home_override, set_hermes_home_override

    launch, bot = _sibling_homes(tmp_path)
    sid = "review"
    server._sessions[sid] = {"profile_home": str(bot), "session_key": sid}
    monkeypatch.delenv("BROWSER_CDP_URL", raising=False)
    _http_ok(monkeypatch)

    token_launch = set_hermes_home_override(str(launch))
    try:
        lease.acquire("human-viewer")
        assert lease.human_holds() is True
        resp = server.handle_request(
            {
                "id": "1",
                "method": "browser.manage",
                "params": {
                    "action": "connect",
                    "session_id": sid,
                    "url": "http://127.0.0.1:9333",
                },
            }
        )
        assert resp.get("result", {}).get("connected") is True, resp
    finally:
        reset_hermes_home_override(token_launch)
        server._sessions.pop(sid, None)
        monkeypatch.delenv("BROWSER_CDP_URL", raising=False)
        _clear_lease_files(launch, bot)


def test_browser_manage_persists_dock_under_session_home_after_multiplex(
    monkeypatch, tmp_path,
):
    """Connect must stamp the session bot's ``dock-cdp-port``, not invent
    a port on the launch profile the process is sitting in."""
    import tools.bot_desktop.browser as bdb
    from hermes_constants import reset_hermes_home_override, set_hermes_home_override
    from tools.browser_tool_session import _reset_dock_port_memory_for_tests

    launch, bot = _sibling_homes(tmp_path)
    sid = "review"
    server._sessions[sid] = {"profile_home": str(bot), "session_key": sid}
    monkeypatch.delenv("BROWSER_CDP_URL", raising=False)
    _http_ok(monkeypatch)
    _reset_dock_port_memory_for_tests()

    bot_profile = (bot / "bot-desktop" / "browser-profile").resolve()

    def live_port(user_data_dir, **_k):
        try:
            return 9333 if Path(user_data_dir).resolve() == bot_profile else None
        except OSError:
            return None

    monkeypatch.setattr(bdb, "running_instance_cdp_port", live_port)

    token_launch = set_hermes_home_override(str(launch))
    try:
        assert bdb.last_known_dock_cdp_port() is None
        resp = server.handle_request(
            {
                "id": "1",
                "method": "browser.manage",
                "params": {
                    "action": "connect",
                    "session_id": sid,
                    "url": "http://127.0.0.1:9333",
                },
            }
        )
        assert resp.get("result", {}).get("connected") is True, resp
        assert bdb.last_known_dock_cdp_port() is None
    finally:
        reset_hermes_home_override(token_launch)

    token_bot = set_hermes_home_override(str(bot))
    try:
        assert bdb.last_known_dock_cdp_port() == 9333
    finally:
        reset_hermes_home_override(token_bot)
        server._sessions.pop(sid, None)
        monkeypatch.delenv("BROWSER_CDP_URL", raising=False)
        _reset_dock_port_memory_for_tests()
        _clear_lease_files(launch, bot)
