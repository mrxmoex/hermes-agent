"""file.attach must not copy bot-desktop/ (or other file_safety denies) into
the session workspace.

The RPC used to read_bytes() any gateway-visible path outside cwd, then
stage it under attachments/. A later @file: of that copy is allowed —
so attaching ~/.hermes/bot-desktop/lease.json leaked the raw viewer_id
(and Cookies) into the agent turn. Same always-on denylist as read_file;
not a lease-gated terminal fence.
"""

from __future__ import annotations

import types
from pathlib import Path

import pytest

from hermes_constants import get_hermes_home
from tui_gateway import server


@pytest.fixture
def attach_session(tmp_path, monkeypatch):
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    home = tmp_path / "home"
    sid = "attach-safety"
    session = {
        "agent": types.SimpleNamespace(),
        "attached_images": [],
        "cwd": str(workspace),
        "image_counter": 0,
        "profile_home": str(home),
        "running": False,
        "session_key": sid,
    }
    server._sessions[sid] = session
    fake_cli = types.ModuleType("cli")
    fake_cli._detect_file_drop = lambda raw: None
    fake_cli._split_path_input = lambda raw: (raw, "")
    monkeypatch.setitem(__import__("sys").modules, "cli", fake_cli)
    yield sid, home, fake_cli
    server._sessions.pop(sid, None)


def _attach(sid: str, path: str) -> dict:
    return server.handle_request(
        {
            "id": "1",
            "method": "file.attach",
            "params": {"session_id": sid, "path": path},
        }
    )


def _assert_not_staged(home: Path) -> None:
    staged = home / "attachments"
    assert not staged.exists() or list(staged.iterdir()) == []


def test_file_attach_refuses_bot_desktop_lease(attach_session):
    sid, home, fake_cli = attach_session
    lease = get_hermes_home() / "bot-desktop" / "lease.json"
    lease.parent.mkdir(parents=True, exist_ok=True)
    lease.write_text('{"holder":"human","viewer_id":"secret-viewer"}\n')
    fake_cli._resolve_attachment_path = lambda raw: lease

    resp = _attach(sid, str(lease))

    assert "error" in resp, resp
    assert "Bot Desktop" in resp["error"]["message"]
    _assert_not_staged(home)


def test_file_attach_refuses_bot_desktop_cookie_jar(attach_session):
    sid, home, fake_cli = attach_session
    cookies = (
        get_hermes_home() / "bot-desktop" / "browser-profile" / "Default" / "Cookies"
    )
    cookies.parent.mkdir(parents=True, exist_ok=True)
    cookies.write_bytes(b"stolen-cookies")
    fake_cli._resolve_attachment_path = lambda raw: cookies

    resp = _attach(sid, str(cookies))

    assert "error" in resp, resp
    assert "Bot Desktop" in resp["error"]["message"]
    _assert_not_staged(home)


def test_file_attach_still_copies_ordinary_outside_file(attach_session, tmp_path):
    sid, home, fake_cli = attach_session
    source = tmp_path / "notes.txt"
    source.write_text("ok\n")
    fake_cli._resolve_attachment_path = lambda raw: source

    resp = _attach(sid, str(source))

    assert resp["result"]["attached"] is True
    assert (home / "attachments" / "notes.txt").read_text() == "ok\n"
