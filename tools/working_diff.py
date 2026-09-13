"""Working-tree git diff collection shared by the CLI and gateway ``/diff``.

Surface-agnostic so the CLI (colored terminal) and gateway (fenced, truncated
messages) render the same data. Modes: ``working`` (unstaged + untracked),
``staged`` (``git diff --cached``), ``all`` (everything since HEAD plus untracked).
Untracked files are folded in via ``git diff --no-index /dev/null <file>`` so
brand-new files show as additions instead of being invisible.
"""

from __future__ import annotations

import os
import shutil
import subprocess
from contextlib import suppress
from typing import Dict, List

from hermes_cli._subprocess_compat import harden_git_argv, noninteractive_git_env

_GIT_TIMEOUT = 15
_MAX_UNTRACKED_FILES = 50  # sanity cap so a node_modules explosion can't hang us

_MODE_ARGS = {
    "working": ["diff"],
    "staged": ["diff", "--cached"],
    "all": ["diff", "HEAD"],
}
VALID_MODES = tuple(_MODE_ARGS)


def _run(args: List[str], cwd: str, timeout: int = _GIT_TIMEOUT):
    """Run git, returning (returncode, stdout). Never raises on git failure. Hardened against a
    malicious repo's ``.git/config`` (GHSA-7x36-8jrh-v4pw): ``noninteractive_git_env`` disables
    fsmonitor/hooks/pager/editor/credential sinks and ``harden_git_argv`` appends ``--no-ext-diff
    --no-textconv`` to diff-rendering subcommands so attribute-scoped drivers can't execute either."""
    proc = subprocess.run(
        ["git", "-c", "core.quotePath=false", *harden_git_argv(args)],
        cwd=cwd, capture_output=True, text=True, timeout=timeout, encoding="utf-8", errors="replace",
        stdin=subprocess.DEVNULL, env=noninteractive_git_env(),
    )
    return proc.returncode, proc.stdout


def _is_sensitive_diff_path(cwd: str, rel: str) -> bool:
    """True when ``rel`` is a credential / Bot Screen store and must not be dumped.

    ``git diff --no-index /dev/null <file>`` prints the whole file. Same
    always-on denylist as the dashboard git rail (finding 30) and
    ``@file:`` (``agent.file_safety.is_sensitive_managed_path``). Fail-closed.
    """
    if not rel or not str(rel).strip():
        return False
    from agent.file_safety import is_sensitive_managed_path
    if is_sensitive_managed_path(rel):
        return True
    try:
        return is_sensitive_managed_path(str(os.path.join(cwd, rel)))
    except (OSError, ValueError):
        return True


def _untracked_files(cwd: str) -> List[str]:
    code, out = _run(["ls-files", "--others", "--exclude-standard"], cwd)
    if code != 0:
        return []
    return [line for line in out.splitlines() if line.strip() and not _is_sensitive_diff_path(cwd, line)]


def _untracked_diff(cwd: str, files: List[str]) -> str:
    """Render untracked files as new-file diffs via ``git diff --no-index``."""
    chunks: List[str] = []
    safe = [rel for rel in files if not _is_sensitive_diff_path(cwd, rel)]
    for rel in safe[:_MAX_UNTRACKED_FILES]:
        with suppress(subprocess.TimeoutExpired, OSError):
            # --no-index exits 1 when files differ — the success path, so the code is ignored.
            _, out = _run(["diff", "--no-index", "--", os.devnull, rel], cwd)
            if out.strip():
                chunks.append(out.rstrip("\n"))
    if len(safe) > _MAX_UNTRACKED_FILES:
        chunks.append(f"... ({len(safe) - _MAX_UNTRACKED_FILES} more untracked files not shown)")
    return "\n".join(chunks)


def collect_working_diff(cwd: str, mode: str = "working", paths: List[str] | None = None) -> Dict:
    """Collect a git diff of the working directory: ``{"success", "stat", "diff", "untracked", "empty"}``
    on success or ``{"success": False, "error": ...}`` when git is unavailable / not a repo. ``paths``
    restricts the diff to pathspecs (sensitive Bot Screen / credential
    targets are dropped); untracked files are then skipped."""
    if mode not in _MODE_ARGS:
        return {"success": False, "error": f"Unknown mode '{mode}'. Use: {', '.join(VALID_MODES)}"}
    if not shutil.which("git"):
        return {"success": False, "error": "git is not installed or not on PATH."}
    try:
        code, _ = _run(["rev-parse", "--is-inside-work-tree"], cwd, timeout=5)
    except (subprocess.TimeoutExpired, OSError) as e:
        return {"success": False, "error": f"git failed: {e}"}
    if code != 0:
        return {"success": False, "error": "Not a git repository."}

    base_args = _MODE_ARGS[mode]
    requested = [p for p in (paths or []) if p and not _is_sensitive_diff_path(cwd, p)]
    if paths and not requested:
        return {"success": True, "stat": "", "diff": "", "untracked": [], "empty": True}
    pathspec = ["--", *requested] if requested else []
    try:
        _, name_out = _run([*base_args, "--name-only", *pathspec], cwd)
        safe_names = [p for p in name_out.splitlines() if p.strip() and not _is_sensitive_diff_path(cwd, p)]
        if not safe_names:
            stat_out, diff_out = "", ""
        else:
            safe_spec = ["--", *safe_names]
            _, stat_out = _run([*base_args, "--stat", *safe_spec], cwd)
            _, diff_out = _run([*base_args, *safe_spec], cwd, timeout=_GIT_TIMEOUT * 2)
        untracked = _untracked_files(cwd) if mode in ("working", "all") and not paths else []
        untracked_diff = _untracked_diff(cwd, untracked) if untracked else ""
    except subprocess.TimeoutExpired:
        return {"success": False, "error": "git diff timed out."}
    except OSError as e:
        return {"success": False, "error": f"git failed: {e}"}

    stat, diff = stat_out.strip(), diff_out.strip()
    if untracked_diff:
        diff = f"{diff}\n{untracked_diff}".strip()
    result = {"success": True, "stat": stat, "diff": diff, "untracked": untracked}
    if not stat and not diff and not untracked:
        result["empty"] = True
    return result
