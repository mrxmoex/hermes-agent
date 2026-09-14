"""Bot Desktop invariants: the byte-level RFB input gate follows the lease, and computer_use refuses
every action (capture included) while a human holds the screen."""

from __future__ import annotations

import json
import os

import pytest

from tools.bot_desktop import lease
from tools.bot_desktop.rfb_filter import RfbClientFilter

_HANDSHAKE = b"RFB 003.008\n" + b"\x01" + b"\x00"
_KEY = b"\x04\x01\x00\x00\x00\x00\x00\x61"          # KeyEvent 'a' down
# QEMU Extended KeyEvent (type 255, sub 0): what noVNC sends once Xvnc advertises the pseudo-encoding.
_QEMU_KEY = bytes([255, 0, 0, 1]) + (0x65).to_bytes(4, "big") + (0x12).to_bytes(4, "big")
_POINTER = b"\x05\x01\x00\x10\x00\x10"              # PointerEvent, button 1
_CUT = b"\x06\x00\x00\x00\x00\x00\x00\x02hi"        # ClientCutText "hi"
_FBUR = b"\x03\x00" + b"\x00" * 8                   # FramebufferUpdateRequest
_SETENC = b"\x02\x00\x00\x02" + b"\x00\x00\x00\x07" + b"\xff\xff\xff\x21"  # SetEncodings x2


@pytest.fixture(autouse=True)
def _fresh_lease():
    lease._reset_for_tests()
    yield
    lease._reset_for_tests()


def test_rfb_filter_forwards_input_only_from_the_lease_holder_across_arbitrary_chunking():
    f = RfbClientFilter(lambda: lease.viewer_may_send_input("v1"))
    head = f.feed(_HANDSHAKE)
    assert head[-1:] == b"\x01", "ClientInit is forced shared so a viewer never kicks the agent's watcher"

    # Agent holds: read-only messages pass, input is dropped, even when split byte by byte.
    stream = _KEY + _FBUR + _POINTER + _SETENC + _CUT + _QEMU_KEY
    out = b"".join(f.feed(stream[i:i + 1]) for i in range(len(stream)))
    assert out == _FBUR + _SETENC

    lease.acquire("v1")
    assert f.feed(_KEY + _POINTER + _QEMU_KEY) == _KEY + _POINTER + _QEMU_KEY

    lease.acquire("v2")  # last writer wins: v1 is evicted from input on the very next message
    assert f.feed(_KEY) == b""
    assert lease.release("v1").holder == lease.HUMAN, "a stale viewer's release must not yank control from v2"
    assert lease.release("v2").holder == lease.AGENT


def test_computer_use_refuses_every_action_while_a_human_holds_the_screen(monkeypatch):
    from tools.computer_use import tool

    calls = []
    monkeypatch.setattr(tool, "_get_backend", lambda session_id="": calls.append(session_id) or object())
    lease.acquire("human")
    for action in ("capture", "click", "type", "list_windows"):
        res = json.loads(tool.handle_computer_use({"action": action, "text": "pw"}))
        assert res["code"] == "human_has_control", action
    assert calls == [], "the driver is never touched while the human may be typing a credential"

    # Handoff round trip: the agent asks, the human takes over and hands back, the agent is unblocked.
    asked = json.loads(tool.handle_computer_use({"action": "request_handoff", "reason": "log in"}))
    assert asked["ok"] and asked["state"]["pending_handoff"] == "log in"
    assert asked["state"]["viewer_id"] is None
    assert "viewer_hash" in asked["state"]
    lease.acquire("human", reason="log in")
    assert lease.get().pending_handoff is None
    lease.release("human")
    done = json.loads(tool.handle_computer_use({"action": "wait_for_human", "seconds": 1}))
    assert done["ok"] and done["state"]["holder"] == lease.AGENT


def test_lease_authority_is_shared_across_processes(tmp_path):
    """The gateway that streams the screen and the process running the agent are different processes;
    a human takeover in one must refuse actions in the other."""
    import os
    import subprocess
    import sys

    lease.acquire("desktop-viewer")
    probe = ("import sys; sys.path.insert(0, %r)\n"
             "from tools.bot_desktop import lease\n"
             "try:\n    lease.assert_agent_may_act(); print('AGENT')\n"
             "except lease.HumanHasControl:\n    print('HUMAN')\n"
             "lease.release('desktop-viewer')\n") % os.getcwd()
    out = subprocess.run([sys.executable, "-c", probe], capture_output=True, text=True, encoding="utf-8", timeout=30,
                         stdin=subprocess.DEVNULL, env={**os.environ, "HERMES_HOME": os.environ["HERMES_HOME"]})
    assert out.stdout.strip() == "HUMAN", out.stderr
    assert lease.get().holder == lease.AGENT, "the other process's release is visible here"


def test_takeover_during_an_admitted_action_discards_its_result(monkeypatch):
    """Approval / backend start-up can take seconds; a human who takes over meanwhile must not have
    their keystrokes captured by an action admitted before they did."""
    from tools.computer_use import tool

    monkeypatch.setattr(tool, "_get_backend", lambda session_id="": object())

    def _dispatch_then_takeover(backend, action, args, **_):
        lease.acquire("human")  # a whole take-over / hand-back cycle inside the driver call:
        lease.release("human")  # control is back, but the frame is still the human's turn
        return json.dumps({"ok": True, "action": action, "png_b64": "SECRET"})

    monkeypatch.setattr(tool, "_dispatch", _dispatch_then_takeover)
    res = json.loads(tool.handle_computer_use({"action": "capture"}))
    assert res["code"] == "human_has_control" and "SECRET" not in json.dumps(res)


def test_takeover_handback_during_approval_does_not_start_the_device_op(monkeypatch):
    """A full take-over / hand-back during approval leaves holder=agent, so assert_agent_may_act
    succeeds. Input still belongs to the human's turn: compare admitted.epoch before _dispatch,
    not only after. Do not patch _dispatch — a recording backend must never be called."""
    from tools.computer_use import tool

    class Rec:
        def __init__(self):
            self.calls = []

        def click(self, **_kw):
            self.calls.append("click")
            return json.dumps({"ok": True, "action": "click"})

    rec = Rec()
    monkeypatch.setattr(tool, "_get_backend", lambda session_id="": rec)

    def _approval_cycles_the_lease(scope, args, session_id=""):
        lease.acquire("human")
        lease.release("human")
        return None

    monkeypatch.setattr(tool, "_request_approval", _approval_cycles_the_lease)
    res = json.loads(tool.handle_computer_use({"action": "click", "coordinate": [1, 1]}))
    assert rec.calls == []
    assert res.get("code") == "human_has_control"


def test_lease_file_is_owner_only_on_posix():
    """The on-disk id is a capability (release still accepts it in-process). The RFB
    socket is 0600; the lease file must not be the looser 0644 umask default."""
    import os
    import stat

    from hermes_constants import get_hermes_home

    lease.acquire("secret-viewer")
    path = get_hermes_home() / "bot-desktop" / "lease.json"
    assert path.is_file()
    if os.name != "posix":
        return
    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    assert stat.S_IMODE(path.parent.stat().st_mode) == 0o700


def test_public_view_replaces_the_raw_viewer_id_with_a_hash():
    """RPC, tool results and CLI JSON must share one redaction: the raw id is a capability."""
    import hashlib
    held = lease.Lease(holder=lease.HUMAN, viewer_id="secret-viewer")
    view = held.public_view()
    assert view["viewer_id"] is None
    assert view["holder"] == lease.HUMAN
    assert view["viewer_hash"] == hashlib.sha256(b"secret-viewer").hexdigest()[:12]
    assert held.as_dict()["viewer_id"] == "secret-viewer"
    assert lease.Lease().public_view()["viewer_hash"] is None


def test_unreadable_lease_file_fails_closed_and_takeover_keeps_the_agents_reason(tmp_path):
    """Missing file = fresh profile (agent). A file that exists but cannot be parsed must not read as
    "agent holds": a torn write must never let the agent act on a human's screen. Taking over after a
    request keeps the agent's reason so the human still sees WHY while they act."""
    from hermes_constants import hermes_home_key

    home = str(tmp_path)
    assert lease.get(profile_key=home).holder == lease.AGENT
    path = lease._path(home)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("{ torn", encoding="utf-8")
    assert lease.get(profile_key=home).holder == lease.HUMAN
    lease.release(profile_key=home)  # a successful write repairs it
    assert lease.get(profile_key=home).holder == lease.AGENT

    # Valid JSON of the wrong shape is just as untrustworthy as torn JSON: never read it as "agent holds".
    for wrong_shape in ("[]", "null", "5", "{}", '{"holder": "root"}'):
        path.write_text(wrong_shape, encoding="utf-8")
        assert lease.get(profile_key=home).holder == lease.HUMAN, wrong_shape
    lease.release(profile_key=home)
    assert lease.get(profile_key=home).holder == lease.AGENT

    # Unlinking a live human lease is the same fail-open as a missing file:
    # holder=agent without release(). Terminal auto-approve and Electron
    # rename/trash must not be able to do this silently.
    held = lease.acquire("desk-unlink", profile_key=home)
    assert held.holder == lease.HUMAN
    path.unlink()
    assert lease.get(profile_key=home).holder == lease.AGENT

    # ``ln -sf`` is the forge, not the drop: the path still exists, but
    # ``read_text`` follows a symlink to an agent-shaped lease. Approval
    # must pair ``ln``/``rsync`` the way it already pairs ``cp``.
    held = lease.acquire("desk-ln", profile_key=home)
    assert held.holder == lease.HUMAN
    forged = tmp_path / "forged-lease.json"
    forged.write_text(
        json.dumps({
            "holder": lease.AGENT,
            "viewer_id": None,
            "since": 0.0,
            "reason": "",
            "pending_handoff": None,
            "epoch": 99,
        }),
        encoding="utf-8",
    )
    staged = path.with_name("lease.link-tmp")
    staged.symlink_to(forged)
    os.replace(staged, path)
    assert path.is_symlink() and path.exists()
    assert lease.get(profile_key=home).holder == lease.AGENT

    lease.request_handoff("log in to the bank, 2FA on your phone", profile_key=home)
    held = lease.acquire("desk-1", profile_key=home)
    assert held.pending_handoff is None and held.reason == "log in to the bank, 2FA on your phone"
    assert hermes_home_key(home)  # sanity: the key derivation used by the bridge is available


def test_acquire_persists_dock_port_under_the_profile_key(monkeypatch, tmp_path):
    """Finding 65 stamped ``dock-cdp-port`` on acquire, but used the ambient
    home. A caller that writes another bot's ``lease.json`` via
    ``profile_key`` (Desktop / serve while this process is still the
    launch profile) then left the owner's file empty. After a DevTools
    miss leftover attach treated that jar as another Chrome.
    """
    from pathlib import Path

    from hermes_constants import get_hermes_home, reset_hermes_home_override, set_hermes_home_override
    import tools.bot_desktop.browser as bdb
    from tools.bot_desktop.lease import HumanHasControl, _path
    from tools.browser_tool_session import (
        _admit_resolved_cdp_for_attach,
        _admit_shared_browser,
        _cdp_url_is_bot_desktop_browser,
        _last_dock_cdp_port,
        _reset_dock_port_memory_for_tests,
    )

    launch = tmp_path / "launch"
    bot = tmp_path / "bot"
    launch.mkdir()
    bot.mkdir()
    dock = "ws://127.0.0.1:9333/devtools/browser/x"
    other = "ws://127.0.0.1:9222/devtools/browser/x"

    def live_port(*_a, **_k):
        return 9333 if Path(get_hermes_home()).resolve() == bot.resolve() else None

    monkeypatch.setattr(bdb, "running_instance_cdp_port", live_port)
    token = set_hermes_home_override(str(launch))
    try:
        _reset_dock_port_memory_for_tests()
        assert bdb.last_known_dock_cdp_port() is None
        held = lease.acquire("human-viewer", profile_key=str(bot))
        assert held.holder == lease.HUMAN
        asked = lease.request_handoff("Finish 2FA", profile_key=str(bot))
        assert asked.pending_handoff == "Finish 2FA"
        assert bdb.last_known_dock_cdp_port() is None
        assert not (launch / "bot-desktop" / "dock-cdp-port").exists()
    finally:
        reset_hermes_home_override(token)

    token = set_hermes_home_override(str(bot))
    try:
        _last_dock_cdp_port.clear()
        monkeypatch.setattr(bdb, "running_instance_cdp_port", lambda *_a, **_k: None)
        assert bdb.last_known_dock_cdp_port() == 9333
        assert _cdp_url_is_bot_desktop_browser(dock) is True
        assert _cdp_url_is_bot_desktop_browser(other) is False
        with pytest.raises(HumanHasControl):
            _admit_shared_browser(cdp_url=dock)
        assert _admit_resolved_cdp_for_attach(dock) is False
        assert _admit_shared_browser(cdp_url=other) is None
        assert _admit_resolved_cdp_for_attach(other) is True
    finally:
        reset_hermes_home_override(token)
        _reset_dock_port_memory_for_tests()
        for home in (launch, bot):
            for f in (_path(str(home)), _path(str(home)).with_suffix(".lock")):
                try:
                    f.unlink()
                except OSError:
                    pass


def test_request_handoff_persists_dock_port_before_devtools_miss(monkeypatch):
    """lease.request_handoff is the ask — stamp while DevTools is still
    readable. computer_use used to return from the action before persist,
    so a later miss leftover-attached as another Chrome."""
    import tools.bot_desktop.browser as bdb
    from tools.bot_desktop.lease import HumanHasControl
    from tools.browser_tool_session import (
        _admit_resolved_cdp_for_attach,
        _admit_shared_browser,
        _cdp_url_is_bot_desktop_browser,
        _last_dock_cdp_port,
        _reset_dock_port_memory_for_tests,
    )

    _reset_dock_port_memory_for_tests()
    dock = "ws://127.0.0.1:9333/devtools/browser/x"
    other = "ws://127.0.0.1:9222/devtools/browser/x"
    monkeypatch.setattr(bdb, "running_instance_cdp_port", lambda *a, **k: 9333)
    assert bdb.last_known_dock_cdp_port() is None
    asked = lease.request_handoff("Finish 2FA")
    assert asked.pending_handoff == "Finish 2FA"
    assert asked.holder == lease.AGENT
    assert bdb.last_known_dock_cdp_port() == 9333

    _last_dock_cdp_port.clear()
    monkeypatch.setattr(bdb, "running_instance_cdp_port", lambda *a, **k: None)
    lease.acquire("human-viewer")
    assert bdb.last_known_dock_cdp_port() == 9333
    assert _cdp_url_is_bot_desktop_browser(dock) is True
    assert _cdp_url_is_bot_desktop_browser(other) is False
    with pytest.raises(HumanHasControl):
        _admit_shared_browser(cdp_url=dock)
    assert _admit_resolved_cdp_for_attach(dock) is False
    assert _admit_shared_browser(cdp_url=other) is None
    assert _admit_resolved_cdp_for_attach(other) is True
    _reset_dock_port_memory_for_tests()


def test_request_handoff_survives_persist_failure(monkeypatch):
    """A disk error stamping dock-cdp-port must not drop the handoff ask."""
    monkeypatch.setattr(
        "tools.bot_desktop.browser.persist_live_dock_cdp_port",
        lambda: (_ for _ in ()).throw(RuntimeError("disk full")),
    )
    asked = lease.request_handoff("Finish 2FA")
    assert asked.pending_handoff == "Finish 2FA"
    assert asked.holder == lease.AGENT


def test_lease_works_without_fcntl(tmp_path):
    """Windows and fcntl-less hosts: ``computer_use`` imports the lease (via handoff) on EVERY call, so a
    module-level fcntl dependency turns every desktop action into ModuleNotFoundError there. The file
    semantics must still work; only the cross-process lock degrades. Subprocess so the module cache is clean."""
    import os
    import subprocess
    import sys

    probe = ("import sys; sys.modules['fcntl'] = None; sys.path.insert(0, %r)\n"
             "from tools.bot_desktop import lease\n"
             "import tools.computer_use.handoff\n"
             "assert lease.get().holder == lease.AGENT\n"
             "assert lease.acquire('v1').holder == lease.HUMAN\n"
             "assert lease.get().holder == lease.HUMAN\n"
             "assert lease.release('v1').holder == lease.AGENT\n"
             "print('OK')\n") % os.getcwd()
    out = subprocess.run([sys.executable, "-c", probe], capture_output=True, text=True, encoding="utf-8", timeout=60,
                         stdin=subprocess.DEVNULL, env={**os.environ, "HERMES_HOME": str(tmp_path)})
    assert out.stdout.strip() == "OK", out.stderr
