"""Tests for ACP pre-edit approval gating."""

from __future__ import annotations

import json
import tempfile
from pathlib import Path

from acp_adapter.edit_approval import (
    EditProposal,
    build_acp_edit_tool_call,
    set_edit_approval_requester,
    should_auto_approve_edit,
)
from model_tools import handle_function_call


def teardown_function() -> None:
    set_edit_approval_requester(None)


def test_acp_permission_tool_call_uses_edit_kind_and_diff_content():
    proposal = EditProposal(
        tool_name="write_file",
        path="demo.txt",
        old_text="old\n",
        new_text="new\n",
        arguments={"path": "demo.txt", "content": "new\n"},
    )

    tool_call = build_acp_edit_tool_call(proposal)

    assert tool_call.kind == "edit"
    assert tool_call.status == "pending"
    assert tool_call.rawInput == {"tool": "write_file", "arguments": proposal.arguments}
    assert len(tool_call.content) == 1
    diff = tool_call.content[0]
    assert diff.path == "demo.txt"
    assert diff.oldText == "old\n"
    assert diff.newText == "new\n"








def test_requester_exception_denies_and_does_not_mutate(tmp_path):
    target = tmp_path / "sample.txt"
    target.write_text("before\n", encoding="utf-8")

    def boom(_proposal):
        raise RuntimeError("zed disconnected")

    set_edit_approval_requester(boom)

    result = json.loads(
        handle_function_call(
            "write_file",
            {"path": str(target), "content": "after\n"},
            task_id="acp-edit-exception",
        )
    )

    assert "error" in result
    assert "Edit approval denied" in result["error"]
    assert target.read_text(encoding="utf-8") == "before\n"


def test_patch_replace_rejection_does_not_mutate(tmp_path):
    target = tmp_path / "sample.txt"
    target.write_text("alpha\nbeta\n", encoding="utf-8")

    set_edit_approval_requester(lambda _proposal: False)

    result = json.loads(
        handle_function_call(
            "patch",
            {
                "mode": "replace",
                "path": str(target),
                "old_string": "beta\n",
                "new_string": "gamma\n",
            },
            task_id="acp-patch-reject",
        )
    )

    assert "error" in result
    assert "Edit approval denied" in result["error"]
    assert target.read_text(encoding="utf-8") == "alpha\nbeta\n"








def test_workspace_auto_approval_allows_workspace_and_tmp_but_not_sensitive(tmp_path):
    workspace_file = tmp_path / "src.py"
    # Use tempfile.gettempdir() so this test exercises the same code path on
    # Linux (`/tmp`), macOS (`/private/var/folders/...`) and Windows
    # (`%LOCALAPPDATA%\Temp`). Before the fix this branch only worked on Linux.
    tmp_file = Path(tempfile.gettempdir()) / "hermes-acp-auto-approve-test.txt"
    env_file = tmp_path / ".env"

    assert should_auto_approve_edit(
        EditProposal("write_file", str(workspace_file), None, "x", {}),
        "workspace_session",
        str(tmp_path),
    )
    assert should_auto_approve_edit(
        EditProposal("write_file", str(tmp_file), None, "x", {}),
        "workspace_session",
        str(tmp_path),
    )
    assert not should_auto_approve_edit(
        EditProposal("write_file", str(env_file), None, "SECRET=x", {}),
        "session",
        str(tmp_path),
    )
    assert not should_auto_approve_edit(
        EditProposal("write_file", str(tmp_path / "bot-desktop" / "lease.json"), None, "{}", {}),
        "session",
        str(tmp_path),
    )


def test_acp_edit_approval_does_not_read_bot_desktop_lease(tmp_path, monkeypatch):
    """Diff prep ran before write_file. Without a file_safety gate the ACP
    client received lease.json (raw viewer_id) even when the write was
    later denied. Cross-process Take over stores that id on disk.
    """
    from acp_adapter.edit_approval import build_edit_proposal, maybe_require_edit_approval

    root = tmp_path / ".hermes"
    profile = root / "profiles" / "work"
    profile.mkdir(parents=True)
    monkeypatch.setenv("HERMES_HOME", str(profile))

    secret = '{"holder":"human","viewer_id":"ACP-LEAK-VIEWER","epoch":9}\n'
    target = profile / "bot-desktop" / "lease.json"
    target.parent.mkdir(parents=True)
    target.write_text(secret, encoding="utf-8")

    seen: list[str | None] = []

    def spy(proposal):
        seen.append(proposal.old_text)
        return True

    set_edit_approval_requester(spy)

    blocked = maybe_require_edit_approval(
        "write_file",
        {"path": str(target), "content": '{"holder":"agent","epoch":10}\n'},
    )
    assert blocked is not None
    assert "ACP-LEAK-VIEWER" not in blocked
    assert seen == []

    try:
        build_edit_proposal(
            "write_file",
            {"path": str(target), "content": '{"holder":"agent"}\n'},
        )
        raise AssertionError("build_edit_proposal must not read a protected lease")
    except PermissionError as exc:
        assert "ACP-LEAK-VIEWER" not in str(exc)

    result = json.loads(
        handle_function_call(
            "write_file",
            {"path": str(target), "content": '{"holder":"agent","epoch":10}\n'},
            task_id="acp-bot-desktop-lease",
        )
    )
    raw = json.dumps(result)
    assert "error" in result
    assert "ACP-LEAK-VIEWER" not in raw
    assert target.read_text(encoding="utf-8") == secret


def test_acp_patch_does_not_read_bot_desktop_cookies(tmp_path, monkeypatch):
    from acp_adapter.edit_approval import maybe_require_edit_approval

    root = tmp_path / ".hermes"
    profile = root / "profiles" / "work"
    profile.mkdir(parents=True)
    monkeypatch.setenv("HERMES_HOME", str(profile))

    cookies = profile / "bot-desktop" / "browser-profile" / "Default" / "Cookies"
    cookies.parent.mkdir(parents=True)
    cookies.write_text("STOLEN-COOKIE-JAR", encoding="utf-8")

    seen: list[str | None] = []
    set_edit_approval_requester(lambda proposal: seen.append(proposal.old_text) or False)

    blocked = maybe_require_edit_approval(
        "patch",
        {
            "mode": "replace",
            "path": str(cookies),
            "old_string": "STOLEN-COOKIE-JAR",
            "new_string": "forged",
        },
    )
    assert blocked is not None
    assert "STOLEN-COOKIE-JAR" not in blocked
    assert seen == []
    assert cookies.read_text(encoding="utf-8") == "STOLEN-COOKIE-JAR"
