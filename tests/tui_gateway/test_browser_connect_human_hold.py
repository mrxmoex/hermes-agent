"""/browser connect and disconnect must not swap CDP while a human holds.

Both RPCs reap every in-process browser session around the override swap.
That used to pass force=True and tree-kill the shared Chromium a human is
mid-login in. Refuse the swap until they hand back.
"""

from __future__ import annotations

import os

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
