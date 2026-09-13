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
            admitted = _bd_lease.assert_agent_may_act()
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
        if _bd_lease.get().epoch != admitted.epoch:
            return {"success": False, "code": "human_has_control",
                    "error": "A human took over the bot's screen while this browser command ran; its result was "
                             "discarded. Call computer_use action='wait_for_human' to block until they hand back."}
        return result
    return _run_browser_command_unfenced(task_id, command, args, timeout, _engine_override, browser_cmd, session_info)


_LOOPBACK_CDP_HOSTS = frozenset({"127.0.0.1", "localhost", "::1", "0.0.0.0"})


def _loopback_cdp_port(url: str) -> Optional[int]:
    """Port if ``url`` is a loopback CDP endpoint, else ``None``.

    Remote / cloud CDP hosts are another browser and must not be compared to
    this profile's dock Chromium.
    """
    text = (url or "").strip()
    if not text:
        return None
    if text.isdigit():
        port = int(text)
        return port if 1 <= port <= 65535 else None
    from urllib.parse import urlparse
    parsed = urlparse(text if "://" in text else f"http://{text}")
    host = (parsed.hostname or "").lower()
    if host not in _LOOPBACK_CDP_HOSTS:
        return None
    port = parsed.port
    return port if port is not None and 1 <= port <= 65535 else None


# Last live dock DevTools port per profile. Take over can unlink
# DevToolsActivePort / SingletonLock while Chromium is still the jar the
# human is typing into; a live miss must not treat that remembered port as
# "another browser". A different loopback port stays another Chrome.
_last_dock_cdp_port: Dict[str, int] = {}


def _reset_dock_port_memory_for_tests() -> None:
    _last_dock_cdp_port.clear()
    try:
        from tools.bot_desktop import browser as _bd_browser
        _bd_browser._dock_port_path().unlink(missing_ok=True)
    except OSError:
        pass


def _cdp_url_is_bot_desktop_browser(cdp_url: str) -> bool:
    """True when ``cdp_url`` is this profile's live Bot Desktop Chromium.

    ``/browser connect`` and ``browser.cdp_url`` create a ``cdp_override``
    session with no ``local`` flag. If that URL is the dock (or agent-launched)
    instance on ``bot-desktop/browser-profile``, it is the same cookie jar a
    human types into — not "another browser".
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
    return remembered is not None and remembered == want


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


def _admit_task_shared_browser(task_id: Optional[str] = None, *, cdp_url: str = ""):
    """Admit the task's session, or a CDP override aimed at this profile's dock Chromium.

    Supervisor-only tools (vault fill, dialog accept) never go through
    ``_run_browser_command``; they still have to hit the same lease as click/eval.
    """
    key = _bt._last_session_key(task_id or "default")
    info = _bt._active_sessions.get(key)
    if info is None and task_id:
        info = _bt._active_sessions.get(task_id)
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
        admitted = _admit_shared_browser(info)
        if admitted is not None:
            return admitted
    return _admit_shared_browser(
        cdp_url=raw, home=leftover_home, treat_as_dock=leftover_dock,
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
