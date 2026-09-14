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
from typing import Dict, Optional, Set, Tuple

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
    """``--name=value`` / ``--name value`` and the single-dash forms.

    Equals is what the dock writes. A human or wrapper can still launch
    the same jar with the spaced form or ``-user-data-dir``; recover /
    configured-listen then missed the jar (admit ``None`` → leftover
    HTTP). A following flag is not a value.

    Chromium's CommandLine keeps the *last* value when a switch repeats
    and stops at ``--``. First-wins / double-dash-only / scanning past
    the terminator treated a live dock as another Chrome.
    """
    keys = (f"--{name}", f"-{name}")
    found: Optional[str] = None
    i = 0
    n = len(tokens)
    while i < n:
        raw = tokens[i] if isinstance(tokens[i], str) else str(tokens[i])
        if raw == "--":
            break
        for key in keys:
            prefix = key + "="
            if raw.startswith(prefix):
                found = raw.split("=", 1)[1] or None
                break
            if raw != key:
                continue
            if i + 1 < n:
                nxt = tokens[i + 1]
                nxt = nxt if isinstance(nxt, str) else str(nxt)
                if nxt.startswith("-"):
                    found = None
                else:
                    found = nxt or None
                    i += 1
            else:
                found = None
            break
        i += 1
    return found


def _user_data_dir_from_cmdline(tokens: list[str]) -> Optional[str]:
    return _chromium_switch_value(tokens, "user-data-dir")


def _proc_env_value(pid: int, name: str) -> Optional[str]:
    """One ``KEY=value`` from ``/proc/<pid>/environ``, or ``None``."""
    try:
        raw = Path(f"/proc/{pid}/environ").read_bytes()
    except OSError:
        return None
    prefix = f"{name}=".encode()
    for part in raw.split(b"\0"):
        if part.startswith(prefix):
            text = part[len(prefix):].decode("utf-8", "replace").strip()
            return text or None
    return None


def _listed_user_data_dir(pid: int, tokens: Optional[list[str]] = None) -> Optional[str]:
    """Jar this Chromium named: cmdline flag, else ``CHROME_USER_DATA_DIR``.

    Chromium documents the env override when ``--user-data-dir`` is absent.
    Recover / configured-listen that only read argv then treated a live
    dock launched that way as another Chrome (admit ``None`` → leftover
    HTTP). The flag still wins when both are set. Do not guess the
    default ``~/.config/chromium`` jar.
    """
    if tokens is None:
        tokens = _chromium_cmdline_tokens(pid)
    listed = _user_data_dir_from_cmdline(tokens)
    if listed:
        return listed
    return _proc_env_value(pid, "CHROME_USER_DATA_DIR")


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


def _loopback_listen_inodes_for_port(port: int) -> Dict[int, Set[str]]:
    """inode → listen IPs for loopback / unspecified LISTEN on *port*.

    Reads this netns ``/proc/net/tcp{,6}``. Used when SingletonLock is
    gone and leftover already named *port* (finding 161). Does not
    consult ``_loopback_listen_ports_for_pid`` — a ``lambda pid: {port}``
    mock must not make every leftover writer look like this jar.
    """
    if not isinstance(port, int) or not (1 <= port <= 65535):
        return {}
    found: Dict[int, Set[str]] = {}
    for name, ipv6 in (("tcp", False), ("tcp6", True)):
        try:
            text = Path(f"/proc/net/{name}").read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        for line in text.splitlines()[1:]:
            parts = line.split()
            if len(parts) < 10 or parts[3] != "0A":
                continue
            local = parts[1]
            if ":" not in local:
                continue
            ip_hex, port_hex = local.rsplit(":", 1)
            try:
                listen_port = int(port_hex, 16)
                addr = _proc_hex_ip(ip_hex, ipv6=ipv6)
                inode = int(parts[9])
            except (ValueError, ipaddress.AddressValueError):
                continue
            if listen_port != port or inode <= 0:
                continue
            ip = None
            if addr.is_loopback or addr.is_unspecified:
                ip = str(addr)
            else:
                mapped = getattr(addr, "ipv4_mapped", None)
                if mapped is not None and (mapped.is_loopback or mapped.is_unspecified):
                    ip = str(addr)
            if ip is None:
                continue
            found.setdefault(inode, set()).add(ip)
    return found


def _pids_holding_socket_inodes(want: Set[int]) -> Dict[int, Set[int]]:
    """pid → subset of *want* inodes that pid currently holds."""
    if not want:
        return {}
    held: Dict[int, Set[int]] = {}
    try:
        names = os.listdir("/proc")
    except OSError:
        return held
    for name in names:
        if not name.isdigit():
            continue
        pid = int(name)
        if pid <= 1:
            continue
        have = _proc_socket_inodes(pid) & want
        if have:
            held[pid] = have
    return held


def _this_jar_listen_holders(
    want: int, user_data_dir: str,
) -> list[Tuple[int, Tuple[str, ...]]]:
    """This-jar pids that inode-listen on *want*, with their connect hosts.

    Finding 161 used a unique holder so persist / skip-kill do not pick
    a random pid. Finding 163: leftover already named *want*. A forked
    helper that inherited the listen fd and still names
    ``--user-data-dir`` made uniqueness fail, so leftover ``--cdp`` to
    the live dock looked like another Chrome. Union hosts for leftover
    identity. A sibling that does not name this jar stays out. No HTTP.
    Does not consult ``_loopback_listen_ports_for_pid``.
    """
    inode_ips = _loopback_listen_inodes_for_port(want)
    if not inode_ips:
        return []
    holders: list[Tuple[int, Tuple[str, ...]]] = []
    for pid, inodes in _pids_holding_socket_inodes(set(inode_ips)).items():
        if not _pid_names_this_jar(pid, user_data_dir):
            continue
        ips: Set[str] = set()
        for inode in inodes:
            ips |= inode_ips.get(inode, set())
        hosts: list[str] = []
        for ip in ips:
            for host in _connect_hosts_for_listen_ip(ip):
                if host not in hosts:
                    hosts.append(host)
        if hosts:
            holders.append((pid, tuple(hosts)))
    return holders


def _union_listen_hosts(
    holders: list[Tuple[int, Tuple[str, ...]]],
) -> Tuple[str, ...]:
    hosts: list[str] = []
    for _pid, hs in holders:
        for host in hs:
            if host not in hosts:
                hosts.append(host)
    return tuple(hosts)


def _scan_this_jar_listen_holder(
    want: int, user_data_dir: str,
) -> Optional[Tuple[int, Tuple[str, ...]]]:
    """Unique this-jar pid that inode-listens on *want*, or ``None``.

    Finding 161: Take over / overlay copies can unlink SingletonLock
    while Chromium still holds DevTools. Leftover already named *want*
    — that is not a guess among ports. Skip-kill / ``_this_jar_chromium_pid``
    still need one pid. Several this-jar holders stay unknown here;
    leftover identity unions their hosts (finding 163). Persist of a
    file-named port does not need a unique pid (finding 164). A sibling
    on the same number is not this jar. No HTTP.
    """
    holders = _this_jar_listen_holders(want, user_data_dir)
    return holders[0] if len(holders) == 1 else None


def _file_named_this_jar_listen_port(
    candidate: int,
    user_data_dir: str,
    *,
    exclude_session: Optional[str] = None,
) -> Optional[int]:
    """``DevToolsActivePort`` *candidate* when this jar still inode-listens.

    Finding 161 required a unique holder so persist / skip-kill did not
    pick a random pid. Finding 163 unions leftover identity hosts when
    several this-jar pids inherit the listen. Persist returns a *port*,
    not a pid — the file already named *candidate*. Several holders on
    that number are not a guess among ports (finding 164). A lock pid
    that no longer holds the listen must not hide those helpers.
    ``exclude_session`` stays None when any holder is that session's
    own browser. Callers must not use this when the lock pid still
    lists *candidate* and only its family failed (finding 165).
    A sibling cmdline is not this jar. No HTTP.
    """
    if not isinstance(candidate, int) or not (1 <= candidate <= 65535):
        return None
    holders = _this_jar_listen_holders(candidate, user_data_dir)
    hosts = _union_listen_hosts(holders)
    if not holders or not hosts:
        return None
    if exclude_session and any(
        _launched_by_session(pid) == exclude_session for pid, _ in holders
    ):
        return None
    if not _cdp_port_reachable(candidate, hosts):
        return None
    return candidate


def file_named_dock_listen_port(user_data_dir: Optional[str] = None) -> Optional[int]:
    """``DevToolsActivePort`` when this jar still inode-listens, or ``None``.

    Persist / ``running_instance_cdp_port`` require a fresh TCP accept
    (finding 165 must not stamp another family's holder). Leftover
    holding the CDP socket is that miss — leftover identity already
    trusts the file-named listen (finding 166). Agent attach did not:
    persist may never have been stamped, so finding 168 launched
    ``--session`` into the jar a human holds (finding 170). The file
    already named the listen. Lock pid still listing that number uses
    only that pid's family (finding 165). Do not stamp persist. No HTTP.
    """
    if user_data_dir is None:
        user_data_dir = str(profile_dir())
    try:
        with open(os.path.join(user_data_dir, "DevToolsActivePort"), encoding="utf-8") as fh:
            port_line = fh.readline().strip()
    except OSError:
        return None
    if not port_line.isdigit():
        return None
    port = int(port_line)
    if not (1 <= port <= 65535):
        return None
    try:
        hosts = _this_jar_listen_connect_hosts(port)
    except Exception:
        hosts = ()
    return port if hosts else None


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
    """Connect hosts for *port* from this pid's listens. Empty if none.

    A stale ``DevToolsActivePort`` / explicit argv port plus a sibling
    Chrome on that number used to fall back to ``127.0.0.1`` / ``::1``
    and stamp the squat (finding 144). A known ::1-only listen must not
    probe ``127.0.0.1``.
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
    return tuple(hosts)


def _cdp_port_reachable(port: int, hosts: Tuple[str, ...]) -> bool:
    """True when *port* accepts TCP on one of *hosts*. No HTTP."""
    for host in hosts:
        try:
            with socket.create_connection((host, port), timeout=0.5):
                return True
        except OSError:
            continue
    return False


def _proc_cwd(pid: int) -> Optional[Path]:
    """Chromium's cwd (``/proc/<pid>/cwd``), or ``None``."""
    try:
        return Path(os.readlink(f"/proc/{pid}/cwd"))
    except OSError:
        return None


def _proc_ppid(pid: int) -> Optional[int]:
    """Parent pid of *pid* from ``/proc``, or ``None``.

    Finding 186: leftover this-jar helpers that dropped chrome's
    DevTools fd still have chrome as parent. That pid is not a
    scan of every this-jar process. pid 1 is not chrome.
    """
    try:
        with open(f"/proc/{pid}/status", encoding="utf-8") as fh:
            ppid = next(
                (int(line.split()[1]) for line in fh if line.startswith("PPid:")),
                0,
            )
    except (OSError, ValueError):
        return None
    return ppid if ppid > 1 else None


def _resolve_user_data_dir(path: str, *, cwd: Optional[Path] = None) -> Path:
    """Resolve a cmdline ``--user-data-dir`` against *cwd* when relative.

    Chromium stores the argv literally. A wrapper that ``cd``s into
    ``~/.hermes/bot-desktop`` and launches ``--user-data-dir=browser-profile``
    is still this jar. ``Path.resolve()`` without *cwd* uses *this*
    process's cwd and then recover / configured-listen treat the dock as
    another Chrome — leftover identity HTTP-probes the jar a human holds.
    """
    raw = Path(path)
    if not raw.is_absolute() and cwd is not None:
        raw = cwd / raw
    return raw.resolve()


def _paths_same_user_data_dir(
    left: str, right: str, *, cwd: Optional[Path] = None,
) -> bool:
    try:
        return _resolve_user_data_dir(left, cwd=cwd) == Path(right).resolve()
    except OSError:
        return os.path.normpath(left) == os.path.normpath(right)


def _pid_names_this_jar(
    pid: int, user_data_dir: str, tokens: Optional[list[str]] = None,
) -> bool:
    """True when ``pid``'s cmdline / ``CHROME_USER_DATA_DIR`` is this jar."""
    listed = _listed_user_data_dir(pid, tokens)
    if not listed:
        return False
    return _paths_same_user_data_dir(
        listed, user_data_dir, cwd=_proc_cwd(pid),
    )


def _lock_pid_text(user_data_dir: str) -> Optional[str]:
    """Chromium ``host-pid`` from SingletonLock — symlink or materialized file.

    Official Chromium on Linux writes a symlink. ``cp -L``, some
    rsync/backup tools, and overlay copies materialize the target as a
    regular file. ``readlink`` then fails and persist / leftover
    identity treated a live dock as another Chrome (finding 160).
    Do not follow a symlink with ``open`` (dangling / other-file).
    Callers still require the pid alive and to name this jar.
    """
    path = os.path.join(user_data_dir, "SingletonLock")
    try:
        return os.readlink(path)
    except OSError:
        pass
    try:
        if os.path.islink(path) or not os.path.isfile(path):
            return None
        with open(path, encoding="utf-8") as fh:
            return fh.readline(256).strip() or None
    except OSError:
        return None


def _lock_pid(user_data_dir: str) -> Optional[int]:
    """Alive SingletonLock pid for ``user_data_dir``, or ``None``."""
    target = _lock_pid_text(user_data_dir)
    if not target:
        return None
    _host, _, pid_text = target.rpartition("-")
    if not pid_text.isdigit():
        return None
    pid = int(pid_text)
    return pid if pid > 1 and _pid_alive(pid) else None


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
    this profile's Chromium. Recover only that pid's explicit DevTools port
    when this pid still listens on it, or a *unique* loopback listen.
    Multiple specific loopbacks stay unknown — do not stamp an arbitrary
    port. An unspecified extra (``0.0.0.0`` / ``::``) next to one specific
    loopback is not ambiguity. A stale explicit port that a sibling Chrome
    now occupies is not the dock.
    Cmdline ``--user-data-dir`` or ``CHROME_USER_DATA_DIR`` must name this
    ``user_data_dir`` so a recycled pid is not trusted. No HTTP (tab list
    is leftover observation).
    """
    tokens = _chromium_cmdline_tokens(pid)
    if not _pid_names_this_jar(pid, user_data_dir, tokens):
        return None
    explicit = _remote_debugging_port_from_cmdline(tokens)
    listens = _loopback_listen_ports_for_pid(pid)
    # Argv can name a port this pid no longer holds. A sibling Chrome
    # that occupied it must not become the dock (finding 144). Fall
    # through to this pid's unique listen when the explicit port is stale.
    if explicit is not None and explicit in listens:
        return explicit
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


def lock_listed_persist_port(user_data_dir: Optional[str] = None) -> Optional[int]:
    """Stamped persist when this jar still lists it, or ``None``.

    Finding 174: unique-listen recover is unknown when Chromium has
    several specific loopbacks (finding 85). Persist already named a
    listen this pid still holds — that is not a guess. Finding 175:
    leftover identity must not stamp a different this-jar listen
    (stale file helpers) over that persist after a persist TCP miss.
    Finding 176: persist_live's configured fallback must not stamp
    those helpers either. Finding 177: persist file then in-process
    memory, same order as ``_remembered_dock_port_candidates``. A
    remember miss or unlinked ``dock-cdp-port`` used to make 164 /
    configured treat lock-listed chrome as "persist not on the lock"
    and stamp leftover file helpers. First candidate still on this
    pid wins.     File still on the lock is finding 172. Finding 184:
    leftover persist / ``DevToolsActivePort`` helpers (several
    holders) must not hide the unique lock listen that is
    chrome when persist was never stamped. A recycled lock
    pid is not this jar. Finding 179: Take over can unlink
    ``SingletonLock`` (finding 161) while chrome still inode-listens.
    177's lock-pid check then missed in-process memory, so 164
    shopped leftover file helpers and attach followed them. Memory
    that this jar still inode-listens on is persist.     File-only leftover helpers matching DevTools stay None so
    86 / 164 can still recover them. Finding 181: a process
    restart clears memory. Persist file that this jar still
    inode-listens on is the last live stamp; leftover
    ``DevToolsActivePort`` helpers (several holders) must not
    overwrite that chrome (unique holder). A different
    file-named live listen with leftover persist holders is
    still finding 172. Finding 193: leftover identity can
    stamp a *different* unique leftover listen (CRI / ``--cdp``)
    into that persist file. Unique leftover persist + leftover
    DevTools several then matched 181 and hid sibling chrome.
    Hidden chrome that differs from leftover persist is chrome.
    Finding 195: leftover daemon that inherited leftover
    CRI / ``--cdp`` is a unique this-jar listen. That
    leftover persist is not hidden chrome — sibling
    chrome is still a this-jar child of leftover daemon.
    Finding 196: leftover daemon and chrome both inherited
    leftover CRI so leftover persist and chrome CDP both
    look leftover-shared. Chrome's own listen is the one
    leftover daemon does not inode-hold.
    Finding 197: leftover fill clients exited and chrome
    dropped the inherited DevTools listen, so leftover
    daemon uniquely holds stale ``DevToolsActivePort``.
    Unique leftover file is not chrome when that holder
    is leftover daemon — sibling chrome is still a
    this-jar child of leftover daemon.
    Finding 198: one leftover fill / CRI can uniquely
    hold that stale listen after leftover daemon dropped
    the fd. Chrome is a sibling under leftover daemon.
    Finding 199: leftover fill can also inherit leftover
    persist with chrome. Leftover daemon holds neither
    leftover persist nor chrome's own CDP. Chrome's own
    listen is the inherited listen leftover file holders
    do not inode-hold. Finding 200: leftover python / CRI
    that inherited chrome's CDP but is not a leftover
    file holder hid that chrome listen.     Finding 201:
    leftover python / CRI inherited leftover persist
    with chrome. Leftover file holders hold neither
    leftover persist nor chrome's own CDP. Chrome's
    own listen is the leftover-file-miss leftover
    siblings do not inode-hold.
    Finding 202: leftover python / CRI spawned by
    leftover fill inherited leftover persist.
    leftover-sibling-miss only walked leftover
    daemon's children. One hop of leftover siblings'
    this-jar children is leftover fill's leftover
    child. Orphan leftover python stays 86.
    Finding 185: persist never stamped and
    the lock is already gone. Leftover holders that inherited
    chrome's DevTools fd still name chrome's other unique
    listen — that is the first persist, not those helpers.
    Finding 186: leftover this-jar helpers that dropped that
    fd still have chrome as parent. That parent's other
    unique listen is the first persist. Finding 188: leftover
    helpers that inherited chrome's unique CDP listen are
    this-jar inode holders of that port (finding 163). 184 /
    185 / 186 required n==1, so first persist stamped leftover
    DevTools. Exactly one leftover-shared listen with chrome
    is still chrome.
    No HTTP.
    """
    if user_data_dir is None:
        user_data_dir = str(profile_dir())
    candidates: list[int] = []
    try:
        persist = last_known_dock_cdp_port()
    except Exception:
        persist = None
    if isinstance(persist, int) and 1 <= persist <= 65535:
        candidates.append(persist)
    try:
        from hermes_constants import hermes_home_key
        from tools.browser_tool_session import _last_dock_cdp_port
        memory = _last_dock_cdp_port.get(hermes_home_key())
    except Exception:
        memory = None
    if isinstance(memory, int) and 1 <= memory <= 65535 and memory not in candidates:
        candidates.append(memory)
    pid = _lock_pid(user_data_dir)
    if pid is not None and _pid_names_this_jar(pid, user_data_dir):
        try:
            lock_ports = _loopback_listen_ports_for_pid(pid)
        except Exception:
            return None
        named = None
        try:
            named = file_named_dock_listen_port(user_data_dir)
        except Exception:
            named = None
        if named is None:
            named = persist
        hidden = unique_lock_chrome_hidden_by_leftover_file(
            named, user_data_dir,
        )
        if hidden is not None and hidden in lock_ports:
            return hidden
        for port in candidates:
            if port in lock_ports:
                return port
        return None

    def _hidden_leftover_holder_chrome() -> Optional[int]:
        named = None
        try:
            named = file_named_dock_listen_port(user_data_dir)
        except Exception:
            named = None
        return unique_lock_chrome_hidden_by_leftover_file(
            named, user_data_dir,
        )

    # Finding 185: persist never stamped (empty candidates). Overlay
    # / Take over can unlink SingletonLock before the first live
    # stamp. 184 needs that pid; leftover holders that inherited
    # chrome's DevTools fd still name chrome's other unique listen.
    # Finding 186: leftover helpers that dropped that fd still
    # have chrome as parent.
    if not candidates:
        hidden = _hidden_leftover_holder_chrome()
        if hidden is not None:
            return hidden
        return None
    # Finding 179: no this-jar lock pid. Do not file-first inode-list
    # leftover helpers — that reopens 178. Memory that this jar still
    # inode-listens on is chrome. No TCP (finding 166). Finding 192:
    # leftover memory (pre-191 stamp / identity) still has leftover
    # helper hosts. That must not hide leftover-holder sibling chrome.
    if isinstance(memory, int) and 1 <= memory <= 65535:
        try:
            hosts = _this_jar_listen_connect_hosts(memory)
        except Exception:
            hosts = ()
        if hosts:
            hidden = _hidden_leftover_holder_chrome()
            if hidden is not None and hidden != memory:
                return hidden
            return memory
    # Finding 181: process restart cleared memory. Persist file that
    # this jar still inode-listens on is the last live stamp. File
    # equal to DevTools stays None so 86 / 164 can recover leftover
    # helpers. Persist unique + DevTools several is leftover helpers
    # hiding chrome. Persist several + DevTools unique is finding 172.
    # Finding 193: leftover persist unique is not chrome when leftover
    # holders hide a different sibling chrome listen.
    if isinstance(persist, int) and 1 <= persist <= 65535:
        try:
            hosts = _this_jar_listen_connect_hosts(persist)
        except Exception:
            hosts = ()
        if hosts:
            hidden = _hidden_leftover_holder_chrome()
            if hidden is not None and hidden != persist:
                return hidden
            named = None
            try:
                with open(
                    os.path.join(user_data_dir, "DevToolsActivePort"),
                    encoding="utf-8",
                ) as fh:
                    port_line = fh.readline().strip()
                if port_line.isdigit():
                    named = int(port_line)
            except OSError:
                named = None
            if leftover_helpers_hide_persist_chrome(
                persist, named, user_data_dir,
            ):
                return persist
    # Finding 185: poisoned leftover persist file (candidates
    # non-empty) must not hide leftover-holder unique chrome.
    hidden = _hidden_leftover_holder_chrome()
    if hidden is not None:
        return hidden
    return None


def leftover_helpers_hide_persist_chrome(
    persist: Optional[int],
    named: Optional[int],
    user_data_dir: Optional[str] = None,
) -> bool:
    """True when persist is unique chrome and *named* is leftover helpers.

    Finding 181 / 183: leftover ``DevToolsActivePort`` helpers (several
    this-jar holders) must not hide persist chrome (unique holder).
    Finding 172 chrome-switch is persist several + named unique.
    Unique+unique is not a guess among ports. No HTTP.
    """
    if user_data_dir is None:
        user_data_dir = str(profile_dir())
    if not isinstance(persist, int) or not (1 <= persist <= 65535):
        return False
    if not isinstance(named, int) or not (1 <= named <= 65535) or named == persist:
        return False
    try:
        persist_n = len(_this_jar_listen_holders(persist, user_data_dir))
        named_n = len(_this_jar_listen_holders(named, user_data_dir))
    except Exception:
        return False
    return persist_n == 1 and named_n > 1


def _this_jar_holder_pids(want: int, user_data_dir: str) -> Set[int]:
    """This-jar pids that inode-listen on *want*."""
    try:
        return {
            holder_pid
            for holder_pid, _hosts in _this_jar_listen_holders(want, user_data_dir)
        }
    except Exception:
        return set()


def _unique_this_jar_parent(
    holder_pids: Set[int], user_data_dir: str,
) -> Optional[int]:
    """Unique this-jar PPID of leftover holders, or ``None``.

    Finding 186 / 188: leftover helpers that name this jar still have
    chrome as parent. Several distinct this-jar parents stay unknown.
    """
    parents: list[int] = []
    for holder_pid in holder_pids:
        try:
            ppid = _proc_ppid(holder_pid)
        except Exception:
            ppid = None
        if (
            isinstance(ppid, int)
            and ppid > 1
            and ppid not in parents
            and _pid_names_this_jar(ppid, user_data_dir)
        ):
            parents.append(ppid)
    return parents[0] if len(parents) == 1 else None


def _this_jar_descendant_of_chrome(
    holder: int, chrome_pid: int, *, hops: int = 4,
) -> bool:
    """True when *holder*'s PPID chain reaches chrome within *hops*.

    Finding 190: Chromium zygote forks GPU / utility / renderer
    children that inherit chrome's CDP listen. Those pids name this
    jar but their parent is zygote, not chrome. Walking PPID of
    holders already on that listen is not a scan of every ``/proc``
    this-jar pid and not a leftover-holder grandparent walk.
    """
    if not isinstance(holder, int) or holder <= 1:
        return False
    if holder == chrome_pid:
        return True
    seen: Set[int] = set()
    pid = holder
    for _ in range(hops):
        if pid in seen or pid <= 1:
            return False
        seen.add(pid)
        try:
            ppid = _proc_ppid(pid)
        except Exception:
            return False
        if not isinstance(ppid, int) or ppid <= 1:
            return False
        if ppid == chrome_pid:
            return True
        pid = ppid
    return False


_CHROMIUM_BROWSER_EXES = frozenset({
    "chrome",
    "chromium",
    "chromium-browser",
    "google-chrome",
    "google-chrome-stable",
    "google-chrome-beta",
    "google-chrome-unstable",
    "ungoogled-chromium",
})


def _pid_is_chromium_browser(pid: int) -> bool:
    """True when *pid* is the browser process, not zygote / GPU / leftover.

    Chromium children use ``--type=zygote`` / ``gpu-process`` /
    ``utility`` / ``renderer``. The browser process omits ``--type=``
    or sets ``--type=browser``. Finding 191 must not pick zygote as
    chrome after chrome dropped the listen fd, and must not treat a
    leftover daemon that names this jar (``agent-browser``, python)
    as chrome just because it has no ``--type=``.
    """
    try:
        tokens = _chromium_cmdline_tokens(pid)
    except Exception:
        return False
    if not tokens:
        return False
    kinds = [
        token.split("=", 1)[1]
        for token in tokens
        if token.startswith("--type=") and "=" in token
    ]
    if kinds and "browser" not in kinds:
        return False
    if "browser" in kinds:
        return True
    exe = os.path.basename(tokens[0]).lower()
    return exe in _CHROMIUM_BROWSER_EXES


def _unique_chromium_browser_holder(
    port: int, user_data_dir: str,
) -> Optional[int]:
    """Unique this-jar holder of *port* when that pid is chrome, or ``None``.

    Finding 195: leftover daemon that inherited leftover CRI /
    ``--cdp`` is a unique this-jar listen. The lock-gone n==1
    scan used to treat that leftover persist as hidden chrome
    and never reach sibling chrome via leftover daemon's
    children. Leftover CRI / ``agent-browser`` / python are
    not chrome.
    """
    holders = _this_jar_holder_pids(port, user_data_dir)
    if len(holders) != 1:
        return None
    holder = next(iter(holders))
    return holder if _pid_is_chromium_browser(holder) else None


def _unique_listen_family_root(extra: Set[int]) -> Optional[int]:
    """Unique browser-process ancestor of *extra* listen holders, or ``None``.

    Finding 191: leftover helpers' this-jar parent is leftover
    daemon; chrome is their sibling. Extra holders of a leftover-
    advertised listen (holders minus leftover file holders) have
    one browser-process root. Several roots stay unknown. A zygote
    root (chrome dropped the inode) is not chrome.
    Finding 200: leftover python / CRI / fill that inherited
    chrome's CDP but is not a ``DevToolsActivePort`` file
    holder is extra — not leftover file holders and not a
    chrome descendant. That leftover sibling is not a
    competing browser root. Non-browser extras stay leftover.
    Several browser roots stay unknown. Leftover-only extras
    stay unknown (86).
    """
    browsers = {pid for pid in extra if _pid_is_chromium_browser(pid)}
    if not browsers:
        return None
    roots: list[int] = []
    for pid in browsers:
        if any(
            other != pid and _this_jar_descendant_of_chrome(pid, other)
            for other in browsers
        ):
            continue
        roots.append(pid)
    return roots[0] if len(roots) == 1 else None


def _this_jar_children(parent: int, user_data_dir: str) -> Set[int]:
    """This-jar pids whose PPID is *parent*.

    Finding 191: leftover connect-only helpers do not advertise
    chrome's listen. Chrome is a sibling of those helpers under
    leftover daemon. Children of that one parent are not a scan
    of every this-jar pid.
    """
    kids: Set[int] = set()
    if not isinstance(parent, int) or parent <= 1:
        return kids
    try:
        names = os.listdir("/proc")
    except OSError:
        return kids
    for name in names:
        if not name.isdigit():
            continue
        pid = int(name)
        if pid <= 1 or pid == parent:
            continue
        try:
            if _proc_ppid(pid) != parent:
                continue
        except Exception:
            continue
        if _pid_names_this_jar(pid, user_data_dir):
            kids.add(pid)
    return kids


def _leftover_or_chrome_family_holds(
    port: int,
    leftover_pids: Set[int],
    chrome_pid: int,
    user_data_dir: str,
) -> bool:
    """True when *port* is chrome plus leftover and/or chrome's family.

    Finding 188: leftover helpers that inherited chrome's unique CDP
    listen are this-jar inode holders of that port — holders equal
    leftover file holders plus chrome. Finding 189: Chromium zygote
    / GPU / utility children also inherit that listen and name this
    jar, and leftover clients are often connect-only (inode only on
    ``DevToolsActivePort``). Holders are then chrome plus this-jar
    children of chrome; leftover file holders need not hold the
    listen. Finding 190: zygote-spawned grandchildren inherit the
    same listen; their PPID is zygote, not chrome. Finding 191:
    leftover helpers' this-jar parent is leftover daemon, not
    chrome, so chrome is absent from that parent check. A unique
    browser-process root of the extra holders is still chrome.
    Finding 195: leftover daemon that inherited leftover CRI
    is a unique this-jar listen. That leftover persist is
    not chrome just because leftover helpers' parent holds
    it. Leftover daemon / CRI / python are not chrome.
    Finding 200: leftover python / CRI / fill that inherited
    chrome's CDP but is not a leftover file holder is extra,
    not an unrelated second Chrome. Non-browser extras stay
    leftover. An unrelated this-jar *browser* is not chrome's
    family.
    """
    holders = _this_jar_holder_pids(port, user_data_dir)
    extra = holders - leftover_pids
    if chrome_pid in holders and _pid_is_chromium_browser(chrome_pid):
        for holder in holders:
            if holder == chrome_pid or holder in leftover_pids:
                continue
            if not _pid_is_chromium_browser(holder):
                continue
            if not _this_jar_descendant_of_chrome(holder, chrome_pid):
                return False
        return True
    leftover_parents = {
        parent
        for helper in leftover_pids
        if (parent := _proc_ppid(helper)) is not None
        and _pid_names_this_jar(parent, user_data_dir)
        and parent in extra
    }
    if len(leftover_parents) == 1:
        parent = next(iter(leftover_parents))
        if _unique_listen_family_root(
            extra & _this_jar_children(parent, user_data_dir)
        ) is not None:
            return True
    return _unique_listen_family_root(extra) is not None


def _leftover_inherited_chrome_listen(
    named: int,
    leftover_pids: Set[int],
    chrome_pid: int,
    scan_pids: list[int],
    user_data_dir: str,
) -> Optional[int]:
    """Other listen leftover holders share with chrome, or ``None``.

    Finding 188: leftover helpers that inherited chrome's unique CDP
    listen are this-jar inode holders of that port (finding 163).
    184 / 185 / 186 required n==1, so first persist stamped leftover
    DevTools. Finding 189: leftover-shared equality missed when
    chrome's children also hold that listen, or leftover is
    connect-only. Exactly one other listen whose this-jar holders
    are chrome plus leftover and/or chrome's this-jar family is
    not a guess among ports. Finding 190: zygote grandchildren
    that inherit that listen are still that family. Several such
    listens stay unknown (85). Finding 196: leftover daemon
    inherited leftover CRI / ``--cdp`` and chrome inherited that
    same listen, so leftover persist and chrome CDP both look
    leftover-shared. Chrome's own listen is the leftover-shared
    port leftover helpers' parent does not inode-hold. Several
    ports that parent also holds stay unknown. Do not guess
    when leftover helpers' parent is chrome (lock-present /
    finding 186). Finding 199: leftover fill uniquely holds
    stale DevTools and also inherited leftover persist with
    chrome. Leftover daemon holds neither leftover persist
    nor chrome's own CDP, so 196's leftover-daemon-miss
    list is several. Prefer the unique inherited listen
    leftover file holders do not inode-hold — that is
    chrome's own CDP. Several such listens stay unknown
    (85). Do not guess when leftover helpers' parent is
    chrome (lock-present / 186). Finding 200: leftover
    python / CRI / fill that inherited chrome's CDP but
    is not a leftover file holder used to be a competing
    extra root, so leftover-shared / leftover-file-miss
    missed chrome's own CDP. Non-browser extras stay
    leftover. Several browser roots stay unknown.
    Finding 201: leftover python / CRI inherited leftover
    persist with chrome. Leftover file holders hold neither
    leftover persist nor chrome's own CDP, so 199 leftover-
    file-miss is several. Prefer the unique leftover-file-
    miss listen leftover siblings do not inode-hold.
    Leftover python that inherited both stays 85.
    Finding 202: leftover python / CRI spawned by leftover
    fill (the unique leftover-file holder) inherited leftover
    persist. leftover-sibling-miss only walked leftover
    daemon's children, so leftover fill's leftover child
    hid sibling chrome. One hop of leftover siblings'
    this-jar children is not a grandparent walk. Orphan
    leftover python stays 86. Leftover GPU / chrome-family
    leftover persist plus chrome CDP leftover siblings
    do not hold stays 85.
    """
    if not leftover_pids or not isinstance(chrome_pid, int) or chrome_pid <= 1:
        return None
    inherited: list[int] = []
    seen: Set[int] = set()
    for scan_pid in scan_pids:
        try:
            ports = _loopback_listen_ports_for_pid(scan_pid)
        except Exception:
            continue
        for port in ports:
            if port == named or port in seen:
                continue
            seen.add(port)
            if _leftover_or_chrome_family_holds(
                port, leftover_pids, chrome_pid, user_data_dir,
            ):
                inherited.append(port)
    if len(inherited) == 1:
        return inherited[0]
    if len(inherited) <= 1 or _pid_is_chromium_browser(chrome_pid):
        return None
    own: list[int] = []
    for port in inherited:
        try:
            holders = _this_jar_holder_pids(port, user_data_dir)
        except Exception:
            continue
        if chrome_pid not in holders:
            own.append(port)
    if len(own) == 1:
        return own[0]
    # Finding 199: leftover daemon holds neither leftover
    # persist leftover fill inherited nor chrome's own CDP.
    # Prefer the unique inherited listen leftover file
    # holders do not inode-hold. Several stay unknown.
    not_leftover: list[int] = []
    for port in inherited:
        try:
            holders = _this_jar_holder_pids(port, user_data_dir)
        except Exception:
            continue
        if leftover_pids.isdisjoint(holders):
            not_leftover.append(port)
    if len(not_leftover) == 1:
        return not_leftover[0]
    # Finding 201: leftover python / CRI inherited leftover
    # persist with chrome. Leftover file holders hold neither
    # leftover persist nor chrome's own CDP, so 199's
    # leftover-file-miss list is several. Prefer the unique
    # leftover-file-miss listen leftover siblings (non-browser
    # this-jar children of leftover parent) do not inode-hold.
    # Leftover python that inherited both leftover persist
    # and chrome's CDP stays 85. Several stay unknown.
    # Do not guess when leftover helpers' parent is chrome
    # (already returned above).
    # Finding 202: leftover python / CRI spawned by leftover
    # fill inherited leftover persist. leftover-sibling-miss
    # only walked leftover daemon's children. One hop of
    # leftover siblings' this-jar children is leftover fill's
    # leftover child, not a leftover-holder grandparent walk.
    if len(not_leftover) <= 1:
        return None
    siblings = set(leftover_pids)
    try:
        for child in _this_jar_children(chrome_pid, user_data_dir):
            if not _pid_is_chromium_browser(child):
                siblings.add(child)
    except Exception:
        pass
    try:
        extra_siblings: Set[int] = set()
        for sibling in siblings:
            if _pid_is_chromium_browser(sibling):
                continue
            for child in _this_jar_children(sibling, user_data_dir):
                if not _pid_is_chromium_browser(child):
                    extra_siblings.add(child)
        siblings |= extra_siblings
    except Exception:
        pass
    not_sibling: list[int] = []
    for port in not_leftover:
        try:
            holders = _this_jar_holder_pids(port, user_data_dir)
        except Exception:
            continue
        if siblings.isdisjoint(holders):
            not_sibling.append(port)
    return not_sibling[0] if len(not_sibling) == 1 else None


def unique_lock_chrome_hidden_by_leftover_file(
    named: Optional[int],
    user_data_dir: Optional[str] = None,
) -> Optional[int]:
    """Unique lock-listed chrome when *named* is leftover helpers, or ``None``.

    Finding 184: persist may never have been stamped. Unique-listen
    recover is unknown when Chromium has several specific loopbacks
    (85). File TCP / finding 164 then stamp leftover
    ``DevToolsActivePort`` helpers (several holders) as the first
    persist, even when the lock pid still has exactly one other
    unique this-jar listen — that is chrome. Several other unique
    lock ports stay unknown (do not guess).     File unique is finding
    170 / 172 advertisement. Finding 197: unique leftover
    daemon on stale DevTools is not chrome — scan leftover
    daemon's this-jar children for sibling chrome. Finding 198:
    unique leftover fill / CRI on stale DevTools is not
    chrome — scan leftover fill's this-jar parent and that
    parent's children. Finding 199: leftover fill that also
    inherited leftover persist with chrome still hides
    sibling chrome when leftover daemon holds neither
    listen — prefer chrome's own CDP leftover fill does
    not inode-hold. Finding 200: leftover python / CRI
    that inherited chrome's CDP but is not a leftover
    file holder is extra, not a competing browser root.
    Finding 201: leftover python / CRI that inherited
    leftover persist with chrome still hides sibling
    chrome when leftover file holders hold neither
    listen — prefer chrome's own CDP leftover siblings
    do not inode-hold.
    Finding 202: leftover python / CRI spawned by leftover
    fill inherited leftover persist. leftover-sibling-miss
    only walked leftover daemon's children. One hop of
    leftover siblings' this-jar children is leftover fill's
    leftover child. Orphan leftover python stays 86.
    Finding 185: when the lock is gone,
    leftover holders that inherited chrome's DevTools fd still
    advertise that unique listen. Finding 186: leftover this-jar
    helpers that are no longer leftover holders of chrome still
    have chrome as parent. That parent's other unique listen is
    chrome. Helpers whose parent does not name this jar stay
    finding 86. Finding 188: leftover helpers that inherited
    chrome's unique CDP listen are this-jar inode holders of
    that port, so n==1 misses. Exactly one leftover-shared
    listen with chrome is still chrome. Finding 189: chrome's
    zygote / GPU / utility children also inherit that listen,
    and leftover clients often only connect to DevTools — they
    are not inode holders of chrome. Chrome plus this-jar
    children of chrome is still chrome; leftover need not hold
    that listen. Finding 190: zygote grandchildren that inherit
    that listen are still chrome's family. Finding 195:
    leftover daemon that inherited leftover CRI / ``--cdp``
    is a unique this-jar listen. That leftover persist is
    not chrome — sibling chrome is still a this-jar child
    of leftover daemon. Several such listens stay unknown.
    No HTTP.
    """
    if user_data_dir is None:
        user_data_dir = str(profile_dir())
    if not isinstance(named, int) or not (1 <= named <= 65535):
        return None
    try:
        leftover_pids = {
            holder_pid
            for holder_pid, _hosts in _this_jar_listen_holders(named, user_data_dir)
        }
    except Exception:
        return None
    if len(leftover_pids) <= 1:
        if len(leftover_pids) != 1:
            return None
        holder = next(iter(leftover_pids))
        # Finding 161: unique leftover file that IS chrome stays
        # the file holder. Finding 197: leftover fill clients
        # exited and chrome dropped the inherited DevTools
        # listen, so leftover daemon uniquely holds stale
        # ``DevToolsActivePort``. Finding 198: one leftover
        # fill / CRI can uniquely hold that stale listen
        # after leftover daemon dropped the fd. Chrome is a
        # sibling under leftover daemon — children of the
        # unique leftover fill miss chrome. Scan leftover
        # fill's this-jar parent (leftover daemon) and that
        # parent's children. A leftover fill whose parent
        # is leftover CRI (cousin chrome) stays 86 — do
        # not walk grandparents. Unique leftover daemon /
        # CRI / python is not chrome.
        if _pid_is_chromium_browser(holder):
            return None
        if not _pid_names_this_jar(holder, user_data_dir):
            return None
        try:
            ppid = _proc_ppid(holder)
        except Exception:
            ppid = None
        if (
            isinstance(ppid, int)
            and ppid > 1
            and _pid_names_this_jar(ppid, user_data_dir)
        ):
            parent = ppid
        else:
            parent = holder
        scan_pids = [parent]
        if holder not in scan_pids:
            scan_pids.append(holder)
        try:
            for child in _this_jar_children(parent, user_data_dir):
                if child not in scan_pids:
                    scan_pids.append(child)
        except Exception:
            pass
        chrome: list[int] = []
        seen: set[int] = set()
        for scan_pid in scan_pids:
            try:
                ports = _loopback_listen_ports_for_pid(scan_pid)
            except Exception:
                continue
            for port in ports:
                if port == named or port in seen:
                    continue
                if _unique_chromium_browser_holder(port, user_data_dir) is not None:
                    seen.add(port)
                    chrome.append(port)
        if len(chrome) == 1:
            return chrome[0]
        if chrome:
            return None
        return _leftover_inherited_chrome_listen(
            named, leftover_pids, parent, scan_pids, user_data_dir,
        )
    pid = _lock_pid(user_data_dir)
    if pid is not None and _pid_names_this_jar(pid, user_data_dir):
        try:
            lock_ports = _loopback_listen_ports_for_pid(pid)
        except Exception:
            return None
        chrome: list[int] = []
        for port in lock_ports:
            if port == named:
                continue
            if _unique_chromium_browser_holder(port, user_data_dir) is not None:
                chrome.append(port)
        if len(chrome) == 1:
            return chrome[0]
        if chrome:
            return None
        return _leftover_inherited_chrome_listen(
            named, leftover_pids, pid, [pid], user_data_dir,
        )
    # Finding 185: lock is gone. Leftover holders that inherited
    # chrome's DevTools fd still advertise chrome's other unique
    # listen. Finding 186: leftover this-jar helpers that dropped
    # that fd still have chrome as parent. Several other unique
    # holder ports stay unknown (do not guess). A parent that
    # does not name this jar stays finding 86. Finding 188:
    # leftover that inherited chrome's unique listen make n==1
    # miss; leftover-shared with that parent is still chrome.
    # Finding 189: leftover connect-only plus chrome's children
    # on that listen is the same class. Finding 190: zygote
    # grandchildren on that listen are still chrome's family.
    chrome = []
    scan_pids: list[int] = []
    for holder_pid in leftover_pids:
        if holder_pid not in scan_pids:
            scan_pids.append(holder_pid)
        try:
            ppid = _proc_ppid(holder_pid)
        except Exception:
            ppid = None
        if (
            isinstance(ppid, int)
            and ppid > 1
            and ppid not in scan_pids
            and _pid_names_this_jar(ppid, user_data_dir)
        ):
            scan_pids.append(ppid)
    seen: set[int] = set()
    for scan_pid in scan_pids:
        try:
            ports = _loopback_listen_ports_for_pid(scan_pid)
        except Exception:
            continue
        for port in ports:
            if port == named or port in seen:
                continue
            if _unique_chromium_browser_holder(port, user_data_dir) is not None:
                seen.add(port)
                chrome.append(port)
    if len(chrome) == 1:
        return chrome[0]
    if chrome:
        return None
    parent = _unique_this_jar_parent(leftover_pids, user_data_dir)
    if parent is None:
        return None
    if parent not in scan_pids:
        scan_pids.append(parent)
    # Finding 191: leftover connect-only helpers do not list
    # chrome's listen. Chrome is a this-jar child of leftover
    # daemon (sibling of those helpers). Lock-present stays
    # lock-ports only (184).
    try:
        for child in _this_jar_children(parent, user_data_dir):
            if child not in scan_pids:
                scan_pids.append(child)
    except Exception:
        pass
    return _leftover_inherited_chrome_listen(
        named, leftover_pids, parent, scan_pids, user_data_dir,
    )


def running_instance_cdp_port(user_data_dir: str, *, exclude_session: Optional[str] = None) -> Optional[int]:
    """DevTools port of a Chromium currently running on ``user_data_dir``, or ``None``.

    Both files outlive a crashed or closed Chromium: ``SingletonLock`` is a
    ``host-pid`` symlink (or a materialized regular file of the same
    text — finding 160) and ``DevToolsActivePort`` keeps the last port, so
    the pid must be alive AND still name this ``user-data-dir`` AND this
    pid must still listen on that port AND the listen must accept a
    connection. When the lock is gone the file port is still this jar
    if this-jar pids inode-listen there (finding 161 unique pid;
    finding 164 several holders — persist is a port, not a pid). A sibling Chrome that reused the stale file port, or a
    recycled lock pid that does not name this jar, is not the dock. When
    the port file is gone or stale but the lock pid is still this
    profile's Chromium, recover the port from that pid (explicit
    ``--remote-debugging-port=N`` this pid still listens on, or a unique
    loopback listen).     When the lock pid no longer holds the file listen
    and recover is unknown, helpers that still name this jar and
    inode-hold the file port are that port (finding 164). If the lock
    pid still lists that number and TCP to its family failed, do not
    stamp another family's holder (finding 165). If recover is unknown
    because Chromium has several specific loopbacks (finding 85) and
    the lock pid still lists stamped persist — not the file — persist
    is current chrome (finding 172 / 174).     Finding 177: that persist
    may live only in ``_last_dock_cdp_port`` after a remember miss.
    Finding 179: that persist may still be this-jar inode-listed
    after Take over unlinks ``SingletonLock``. Do not let 164
    overwrite that stamp with a stale ``DevToolsActivePort``
    helpers still hold.     Finding 182: ``remember_dock_cdp_port``
    writes the file only. Agent attach calls this function
    without ``persist_live`` (180). Sync in-process memory on a
    live stamp so a later unlink cannot treat leftover memory
    as lock-listed chrome. Finding 183: lock inherited leftover
    DevTools fd so the file port is also listed. File TCP then
    stamped helpers over persist chrome (unique holder, several
    leftover holders). Finding 172 chrome-switch (persist
    several / named unique) still keeps the file. When file
    TCP misses because leftover already holds that socket
    (166), still try persist chrome — do not skip 174 just
    because the lock also lists the leftover file. Finding
    184: persist may never have been stamped. File TCP /
    finding 164 then stamp leftover helpers as the first
    persist even when the lock pid still has exactly one
    other unique this-jar listen. That listen is chrome.
    Finding 185: overlay can unlink the lock before that
    first stamp. Leftover holders that inherited chrome's
    DevTools fd still name the unique listen. Do not let
    164 make leftover helpers the first persist.
    Finding 188: leftover that inherited chrome's unique
    CDP listen are this-jar inode holders of that port,
    so n==1 misses. Leftover-shared with chrome is still
    the first persist.
    An instance
    agent-browser launched for ``exclude_session`` itself is reported as ``None``:
    its daemon already owns that browser, and handing it ``--cdp`` would make it
    close the browser as a config change and then attach to the port that just died with it.
    """
    pid = _lock_pid(user_data_dir)
    if pid is not None and not _pid_names_this_jar(pid, user_data_dir):
        pid = None
    port_line = ""
    try:
        with open(os.path.join(user_data_dir, "DevToolsActivePort"), encoding="utf-8") as fh:
            port_line = fh.readline().strip()
    except OSError:
        port_line = ""
    if pid is None:
        # Finding 161: SingletonLock can be gone while Chromium still
        # holds DevTools. The file still names a port — leftover that
        # already aimed there used to look like another Chrome. Do not
        # guess among ports when the file is gone too. Several this-jar
        # holders on that file-named number are not a guess (finding 164).
        # Finding 179: in-process memory may still name chrome after
        # Take over unlinks the lock. Finding 181: a process restart
        # clears that memory; persist file that this jar still
        # inode-listens on is the last live stamp. Do not let 164
        # stamp leftover file helpers over that persist.
        if not port_line.isdigit():
            return None
        persist = lock_listed_persist_port(user_data_dir)
        file_port = int(port_line)
        if persist is not None and persist != file_port:
            # Finding 179 / 185: do not shop leftover file helpers
            # over lock-listed / leftover-holder chrome. Try that
            # chrome's TCP so first persist is not those helpers
            # after overlay unlinks the lock. A TCP miss stays
            # None (179 leftover already holds the socket).
            if exclude_session:
                try:
                    holders = _this_jar_listen_holders(persist, user_data_dir)
                except Exception:
                    holders = []
                if any(
                    _launched_by_session(holder_pid) == exclude_session
                    for holder_pid, _ in holders
                ):
                    return None
            try:
                hosts = _this_jar_listen_connect_hosts(persist)
            except Exception:
                hosts = ()
            if hosts and _cdp_port_reachable(persist, hosts):
                port = persist
            else:
                return None
        else:
            port = _file_named_this_jar_listen_port(
                file_port, user_data_dir, exclude_session=exclude_session,
            )
            if port is None:
                return None
    else:
        # Recover already refuses a recycled lock pid whose cmdline /
        # ``CHROME_USER_DATA_DIR`` is not this jar. The DevToolsActivePort
        # branch used to skip that check: a sibling Chrome that inherited
        # the lock and listened on the stale file port was stamped as the
        # dock (finding 158). Leftover aimed at that sibling then looked
        # like the jar a human holds.
        if exclude_session and _launched_by_session(pid) == exclude_session:
            return None
        port = None
        lock_still_lists_file_port = False
        file_port = int(port_line) if port_line.isdigit() else None
        if file_port is not None:
            # File has no address and outlives a port switch. Trust it only
            # when this pid still holds that listen — otherwise a sibling
            # on the stale number is stamped as the dock (finding 144).
            if file_port in _loopback_listen_ports_for_pid(pid):
                lock_still_lists_file_port = True
                hosts = _listen_connect_hosts(pid, file_port)
                if hosts and _cdp_port_reachable(file_port, hosts):
                    port = file_port
        lock_ports = _loopback_listen_ports_for_pid(pid)
        persist = lock_listed_persist_port(user_data_dir)
        persist_on_lock = persist is not None
        hidden_chrome = unique_lock_chrome_hidden_by_leftover_file(
            file_port, user_data_dir,
        )
        file_hides_chrome = persist_on_lock and leftover_helpers_hide_persist_chrome(
            persist, file_port, user_data_dir,
        )
        if (
            hidden_chrome is not None
            and port is not None
            and hidden_chrome != port
        ):
            # Finding 188: leftover inherited chrome's unique listen,
            # so persist / hidden chrome has several holders and
            # 183's unique-persist check misses. File TCP then
            # stamped leftover DevTools over chrome.
            file_hides_chrome = True
        if port is not None and file_hides_chrome and (
            persist != port if persist_on_lock else hidden_chrome != port
        ):
            # Finding 183: lock inherited leftover DevTools fd, so
            # file TCP succeeds on helpers chrome still lists.
            # Persist unique + named several is leftover hiding
            # chrome — do not overwrite dock-cdp-port. Finding 172
            # chrome-switch (persist several / named unique) keeps
            # the file stamp. Finding 188: leftover-shared chrome
            # is the same class when n!=1.
            port = None
        if port is None:
            recovered = _recover_cdp_port_from_singleton(user_data_dir, pid)
            if (
                recovered is not None
                and recovered in lock_ports
            ):
                hosts = _listen_connect_hosts(pid, recovered)
                if hosts and _cdp_port_reachable(recovered, hosts):
                    port = recovered
        if (
            port is None
            and persist_on_lock
            and (not lock_still_lists_file_port or file_hides_chrome)
        ):
            # Finding 174: unique-listen recover stays unknown when
            # Chromium has several specific loopbacks (finding 85).
            # Persist already named a listen this pid still holds —
            # that is not a guess. Finding 164 then persisted a stale
            # file-named helper listen (inherited leftover fd) and
            # overwrote ``dock-cdp-port``. Finding 172's attach
            # tie-break never ran because recover / 164 succeeded.
            # Lock listing persist and the file is finding 172 (file
            # TCP already tried) unless leftover helpers hide
            # persist chrome (finding 183). Do not shop helpers on
            # the file while persist is still on this pid.
            hosts = _listen_connect_hosts(pid, persist)
            if hosts and _cdp_port_reachable(persist, hosts):
                port = persist
        if (
            port is None
            and hidden_chrome is not None
            and hidden_chrome in lock_ports
        ):
            # Finding 184: persist never stamped. Unique chrome on
            # the lock is not a guess. Do not let 164 / file TCP
            # make leftover helpers the first persist.
            hosts = _listen_connect_hosts(pid, hidden_chrome)
            if hosts and _cdp_port_reachable(hidden_chrome, hosts):
                port = hidden_chrome
        if port is None and port_line.isdigit() and not lock_still_lists_file_port:
            # Finding 164: lock pid is still this jar but no longer holds
            # the file listen (fork inherit). Helpers that name this jar
            # and inode-hold that number are the file-named dock port.
            # Finding 165: if this pid still lists that number and TCP to
            # its family failed, do not shop other holders' families —
            # a sibling squat on 127.0.0.1 after ::1 died used to stamp.
            # Finding 174: if this pid still lists persist, the file is
            # the inherited leftover — do not overwrite that stamp.
            # Finding 184: unique lock chrome hidden by leftover
            # helpers is not a 164 stamp. Do not guess when the
            # file is gone too.
            if not persist_on_lock and hidden_chrome is None:
                port = _file_named_this_jar_listen_port(
                    int(port_line), user_data_dir, exclude_session=exclude_session,
                )
        if port is None:
            return None
    try:
        if Path(user_data_dir).resolve() == profile_dir().resolve():
            remember_dock_cdp_port(port)
            # Finding 182: remember writes the file only. Agent
            # attach calls this without persist_live. Leftover
            # memory then became lock-listed after Take over
            # unlinked the lock (179) and attach followed
            # leftover helpers. A miss must not clobber chrome.
            _sync_last_dock_cdp_port(port)
    except OSError:
        pass
    return port


_DOCK_PORT_FILE = "dock-cdp-port"


def _dock_port_path() -> Path:
    return runtime.state_dir() / _DOCK_PORT_FILE


def _this_jar_chromium_pid(user_data_dir: Optional[str] = None) -> Optional[int]:
    """Alive lock pid that still names this jar, or ``None``.

    Recover already refuses a recycled SingletonLock pid whose cmdline
    / ``CHROME_USER_DATA_DIR`` is not this ``user-data-dir``. Take over
    used to skip whatever pid the lock pointed at (finding 157), so a
    leftover writer that inherited a crashed chrome's lock survived.
    A dead lock pid is not Chromium. A materialized regular-file lock
    is still this jar when the pid names it (finding 160).     A missing
    lock still names this jar when DevToolsActivePort and a unique
    this-jar listen agree (finding 161). Finding 187: leftover
    ``DevToolsActivePort`` helpers are several holders, so 161
    stays unknown. Findings 185 / 186 already named unique chrome
    on another listen. Skip-kill / ``shared_chromium_owner_session``
    still need that pid — Take over otherwise tree-kills the
    daemon that spawned chrome.     Unique holder of that hidden
    chrome is not a guess among leftover helpers. Finding 197:
    unique leftover daemon on stale DevTools is not chrome.
    Finding 200: leftover python / CRI that inherited
    chrome's CDP but is not a leftover file holder is
    extra — unique browser-process root of extra is
    still chrome.
    Finding 188:
    leftover that inherited chrome's unique listen make that
    holder several. Lock pid or the unique leftover-file parent
    that still holds hidden is chrome. Finding 189: chrome's
    children also hold hidden, so unique-holder scan still
    misses. That parent is chrome, not a zygote sibling. No
    HTTP.
    """
    if user_data_dir is None:
        user_data_dir = str(profile_dir())
    pid = _lock_pid(user_data_dir)
    if pid is not None and _pid_names_this_jar(pid, user_data_dir):
        return pid
    try:
        with open(os.path.join(user_data_dir, "DevToolsActivePort"), encoding="utf-8") as fh:
            port_line = fh.readline().strip()
    except OSError:
        return None
    if not port_line.isdigit():
        return None
    file_port = int(port_line)
    holder = _scan_this_jar_listen_holder(file_port, user_data_dir)
    if holder is not None and _pid_is_chromium_browser(holder[0]):
        return holder[0]
    # Finding 187: leftover file helpers are several holders.
    # Finding 197: unique leftover daemon on stale DevTools is
    # not chrome — fall through to hidden sibling chrome.
    # 185 / 186 already named unique chrome. Skip-kill and
    # owner-session still need that pid.
    try:
        hidden = unique_lock_chrome_hidden_by_leftover_file(
            file_port, user_data_dir,
        )
    except Exception:
        hidden = None
    if hidden is None:
        return None
    holder = _scan_this_jar_listen_holder(hidden, user_data_dir)
    if holder is not None:
        return holder[0]
    # Finding 188: leftover inherited chrome's unique listen, so
    # hidden chrome has several this-jar holders. Lock pid is
    # chrome when present. Otherwise the unique this-jar parent
    # of leftover file holders that still holds hidden is chrome.
    lock = _lock_pid(user_data_dir)
    if lock is not None and _pid_names_this_jar(lock, user_data_dir):
        try:
            if hidden in _loopback_listen_ports_for_pid(lock):
                return lock
        except Exception:
            return None
        return None
    leftover_pids = _this_jar_holder_pids(file_port, user_data_dir)
    parent = _unique_this_jar_parent(leftover_pids, user_data_dir)
    # Finding 197: unique leftover daemon on stale DevTools has
    # no this-jar parent. That leftover daemon IS the leftover
    # parent — sibling chrome is still its this-jar child.
    if parent is None and len(leftover_pids) == 1:
        only = next(iter(leftover_pids))
        if (
            not _pid_is_chromium_browser(only)
            and _pid_names_this_jar(only, user_data_dir)
        ):
            parent = only
    if parent is None:
        return None
    hidden_holders = _this_jar_holder_pids(hidden, user_data_dir)
    extra = hidden_holders - leftover_pids
    extra_root = None
    # Finding 191: leftover parent that inherited chrome's listen
    # is in extra. Unique ancestor of extra is leftover daemon,
    # not chrome. Children of that one parent first.
    if parent in hidden_holders:
        extra_root = _unique_listen_family_root(
            extra & _this_jar_children(parent, user_data_dir)
        )
    if extra_root is None:
        extra_root = _unique_listen_family_root(extra)
    if extra_root is not None:
        return extra_root
    if parent in hidden_holders and _pid_is_chromium_browser(parent):
        return parent
    return None


def _configured_cdp_override_url() -> str:
    """``/browser connect`` / ``BROWSER_CDP_URL`` / ``browser.cdp_url``, or empty."""
    try:
        from tools.browser_tool_cdp import _get_cdp_override_raw
        return (_get_cdp_override_raw() or "").strip()
    except Exception:
        return ""


def _this_jar_listen_connect_hosts(want: Optional[int]) -> Tuple[str, ...]:
    """This jar's connect hosts for leftover-named ``want``, or empty.

    Named-listen identity (finding 155 / 161) does not need
    ``DevToolsActivePort``. Family check used ``_this_jar_chromium_pid``,
    which does — so a missing lock *and* missing file left leftover
    ``--cdp http://127.0.0.1:<port>`` looking like this jar when the
    dock was ``::1``-only (finding 162). Several this-jar pids that
    still inode-hold the named listen are not another Chrome
    (finding 163) — union their connect hosts. Persist / skip-kill
    still need a unique pid. Empty hosts stay port-only. No HTTP.
    """
    if not isinstance(want, int) or not (1 <= want <= 65535):
        return ()
    user_data_dir = str(profile_dir())
    pid = _lock_pid(user_data_dir)
    if pid is not None and _pid_names_this_jar(pid, user_data_dir):
        if want in _loopback_listen_ports_for_pid(pid):
            return _listen_connect_hosts(pid, want)
    return _union_listen_hosts(_this_jar_listen_holders(want, user_data_dir))


def dock_cdp_attach_target(port: int) -> str:
    """CDP target for attaching to this jar's *port*. Family-correct.

    Persist / ``running_instance_cdp_port`` are a port. Agent-browser
    ``--cdp <port>`` is unknown-family (localhost / ``127.0.0.1``), so
    a ``::1``-only dock plus a sibling on ``127.0.0.1:same`` attached
    to the squat — leftover identity already rejects that family
    (finding 145). Prefer a this-jar connect host. Empty hosts stay
    a bare port (fixtures / unknown listen). No HTTP.
    """
    if not isinstance(port, int) or not (1 <= port <= 65535):
        return ""
    try:
        hosts = _this_jar_listen_connect_hosts(port)
    except Exception:
        hosts = ()
    if not hosts:
        return str(port)
    host = hosts[0]
    if ":" in host and not host.startswith("["):
        return f"http://[{host}]:{port}"
    return f"http://{host}:{port}"


def _this_jar_listens_on_port(want: Optional[int]) -> bool:
    """True when this profile's Chromium inode-listens on ``want``.

    Unique-listen recover stays unknown when Chromium has several
    *specific* loopbacks — do not guess among them. A caller that
    already named ``want`` (leftover ``--cdp-url`` / vault attach /
    ``/browser connect``) is not a guess. Stamp if SingletonLock's
    pid holds that listen and cmdline names this ``user-data-dir``,
    or — when the lock is gone — if a unique this-jar pid still
    inode-listens there (finding 161). A sibling on 9222, empty
    inodes, and a recycled pid are not. No HTTP.
    """
    hosts = _this_jar_listen_connect_hosts(want)
    return bool(hosts) and _cdp_port_reachable(want, hosts)


def _configured_listen_port_for_this_jar() -> Optional[int]:
    """Override port when this profile's Chromium listens on it.

    Unique-listen recover stays unknown when Chromium has several *specific*
    loopbacks. The operator override still names the DevTools port. Stamp
    it only if SingletonLock's pid (symlink or materialized file) holds
    that listen and cmdline names this ``user-data-dir`` — a config
    pointing at another Chrome on 9222 must not become the dock. No HTTP.
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
    if not _this_jar_listens_on_port(want):
        return None
    # Finding 176: persist is still on the lock pid after a 174
    # persist-TCP miss. A configured / ``/browser connect`` /
    # ``BROWSER_CDP_URL`` URL that names leftover ``DevToolsActivePort``
    # helpers is a this-jar listen (finding 86), but it is the
    # inherited leftover — not the current chrome. Identity already
    # refuses to stamp that listen (finding 175); persist_live must
    # not fall through to the same URL and overwrite lock-listed
    # persist. Finding 177: lock-listed persist includes in-process
    # memory when the persist file is missing. Finding 179: that
    # persist may still be this-jar inode-listed after the lock is
    # gone. Finding 86 still stamps configured when persist is
    # *not* on the lock.
    listed = lock_listed_persist_port()
    if listed is not None and listed != want:
        return None
    return want


def persist_live_dock_cdp_port() -> Optional[int]:
    """Best-effort stamp of the live dock DevTools port for this profile.

    Human-first Take over can happen before any agent browser call has
    seen ``DevToolsActivePort``. Persist now, while the probe still
    works, so a later miss cannot treat this jar as another Chrome.

    When unique-listen recover is ambiguous, a loopback override that
    this jar actually listens on is still this profile's DevTools port.
    Finding 176: persist_live must not fall through to a configured
    listen that names leftover file helpers while persist is still
    on the lock.     Finding 177: that persist may live only in
    ``_last_dock_cdp_port`` after a remember miss. Finding 179:
    that persist may still be this-jar inode-listed after the
    lock is gone. Finding 180: ``remember_dock_cdp_port`` still
    writes the file only, so a live stamp left leftover memory
    in place. Take over can then unlink the lock and 179 / 172
    follow that leftover. Sync in-process memory when this
    stamp succeeds. Finding 182: ``running_instance_cdp_port``
    is the other live-stamp door (agent attach) and syncs
    itself. A miss must not clobber chrome memory.
    Finding 183: lock inherited leftover DevTools fd so
    file TCP can succeed on helpers chrome still lists.
    persist_live must not stamp those helpers over unique
    persist chrome. Finding 172 chrome-switch stays.
    Finding 184: persist may never have been stamped —
    unique lock chrome hidden by leftover file helpers
    is the first persist, not those helpers.
    Finding 185: overlay can unlink the lock before that
    first stamp. Leftover-holder unique chrome is still
    the first persist.     Finding 186: leftover this-jar
    helpers that dropped chrome's inherited fd still
    have chrome as parent. That parent's other unique
    listen is the first persist. Finding 188: leftover
    that inherited chrome's unique CDP listen are
    this-jar inode holders of that port, so n==1
    misses. Leftover-shared with chrome is still
    the first persist.
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
        _sync_last_dock_cdp_port(port)
    return port


def _sync_last_dock_cdp_port(port: int) -> None:
    """In-process leftover-identity cache for a live dock stamp.

    ``remember_dock_cdp_port`` writes the file only (finding 178).
    persist_live (180) and running_instance (182) sync memory
    themselves. Do not call this from ``remember_dock_cdp_port`` —
    a leftover ``remember(40141)`` must not clobber chrome memory.
    """
    if not isinstance(port, int) or not (1 <= port <= 65535):
        return
    try:
        from hermes_constants import hermes_home_key
        from tools.browser_tool_session import _last_dock_cdp_port
        _last_dock_cdp_port[hermes_home_key()] = port
    except Exception:
        pass


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
    Finding 187: overlay can unlink SingletonLock while leftover
    DevTools helpers are several holders. ``_this_jar_chromium_pid``
    must still find chrome so this is not ``None`` for the owner.
    """
    if user_data_dir is None:
        user_data_dir = str(profile_dir())
    pid = _this_jar_chromium_pid(user_data_dir)
    if pid is None:
        return None
    return _launched_by_session(pid)


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
