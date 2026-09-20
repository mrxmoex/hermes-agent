"""Dashboard git --no-index / status must not open bot-desktop/.

Finding 26 closed /api/fs and /api/files. The coding-rail git routes
still resolved ``path`` via ``_fs_path`` only, then ``git diff --no-index``
dumped untracked Cookies / lease.json. Same always-on denylist; not a
lease-gated terminal fence.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from hermes_cli import web_git


def _git(cwd: Path, *args: str) -> None:
    subprocess.run(["git", *args], cwd=cwd, check=True, capture_output=True)


def _repo_with_jar(tmp_path: Path) -> Path:
    repo = tmp_path / "repo"
    repo.mkdir()
    _git(repo, "init")
    _git(repo, "config", "user.email", "t@example.com")
    _git(repo, "config", "user.name", "test")
    (repo / "readme.md").write_text("hi\n")
    _git(repo, "add", "readme.md")
    _git(repo, "commit", "-m", "init")
    secret = repo / "bot-desktop" / "lease.json"
    secret.parent.mkdir()
    secret.write_text('{"holder":"human","viewer_id":"SECRET-VIEWER"}\n')
    (repo / "notes.md").write_text("ok\n")
    return repo


def test_review_diff_does_not_dump_bot_desktop_lease(tmp_path):
    repo = _repo_with_jar(tmp_path)
    leaked = web_git.review_diff(str(repo), "bot-desktop/lease.json", "uncommitted", None, False)
    assert leaked == ""
    ordinary = web_git.review_diff(str(repo), "notes.md", "uncommitted", None, False)
    assert "ok" in ordinary
    assert "SECRET-VIEWER" not in ordinary


def test_file_diff_and_all_add_refuse_cookie_jar(tmp_path):
    repo = _repo_with_jar(tmp_path)
    cookies = repo / "bot-desktop" / "browser-profile" / "Default" / "Cookies"
    cookies.parent.mkdir(parents=True)
    cookies.write_bytes(b"SQLite format 3\0COOKIE-SECRET")
    assert web_git.file_diff_vs_head(str(repo), "bot-desktop/browser-profile/Default/Cookies") == ""
    assert web_git._all_add_diff(str(repo), "bot-desktop/lease.json") == ""
    assert "SECRET-VIEWER" not in web_git.file_diff_vs_head(str(repo), "notes.md")


def test_untracked_insertions_does_not_read_lease(tmp_path):
    repo = _repo_with_jar(tmp_path)
    assert web_git._untracked_insertions(str(repo), "bot-desktop/lease.json") == 0
    assert web_git._untracked_insertions(str(repo), "notes.md") >= 1


def test_review_list_and_status_hide_bot_desktop(tmp_path):
    repo = _repo_with_jar(tmp_path)
    listed = web_git.review_list(str(repo), "uncommitted", None)
    paths = [row["path"] for row in listed["files"]]
    assert "notes.md" in paths
    assert not any("bot-desktop" in path for path in paths)
    status = web_git.repo_status(str(repo))
    assert status is not None
    assert not any("bot-desktop" in row["path"] for row in status["files"])


def test_stage_and_revert_refuse_bot_desktop(tmp_path):
    repo = _repo_with_jar(tmp_path)
    with pytest.raises(RuntimeError, match="sensitive"):
        web_git.review_stage(str(repo), "bot-desktop/lease.json")
    with pytest.raises(RuntimeError, match="sensitive"):
        web_git.review_revert(str(repo), "bot-desktop/lease.json")
    web_git.review_stage(str(repo), None)
    staged = subprocess.run(
        ["git", "diff", "--cached", "--name-only"],
        cwd=repo, capture_output=True, text=True, check=True,
    ).stdout
    assert "notes.md" in staged
    assert "bot-desktop" not in staged
    assert (repo / "bot-desktop" / "lease.json").read_text().startswith('{"holder"')


def test_resolved_symlink_into_bot_desktop_is_sensitive(tmp_path):
    repo = _repo_with_jar(tmp_path)
    (repo / "alias.json").symlink_to(repo / "bot-desktop" / "lease.json")
    assert web_git._is_sensitive_git_target(str(repo), "alias.json") is True
    assert web_git.review_diff(str(repo), "alias.json", "uncommitted", None, False) == ""
