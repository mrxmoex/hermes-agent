"""/diff must not dump bot-desktop/ via git --no-index or a tracked diff.

Same always-on denylist as the dashboard git rail (finding 30): an untracked
or modified cookie jar / lease in the session cwd is injected into the
conversation by collect_working_diff. Path-name match is independent of
HERMES_HOME so a copied jar sitting in a project repo is refused too.
"""

from __future__ import annotations

import os
import shutil
import subprocess
from pathlib import Path

import pytest

from tools.working_diff import collect_working_diff

pytestmark = pytest.mark.skipif(
    shutil.which("git") is None, reason="git required for working-diff tests"
)

_SECRET = "LIVE-COOKIE-JAR-SECRET"


def _git(repo: Path, *args: str) -> None:
    subprocess.run(
        ["git", *args], cwd=repo, check=True, capture_output=True,
        env={
            "HOME": str(repo),
            "GIT_AUTHOR_NAME": "t",
            "GIT_AUTHOR_EMAIL": "t@t",
            "GIT_COMMITTER_NAME": "t",
            "GIT_COMMITTER_EMAIL": "t@t",
            "PATH": os.environ["PATH"],
        },
    )


@pytest.fixture()
def repo(tmp_path: Path) -> Path:
    d = tmp_path / "repo"
    d.mkdir()
    _git(d, "init", "-q")
    (d / "tracked.py").write_text("print('hello')\n", encoding="utf-8")
    _git(d, "add", "-A")
    _git(d, "commit", "-q", "-m", "init")
    return d


def _write_jar(repo: Path) -> Path:
    lease = repo / "bot-desktop" / "lease.json"
    lease.parent.mkdir(parents=True)
    lease.write_text(_SECRET + "\n", encoding="utf-8")
    return lease


def test_untracked_bot_desktop_is_not_dumped(repo: Path):
    _write_jar(repo)
    (repo / "notes.py").write_text("ok = True\n", encoding="utf-8")

    result = collect_working_diff(str(repo))

    assert result["success"] is True
    assert _SECRET not in result["diff"]
    assert "bot-desktop" not in result["diff"]
    assert not any("bot-desktop" in f for f in result["untracked"])
    assert any(f.endswith("notes.py") or f == "notes.py" for f in result["untracked"])
    assert "ok = True" in result["diff"]


def test_tracked_bot_desktop_change_is_not_dumped(repo: Path):
    lease = _write_jar(repo)
    _git(repo, "add", "-A")
    _git(repo, "commit", "-q", "-m", "jar")
    lease.write_text(_SECRET + "-CHANGED\n", encoding="utf-8")
    (repo / "tracked.py").write_text("print('visible')\n", encoding="utf-8")

    result = collect_working_diff(str(repo))

    assert result["success"] is True
    assert _SECRET not in result["diff"]
    assert "bot-desktop" not in result["diff"]
    assert "+print('visible')" in result["diff"]


def test_symlink_alias_of_the_jar_is_not_dumped(repo: Path):
    lease = _write_jar(repo)
    alias = repo / "alias.json"
    alias.symlink_to(lease)

    result = collect_working_diff(str(repo))

    assert result["success"] is True
    assert _SECRET not in result["diff"]
    assert "alias.json" not in result.get("untracked", [])


def test_explicit_pathspec_of_the_jar_is_empty(repo: Path):
    _write_jar(repo)

    result = collect_working_diff(str(repo), paths=["bot-desktop/lease.json"])

    assert result["success"] is True
    assert _SECRET not in result["diff"]
    assert result.get("empty") is True
