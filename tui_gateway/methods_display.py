"""Bot Desktop JSON-RPC handlers: the Desktop app's door to a profile's headless screen.

``display.status`` reports runtime + lease; ``display.start`` / ``display.stop`` manage the Xvnc/Xfce
process; ``display.observe`` mints a single-use ticket the renderer redeems on ``/api/display/ws``
(``hermes_cli.web_routers.display``) to stream raw RFB; ``display.lease.acquire`` / ``release`` are
Take over / Hand back. ``display.install`` runs the distro package install on the gateway host: sudo
privilege is asked for through the masked ``display.install.sudo.request`` card (same ``_block`` bridge as
the terminal tool's sudo prompt), stdout streams as ``display.install.log`` and the run ends with
``display.install.done`` carrying a fresh status snapshot. Every handler is profile-scoped so a multiplexed gateway answers for the bot
the pane is looking at. Lease transitions fan out as the global ``display.lease`` event so every
connected client repaints (badge on the bot row, red border on the viewer, agent handoff prompt).

Bodies are rebound onto server.py's globals (method_ctx.bind_module) and reference them bare.
"""

import logging
import threading
import weakref

from .method_ctx import HandlerRegistry, bind_module

logger = logging.getLogger(__name__)
_registry = HandlerRegistry()
method = _registry.method
_profile_scoped = _registry.profile_scoped

_DISPLAY_ERR = 5300
_lease_listener_installed = threading.Event()


def _lease_view(lease) -> dict:
    """The lease as clients may see it. Redaction lives on ``Lease.public_view`` so RPC,
    tool results and CLI JSON cannot drift back to leaking the raw viewer id."""
    return lease.public_view()


def _display_snapshot() -> dict:
    from hermes_constants import hermes_home_key
    from tools.bot_desktop import lease as _bd_lease, runtime as _bd_runtime
    st = _bd_runtime.status()
    return {**st.as_dict(), "lease": _lease_view(_bd_lease.get()), "profile_key": hermes_home_key()}


def _install_lease_listener() -> None:
    """Once per process: broadcast every lease change to all connected clients."""
    if _lease_listener_installed.is_set():
        return
    from tools.bot_desktop import lease as _bd_lease

    def _on_change(profile_key: str, lease) -> None:
        _broadcast_global_event("display.lease", {"profile_key": profile_key, "lease": _lease_view(lease)})
    _bd_lease.on_change(_on_change)
    _lease_listener_installed.set()  # only once the subscription exists, or a failed import would silence every client


@method("display.status")
@_profile_scoped
def _(rid, params: dict) -> dict:
    _install_lease_listener()
    try:
        return _ok(rid, _display_snapshot())
    except Exception as e:
        return _err(rid, _DISPLAY_ERR, str(e))


@method("display.thumbnail")
@_profile_scoped
def _(rid, params: dict) -> dict:
    """One JPEG grab of the bot's screen (``data_url``: null while stopped). Read-only: no lease change.
    Suppressed while a human holds the lease — the frame may show what they are typing."""
    try:
        from tools.bot_desktop import lease as _bd_lease
        admitted = _bd_lease.get()
        if admitted.holder == _bd_lease.HUMAN:
            return _ok(rid, {"data_url": None, "suppressed": "human_has_control"})
        from tools.bot_desktop.thumbnail import thumbnail_data_url
        data_url = thumbnail_data_url()
        # Same epoch fence as computer_use capture: a takeover (or a full take-over /
        # hand-back cycle) during ImageGrab must not ship the frame the human typed on.
        if _bd_lease.get().epoch != admitted.epoch:
            return _ok(rid, {"data_url": None, "suppressed": "human_has_control"})
        return _ok(rid, {"data_url": data_url})
    except Exception as e:
        return _err(rid, _DISPLAY_ERR, str(e))


@method("display.start")
@_profile_scoped
def _(rid, params: dict) -> dict:
    _install_lease_listener()
    from tools.bot_desktop import runtime as _bd_runtime
    try:
        _bd_runtime.start()
        return _ok(rid, _display_snapshot())
    except Exception as e:
        return _err(rid, _DISPLAY_ERR, str(e))


@method("display.stop")
@_profile_scoped
def _(rid, params: dict) -> dict:
    from tools.bot_desktop import lease as _bd_lease, runtime as _bd_runtime
    try:
        _bd_lease.release()
        stopped = _bd_runtime.stop()
        return _ok(rid, {**_display_snapshot(), "stopped": stopped})
    except Exception as e:
        return _err(rid, _DISPLAY_ERR, str(e))


# viewer ids minted per (caller, profile). A multiplexed client must not take over
# bot B with an id observe minted for bot A on the same socket. The caller's
# identity is the WS-upgrade auth when present (survives /api/ws reconnect —
# a new WSTransport object is not a new person) and the transport object otherwise.
_minted_viewer_ids: "weakref.WeakKeyDictionary[object, dict[str, set[str]]]" = weakref.WeakKeyDictionary()
# Transports that cannot be weak-referenced (stdio / slotted / handle_request with no
# transport) still need a durable bucket: otherwise observe returns an id that acquire
# cannot recognise, and Take over from those callers would always fail closed.
_minted_fallback: dict[int, dict[str, set[str]]] = {}
# (provider, user_id) → profile → ids. Same authenticated Desktop after a
# socket replace must still be able to release the lease it holds.
_minted_by_auth: dict[tuple[str, str], dict[str, set[str]]] = {}


def _caller_minted_ids() -> set[str]:
    # get_hermes_home is on server.py; _profile_scoped has already bound the requested profile.
    profile = str(get_hermes_home())
    transport = current_transport()
    identity = getattr(transport, "auth_identity", None) if transport is not None else None
    if isinstance(identity, dict):
        user_id = str(identity.get("user_id") or "").strip()
        provider = str(identity.get("provider") or "").strip()
        if user_id and provider:
            return _minted_by_auth.setdefault((provider, user_id), {}).setdefault(profile, set())
    try:
        by_profile = _minted_viewer_ids.setdefault(transport, {})
    except TypeError:
        by_profile = _minted_fallback.setdefault(id(transport) if transport is not None else 0, {})
    return by_profile.setdefault(profile, set())


def _viewer_id_is_minted(viewer_id: str) -> bool:
    return bool(viewer_id) and viewer_id in _caller_minted_ids()


def _reset_minted_for_tests() -> None:
    _minted_viewer_ids.clear()
    _minted_fallback.clear()
    _minted_by_auth.clear()


def _mint_viewer_id(requested: str) -> str:
    """Server-minted viewer identity. ``requested`` is honoured only when THIS connection minted it
    earlier; anything else (including a holder id read off display.status) gets a fresh id."""
    mine = _caller_minted_ids()
    if requested in mine:
        return requested
    # Inline: bind_module rebinds this body onto server.py's globals, which do not import secrets.
    import secrets
    viewer_id = secrets.token_urlsafe(16)
    mine.add(viewer_id)
    return viewer_id


@method("display.observe")
@_profile_scoped
def _(rid, params: dict) -> dict:
    """Mint a single-use, 30 s ticket for ``/api/display/ws``. The ticket carries the profile home so
    the bridge dials THIS profile's RFB socket, and a server-minted viewer id (returned to the caller,
    who passes it to ``display.lease.acquire`` / ``release``) so the lease can name the holder."""
    from hermes_constants import get_hermes_home
    from hermes_cli.dashboard_auth.ws_tickets import mint_ticket
    from tools.bot_desktop import runtime as _bd_runtime
    try:
        if _bd_runtime.rfb_socket_path() is None:
            return _err(rid, _DISPLAY_ERR, "this profile's Bot Desktop is not running; call display.start first")
        viewer_id = _mint_viewer_id(str(params.get("viewer_id") or "").strip())
        ticket = mint_ticket(user_id=f"display:{viewer_id}", provider="bot-desktop",
                             extra={"hermes_home": str(get_hermes_home()), "viewer_id": viewer_id})
        return _ok(rid, {"ticket": ticket, "path": "/api/display/ws", "viewer_id": viewer_id,
                         **_display_snapshot()})
    except Exception as e:
        return _err(rid, _DISPLAY_ERR, str(e))


@method("display.install")
@_profile_scoped
def _(rid, params: dict) -> dict:
    """Start the package install in the background; the renderer follows ``display.install.log`` /
    ``display.install.done``. Refused while one is already running for this profile."""
    from hermes_constants import hermes_home_key
    from tools.bot_desktop import install as _bd_install, runtime as _bd_runtime
    if not _bd_runtime.is_supported_host():
        return _err(rid, _DISPLAY_ERR, "Bot Desktop runs on Linux gateway hosts only")
    if _bd_runtime.install_command() is None:
        return _err(rid, _DISPLAY_ERR, "no supported package manager (apt-get, dnf, pacman) on this host")
    profile_key = hermes_home_key()
    sid = str(params.get("session_id") or "")

    def _ask_password() -> str:
        return _block("display.install.sudo.request", sid, {"profile_key": profile_key}, timeout=300)

    def _line(text: str) -> None:
        _broadcast_global_event("display.install.log", {"profile_key": profile_key, "line": text})

    # The worker thread inherits NO context: the caller's transport (so the sudo card reaches the
    # CLIENT that clicked Install) and the profile scope `_profile_scoped` installed (so status, lock
    # and events all speak for the requested profile) are carried across with copy_context().
    import contextvars
    ctx = contextvars.copy_context()

    def _run() -> None:
        try:
            code = _bd_install.install_packages(ask_password=_ask_password, on_line=_line, claimed=True)
        except Exception as e:
            _line(f"install failed: {e}")
            code = 1
        _broadcast_global_event("display.install.done", {"profile_key": profile_key, "code": code,
                                                         "status": _display_snapshot()})

    try:
        _bd_install.claim()  # atomic: two fast clicks cannot both start a package manager
    except _bd_install.InstallBusy as e:
        return _err(rid, _DISPLAY_ERR, str(e))
    threading.Thread(target=ctx.run, args=(_run,), name=f"bot-desktop-install:{profile_key}", daemon=True).start()
    return _ok(rid, {"started": True, "command": _bd_runtime.install_command(), "profile_key": profile_key})


@method("display.lease.acquire")
@_profile_scoped
def _(rid, params: dict) -> dict:
    from tools.bot_desktop import lease as _bd_lease
    viewer_id = str(params.get("viewer_id") or "").strip()
    if not viewer_id:
        return _err(rid, _DISPLAY_ERR, "viewer_id required")
    # observe mints the id; acquire must not accept a client-invented string. A forged
    # id evicts the real holder and freezes the agent while nobody can type (the
    # forger's later observe mints a different id than the one now on the lease).
    if not _viewer_id_is_minted(viewer_id):
        return _err(rid, _DISPLAY_ERR, "viewer_id is not valid for this connection",
                    data={"code": "viewer_unminted"})
    lease = _bd_lease.acquire(viewer_id, reason=str(params.get("reason") or ""))
    return _ok(rid, {"lease": _lease_view(lease)})


@method("display.lease.release")
@_profile_scoped
def _(rid, params: dict) -> dict:
    from tools.bot_desktop import lease as _bd_lease
    viewer_id = str(params.get("viewer_id") or "").strip() or None
    # lease.release(None) skips the holder check; a client that lost its viewer id must not be able to
    # yank control from whoever holds it unless it says so explicitly (force).
    if viewer_id is None and not params.get("force") and _bd_lease.human_holds():
        return _err(rid, _DISPLAY_ERR, "viewer_id required to release another viewer's lease (or pass force: true)",
                    data={"code": "viewer_mismatch"})
    # Same mint gate as acquire: a stolen or cross-profile id must not yank the holder.
    # force remains the documented recovery when this window no longer has a minted id.
    if viewer_id is not None and not params.get("force") and not _viewer_id_is_minted(viewer_id):
        return _err(rid, _DISPLAY_ERR, "viewer_id is not valid for this connection",
                    data={"code": "viewer_unminted"})
    lease = _bd_lease.release(viewer_id)
    return _ok(rid, {"lease": _lease_view(lease)})


def register(server) -> None:
    bind_module(globals(), server, skip=("_",))
