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
import ipaddress
import os
import shlex
import shutil
import socket
from pathlib import Path
from typing import Optional, Set, Tuple

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


def _chromium_cmdline_tokens(pid: int) -> list[str]:
    """Argv of ``pid`` from ``/proc`` (Linux). Empty on other hosts or a dead pid."""
    try:
        raw = Path(f"/proc/{pid}/cmdline").read_bytes()
    except OSError:
        return []
    return [part.decode("utf-8", "replace") for part in raw.split(b"\0") if part]


def _chromium_switch_value(tokens: list[str], name: str) -> Optional[str]:
    """``--name=value`` or ``--name value``. Chromium accepts both.

    Equals is what the dock writes. A human or wrapper can still launch
    the same jar with the spaced form; recover / configured-listen then
    missed ``user-data-dir`` and leftover identity treated the jar as
    another Chrome. A following flag is not a value.
    """
    key = f"--{name}"
    prefix = key + "="
    for i, token in enumerate(tokens):
        raw = token if isinstance(token, str) else str(token)
        if raw.startswith(prefix):
            return raw.split("=", 1)[1] or None
        if raw == key and i + 1 < len(tokens):
            nxt = tokens[i + 1]
            nxt = nxt if isinstance(nxt, str) else str(nxt)
            if nxt.startswith("-"):
                return None
            return nxt or None
    return None


def _user_data_dir_from_cmdline(tokens: list[str]) -> Optional[str]:
    return _chromium_switch_value(tokens, "user-data-dir")


def _remote_debugging_port_from_cmdline(tokens: list[str]) -> Optional[int]:
    """Explicit ``--remote-debugging-port`` when the value is a real port.

    Dock argv uses ``--remote-debugging-port=0`` (ephemeral); that is not a port.
    """
    raw = _chromium_switch_value(tokens, "remote-debugging-port")
    if raw is None:
        return None
    try:
        port = int(raw)
    except ValueError:
        return None
    return port if 1 <= port <= 65535 else None


def _proc_hex_ip(ip_hex: str, *, ipv6: bool = False):
    """Decode a ``/proc/net/tcp{,6}`` local-address hex field (native dword order)."""
    text = (ip_hex or "").replace(":", "")
    if not ipv6:
        if len(text) != 8:
            raise ValueError(ip_hex)
        return ipaddress.IPv4Address(int.from_bytes(bytes.fromhex(text), "little"))
    if len(text) != 32:
        raise ValueError(ip_hex)
    packed = b"".join(
        int.from_bytes(bytes.fromhex(text[i:i + 8]), "little").to_bytes(4, "big")
        for i in range(0, 32, 8)
    )
    return ipaddress.IPv6Address(packed)


def _proc_socket_inodes(pid: int) -> Set[int]:
    """Inodes of sockets this *pid* currently holds (``/proc/<pid>/fd``).

    ``/proc/<pid>/net/tcp`` is the *network namespace* table, not this
    process's sockets. Unique-listen recover must intersect that table
    with these inodes or it sees every loopback LISTEN on the machine
    (VNC, other Chromes, the dashboard) and refuses — or, if the table
    happens to look unique, stamps a sibling Chrome's port as the dock.
    """
    found: Set[int] = set()
    fd_dir = f"/proc/{int(pid)}/fd"
    try:
        names = os.listdir(fd_dir)
    except OSError:
        return found
    for name in names:
        try:
            target = os.readlink(os.path.join(fd_dir, name))
        except OSError:
            continue
        if not (target.startswith("socket:[") and target.endswith("]")):
            continue
        raw = target[8:-1]
        if not raw.isdigit():
            continue
        inode = int(raw)
        if inode > 0:
            found.add(inode)
    return found


def _parse_proc_tcp_listen_targets(
    text: str,
    *,
    ipv6: bool = False,
    inodes: Optional[Set[int]] = None,
) -> Set[Tuple[str, int]]:
    """Loopback / unspecified LISTEN ``(ip, port)`` rows from ``/proc/net/tcp{,6}``.

    LAN / remote listeners stay out. No connect, no HTTP.

    When *inodes* is set, keep a LISTEN only if its inode (column 10)
    belongs to that set. *inodes=None* keeps the unfiltered parse for
    hex-layout unit tests that use truncated lines without an inode.
    """
    found: Set[Tuple[str, int]] = set()
    for line in (text or "").splitlines()[1:]:
        parts = line.split()
        if len(parts) < 4 or parts[3] != "0A":
            continue
        if inodes is not None:
            if len(parts) < 10:
                continue
            try:
                inode = int(parts[9])
            except ValueError:
                continue
            if inode not in inodes:
                continue
        local = parts[1]
        if ":" not in local:
            continue
        ip_hex, port_hex = local.rsplit(":", 1)
        try:
            port = int(port_hex, 16)
            addr = _proc_hex_ip(ip_hex, ipv6=ipv6)
        except (ValueError, ipaddress.AddressValueError):
            continue
        if not (1 <= port <= 65535):
            continue
        if addr.is_loopback or addr.is_unspecified:
            found.add((str(addr), port))
            continue
        mapped = getattr(addr, "ipv4_mapped", None)
        if mapped is not None and (mapped.is_loopback or mapped.is_unspecified):
            found.add((str(addr), port))
    return found


def _parse_proc_tcp_listen_ports(
    text: str,
    *,
    ipv6: bool = False,
    inodes: Optional[Set[int]] = None,
) -> Set[int]:
    """Loopback / unspecified LISTEN ports from one ``/proc/net/tcp{,6}`` table."""
    return {port for _ip, port in _parse_proc_tcp_listen_targets(text, ipv6=ipv6, inodes=inodes)}


def _loopback_listen_targets_for_pid(pid: int) -> Set[Tuple[str, int]]:
    """Loopback / unspecified TCP listens whose sockets *pid* holds.

    Empty inode set → empty result (fail closed). Falling back to the
    unfiltered netns table would re-open the sibling-Chrome stamp.
    """
    inodes = _proc_socket_inodes(pid)
    if not inodes:
        return set()
    found: Set[Tuple[str, int]] = set()
    for name, ipv6 in (("tcp", False), ("tcp6", True)):
        try:
            text = Path(f"/proc/{pid}/net/{name}").read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        found |= _parse_proc_tcp_listen_targets(text, ipv6=ipv6, inodes=inodes)
    return found


def _loopback_listen_ports_for_pid(pid: int) -> Set[int]:
    """Loopback / unspecified TCP listen ports whose sockets *pid* holds."""
    return {port for _ip, port in _loopback_listen_targets_for_pid(pid)}


def _connect_hosts_for_listen_ip(ip: str) -> Tuple[str, ...]:
    """Hosts to TCP-probe for a ``/proc`` listen address. No other family.

    A ::1-only dock plus a sibling on ``127.0.0.1:same`` must not stamp
    the squat. Unspecified ``0.0.0.0`` / ``::`` map to that family's
    loopback; IPv4-mapped ``::ffff:127.0.0.1`` maps to ``127.0.0.1``.
    """
    try:
        addr = ipaddress.ip_address(ip)
    except ValueError:
        return (ip,) if ip else ()
    mapped = getattr(addr, "ipv4_mapped", None)
    if mapped is not None:
        if mapped.is_unspecified:
            return ("127.0.0.1",)
        return (str(mapped),)
    if addr.is_unspecified:
        return ("127.0.0.1",) if addr.version == 4 else ("::1",)
    return (str(addr),)


def _listen_connect_hosts(pid: int, port: int) -> Tuple[str, ...]:
    """Connect hosts for *port* from this pid's listens, else IPv4 then IPv6.

    Unknown address (DevTools file, mocked ports, inode miss) may try both
    families. A known ::1-only listen must not probe ``127.0.0.1``.
    """
    hosts: list[str] = []
    try:
        targets = _loopback_listen_targets_for_pid(pid)
    except Exception:
        targets = set()
    for ip, listen_port in targets:
        if listen_port != port:
            continue
        for host in _connect_hosts_for_listen_ip(ip):
            if host not in hosts:
                hosts.append(host)
    if hosts:
        return tuple(hosts)
    return ("127.0.0.1", "::1")


def _cdp_port_reachable(port: int, hosts: Tuple[str, ...]) -> bool:
    """True when *port* accepts TCP on one of *hosts*. No HTTP."""
    for host in hosts:
        try:
            with socket.create_connection((host, port), timeout=0.5):
                return True
        except OSError:
            continue
    return False


def _paths_same_user_data_dir(left: str, right: str) -> bool:
    try:
        return Path(left).resolve() == Path(right).resolve()
    except OSError:
        return os.path.normpath(left) == os.path.normpath(right)


def _ip_is_unspecified_listen(ip: str) -> bool:
    try:
        addr = ipaddress.ip_address(ip)
    except ValueError:
        return False
    mapped = getattr(addr, "ipv4_mapped", None)
    if mapped is not None:
        return bool(mapped.is_unspecified)
    return bool(addr.is_unspecified)


def _unique_recoverable_listen_port(targets: Set[Tuple[str, int]]) -> Optional[int]:
    """Unique listen, or the one specific loopback when extras are unspecified.

    Chromium often keeps DevTools on ``127.0.0.1`` / ``::1`` plus a
    ``0.0.0.0`` / ``::`` sibling. Treating that pair as "multiple" misses
    leftover identity. Several *specific* loopbacks stay unknown — do not
    guess. No HTTP.
    """
    ports = {port for _ip, port in targets}
    if len(ports) == 1:
        return next(iter(ports))
    specific = {
        port for ip, port in targets
        if not _ip_is_unspecified_listen(ip)
    }
    if len(specific) == 1:
        return next(iter(specific))
    return None


def _recover_cdp_port_from_singleton(user_data_dir: str, pid: int) -> Optional[int]:
    """Port of the still-alive jar when ``DevToolsActivePort`` is gone.

    Persist sites can miss a human-first dock. ``SingletonLock`` still names
    this profile's Chromium. Recover only that pid's explicit DevTools port,
    or a *unique* loopback listen. Multiple specific loopbacks stay unknown
    — do not stamp an arbitrary port. An unspecified extra
    (``0.0.0.0`` / ``::``) next to one specific loopback is not ambiguity.
    Cmdline must name this ``user_data_dir`` so a recycled pid is not
    trusted. No HTTP (tab list is leftover observation).
    """
    tokens = _chromium_cmdline_tokens(pid)
    listed = _user_data_dir_from_cmdline(tokens)
    if not listed or not _paths_same_user_data_dir(listed, user_data_dir):
        return None
    explicit = _remote_debugging_port_from_cmdline(tokens)
    if explicit is not None:
        return explicit
    listens = _loopback_listen_ports_for_pid(pid)
    if len(listens) == 1:
        return next(iter(listens))
    targets = {
        (ip, port)
        for ip, port in _loopback_listen_targets_for_pid(pid)
        if port in listens
    }
    # Ports/targets must agree. A ports-only mock with extra members must
    # not be refined by this process's real sockets (would stamp a test
    # listener as "unique loopback").
    if {port for _ip, port in targets} != listens:
        return None
    return _unique_recoverable_listen_port(targets)


def running_instance_cdp_port(user_data_dir: str, *, exclude_session: Optional[str] = None) -> Optional[int]:
    """DevTools port of a Chromium currently running on ``user_data_dir``, or ``None``.

    Both files outlive a crashed or closed Chromium: ``SingletonLock`` is a symlink to ``host-pid`` and
    ``DevToolsActivePort`` keeps the last port, so the pid must be alive AND the port must accept a
    connection before it is trusted. When the port file is gone but the lock pid is still this
    profile's Chromium, recover the port from that pid (explicit
    ``--remote-debugging-port=N``, or a unique loopback listen). An instance
    agent-browser launched for ``exclude_session`` itself is reported as ``None``:
    its daemon already owns that browser, and handing it ``--cdp`` would make it
    close the browser as a config change and then attach to the port that just died with it.
    """
    try:
        target = os.readlink(os.path.join(user_data_dir, "SingletonLock"))
    except OSError:
        return None
    _host, _, pid_text = target.rpartition("-")
    if not pid_text.isdigit() or not _pid_alive(int(pid_text)):
        return None
    pid = int(pid_text)
    if exclude_session and _launched_by_session(pid) == exclude_session:
        return None
    port_line = ""
    try:
        with open(os.path.join(user_data_dir, "DevToolsActivePort"), encoding="utf-8") as fh:
            port_line = fh.readline().strip()
    except OSError:
        port_line = ""
    if port_line.isdigit():
        port = int(port_line)
    else:
        recovered = _recover_cdp_port_from_singleton(user_data_dir, pid)
        if recovered is None:
            return None
        port = recovered
    # Port file has no address. Probe this pid's listen family so a
    # ::1-only dock is not stamped as a sibling on 127.0.0.1:same
    # (finding 81's recover path; the file path used to IPv4-first).
    hosts = _listen_connect_hosts(pid, port)
    if not _cdp_port_reachable(port, hosts):
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


def _lock_pid(user_data_dir: str) -> Optional[int]:
    """Alive SingletonLock pid for ``user_data_dir``, or ``None``."""
    try:
        target = os.readlink(os.path.join(user_data_dir, "SingletonLock"))
    except OSError:
        return None
    _host, _, pid_text = target.rpartition("-")
    if not pid_text.isdigit():
        return None
    pid = int(pid_text)
    return pid if pid > 1 and _pid_alive(pid) else None


def _configured_cdp_override_url() -> str:
    """``/browser connect`` / ``BROWSER_CDP_URL`` / ``browser.cdp_url``, or empty."""
    try:
        from tools.browser_tool_cdp import _get_cdp_override_raw
        return (_get_cdp_override_raw() or "").strip()
    except Exception:
        return ""


def _configured_listen_port_for_this_jar() -> Optional[int]:
    """Override port when this profile's Chromium listens on it.

    Unique-listen recover stays unknown when Chromium has several *specific*
    loopbacks. The operator override still names the DevTools port. Stamp
    it only if SingletonLock's pid holds that listen and cmdline names
    this ``user-data-dir`` — a config pointing at another Chrome on 9222
    must not become the dock. No HTTP.
    """
    raw = _configured_cdp_override_url()
    if not raw:
        return None
    try:
        from tools.browser_tool_session import _loopback_cdp_port
        want = _loopback_cdp_port(raw)
    except Exception:
        return None
    if want is None:
        return None
    user_data_dir = str(profile_dir())
    pid = _lock_pid(user_data_dir)
    if pid is None:
        return None
    listed = _user_data_dir_from_cmdline(_chromium_cmdline_tokens(pid))
    if not listed or not _paths_same_user_data_dir(listed, user_data_dir):
        return None
    if want not in _loopback_listen_ports_for_pid(pid):
        return None
    if not _cdp_port_reachable(want, _listen_connect_hosts(pid, want)):
        return None
    return want


def persist_live_dock_cdp_port() -> Optional[int]:
    """Best-effort stamp of the live dock DevTools port for this profile.

    Human-first Take over can happen before any agent browser call has
    seen ``DevToolsActivePort``. Persist now, while the probe still
    works, so a later miss cannot treat this jar as another Chrome.

    When unique-listen recover is ambiguous, a loopback override that
    this jar actually listens on is still this profile's DevTools port.
    """
    try:
        port = running_instance_cdp_port(str(profile_dir()))
    except Exception:
        port = None
    if port is None:
        try:
            port = _configured_listen_port_for_this_jar()
        except Exception:
            port = None
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
    if last_known_dock_cdp_port() == port:
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
