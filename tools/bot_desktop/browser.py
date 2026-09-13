"""The bot's browser on its Bot Desktop: one executable, one persistent user-data-dir per profile.

The agent drives Chromium through agent-browser; a human who takes over clicks the dock's Browser
icon. Both must be THE SAME browser — same binary, same ``--user-data-dir`` — or the human logs in
to a jar the bot never sees. Chromium's singleton makes a second launch on the same user-data-dir
open a window in the running instance, which is exactly the hand-over we want — in ONE direction. When the
human's dock instance is already up, agent-browser's own launch is forwarded to it and dies without a
DevTools endpoint, so the dock exposes a debugging port and the agent ATTACHES to it (see
:func:`running_instance_cdp_port`) instead of launching.
"""

from __future__ import annotations

import glob
import os
import shlex
import shutil
import socket
from pathlib import Path
from typing import Optional, Tuple

from tools.bot_desktop import runtime

_SYSTEM_BROWSERS = ("google-chrome", "google-chrome-stable", "chromium", "chromium-browser")


def _path_is_under(child: Path, parent: Path) -> bool:
    try:
        child.resolve().relative_to(parent.resolve())
        return True
    except (OSError, ValueError):
        return False


def profile_dir() -> Path:
    """User-data-dir the bot's browser uses on this profile's screen.

    A process-wide ``AGENT_BROWSER_PROFILE`` is the launch profile's jar. Honor
    it only when this call is unscoped, or when the pin already lives under
    the active ``get_hermes_home()``. Under multiplex the override is a
    different home — using the launch pin would share the cookie jar the
    human typed into on another bot.
    """
    pinned = os.environ.get("AGENT_BROWSER_PROFILE", "").strip()
    if pinned and os.path.isabs(pinned):
        pin = Path(pinned)
        from hermes_constants import get_hermes_home, get_hermes_home_override
        if get_hermes_home_override() is None or _path_is_under(pin, Path(get_hermes_home())):
            return pin
    return runtime.state_dir() / "browser-profile"


def executable() -> Optional[str]:
    """The Chromium agent-browser launches: an explicit ``AGENT_BROWSER_EXECUTABLE_PATH``, else the newest
    Playwright Chromium it bundles, else a system Chrome/Chromium. ``None`` when there is none."""
    explicit = os.environ.get("AGENT_BROWSER_EXECUTABLE_PATH", "").strip()
    if explicit and os.access(explicit, os.X_OK):
        return explicit
    from tools.browser_tool_install import _chromium_search_roots
    candidates = sorted(
        (p for root in _chromium_search_roots() for p in glob.glob(os.path.join(root, "chromium-*", "chrome-linux*", "chrome"))),
        key=os.path.getmtime, reverse=True)
    for exe in candidates:
        if os.access(exe, os.X_OK):
            return exe
    return next((shutil.which(name) for name in _SYSTEM_BROWSERS if shutil.which(name)), None)


def dock_launch() -> Optional[Tuple[str, str]]:
    """``(executable, user_data_dir)`` for the dock's Browser icon, or ``None`` when no Chromium exists."""
    exe = executable()
    return (exe, str(profile_dir())) if exe else None


def dock_argv(exe: str, user_data_dir: str) -> list[str]:
    """Argv the dock's Browser icon must run: this binary, this profile's user-data-dir.

    ``--remote-debugging-port=0`` makes a human-started instance attachable (Chromium
    writes the chosen port to ``<user-data-dir>/DevToolsActivePort``); first-run /
    default-browser dialogs would sit between the human and the bot's tabs.
    """
    # --test-type hides the "Chrome for Testing is only for automated testing" and unsupported-flag
    # (--no-sandbox as root) infobars, which otherwise sit at the top of the human's takeover view.
    return [exe, f"--user-data-dir={user_data_dir}", "--remote-debugging-port=0",
            "--no-first-run", "--no-default-browser-check", "--test-type"]


def dock_command(exe: str, user_data_dir: str) -> str:
    """Shell-safe ``Exec=`` line for the dock icon. Paths with spaces (a ``HERMES_HOME``
    under ``My Home``, a Chrome-for-Testing install) must stay one argv or the human
    lands in a different cookie jar than the bot."""
    return shlex.join(dock_argv(exe, user_data_dir))


def running_instance_cdp_port(user_data_dir: str, *, exclude_session: Optional[str] = None) -> Optional[int]:
    """DevTools port of a Chromium currently running on ``user_data_dir``, or ``None``.

    Both files outlive a crashed or closed Chromium: ``SingletonLock`` is a symlink to ``host-pid`` and
    ``DevToolsActivePort`` keeps the last port, so the pid must be alive AND the port must accept a
    connection before it is trusted. An instance agent-browser launched for ``exclude_session`` itself is
    reported as ``None``: its daemon already owns that browser, and handing it ``--cdp`` would make it
    close the browser as a config change and then attach to the port that just died with it.
    """
    try:
        with open(os.path.join(user_data_dir, "DevToolsActivePort"), encoding="utf-8") as fh:
            port_line = fh.readline().strip()
        target = os.readlink(os.path.join(user_data_dir, "SingletonLock"))
    except OSError:
        return None
    _host, _, pid_text = target.rpartition("-")
    if not (port_line.isdigit() and pid_text.isdigit()) or not _pid_alive(int(pid_text)):
        return None
    if exclude_session and _launched_by_session(int(pid_text)) == exclude_session:
        return None
    port = int(port_line)
    try:
        with socket.create_connection(("127.0.0.1", port), timeout=0.5):
            pass
    except OSError:
        return None
    try:
        if Path(user_data_dir).resolve() == profile_dir().resolve():
            remember_dock_cdp_port(port)
    except OSError:
        pass
    return port


_DOCK_PORT_FILE = "dock-cdp-port"


def _dock_port_path() -> Path:
    return runtime.state_dir() / _DOCK_PORT_FILE


def persist_live_dock_cdp_port() -> Optional[int]:
    """Best-effort stamp of the live dock DevTools port for this profile.

    Human-first Take over can happen before any agent browser call has
    seen ``DevToolsActivePort``. Persist now, while the probe still
    works, so a later miss cannot treat this jar as another Chrome.
    """
    try:
        port = running_instance_cdp_port(str(profile_dir()))
    except Exception:
        return None
    if port is not None:
        remember_dock_cdp_port(port)
    return port


def remember_dock_cdp_port(port: int) -> None:
    """Persist the last live dock DevTools port for this profile.

    Take over can unlink ``DevToolsActivePort`` while Chromium is still up.
    A later process that never saw the live probe still has to treat this
    port as the dock — not another Chrome.
    """
    if not isinstance(port, int) or not (1 <= port <= 65535):
        return
    path = _dock_port_path()
    tmp = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        try:
            path.parent.chmod(0o700)
        except OSError:
            pass
        tmp.write_text(str(port), encoding="utf-8")
        try:
            os.chmod(tmp, 0o600)
        except OSError:
            pass
        os.replace(tmp, path)
        try:
            os.chmod(path, 0o600)
        except OSError:
            pass
    except OSError:
        try:
            tmp.unlink()
        except OSError:
            pass


def last_known_dock_cdp_port() -> Optional[int]:
    """Last persisted dock DevTools port for this profile, or ``None``."""
    try:
        port = int(_dock_port_path().read_text(encoding="utf-8").strip())
    except (OSError, ValueError):
        return None
    return port if 1 <= port <= 65535 else None


def _launched_by_session(chromium_pid: int) -> Optional[str]:
    """``AGENT_BROWSER_SESSION`` of the agent-browser daemon that spawned ``chromium_pid``, or ``None``
    for a human-started (dock) instance. Chromium itself gets a scrubbed environment, so the daemon's
    ``/proc/<ppid>/environ`` is the marker (Linux-only, same user)."""
    try:
        with open(f"/proc/{chromium_pid}/status", encoding="utf-8") as fh:
            ppid = next((int(line.split()[1]) for line in fh if line.startswith("PPid:")), 0)
        with open(f"/proc/{ppid}/environ", "rb") as fh:
            raw = fh.read()
    except (OSError, ValueError):
        return None
    for item in raw.split(b"\0"):
        key, sep, value = item.partition(b"=")
        if sep and key == b"AGENT_BROWSER_SESSION":
            return value.decode("utf-8", "replace") or None
    return None


def shared_chromium_owner_session(user_data_dir: Optional[str] = None) -> Optional[str]:
    """``AGENT_BROWSER_SESSION`` that spawned Chromium on this profile, or ``None``.

    ``None`` means the instance is dock/launcher-owned (or down). Tree-killing
    an agent-browser daemon is then leftover-CDP cleanup, not a Browser kill.
    When this equals a session's ``session_name``, that daemon *is* the parent
    of the page the human is typing into and must stay reserved.
    """
    if user_data_dir is None:
        user_data_dir = str(profile_dir())
    try:
        target = os.readlink(os.path.join(user_data_dir, "SingletonLock"))
    except OSError:
        return None
    _host, _, pid_text = target.rpartition("-")
    if not pid_text.isdigit() or not _pid_alive(int(pid_text)):
        return None
    return _launched_by_session(int(pid_text))


def _pid_alive(pid: int) -> bool:
    import psutil
    return psutil.pid_exists(pid)


def env_for_agent(env: dict) -> dict:
    """Pin agent-browser to THIS profile's browser identity.

    Always set ``AGENT_BROWSER_PROFILE`` — ``setdefault`` would keep a launch
    pin copied from ``os.environ`` after ``profile_dir()`` already refused it.
    """
    env["AGENT_BROWSER_PROFILE"] = str(profile_dir())
    exe = executable()
    if exe:
        env.setdefault("AGENT_BROWSER_EXECUTABLE_PATH", exe)
    return env
