"""agent-browser session management: daemon spawn, per-backend session creation
(local/lightpanda/cdp/cloud), cached lookup, command execution + output interpretation.

Split out of ``tools/browser_tool.py``. Facade-owned state is read through ``_bt`` (``tools.browser_tool``, resolved per call) — no import cycle.
"""

import json
import logging
import os
import shutil
import subprocess
import uuid
from pathlib import Path
from typing import Any, Dict, List, Optional
from urllib.parse import urlparse

from hermes_cli._subprocess_compat import windows_hide_flags
from hermes_constants import get_hermes_home
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
    from tools.bot_desktop import lease as _bd_lease

    if _bd_lease.human_holds():
        raise _bd_lease.HumanHasControl(
            "A human holds the bot's screen; refusing to launch the shared local browser."
        )
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
    from tools.bot_desktop import lease as _bd_lease

    if _shares_bot_desktop_browser({"cdp_url": cdp_url}) and _bd_lease.human_holds():
        raise _bd_lease.HumanHasControl(
            "A human holds the bot's screen; refusing to attach to the shared local browser."
        )
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
        provider_name = type(provider).__name__
        _bt.logger.warning("Cloud provider %s failed (%s); attempting fallback to local Chromium for task %s",
                           provider_name, e, task_id, exc_info=True)
        from tools.bot_desktop import lease as _bd_lease
        if _bd_lease.human_holds():
            raise _bd_lease.HumanHasControl(
                "A human holds the bot's screen; refusing to fall back to the shared local browser."
            ) from e
        try:
            session_info = _create_local_session(task_id)
        except _bd_lease.HumanHasControl:
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
    cdp_override = _cdp._get_cdp_override()
    if cdp_override and not force_local:
        return _create_cdp_session(task_id, cdp_override)
    if force_local:
        return _create_local_session(task_id, allow_real_profile=False)
    provider = _cloud._get_cloud_provider()
    if provider is None:
        return _create_local_session(task_id)
    return _create_cloud_session_or_fallback(task_id, provider)


def _get_session_info(task_id: Optional[str] = None) -> Dict[str, Any]:
    """Get or create session info for a session key (thread-safe); also starts the
    inactivity thread and touches activity. A ``::local`` key forces local Chromium
    even with a cloud provider configured."""
    if task_id is None:
        task_id = "default"

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
    session_info = _create_session_for_key(task_id, force_local)

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
    if _defer_shared_browser_teardown(session_info):
        _bt.logger.warning(
            "Skipping timed-out discard of %s: a human holds the shared browser", task_id,
        )
        return
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

    A human on the shared Chromium must not lose that browser because an agent
    command timed out. Same class as the janitor defer: no tree-kill, no
    suspect mark (``ensure_healthy`` would treat a deferred cleanup as a miss
    and mint a second Chromium).
    """
    if _defer_shared_browser_teardown(session_info):
        _bt.logger.warning(
            "browser command timed out for %s but a human holds the shared browser; "
            "leaving the session in place", task_id,
        )
        return
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

    # Admit BEFORE session create/recycle. ``_get_session_info`` can launch real-profile
    # Chromium, spawn Lightpanda, attach a CDP supervisor, or tear down a suspect session
    # — all of that must stay off the shared screen while a human holds the lease.
    admitted, refuse = _shared_browser_fence(task_id)
    if refuse:
        return refuse
    admitted, session_info, refuse = _session_after_shared_fence(task_id, admitted)
    if refuse:
        return refuse
    result = _run_browser_command_unfenced(
        task_id, command, args, timeout, _engine_override, browser_cmd, session_info
    )
    stole = _discard_if_lease_moved(admitted)
    if stole:
        _discard_shared_browser_captures(command=command, args=args, result=result)
        return stole
    return result


_HUMAN_TOOK_OVER = (
    "A human took over the bot's screen while this browser command ran; its result was "
    "discarded. Call computer_use action='wait_for_human' to block until they hand back."
)


def _peek_active_session(task_id: str) -> Optional[Dict[str, Any]]:
    """Cached session dict, or None. Does not create, recycle, or attach anything."""
    with _bt._cleanup_lock:
        existing = _bt._active_sessions.get(task_id)
    return dict(existing) if existing is not None else None


def _non_nav_session_key(task_id: Optional[str] = None) -> str:
    """Session key a non-nav tool must fence and attach: last navigation, not the raw task id.

    Hybrid routing records ``{task}::local`` after a private-URL navigate. Fencing the
    bare task id predicted-cloud-skips (another browser) while the sidecar is this
    profile's Bot Desktop Chromium — the same skip that left ``admitted=None`` so a
    reminted sidecar write was delivered or rewritten.
    """
    return _bt._last_session_key(task_id or "default")


def _predicted_local_shared_browser(task_id: str) -> bool:
    """True when a *new* session for ``task_id`` would be the Bot Desktop Chromium.

    Mirrors ``_create_session_for_key`` precedence without launching: a CDP
    override is another browser unless the URL is this profile's live dock
    instance or this profile's real-profile Chrome; a configured cloud
    provider is another browser; everything else (including a sidecar
    ``::local`` key) lands on this profile's DISPLAY.
    """
    force_local = _bt._is_local_sidecar_key(task_id)
    if not force_local:
        override = _cdp._get_cdp_override_raw()
        if override:
            return _shares_bot_desktop_browser({"cdp_url": override})
        if _cloud._get_cloud_provider() is not None:
            return False
    return True


def _supervisor_cdp_url(task_id: str) -> str:
    """Live supervisor endpoint for ``task_id``, or empty. Does not start one."""
    try:
        from tools.browser_supervisor import SUPERVISOR_REGISTRY

        supervisor = SUPERVISOR_REGISTRY.get(task_id)
    except Exception:
        return ""
    if supervisor is None:
        return ""
    return str(getattr(supervisor, "cdp_url", "") or "")


def _dock_supervisor_session_info(task_id: str) -> Dict[str, Any]:
    """Session-info overlay when the live supervisor is this profile's shared Chromium.

    Supervisor-only paths (eval / dialog / CDP) never call
    ``_session_after_shared_fence``, so a predicted-cloud cache miss would
    otherwise leave the dock / real-profile unfenced. Foreign / cloud
    supervisors stay ``{}``.
    """
    cdp_url = _supervisor_cdp_url(task_id)
    if not cdp_url:
        return {}
    if _shares_bot_desktop_browser({"cdp_url": cdp_url}):
        return {"cdp_url": cdp_url}
    return {}


def _session_info_for_shared_browser_fence(task_id: str) -> Dict[str, Any]:
    """Session info for the lease bracket — never creates or recycles a session.

    Skip even a cache peek when this profile has no screen and no human lease
    (so existing Lightpanda unit tests do not start cleanup threads). If a
    screen is published or a human holds, peek the cache; on a miss assume
    local unless config already names a cloud / user-CDP backend. A failed
    cloud session can still fall back to local later — ``_run_browser_command``
    re-admits on that provenance.

    A live supervisor on the dock Chromium wins over a predicted-cloud miss
    (and over a cached non-shared label): evaluate / dialog / CDP talk that
    endpoint directly and never re-admit.
    """
    from tools.bot_desktop import lease as _bd_lease, runtime as _bd_runtime

    if not (_bd_runtime.published_env().get("DISPLAY") or _bd_lease.human_holds()):
        return {}
    cached = _peek_active_session(task_id)
    if cached is not None:
        if _shares_bot_desktop_browser(cached):
            return cached
        return _dock_supervisor_session_info(task_id) or cached
    dock = _dock_supervisor_session_info(task_id)
    if dock:
        return dock
    if _predicted_local_shared_browser(task_id):
        return {"features": {"local": True}}
    return {}


def _admit_bot_desktop_browser(session_info: Dict[str, Any]):
    """Admit a shared-browser run. Returns ``(lease_or_None, refuse_dict_or_None)``."""
    if not _shares_bot_desktop_browser(session_info):
        return None, None
    from tools.bot_desktop import lease as _bd_lease
    try:
        return _bd_lease.assert_agent_may_act(), None
    except _bd_lease.HumanHasControl as e:
        return None, {"success": False, "error": str(e), "code": "human_has_control"}


def _shared_browser_fence(task_id: str):
    """Admit the shared Bot Desktop browser for ``task_id`` (same pair as ``_admit``)."""
    return _admit_bot_desktop_browser(_session_info_for_shared_browser_fence(task_id))


def _session_after_shared_fence(task_id: str, admitted):
    """Create/reuse the session after a fence admit. Never remints a ticket already held.

    Predicted-cloud can still fall back to local Chromium; re-admit on that provenance.
    A human hold during create/fallback is ``code: human_has_control``, not a generic
    session-create failure — the agent must call ``wait_for_human``.
    """
    from tools.bot_desktop.lease import HumanHasControl

    try:
        session_info = _get_session_info(task_id)
    except HumanHasControl as e:
        return admitted, None, {"success": False, "error": str(e), "code": "human_has_control"}
    except Exception as e:
        _bt.logger.warning("Failed to create browser session for task=%s: %s", task_id, e)
        return admitted, None, {"success": False, "error": f"Failed to create browser session: {str(e)}"}
    if admitted is None and _shares_bot_desktop_browser(session_info):
        admitted, refuse = _admit_bot_desktop_browser(session_info)
        if refuse:
            return admitted, None, refuse
    return admitted, session_info, None


def _discard_if_lease_moved(admitted) -> Optional[Dict[str, Any]]:
    if admitted is None:
        return None
    from tools.bot_desktop import lease as _bd_lease
    if _bd_lease.get().epoch != admitted.epoch:
        return {"success": False, "code": "human_has_control", "error": _HUMAN_TOOK_OVER}
    return None


def _unlink_shared_capture(raw: str) -> None:
    """Unlink ``raw`` only when it resolves inside this profile's HERMES_HOME."""
    try:
        path = Path(raw).expanduser().resolve()
        home = Path(get_hermes_home()).resolve()
        if home not in path.parents and path.parent != home:
            return
        path.unlink(missing_ok=True)
    except Exception:
        return


def _discard_shared_browser_captures(
    *,
    command: Optional[str] = None,
    args: Optional[List[str]] = None,
    result: Optional[Dict[str, Any]] = None,
) -> None:
    """Unlink capture files a reminted run already wrote.

    ``computer_use`` fences before persist; browser screenshot writes the PNG
    first. A discarded tool result must not leave the human's frame on disk
    for a later ``read_file`` / ``MEDIA:`` path.
    """
    paths: List[str] = []
    data = (result or {}).get("data") if isinstance(result, dict) else None
    if isinstance(data, dict) and data.get("path"):
        paths.append(str(data["path"]))
    if isinstance(result, dict) and result.get("screenshot_path"):
        paths.append(str(result["screenshot_path"]))
    if command == "screenshot":
        for arg in args or []:
            if isinstance(arg, str) and arg.endswith((".png", ".jpg", ".jpeg", ".webp")):
                paths.append(arg)
    for raw in paths:
        _unlink_shared_capture(raw)


def _bracket_bot_desktop_browser(session_info: Dict[str, Any], run):
    """Refuse or discard a shared-browser result the same way ``computer_use`` does.

    Used by callers that already have session info (Lightpanda Chrome fallback,
    vault eval). ``_run_browser_command`` admits first, then creates the session,
    so a human hold cannot launch or recycle the shared Chromium.
    """
    admitted, refuse = _admit_bot_desktop_browser(session_info)
    if refuse:
        return refuse
    result = run()
    stole = _discard_if_lease_moved(admitted)
    if stole:
        if isinstance(result, dict):
            _discard_shared_browser_captures(result=result)
        return stole
    return result


def _defer_shared_browser_teardown(session_info: Optional[Dict[str, Any]]) -> bool:
    """True when janitor/recycle must not close the shared Chromium (human holds it)."""
    if not session_info or not _shares_bot_desktop_browser(session_info):
        return False
    from tools.bot_desktop import lease as _bd_lease
    return _bd_lease.human_holds()


def _shares_bot_desktop_browser(session_info: Dict[str, Any]) -> bool:
    """The shared Bot Desktop Chromium — by local provenance OR by endpoint identity.

    Every LOCAL session (plain ``--session``, real-profile CDP attach, Lightpanda)
    is a browser Hermes launched with this profile's DISPLAY. Cloud and a
    user-supplied CDP session are another browser — unless that CDP URL is this
    profile's live dock instance or this profile's real-profile Chrome
    (``/browser connect`` / ``browser.cdp_url`` still label that attach
    ``cdp_override``). Real-profile identity is the in-process cache / leftover
    session row, or a Chromium still running on this profile's snapshot copy
    dir after those rows die. A human lease with the screen already gone
    (dead Xvnc) still fences local sessions — computer_use does the same.
    """
    from tools.bot_desktop import lease as _bd_lease, runtime as _bd_runtime
    from tools.bot_desktop.browser import cdp_url_is_running_instance

    if (session_info.get("features") or {}).get("local"):
        return bool(_bd_runtime.published_env().get("DISPLAY")) or _bd_lease.human_holds()
    cdp_url = session_info.get("cdp_url")
    if not isinstance(cdp_url, str) or not cdp_url:
        return False
    return cdp_url_is_running_instance(cdp_url) or _cdp_is_this_profile_real_profile(cdp_url)


def _cdp_loopback_port(url: str) -> Optional[int]:
    """DevTools port when ``url`` is loopback HTTP/WS, else ``None``."""
    if not isinstance(url, str) or not url:
        return None
    parsed = urlparse(url)
    if parsed.hostname not in ("127.0.0.1", "localhost", "::1"):
        return None
    return parsed.port


def _cdp_is_this_profile_real_profile(cdp_url: str) -> bool:
    """True when ``cdp_url`` is this profile's consented real-profile Chrome.

    Cache stores the HTTP discovery root; callers often have the rewritten
    ``ws://`` URL on the same loopback port. Those rows are process-local —
    a surviving copy-dir Chrome (``_surviving_chrome_cdp``) outlives them, and
    ``/browser connect`` then labels the attach ``cdp_override``.
    """
    if not isinstance(cdp_url, str) or not cdp_url:
        return False
    rp = _bt._active_sessions.get(_bt._REAL_PROFILE_SESSION) or {}
    rp_cdp = str((_bt._real_profile_cdp_cache or {}).get("cdp") or rp.get("cdp_url") or "")
    if rp_cdp and _cdp_endpoints_match(cdp_url, rp_cdp):
        return True
    return _cdp_is_live_real_profile_copy(cdp_url)


def _real_profile_copy_dirs() -> List[Path]:
    """Hermes-owned real-profile snapshot dirs (``{HERMES_HOME}/browser-profile/<browser>``)."""
    root = Path(get_hermes_home()) / "browser-profile"
    try:
        return [p for p in root.iterdir() if p.is_dir()]
    except OSError:
        return []


def _cdp_is_live_real_profile_copy(cdp_url: str) -> bool:
    """True when ``cdp_url`` is Chromium still running on a snapshot copy dir.

    Same cheap probe as the dock (``DevToolsActivePort`` + live pid + connect),
    not ``agent-browser get cdp``. Foreign Chrome on another loopback port stays
    unshared even when a copy dir exists.
    """
    from tools.bot_desktop.browser import running_instance_cdp_port

    want = _cdp_loopback_port(cdp_url)
    if want is None:
        return False
    return any(running_instance_cdp_port(str(p)) == want for p in _real_profile_copy_dirs())


def _cdp_endpoints_match(left: str, right: str) -> bool:
    """Same CDP endpoint, including ``http://127.0.0.1:PORT`` vs rewritten ``ws://``.

    Real-profile cache stores the HTTP discovery root; ``/browser connect`` and
    ``_get_cdp_override`` rewrite it to a WebSocket URL on the same port.
    """
    if left == right:
        return True
    a, b = _cdp_loopback_port(left), _cdp_loopback_port(right)
    return a is not None and a == b


def _session_info_for_routed_cdp(cdp_url: str) -> Dict[str, Any]:
    """Session identity for a CDP URL the wrapper already resolved.

    Dock instance (endpoint identity) OR this profile's real-profile Chrome
    (keyed ``hermes-real-profile``; a cache hit never writes ``_active_sessions``
    under the caller key). Foreign / cloud URLs stay unshared. Do not consult
    a leftover dock supervisor — that would fence the wrong browser.
    """
    info: Dict[str, Any] = {"cdp_url": cdp_url or ""}
    if not cdp_url:
        return info
    if _shares_bot_desktop_browser(info):
        return info
    rp = _bt._active_sessions.get(_bt._REAL_PROFILE_SESSION) or {}
    rp_cdp = str((_bt._real_profile_cdp_cache or {}).get("cdp") or rp.get("cdp_url") or "")
    if rp_cdp and _cdp_endpoints_match(cdp_url, rp_cdp):
        if rp and _shares_bot_desktop_browser(rp):
            out = dict(rp)
            out["cdp_url"] = cdp_url
            return out
        return {"cdp_url": cdp_url, "features": {"local": True, "real_profile": True}}
    return info


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
        _bt.logger.info("Lightpanda fallback: retrying '%s' with Chrome (task=%s): %s", command, task_id, fallback_reason)
        if command == "screenshot":  # separate Chrome session to the same URL
            fallback_result = _lp._chrome_fallback_screenshot(task_id, args or [], timeout)
        else:
            fallback_result = _lp._run_chrome_fallback_command(task_id, command, args, timeout)
        return _lp._annotate_lightpanda_fallback(fallback_result, fallback_reason)

    return result
