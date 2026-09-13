"""The dock's Browser and agent-browser resolve to one identity: same executable, same user-data-dir —
and when a human opened that browser first, the agent attaches to it instead of launching a second one
(Chromium's profile singleton would forward the launch and kill it without a DevTools endpoint)."""

from __future__ import annotations

import os
import socket

from tools.bot_desktop import browser, runtime


def test_dock_and_agent_share_browser_identity(tmp_path, monkeypatch):
    exe = tmp_path / "chrome"
    exe.write_text("#!/bin/sh\n", encoding="utf-8")
    exe.chmod(0o755)
    monkeypatch.setenv("AGENT_BROWSER_EXECUTABLE_PATH", str(exe))
    monkeypatch.delenv("AGENT_BROWSER_PROFILE", raising=False)
    monkeypatch.setattr(runtime, "state_dir", lambda: tmp_path / "bot-desktop")

    dock_exe, dock_profile = browser.dock_launch()
    agent_env = browser.env_for_agent({})

    assert dock_exe == agent_env["AGENT_BROWSER_EXECUTABLE_PATH"] == str(exe)
    assert dock_profile == agent_env["AGENT_BROWSER_PROFILE"] == str(tmp_path / "bot-desktop" / "browser-profile")


def test_user_pinned_profile_wins(tmp_path, monkeypatch):
    monkeypatch.setenv("AGENT_BROWSER_PROFILE", str(tmp_path / "mine"))
    monkeypatch.setattr(runtime, "state_dir", lambda: tmp_path / "bot-desktop")
    assert browser.profile_dir() == tmp_path / "mine"


def test_multiplex_override_does_not_inherit_the_launch_browser_pin(tmp_path, monkeypatch):
    """os.environ holds the launch profile's pin. A secondary bot under
    get_hermes_home_override must not share that cookie jar — the human may
    have typed a login into it."""
    from hermes_constants import get_hermes_home, reset_hermes_home_override, set_hermes_home_override

    launch = tmp_path / "launch"
    bot_b = tmp_path / "bot-b"
    launch.mkdir()
    bot_b.mkdir()
    pin = launch / "shared-jar"
    pin.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(launch))
    monkeypatch.setenv("AGENT_BROWSER_PROFILE", str(pin))

    assert browser.profile_dir() == pin

    token = set_hermes_home_override(str(bot_b))
    try:
        assert get_hermes_home() == bot_b
        scoped = bot_b / "bot-desktop" / "browser-profile"
        assert browser.profile_dir() == scoped
        env = browser.env_for_agent({"AGENT_BROWSER_PROFILE": str(pin)})
        assert env["AGENT_BROWSER_PROFILE"] == str(scoped)
    finally:
        reset_hermes_home_override(token)

    scoped_pin = bot_b / "custom-jar"
    scoped_pin.mkdir()
    monkeypatch.setenv("AGENT_BROWSER_PROFILE", str(scoped_pin))
    token = set_hermes_home_override(str(bot_b))
    try:
        assert browser.profile_dir() == scoped_pin
    finally:
        reset_hermes_home_override(token)


def test_dock_browser_advertises_a_devtools_port():
    """A human-started instance must be attachable, or the agent can never drive it afterwards."""
    import shlex
    parts = shlex.split(browser.dock_command("/opt/chrome", "/p/dir"))
    assert parts[0] == "/opt/chrome"
    assert parts[1] == "--user-data-dir=/p/dir"
    assert any(p.startswith("--remote-debugging-port=") for p in parts)


def test_dock_command_keeps_a_profile_dir_that_contains_spaces():
    """HERMES_HOME under a folder with spaces used to split Exec= into extra argv;
    the human then logged into a different jar than the bot."""
    import shlex
    profile = "/tmp/My Home/.hermes/bot-desktop/browser-profile"
    parts = shlex.split(browser.dock_command("/opt/Google Chrome/chrome", profile))
    assert parts[0] == "/opt/Google Chrome/chrome"
    assert parts[1] == f"--user-data-dir={profile}"


def _fake_running_instance(user_data_dir, pid: int, port: int) -> None:
    (user_data_dir / "DevToolsActivePort").write_text(f"{port}\n/devtools/browser/abc\n", encoding="utf-8")
    os.symlink(f"host-{pid}", user_data_dir / "SingletonLock")


def test_running_instance_port_requires_live_pid_and_open_port(tmp_path):
    listener = socket.socket()
    listener.bind(("127.0.0.1", 0))
    listener.listen(1)
    port = listener.getsockname()[1]
    try:
        _fake_running_instance(tmp_path, os.getpid(), port)
        assert browser.running_instance_cdp_port(str(tmp_path)) == port

        # Both files outlive a closed Chromium: a dead pid must not be trusted.
        os.unlink(tmp_path / "SingletonLock")
        os.symlink("host-2147483000", tmp_path / "SingletonLock")
        assert browser.running_instance_cdp_port(str(tmp_path)) is None
    finally:
        listener.close()
    # Live pid, port no longer accepting: still not attachable.
    os.unlink(tmp_path / "SingletonLock")
    os.symlink(f"host-{os.getpid()}", tmp_path / "SingletonLock")
    assert browser.running_instance_cdp_port(str(tmp_path)) is None
    assert browser.running_instance_cdp_port(str(tmp_path / "missing")) is None


def test_parse_proc_tcp_listen_ports_keeps_loopback_only():
    """IPv4 ``/proc/net/tcp``: 127.0.0.1 and 0.0.0.0 LISTEN; LAN / established stay out."""
    # 0100007F:2475 = 127.0.0.1:9333 LISTEN; 00000000:1F90 = 0.0.0.0:8080 LISTEN;
    # 0101A8C0:0050 = 192.168.1.1:80 LISTEN; 0100007F:0050 established (01).
    text = (
        "  sl  local_address rem_address   st\n"
        "   0: 0100007F:2475 00000000:0000 0A 00000000:00000000 00:00000000 00000000\n"
        "   1: 00000000:1F90 00000000:0000 0A 00000000:00000000 00:00000000 00000000\n"
        "   2: 0101A8C0:0050 00000000:0000 0A 00000000:00000000 00:00000000 00000000\n"
        "   3: 0100007F:0050 0100007F:1234 01 00000000:00000000 00:00000000 00000000\n"
    )
    assert browser._parse_proc_tcp_listen_ports(text) == {9333, 8080}


def test_parse_proc_tcp_listen_ports_filters_to_pid_socket_inodes():
    """Two loopback LISTENs in the netns table; only this pid's inode is kept.

    Column 10 is inode (proc(5)). A sibling Chrome's 9222 must not count as
    this jar's unique listen.
    """
    text = (
        "  sl  local_address rem_address   st tx_queue rx_queue tr tm retrnsmt uid timeout inode\n"
        "   0: 0100007F:23FB 00000000:0000 0A 00000000:00000000 00:00000000 00000000     0        0 111 1 0000000000000000 100 0 0 10 0\n"
        "   1: 0100007F:2406 00000000:0000 0A 00000000:00000000 00:00000000 00000000     0        0 222 1 0000000000000000 100 0 0 10 0\n"
    )
    assert browser._parse_proc_tcp_listen_ports(text) == {9211, 9222}
    assert browser._parse_proc_tcp_listen_ports(text, inodes={111}) == {9211}
    assert browser._parse_proc_tcp_listen_ports(text, inodes={222}) == {9222}
    assert browser._parse_proc_tcp_listen_ports(text, inodes={999}) == set()
    assert browser._parse_proc_tcp_listen_ports(text, inodes=set()) == set()


def test_loopback_listen_ports_fail_closed_without_fd_inodes(monkeypatch):
    """No /proc/pid/fd sockets → empty, even when the netns table looks unique."""
    monkeypatch.setattr(browser, "_proc_socket_inodes", lambda pid: set())
    assert browser._loopback_listen_ports_for_pid(os.getpid()) == set()


def test_loopback_listen_ports_are_pid_sockets_not_netns_table():
    """``/proc/<pid>/net/tcp`` is the netns table. Recover must not stamp VNC."""
    listener = socket.socket()
    listener.bind(("127.0.0.1", 0))
    listener.listen(1)
    port = listener.getsockname()[1]
    try:
        found = browser._loopback_listen_ports_for_pid(os.getpid())
        assert port in found
        unfiltered: set[int] = set()
        for name, ipv6 in (("tcp", False), ("tcp6", True)):
            path = f"/proc/{os.getpid()}/net/{name}"
            try:
                text = open(path, encoding="utf-8").read()
            except OSError:
                continue
            unfiltered |= browser._parse_proc_tcp_listen_ports(text, ipv6=ipv6)
        assert found <= unfiltered
        if len(unfiltered) > 1:
            assert found != unfiltered
    finally:
        listener.close()


def test_parse_proc_tcp6_listen_ports_keeps_loopback_only():
    # ::1:9333 and ::ffff:127.0.0.1:9222 LISTEN; a global LISTEN stays out.
    text = (
        "  sl  local_address                         remote_address                        st\n"
        "   0: 00000000000000000000000001000000:2475 00000000000000000000000000000000:0000 0A\n"
        "   1: 0000000000000000FFFF00000100007F:2406 00000000000000000000000000000000:0000 0A\n"
        "   2: 00000000000000000000000000000000:0050 00000000000000000000000000000000:0000 0A\n"
        "   3: 2A00DEAD000000000000000000000001:01BB 00000000000000000000000000000000:0000 0A\n"
    )
    assert browser._parse_proc_tcp_listen_ports(text, ipv6=True) == {9333, 9222, 80}


def test_remote_debugging_port_from_cmdline_ignores_ephemeral_zero():
    tokens = ["chrome", "--user-data-dir=/p", "--remote-debugging-port=0"]
    assert browser._remote_debugging_port_from_cmdline(tokens) is None
    assert browser._remote_debugging_port_from_cmdline(
        ["chrome", "--remote-debugging-port=9333"]
    ) == 9333


def test_running_instance_recovers_port_when_devtools_file_is_gone(tmp_path, monkeypatch):
    """Persist-never-ran + missing DevToolsActivePort still identifies this jar.

    Dock argv uses ``--remote-debugging-port=0``, so recovery is the unique
    loopback listen on the SingletonLock pid. A second listen stays unknown.
    """
    listener = socket.socket()
    listener.bind(("127.0.0.1", 0))
    listener.listen(1)
    port = listener.getsockname()[1]
    os.symlink(f"host-{os.getpid()}", tmp_path / "SingletonLock")
    monkeypatch.setattr(
        browser,
        "_chromium_cmdline_tokens",
        lambda pid: ["chrome", f"--user-data-dir={tmp_path}", "--remote-debugging-port=0"],
    )
    monkeypatch.setattr(browser, "_loopback_listen_ports_for_pid", lambda pid: {port})
    try:
        assert browser.running_instance_cdp_port(str(tmp_path)) == port
        monkeypatch.setattr(browser, "_loopback_listen_ports_for_pid", lambda pid: {port, port + 1})
        assert browser.running_instance_cdp_port(str(tmp_path)) is None
        monkeypatch.setattr(
            browser,
            "_chromium_cmdline_tokens",
            lambda pid: ["chrome", "--user-data-dir=/other/profile", "--remote-debugging-port=0"],
        )
        monkeypatch.setattr(browser, "_loopback_listen_ports_for_pid", lambda pid: {port})
        assert browser.running_instance_cdp_port(str(tmp_path)) is None
    finally:
        listener.close()


def test_running_instance_recovers_explicit_cmdline_port(tmp_path, monkeypatch):
    listener = socket.socket()
    listener.bind(("127.0.0.1", 0))
    listener.listen(1)
    port = listener.getsockname()[1]
    os.symlink(f"host-{os.getpid()}", tmp_path / "SingletonLock")
    monkeypatch.setattr(
        browser,
        "_chromium_cmdline_tokens",
        lambda pid: ["chrome", f"--user-data-dir={tmp_path}", f"--remote-debugging-port={port}"],
    )
    monkeypatch.setattr(browser, "_loopback_listen_ports_for_pid", lambda pid: {port, 22})
    try:
        # Explicit cmdline port wins even when several listens exist.
        assert browser.running_instance_cdp_port(str(tmp_path)) == port
    finally:
        listener.close()


def test_agent_attaches_to_human_started_browser(monkeypatch):
    """With a live dock instance on the shared profile the local argv carries ``--cdp <port>``; without one
    it stays a plain ``--session`` launch."""
    from tools import browser_tool_session as session

    monkeypatch.setattr(runtime, "published_env", lambda: {"DISPLAY": ":37"})
    monkeypatch.setattr(session._cloud, "_get_browser_engine", lambda: "auto")
    monkeypatch.setattr(session._cloud, "_is_headed_mode", lambda: False)
    monkeypatch.setattr(session, "_agent_browser_argv", lambda cmd: [cmd])
    argvs: list = []

    def spawn(task_id, session_info, cmd_parts, *rest):
        argvs.append(cmd_parts)
        return {"success": True}

    monkeypatch.setattr(session, "_spawn_and_collect", spawn)
    monkeypatch.setattr(session._lp, "_lightpanda_fallback_reason", lambda *a: None)
    info = {"session_name": "h_abc", "cdp_url": None, "features": {"local": True}}

    monkeypatch.setattr(browser, "running_instance_cdp_port", lambda d, **kw: 41234)
    session._run_browser_command_unfenced("t", "open", ["https://x"], 10, None, "agent-browser", info)
    assert argvs[-1][:5] == ["agent-browser", "--session", "h_abc", "--cdp", "41234"]

    monkeypatch.setattr(browser, "running_instance_cdp_port", lambda d, **kw: None)
    session._run_browser_command_unfenced("t", "open", ["https://x"], 10, None, "agent-browser", info)
    assert "--cdp" not in argvs[-1] and argvs[-1][:3] == ["agent-browser", "--session", "h_abc"]
