"""agent-browser session management: daemon spawn, per-backend session creation
(local/lightpanda/cdp/cloud), cached lookup, command execution + output interpretation.

Split out of ``tools/browser_tool.py``. Facade-owned state is read through ``_bt`` (``tools.browser_tool``, resolved per call) — no import cycle.
"""

import contextlib
import ipaddress
import json
import logging
import os
import re
import shlex
import shutil
import signal
import socket
import subprocess
import threading
import uuid
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Tuple

from hermes_cli._subprocess_compat import windows_hide_flags
from tools.browser_tool_origin import origin as _bt
from tools import browser_tool_cdp as _cdp
from tools import browser_tool_cloud as _cloud
from tools import browser_tool_install as _install
from tools import browser_tool_lifecycle as _lifecycle
from tools import browser_tool_lightpanda_fallback as _lp
from tools import browser_tool_real_profile as _real_profile
from tools import browser_tool_snapshot as _snapshot

_DOCKER_PULL = "docker pull ghcr.io/nousresearch/hermes-agent:latest"
_CHROMIUM_INSTALL = "npx agent-browser install --with-deps (or: npx playwright install --with-deps chromium)"
_CHROMIUM_MISSING_DOCKER_HINT = ("Chromium browser is missing. You're running in Docker — pull the latest image "
                                 f"to get the bundled Chromium: {_DOCKER_PULL}")
_CHROMIUM_MISSING_HINT = f"Chromium browser is missing. Install it with: {_CHROMIUM_INSTALL}"


def _needs_chromium_sandbox_bypass() -> bool:
    """True when Chromium needs --no-sandbox to start reliably (root, Docker, AppArmor userns)."""
    if hasattr(os, "geteuid") and os.geteuid() == 0:
        return True
    if _install._running_in_docker():
        return True
    try:
        with open("/proc/sys/kernel/apparmor_restrict_unprivileged_userns", encoding="utf-8") as f:
            return f.read().strip() == "1"
    except OSError:
        return False


def _apply_chromium_sandbox_args(browser_env: Dict[str, str]) -> None:
    """Add required Chromium sandbox flags without overriding user settings."""
    if ("AGENT_BROWSER_ARGS" not in browser_env and "AGENT_BROWSER_CHROME_FLAGS" not in browser_env
            and _needs_chromium_sandbox_bypass()):
        _bt.logger.debug("browser: sandbox bypass needed (root/docker/AppArmor userns) — injecting --no-sandbox")
        browser_env["AGENT_BROWSER_ARGS"] = "--no-sandbox,--disable-dev-shm-usage"


def _read_command_output_files(stdout_path: str, stderr_path: str) -> tuple[str, str]:
    """Best-effort read of agent-browser stdout/stderr temp files."""
    out = []
    for path in (stdout_path, stderr_path):
        try:
            with open(path, "r", encoding="utf-8") as f:
                out.append(f.read().strip())
        except OSError:
            out.append("")
    return out[0], out[1]


def _unlink_command_output_files(*paths: str) -> None:
    for path in paths:
        try:
            os.unlink(path)
        except OSError:
            pass


def _format_browser_timeout_error(
    command: str, timeout: int, stdout: str, stderr: str
) -> str:
    """Actionable timeout message from captured daemon output."""
    parts = [f"Command timed out after {timeout} seconds"]
    detail = (stderr or stdout or "").strip()
    if detail:
        parts.append(detail[:1500])

    if "sandbox" in f"{stderr}\n{stdout}".lower():
        parts.append("Chromium sandbox launch failed. Set AGENT_BROWSER_ARGS="
                     "'--no-sandbox,--disable-dev-shm-usage' in your environment, "
                     "or run: npx agent-browser install --with-deps")
    elif command == "open" and _cloud._is_local_mode():
        if _install._running_in_docker():
            parts.append("The browser daemon may still be starting or Chromium may be "
                         f"missing. Pull the latest image: {_DOCKER_PULL}")
        else:
            parts.append("The browser daemon may still be starting, or Chromium may be "
                         f"missing system libraries. Install/repair with: {_CHROMIUM_INSTALL}")
    return "\n".join(parts)


def _agent_browser_argv(browser_cmd: str) -> list:
    """Command prefix to invoke agent-browser (concrete binary, or the npx sentinel expanded).

    npx is resolved through the same PATH cascade as ``_find_agent_browser`` (a bare
    ``which("npx")`` would let a broken system npx shadow a healthy managed one); if
    absent the bare name gives a readable ``FileNotFoundError``. ``--ignore-scripts``:
    the spec is a floating range — a compromised future patch must not run install scripts.
    """
    if _install._is_npx_agent_browser_sentinel(browser_cmd):
        _npx_bin = _install._resolve_npx_bin() or "npx"
        return [_npx_bin, "--ignore-scripts", "--prefer-offline", "-y", _bt.AGENT_BROWSER_NPX_SPEC]
    return [browser_cmd]


def _prepare_session_socket_dir(session_name: str) -> str:
    """Create the per-session socket dir (parallel workers must not share one) and claim it
    with our PID BEFORE first use — another hermes process's orphan reaper rmtree's any
    ownerless agent-browser-* dir in the shared tmpdir."""
    socket_dir = os.path.join(_bt._socket_safe_tmpdir(), f"agent-browser-{session_name}")
    os.makedirs(socket_dir, mode=0o700, exist_ok=True)
    _lifecycle._write_owner_pid(socket_dir, session_name)
    return socket_dir


def _agent_browser_command_env(socket_dir: str) -> Dict[str, str]:
    """Credential-scrubbed env for one command: PATH fallbacks, the session socket dir, and
    daemon-side idle self-termination (agent-browser 0.24+) mirroring the Python janitor
    unless the user set ``AGENT_BROWSER_IDLE_TIMEOUT_MS`` explicitly."""
    env = _bt._build_browser_env()
    env["PATH"] = _install._merge_browser_path(env.get("PATH", ""))
    env["AGENT_BROWSER_SOCKET_DIR"] = socket_dir
    if "AGENT_BROWSER_IDLE_TIMEOUT_MS" not in env:
        env["AGENT_BROWSER_IDLE_TIMEOUT_MS"] = str(_bt.BROWSER_SESSION_INACTIVITY_TIMEOUT * 1000)
    return env


def _popen_agent_browser(argv: List[str], env: Dict[str, str], socket_dir: str, tag: str) -> "subprocess.Popen":
    """Spawn agent-browser with stdout/stderr redirected to ``socket_dir/_std{out,err}_<tag>``.

    Temp files, not pipes: the CLI forks a daemon that inherits its fds, so pipes never
    see EOF until the timeout. Windows: CREATE_NO_WINDOW only (CREATE_NEW_PROCESS_GROUP
    cancels asyncio's running task on 3.11), STARTF_USESTDHANDLES + close_fds so the child
    gets ONLY our three handles (leaked console handles kill the Rust daemon grandchild).
    """
    fds = [os.open(os.path.join(socket_dir, f"_{slot}_{tag}"), os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
           for slot in ("stdout", "stderr")]
    try:
        _popen_extra: dict = {}
        if os.name == "nt":
            _si = subprocess.STARTUPINFO()
            _si.dwFlags |= subprocess.STARTF_USESTDHANDLES
            _popen_extra = {"creationflags": windows_hide_flags(), "close_fds": True, "startupinfo": _si}
        return subprocess.Popen(argv, stdout=fds[0], stderr=fds[1], stdin=subprocess.DEVNULL, env=env, **_popen_extra)
    finally:
        for fd in fds:
            os.close(fd)


def _session_record(prefix: str, cdp_url: Optional[str], features: Dict[str, Any]) -> Dict[str, Any]:
    """Fresh session dict with a random ``<prefix>_<hex10>`` session name."""
    return {"session_name": f"{prefix}_{uuid.uuid4().hex[:10]}", "bb_session_id": None,
            "cdp_url": cdp_url, "features": features}


def _create_local_session(task_id: str, allow_real_profile: bool = True) -> Dict[str, str]:
    """Local Chromium session; consented real-profile CDP attach when allowed.

    Real-profile fails closed on resolver/launch errors (a consented user must never be
    silently downgraded to a throwaway). The hybrid private-URL sidecar passes
    ``allow_real_profile=False``: the user's cookie jar must not reach an arbitrary
    internal host the model chose.
    """
    _refuse_shared_session_while_human_holds()
    if allow_real_profile:
        cdp_url, err = _real_profile._real_profile_cdp()
        if err:
            raise RuntimeError(err)
        if cdp_url:
            info = _session_record("rp", _cdp._resolve_cdp_override(cdp_url), {"local": True, "real_profile": True})
            _bt.logger.info("Created real-profile local session %s for task %s", info["session_name"], task_id)
            return info

    # Browser Use mode + ``browser.engine: lightpanda`` drives a Hermes-spawned
    # ``lightpanda serve`` (the built-in tools are hidden in that mode).
    if _bt._is_browser_use_cli_mode() and _lp._using_lightpanda_engine():
        return _create_lightpanda_session(task_id)

    info = _session_record("h", None, {"local": True})
    _bt.logger.info("Created local browser session %s for task %s", info["session_name"], task_id)
    return info


def _create_lightpanda_session(task_id: str) -> Dict[str, Any]:
    """Spawn ``lightpanda serve`` for this session key (Browser Use mode)."""
    from tools.browser_lightpanda import launch_lightpanda

    info = _session_record("lp", None, {"local": True, "lightpanda": True})
    server, err = launch_lightpanda(info["session_name"], block_private_networks=not _cloud._is_local_backend())
    if err:
        raise RuntimeError(err)
    info["cdp_url"] = server.cdp_url
    _bt.logger.info("Created Lightpanda session %s (port %s) for task %s", info["session_name"], server.port, task_id)
    return info


def _local_backend_process_dead(session_info: Dict[str, Any]) -> bool:
    """True for a Lightpanda session whose ``lightpanda serve`` is gone."""
    if not (session_info.get("features") or {}).get("lightpanda"):
        return False
    from tools.browser_lightpanda import get_server

    server = get_server(session_info.get("session_name", ""))
    return server is None or not server.is_alive()


def _create_cdp_session(task_id: str, cdp_url: str) -> Dict[str, str]:
    """Session connecting to a user-supplied CDP endpoint."""
    _refuse_shared_session_while_human_holds(cdp_url=cdp_url)
    info = _session_record("cdp", cdp_url, {"cdp_override": True})
    _bt.logger.info("Created CDP browser session %s → %s for task %s",
                info["session_name"], _bt._sanitize_url_for_logs(cdp_url), task_id)
    return info


def _create_cloud_session_or_fallback(task_id: str, provider) -> Dict[str, Any]:
    """Cloud session; fall back to local Chromium (marked degraded) on failure. ``cdp_url``
    is resolved here because some providers return an HTTP discovery URL, not a websocket."""
    try:
        session_info = provider.create_session(task_id)
        if not session_info or not isinstance(session_info, dict):
            raise ValueError(f"Cloud provider returned invalid session: {session_info!r}")
        if session_info.get("cdp_url"):
            session_info = dict(session_info)
            session_info["cdp_url"] = _cdp._resolve_cdp_override(str(session_info["cdp_url"]))
        return session_info
    except Exception as e:
        from tools.bot_desktop.lease import HumanHasControl

        if isinstance(e, HumanHasControl):
            raise
        provider_name = type(provider).__name__
        _bt.logger.warning("Cloud provider %s failed (%s); attempting fallback to local Chromium for task %s",
                           provider_name, e, task_id, exc_info=True)
        try:
            session_info = _create_local_session(task_id)
        except HumanHasControl:
            raise
        except Exception as local_error:
            raise RuntimeError(f"Cloud provider {provider_name} failed ({e}) and local "
                               f"fallback also failed ({local_error})") from e
        if isinstance(session_info, dict):  # mark degraded for observability
            session_info = {**session_info, "fallback_from_cloud": True, "fallback_reason": str(e),
                            "fallback_provider": provider_name}
        return session_info


def _create_session_for_key(task_id: str, force_local: bool) -> Dict[str, Any]:
    """Fresh session for ``task_id`` (runs OUTSIDE the lock: cloud mode makes a network call).
    Precedence: CDP override > hybrid local sidecar (never real-profile) > cloud > local."""
    # Peek the raw override before the HTTP /json/version probe. A live
    # dock Chromium is this profile's desktop jar; probing it while a
    # human holds would still talk to the jar — including the hybrid
    # ``::local`` sidecar, which used to skip the refuse and then probe.
    raw_cdp = _cdp._get_cdp_override_raw()
    if raw_cdp:
        _refuse_shared_session_while_human_holds(cdp_url=raw_cdp)
    if force_local:
        _refuse_shared_session_while_human_holds()
        return _create_local_session(task_id, allow_real_profile=False)
    if raw_cdp:
        cdp_override = _cdp._get_cdp_override()
        if cdp_override:
            return _create_cdp_session(task_id, cdp_override)
    provider = _cloud._get_cloud_provider()
    if provider is None:
        _refuse_shared_session_while_human_holds()
        return _create_local_session(task_id)
    return _create_cloud_session_or_fallback(task_id, provider)


def _get_session_info(task_id: Optional[str] = None) -> Dict[str, Any]:
    """Get or create session info for a session key (thread-safe); also starts the
    inactivity thread and touches activity. A ``::local`` key forces local Chromium
    even with a cloud provider configured.

    Re-enter the session's owning HERMES_HOME first. After a multiplex turn
    the process home is the launch profile; recycle / mint refuse must read
    *this* session's ``lease.json``, not the launch bot's (finding 101).
    """
    if task_id is None:
        task_id = "default"
    with _lifecycle._session_owner_scope(task_id):
        return _get_session_info_unscoped(task_id)


def _get_session_info_unscoped(task_id: str) -> Dict[str, Any]:
    _lifecycle._start_browser_cleanup_thread()
    _lifecycle._update_session_activity(task_id)

    with _bt._cleanup_lock:
        existing_session = _bt._active_sessions.get(task_id)

    def _replacement_after_teardown() -> Optional[Dict[str, Any]]:
        # Teardown removes the activity entry; re-touch so the reaper tracks the
        # replacement. Another thread may already have re-created it — reuse that.
        _lifecycle._update_session_activity(task_id)
        with _bt._cleanup_lock:
            replacement = _bt._active_sessions.get(task_id)
        return replacement if replacement is not None and replacement is not existing_session else None

    if existing_session is not None:
        # A human mid-login owns this Chromium. Recycle (suspect or expired)
        # would kill the daemon after a fenced close — do not even start it.
        if _local_browser_reserved_by_human(existing_session):
            return existing_session
        # Suspect recycle: a command timeout marked this session; the expensive recycle
        # lives here at next use, not on the timeout path (mark must stay cheap).
        if not _bt._browser_session_backend(task_id).ensure_healthy():
            replacement = _replacement_after_teardown()
            if replacement is not None:
                return replacement
            existing_session = None
        elif not _lifecycle._session_has_expired(existing_session) and not _local_backend_process_dead(existing_session):
            return existing_session
        else:
            _bt.logger.info("Replacing expired or dead browser session for task %s", task_id)
            _lifecycle._cleanup_single_browser_session(task_id)
            replacement = _replacement_after_teardown()
            if replacement is not None:
                return replacement

    force_local = _bt._is_local_sidecar_key(task_id)
    try:
        session_info = _create_session_for_key(task_id, force_local)
    except Exception as exc:
        from tools.bot_desktop.lease import HumanHasControl

        if isinstance(exc, HumanHasControl):
            # Create never wrote a row. Drop the activity touch so the
            # janitor does not reap a session that was never minted.
            with _bt._cleanup_lock:
                _bt._session_last_activity.pop(task_id, None)
            raise
        raise

    with _bt._cleanup_lock:
        if task_id in _bt._active_sessions:  # created concurrently during the network call — don't leak ours
            return _bt._active_sessions[task_id]
        session_info = dict(session_info)
        session_info.setdefault("session_key", task_id)
        session_info.setdefault("owner_task_id", _bt._bare_task_id_for_session_key(task_id))
        _bt._active_sessions[task_id] = session_info
        _bt._suspect_browser_sessions.pop(task_id, None)  # brand-new session is healthy by definition

    # Lazy-start the CDP supervisor (idempotent). Skip local sidecars (no CDP URL) and
    # Lightpanda sessions (Browser Use mode hides the tools that consume supervisor state).
    if not force_local and not (session_info.get("features") or {}).get("lightpanda"):
        _cdp._ensure_cdp_supervisor(task_id)

    return session_info


def _discard_timed_out_browser_session(task_id: str, session_info: Dict[str, Any], task_socket_dir: str) -> None:
    """Drop a stuck client generation without losing cloud cleanup state."""
    with _bt._cleanup_lock:
        if _bt._active_sessions.get(task_id) is not session_info:
            return
        _cdp._stop_cdp_supervisor(task_id)
        if session_info.get("bb_session_id") or session_info.get("cdp_url"):
            replacement = dict(session_info)
            replacement["session_name"] = f"h_{uuid.uuid4().hex[:10]}"
            replacement.pop("_first_nav", None)
            _bt._active_sessions[task_id] = replacement
        else:
            _bt._active_sessions.pop(task_id, None)
            _bt._session_last_activity.pop(task_id, None)

        bare_task_id = _bt._bare_task_id_for_session_key(task_id)
        if _bt._last_active_session_key.get(bare_task_id) == task_id:
            _bt._last_active_session_key.pop(bare_task_id, None)

    session_name = str(session_info.get("session_name") or "")
    if session_name and os.path.isfile(os.path.join(task_socket_dir, f"{session_name}.pid")):
        daemon_pid = _read_browser_daemon_pid(task_socket_dir, session_name)
        if daemon_pid is None:  # corrupt pid file
            _bt.logger.debug("Could not kill timed-out browser daemon for %s", session_name)
            return
        if not _lifecycle._verify_reapable_browser_daemon(daemon_pid, task_socket_dir, session_name):
            return
        try:
            # Tree-kill: terminating only the daemon PID leaks the Chromium tree.
            # See #68139.
            from agent import deadline as _deadline

            _deadline.kill_process_tree(daemon_pid)
        except (ProcessLookupError, PermissionError, OSError):
            _bt.logger.debug("Could not kill timed-out browser daemon for %s", session_name)
            return
    shutil.rmtree(task_socket_dir, ignore_errors=True)


def _read_browser_daemon_pid(task_socket_dir: str, session_name: str) -> Optional[int]:
    """Read the agent-browser daemon PID for a session (best-effort)."""
    pid_file = os.path.join(task_socket_dir, f"{session_name}.pid")
    try:
        return int(Path(pid_file).read_text(encoding="utf-8").strip())
    except (OSError, ValueError):
        return None


def _browser_daemon_responsive(task_socket_dir: str, probe_timeout_s: float = 1.0) -> bool:
    """Cheap liveness probe: a connect to the daemon's unix control socket proves the accept
    loop is alive (the command wedged page/CDP-side). Windows named pipes can't be probed →
    report unresponsive (tree-kill + respawn is the safe recovery)."""
    if os.name == "nt":
        return False
    import socket as socket_mod

    if not hasattr(socket_mod, "AF_UNIX"):
        return False
    try:
        entries = os.listdir(task_socket_dir)
    except OSError:
        return False
    for entry in (e for e in entries if e.endswith(".sock")):
        try:
            with socket_mod.socket(socket_mod.AF_UNIX, socket_mod.SOCK_STREAM) as s:
                s.settimeout(probe_timeout_s)
                s.connect(os.path.join(task_socket_dir, entry))
                return True
        except OSError:
            continue
    return False


def _handle_browser_command_timeout(task_id: str, session_info: Dict[str, Any], task_socket_dir: str) -> None:
    """Recover session state after a command timeout.

    Cloud/CDP: no daemon to probe — replace the stuck client generation now (same
    ``bb_session_id`` so cloud cleanup works). Local daemon alive (PID live, verified,
    socket accepts): only the command wedged — mark suspect, recycle at next use.
    Local daemon wedged/dead: tree-kill and evict now (Chromium children would leak).
    Both local branches ``mark_suspect`` first so the poisoned-cache invariant holds
    even if eviction races another thread's replacement.

    See #68139, #72205.
    * **Local daemon alive** (PID readable, process alive, identity-verified as ours, control socket accepts
    a connection): the *command* wedged — page hang, stuck navigation — but the daemon itself is fine.
    Killing it would be overkill and slow. Mark the session suspect only; the next use recycles it through
    ``ensure_healthy`` → clean agent-browser ``close`` → fresh session. Tree-kill the daemon's process tree
    via ``agent.deadline.kill_process_tree`` and evict the cache entry now; the next browser call respawns
    from scratch. See #72206.
    """
    if session_info.get("bb_session_id") or session_info.get("cdp_url"):
        _discard_timed_out_browser_session(task_id, session_info, task_socket_dir)
        return

    _bt._browser_session_backend(task_id).mark_suspect("browser command timed out; session may be poisoned")

    session_name = str(session_info.get("session_name") or "")
    daemon_pid = _read_browser_daemon_pid(task_socket_dir, session_name) if session_name else None
    daemon_alive = (
        daemon_pid is not None
        and _lifecycle._pid_exists(daemon_pid)
        and _lifecycle._verify_reapable_browser_daemon(daemon_pid, task_socket_dir, session_name)
        and _browser_daemon_responsive(task_socket_dir)
    )
    if daemon_alive:
        _bt.logger.warning("browser daemon for %s is alive after command timeout; session "
                           "marked suspect and will be recycled at next use", task_id)
        return

    _bt.logger.warning("browser daemon for %s is wedged or dead after command timeout; "
                       "tree-killing and evicting the session", task_id)
    _discard_timed_out_browser_session(task_id, session_info, task_socket_dir)
    # The poisoned entry is gone either way; the flag must not poison a session
    # created later under the same key.
    _bt._suspect_browser_sessions.pop(task_id, None)


def _interpret_browser_command_output(command: str, stdout: str, stderr: str, returncode: int) -> Dict[str, Any]:
    """Finished agent-browser process output → result dict. Empty stdout with rc=0 is a
    broken state (stale daemon) reported as failure except for ``_EMPTY_OK_COMMANDS``;
    non-JSON output is an error except ``screenshot``, whose path is recovered from prose."""
    if stderr and stderr.strip():
        level = logging.WARNING if returncode != 0 else logging.DEBUG
        _bt.logger.log(level, "browser '%s' stderr: %s", command, stderr.strip()[:500])

    stdout_text = stdout.strip()
    if not stdout_text:
        if returncode != 0:
            error_msg = stderr.strip() if stderr else f"Command failed with code {returncode}"
            _bt.logger.warning("browser '%s' failed (rc=%s): %s", command, returncode, error_msg[:300])
            return {"success": False, "error": error_msg}
        if command not in _bt._EMPTY_OK_COMMANDS:
            _bt.logger.warning("browser '%s' returned empty output (rc=0)", command)
            return {"success": False, "error": f"Browser command '{command}' returned no output"}
        return {"success": True, "data": {}}

    try:
        parsed = json.loads(stdout_text)
    except json.JSONDecodeError:
        raw = stdout_text[:2000]
        _bt.logger.warning("browser '%s' returned non-JSON output (rc=%s): %s", command, returncode, raw[:500])
        if command == "screenshot":
            combined_text = "\n".join(part for part in [stdout_text, (stderr or "").strip()] if part)
            recovered_path = _snapshot._extract_screenshot_path_from_text(combined_text)
            if recovered_path and Path(recovered_path).exists():
                _bt.logger.info("browser 'screenshot' recovered file from non-JSON output: %s", recovered_path)
                return {"success": True, "data": {"path": recovered_path, "raw": raw}}
        return {"success": False, "error": f"Non-JSON output from agent-browser for '{command}': {raw}"}

    # Empty snapshot content is a common sign of daemon/CDP issues.
    if command == "snapshot" and parsed.get("success"):
        snap_data = parsed.get("data", {})
        if not snap_data.get("snapshot") and not snap_data.get("refs"):
            _bt.logger.warning("snapshot returned empty content. Possible stale daemon or CDP connection issue. "
                               "returncode=%s", returncode)
    return parsed


def _browser_command_preflight() -> Dict[str, Any]:
    """Fail fast before spawning (missing CLI, Termux gap, interrupt, no Chromium in local
    mode — else every call hangs for command_timeout). Error result, or ``{"browser_cmd": path}``."""
    try:
        browser_cmd = _install._find_agent_browser()
    except FileNotFoundError as e:
        _bt.logger.warning("agent-browser CLI not found: %s", e)
        return {"success": False, "error": str(e)}

    if _install._requires_real_termux_browser_install(browser_cmd):
        error = _install._termux_browser_install_error()
        _bt.logger.warning("browser command blocked on Termux: %s", error)
        return {"success": False, "error": error}

    # Skip when engine=lightpanda — LP doesn't need Chromium for navigation.
    if (
        _cloud._is_local_mode()
        and not _install._chromium_installed()
        and _cloud._get_browser_engine() != "lightpanda"
        and not _install._maybe_autoinstall_chromium()
    ):
        hint = _CHROMIUM_MISSING_DOCKER_HINT if _install._running_in_docker() else _CHROMIUM_MISSING_HINT
        _bt.logger.warning("browser command blocked: %s", hint)
        return {"success": False, "error": hint}

    from tools.interrupt import is_interrupted
    if is_interrupted():
        return {"success": False, "error": "Interrupted"}
    return {"browser_cmd": browser_cmd}


def _spawn_and_collect(
    task_id: str, session_info: Dict[str, Any], cmd_parts: List[str],
    command: str, engine: str, timeout: int,
) -> Dict[str, Any]:
    """Run the prepared agent-browser argv once and interpret its output (handles timeout)."""
    task_socket_dir = _prepare_session_socket_dir(session_info["session_name"])
    _bt.logger.debug("browser cmd=%s task=%s socket_dir=%s (%d chars)",
                 command, task_id, task_socket_dir, len(task_socket_dir))
    browser_env = _agent_browser_command_env(task_socket_dir)

    # Lightpanda rejects Chromium-only launch flags: strip current and legacy vars;
    # Chrome commands and fallback use the shared Chromium policy.
    if engine == "lightpanda":
        stripped = [browser_env.pop(k, None) for k in ("AGENT_BROWSER_ARGS", "AGENT_BROWSER_CHROME_FLAGS")]
        if any(v is not None for v in stripped):
            _bt.logger.debug("browser: stripped Chromium-only AGENT_BROWSER_ARGS/AGENT_BROWSER_CHROME_FLAGS "
                             "for Lightpanda command %s (agent-browser rejects them with --engine lightpanda)",
                             command)
    else:
        _apply_chromium_sandbox_args(browser_env)

    stdout_path = os.path.join(task_socket_dir, f"_stdout_{command}")
    stderr_path = os.path.join(task_socket_dir, f"_stderr_{command}")
    proc = _popen_agent_browser(cmd_parts, browser_env, task_socket_dir, command)
    dock_home = _dock_cli_home(task_id, session_info)
    if dock_home:
        # PID-only: this CLI shares Hermes' process group. Do not killpg.
        register_inflight_dock_cli(proc, dock_home)

    try:
        try:
            proc.wait(timeout=timeout)
        except subprocess.TimeoutExpired:
            proc.kill()
            proc.wait()
            stdout, stderr = _read_command_output_files(stdout_path, stderr_path)
            _unlink_command_output_files(stdout_path, stderr_path)
            _handle_browser_command_timeout(task_id, session_info, task_socket_dir)
            if stderr and stderr.strip():
                _bt.logger.warning("browser '%s' stderr after timeout: %s", command, stderr.strip()[:500])
            _bt.logger.warning("browser '%s' timed out after %ds (task=%s, socket_dir=%s)",
                           command, timeout, task_id, task_socket_dir)
            return {"success": False, "error": _format_browser_timeout_error(command, timeout, stdout, stderr)}
        with open(stdout_path, "r", encoding="utf-8") as f:
            stdout = f.read()
        with open(stderr_path, "r", encoding="utf-8") as f:
            stderr = f.read()
        _unlink_command_output_files(stdout_path, stderr_path)
        return _interpret_browser_command_output(command, stdout, stderr, proc.returncode)
    finally:
        if dock_home:
            unregister_inflight_dock_cli(proc)


def _run_browser_command(
    task_id: str,
    command: str,
    args: List[str] = None,
    timeout: Optional[int] = None,
    _engine_override: Optional[str] = None,
) -> Dict[str, Any]:
    """Run one agent-browser CLI command against the task's session; returns its parsed JSON.
    ``timeout=None`` reads ``browser.command_timeout``; ``_engine_override`` forces an engine
    for this call only (Lightpanda fallback retries with Chrome without touching global state)."""
    if timeout is None:
        timeout = _bt._safe_command_timeout()
    args = args or []

    preflight = _browser_command_preflight()
    if "browser_cmd" not in preflight:
        return preflight
    browser_cmd = preflight["browser_cmd"]

    # Owner scope so admit / epoch / leftover identity read the session's
    # lease after a multiplex turn resets the process home to launch
    # (finding 101). ``record stop`` stays unfenced inside that home.
    with _lifecycle._session_owner_scope(task_id):
        return _run_browser_command_fenced(
            task_id, command, args, timeout, _engine_override, browser_cmd,
        )


def _run_browser_command_fenced(
    task_id: str,
    command: str,
    args: List[str],
    timeout: int,
    _engine_override: Optional[str],
    browser_cmd,
) -> Dict[str, Any]:
    try:
        session_info = _get_session_info(task_id)
    except Exception as e:
        from tools.bot_desktop.lease import HumanHasControl

        if isinstance(e, HumanHasControl):
            # No session row exists to drive. ``record stop`` has nothing to
            # cease — do not mint or attach to the dock just to no-op.
            if command == "record" and args and str(args[0]).strip() == "stop":
                return {"success": True, "data": {"stopped": False}}
            try:
                _bt._maybe_stop_recording(task_id)
            except Exception:
                pass
            return {"success": False, "error": str(e), "code": "human_has_control"}
        _bt.logger.warning("Failed to create browser session for task=%s: %s", task_id, e)
        return {"success": False, "error": f"Failed to create browser session: {str(e)}"}
    # The bot's LOCAL browser lives on its Bot Desktop screen, in the same profile a human who took
    # over is typing into. While the human holds the lease every action AND read against it is
    # refused (the page may show their credential); the fence brackets the whole run so a takeover
    # mid-command also voids the result. Cloud / user-supplied CDP sessions are a different browser.
    if _shares_bot_desktop_browser(session_info):
        from tools.bot_desktop import lease as _bd_lease
        try:
            admitted = _stamp_admitted(_bd_lease.assert_agent_may_act())
        except _bd_lease.HumanHasControl as e:
            # ``record stop`` ceases observation — it must work while the human
            # holds, or an opted-in WebM keeps capturing the credential they type.
            # ``close`` stays refused: that tree-kills the shared Chromium.
            if command == "record" and args and str(args[0]).strip() == "stop":
                return _run_browser_command_unfenced(
                    task_id, command, args, timeout, _engine_override, browser_cmd, session_info)
            try:
                _bt._maybe_stop_recording(task_id)
            except Exception:
                pass
            return {"success": False, "error": str(e), "code": "human_has_control"}
        result = _run_browser_command_unfenced(task_id, command, args, timeout, _engine_override, browser_cmd, session_info)
        moved = _lease_moved_result(admitted)
        if moved:
            return moved
        return result
    return _run_browser_command_unfenced(task_id, command, args, timeout, _engine_override, browser_cmd, session_info)


def _this_machine_cdp_hostnames() -> set[str]:
    """Short + FQDN-looking names this process reports for itself.

    Leftover identity must not call ``getfqdn()`` (DNS + rebinding).
    ``os.uname().nodename`` can differ from ``gethostname()`` on the same box.
    """
    names: set[str] = set()
    candidates: list[str] = []
    try:
        candidates.append(socket.gethostname())
    except OSError:
        pass
    try:
        nodename = os.uname().nodename
        if nodename:
            candidates.append(nodename)
    except (AttributeError, OSError):
        pass
    for raw in candidates:
        text = (raw or "").strip().lower().strip("[]").rstrip(".")
        if not text:
            continue
        names.add(text)
        short = text.split(".", 1)[0]
        if short:
            names.add(short)
    return names


def _is_this_machine_hostname(host: str) -> bool:
    """True when ``host`` is this process's hostname (or its short name).

    Chromium on Debian often advertises ``hostname`` / ``hostname.localdomain``
    in ``webSocketDebuggerUrl``. An arbitrary other hostname with the same
    port is another Chrome and must not be compared to this profile's dock.
    """
    text = (host or "").strip().lower().strip("[]").rstrip(".")
    if not text:
        return False
    ours = _this_machine_cdp_hostnames()
    if not ours:
        return False
    if text in ours:
        return True
    return text.split(".", 1)[0] in ours


def _hosts_file_paths() -> list[str]:
    """Local hosts files only. Do not resolve DNS."""
    if os.name == "nt":
        windir = os.environ.get("SystemRoot") or os.environ.get("WINDIR") or r"C:\Windows"
        return [os.path.join(windir, "System32", "drivers", "etc", "hosts")]
    return ["/etc/hosts"]


def _ip_is_this_machine_loopback(addr) -> bool:
    if addr.is_loopback or addr.is_unspecified:
        return True
    mapped = getattr(addr, "ipv4_mapped", None)
    return bool(mapped and (mapped.is_loopback or mapped.is_unspecified))


# Last-part width for 1..4 dotted decimal IPv4 pieces (WHATWG).
_IPV4_LAST_PART_LIMIT = (0xFFFFFFFF, 0xFFFFFF, 0xFFFF, 0xFF)


def _parse_decimal_ipv4(text: str):
    """WHATWG decimal IPv4, including dotted shorthand. No octal / hex.

    Chromium and leftover CLIs accept ``http://127.1:9333`` /
    ``http://0:9333`` as 127.0.0.1 / 0.0.0.0. ``ipaddress`` does not, so
    leftover identity treated those as another Chrome (admit None) on
    the jar a human is typing into. LAN shorthand (``10.1``) stays LAN.
    """
    raw = (text or "").strip()
    if not raw:
        return None
    parts = raw.split(".")
    n = len(parts)
    if n < 1 or n > 4:
        return None
    values: list[int] = []
    for i, part in enumerate(parts):
        if not part or not part.isascii() or not part.isdigit():
            return None
        try:
            value = int(part, 10)
        except ValueError:
            return None
        limit = 0xFF if i < n - 1 else _IPV4_LAST_PART_LIMIT[n - 1]
        if value > limit:
            return None
        values.append(value)
    if n == 1:
        packed = values[0]
    elif n == 2:
        packed = (values[0] << 24) | values[1]
    elif n == 3:
        packed = (values[0] << 24) | (values[1] << 16) | values[2]
    else:
        packed = (values[0] << 24) | (values[1] << 16) | (values[2] << 8) | values[3]
    return ipaddress.IPv4Address(packed)


def _parse_cdp_ip(text: str):
    """Parse a CDP host as an IP, including WHATWG IPv4 shorthand.

    ``ipaddress`` first (127/8, ``::1``, ``::ffff:127.0.0.1``). Then
    dotted / 32-bit decimal shorthand and IPv4-mapped ``::ffff:127.1``.
    Hostnames stay ``None`` — no DNS.
    """
    raw = (text or "").strip()
    if not raw:
        return None
    try:
        return ipaddress.ip_address(raw)
    except ValueError:
        pass
    mapped_prefix = "::ffff:"
    if raw.lower().startswith(mapped_prefix):
        v4 = _parse_decimal_ipv4(raw[len(mapped_prefix):])
        if v4 is None:
            return None
        try:
            return ipaddress.IPv6Address(f"::ffff:{v4}")
        except ValueError:
            return None
    return _parse_decimal_ipv4(raw)


def _parse_loopback_hosts_text(text: str) -> set[str]:
    """Names whose hosts-file address is this machine's loopback.

    LAN / remote mappings stay another browser. No DNS.
    """
    names: set[str] = set()
    for raw_line in (text or "").splitlines():
        line = raw_line.split("#", 1)[0].strip()
        if not line:
            continue
        parts = line.split()
        if len(parts) < 2:
            continue
        try:
            addr = ipaddress.ip_address(parts[0].strip().strip("[]"))
        except ValueError:
            continue
        if not _ip_is_this_machine_loopback(addr):
            continue
        for raw_name in parts[1:]:
            name = (raw_name or "").strip().lower().strip("[]").rstrip(".")
            if not name:
                continue
            names.add(name)
            short = name.split(".", 1)[0]
            if short:
                names.add(short)
    return names


_hosts_loopback_names_cache: Optional[tuple] = None


def _reset_loopback_hosts_cache_for_tests() -> None:
    global _hosts_loopback_names_cache
    _hosts_loopback_names_cache = None


def _loopback_hosts_file_names() -> set[str]:
    """Loopback aliases from the local hosts file, cached by mtime/size."""
    global _hosts_loopback_names_cache
    paths = _hosts_file_paths()
    stamp = []
    for path in paths:
        try:
            st = os.stat(path)
            stamp.append((path, st.st_mtime_ns, st.st_size))
        except OSError:
            stamp.append((path, None, None))
    stamp_t = tuple(stamp)
    cached = _hosts_loopback_names_cache
    if cached and cached[0] == stamp_t:
        return set(cached[1])
    names: set[str] = set()
    for path in paths:
        try:
            with open(path, encoding="utf-8", errors="replace") as fh:
                names.update(_parse_loopback_hosts_text(fh.read()))
        except OSError:
            continue
    _hosts_loopback_names_cache = (stamp_t, frozenset(names))
    return names


def _is_loopback_hosts_alias(host: str) -> bool:
    """True when ``host`` is a local hosts-file loopback alias.

    ``/browser connect http://dock-chrome:9333`` is this machine when
    ``/etc/hosts`` maps that name to 127/8 — not only when the name equals
    ``gethostname()``. A hosts name mapped to a LAN IP is another Chrome.
    """
    text = (host or "").strip().lower().strip("[]").rstrip(".")
    if not text:
        return False
    ours = _loopback_hosts_file_names()
    if not ours:
        return False
    if text in ours:
        return True
    return text.split(".", 1)[0] in ours


def _percent_decode_cdp_host(host: str) -> str:
    """WHATWG percent-decode a CDP host once. No DNS.

    Node leftover (chrome-devtools-mcp, playwright, lighthouse,
    agent-browser) uses ``new URL()``, which turns
    ``http://127%2e1:9333`` into 127.0.0.1. ``urllib.parse`` does not,
    so persist could not match and leftover attach was another Chrome
    (admit None) on the jar a human is typing into. Decode once only
    — ``%2531`` stays ``%31``. LAN ``10%2e1`` stays LAN after decode.
    """
    from urllib.parse import unquote
    return unquote(host or "")


def _is_loopback_cdp_host(host: str) -> bool:
    """True for this machine's CDP hosts, including 127/8, IPv4-mapped, and hostname.

    A closed hostname set missed Debian's ``127.0.1.1``, ``::ffff:127.0.0.1``,
    this process's own hostname, and extra ``/etc/hosts`` loopback aliases
    (``127.0.0.1 dock-chrome``). WHATWG / Chromium dotted shorthand
    (``127.1``, ``127.0.1``, ``0``, ``2130706433``) is the same miss:
    leftover ``--cdp-url http://127.1:9333`` never extracted a port, so
    persist could not match and attach was another Chrome (admit None)
    on the jar a human is typing into. Percent-encoded dots / digits
    (``127%2e1``, ``%31%32%37.0.0.1``) are the Node leftover twin —
    ``new URL()`` decodes them, ``urllib.parse`` does not. Remote /
    LAN / other hostnames stay another browser. Do not resolve DNS
    here: leftover identity must stay a local parse.
    """
    text = _percent_decode_cdp_host(host).strip().lower().strip("[]").rstrip(".")
    if not text:
        return False
    if (
        text in {"localhost", "localhost.localdomain", "ip6-localhost", "ip6-loopback"}
        or text.endswith(".localhost")
    ):
        return True
    if _is_this_machine_hostname(text) or _is_loopback_hosts_alias(text):
        return True
    addr = _parse_cdp_ip(text)
    if addr is None:
        return False
    return _ip_is_this_machine_loopback(addr)


def _cdp_url_host_port(url: str) -> Tuple[str, Optional[int]]:
    """Host + port from a CDP URL. Never raises on Chromium shorthand.

    ``urllib.parse`` validates bracketed hosts with ``ipaddress``, so
    ``ws://[::ffff:127.1]:9333`` — a mapped WHATWG form leftover CLIs
    can pass through — aborted leftover interrupt instead of fencing.
    """
    text = (url or "").strip()
    if not text:
        return "", None
    raw = text if "://" in text else f"http://{text}"
    from urllib.parse import urlparse
    try:
        parsed = urlparse(raw)
        return (parsed.hostname or "").lower(), parsed.port
    except ValueError:
        pass
    rest = raw.split("://", 1)[-1]
    netloc = rest.split("/", 1)[0].split("?", 1)[0].split("#", 1)[0]
    if netloc.startswith("["):
        end = netloc.find("]")
        if end < 0:
            return "", None
        host = netloc[1:end].lower()
        tail = netloc[end + 1:]
        if tail.startswith(":") and tail[1:].isdigit():
            return host, int(tail[1:])
        return host, None
    if "@" in netloc:
        netloc = netloc.rsplit("@", 1)[-1]
    host, sep, port_text = netloc.rpartition(":")
    if sep and port_text.isdigit() and host and ":" not in host:
        return host.lower(), int(port_text)
    return netloc.lower(), None


def _loopback_cdp_port(url: str) -> Optional[int]:
    """Port if ``url`` is this machine's CDP endpoint, else ``None``.

    Remote / cloud / other-hostname CDP hosts are another browser and must
    not be compared to this profile's dock Chromium.
    """
    text = (url or "").strip()
    if not text:
        return None
    if text.isdigit():
        port = int(text)
        return port if 1 <= port <= 65535 else None
    host, port = _cdp_url_host_port(text)
    if not _is_loopback_cdp_host(host):
        return None
    return port if port is not None and 1 <= port <= 65535 else None


# Last live dock DevTools port per profile. Take over can unlink
# DevToolsActivePort / SingletonLock while Chromium is still the jar the
# human is typing into; a live miss must not treat that remembered port as
# "another browser". A different loopback port stays another Chrome.
_last_dock_cdp_port: Dict[str, int] = {}


def _reset_dock_port_memory_for_tests() -> None:
    _last_dock_cdp_port.clear()
    _reset_loopback_hosts_cache_for_tests()
    _reset_inflight_dock_cli_for_tests()
    _reset_reserved_dock_harness_for_tests()
    try:
        from tools.bot_desktop import browser as _bd_browser
        _bd_browser._dock_port_path().unlink(missing_ok=True)
    except OSError:
        pass


_inflight_dock_cli_lock = threading.Lock()
_inflight_dock_cli: list[dict] = []


def _dock_cli_home(task_id: str, session_info: Dict[str, Any]) -> Optional[str]:
    """HERMES_HOME of a leftover CLI aimed at this profile's dock Chromium, else None."""
    if not _shares_bot_desktop_browser(session_info):
        return None
    owner = _bt._session_owner_homes.get(task_id)
    if owner:
        return str(owner)
    from hermes_constants import get_hermes_home
    return str(get_hermes_home())


def register_inflight_dock_cli(
    proc,
    home: Optional[str] = None,
    kill: Optional[Callable] = None,
) -> None:
    """Track a leftover writer so Take over can drop it without waiting it out.

    Agent-browser CLI is in Hermes' process group — ``kill`` must be PID-only
    (default ``Popen.kill``). browser_exec uses ``start_new_session`` so its
    killer may ``killpg`` that group; dock Chromium is not in it.
    """
    from hermes_constants import hermes_home_key
    from tools.browser_tool_supervisor_lease import install_supervisor_lease_hook

    install_supervisor_lease_hook()
    owner = str(home) if home else ""
    with _inflight_dock_cli_lock:
        _inflight_dock_cli.append({
            "proc": proc,
            "home": owner or None,
            "home_key": hermes_home_key(owner or None),
            "kill": kill,
        })


def unregister_inflight_dock_cli(proc) -> None:
    with _inflight_dock_cli_lock:
        _inflight_dock_cli[:] = [e for e in _inflight_dock_cli if e.get("proc") is not proc]


def _reset_inflight_dock_cli_for_tests() -> None:
    with _inflight_dock_cli_lock:
        _inflight_dock_cli.clear()


def interrupt_reserved_browser_cli(home: Optional[str] = None) -> None:
    """Kill leftover agent-browser / browser_exec writers aimed at a human-held dock.

    ``_run_browser_command`` and ``browser_exec`` only discard the result after
    the CLI finishes — leftover ``fill`` / Playwright ``Input.dispatchKeyEvent``
    still land in the field the human is typing into. Same class as leftover
    CDP ``ws.send`` and leftover ``computer_use`` ``type_text``.
    """
    from hermes_constants import hermes_home_key, reset_hermes_home_override, set_hermes_home_override
    from tools.bot_desktop import lease as _bd_lease

    want = hermes_home_key(home) if home is not None else None
    victims: list[dict] = []
    with _inflight_dock_cli_lock:
        kept: list[dict] = []
        for entry in _inflight_dock_cli:
            owner_key = entry.get("home_key") or hermes_home_key()
            if want is not None and owner_key != want:
                kept.append(entry)
                continue
            owner = entry.get("home")
            token = None
            try:
                if owner:
                    token = set_hermes_home_override(owner)
                held = _bd_lease.human_holds()
            finally:
                if token is not None:
                    reset_hermes_home_override(token)
            if not held:
                kept.append(entry)
                continue
            victims.append(entry)
        _inflight_dock_cli[:] = kept
    for entry in victims:
        proc = entry.get("proc")
        killer = entry.get("kill")
        try:
            if callable(killer):
                killer(proc)
            elif proc is not None and getattr(proc, "poll", lambda: None)() is None:
                proc.kill()
        except Exception:
            _bt.logger.debug("reserved browser CLI interrupt failed", exc_info=True)


_NPX_LAUNCHERS = frozenset({"npx", "pnpx", "bunx"})
_PACKAGE_EXEC_LAUNCHERS = frozenset({"npm", "pnpm", "yarn"})
_PACKAGE_EXEC_SUBCOMMANDS = frozenset({"exec", "dlx"})
_BUN_LAUNCHERS = frozenset({"bun"})
_BUN_X_SUBCOMMANDS = frozenset({"x", "exec"})
_UVX_LAUNCHERS = frozenset({"uvx", "uv"})
_NODE_LAUNCHERS = frozenset({"node", "nodejs", "iojs"})
_ENV_LAUNCHERS = frozenset({"env"})
_COREPACK_LAUNCHERS = frozenset({"corepack"})
_AGENT_BROWSER_NODE_ENTRYPOINTS = frozenset({
    "agent-browser", "cli.js", "cli.mjs", "cli.cjs", "index.js",
})
_PLAYWRIGHT_NODE_ENTRYPOINTS = frozenset({
    "playwright", "cli.js", "cli.mjs", "cli.cjs",
})
_PLAYWRIGHT_MCP_NODE_ENTRYPOINTS = frozenset({
    "mcp", "cli.js", "cli.mjs", "cli.cjs", "index.js",
})
_LIGHTHOUSE_NODE_ENTRYPOINTS = frozenset({
    "lighthouse", "lighthouse.js", "lighthouse-cli.js",
    "cli.js", "cli.mjs", "cli.cjs", "index.js",
})
_YARN_NODE_ENTRYPOINTS = frozenset({
    "yarn", "yarn.js", "yarn.cjs", "yarn.mjs",
    "cli.js", "cli.cjs", "cli.mjs",
})
_NPX_NODE_ENTRYPOINTS = frozenset({
    "npx", "npx.js", "npx.cjs", "npx-cli.js", "npx-cli.cjs",
})
_NPM_NODE_ENTRYPOINTS = frozenset({
    "npm", "npm.js", "npm.cjs", "npm-cli.js", "npm-cli.cjs",
})
_PNPM_NODE_ENTRYPOINTS = frozenset({
    "pnpm", "pnpm.js", "pnpm.cjs", "pnpm.mjs",
    "cli.js", "cli.cjs", "cli.mjs",
})
_COREPACK_NODE_ENTRYPOINTS = frozenset({
    "corepack", "corepack.js", "corepack.cjs", "corepack.mjs",
})
_COREPACK_YARN_SHIMS = frozenset({
    "yarn.js", "yarn.cjs", "yarn.mjs", "yarnpkg.js", "yarnpkg.cjs",
})
_COREPACK_PNPM_SHIMS = frozenset({"pnpm.js", "pnpm.cjs", "pnpm.mjs"})
_COREPACK_NPM_SHIMS = frozenset({"npm.js", "npm.cjs"})
_COREPACK_NPX_SHIMS = frozenset({
    "pnpx.js", "pnpx.cjs", "pnpx.mjs",
})
_PLAYWRIGHT_CDP_ENV = (
    "PW_TEST_CONNECT_WS_ENDPOINT",
    "PLAYWRIGHT_WS_ENDPOINT",
    "PLAYWRIGHT_MCP_CDP_ENDPOINT",
    "BROWSER_CDP_URL",
)


def _token_basename_is(token: str, name: str) -> bool:
    """True when this argv token *is* ``name`` (binary or package@ver).

    Token-match only — never ``name in cmdline``. A path substring such as
    ``cat agent-browser.log`` or ``chrome --user-data-dir=…/agent-browser``
    is not an invocation.
    """
    raw = (token or "").strip().strip("\"'")
    if not raw or not name:
        return False
    pkg = Path(raw).name.split("@", 1)[0]
    lower = pkg.lower()
    if lower.endswith((".exe", ".cmd", ".bat")):
        lower = Path(lower).stem.lower()
    return lower == name.lower()


def _token_basename_is_agent_browser(token: str) -> bool:
    return _token_basename_is(token, "agent-browser")


def _is_python_launcher(name0: str) -> bool:
    return name0 == "python" or name0.startswith("python3")


def _token_is_agent_browser_script(token: str) -> bool:
    """True when this token is the agent-browser binary or its Node entry.

    Linux shebang rewrites argv0 to ``node`` and the script path. A package
    path ``…/agent-browser/dist/cli.js`` is an invocation; ``cli.js`` outside
    that package and ``--user-data-dir=…/agent-browser`` are not.
    """
    if _token_basename_is_agent_browser(token):
        return True
    raw = (token or "").strip().strip("\"'")
    if not raw:
        return False
    path = Path(raw)
    parts = [p.lower() for p in path.parts]
    if "agent-browser" not in parts:
        return False
    return path.name.lower() in _AGENT_BROWSER_NODE_ENTRYPOINTS


def _env_command_tokens(tokens: List[str]) -> List[str]:
    """Argv after ``env`` [assignments] [flags], or empty."""
    i = 1
    while i < len(tokens):
        raw = str(tokens[i])
        if raw == "--":
            return [str(t) for t in tokens[i + 1:]]
        if raw.startswith("-"):
            if raw in {"-u", "--unset"}:
                i += 2
                continue
            i += 1
            continue
        if "=" in raw and not raw.startswith("="):
            i += 1
            continue
        return [str(t) for t in tokens[i:]]
    return []


def _corepack_command_tokens(tokens: List[str]) -> List[str]:
    """Argv after ``corepack`` [flags], or empty.

    ``corepack pnpm exec lighthouse --port 9333`` is the leftover writer
    finding 91's ``pnpm exec`` match misses. ``corepack enable`` /
    ``corepack use`` are not leftover invocations. Keep the child's
    flags (``--port``, ``--browserUrl``) — do not strip them as
    corepack's own.
    """
    if not tokens or _launcher_basename(tokens[0]) not in _COREPACK_LAUNCHERS:
        return []
    i = 1
    while i < len(tokens):
        raw = str(tokens[i])
        if raw == "--":
            return [str(t) for t in tokens[i + 1:]]
        if raw.startswith("-"):
            i += 1
            continue
        return [str(t) for t in tokens[i:]]
    return []


def _launcher_basename(token: str) -> str:
    name0 = Path((token or "").strip().strip("\"'")).name.lower()
    if name0.endswith((".exe", ".cmd", ".bat")):
        name0 = Path(name0).stem.lower()
    return name0


_LAUNCHER_VALUE_FLAGS = frozenset({"--from"})
# agent-browser globals that take a value before ``connect <port|url>``.
# ``--session foo connect 9333`` must see ``connect``, not ``foo``.
_AGENT_BROWSER_VALUE_FLAGS = frozenset({
    "--session", "--profile", "--cdp", "--cdp-endpoint",
    "-p", "--headers", "--executable-path", "--args",
    "--user-agent", "--proxy", "--proxy-bypass", "--name", "-n",
    "--color-scheme",
})
# Global options that take a path / selector *before* exec|dlx|x.
# Space-separated values used to become the "command" (``pnpm --dir /tmp
# exec lighthouse`` → rest[0] == ``/tmp``), so leftover unwrap missed
# every workspace-dir writer. Equals form (``--dir=/tmp``) already
# skipped as a flag. Do not put ``-p`` here — that is npm's package pin.
_PACKAGE_EXEC_VALUE_FLAGS = frozenset({
    "--dir", "-C", "--prefix", "--cwd", "--filter", "--workspace", "-w",
})
_BUN_VALUE_FLAGS = frozenset({"--cwd"})
# npx / pnpx / bunx. ``--prefix /tmp lighthouse`` used to see ``/tmp`` as
# the package (finding 103). ``--workspace web`` is the same shape
# (finding 104). Do not put ``-p`` / ``--package`` here — ``npx -p
# lighthouse --port 9333`` (no binary repeat) matches the pin. Do not
# put ``-w`` here globally — pnpx ``-w`` is boolean ``--workspace-root``
# and would swallow the binary. ``npx -w`` is npm's workspace pin and
# is added only for argv0 ``npx``.
_NPX_VALUE_FLAGS = frozenset({"--prefix", "--cwd", "--workspace"})


def _npx_package_tokens(tokens: List[str]) -> List[str]:
    """Operands after ``npx`` / ``pnpx`` / ``bunx``, prefix/cwd/workspace skipped."""
    flags = _NPX_VALUE_FLAGS
    if tokens and _launcher_basename(tokens[0]) == "npx":
        flags = _NPX_VALUE_FLAGS | {"-w"}
    return _first_non_flag_tokens(tokens, value_flags=flags)


_NPX_PACKAGE_PIN_FLAGS = frozenset({"--package", "-p"})


def _npx_launcher_value_flags(tokens: List[str]) -> frozenset:
    """Value flags for peeling npx argv. Includes ``--package`` / ``-p``.

    Do not feed these into ``_npx_package_tokens`` — consuming ``-p``
    there would miss ``npx -p lighthouse --port`` (no binary repeat).
    """
    flags = set(_NPX_VALUE_FLAGS) | set(_NPX_PACKAGE_PIN_FLAGS)
    if tokens and _launcher_basename(tokens[0]) == "npx":
        flags.add("-w")
    return frozenset(flags)


def _npx_package_pins(tokens: List[str]) -> List[str]:
    """``--package=foo`` / ``-p foo`` pins before ``--``, in order."""
    if not tokens or _launcher_basename(tokens[0]) not in _NPX_LAUNCHERS:
        return []
    pins: List[str] = []
    i = 1
    while i < len(tokens):
        raw = str(tokens[i]) if tokens[i] is not None else ""
        if raw == "--":
            break
        if raw in _NPX_PACKAGE_PIN_FLAGS and i + 1 < len(tokens):
            nxt = str(tokens[i + 1])
            if not nxt.startswith("-"):
                pins.append(nxt)
            i += 2
            continue
        if raw.startswith("--package="):
            val = raw.split("=", 1)[1]
            if val:
                pins.append(val)
            i += 1
            continue
        i += 1
    return pins


def _npx_invocation_matches(tokens: List[str], match_token) -> bool:
    """True when the npx operand or last ``--package`` / ``-p`` pin matches.

    ``npx --package=lighthouse -- --port <dock>`` has no leftover binary
    operand (finding 105). Last-wins: a later ``--package=ruff`` is not
    lighthouse. Do not consume ``-p`` as a value flag on the operand
    walk — ``npx -p lighthouse --port`` still matches via ``rest[0]``.
    """
    rest = _npx_package_tokens(tokens)
    if rest and match_token(rest[0]):
        return True
    pins = _npx_package_pins(tokens)
    return bool(pins) and match_token(pins[-1])


def _npx_child_argv(tokens: List[str]) -> Optional[List[str]]:
    """Child argv after npx's ``--``, or None when ``--`` is the child's.

    ``npx --package=lighthouse -- --port <dock>`` puts leftover flags
    after npx's ``--``. ``npx lighthouse --port <dock> -- url`` keeps
    ``--port`` *before* the child's terminator — peeling that ``--``
    would hide the aim. Only peel when no operand remains before
    ``--`` after skipping prefix/package pins.
    """
    if not tokens or _launcher_basename(tokens[0]) not in _NPX_LAUNCHERS:
        return None
    sep = None
    for i, tok in enumerate(tokens):
        if str(tok) == "--":
            sep = i
            break
    if sep is None:
        return None
    rest_before = _first_non_flag_tokens(
        list(tokens[:sep]), value_flags=_npx_launcher_value_flags(tokens),
    )
    if rest_before:
        return None
    return [str(t) for t in tokens[sep + 1:]]


def _first_non_flag_tokens(
    tokens: List[str],
    skip: int = 1,
    value_flags: Optional[frozenset] = None,
    reserved: Optional[frozenset] = None,
) -> List[str]:
    """Non-flag argv after the launcher, skipping values of known launcher flags.

    ``uvx --from browser-use==1 browser-use`` must see ``browser-use`` as the
    command, not the pin. ``npx -p agent-browser@latest agent-browser`` is
    the same shape. ``pnpm --dir /tmp exec lighthouse`` must see ``exec``,
    not the directory. A reserved next token (``exec`` / ``x``) is the
    subcommand, not a directory named ``exec``.
    """
    known = _LAUNCHER_VALUE_FLAGS if not value_flags else (_LAUNCHER_VALUE_FLAGS | value_flags)
    reserved_next = reserved or frozenset()
    out: List[str] = []
    expect_value = False
    for tok in tokens[skip:]:
        raw = str(tok) if tok is not None else ""
        if expect_value:
            expect_value = False
            if raw in reserved_next:
                out.append(raw)
            continue
        if not raw or raw.startswith("-"):
            key = raw.split("=", 1)[0]
            if key in known and "=" not in raw:
                expect_value = True
            continue
        out.append(raw)
    return out


def _package_exec_parts(
    tokens: List[str],
) -> Optional[Tuple[List[str], List[str]]]:
    """``npm|pnpm|yarn exec|dlx`` → ``(flag_packages, operands)``, else None.

    ``npx`` / ``pnpx`` / ``bunx`` already unwrap. ``pnpm exec`` / ``npm exec``
    / ``yarn dlx`` are the leftover writers those launchers miss. ``pnpm run``
    / ``npm install`` / ``yarn add`` are not invocations. ``npm x`` is
    ``npm exec``. ``--package`` / ``-p`` before a bare ``--`` are npm's
    package pins, not the child's ``-p`` port. ``--dir`` / ``-C`` /
    ``--prefix`` / ``--cwd`` / ``--filter`` / ``--workspace`` / ``-w``
    take a value — do not treat that selector as the command (findings
    102, 104).     ``yarn workspace <name> exec`` is the leftover writer
    ``yarn exec`` misses. ``yarn workspace <name> run`` /
    ``yarn workspaces foreach`` are not leftover exec.
    ``yarn npm exec`` is Yarn Berry's leftover writer ``npm exec``
    misses when argv0 is yarn (finding 106).
    """
    if not tokens:
        return None
    rewritten = _yarn_npm_as_npm_argv(tokens)
    if rewritten is not None:
        tokens = rewritten
    name0 = _launcher_basename(tokens[0])
    if name0 not in _PACKAGE_EXEC_LAUNCHERS:
        return None
    reserved = _PACKAGE_EXEC_SUBCOMMANDS | ({"x"} if name0 == "npm" else set())
    rest = _first_non_flag_tokens(
        tokens, value_flags=_PACKAGE_EXEC_VALUE_FLAGS, reserved=reserved,
    )
    if not rest:
        return None
    sub = rest[0]
    if name0 == "yarn" and sub == "workspace":
        if len(rest) < 3 or rest[2] not in _PACKAGE_EXEC_SUBCOMMANDS:
            return None
        operands = rest[3:]
    elif name0 == "npm" and sub == "x":
        operands = rest[1:]
    elif sub not in _PACKAGE_EXEC_SUBCOMMANDS:
        return None
    else:
        operands = rest[1:]
    if operands and operands[0] == "--":
        operands = operands[1:]
    flag_pkgs: List[str] = []
    i = 1
    while i < len(tokens):
        raw = str(tokens[i]) if tokens[i] is not None else ""
        if raw == "--":
            break
        if raw in {"--package", "-p"} and i + 1 < len(tokens):
            nxt = str(tokens[i + 1])
            if not nxt.startswith("-"):
                flag_pkgs.append(nxt)
            i += 2
            continue
        if raw.startswith("--package="):
            val = raw.split("=", 1)[1]
            if val:
                flag_pkgs.append(val)
            i += 1
            continue
        i += 1
    return (flag_pkgs, operands)


def _invocation_via_package_exec(tokens: List[str], matches) -> Optional[bool]:
    """None when argv is not npm/pnpm/yarn exec|dlx; else leftover-CLI match."""
    parts = _package_exec_parts(tokens)
    if parts is None:
        return None
    flag_pkgs, operands = parts
    if any(matches([pkg]) for pkg in flag_pkgs):
        return True
    return bool(operands) and bool(matches(operands))


def _bun_x_operands(tokens: List[str]) -> Optional[List[str]]:
    """``bun x|exec`` operands, else None.

    ``bunx`` already unwraps via ``_NPX_LAUNCHERS``. ``bun x lighthouse``
    and ``bun exec chrome-devtools-mcp`` are the leftover writers that
    miss. ``bun run`` / ``bun install`` / ``bun add`` are not invocations.
    """
    if not tokens or _launcher_basename(tokens[0]) not in _BUN_LAUNCHERS:
        return None
    rest = _first_non_flag_tokens(
        tokens, value_flags=_BUN_VALUE_FLAGS, reserved=_BUN_X_SUBCOMMANDS,
    )
    if not rest or rest[0] not in _BUN_X_SUBCOMMANDS:
        return None
    operands = rest[1:]
    if operands and operands[0] == "--":
        operands = operands[1:]
    return operands


def _invocation_via_bun_x(tokens: List[str], matches) -> Optional[bool]:
    """None when argv is not ``bun x|exec``; else leftover-CLI match."""
    operands = _bun_x_operands(tokens)
    if operands is None:
        return None
    return bool(operands) and bool(matches(operands))


def _package_manager_child_argv(tokens: List[str]) -> Optional[List[str]]:
    """Child argv after npm|pnpm|yarn exec|dlx or bun x|exec, flags kept.

    ``_package_exec_parts`` / ``_bun_x_operands`` drop ``--port`` /
    ``--browserUrl`` via ``_first_non_flag_tokens``. Flag parse needs
    those. ``pnpm run`` / ``bun install`` return None.
    """
    if not tokens:
        return None
    name0 = _launcher_basename(tokens[0])
    if name0 in _PACKAGE_EXEC_LAUNCHERS:
        return _package_exec_child_argv(tokens)
    if name0 in _BUN_LAUNCHERS:
        return _bun_x_child_argv(tokens)
    return None


def _yarn_npm_as_npm_argv(tokens: List[str]) -> Optional[List[str]]:
    """``yarn [flags] npm <rest>`` → ``[npm, <rest>]``, else None.

    Yarn Berry ``yarn npm exec lighthouse --port <dock>`` keeps argv0
    ``yarn``, so leftover unwrap never entered ``npm exec``. ``yarn npm
    install`` rewrites to ``npm install`` and stays unknown. ``yarn
    exec`` / ``yarn workspace`` are not this shape. Do not eat
    ``npm`` as a ``--cwd`` value.
    """
    if not tokens or _launcher_basename(tokens[0]) != "yarn":
        return None
    reserved = frozenset({"npm"})
    i = 1
    while i < len(tokens):
        raw = str(tokens[i]) if tokens[i] is not None else ""
        if raw == "--":
            rest = [str(t) for t in tokens[i + 1:]]
            if rest and _launcher_basename(rest[0]) == "npm":
                return rest
            return None
        skipped = _skip_manager_value_flag(
            tokens, i, raw, _PACKAGE_EXEC_VALUE_FLAGS, reserved,
        )
        if skipped is not None:
            i = skipped
            continue
        if raw.startswith("-"):
            i += 1
            continue
        if raw == "npm":
            return ["npm"] + [str(t) for t in tokens[i + 1:]]
        return None
    return None


def _is_package_exec_sub(name0: str, raw: str) -> bool:
    return (name0 == "npm" and raw == "x") or raw in _PACKAGE_EXEC_SUBCOMMANDS


def _skip_manager_value_flag(
    tokens: List[str],
    i: int,
    raw: str,
    value_flags: frozenset,
    reserved: frozenset,
) -> Optional[int]:
    """Advance past ``--dir /tmp`` / ``--cwd=/tmp``. None if ``raw`` is not one.

    Do not eat the subcommand as a value (``pnpm --dir exec lighthouse``).
    """
    key = raw.split("=", 1)[0]
    if key not in value_flags:
        return None
    if "=" in raw:
        return i + 1
    if i + 1 < len(tokens):
        nxt = str(tokens[i + 1]) if tokens[i + 1] is not None else ""
        if nxt and not nxt.startswith("-") and nxt not in reserved:
            return i + 2
    return i + 1


def _peel_exec_operand_separator(tokens: List[str]) -> List[str]:
    """``npm exec pkg -- --port`` drops the manager ``--``, not the child's.

    Finding 105 peels npx ``--`` only when no leftover operand remains.
    ``npm exec lighthouse -- --port <dock>`` / ``npm exec playwright-cli
    -- attach --cdp <dock>`` have an operand, so leftover flag parse
    stopped at the manager ``--`` and missed the aim (finding 109).
    The child does not see that ``--``. A later child ``--`` still ends
    flag parse.
    """
    if len(tokens) >= 2 and tokens[1] == "--":
        return [tokens[0]] + [str(t) for t in tokens[2:]]
    return tokens


def _package_exec_child_argv(tokens: List[str]) -> Optional[List[str]]:
    rewritten = _yarn_npm_as_npm_argv(tokens)
    if rewritten is not None:
        tokens = rewritten
    name0 = _launcher_basename(tokens[0])
    reserved = _PACKAGE_EXEC_SUBCOMMANDS | ({"x"} if name0 == "npm" else set())
    i = 1
    saw_sub = False
    skip_workspace_name = False
    while i < len(tokens):
        raw = str(tokens[i]) if tokens[i] is not None else ""
        if raw == "--":
            return [str(t) for t in tokens[i + 1:]] if saw_sub else None
        if raw in {"--package", "-p"} and i + 1 < len(tokens):
            nxt = str(tokens[i + 1])
            if not nxt.startswith("-"):
                i += 2
                continue
        skipped = _skip_manager_value_flag(
            tokens, i, raw, _PACKAGE_EXEC_VALUE_FLAGS, reserved,
        )
        if skipped is not None:
            i = skipped
            continue
        if raw.startswith("-"):
            i += 1
            continue
        if not saw_sub:
            if name0 == "yarn" and raw == "workspace" and not skip_workspace_name:
                skip_workspace_name = True
                i += 1
                continue
            if skip_workspace_name:
                skip_workspace_name = False
                i += 1
                continue
            if _is_package_exec_sub(name0, raw):
                saw_sub = True
                i += 1
                continue
            return None
        return _peel_exec_operand_separator([str(t) for t in tokens[i:]])
    return [] if saw_sub else None


def _bun_x_child_argv(tokens: List[str]) -> Optional[List[str]]:
    i = 1
    saw_sub = False
    while i < len(tokens):
        raw = str(tokens[i]) if tokens[i] is not None else ""
        if raw == "--":
            return [str(t) for t in tokens[i + 1:]] if saw_sub else None
        skipped = _skip_manager_value_flag(
            tokens, i, raw, _BUN_VALUE_FLAGS, _BUN_X_SUBCOMMANDS,
        )
        if skipped is not None:
            i = skipped
            continue
        if raw.startswith("-"):
            i += 1
            continue
        if not saw_sub:
            if raw in _BUN_X_SUBCOMMANDS:
                saw_sub = True
                i += 1
                continue
            return None
        return _peel_exec_operand_separator([str(t) for t in tokens[i:]])
    return [] if saw_sub else None


def _path_has_pkg(parts: List[str], *names: str) -> bool:
    """True when a path part is ``name`` or ``.name`` (Yarn Berry ``.yarn``)."""
    want = {n.lower() for n in names}
    return any(p.lower() in want or p.lower().lstrip(".") in want for p in parts)


def _token_is_yarn_script(token: str) -> bool:
    """True when this token is the Yarn CLI or its Node entry.

    Linux shebang rewrites argv0 to ``node``. Official
    ``…/yarn/bin/yarn.js`` and Berry ``…/.yarn/releases/yarn-4.x.cjs``
    are leftover writers. ``cat yarn.js`` is not.
    """
    if _token_basename_is(token, "yarn"):
        return True
    raw = (token or "").strip().strip("\"'")
    if not raw:
        return False
    path = Path(raw)
    name = path.name.lower()
    parts = [p.lower() for p in path.parts]
    # Corepack's ``yarn`` bin is ``…/corepack/dist/yarn.js`` — no ``yarn/``
    # package dir, so finding 107's yarn-pkg walk missed it (finding 108).
    if _path_has_pkg(parts, "corepack") and name in _COREPACK_YARN_SHIMS:
        return True
    if not _path_has_pkg(parts, "yarn"):
        return False
    if name in _YARN_NODE_ENTRYPOINTS:
        return True
    return name.startswith("yarn-") and name.endswith((".js", ".cjs", ".mjs"))


def _token_is_npx_script(token: str) -> bool:
    """True when this token is npx's Node entry (``npx-cli.js`` under npm)."""
    if _token_basename_is(token, "npx") or _token_basename_is(token, "pnpx"):
        return True
    raw = (token or "").strip().strip("\"'")
    if not raw:
        return False
    path = Path(raw)
    name = path.name.lower()
    if name in _NPX_NODE_ENTRYPOINTS:
        return True
    parts = [p.lower() for p in path.parts]
    return _path_has_pkg(parts, "corepack") and name in _COREPACK_NPX_SHIMS


def _token_is_npm_script(token: str) -> bool:
    """True when this token is npm's Node entry (``npm-cli.js``)."""
    if _token_basename_is(token, "npm"):
        return True
    raw = (token or "").strip().strip("\"'")
    if not raw:
        return False
    path = Path(raw)
    name = path.name.lower()
    if name not in _NPM_NODE_ENTRYPOINTS:
        return False
    parts = [p.lower() for p in path.parts]
    if _path_has_pkg(parts, "corepack") and name in _COREPACK_NPM_SHIMS:
        return True
    return _path_has_pkg(parts, "npm") or name.startswith("npm-")


def _token_is_pnpm_script(token: str) -> bool:
    """True when this token is pnpm's Node entry (``pnpm.cjs``)."""
    if _token_basename_is(token, "pnpm"):
        return True
    raw = (token or "").strip().strip("\"'")
    if not raw:
        return False
    path = Path(raw)
    name = path.name.lower()
    parts = [p.lower() for p in path.parts]
    if _path_has_pkg(parts, "corepack") and name in _COREPACK_PNPM_SHIMS:
        return True
    if not _path_has_pkg(parts, "pnpm"):
        return False
    return name in _PNPM_NODE_ENTRYPOINTS


def _token_is_corepack_script(token: str) -> bool:
    """True when this token is Corepack's Node entry (``corepack.js``)."""
    if _token_basename_is(token, "corepack"):
        return True
    raw = (token or "").strip().strip("\"'")
    if not raw:
        return False
    path = Path(raw)
    name = path.name.lower()
    parts = [p.lower() for p in path.parts]
    if not _path_has_pkg(parts, "corepack"):
        return False
    return name in _COREPACK_NODE_ENTRYPOINTS


def _node_package_manager_argv(tokens: List[str]) -> Optional[List[str]]:
    """``node …/yarn.js|npx-cli.js|npm-cli.js|pnpm.cjs <rest>`` → PM argv.

    Finding 79 matches shebang ``node …/lighthouse/cli.js``. After
    shebang the leftover *writer* for ``yarn npm exec --package=`` /
    ``npx --package=`` is still ``node``, so findings 105–106 never
    ran (finding 107). Corepack's ``yarn`` / ``pnpm`` bins are
    ``…/corepack/dist/yarn.js`` — no ``yarn/`` package dir — so 107
    missed those shims (finding 108). ``node /tmp/other.js
    --package=lighthouse`` is not a package-manager entry.
    """
    if not tokens or _launcher_basename(tokens[0]) not in _NODE_LAUNCHERS:
        return None
    i = 1
    while i < len(tokens):
        raw = str(tokens[i]) if tokens[i] is not None else ""
        if raw == "--":
            return None
        if raw.startswith("-"):
            i += 1
            continue
        if _token_is_yarn_script(raw):
            return ["yarn"] + [str(t) for t in tokens[i + 1:]]
        if _token_is_npx_script(raw):
            name = Path(raw).name.lower()
            launcher = (
                "pnpx"
                if name.startswith("pnpx") or _token_basename_is(raw, "pnpx")
                else "npx"
            )
            return [launcher] + [str(t) for t in tokens[i + 1:]]
        if _token_is_npm_script(raw):
            return ["npm"] + [str(t) for t in tokens[i + 1:]]
        if _token_is_pnpm_script(raw):
            return ["pnpm"] + [str(t) for t in tokens[i + 1:]]
        if _token_is_corepack_script(raw):
            return ["corepack"] + [str(t) for t in tokens[i + 1:]]
        return None
    return None


def _leftover_flag_tokens(tokens: List[str]) -> List[str]:
    """Argv the leftover CLI itself sees.

    ``npm exec --package=foo -- --browserUrl <dock>`` puts the child's
    flags after npm's ``--``. ``npx --package=lighthouse -- --port``
    is the same shape (finding 105). Shebang ``node …/npx-cli.js --``
    is finding 107. Stopping ``_flag_value`` at the first ``--`` then
    missed the dock aim. Peel corepack / exec|dlx / bun x / npx /
    shebang node PM *without* dropping child flags. A later ``--`` on
    the child (lighthouse yargs) still ends flag parse.
    """
    if not tokens:
        return []
    peeled = _corepack_command_tokens(tokens)
    work = peeled or [str(t) if t is not None else "" for t in tokens]
    node_pm = _node_package_manager_argv(work)
    if node_pm is not None:
        return _leftover_flag_tokens(node_pm)
    child = _package_manager_child_argv(work)
    if child is not None:
        return child
    npx_child = _npx_child_argv(work)
    return npx_child if npx_child is not None else work


def _is_agent_browser_invocation(tokens: List[str]) -> bool:
    """True when argv launches agent-browser (binary, npx, or shebang node).

    ``terminal()`` is ``bash -c`` (new session). After Linux shebang the
    leftover writer is ``node /path/to/agent-browser``, not argv0
    ``agent-browser``. Do not treat the bash parent as the writer — PID-only
    kill of bash orphans the Node child still sending CDP.
    """
    if not tokens:
        return False
    if _token_basename_is_agent_browser(tokens[0]):
        return True
    name0 = _launcher_basename(tokens[0])
    if name0 in _ENV_LAUNCHERS:
        return _is_agent_browser_invocation(_env_command_tokens(tokens))
    if name0 in _COREPACK_LAUNCHERS:
        rest = _corepack_command_tokens(tokens)
        return bool(rest) and _is_agent_browser_invocation(rest)
    via = _invocation_via_package_exec(tokens, _is_agent_browser_invocation)
    if via is not None:
        return via
    via = _invocation_via_bun_x(tokens, _is_agent_browser_invocation)
    if via is not None:
        return via
    if name0 in _NPX_LAUNCHERS:
        return _npx_invocation_matches(tokens, _token_basename_is_agent_browser)
    if name0 in _NODE_LAUNCHERS:
        node_pm = _node_package_manager_argv(tokens)
        if node_pm:
            return _is_agent_browser_invocation(node_pm)
        return any(_token_is_agent_browser_script(t) for t in _first_non_flag_tokens(tokens))
    if _is_python_launcher(name0):
        rest = _first_non_flag_tokens(tokens)
        return bool(rest) and _token_basename_is_agent_browser(rest[0])
    return False


def _token_is_browser_use_module(token: str) -> bool:
    """True when this token is the official ``python -m browser_use`` module.

    The PyPI package's console script is ``browser-use``; its import name is
    ``browser_use``. After ``python -m`` the leftover writer is ``python3``,
    not argv0 ``browser-use``. A path named ``browser_use`` is a random
    script, not the module. ``browser_use_cli`` / ``browser_usage`` are not.
    """
    raw = (token or "").strip().strip("\"'")
    if not raw or "/" in raw or "\\" in raw:
        return False
    return _token_basename_is(raw, "browser_use")


def _is_browser_use_invocation(tokens: List[str]) -> bool:
    """True when argv launches the browser-use CLI (direct, uvx, uv run, python).

    Token-match only. ``cat browser-use.log`` and ``uvx ruff`` are not
    invocations. Finding 42 only tracks Hermes-spawned ``browser_exec``.
    Official leftover when the console script is missing is
    ``python -m browser_use`` — finding 79 matched the hyphenated script
    path and missed the module form, so Take over left that writer in
    the field a human was typing into.
    """
    if not tokens:
        return False
    if _token_basename_is(tokens[0], "browser-use"):
        return True
    name0 = _launcher_basename(tokens[0])
    if name0 in _ENV_LAUNCHERS:
        return _is_browser_use_invocation(_env_command_tokens(tokens))
    if name0 in _COREPACK_LAUNCHERS:
        rest = _corepack_command_tokens(tokens)
        return bool(rest) and _is_browser_use_invocation(rest)
    via = _invocation_via_package_exec(tokens, _is_browser_use_invocation)
    if via is not None:
        return via
    via = _invocation_via_bun_x(tokens, _is_browser_use_invocation)
    if via is not None:
        return via
    if _is_python_launcher(name0):
        rest = _first_non_flag_tokens(tokens)
        return bool(rest) and (
            _token_basename_is(rest[0], "browser-use")
            or _token_is_browser_use_module(rest[0])
        )
    if name0 not in _UVX_LAUNCHERS:
        return False
    rest = _first_non_flag_tokens(tokens)
    if not rest:
        return False
    if name0 == "uvx":
        return _token_basename_is(rest[0], "browser-use")
    # uv tool run browser-use / uv run browser-use
    if rest[0] == "tool" and len(rest) >= 3 and rest[1] == "run":
        return _token_basename_is(rest[2], "browser-use")
    if rest[0] == "run" and len(rest) >= 2:
        return _token_basename_is(rest[1], "browser-use")
    return False


def _token_is_playwright_script(token: str) -> bool:
    """True when this token is the Playwright CLI or its Node entry.

    ``…/playwright/cli.js`` is an invocation. ``playwright-cli`` is the
    Agent CLI leftover writer (``attach --cdp=<dock>``).
    ``…/@playwright/mcp/cli.js`` is not — Path parts are ``@playwright``
    + ``mcp``, not ``playwright``. ``playwright-core`` is not.
    ``npx playwright install`` is an invocation but stays unknown without
    a CDP aim (not leftover action).
    """
    if _token_basename_is(token, "playwright") or _token_basename_is(
        token, "playwright-cli",
    ):
        return True
    raw = (token or "").strip().strip("\"'")
    if not raw:
        return False
    path = Path(raw)
    name = path.name.lower()
    parts = [p.lower() for p in path.parts]
    if "playwright-cli" in parts:
        return name in _PLAYWRIGHT_NODE_ENTRYPOINTS or name in {
            "playwright-cli", "playwright-cli.js",
        }
    if "playwright" not in parts:
        return False
    return name in _PLAYWRIGHT_NODE_ENTRYPOINTS


def _is_playwright_invocation(tokens: List[str]) -> bool:
    """True when argv launches the Playwright CLI (binary, npx, shebang node).

    Token-match only. ``npx playwright install`` is an invocation; it is
    not dock-aimed unless ``--cdp-endpoint`` / ``PW_TEST_CONNECT_*`` pin
    this jar. ``playwright-cli attach --cdp=<dock>`` is leftover attach
    (finding 109). Do not match ``@playwright/mcp`` or ``playwright-core``
    by substring.
    """
    if not tokens:
        return False
    if _token_basename_is(tokens[0], "playwright") or _token_basename_is(
        tokens[0], "playwright-cli",
    ):
        return True
    name0 = _launcher_basename(tokens[0])
    if name0 in _ENV_LAUNCHERS:
        return _is_playwright_invocation(_env_command_tokens(tokens))
    if name0 in _COREPACK_LAUNCHERS:
        rest = _corepack_command_tokens(tokens)
        return bool(rest) and _is_playwright_invocation(rest)
    via = _invocation_via_package_exec(tokens, _is_playwright_invocation)
    if via is not None:
        return via
    via = _invocation_via_bun_x(tokens, _is_playwright_invocation)
    if via is not None:
        return via
    if name0 in _NPX_LAUNCHERS:
        return _npx_invocation_matches(tokens, _token_is_playwright_script)
    if name0 in _NODE_LAUNCHERS:
        node_pm = _node_package_manager_argv(tokens)
        if node_pm:
            return _is_playwright_invocation(node_pm)
        return any(_token_is_playwright_script(t) for t in _first_non_flag_tokens(tokens))
    if _is_python_launcher(name0):
        rest = _first_non_flag_tokens(tokens)
        return bool(rest) and _token_basename_is(rest[0], "playwright")
    return False


def _token_is_playwright_mcp(token: str) -> bool:
    """True when this token is the ``@playwright/mcp`` package or its Node entry.

    Path parts are ``@playwright`` + ``mcp``, not ``playwright`` — finding 84
    correctly refused to treat that as the Playwright CLI. Token-match the
    scoped package only. ``…/playwright/cli.js`` and ``cat mcp.log`` are not.
    """
    raw = (token or "").strip().strip("\"'")
    if not raw:
        return False
    lower = raw.lower()
    if lower.startswith("@playwright/mcp"):
        rest = lower[len("@playwright/mcp"):]
        return rest == "" or rest.startswith("@")
    path = Path(raw)
    parts = [p.lower() for p in path.parts]
    if "@playwright" not in parts:
        return False
    idx = parts.index("@playwright")
    if idx + 1 >= len(parts):
        return False
    if parts[idx + 1].split("@", 1)[0] != "mcp":
        return False
    return path.name.lower() in _PLAYWRIGHT_MCP_NODE_ENTRYPOINTS


def _flag_value(tokens: List[str], keys: Tuple[str, ...]) -> Optional[str]:
    """Value of a flag (``--key val`` or ``--key=val``).

    yargs / commander / Chromium keep the *last* value when a flag
    repeats and stop at ``--``. First-wins treated
    ``lighthouse --port=1 --port=<dock>`` as another Chrome, so Take
    over left the leftover writer running. A following flag is not a
    value. ``--cdp`` does not eat ``--cdp-endpoint`` (prefix is
    ``key=``).
    """
    tokens = _leftover_flag_tokens(tokens)
    found: Optional[str] = None
    i = 0
    n = len(tokens)
    while i < n:
        raw = str(tokens[i]) if tokens[i] is not None else ""
        if raw == "--":
            break
        for key in keys:
            if raw == key:
                if i + 1 < n:
                    nxt = str(tokens[i + 1])
                    if nxt.startswith("-"):
                        found = None
                    else:
                        found = nxt
                        i += 1
                else:
                    found = None
                break
            if raw.startswith(key + "="):
                found = raw.split("=", 1)[1]
                break
        i += 1
    return found


def _flag_value_allow_leading_dash(
    tokens: List[str], keys: Tuple[str, ...],
) -> Optional[str]:
    """Like ``_flag_value`` but a following ``--switch`` is still a value.

    lighthouse ``--chrome-flags "--user-data-dir=<dock>"`` /
    ``--chrome-flags --user-data-dir=<dock>`` are Chrome switches, so
    ``_flag_value`` treated them as missing. ``--`` still ends parse.
    Last-wins when the flag repeats.
    """
    tokens = _leftover_flag_tokens(tokens)
    found: Optional[str] = None
    i = 0
    n = len(tokens)
    while i < n:
        raw = str(tokens[i]) if tokens[i] is not None else ""
        if raw == "--":
            break
        for key in keys:
            if raw == key:
                if i + 1 < n:
                    nxt = str(tokens[i + 1])
                    if nxt == "--":
                        found = None
                    else:
                        found = nxt
                        i += 1
                else:
                    found = None
                break
            if raw.startswith(key + "="):
                found = raw.split("=", 1)[1]
                break
        i += 1
    return found


def _flag_values_allow_leading_dash(
    tokens: List[str], keys: Tuple[str, ...],
) -> List[str]:
    """Every leftover value for a yargs-style array flag.

    ``--chromeArg --no-sandbox --chromeArg --user-data-dir=<dock>``
    keeps both operands. Last-wins ``_flag_value_allow_leading_dash``
    dropped the dock pin when a later chromeArg was ``--headless``.
    ``--`` still ends parse.
    """
    tokens = _leftover_flag_tokens(tokens)
    found: List[str] = []
    i = 0
    n = len(tokens)
    while i < n:
        raw = str(tokens[i]) if tokens[i] is not None else ""
        if raw == "--":
            break
        matched = False
        for key in keys:
            if raw == key:
                if i + 1 < n:
                    nxt = str(tokens[i + 1])
                    if nxt != "--":
                        found.append(nxt)
                        i += 1
                matched = True
                break
            if raw.startswith(key + "="):
                found.append(raw.split("=", 1)[1])
                matched = True
                break
        i += 1
    return found


def _token_is_chrome_remote_interface(token: str) -> bool:
    """True when this token is the CRI CLI or its Node entry.

    Token-match only. ``node /tmp/cdp-debug.js`` that ``require``s the
    library is not an invocation — that argv has no package token.
    """
    if _token_basename_is(token, "chrome-remote-interface"):
        return True
    raw = (token or "").strip().strip("\"'")
    if not raw:
        return False
    path = Path(raw)
    parts = [p.lower() for p in path.parts]
    if "chrome-remote-interface" not in parts:
        return False
    return path.name.lower() in {
        "chrome-remote-interface", "client.js", "cli.js", "cli.mjs", "index.js",
    }


def _is_chrome_remote_interface_invocation(tokens: List[str]) -> bool:
    """True when argv launches the chrome-remote-interface CLI.

    ``npx chrome-remote-interface --port <dock> inspect`` is leftover
    action. A random ``node script.js`` is not, even if the script
    requires the library.
    """
    if not tokens:
        return False
    if _token_is_chrome_remote_interface(tokens[0]):
        return True
    name0 = _launcher_basename(tokens[0])
    if name0 in _ENV_LAUNCHERS:
        return _is_chrome_remote_interface_invocation(_env_command_tokens(tokens))
    if name0 in _COREPACK_LAUNCHERS:
        rest = _corepack_command_tokens(tokens)
        return bool(rest) and _is_chrome_remote_interface_invocation(rest)
    via = _invocation_via_package_exec(tokens, _is_chrome_remote_interface_invocation)
    if via is not None:
        return via
    via = _invocation_via_bun_x(tokens, _is_chrome_remote_interface_invocation)
    if via is not None:
        return via
    if name0 in _NPX_LAUNCHERS:
        return _npx_invocation_matches(tokens, _token_is_chrome_remote_interface)
    if name0 in _NODE_LAUNCHERS:
        node_pm = _node_package_manager_argv(tokens)
        if node_pm:
            return _is_chrome_remote_interface_invocation(node_pm)
        return any(_token_is_chrome_remote_interface(t) for t in _first_non_flag_tokens(tokens))
    return False


def _is_playwright_mcp_invocation(tokens: List[str]) -> bool:
    """True when argv launches ``@playwright/mcp`` (npx, shebang node).

    Token-match only. ``npx @playwright/mcp`` without ``--cdp-endpoint``
    launches its own Chrome and stays unknown. Do not match a bare ``mcp``
    binary or ``@playwright/test``.
    """
    if not tokens:
        return False
    if _token_is_playwright_mcp(tokens[0]):
        return True
    name0 = _launcher_basename(tokens[0])
    if name0 in _ENV_LAUNCHERS:
        return _is_playwright_mcp_invocation(_env_command_tokens(tokens))
    if name0 in _COREPACK_LAUNCHERS:
        rest = _corepack_command_tokens(tokens)
        return bool(rest) and _is_playwright_mcp_invocation(rest)
    via = _invocation_via_package_exec(tokens, _is_playwright_mcp_invocation)
    if via is not None:
        return via
    via = _invocation_via_bun_x(tokens, _is_playwright_mcp_invocation)
    if via is not None:
        return via
    if name0 in _NPX_LAUNCHERS:
        return _npx_invocation_matches(tokens, _token_is_playwright_mcp)
    if name0 in _NODE_LAUNCHERS:
        node_pm = _node_package_manager_argv(tokens)
        if node_pm:
            return _is_playwright_mcp_invocation(node_pm)
        return any(_token_is_playwright_mcp(t) for t in _first_non_flag_tokens(tokens))
    return False


def _token_is_chrome_devtools_mcp(token: str) -> bool:
    """True when this token is chrome-devtools-mcp or its official CLI bin.

    The package ships two bins: ``chrome-devtools-mcp`` (MCP server) and
    ``chrome-devtools`` (CLI wrapper — ``chrome-devtools start`` /
    ``fill`` / ``click``). Finding 89 / 123 only matched the MCP name, so
    leftover ``chrome-devtools start --userDataDir=<dock>`` never
    counted as an invocation. ``chrome-devtools-frontend`` /
    ``cat chrome-devtools-mcp.log`` are not.
    """
    if _token_basename_is(token, "chrome-devtools-mcp"):
        return True
    if _token_basename_is(token, "chrome-devtools"):
        return True
    raw = (token or "").strip().strip("\"'")
    if not raw:
        return False
    path = Path(raw)
    parts = [p.lower() for p in path.parts]
    if "chrome-devtools-mcp" not in parts:
        return False
    name = path.name.lower()
    if name.startswith("chrome-devtools-mcp"):
        return True
    return name in {
        "cli.js", "cli.mjs", "cli.cjs", "index.js", "bin.js", "main.js",
        "chrome-devtools.js", "chrome-devtools.mjs", "chrome-devtools.cjs",
    }


def _is_chrome_devtools_mcp_invocation(tokens: List[str]) -> bool:
    """True when argv launches chrome-devtools-mcp or chrome-devtools.

    Token-match only. Official leftover CLI is argv0 ``chrome-devtools``
    after ``npm i -g chrome-devtools-mcp``. ``--autoConnect`` / no pin
    launches or attaches a Chrome we cannot prove is this jar — stay
    unknown. ``chrome-devtools-frontend`` is not this package.
    """
    if not tokens:
        return False
    if _token_is_chrome_devtools_mcp(tokens[0]):
        return True
    name0 = _launcher_basename(tokens[0])
    if name0 in _ENV_LAUNCHERS:
        return _is_chrome_devtools_mcp_invocation(_env_command_tokens(tokens))
    if name0 in _COREPACK_LAUNCHERS:
        rest = _corepack_command_tokens(tokens)
        return bool(rest) and _is_chrome_devtools_mcp_invocation(rest)
    via = _invocation_via_package_exec(tokens, _is_chrome_devtools_mcp_invocation)
    if via is not None:
        return via
    via = _invocation_via_bun_x(tokens, _is_chrome_devtools_mcp_invocation)
    if via is not None:
        return via
    if name0 in _NPX_LAUNCHERS:
        return _npx_invocation_matches(tokens, _token_is_chrome_devtools_mcp)
    if name0 in _NODE_LAUNCHERS:
        node_pm = _node_package_manager_argv(tokens)
        if node_pm:
            return _is_chrome_devtools_mcp_invocation(node_pm)
        return any(_token_is_chrome_devtools_mcp(t) for t in _first_non_flag_tokens(tokens))
    return False


def _token_is_lighthouse(token: str) -> bool:
    """True when this token is the Lighthouse CLI or its Node entry.

    Token-match only. ``lighthouse-ci`` / ``@lhci/cli`` / ``cat lighthouse.log``
    are not invocations. Path parts must include ``lighthouse`` — not
    ``lighthouse-ci``.
    """
    if _token_basename_is(token, "lighthouse"):
        return True
    raw = (token or "").strip().strip("\"'")
    if not raw:
        return False
    path = Path(raw)
    parts = [p.lower() for p in path.parts]
    if "lighthouse" not in parts:
        return False
    return path.name.lower() in _LIGHTHOUSE_NODE_ENTRYPOINTS


def _is_lighthouse_invocation(tokens: List[str]) -> bool:
    """True when argv launches the Lighthouse CLI (binary, npx, shebang node).

    Token-match only. ``npx lighthouse https://example.com`` without
    ``--port`` launches its own Chrome and stays unknown at aim time.
    Do not match ``lighthouse-ci`` or a bash ``-c`` parent. Do not use
    ``-p`` as a port flag — that is npm's package pin / CRI's short port.
    """
    if not tokens:
        return False
    if _token_is_lighthouse(tokens[0]):
        return True
    name0 = _launcher_basename(tokens[0])
    if name0 in _ENV_LAUNCHERS:
        return _is_lighthouse_invocation(_env_command_tokens(tokens))
    if name0 in _COREPACK_LAUNCHERS:
        rest = _corepack_command_tokens(tokens)
        return bool(rest) and _is_lighthouse_invocation(rest)
    via = _invocation_via_package_exec(tokens, _is_lighthouse_invocation)
    if via is not None:
        return via
    via = _invocation_via_bun_x(tokens, _is_lighthouse_invocation)
    if via is not None:
        return via
    if name0 in _NPX_LAUNCHERS:
        return _npx_invocation_matches(tokens, _token_is_lighthouse)
    if name0 in _NODE_LAUNCHERS:
        node_pm = _node_package_manager_argv(tokens)
        if node_pm:
            return _is_lighthouse_invocation(node_pm)
        return any(_token_is_lighthouse(t) for t in _first_non_flag_tokens(tokens))
    if _is_python_launcher(name0):
        rest = _first_non_flag_tokens(tokens)
        return bool(rest) and _token_is_lighthouse(rest[0])
    return False


def _is_unregistered_dock_cli_invocation(tokens: List[str]) -> bool:
    return (
        _is_agent_browser_invocation(tokens)
        or _is_browser_use_invocation(tokens)
        or _is_playwright_invocation(tokens)
        or _is_playwright_mcp_invocation(tokens)
        or _is_chrome_remote_interface_invocation(tokens)
        or _is_chrome_devtools_mcp_invocation(tokens)
        or _is_lighthouse_invocation(tokens)
    )


def _cdp_arg_from_argv(tokens: List[str]) -> Optional[str]:
    """``--cdp`` / ``--cdp-endpoint`` value, or None. Does not guess ``--session``."""
    return _flag_value(tokens, ("--cdp-endpoint", "--cdp"))


def _agent_browser_connect_target(tokens: List[str]) -> Optional[str]:
    """``agent-browser connect <port|url>`` leftover attach, or None.

    Official leftover: connect once, then later commands have no ``--cdp``.
    The in-flight writer is ``connect 9333`` / ``connect http://…``.
    ``--session foo connect 9333`` must not treat ``foo`` as the target.
    ``--auto-connect`` is not a port. ``connect`` with no operand stays
    unknown.
    """
    if not tokens or not _is_agent_browser_invocation(tokens):
        return None
    work = _leftover_flag_tokens(tokens)
    rest = _first_non_flag_tokens(work, value_flags=_AGENT_BROWSER_VALUE_FLAGS)
    if rest and (
        _token_basename_is_agent_browser(rest[0])
        or _token_is_agent_browser_script(rest[0])
    ):
        rest = rest[1:]
    if len(rest) < 2 or rest[0] != "connect":
        return None
    target = str(rest[1]) if rest[1] is not None else ""
    if not target or target.startswith("-"):
        return None
    return target


def _unregistered_cli_aims_at_dock(
    tokens: List[str],
    environ: Optional[Dict[str, str]],
    profile: Optional[Path],
    dock_port: Optional[int],
    *,
    cwd: Optional[Path] = None,
) -> bool:
    """True when this leftover CLI is aimed at *this* profile's dock jar.

    Explicit ``--cdp`` wins: another loopback Chrome, a LAN endpoint, or a
    cloud URL is not the dock even if ``AGENT_BROWSER_PROFILE`` is pinned.
    Official leftover that *stays* after ``connect <dock>`` is the
    daemon with ``AGENT_BROWSER_CDP`` frozen (finding 112). Positional
    ``connect <port|url>`` is the in-flight attach. ``--auto-connect`` /
    ``AGENT_BROWSER_AUTO_CONNECT`` stay unknown (a Chrome we cannot
    prove is this jar). ``--session`` without a CDP pin stays unknown.

    browser-use leftover writers (finding 78) aim via ``BU_CDP_*`` /
    ``BROWSER_CDP_URL``. Official leftover attach is also
    ``--cdp-url <dock>`` on argv (finding 110); env-only hid that
    writer. ``--connect`` / no URL stays unknown (a Chrome we cannot
    prove is this jar). Explicit ``--cdp-url`` wins over env.

    A relative ``AGENT_BROWSER_PROFILE`` is the leftover writer's jar,
    resolved against *that* process cwd (finding 95 for Chromium argv).
    Official leftover also expands ``~/`` on ``--profile`` /
    ``AGENT_BROWSER_PROFILE`` (finding 129) — agent-browser replaces
    a leading ``~/`` with ``os.homedir()``. Chromium / Playwright /
    lighthouse do not; do not expand ``--user-data-dir``. Official
    leftover also pins the jar on argv: agent-browser
    ``--profile`` and Playwright ``--user-data-dir`` / ``open --profile``
    (finding 111). Official leftover also forwards Chromium
    ``--user-data-dir`` via ``--args`` / ``AGENT_BROWSER_ARGS``
    (finding 130) — comma or newline separated. Finding 111 only
    checked ``--profile``, so Take over left that writer running.
    Explicit ``--cdp`` still wins.     chrome-devtools-mcp ``--userDataDir`` /
    ``--user-data-dir`` is the same launch pin (finding 123) — it
    conflicts with attach flags, so URL-only hid that writer. Official
    leftover also hides the pin in ``--chromeArg`` / ``--chrome-arg``
    (yargs array; Puppeteer appends those switches after its
    ``userDataDir``, so Chromium last-wins the dock) and in ``--config``
    JSON ``userDataDir`` / ``browserUrl`` / ``wsEndpoint`` / ``chromeArg``
    (finding 131). Finding 123 only checked argv ``--userDataDir``, so
    Take over left ``--chrome-arg=--user-data-dir=<dock>`` and
    ``--config {userDataDir}`` typing. yargs CLI flags still override
    the file per key. Env-only hid those writers. Explicit ``--cdp`` /
    ``--browserUrl`` still wins. Gateway cwd must not decide the pin.
    browser-use ``--profile`` is a Chrome profile *name* and stays
    unknown. ``--autoConnect`` / no pin stays unknown.

    lighthouse leftover launch pin is ``--chrome-flags=--user-data-dir=<dock>``
    (finding 126). Official leftover also hides ``--port`` /
    ``--chrome-flags`` in ``--cli-flags-path`` / ``--cliFlagsPath`` JSON
    (finding 128) — yargs ``config: true``. Finding 126 only checked
    argv, so Take over left that writer running. CLI flags still
    override the file. Official attach is still ``--port``.
    chrome-launcher appends chrome-flags *after* its temp
    ``--user-data-dir``, so Chromium last-wins the dock jar.
    ``--port=0`` / missing ``--port`` launch that Chrome. A set
    ``--port`` that is not this dock stays another Chrome (do not
    guess an empty listen). LAN ``--hostname`` does not launch local
    Chrome. ``--config-path`` is Lighthouse audit config, not CLI
    flags.

    Playwright MCP leftover also pins the jar off argv (finding 127):
    ``PLAYWRIGHT_MCP_USER_DATA_DIR`` and ``--config`` /
    ``PLAYWRIGHT_MCP_CONFIG`` ``browser.userDataDir`` / ``cdpEndpoint``.
    Finding 111 only checked ``--user-data-dir``. Regular Playwright
    CLI does not read those MCP keys. ``--isolated`` / no pin stays
    unknown.
    """
    env = environ or {}
    if _is_browser_use_invocation(tokens) and not _is_agent_browser_invocation(tokens):
        # Official leftover attach: ``browser-use --cdp-url <dock>``.
        # Finding 78 only checked BU_CDP_* env, so Take over left the
        # argv-aimed writer running (finding 110). Do not guess
        # ``--cdp`` (agent-browser) or treat ``--connect`` as this jar.
        cdp = _flag_value(tokens, ("--cdp-url",))
        if cdp:
            if _cdp_url_is_bot_desktop_browser(cdp):
                return True
            port = _loopback_cdp_port(cdp)
            return dock_port is not None and port == dock_port
        for key in ("BU_CDP_WS", "BU_CDP_URL", "BROWSER_CDP_URL"):
            val = (env.get(key) or "").strip()
            if not val:
                continue
            if _cdp_url_is_bot_desktop_browser(val):
                return True
            port = _loopback_cdp_port(val)
            return dock_port is not None and port == dock_port
        return False
    if (
        (_is_playwright_invocation(tokens) or _is_playwright_mcp_invocation(tokens))
        and not _is_agent_browser_invocation(tokens)
    ):
        cdp = _cdp_arg_from_argv(tokens)
        if not cdp:
            for key in _PLAYWRIGHT_CDP_ENV:
                cdp = (env.get(key) or "").strip()
                if cdp:
                    break
        if not cdp:
            # Official leftover: ``codegen --user-data-dir=<dock>`` /
            # ``playwright-cli open --profile=<dock>``. Finding 99 only
            # checked AGENT_BROWSER_PROFILE on the agent-browser
            # fallthrough, so Take over left these writers running.
            pinned = _flag_value(tokens, ("--user-data-dir", "--profile"))
            if _leftover_profile_pin_aims_at_dock(pinned, profile, cwd):
                return True
            # Official MCP leftover launch / attach also lives in env
            # and ``--config`` JSON (finding 127). Finding 111 only
            # checked argv ``--user-data-dir``, so Take over left
            # ``PLAYWRIGHT_MCP_USER_DATA_DIR=<dock>`` and
            # ``--config {browser.userDataDir|cdpEndpoint}`` typing.
            # Regular Playwright CLI does not read those keys.
            if not _is_playwright_mcp_invocation(tokens):
                return False
            pinned = (env.get("PLAYWRIGHT_MCP_USER_DATA_DIR") or "").strip()
            if _leftover_profile_pin_aims_at_dock(pinned, profile, cwd):
                return True
            cfg_cdp, cfg_dir = _playwright_mcp_config_pins(tokens, env, cwd)
            if cfg_cdp:
                if _cdp_url_is_bot_desktop_browser(cfg_cdp):
                    return True
                port = _loopback_cdp_port(cfg_cdp)
                return dock_port is not None and port == dock_port
            return _leftover_profile_pin_aims_at_dock(cfg_dir, profile, cwd)
        if _cdp_url_is_bot_desktop_browser(cdp):
            return True
        port = _loopback_cdp_port(cdp)
        return dock_port is not None and port == dock_port
    if (
        _is_chrome_remote_interface_invocation(tokens)
        and not _is_agent_browser_invocation(tokens)
    ):
        ws = _flag_value(tokens, ("--web-socket", "-w"))
        if ws:
            if _cdp_url_is_bot_desktop_browser(ws):
                return True
            port = _loopback_cdp_port(ws)
            return dock_port is not None and port == dock_port
        host = _flag_value(tokens, ("--host",))
        if host and not _is_loopback_cdp_host(host):
            return False
        port_text = _flag_value(tokens, ("--port", "-p"))
        if port_text and str(port_text).isdigit():
            port = int(port_text)
            return dock_port is not None and 1 <= port <= 65535 and port == dock_port
        val = (env.get("BROWSER_CDP_URL") or "").strip()
        if val:
            if _cdp_url_is_bot_desktop_browser(val):
                return True
            port = _loopback_cdp_port(val)
            return dock_port is not None and port == dock_port
        return False
    if (
        _is_chrome_devtools_mcp_invocation(tokens)
        and not _is_agent_browser_invocation(tokens)
    ):
        for keys in (
            ("--browserUrl", "--browser-url", "-u"),
            ("--wsEndpoint", "--ws-endpoint", "-w"),
            ("--cdp-endpoint", "--cdp"),
        ):
            cdp = _flag_value(tokens, keys)
            if not cdp:
                continue
            if _cdp_url_is_bot_desktop_browser(cdp):
                return True
            port = _loopback_cdp_port(cdp)
            return dock_port is not None and port == dock_port
        val = (env.get("BROWSER_CDP_URL") or "").strip()
        if val:
            if _cdp_url_is_bot_desktop_browser(val):
                return True
            port = _loopback_cdp_port(val)
            return dock_port is not None and port == dock_port
        # Official leftover launch pin: ``--userDataDir`` / ``--user-data-dir``
        # conflicts with ``--browserUrl`` / ``--wsEndpoint`` / ``--isolated``.
        # Finding 89 only checked URL attach, so Take over left the
        # launch-on-jar writer running on the cookie jar a human holds.
        # Official leftover also forwards Chromium ``--user-data-dir``
        # via ``--chromeArg`` / ``--chrome-arg`` and hides both launch
        # and attach in ``--config`` JSON (finding 131). Finding 123
        # only checked argv ``--userDataDir``. ``_flag_value`` treats
        # ``--chrome-arg=--user-data-dir=<dock>`` as missing.
        # Puppeteer appends chromeArg after its ``userDataDir``, so
        # Chromium last-wins the dock. yargs CLI still overrides the
        # file per key. ``--autoConnect`` / no pin stays unknown (a
        # Chrome we cannot prove is this jar).
        argv_chrome = _chrome_devtools_chrome_args(tokens)
        if argv_chrome:
            pinned = _user_data_dir_from_chrome_flags(" ".join(argv_chrome))
            if _leftover_profile_pin_aims_at_dock(pinned, profile, cwd):
                return True
        pinned = _flag_value(tokens, ("--userDataDir", "--user-data-dir"))
        if _leftover_profile_pin_aims_at_dock(pinned, profile, cwd):
            return True
        return _chrome_devtools_config_aims_at_dock(
            tokens, profile, dock_port, cwd,
            skip_user_data_dir=pinned is not None,
            skip_chrome_arg=bool(argv_chrome),
        )
    if (
        _is_lighthouse_invocation(tokens)
        and not _is_agent_browser_invocation(tokens)
    ):
        # Official attach: ``lighthouse URL --port=<dock>``. Default
        # hostname is localhost. ``--port=0`` / missing ``--port`` launch
        # their own Chrome. LAN ``--hostname`` is another machine.
        # ``--port`` only — never ``-p`` (npm pin / CRI short port).
        host = _flag_value(tokens, ("--hostname",))
        if host and not _is_loopback_cdp_host(host):
            return False
        port_text = _flag_value(tokens, ("--port",))
        if port_text and str(port_text).isdigit():
            port = int(port_text)
            if 1 <= port <= 65535:
                return dock_port is not None and port == dock_port
        # Official leftover launch pin: ``--chrome-flags=--user-data-dir``.
        # Finding 92 only checked ``--port``, so Take over left the
        # launch-on-jar writer running on the cookie jar a human holds.
        # chrome-launcher emits its temp dir *before* chrome-flags;
        # Chromium last-wins the dock. ``--chromeFlags`` is yargs
        # camelCase. A set non-dock ``--port`` stays another Chrome.
        chrome_flags = _flag_value_allow_leading_dash(
            tokens, ("--chrome-flags", "--chromeFlags"),
        )
        pinned = _user_data_dir_from_chrome_flags(chrome_flags)
        if _leftover_profile_pin_aims_at_dock(pinned, profile, cwd):
            return True
        # Official leftover also hides ``--port`` / ``--chrome-flags``
        # in ``--cli-flags-path`` JSON (finding 128). Finding 126 only
        # checked argv. CLI flags still override the file (yargs).
        return _lighthouse_cli_flags_path_aims_at_dock(
            tokens, profile, dock_port, cwd,
        )
    cdp = _cdp_arg_from_argv(tokens)
    if not cdp and _is_agent_browser_invocation(tokens):
        cdp = _agent_browser_connect_target(tokens)
    if not cdp and _is_agent_browser_invocation(tokens):
        cdp = (env.get("AGENT_BROWSER_CDP") or "").strip() or None
    if cdp:
        if _cdp_url_is_bot_desktop_browser(cdp):
            return True
        port = _loopback_cdp_port(cdp)
        return dock_port is not None and port == dock_port
    pinned = _flag_value(tokens, ("--profile",))
    if not pinned:
        pinned = (env.get("AGENT_BROWSER_PROFILE") or "").strip()
    # Official leftover expands ``~/`` on --profile / AGENT_BROWSER_PROFILE
    # (finding 129). Finding 111 compared the literal ``~/…`` path, so
    # Take over left that writer running. Chromium does not expand
    # ``--user-data-dir``; only this agent-browser pin does.
    pinned = _expand_agent_browser_home_prefix(pinned, env)
    if _leftover_profile_pin_aims_at_dock(pinned, profile, cwd):
        return True
    # Official leftover launch pin: ``--args --user-data-dir=<dock>`` /
    # ``AGENT_BROWSER_ARGS`` (finding 130). Finding 111 only checked
    # ``--profile``. Playwright launch appends user args after its temp
    # dir; Chromium last-wins the dock jar. CLI ``--args`` overrides
    # the env key. ``--cdp`` already returned above.
    raw_args = _flag_value_allow_leading_dash(tokens, ("--args",))
    if raw_args is None:
        raw_args = (env.get("AGENT_BROWSER_ARGS") or "").strip() or None
        if raw_args is None:
            raw_args = (env.get("AGENT_BROWSER_CHROME_FLAGS") or "").strip() or None
    pinned = _user_data_dir_from_agent_browser_args(raw_args)
    return _leftover_profile_pin_aims_at_dock(pinned, profile, cwd)


_CHROME_DEVTOOLS_CHROME_ARG_FLAGS = ("--chromeArg", "--chrome-arg")
_CHROME_DEVTOOLS_CONFIG_MAX_BYTES = 256 * 1024


def _chrome_devtools_chrome_args(tokens: List[str]) -> List[str]:
    """All leftover ``--chromeArg`` / ``--chrome-arg`` values (yargs array)."""
    return _flag_values_allow_leading_dash(tokens, _CHROME_DEVTOOLS_CHROME_ARG_FLAGS)


def _chrome_devtools_config_str(data: dict, *keys: str) -> Optional[str]:
    for key in keys:
        raw = data.get(key)
        if isinstance(raw, str) and raw.strip():
            return raw.strip()
    return None


def _chrome_devtools_config_chrome_args(data: dict) -> List[str]:
    raw = data.get("chromeArg")
    if raw is None:
        raw = data.get("chrome-arg")
    if isinstance(raw, str) and raw.strip():
        return [raw.strip()]
    if not isinstance(raw, list):
        return []
    out: List[str] = []
    for item in raw:
        if isinstance(item, str) and item.strip():
            out.append(item.strip())
    return out


def _chrome_devtools_config_aims_at_dock(
    tokens: List[str],
    profile: Optional[Path],
    dock_port: Optional[int],
    cwd: Optional[Path],
    *,
    skip_user_data_dir: bool = False,
    skip_chrome_arg: bool = False,
) -> bool:
    """True when leftover ``--config`` JSON aims at this dock.

    Official leftover: ``npx chrome-devtools-mcp --config mcp.json``
    with flat ``userDataDir`` / ``browserUrl`` / ``wsEndpoint`` /
    ``chromeArg``. Finding 123 only checked argv ``--userDataDir``,
    so Take over left that writer running. yargs CLI flags override
    the file per key. Relative paths resolve against the leftover
    writer cwd. Unreadable / oversized / non-JSON stays unknown.
    """
    path_text = _flag_value(tokens, ("--config",))
    text = (path_text or "").strip()
    if not text:
        return False
    path = Path(text)
    if not path.is_absolute():
        base = cwd if cwd is not None else Path.cwd()
        path = base / path
    try:
        if path.stat().st_size > _CHROME_DEVTOOLS_CONFIG_MAX_BYTES:
            return False
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError, ValueError):
        return False
    if not isinstance(data, dict):
        return False
    cdp = _chrome_devtools_config_str(
        data,
        "browserUrl", "browser-url",
        "wsEndpoint", "ws-endpoint",
    )
    if cdp:
        if _cdp_url_is_bot_desktop_browser(cdp):
            return True
        port = _loopback_cdp_port(cdp)
        return dock_port is not None and port == dock_port
    if not skip_user_data_dir:
        pinned = _chrome_devtools_config_str(data, "userDataDir", "user-data-dir")
        if _leftover_profile_pin_aims_at_dock(pinned, profile, cwd):
            return True
    if skip_chrome_arg:
        return False
    chrome_args = _chrome_devtools_config_chrome_args(data)
    if not chrome_args:
        return False
    pinned = _user_data_dir_from_chrome_flags(" ".join(chrome_args))
    return _leftover_profile_pin_aims_at_dock(pinned, profile, cwd)


_PLAYWRIGHT_MCP_CONFIG_MAX_BYTES = 256 * 1024


def _playwright_mcp_config_pins(
    tokens: List[str],
    environ: Dict[str, str],
    cwd: Optional[Path],
) -> Tuple[Optional[str], Optional[str]]:
    """``(cdpEndpoint, userDataDir)`` from leftover MCP ``--config`` / env.

    Official leftover: ``npx @playwright/mcp --config mcp.json`` with
    ``browser.cdpEndpoint`` / ``browser.userDataDir``. Finding 109
    covered argv / ``PLAYWRIGHT_MCP_CDP_ENDPOINT``; finding 111 covered
    argv ``--user-data-dir``. The config file hid both. Relative paths
    resolve against the leftover writer cwd. Unreadable / oversized /
    non-JSON stays unknown. ``remoteEndpoint`` is Playwright protocol,
    not CDP.
    """
    path_text = _flag_value(tokens, ("--config",))
    if not path_text:
        path_text = (environ.get("PLAYWRIGHT_MCP_CONFIG") or "").strip()
    text = (path_text or "").strip()
    if not text:
        return None, None
    path = Path(text)
    if not path.is_absolute():
        base = cwd if cwd is not None else Path.cwd()
        path = base / path
    try:
        if path.stat().st_size > _PLAYWRIGHT_MCP_CONFIG_MAX_BYTES:
            return None, None
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError, ValueError):
        return None, None
    if not isinstance(data, dict):
        return None, None
    browser = data.get("browser")
    if not isinstance(browser, dict):
        return None, None
    cdp = browser.get("cdpEndpoint")
    pinned = browser.get("userDataDir")
    if not isinstance(cdp, str) or not cdp.strip():
        cdp = None
    else:
        cdp = cdp.strip()
    if not isinstance(pinned, str) or not pinned.strip():
        pinned = None
    else:
        pinned = pinned.strip()
    return cdp, pinned


_LIGHTHOUSE_CLI_FLAGS_MAX_BYTES = 256 * 1024


def _lighthouse_cli_flags_path_aims_at_dock(
    tokens: List[str],
    profile: Optional[Path],
    dock_port: Optional[int],
    cwd: Optional[Path],
) -> bool:
    """True when leftover ``--cli-flags-path`` JSON aims at this dock.

    Official leftover: ``lighthouse URL --cli-flags-path=flags.json``
    with ``port`` / ``chromeFlags`` / ``chrome-flags``. Finding 126
    only checked argv ``--port`` / ``--chrome-flags``, so Take over
    left that writer running. yargs CLI flags override the file:
    a set argv ``--port`` (including ``0``) / ``--hostname`` /
    ``--chrome-flags`` wins that key. ``--config-path`` is audit
    config, not this file. Unreadable / oversized / non-JSON stays
    unknown. Relative paths resolve against the leftover writer cwd.
    """
    path_text = _flag_value(tokens, ("--cli-flags-path", "--cliFlagsPath"))
    text = (path_text or "").strip()
    if not text:
        return False
    path = Path(text)
    if not path.is_absolute():
        base = cwd if cwd is not None else Path.cwd()
        path = base / path
    try:
        if path.stat().st_size > _LIGHTHOUSE_CLI_FLAGS_MAX_BYTES:
            return False
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError, ValueError):
        return False
    if not isinstance(data, dict):
        return False
    if _flag_value(tokens, ("--hostname",)) is None:
        host = data.get("hostname")
        if isinstance(host, str) and host.strip() and not _is_loopback_cdp_host(host):
            return False
    if _flag_value(tokens, ("--port",)) is None:
        raw_port = data.get("port")
        port: Optional[int] = None
        if isinstance(raw_port, bool):
            port = None
        elif isinstance(raw_port, int):
            port = raw_port
        elif isinstance(raw_port, str) and raw_port.strip().isdigit():
            port = int(raw_port.strip())
        if port is not None and 1 <= port <= 65535:
            return dock_port is not None and port == dock_port
    if _flag_value_allow_leading_dash(
        tokens, ("--chrome-flags", "--chromeFlags"),
    ) is not None:
        return False
    raw_flags = data.get("chromeFlags")
    if not isinstance(raw_flags, str) or not raw_flags.strip():
        raw_flags = data.get("chrome-flags")
    if not isinstance(raw_flags, str):
        return False
    pinned = _user_data_dir_from_chrome_flags(raw_flags)
    return _leftover_profile_pin_aims_at_dock(pinned, profile, cwd)


def _user_data_dir_from_agent_browser_args(raw: Optional[str]) -> Optional[str]:
    """``--user-data-dir`` inside leftover ``--args`` / ``AGENT_BROWSER_ARGS``.

    Official leftover is comma or newline separated Chromium switches.
    A quoted space-delimited group is the same class as lighthouse
    ``--chrome-flags``. Chromium last-wins when the switch repeats.
    """
    text = (raw or "").strip()
    if not text:
        return None
    return _user_data_dir_from_chrome_flags(text.replace("\n", " ").replace(",", " "))


def _user_data_dir_from_chrome_flags(chrome_flags: Optional[str]) -> Optional[str]:
    """``--user-data-dir`` inside lighthouse ``--chrome-flags``.

    Official leftover is space-delimited Chrome switches. Lighthouse
    ``parseChromeFlags`` also strips wrapping quotes around the whole
    group (``execFile`` leftover). Chromium last-wins when the switch
    repeats. ``--remote-debugging-port`` here is not leftover attach
    (finding 92 is lighthouse ``--port``).
    """
    text = (chrome_flags or "").strip()
    if len(text) >= 2 and text[0] in "'\"" and text[-1] == text[0]:
        text = text[1:-1].strip()
    if not text:
        return None
    try:
        flag_tokens = shlex.split(text, posix=True)
    except ValueError:
        flag_tokens = text.split()
    from tools.bot_desktop.browser import _chromium_switch_value
    return _chromium_switch_value(flag_tokens, "user-data-dir")


def _expand_agent_browser_home_prefix(
    pinned: Optional[str],
    environ: Dict[str, str],
) -> Optional[str]:
    """Expand leftover agent-browser ``~/`` profile pins.

    Official leftover replaces a leading ``~/`` with ``os.homedir()``.
    Use that process ``HOME`` / ``USERPROFILE``, then this process home.
    ``~`` alone / ``~user/`` stay literal. Do not use for Chromium
    ``--user-data-dir`` (Chromium stores the path as written).
    """
    text = (pinned or "").strip()
    if not text.startswith("~/"):
        return pinned
    home = (environ.get("HOME") or "").strip()
    if not home:
        home = (environ.get("USERPROFILE") or "").strip()
    if not home:
        home = str(Path.home())
    return str(Path(home) / text[2:])


def _leftover_profile_pin_aims_at_dock(
    pinned: Optional[str],
    profile: Optional[Path],
    cwd: Optional[Path],
) -> bool:
    """True when a leftover profile-dir pin is *this* dock jar.

    Finding 99 resolved ``AGENT_BROWSER_PROFILE`` against the writer cwd.
    Finding 111 is the official argv twin (``--profile`` /
    ``--user-data-dir``). Empty / other / unreadable pins stay unknown.
    """
    text = (pinned or "").strip()
    if not text or profile is None:
        return False
    try:
        from tools.bot_desktop.browser import _paths_same_user_data_dir
        return _paths_same_user_data_dir(text, str(profile), cwd=cwd)
    except Exception:
        return False


def _singleton_lock_pid(user_data_dir: str) -> Optional[int]:
    try:
        target = os.readlink(os.path.join(user_data_dir, "SingletonLock"))
    except OSError:
        return None
    _host, _, pid_text = target.rpartition("-")
    if not pid_text.isdigit():
        return None
    pid = int(pid_text)
    return pid if pid > 1 else None


def _proc_ppid(pid: int) -> Optional[int]:
    try:
        with open(f"/proc/{pid}/status", encoding="utf-8") as fh:
            ppid = next((int(line.split()[1]) for line in fh if line.startswith("PPid:")), 0)
    except (OSError, ValueError):
        return None
    return ppid if ppid > 1 else None


def _inflight_dock_cli_pids() -> set:
    pids: set = set()
    with _inflight_dock_cli_lock:
        for entry in _inflight_dock_cli:
            proc = entry.get("proc")
            pid = getattr(proc, "pid", None)
            if type(pid) is int and pid > 1:
                pids.add(pid)
    return pids


def _live_scan_unregistered_dock_cli_allowed() -> bool:
    """Pytest must not process_iter the host — tests inject ``processes=``.

    ``lease.acquire(human)`` already runs ``stop_reserved_supervisors``. A
    remembered dock port of 9333 in a fence test would otherwise SIGKILL a
    developer's leftover ``agent-browser --cdp …:9333``.
    """
    return not os.environ.get("PYTEST_CURRENT_TEST")


def _known_unregistered_cli_homes() -> list[Optional[str]]:
    """Homes a ``home=None`` leftover scan must re-enter after multiplex.

    Inflight CLI / harness already walk per-entry homes. Unregistered
    ``terminal()`` leftover has no row. The 0.25s watch calls
    ``interrupt_unregistered_dock_cli()`` with no home — ambient is the
    launch bot, missing ``lease.json`` fail-opens as agent, and leftover
    ``python -m browser_use --cdp-url <sibling dock>`` kept typing.
    Session owners, cua backends, inflight / harness / supervisor homes,
    and served profiles are the same set leftover persist already walks.
    Unrecorded owner stays ambient-only.
    """
    homes: list[Optional[str]] = [None]
    try:
        homes.extend(
            h for h in _bt._session_owner_homes.values()
            if isinstance(h, str) and h
        )
    except Exception:
        pass
    try:
        from tools.computer_use.tool import _backend_homes
        homes.extend(h for h in _backend_homes.values() if isinstance(h, str) and h)
    except Exception:
        pass
    with _inflight_dock_cli_lock:
        for entry in _inflight_dock_cli:
            h = entry.get("home")
            if isinstance(h, str) and h:
                homes.append(h)
    with _reserved_dock_harness_lock:
        for entry in _reserved_dock_harness:
            h = entry.get("home")
            if isinstance(h, str) and h:
                homes.append(h)
    try:
        from tools.browser_supervisor import SUPERVISOR_REGISTRY
        from tools.browser_tool_supervisor_lease import _supervisor_home
        with SUPERVISOR_REGISTRY._lock:
            items = list(SUPERVISOR_REGISTRY._by_task.items())
        for raw_key, sup in items:
            stored_id = getattr(sup, "task_id", None)
            task_id = stored_id if isinstance(stored_id, str) and stored_id else raw_key
            h = _supervisor_home(sup, task_id if isinstance(task_id, str) else None)
            if isinstance(h, str) and h:
                homes.append(h)
    except Exception:
        pass
    try:
        from hermes_cli.profiles import profiles_to_serve
        for _name, path in profiles_to_serve(True):
            homes.append(str(path))
    except Exception:
        pass
    try:
        from tui_gateway.methods_display_watch import _served_profile_homes
        for path in list(_served_profile_homes):
            homes.append(str(path))
    except Exception:
        pass
    return homes


def _unique_hermes_homes(homes: list[Optional[str]]) -> list[Optional[str]]:
    from hermes_constants import hermes_home_key
    seen: set[str] = set()
    out: list[Optional[str]] = []
    for home in homes:
        key = hermes_home_key(home) if home else hermes_home_key()
        if key in seen:
            continue
        seen.add(key)
        out.append(home)
    return out


def interrupt_unregistered_dock_cli(
    home: Optional[str] = None,
    *,
    processes=None,
    chromium_pid: Optional[int] = None,
    owner_daemon_pid: Optional[int] = None,
) -> int:
    """PID-only SIGKILL leftover terminal-spawned agent-browser aimed at the dock.

    Finding 42 only tracks Hermes-spawned CLIs (``_spawn_and_collect`` /
    ``browser_exec``). ``terminal()`` ``agent-browser`` / ``npx agent-browser``
    and ``browser-use`` / ``uvx browser-use`` / ``npx playwright`` /
    ``npx lighthouse --port <dock>`` never enter ``_inflight_dock_cli``,
    so Take over would wait those writers out — leftover *action* in the
    field the human is typing into, the same class as leftover
    ``ws.send``. After Linux shebang the writer is
    ``node /path/agent-browser`` (or ``python3 …/browser-use``), not
    argv0. The bash ``-c`` parent is not the writer — PID-only kill of
    bash orphans the child. This is not a lease-gated terminal fence:
    ``terminal()`` still runs the CLI; Take over drops a dock-aimed leftover.

    Never ``killpg`` / tree-kill. Skip the shared Chromium and the daemon
    that spawned it (finding 40). Skip already-registered inflight PIDs
    (they already got the finding-42 SIGINT). Other Chrome ``--cdp 9222``,
    LAN CDP, and a sibling profile's jar stay up.

    ``home=None`` (watch / ``stop_reserved_supervisors``) re-enters each
    known leftover home. After a multiplex turn ambient is the launch
    bot; a human on a sibling must still drop writers aimed at that jar.
    An explicit ``home`` stays single-profile (in-process acquire).
    """
    homes = [home] if home else _unique_hermes_homes(_known_unregistered_cli_homes())
    shared = processes
    if shared is None:
        if not _live_scan_unregistered_dock_cli_allowed():
            return 0
        try:
            import psutil
            shared = list(psutil.process_iter(attrs=["pid"]))
        except Exception:
            return 0
    killed = 0
    skip_pids: set[int] = set()
    for candidate in homes:
        killed += _interrupt_unregistered_dock_cli_at(
            candidate,
            processes=shared,
            chromium_pid=chromium_pid,
            owner_daemon_pid=owner_daemon_pid,
            skip_pids=skip_pids,
        )
    return killed


def _interrupt_unregistered_dock_cli_at(
    home: Optional[str],
    *,
    processes,
    chromium_pid: Optional[int],
    owner_daemon_pid: Optional[int],
    skip_pids: set[int],
) -> int:
    from hermes_constants import hermes_home_key, reset_hermes_home_override, set_hermes_home_override
    from tools.bot_desktop import browser as _bd_browser
    from tools.bot_desktop import lease as _bd_lease

    token = None
    killed = 0
    try:
        if home:
            token = set_hermes_home_override(home)
        try:
            _bd_browser.persist_live_dock_cdp_port()
        except Exception:
            pass
        if not _bd_lease.human_holds():
            return 0
        try:
            profile = _bd_browser.profile_dir()
        except Exception:
            profile = None
        dock_port = None
        try:
            if profile is not None:
                dock_port = _bd_browser.running_instance_cdp_port(str(profile))
        except Exception:
            dock_port = None
        if dock_port is None:
            try:
                dock_port = _bd_browser.last_known_dock_cdp_port()
            except Exception:
                dock_port = None
        if dock_port is None:
            dock_port = _last_dock_cdp_port.get(hermes_home_key())
        live_chromium = chromium_pid
        live_daemon = owner_daemon_pid
        if live_chromium is None and profile is not None:
            live_chromium = _singleton_lock_pid(str(profile))
        if live_daemon is None and type(live_chromium) is int and live_chromium > 1:
            try:
                if _bd_browser._launched_by_session(live_chromium):
                    live_daemon = _proc_ppid(live_chromium)
            except Exception:
                live_daemon = None
        skip = {os.getpid(), os.getppid()} | skip_pids
        if type(live_chromium) is int and live_chromium > 1:
            skip.add(live_chromium)
        if type(live_daemon) is int and live_daemon > 1:
            skip.add(live_daemon)
        skip |= _inflight_dock_cli_pids()
        for proc in processes:
            try:
                pid = int(getattr(proc, "pid", 0) or 0)
            except (TypeError, ValueError):
                continue
            if pid <= 1 or pid in skip:
                continue
            try:
                raw_cmd = proc.cmdline() if callable(getattr(proc, "cmdline", None)) else None
            except Exception:
                raw_cmd = None
            if not raw_cmd:
                continue
            tokens = [str(t) for t in raw_cmd]
            if not _is_unregistered_dock_cli_invocation(tokens):
                continue
            try:
                environ = proc.environ() if callable(getattr(proc, "environ", None)) else {}
            except Exception:
                environ = {}
            cwd = None
            cwd_fn = getattr(proc, "cwd", None)
            if callable(cwd_fn):
                try:
                    raw_cwd = cwd_fn()
                    if raw_cwd:
                        cwd = Path(raw_cwd)
                except Exception:
                    cwd = None
            if cwd is None:
                try:
                    from tools.bot_desktop.browser import _proc_cwd
                    cwd = _proc_cwd(pid)
                except Exception:
                    cwd = None
            if not _unregistered_cli_aims_at_dock(
                tokens, environ or {}, profile, dock_port, cwd=cwd,
            ):
                continue
            try:
                killer = getattr(proc, "kill", None)
                if callable(killer):
                    killer()
                else:
                    os.kill(pid, signal.SIGKILL)
                killed += 1
                skip_pids.add(pid)
                skip.add(pid)
            except (ProcessLookupError, PermissionError, OSError):
                continue
            except Exception:
                _bt.logger.debug(
                    "unregistered dock CLI interrupt failed pid=%s", pid, exc_info=True,
                )
        return killed
    finally:
        if token is not None:
            reset_hermes_home_override(token)


_HARNESS_NAME_RE = re.compile(r"\A[A-Za-z0-9_-]{1,64}\Z")
_reserved_dock_harness_lock = threading.Lock()
_reserved_dock_harness: list[dict] = []


def register_reserved_dock_harness(
    name: str,
    home: Optional[str] = None,
    *,
    pid: Optional[int] = None,
    kill: Optional[Callable] = None,
) -> None:
    """Remember a browser-use harness daemon aimed at this profile's dock Chromium.

    The CLI process-group kill (finding 42) does not reach this daemon: it
    ``start_new_session``s out of the CLI, then stays CDP-connected after the
    command returns. Take over must drop that leftover client — same class as
    attach-only agent-browser (finding 40) — without tree-killing Chromium.
    """
    from hermes_constants import hermes_home_key
    from tools.browser_tool_supervisor_lease import install_supervisor_lease_hook

    if not name or not _HARNESS_NAME_RE.match(name):
        return
    install_supervisor_lease_hook()
    owner = str(home) if home else ""
    owner_key = hermes_home_key(owner or None)
    with _reserved_dock_harness_lock:
        for entry in _reserved_dock_harness:
            if entry.get("name") == name and entry.get("home_key") == owner_key:
                if pid is not None:
                    entry["pid"] = pid
                if kill is not None:
                    entry["kill"] = kill
                return
        _reserved_dock_harness.append({
            "name": name,
            "home": owner or None,
            "home_key": owner_key,
            "pid": pid,
            "kill": kill,
        })


def refresh_reserved_dock_harness_pid(name: str, home: Optional[str] = None) -> Optional[int]:
    """Resolve and store the live harness PID for a registered dock leftover."""
    from hermes_constants import hermes_home_key

    owner_key = hermes_home_key(home) if home is not None else hermes_home_key()
    with _reserved_dock_harness_lock:
        for entry in _reserved_dock_harness:
            if entry.get("name") != name or entry.get("home_key") != owner_key:
                continue
            pid = entry.get("pid") or _resolve_harness_daemon_pid(name)
            if pid:
                entry["pid"] = pid
            return pid
    return None


def _reset_reserved_dock_harness_for_tests() -> None:
    with _reserved_dock_harness_lock:
        _reserved_dock_harness.clear()


def _harness_pid_file_candidates(name: str) -> List[Path]:
    """Pid files the vendor daemon writes. Isolated ``bu.pid`` only when a runtime dir is set."""
    if not name or not _HARNESS_NAME_RE.match(name):
        return []
    dirs: list[Path] = []
    isolated = os.environ.get("BH_RUNTIME_DIR") or os.environ.get("BH_TMP_DIR")
    if isolated:
        dirs.append(Path(isolated))
    dirs.append(Path("/tmp") if os.name != "nt" else Path(os.environ.get("TEMP") or os.environ.get("TMP") or "/tmp"))
    try:
        import tempfile
        tmp = Path(tempfile.gettempdir())
        if tmp not in dirs:
            dirs.append(tmp)
    except Exception:
        pass
    xdg = Path(os.environ.get("XDG_CONFIG_HOME") or (Path.home() / ".config")) / "browser-harness" / "runtime"
    dirs.append(xdg)
    for key in ("BH_HOME", "BROWSER_HARNESS_HOME"):
        raw = os.environ.get(key)
        if raw:
            dirs.append(Path(raw) / "runtime")
    stems = [f"bu-{name}", name]
    if isolated:
        stems.append("bu")
    out: list[Path] = []
    seen = set()
    for directory in dirs:
        for stem in stems:
            path = directory / f"{stem}.pid"
            key = str(path)
            if key in seen:
                continue
            seen.add(key)
            out.append(path)
    return out


def _resolve_harness_daemon_pid(name: str) -> Optional[int]:
    """Live harness PID for ``BU_NAME``, or None. Never trusts an unverified pid file."""
    if not name or not _HARNESS_NAME_RE.match(name):
        return None
    try:
        from browser_harness import _ipc as _bipc
        pid = _bipc.identify(name)
        if type(pid) is int and 0 < pid < (1 << 31) and _verify_harness_daemon(pid):
            return pid
    except Exception:
        pass
    for path in _harness_pid_file_candidates(name):
        try:
            pid = int(path.read_text(encoding="utf-8").strip())
        except (OSError, ValueError):
            continue
        if type(pid) is int and 0 < pid < (1 << 31) and _verify_harness_daemon(pid):
            return pid
    return None


def _verify_harness_daemon(pid: int) -> bool:
    """True when ``pid`` is a browser-harness daemon, not a recycled/planted number."""
    try:
        import psutil
    except ImportError:
        return False
    try:
        proc = psutil.Process(pid)
        blob = f"{proc.name() or ''} {' '.join(proc.cmdline() or [])}".lower()
    except (psutil.NoSuchProcess, psutil.AccessDenied, OSError):
        return False
    return "browser_harness" in blob or "browser-harness" in blob


def _kill_harness_daemon_pid(pid: int) -> None:
    """PID-only kill. ``killpg`` / tree-kill would take a session-leader Chromium with it."""
    if type(pid) is not int or pid <= 0:
        return
    if not _verify_harness_daemon(pid):
        return
    if os.name == "nt":
        from hermes_cli._subprocess_compat import windows_hide_flags
        with contextlib.suppress(OSError, subprocess.SubprocessError):
            subprocess.run(
                ["taskkill", "/F", "/PID", str(pid)],
                stdin=subprocess.DEVNULL, capture_output=True, text=True,
                encoding="utf-8", errors="replace", timeout=10, check=False,
                creationflags=windows_hide_flags(),
            )
        return
    with contextlib.suppress(ProcessLookupError, PermissionError):
        os.kill(pid, signal.SIGKILL)


def interrupt_reserved_browser_harness(home: Optional[str] = None) -> None:
    """Drop leftover browser-use harness CDP clients aimed at a human-held dock.

    After ``browser_exec`` returns, the harness daemon stays attached and
    ``Target.setAutoAttach`` / Playwright input still land in the field the
    human is typing into. Finding 42 only kills the in-flight CLI; this
    daemon is a third leftover CDP client (findings 35–36, 40).
    """
    from hermes_constants import hermes_home_key, reset_hermes_home_override, set_hermes_home_override
    from tools.bot_desktop import lease as _bd_lease

    want = hermes_home_key(home) if home is not None else None
    victims: list[dict] = []
    with _reserved_dock_harness_lock:
        for entry in _reserved_dock_harness:
            owner_key = entry.get("home_key") or hermes_home_key()
            if want is not None and owner_key != want:
                continue
            owner = entry.get("home")
            token = None
            try:
                if owner:
                    token = set_hermes_home_override(owner)
                held = _bd_lease.human_holds()
            finally:
                if token is not None:
                    reset_hermes_home_override(token)
            if not held:
                continue
            victims.append(entry)
    for entry in victims:
        try:
            killer = entry.get("kill")
            pid = entry.get("pid") or _resolve_harness_daemon_pid(str(entry.get("name") or ""))
            if pid:
                entry["pid"] = pid
            if callable(killer):
                killer(pid)
            elif pid:
                _kill_harness_daemon_pid(pid)
        except Exception:
            _bt.logger.debug("reserved browser harness interrupt failed", exc_info=True)


def _cdp_url_is_bot_desktop_browser(cdp_url: str) -> bool:
    """True when ``cdp_url`` is this profile's live Bot Desktop Chromium.

    ``/browser connect`` and ``browser.cdp_url`` create a ``cdp_override``
    session with no ``local`` flag. If that URL is the dock (or agent-launched)
    instance on ``bot-desktop/browser-profile``, it is the same cookie jar a
    human types into — not "another browser".

    Unique-listen recover stays unknown when Chromium has several *specific*
    loopbacks. Persist can also miss (restart, worker, human ``lease.json``
    already on disk). The operator override still names the DevTools port —
    consult the same lock-pid + cmdline + listen guard persist uses. Do not
    treat every configured loopback as the dock.
    """
    want = _loopback_cdp_port(cdp_url)
    if want is None:
        return False
    from hermes_constants import hermes_home_key
    from tools.bot_desktop import browser as _bd_browser
    live = _bd_browser.running_instance_cdp_port(str(_bd_browser.profile_dir()))
    key = hermes_home_key()
    if live is not None:
        _last_dock_cdp_port[key] = live
        _bd_browser.remember_dock_cdp_port(live)
        return live == want
    remembered = _last_dock_cdp_port.get(key)
    if remembered is None:
        remembered = _bd_browser.last_known_dock_cdp_port()
        if remembered is not None:
            _last_dock_cdp_port[key] = remembered
    if remembered is not None and remembered == want:
        return True
    try:
        configured = _bd_browser._configured_listen_port_for_this_jar()
    except Exception:
        configured = None
    if configured is not None and configured == want:
        _last_dock_cdp_port[key] = configured
        _bd_browser.remember_dock_cdp_port(configured)
        return True
    return False


_LEASE_MOVED_ERROR = (
    "A human took over the bot's screen while this browser command ran; its result was "
    "discarded. Call computer_use action='wait_for_human' to block until they hand back."
)


def _refuse_shared_session_while_human_holds(*, cdp_url: str = "") -> None:
    """Refuse minting or joining the profile's shared local browser.

    ``_run_browser_command`` fences the *command*, but ``_get_session_info``
    launches and joins first. A leftover reserved session is already refused
    by the existing holder check; this covers the no-row path: dock CDP
    override, local Chromium, Lightpanda, and real-profile copies. Unrelated
    remote CDP and cloud sessions are left alone — they are not the bot's
    desktop jar.
    """
    if cdp_url:
        session_info: Dict[str, Any] = {"cdp_url": cdp_url, "features": {"cdp_override": True}}
    else:
        session_info = {"features": {"local": True}}
    if not _shares_bot_desktop_browser(session_info):
        return
    from tools.bot_desktop import lease as _bd_lease
    _bd_lease.assert_agent_may_act()


def _admit_shared_browser(
    session_info: Optional[Dict[str, Any]] = None,
    *,
    cdp_url: str = "",
    home: Optional[str] = None,
    treat_as_dock: bool = False,
):
    """Admit a call against the Bot Desktop's shared Chromium.

    Returns the lease snapshot when this *is* that browser (so the caller can
    discard a mid-flight result). Returns ``None`` for another browser.
    Raises ``HumanHasControl`` while a human holds.

    ``home`` re-enters the profile that owns the dock (leftover supervisors
    outlive a multiplex turn). ``treat_as_dock`` is mint-time identity: a
    leftover WS stamped as the dock stays the dock even after
    ``DevToolsActivePort`` disappears.
    """
    token = None
    if isinstance(home, str) and home:
        from hermes_constants import reset_hermes_home_override, set_hermes_home_override
        token = set_hermes_home_override(home)
    try:
        from tools.bot_desktop import lease as _bd_lease, runtime as _bd_runtime
        if treat_as_dock:
            if not (_bd_runtime.published_env().get("DISPLAY") or _bd_lease.human_holds()):
                return None
            return _stamp_admitted(_bd_lease.assert_agent_may_act())
        if session_info is None and cdp_url:
            session_info = {"cdp_url": cdp_url, "features": {"cdp_override": True}}
        if not session_info or not _shares_bot_desktop_browser(session_info):
            return None
        return _stamp_admitted(_bd_lease.assert_agent_may_act())
    finally:
        if token is not None:
            from hermes_constants import reset_hermes_home_override
            reset_hermes_home_override(token)


def _admit_resolved_cdp_for_attach(
    endpoint: str,
    *,
    home: Optional[str] = None,
    task_id: Optional[str] = None,
) -> bool:
    """True unless *endpoint* is the owner profile's dock and a human now holds.

    Discovery (``/json/version``) can outlive the start-of-call admit.
    Re-check the *resolved* WebSocket before ``get_or_start`` so a mid-resolve
    Take over cannot mint a leftover supervisor on the jar. Unrelated
    Chromes stay unfenced (admit returns None).

    After a multiplex turn the process home is the launch profile. Ambient
    admit then reads launch ``lease.json`` (agent, missing file) and leftover
    vault / ``browser_cdp`` / supervisor attach still talked to the bot jar.
    Re-enter ``home`` or ``_session_owner_homes[task_id]``. Unrecorded owner
    stays ambient. A human on the launch bot does not void a sibling attach.
    """
    from tools.bot_desktop.lease import HumanHasControl
    if not (isinstance(home, str) and home):
        home = _session_owner_home(task_id)
    try:
        _admit_shared_browser(cdp_url=endpoint, home=home)
        return True
    except HumanHasControl:
        return False


def _session_owner_home(*names: Optional[str]) -> Optional[str]:
    """HERMES_HOME that minted this task's session row, or None if unrecorded."""
    try:
        homes = _bt._session_owner_homes
    except Exception:
        return None
    seen: set[str] = set()
    for name in names:
        if not isinstance(name, str) or not name or name in seen:
            continue
        seen.add(name)
        owner = homes.get(name)
        if isinstance(owner, str) and owner:
            return owner
    return None


def _admit_task_shared_browser(task_id: Optional[str] = None, *, cdp_url: str = ""):
    """Admit the task's session, or a CDP override aimed at this profile's dock Chromium.

    Supervisor-only tools (vault fill, dialog accept) never go through
    ``_run_browser_command``; they still have to hit the same lease as click/eval.

    Re-enter the session owner's home. After a multiplex turn the process
    home is the launch profile; ambient ``assert_agent_may_act`` then
    reads launch's ``lease.json`` (agent, missing file) and leftover
    vault / dialog / ``browser_cdp`` / vision talked to the bot jar a
    human was typing into. Finding 101 scoped ``_run_browser_command``;
    this is the leftover-admit twin. An unrecorded owner stays ambient.
    """
    key = _bt._last_session_key(task_id or "default")
    info = _bt._active_sessions.get(key)
    if info is None and task_id:
        info = _bt._active_sessions.get(task_id)
    owner = _session_owner_home(task_id, key)
    raw = (cdp_url or "").strip()
    leftover_home: Optional[str] = None
    leftover_dock = False
    if not raw:
        try:
            raw = _cdp._get_cdp_override_raw()
        except Exception:
            raw = ""
    if not raw:
        # Leftover supervisor after the session row was dropped: still the dock
        # jar if it was stamped as THIS profile's Chromium at mint. Do not walk
        # another task's ``default`` leftover — the registry is keyed only by
        # task_id, so a sibling multiplex bot can leave one behind.
        raw, leftover_home, leftover_dock = _leftover_supervisor_identity(task_id, key)
    if info:
        admitted = _admit_shared_browser(info, home=owner)
        if admitted is not None:
            return admitted
    return _admit_shared_browser(
        cdp_url=raw, home=leftover_home or owner, treat_as_dock=leftover_dock,
    )


def _admit_supervisor(supervisor, *, home: Optional[str] = None):
    """Admit the jar this leftover CDP supervisor is actually attached to.

    Leftover I/O talks to ``supervisor.cdp_url``, not the current session's
    shared-browser row. After Take over, a same-profile leftover dock
    supervisor can still be ``SUPERVISOR_REGISTRY.get(task_id)`` while the
    current session is a cloud / other-profile / already-overridden row
    whose admit is a no-op. Re-run the dock fence on the leftover's own
    URL and ``targets_bot_desktop`` stamp so HumanHasControl / epoch
    discard apply to the jar we are about to snapshot / eval / CDP.
    """
    if supervisor is None:
        return None
    leftover_home = getattr(supervisor, "hermes_home", None)
    if not (isinstance(home, str) and home):
        home = leftover_home if isinstance(leftover_home, str) and leftover_home else None
    return _admit_shared_browser(
        cdp_url=str(getattr(supervisor, "cdp_url", "") or ""),
        home=home,
        treat_as_dock=getattr(supervisor, "targets_bot_desktop", None) is True,
    )


def _admit_leftover_io(supervisor, task_id: Optional[str] = None):
    """Admit leftover snapshot / eval / dialog / CDP against the leftover jar.

    The leftover jar first (it is what I/O talks to). If that leftover is
    not the dock, fall through to the task's current session so a local
    session row still fences unstamped leftovers that share this profile's
    Chromium.
    """
    admitted = _admit_supervisor(supervisor)
    if admitted is not None:
        return admitted
    return _admit_task_shared_browser(task_id)


def _stamp_admitted(lease):
    """Remember which HERMES_HOME this snapshot was read from.

    Leftover admit re-enters the minting profile, then resets the override
    before the caller sees the lease. Epoch checks must re-read THAT file,
    not the launch profile's ``lease.json``.
    """
    from hermes_constants import hermes_home_key
    lease._hermes_home = hermes_home_key()
    return lease


def _leftover_supervisor_identity(
    task_id: Optional[str], session_key: str,
) -> tuple:
    """This task's leftover supervisor on this profile, or empty.

    ``SUPERVISOR_REGISTRY`` is one map for the process. A sibling leftover
    stored as ``default`` is a different bot's jar — adopting it would admit
    that screen (and its lease) on this turn.
    """
    try:
        from hermes_constants import hermes_home_key
        from tools.browser_supervisor import SUPERVISOR_REGISTRY
    except Exception:
        return "", None, False
    candidates: List[str] = []
    for name in (task_id, session_key):
        if isinstance(name, str) and name and name not in candidates:
            candidates.append(name)
    if (not task_id or task_id == "default") and "default" not in candidates:
        candidates.append("default")
    here = hermes_home_key()
    for candidate in candidates:
        sup = SUPERVISOR_REGISTRY.get(candidate)
        if sup is None:
            continue
        home = getattr(sup, "hermes_home", None)
        owner = home if isinstance(home, str) and home else None
        if owner and hermes_home_key(owner) != here:
            continue
        url = str(getattr(sup, "cdp_url", "") or "")
        dock = getattr(sup, "targets_bot_desktop", None) is True
        if url or dock:
            return url, owner, dock
    return "", None, False


def _lease_moved_result(admitted) -> Optional[Dict[str, Any]]:
    """Refuse payload when the lease epoch moved after ``admitted``, else ``None``."""
    if admitted is None:
        return None
    from tools.bot_desktop import lease as _bd_lease
    home = getattr(admitted, "_hermes_home", None)
    current = _bd_lease.get(home if isinstance(home, str) and home else None)
    if current.epoch != admitted.epoch:
        return {"success": False, "code": "human_has_control", "error": _LEASE_MOVED_ERROR}
    return None


def _is_shared_bot_desktop_session(session_info: Dict[str, Any]) -> bool:
    """Same Chromium as the Bot Desktop dock / agent-browser profile, any transport."""
    if (session_info.get("features") or {}).get("local"):
        return True
    return _cdp_url_is_bot_desktop_browser(str(session_info.get("cdp_url") or ""))


def _shares_bot_desktop_browser(session_info: Dict[str, Any]) -> bool:
    """Decided by provenance, not transport: every LOCAL session (plain ``--session``, real-profile CDP
    attach, Lightpanda) is a browser Hermes launched with this profile's Bot Desktop DISPLAY, so it is the
    screen a human who took over is typing into. A CDP override that resolves to the same live
    dock/agent Chromium is that browser too. Cloud / unrelated user-CDP sessions are another browser.
    A human lease with the screen already gone (dead Xvnc) still fences — computer_use does the same."""
    if not _is_shared_bot_desktop_session(session_info):
        return False
    from tools.bot_desktop import lease as _bd_lease, runtime as _bd_runtime
    return bool(_bd_runtime.published_env().get("DISPLAY")) or _bd_lease.human_holds()


def _local_browser_reserved_by_human(session_info: Dict[str, Any]) -> bool:
    """Teardown of this session would kill the Chromium a human is typing into.

    The command fence refuses ``close`` while the human holds, but the inactivity
    janitor and suspect/expiry recycle still ran ``_release_session_resources``
    afterwards and tree-killed the agent-browser daemon — and the Chromium it
    spawned, which is the same jar the dock Browser uses. Cloud / unrelated
    user-CDP sessions are another browser and are not reserved.
    """
    if not _is_shared_bot_desktop_session(session_info):
        return False
    from tools.bot_desktop import lease as _bd_lease
    return _bd_lease.human_holds()


def _daemon_owns_shared_chromium(session_info: Dict[str, Any]) -> bool:
    """True when tree-killing this session's daemon would kill the dock Chromium."""
    name = str(session_info.get("session_name") or "")
    if not name:
        return False
    from tools.bot_desktop import browser as _bd_browser
    return _bd_browser.shared_chromium_owner_session() == name


def _bot_desktop_attach_port(session_info: Dict[str, Any]) -> Optional[int]:
    """DevTools port of a human-started Chromium on the Bot Desktop's shared profile, else ``None``."""
    if not _shares_bot_desktop_browser(session_info):
        return None
    from tools.bot_desktop import browser as _bd_browser
    return _bd_browser.running_instance_cdp_port(str(_bd_browser.profile_dir()),
                                                 exclude_session=session_info["session_name"])


def _run_browser_command_unfenced(task_id: str, command: str, args: List[str], timeout: int,
                                  _engine_override: Optional[str], browser_cmd, session_info: Dict[str, Any]) -> Dict[str, Any]:
    # Cleanup stops the supervisor before closing the backend; keep it stopped.
    if command != "close" and session_info.get("cdp_url"):
        _cdp._ensure_cdp_supervisor(task_id)

    # Cloud/CDP: ``--cdp <ws_url>`` (NEVER with --session: agent-browser >=0.13
    # would create a local browser and silently ignore --cdp). Local: ``--session <name>``.
    # Engine injection keys off the resolved session backend, not global provider
    # state: hybrid routing can create a local sidecar while a cloud provider stays configured.
    engine = _engine_override or _cloud._get_browser_engine()
    if session_info.get("cdp_url"):
        backend_args = ["--cdp", session_info["cdp_url"]]
    else:
        backend_args = ["--session", session_info["session_name"]]
        if (bd_port := _bot_desktop_attach_port(session_info)) is not None:
            # A Chromium already runs on the Bot Desktop's shared profile (the human clicked the dock's
            # Browser first): a launch would be forwarded into it by Chromium's singleton and die without
            # a DevTools endpoint, so the session's daemon attaches to the port it advertises instead.
            # Same daemon (keyed by --session) either way, so snapshot refs stay valid across commands.
            backend_args += ["--cdp", str(bd_port)]
        if _cloud._is_headed_mode():
            backend_args.append("--headed")
        if engine != "auto" and not _bt._is_camofox_mode():
            backend_args += ["--engine", engine]

    cmd_parts = _agent_browser_argv(browser_cmd) + backend_args + ["--json", command] + args

    try:
        result = _spawn_and_collect(task_id, session_info, cmd_parts, command, engine, timeout)
    except Exception as e:
        _bt.logger.warning("browser '%s' exception: %s", command, e, exc_info=True)
        result = {"success": False, "error": str(e)}

    # Lightpanda automatic Chrome fallback — runs for ALL exit paths (timeout,
    # empty, non-JSON, nonzero rc, parsed).
    fallback_reason = _lp._lightpanda_fallback_reason(engine, command, result)
    if fallback_reason:
        from tools.bot_desktop.lease import HumanHasControl

        try:
            _refuse_shared_session_while_human_holds()
        except HumanHasControl as e:
            return {"success": False, "error": str(e), "code": "human_has_control"}
        _bt.logger.info("Lightpanda fallback: retrying '%s' with Chrome (task=%s): %s", command, task_id, fallback_reason)
        if command == "screenshot":  # separate Chrome session to the same URL
            fallback_result = _lp._chrome_fallback_screenshot(task_id, args or [], timeout)
        else:
            fallback_result = _lp._run_chrome_fallback_command(task_id, command, args, timeout)
        return _lp._annotate_lightpanda_fallback(fallback_result, fallback_reason)

    return result
