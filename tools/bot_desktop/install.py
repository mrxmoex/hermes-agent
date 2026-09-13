"""Install the Bot Desktop packages on the gateway host from a Desktop client.

The install runs the distro command from ``runtime.install_command()`` (apt/dnf/pacman) as a child
process on THIS host. Privilege comes from the same masked ``sudo.request`` card the terminal tool
raises: ``sudo -n true`` is probed first (NOPASSWD / cached timestamp hosts never see a prompt); when
a password is needed the caller-supplied ``ask_password`` blocks on the card and the value is written
to sudo's stdin (``-S``) exactly once, never logged, never placed on the command line. Output lines
stream through ``on_line`` so the pane can show apt's progress; the return value is the exit code.

One install per profile at a time; a second request while one runs is refused.
"""

from __future__ import annotations

import contextlib
import logging
import os
import shlex
import signal
import subprocess
import threading
from typing import Callable, Optional

from hermes_constants import hermes_home_key
from tools.bot_desktop import runtime

logger = logging.getLogger(__name__)

_install_lock = threading.Lock()
_running: set[str] = set()
# After SIGKILL the drain is normally instant; this only guards a writer outside the group.
_POST_KILL_DRAIN_S = 3.0


class InstallBusy(RuntimeError):
    pass


def assert_not_running() -> None:
    with _install_lock:
        if hermes_home_key() in _running:
            raise InstallBusy("an install is already running for this profile")


def claim() -> str:
    """Atomically take this profile's install slot; raises :class:`InstallBusy` when taken. A caller that
    claims before handing off to a worker passes ``claimed=True`` to :func:`install_packages`, which then
    owns releasing it — a check-then-spawn pair (``assert_not_running`` + later claim on the worker) lets
    two Install clicks both pass the check."""
    key = hermes_home_key()
    with _install_lock:
        if key in _running:
            raise InstallBusy("an install is already running for this profile")
        _running.add(key)
    return key


def release(key: str) -> None:
    with _install_lock:
        _running.discard(key)


def install_packages(*, ask_password: Callable[[], str], on_line: Callable[[str], None],
                     timeout_seconds: float = 900.0, claimed: bool = False) -> int:
    """Run the package install; returns the process exit code (0 = success, ``-1`` = cancelled).
    ``claimed=True``: the caller already holds the slot via :func:`claim`; it is released here either way."""
    key = hermes_home_key() if claimed else None
    try:
        if not runtime.is_supported_host():
            raise RuntimeError("Bot Desktop runs on Linux gateway hosts only")
        cmd = runtime.install_command()
        if cmd is None:
            raise RuntimeError("no supported package manager (apt-get, dnf, pacman) found on this host")
        if key is None:
            key = claim()
        return _run(cmd, ask_password=ask_password, on_line=on_line, timeout_seconds=timeout_seconds)
    finally:
        if key is not None:
            release(key)


def _sudo_nopasswd() -> bool:
    try:
        return subprocess.run(["sudo", "-n", "true"], capture_output=True, timeout=3,
                              stdin=subprocess.DEVNULL).returncode == 0
    except Exception:
        return False


def _kill_install_tree(proc: subprocess.Popen) -> None:
    """Kill sudo's process group, including root apt/dnf children.

    ``os.killpg`` from unprivileged Hermes can take the sudo leader while a
    root-owned package manager in the same group survives and keeps stdout
    open. The ``for line in proc.stdout`` drain then never EOFs and the
    profile install slot is never released. Ask sudo to SIGKILL the group
    (``-n``: same cached timestamp / NOPASSWD the install itself used),
    then close our read end so the drain cannot block on a surviving writer.
    """
    pgid = None
    with contextlib.suppress(ProcessLookupError, OSError):
        pgid = os.getpgid(proc.pid)
    if pgid is not None:
        with contextlib.suppress(ProcessLookupError, PermissionError, OSError):
            os.killpg(pgid, signal.SIGKILL)  # windows-footgun: ok — Linux-only (is_supported_host)
        with contextlib.suppress(Exception):
            subprocess.run(
                ["sudo", "-n", "kill", "-9", f"-{pgid}"],
                stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                timeout=3,
                check=False,
            )
    if proc.stdout is not None:
        with contextlib.suppress(OSError, ValueError):
            proc.stdout.close()


def _run(cmd: str, *, ask_password: Callable[[], str], on_line: Callable[[str], None],
         timeout_seconds: float) -> int:
    argv = shlex.split(cmd)
    assert argv[0] == "sudo", cmd
    stdin_payload: Optional[str] = None
    if not _sudo_nopasswd():
        password = ask_password() or ""
        if not password:
            on_line("install cancelled: no sudo password provided")
            return -1
        # -S: read the password from stdin; -p '': no prompt text mixed into the streamed output.
        argv = ["sudo", "-S", "-p", "", *argv[1:]]
        stdin_payload = password + "\n"
    on_line(f"$ {cmd}")
    env = {"DEBIAN_FRONTEND": "noninteractive", "LC_ALL": "C.UTF-8"}
    proc = subprocess.Popen(  # windows-footgun: ok — Linux-only (is_supported_host)
        argv, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
        env={**os.environ, **env}, text=True, encoding="utf-8", errors="replace", start_new_session=True)
    try:
        if stdin_payload is not None:
            proc.stdin.write(stdin_payload)  # type: ignore[union-attr]
        proc.stdin.close()  # type: ignore[union-attr]
    except OSError:
        pass
    # The package manager runs in its own session (start_new_session); killing only sudo would leave apt/dnf
    # running as root with the dpkg lock while the slot is released, so the whole group goes. Unprivileged
    # killpg is not enough when the child is root — see :func:`_kill_install_tree`.
    timed_out = threading.Event()

    def _on_timeout() -> None:
        timed_out.set()
        _kill_install_tree(proc)

    timer = threading.Timer(timeout_seconds, _on_timeout)
    timer.start()
    try:
        try:
            for line in proc.stdout:  # type: ignore[union-attr]
                on_line(line.rstrip("\n"))
        except ValueError:
            # Timeout closed stdout from the timer thread; the iterator is done.
            pass
        try:
            return proc.wait(timeout=_POST_KILL_DRAIN_S if timed_out.is_set() else None)
        except subprocess.TimeoutExpired:
            return -1
    finally:
        timer.cancel()
