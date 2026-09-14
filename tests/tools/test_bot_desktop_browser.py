"""The dock's Browser and agent-browser resolve to one identity: same executable, same user-data-dir —
and when a human opened that browser first, the agent attaches to it instead of launching a second one
(Chromium's profile singleton would forward the launch and kill it without a DevTools endpoint)."""

from __future__ import annotations

import os
import socket
import subprocess
import sys
import threading
from pathlib import Path

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


def _other_pid_loopback_listen():
    """Sibling process that LISTENs on an ephemeral loopback port."""
    return subprocess.Popen(
        [
            sys.executable,
            "-c",
            "import socket, time\n"
            "s = socket.socket()\n"
            "s.bind(('127.0.0.1', 0))\n"
            "s.listen(1)\n"
            "print(s.getsockname()[1], flush=True)\n"
            "time.sleep(30)\n",
        ],
        stdout=subprocess.PIPE,
        text=True,
    )


def test_running_instance_port_requires_live_pid_and_open_port(tmp_path, monkeypatch):
    listener = socket.socket()
    listener.bind(("127.0.0.1", 0))
    listener.listen(1)
    port = listener.getsockname()[1]
    monkeypatch.setattr(
        browser,
        "_chromium_cmdline_tokens",
        lambda pid: ["chrome", f"--user-data-dir={tmp_path}"],
    )
    try:
        _fake_running_instance(tmp_path, os.getpid(), port)
        assert browser.running_instance_cdp_port(str(tmp_path)) == port

        # Dead lock pid is not trusted by itself. A sibling occupying
        # the stale DevTools port is not this jar. This jar still
        # listening after the lock died is finding 161.
        os.unlink(tmp_path / "SingletonLock")
        os.symlink("host-2147483000", tmp_path / "SingletonLock")
        monkeypatch.setattr(
            browser,
            "_chromium_cmdline_tokens",
            lambda pid: ["chrome", "--user-data-dir=/other/profile"],
        )
        assert browser.running_instance_cdp_port(str(tmp_path)) is None
        monkeypatch.setattr(
            browser,
            "_chromium_cmdline_tokens",
            lambda pid: ["chrome", f"--user-data-dir={tmp_path}"],
        )
        assert browser.running_instance_cdp_port(str(tmp_path)) == port
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


def test_chromium_cmdline_accepts_spaced_user_data_dir_and_port():
    """Chromium accepts ``--switch value``. Equals-only parse missed the jar."""
    tokens = ["chrome", "--user-data-dir", "/p/dir", "--remote-debugging-port", "9333"]
    assert browser._user_data_dir_from_cmdline(tokens) == "/p/dir"
    assert browser._remote_debugging_port_from_cmdline(tokens) == 9333
    assert browser._remote_debugging_port_from_cmdline(
        ["chrome", "--remote-debugging-port", "0"]
    ) is None
    assert browser._user_data_dir_from_cmdline(
        ["chrome", "--user-data-dir", "--no-first-run"]
    ) is None
    assert browser._user_data_dir_from_cmdline(
        ["chrome", "--user-data-dir=/p/dir"]
    ) == "/p/dir"
    # Chromium last-wins. First scratch dir must not hide the dock.
    assert browser._user_data_dir_from_cmdline(
        ["chrome", "--user-data-dir=/scratch", "--user-data-dir=/p/dir"]
    ) == "/p/dir"
    assert browser._user_data_dir_from_cmdline(
        ["chrome", "--user-data-dir", "/scratch", "--user-data-dir=/p/dir"]
    ) == "/p/dir"
    assert browser._user_data_dir_from_cmdline(
        ["chrome", "--user-data-dir=/p/dir", "--user-data-dir", "/other"]
    ) == "/other"
    assert browser._remote_debugging_port_from_cmdline(
        ["chrome", "--remote-debugging-port=0", "--remote-debugging-port=9333"]
    ) == 9333
    assert browser._remote_debugging_port_from_cmdline(
        ["chrome", "--remote-debugging-port=9333", "--remote-debugging-port=0"]
    ) is None
    # Single-dash is a Chromium switch prefix. ``--`` ends switch parse.
    assert browser._user_data_dir_from_cmdline(
        ["chrome", "-user-data-dir=/p/dir"]
    ) == "/p/dir"
    assert browser._user_data_dir_from_cmdline(
        ["chrome", "-user-data-dir", "/p/dir", "-remote-debugging-port", "9333"]
    ) == "/p/dir"
    assert browser._remote_debugging_port_from_cmdline(
        ["chrome", "-remote-debugging-port=9333"]
    ) == 9333
    assert browser._user_data_dir_from_cmdline(
        ["chrome", "--user-data-dir=/scratch", "-user-data-dir=/p/dir"]
    ) == "/p/dir"
    assert browser._user_data_dir_from_cmdline(
        ["chrome", "--user-data-dir=/p/dir", "--", "--user-data-dir=/other"]
    ) == "/p/dir"
    assert browser._user_data_dir_from_cmdline(
        ["chrome", "--", "--user-data-dir=/p/dir"]
    ) is None


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


def test_connect_hosts_for_listen_ip_stay_in_family():
    """A ::1-only dock must not probe 127.0.0.1 (sibling squat / miss)."""
    assert browser._connect_hosts_for_listen_ip("::1") == ("::1",)
    assert browser._connect_hosts_for_listen_ip("127.0.0.1") == ("127.0.0.1",)
    assert browser._connect_hosts_for_listen_ip("0.0.0.0") == ("127.0.0.1",)
    assert browser._connect_hosts_for_listen_ip("::") == ("::1",)
    assert browser._connect_hosts_for_listen_ip("::ffff:127.0.0.1") == ("127.0.0.1",)


def test_running_instance_recovers_ipv6_only_listen(tmp_path, monkeypatch):
    """Dock on [::1] only: IPv4 create_connection misses; connect the listen."""
    listener = socket.socket(socket.AF_INET6, socket.SOCK_STREAM)
    listener.setsockopt(socket.IPPROTO_IPV6, socket.IPV6_V6ONLY, 1)
    listener.bind(("::1", 0))
    listener.listen(1)
    port = listener.getsockname()[1]
    os.symlink(f"host-{os.getpid()}", tmp_path / "SingletonLock")
    monkeypatch.setattr(
        browser,
        "_chromium_cmdline_tokens",
        lambda pid: ["chrome", f"--user-data-dir={tmp_path}", "--remote-debugging-port=0"],
    )
    monkeypatch.setattr(browser, "_loopback_listen_ports_for_pid", lambda pid: {port})
    monkeypatch.setattr(browser, "_loopback_listen_targets_for_pid", lambda pid: {("::1", port)})
    try:
        assert browser.running_instance_cdp_port(str(tmp_path)) == port
    finally:
        listener.close()


def test_running_instance_does_not_stamp_ipv4_squat_for_ipv6_listen(tmp_path, monkeypatch):
    """Unique ::1 listen + sibling on 127.0.0.1:same must not become the dock."""
    v6 = socket.socket(socket.AF_INET6, socket.SOCK_STREAM)
    v6.setsockopt(socket.IPPROTO_IPV6, socket.IPV6_V6ONLY, 1)
    v6.bind(("::1", 0))
    v6.listen(1)
    port = v6.getsockname()[1]
    v4 = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    v4.bind(("127.0.0.1", port))
    v4.listen(1)
    os.symlink(f"host-{os.getpid()}", tmp_path / "SingletonLock")
    monkeypatch.setattr(
        browser,
        "_chromium_cmdline_tokens",
        lambda pid: ["chrome", f"--user-data-dir={tmp_path}", "--remote-debugging-port=0"],
    )
    monkeypatch.setattr(browser, "_loopback_listen_ports_for_pid", lambda pid: {port})
    monkeypatch.setattr(browser, "_loopback_listen_targets_for_pid", lambda pid: {("::1", port)})
    try:
        assert browser.running_instance_cdp_port(str(tmp_path)) == port
        v6.close()
        # IPv4 squat still accepts. Connecting 127.0.0.1 would stamp it.
        assert browser.running_instance_cdp_port(str(tmp_path)) is None
    finally:
        v4.close()
        try:
            v6.close()
        except OSError:
            pass


def test_devtools_file_does_not_stamp_ipv4_squat_for_ipv6_listen(tmp_path, monkeypatch):
    """DevToolsActivePort still used 127.0.0.1 first (finding 81 only fixed recover).

    Finding 165: lock pid still lists this number after ::1 dies. Do not
    fall through to inode holders that still accept on 127.0.0.1.
    """
    v6 = socket.socket(socket.AF_INET6, socket.SOCK_STREAM)
    v6.setsockopt(socket.IPPROTO_IPV6, socket.IPV6_V6ONLY, 1)
    v6.bind(("::1", 0))
    v6.listen(1)
    port = v6.getsockname()[1]
    v4 = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    v4.bind(("127.0.0.1", port))
    v4.listen(1)
    (tmp_path / "DevToolsActivePort").write_text(
        f"{port}\n/devtools/browser/abc\n", encoding="utf-8")
    os.symlink(f"host-{os.getpid()}", tmp_path / "SingletonLock")
    monkeypatch.setattr(
        browser,
        "_chromium_cmdline_tokens",
        lambda pid: ["chrome", f"--user-data-dir={tmp_path}"],
    )
    monkeypatch.setattr(browser, "_loopback_listen_targets_for_pid", lambda pid: {("::1", port)})
    try:
        assert browser.running_instance_cdp_port(str(tmp_path)) == port
        v6.close()
        assert browser.running_instance_cdp_port(str(tmp_path)) is None
    finally:
        v4.close()
        try:
            v6.close()
        except OSError:
            pass


def test_unique_recoverable_listen_drops_unspecified_extra():
    """127.0.0.1 DevTools + 0.0.0.0 sibling is the dock, not ambiguity."""
    assert browser._unique_recoverable_listen_port({("127.0.0.1", 9333)}) == 9333
    assert browser._unique_recoverable_listen_port({
        ("127.0.0.1", 9333), ("0.0.0.0", 5555),
    }) == 9333
    assert browser._unique_recoverable_listen_port({
        ("::1", 9333), ("::", 5555),
    }) == 9333
    assert browser._unique_recoverable_listen_port({
        ("127.0.0.1", 9333), ("::1", 9444),
    }) is None
    assert browser._unique_recoverable_listen_port({
        ("0.0.0.0", 9333), ("0.0.0.0", 5555),
    }) is None


def test_running_instance_recovers_specific_loopback_when_unspecified_extra(
    tmp_path, monkeypatch,
):
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
    monkeypatch.setattr(browser, "_loopback_listen_ports_for_pid", lambda pid: {port, 5555})
    monkeypatch.setattr(
        browser,
        "_loopback_listen_targets_for_pid",
        lambda pid: {("127.0.0.1", port), ("0.0.0.0", 5555)},
    )
    try:
        assert browser.running_instance_cdp_port(str(tmp_path)) == port
        monkeypatch.setattr(
            browser,
            "_loopback_listen_targets_for_pid",
            lambda pid: {("127.0.0.1", port), ("::1", port + 1)},
        )
        monkeypatch.setattr(browser, "_loopback_listen_ports_for_pid", lambda pid: {port, port + 1})
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


def test_paths_same_user_data_dir_resolves_relative_against_pid_cwd(tmp_path):
    """Cmdline ``--user-data-dir`` is relative to Chromium's cwd, not ours."""
    profile = tmp_path / "bot-desktop" / "browser-profile"
    profile.mkdir(parents=True)
    cwd = tmp_path / "bot-desktop"
    assert browser._paths_same_user_data_dir("browser-profile", str(profile), cwd=cwd)
    assert browser._paths_same_user_data_dir("./browser-profile", str(profile), cwd=cwd)
    assert browser._paths_same_user_data_dir(".", str(profile), cwd=profile)
    assert not browser._paths_same_user_data_dir(
        "browser-profile", str(profile), cwd=tmp_path)
    assert browser._paths_same_user_data_dir(str(profile), str(profile))


def test_running_instance_recovers_relative_user_data_dir(tmp_path, monkeypatch):
    """Relative cmdline dir resolved against this process cwd missed the jar.

    A wrapper ``cd ~/.hermes/bot-desktop && chrome --user-data-dir=browser-profile``
    is still this cookie jar. Admit None → leftover HTTP on the human hold.
    """
    profile = tmp_path / "jar"
    profile.mkdir()
    listener = socket.socket()
    listener.bind(("127.0.0.1", 0))
    listener.listen(1)
    port = listener.getsockname()[1]
    os.symlink(f"host-{os.getpid()}", profile / "SingletonLock")
    monkeypatch.setattr(
        browser,
        "_chromium_cmdline_tokens",
        lambda pid: ["chrome", "--user-data-dir=jar", "--remote-debugging-port=0"],
    )
    monkeypatch.setattr(browser, "_proc_cwd", lambda pid: tmp_path)
    monkeypatch.setattr(browser, "_loopback_listen_ports_for_pid", lambda pid: {port})
    try:
        assert browser.running_instance_cdp_port(str(profile)) == port
        monkeypatch.setattr(browser, "_proc_cwd", lambda pid: Path("/tmp"))
        assert browser.running_instance_cdp_port(str(profile)) is None
    finally:
        listener.close()


def test_persist_configured_listen_accepts_relative_user_data_dir(tmp_path, monkeypatch):
    """Configured listen must not fail the jar check on a relative cmdline dir."""
    listener = socket.socket()
    listener.bind(("127.0.0.1", 0))
    listener.listen(1)
    port = listener.getsockname()[1]
    profile = tmp_path / "browser-profile"
    profile.mkdir()
    os.symlink(f"host-{os.getpid()}", profile / "SingletonLock")
    monkeypatch.setattr(runtime, "state_dir", lambda: tmp_path)
    monkeypatch.setattr(browser, "profile_dir", lambda: profile)
    monkeypatch.setattr(browser, "running_instance_cdp_port", lambda *a, **k: None)
    monkeypatch.setattr(
        browser,
        "_chromium_cmdline_tokens",
        lambda pid: ["chrome", "--user-data-dir=browser-profile"],
    )
    monkeypatch.setattr(browser, "_proc_cwd", lambda pid: tmp_path)
    monkeypatch.setattr(browser, "_loopback_listen_ports_for_pid", lambda pid: {port})
    monkeypatch.setattr(
        browser, "_configured_cdp_override_url", lambda: f"http://127.0.0.1:{port}",
    )
    try:
        assert browser.persist_live_dock_cdp_port() == port
        assert browser.last_known_dock_cdp_port() == port
    finally:
        listener.close()


def test_proc_env_value_reads_child_environ():
    """``/proc/<pid>/environ`` is how Chromium's ``CHROME_USER_DATA_DIR`` is found.

    ``os.environ`` mutations do not rewrite ``/proc/self/environ`` — spawn a
    child that actually received the key.
    """
    import subprocess

    child = subprocess.Popen(
        ["sleep", "30"],
        env={**os.environ, "CHROME_USER_DATA_DIR": "/tmp/hermes-dock-jar-env"},
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    try:
        assert browser._proc_env_value(child.pid, "CHROME_USER_DATA_DIR") == "/tmp/hermes-dock-jar-env"
        assert browser._proc_env_value(child.pid, "HERMES_BD_TEST_ENV_MISSING") is None
        assert browser._proc_env_value(-1, "PATH") is None
    finally:
        child.kill()
        child.wait()


def test_running_instance_recovers_chrome_user_data_dir_env(tmp_path, monkeypatch):
    """Chromium's ``CHROME_USER_DATA_DIR`` is the jar when the flag is absent.

    Official override. Recover that only read argv then treated this live
    dock as another Chrome (admit None → leftover HTTP on a human hold).
    A conflicting flag still wins. Another env dir must not match.
    """
    profile = tmp_path / "jar"
    profile.mkdir()
    listener = socket.socket()
    listener.bind(("127.0.0.1", 0))
    listener.listen(1)
    port = listener.getsockname()[1]
    os.symlink(f"host-{os.getpid()}", profile / "SingletonLock")
    monkeypatch.setattr(
        browser,
        "_chromium_cmdline_tokens",
        lambda pid: ["chrome", "--remote-debugging-port=0"],
    )
    monkeypatch.setattr(
        browser,
        "_proc_env_value",
        lambda pid, name: str(profile) if name == "CHROME_USER_DATA_DIR" else None,
    )
    monkeypatch.setattr(browser, "_loopback_listen_ports_for_pid", lambda pid: {port})
    try:
        assert browser.running_instance_cdp_port(str(profile)) == port
        monkeypatch.setattr(
            browser,
            "_proc_env_value",
            lambda pid, name: str(tmp_path / "other") if name == "CHROME_USER_DATA_DIR" else None,
        )
        assert browser.running_instance_cdp_port(str(profile)) is None
        monkeypatch.setattr(
            browser,
            "_chromium_cmdline_tokens",
            lambda pid: ["chrome", "--user-data-dir=/other/profile", "--remote-debugging-port=0"],
        )
        monkeypatch.setattr(
            browser,
            "_proc_env_value",
            lambda pid, name: str(profile) if name == "CHROME_USER_DATA_DIR" else None,
        )
        assert browser.running_instance_cdp_port(str(profile)) is None
    finally:
        listener.close()


def test_running_instance_recovers_relative_chrome_user_data_dir(tmp_path, monkeypatch):
    """Env override is still resolved against Chromium cwd when relative."""
    profile = tmp_path / "jar"
    profile.mkdir()
    listener = socket.socket()
    listener.bind(("127.0.0.1", 0))
    listener.listen(1)
    port = listener.getsockname()[1]
    os.symlink(f"host-{os.getpid()}", profile / "SingletonLock")
    monkeypatch.setattr(
        browser,
        "_chromium_cmdline_tokens",
        lambda pid: ["chrome", "--remote-debugging-port=0"],
    )
    monkeypatch.setattr(
        browser,
        "_proc_env_value",
        lambda pid, name: "jar" if name == "CHROME_USER_DATA_DIR" else None,
    )
    monkeypatch.setattr(browser, "_proc_cwd", lambda pid: tmp_path)
    monkeypatch.setattr(browser, "_loopback_listen_ports_for_pid", lambda pid: {port})
    try:
        assert browser.running_instance_cdp_port(str(profile)) == port
        monkeypatch.setattr(browser, "_proc_cwd", lambda pid: Path("/tmp"))
        assert browser.running_instance_cdp_port(str(profile)) is None
    finally:
        listener.close()


def test_persist_configured_listen_accepts_chrome_user_data_dir_env(tmp_path, monkeypatch):
    """Configured listen must honor ``CHROME_USER_DATA_DIR`` when argv omits the flag."""
    listener = socket.socket()
    listener.bind(("127.0.0.1", 0))
    listener.listen(1)
    port = listener.getsockname()[1]
    profile = tmp_path / "browser-profile"
    profile.mkdir()
    os.symlink(f"host-{os.getpid()}", profile / "SingletonLock")
    monkeypatch.setattr(runtime, "state_dir", lambda: tmp_path)
    monkeypatch.setattr(browser, "profile_dir", lambda: profile)
    monkeypatch.setattr(browser, "running_instance_cdp_port", lambda *a, **k: None)
    monkeypatch.setattr(browser, "_chromium_cmdline_tokens", lambda pid: ["chrome"])
    monkeypatch.setattr(
        browser,
        "_proc_env_value",
        lambda pid, name: str(profile) if name == "CHROME_USER_DATA_DIR" else None,
    )
    monkeypatch.setattr(browser, "_loopback_listen_ports_for_pid", lambda pid: {port})
    monkeypatch.setattr(
        browser, "_configured_cdp_override_url", lambda: f"http://127.0.0.1:{port}",
    )
    try:
        assert browser.persist_live_dock_cdp_port() == port
        assert browser.last_known_dock_cdp_port() == port
    finally:
        listener.close()


def test_running_instance_recovers_single_dash_user_data_dir(tmp_path, monkeypatch):
    """Single-dash ``-user-data-dir`` is still this jar. Double-dash-only missed it."""
    listener = socket.socket()
    listener.bind(("127.0.0.1", 0))
    listener.listen(1)
    port = listener.getsockname()[1]
    os.symlink(f"host-{os.getpid()}", tmp_path / "SingletonLock")
    monkeypatch.setattr(
        browser,
        "_chromium_cmdline_tokens",
        lambda pid: ["chrome", f"-user-data-dir={tmp_path}", "--remote-debugging-port=0"],
    )
    monkeypatch.setattr(browser, "_loopback_listen_ports_for_pid", lambda pid: {port})
    try:
        assert browser.running_instance_cdp_port(str(tmp_path)) == port
    finally:
        listener.close()


def test_running_instance_recovers_last_user_data_dir(tmp_path, monkeypatch):
    """Chromium last-wins. A leading scratch ``--user-data-dir`` hid the dock."""
    listener = socket.socket()
    listener.bind(("127.0.0.1", 0))
    listener.listen(1)
    port = listener.getsockname()[1]
    os.symlink(f"host-{os.getpid()}", tmp_path / "SingletonLock")
    monkeypatch.setattr(
        browser,
        "_chromium_cmdline_tokens",
        lambda pid: [
            "chrome",
            "--user-data-dir=/scratch",
            f"--user-data-dir={tmp_path}",
            "--remote-debugging-port=0",
        ],
    )
    monkeypatch.setattr(browser, "_loopback_listen_ports_for_pid", lambda pid: {port})
    try:
        assert browser.running_instance_cdp_port(str(tmp_path)) == port
        monkeypatch.setattr(
            browser,
            "_chromium_cmdline_tokens",
            lambda pid: [
                "chrome",
                f"--user-data-dir={tmp_path}",
                "--user-data-dir=/other",
                "--remote-debugging-port=0",
            ],
        )
        assert browser.running_instance_cdp_port(str(tmp_path)) is None
    finally:
        listener.close()


def test_running_instance_recovers_spaced_user_data_dir(tmp_path, monkeypatch):
    """Equals-only parse treated a spaced ``--user-data-dir`` as another Chrome."""
    listener = socket.socket()
    listener.bind(("127.0.0.1", 0))
    listener.listen(1)
    port = listener.getsockname()[1]
    os.symlink(f"host-{os.getpid()}", tmp_path / "SingletonLock")
    monkeypatch.setattr(
        browser,
        "_chromium_cmdline_tokens",
        lambda pid: ["chrome", "--user-data-dir", str(tmp_path), f"--remote-debugging-port={port}"],
    )
    monkeypatch.setattr(browser, "_loopback_listen_ports_for_pid", lambda pid: {port, 22})
    try:
        assert browser.running_instance_cdp_port(str(tmp_path)) == port
    finally:
        listener.close()


def test_materialized_lock_file_stamps_this_jar(tmp_path, monkeypatch):
    """Finding 160: a regular-file SingletonLock is still this jar.

    Chromium writes a ``host-pid`` symlink. ``cp -L`` / overlay copies
    materialize that text as a file, ``readlink`` fails, and persist
    treated a live dock as another Chrome. Recycled file text that
    names another profile stays unknown. A directory is not a lock.
    """
    listener = socket.socket()
    listener.bind(("127.0.0.1", 0))
    listener.listen(8)
    port = listener.getsockname()[1]
    (tmp_path / "DevToolsActivePort").write_text(
        f"{port}\n/devtools/browser/abc\n", encoding="utf-8",
    )
    (tmp_path / "SingletonLock").write_text(f"host-{os.getpid()}\n", encoding="utf-8")
    monkeypatch.setattr(
        browser,
        "_chromium_cmdline_tokens",
        lambda pid: ["chrome", f"--user-data-dir={tmp_path}"],
    )
    try:
        assert not (tmp_path / "SingletonLock").is_symlink()
        assert browser._lock_pid(str(tmp_path)) == os.getpid()
        assert browser.running_instance_cdp_port(str(tmp_path)) == port

        monkeypatch.setattr(
            browser,
            "_chromium_cmdline_tokens",
            lambda pid: ["chrome", "--user-data-dir=/other/profile"],
        )
        assert browser.running_instance_cdp_port(str(tmp_path)) is None

        (tmp_path / "SingletonLock").unlink()
        (tmp_path / "SingletonLock").mkdir()
        monkeypatch.setattr(
            browser,
            "_chromium_cmdline_tokens",
            lambda pid: ["chrome", f"--user-data-dir={tmp_path}"],
        )
        assert browser._lock_pid(str(tmp_path)) is None
        # Directory is not a lock; finding 161 still sees the named listen.
        assert browser.running_instance_cdp_port(str(tmp_path)) == port
    finally:
        listener.close()


def test_missing_lock_still_stamps_named_listen(tmp_path, monkeypatch):
    """Finding 161: a missing SingletonLock is not another Chrome.

    Supervisor comments say the lock can be gone while Chromium still
    holds DevTools. Persist / leftover identity required the lock pid,
    so leftover ``--cdp`` to the live port survived Take over.
    Recycled cmdline that names another profile stays unknown. 9222
    stays unknown. No lock and no DevTools file does not guess a port.
    """
    listener = socket.socket()
    listener.bind(("127.0.0.1", 0))
    listener.listen(8)
    port = listener.getsockname()[1]
    (tmp_path / "DevToolsActivePort").write_text(
        f"{port}\n/devtools/browser/abc\n", encoding="utf-8",
    )
    monkeypatch.setattr(runtime, "state_dir", lambda: tmp_path)
    monkeypatch.setattr(browser, "profile_dir", lambda: tmp_path)
    monkeypatch.setattr(
        browser,
        "_chromium_cmdline_tokens",
        lambda pid: ["chrome", f"--user-data-dir={tmp_path}"],
    )
    try:
        assert not (tmp_path / "SingletonLock").exists()
        assert browser._lock_pid(str(tmp_path)) is None
        assert browser.running_instance_cdp_port(str(tmp_path)) == port
        assert browser._this_jar_listens_on_port(port) is True
        assert browser._this_jar_listens_on_port(9222) is False

        monkeypatch.setattr(
            browser,
            "_chromium_cmdline_tokens",
            lambda pid: ["chrome", "--user-data-dir=/other/profile"],
        )
        assert browser.running_instance_cdp_port(str(tmp_path)) is None
        assert browser._this_jar_listens_on_port(port) is False

        (tmp_path / "DevToolsActivePort").unlink()
        monkeypatch.setattr(
            browser,
            "_chromium_cmdline_tokens",
            lambda pid: ["chrome", f"--user-data-dir={tmp_path}"],
        )
        assert browser.running_instance_cdp_port(str(tmp_path)) is None
        # Leftover already named the listen — not a persist guess.
        assert browser._this_jar_listens_on_port(port) is True
    finally:
        listener.close()


def test_missing_lock_and_file_keeps_listen_family(tmp_path, monkeypatch):
    """Finding 162: named listen without lock/file still has a family.

    Persist must not guess a port when both files are gone. Leftover
    already named the listen — family hosts must still be this jar's
    ``::1`` so leftover IPv4 does not look like the dock.
    """
    import threading

    listener = socket.socket(socket.AF_INET6, socket.SOCK_STREAM)
    listener.setsockopt(socket.IPPROTO_IPV6, socket.IPV6_V6ONLY, 1)
    listener.bind(("::1", 0))
    listener.listen(8)

    def _drain():
        while True:
            try:
                conn, _ = listener.accept()
            except OSError:
                return
            try:
                conn.close()
            except OSError:
                pass

    threading.Thread(target=_drain, daemon=True).start()
    port = listener.getsockname()[1]
    monkeypatch.setattr(runtime, "state_dir", lambda: tmp_path)
    monkeypatch.setattr(browser, "profile_dir", lambda: tmp_path)
    monkeypatch.setattr(
        browser,
        "_chromium_cmdline_tokens",
        lambda pid: ["chrome", f"--user-data-dir={tmp_path}"],
    )
    try:
        assert not (tmp_path / "SingletonLock").exists()
        assert not (tmp_path / "DevToolsActivePort").exists()
        assert browser._lock_pid(str(tmp_path)) is None
        assert browser._this_jar_chromium_pid(str(tmp_path)) is None
        assert browser.running_instance_cdp_port(str(tmp_path)) is None
        assert browser._this_jar_listens_on_port(port) is True
        assert browser._this_jar_listen_connect_hosts(port) == ("::1",)
        assert browser._this_jar_listen_connect_hosts(9222) == ()
    finally:
        listener.close()


def test_several_this_jar_listen_holders_still_name_the_port(tmp_path, monkeypatch):
    """Finding 163: leftover identity must survive several this-jar holders.

    Unique-holder skip-kill stays unknown. Leftover already named the
    listen — a forked helper that inherited the fd and still names
    ``--user-data-dir`` must not make that port another Chrome.
    Without the port file persist still does not guess. The file-named
    port is that listen (finding 164). 9222 stays unknown.
    """
    monkeypatch.setattr(runtime, "state_dir", lambda: tmp_path)
    monkeypatch.setattr(browser, "profile_dir", lambda: tmp_path)
    monkeypatch.setattr(browser, "_lock_pid", lambda d: None)
    monkeypatch.setattr(
        browser,
        "_chromium_cmdline_tokens",
        lambda pid: ["chrome", f"--user-data-dir={tmp_path}"],
    )
    monkeypatch.setattr(
        browser,
        "_loopback_listen_inodes_for_port",
        lambda port: {7: {"::1"}} if port == 40141 else {},
    )
    monkeypatch.setattr(
        browser,
        "_pids_holding_socket_inodes",
        lambda want: {4242: {7}, 4243: {7}} if 7 in want else {},
    )
    monkeypatch.setattr(
        browser,
        "_cdp_port_reachable",
        lambda port, hosts: port == 40141 and "::1" in hosts,
    )
    assert browser._scan_this_jar_listen_holder(40141, str(tmp_path)) is None
    assert browser._this_jar_listen_connect_hosts(40141) == ("::1",)
    assert browser._this_jar_listens_on_port(40141) is True
    assert browser._this_jar_listens_on_port(9222) is False
    assert browser.running_instance_cdp_port(str(tmp_path)) is None
    (tmp_path / "DevToolsActivePort").write_text(
        "40141\n/devtools/browser/abc\n", encoding="utf-8",
    )
    # Finding 164: the file named the port. Several holders are not a
    # guess among ports. Skip-kill still needs a unique pid.
    assert browser.running_instance_cdp_port(str(tmp_path)) == 40141
    assert browser._this_jar_chromium_pid(str(tmp_path)) is None
    assert browser._this_jar_listens_on_port(40141) is True


def test_file_named_port_survives_lock_pid_that_dropped_the_listen(tmp_path, monkeypatch):
    """Finding 164: lock pid that no longer listens must not hide persist.

    SingletonLock can still name this jar after a helper inherited the
    DevTools fd. Recover from that pid is empty / unknown. The file
    already named the port — helpers that still name this jar and
    inode-hold it are that port. Skip-kill still uses the lock pid.
    9222 stays unknown. No file still does not guess.
    """
    monkeypatch.setattr(runtime, "state_dir", lambda: tmp_path)
    monkeypatch.setattr(browser, "profile_dir", lambda: tmp_path)
    monkeypatch.setattr(browser, "_lock_pid", lambda d: 4240)
    monkeypatch.setattr(
        browser,
        "_chromium_cmdline_tokens",
        lambda pid: ["chrome", f"--user-data-dir={tmp_path}"],
    )
    monkeypatch.setattr(browser, "_loopback_listen_ports_for_pid", lambda pid: set())
    monkeypatch.setattr(browser, "_recover_cdp_port_from_singleton", lambda *a, **k: None)
    monkeypatch.setattr(
        browser,
        "_loopback_listen_inodes_for_port",
        lambda port: {7: {"::1"}} if port == 40141 else {},
    )
    monkeypatch.setattr(
        browser,
        "_pids_holding_socket_inodes",
        lambda want: {4242: {7}, 4243: {7}} if 7 in want else {},
    )
    monkeypatch.setattr(
        browser,
        "_cdp_port_reachable",
        lambda port, hosts: port == 40141 and "::1" in hosts,
    )
    assert browser.running_instance_cdp_port(str(tmp_path)) is None
    (tmp_path / "DevToolsActivePort").write_text(
        "40141\n/devtools/browser/abc\n", encoding="utf-8",
    )
    assert browser.running_instance_cdp_port(str(tmp_path)) == 40141
    assert browser._this_jar_chromium_pid(str(tmp_path)) == 4240
    assert browser._this_jar_listens_on_port(40141) is True
    assert browser._this_jar_listens_on_port(9222) is False
    (tmp_path / "DevToolsActivePort").unlink()
    assert browser.running_instance_cdp_port(str(tmp_path)) is None


def test_lock_pid_dead_family_does_not_shop_holder_squat(tmp_path, monkeypatch):
    """Finding 165: lock pid still lists the file port; its family is dead.

    Finding 164 fell through to inode holders whenever persist was empty.
    A this-jar helper (or the lock pid itself) that still holds another
    family's socket on that number then stamped the IPv4 squat after
    ::1 died. Helpers after a *dropped* listen still persist (164).
    Skip-kill stays the lock pid. 9222 stays unknown.

    Finding 166: leftover ``--cdp http://[::1]:<port>`` still aims at
    that listed family when the fresh TCP probe fails (leftover may
    already hold the CDP socket). Do not stamp persist. Leftover IPv4
    to the squat stays another Chrome.
    """
    monkeypatch.setattr(runtime, "state_dir", lambda: tmp_path)
    monkeypatch.setattr(browser, "profile_dir", lambda: tmp_path)
    monkeypatch.setattr(browser, "_lock_pid", lambda d: 4240)
    monkeypatch.setattr(
        browser,
        "_chromium_cmdline_tokens",
        lambda pid: ["chrome", f"--user-data-dir={tmp_path}"],
    )
    monkeypatch.setattr(browser, "_loopback_listen_ports_for_pid", lambda pid: {40141})
    monkeypatch.setattr(
        browser, "_loopback_listen_targets_for_pid", lambda pid: {("::1", 40141)},
    )
    monkeypatch.setattr(browser, "_recover_cdp_port_from_singleton", lambda *a, **k: None)
    monkeypatch.setattr(
        browser,
        "_loopback_listen_inodes_for_port",
        lambda port: {7: {"127.0.0.1"}} if port == 40141 else {},
    )
    monkeypatch.setattr(
        browser,
        "_pids_holding_socket_inodes",
        lambda want: {4242: {7}} if 7 in want else {},
    )
    monkeypatch.setattr(
        browser,
        "_cdp_port_reachable",
        lambda port, hosts: port == 40141 and "127.0.0.1" in hosts,
    )
    (tmp_path / "DevToolsActivePort").write_text(
        "40141\n/devtools/browser/abc\n", encoding="utf-8",
    )
    assert browser.running_instance_cdp_port(str(tmp_path)) is None
    assert browser._this_jar_chromium_pid(str(tmp_path)) == 4240
    assert browser._this_jar_listen_connect_hosts(40141) == ("::1",)
    assert browser._this_jar_listens_on_port(40141) is False
    assert browser._this_jar_listens_on_port(9222) is False
    from tools.browser_tool_session import (
        _cdp_url_is_bot_desktop_browser,
        _leftover_cdp_host_matches_this_jar,
        _last_dock_cdp_port,
    )
    monkeypatch.setattr(browser, "_configured_cdp_override_url", lambda: "")
    _last_dock_cdp_port.clear()
    # Finding 166: leftover already named the lock pid's listed family.
    # A failed TCP probe must not hide that writer. Do not stamp persist
    # (165). Leftover IPv4 to the squat stays another Chrome.
    assert _leftover_cdp_host_matches_this_jar("http://[::1]:40141", 40141) is True
    assert _leftover_cdp_host_matches_this_jar("http://127.0.0.1:40141", 40141) is False
    assert _cdp_url_is_bot_desktop_browser("http://[::1]:40141") is True
    assert _cdp_url_is_bot_desktop_browser("http://127.0.0.1:40141") is False
    assert _cdp_url_is_bot_desktop_browser("http://127.0.0.1:9222") is False
    assert browser.last_known_dock_cdp_port() is None
    assert browser.running_instance_cdp_port(str(tmp_path)) is None


def test_devtools_file_does_not_stamp_recycled_lock_pid(tmp_path, monkeypatch):
    """Finding 158: DevToolsActivePort trusted a recycled lock pid.

    Recover already refuses a lock pid whose cmdline is not this jar.
    The file-port branch only checked alive + listen, so a sibling
    Chrome that inherited the lock and occupied the stale number was
    stamped as the dock. Same-jar cmdline still identifies.
    """
    listener = socket.socket()
    listener.bind(("127.0.0.1", 0))
    listener.listen(8)
    port = listener.getsockname()[1]
    _fake_running_instance(tmp_path, os.getpid(), port)
    monkeypatch.setattr(
        browser,
        "_chromium_cmdline_tokens",
        lambda pid: ["chrome", "--user-data-dir=/other/profile"],
    )
    try:
        assert browser.running_instance_cdp_port(str(tmp_path)) is None
        monkeypatch.setattr(
            browser,
            "_chromium_cmdline_tokens",
            lambda pid: ["chrome", f"--user-data-dir={tmp_path}"],
        )
        assert browser.running_instance_cdp_port(str(tmp_path)) == port
    finally:
        listener.close()


def test_devtools_file_does_not_stamp_sibling_listen(tmp_path):
    """Stale DevToolsActivePort plus another Chrome on that port is not this jar.

    ``_listen_connect_hosts`` used to fall back to 127.0.0.1 when this pid
    no longer listened, so persist stamped the sibling as the dock.
    """
    child = _other_pid_loopback_listen()
    try:
        port = int(child.stdout.readline())
        _fake_running_instance(tmp_path, os.getpid(), port)
        assert port not in browser._loopback_listen_ports_for_pid(os.getpid())
        assert port in browser._loopback_listen_ports_for_pid(child.pid)
        assert browser.running_instance_cdp_port(str(tmp_path)) is None
    finally:
        child.kill()
        child.wait(timeout=5)


def test_stale_devtools_file_recovers_this_jar_listen(tmp_path, monkeypatch):
    """File names a sibling port; this jar still listens elsewhere — recover that."""
    child = _other_pid_loopback_listen()
    listener = socket.socket()
    listener.bind(("127.0.0.1", 0))
    listener.listen(1)
    ours = listener.getsockname()[1]
    try:
        stale = int(child.stdout.readline())
        _fake_running_instance(tmp_path, os.getpid(), stale)
        monkeypatch.setattr(
            browser,
            "_chromium_cmdline_tokens",
            lambda pid: ["chrome", f"--user-data-dir={tmp_path}", "--remote-debugging-port=0"],
        )
        monkeypatch.setattr(browser, "_loopback_listen_ports_for_pid", lambda pid: {ours})
        monkeypatch.setattr(
            browser, "_loopback_listen_targets_for_pid", lambda pid: {("127.0.0.1", ours)},
        )
        assert browser.running_instance_cdp_port(str(tmp_path)) == ours
    finally:
        listener.close()
        child.kill()
        child.wait(timeout=5)


def test_recover_explicit_does_not_stamp_sibling_listen(tmp_path, monkeypatch):
    """File gone + argv ``--remote-debugging-port`` of a sibling is not the dock."""
    child = _other_pid_loopback_listen()
    try:
        port = int(child.stdout.readline())
        os.symlink(f"host-{os.getpid()}", tmp_path / "SingletonLock")
        monkeypatch.setattr(
            browser,
            "_chromium_cmdline_tokens",
            lambda pid: [
                "chrome",
                f"--user-data-dir={tmp_path}",
                f"--remote-debugging-port={port}",
            ],
        )
        monkeypatch.setattr(browser, "_loopback_listen_ports_for_pid", lambda pid: set())
        monkeypatch.setattr(browser, "_loopback_listen_targets_for_pid", lambda pid: set())
        assert browser.running_instance_cdp_port(str(tmp_path)) is None
    finally:
        child.kill()
        child.wait(timeout=5)


def test_persist_does_not_stamp_stale_devtools_sibling(tmp_path, monkeypatch):
    """Human-first persist must not write a sibling Chrome's port as this jar."""
    child = _other_pid_loopback_listen()
    profile = tmp_path / "browser-profile"
    profile.mkdir()
    try:
        port = int(child.stdout.readline())
        _fake_running_instance(profile, os.getpid(), port)
        monkeypatch.setattr(runtime, "state_dir", lambda: tmp_path)
        monkeypatch.setattr(browser, "profile_dir", lambda: profile)
        monkeypatch.setattr(browser, "_configured_cdp_override_url", lambda: "")
        monkeypatch.setattr(browser, "_chromium_cmdline_tokens", lambda pid: ["chrome"])
        assert browser.persist_live_dock_cdp_port() is None
        assert browser.last_known_dock_cdp_port() is None
    finally:
        child.kill()
        child.wait(timeout=5)


def test_persist_stamps_configured_port_when_recover_is_ambiguous(tmp_path, monkeypatch):
    """Two specific loopbacks hide unique-listen recover. Config still names DevTools."""
    listener = socket.socket()
    listener.bind(("127.0.0.1", 0))
    listener.listen(1)
    port = listener.getsockname()[1]
    profile = tmp_path / "browser-profile"
    profile.mkdir()
    os.symlink(f"host-{os.getpid()}", profile / "SingletonLock")
    monkeypatch.setattr(runtime, "state_dir", lambda: tmp_path)
    monkeypatch.setattr(browser, "profile_dir", lambda: profile)
    monkeypatch.setattr(browser, "running_instance_cdp_port", lambda *a, **k: None)
    monkeypatch.setattr(
        browser,
        "_chromium_cmdline_tokens",
        lambda pid: ["chrome", f"--user-data-dir={profile}"],
    )
    monkeypatch.setattr(browser, "_loopback_listen_ports_for_pid", lambda pid: {port, port + 1})
    monkeypatch.setattr(
        browser,
        "_loopback_listen_targets_for_pid",
        lambda pid: {("127.0.0.1", port), ("::1", port + 1)},
    )
    monkeypatch.setattr(
        browser, "_configured_cdp_override_url", lambda: f"http://127.0.0.1:{port}",
    )
    try:
        assert browser.persist_live_dock_cdp_port() == port
        assert browser.last_known_dock_cdp_port() == port
        monkeypatch.setattr(
            browser, "_configured_cdp_override_url", lambda: "http://127.0.0.1:9222",
        )
        (tmp_path / "dock-cdp-port").unlink(missing_ok=True)
        assert browser.persist_live_dock_cdp_port() is None
        assert browser.last_known_dock_cdp_port() is None
        monkeypatch.setattr(browser, "_configured_cdp_override_url", lambda: "")
        assert browser.persist_live_dock_cdp_port() is None
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
    # Unknown listen family stays a bare port (finding 167).
    assert argvs[-1][:5] == ["agent-browser", "--session", "h_abc", "--cdp", "41234"]

    monkeypatch.setattr(browser, "running_instance_cdp_port", lambda d, **kw: None)
    session._run_browser_command_unfenced("t", "open", ["https://x"], 10, None, "agent-browser", info)
    assert "--cdp" not in argvs[-1] and argvs[-1][:3] == ["agent-browser", "--session", "h_abc"]


def test_agent_attach_uses_this_jar_listen_family(tmp_path, monkeypatch):
    """Finding 167: ``--cdp <port>`` raced to the other loopback family's squat.

    Persist / ``running_instance_cdp_port`` are a port. Agent-browser
    treats a bare port as localhost / ``127.0.0.1``. A ``::1``-only
    dock plus a sibling on ``127.0.0.1:same`` then attached to the
    squat — leftover identity already rejects that family (finding
    145). Attach with this jar's connect host. Empty hosts stay a
    bare port. 9222 stays unknown.
    """
    from tools import browser_tool_session as session
    from tools.browser_tool_session import _reset_dock_port_memory_for_tests

    v6, v4, port, profile = _ipv6_only_dock(tmp_path, monkeypatch)
    def _drain():
        while True:
            try:
                conn, _ = v6.accept()
                conn.close()
            except OSError:
                return
    threading.Thread(target=_drain, daemon=True).start()
    try:
        _reset_dock_port_memory_for_tests()
        assert browser.running_instance_cdp_port(str(profile)) == port
        assert browser._this_jar_listen_connect_hosts(port) == ("::1",)
        assert browser.dock_cdp_attach_target(port) == f"http://[::1]:{port}"
        assert browser.dock_cdp_attach_target(9222) == "9222"

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
        session._run_browser_command_unfenced(
            "t", "open", ["https://x"], 10, None, "agent-browser", info,
        )
        assert argvs[-1][:5] == [
            "agent-browser", "--session", "h_abc", "--cdp", f"http://[::1]:{port}",
        ]
        assert argvs[-1][4] != str(port)
        assert "127.0.0.1" not in argvs[-1][4]
    finally:
        v6.close()
        v4.close()


def _spawn_agent_open(monkeypatch, session, session_name="h_abc"):
    """Capture agent-browser argv for one unfenced ``open``."""
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
    info = {"session_name": session_name, "cdp_url": None, "features": {"local": True}}
    session._run_browser_command_unfenced(
        "t", "open", ["https://x"], 10, None, "agent-browser", info,
    )
    return argvs


def test_agent_attach_uses_persist_when_live_tcp_misses(tmp_path, monkeypatch):
    """Finding 168: leftover holding CDP hid live persist, so agent launched.

    ``running_instance_cdp_port`` requires a fresh TCP accept. Leftover
    identity already trusts remembered persist when that probe misses
    (finding 166). Agent attach did not — it launched ``--session`` and
    Chromium singleton-forwarded into the jar a human holds. Attach
    with this jar's connect host. Empty hosts stay a launch. A session
    that owns the chrome stays a launch. 9222 stays unknown.
    """
    from hermes_constants import hermes_home_key
    from tools import browser_tool_session as session
    from tools.browser_tool_session import (
        _cdp_url_is_bot_desktop_browser,
        _last_dock_cdp_port,
        _reset_dock_port_memory_for_tests,
    )

    v6, v4, port, profile = _ipv6_only_dock(tmp_path, monkeypatch)
    try:
        _reset_dock_port_memory_for_tests()
        browser.remember_dock_cdp_port(port)
        _last_dock_cdp_port.clear()
        # Live recover requires a fresh TCP accept. Leftover holding the
        # CDP socket is that miss (finding 166). Linux listen(1) still
        # accepts a second probe — mock the miss, keep inode hosts.
        monkeypatch.setattr(browser, "running_instance_cdp_port", lambda *a, **k: None)
        assert browser.last_known_dock_cdp_port() == port
        assert browser._this_jar_listen_connect_hosts(port) == ("::1",)
        assert _cdp_url_is_bot_desktop_browser(f"http://[::1]:{port}") is True
        assert _cdp_url_is_bot_desktop_browser(f"http://127.0.0.1:{port}") is False

        argvs = _spawn_agent_open(monkeypatch, session)
        assert argvs[-1][:5] == [
            "agent-browser", "--session", "h_abc", "--cdp", f"http://[::1]:{port}",
        ]
        assert "127.0.0.1" not in argvs[-1][4]
        assert argvs[-1][4] != str(port)

        _reset_dock_port_memory_for_tests()
        browser._dock_port_path().unlink(missing_ok=True)
        _last_dock_cdp_port[hermes_home_key()] = port
        assert browser.last_known_dock_cdp_port() is None
        argvs = _spawn_agent_open(monkeypatch, session)
        assert argvs[-1][4] == f"http://[::1]:{port}"

        monkeypatch.setattr(browser, "_launched_by_session", lambda pid: "h_abc")
        argvs = _spawn_agent_open(monkeypatch, session)
        assert "--cdp" not in argvs[-1]
        assert argvs[-1][:3] == ["agent-browser", "--session", "h_abc"]

        monkeypatch.setattr(browser, "_launched_by_session", lambda pid: None)
        _reset_dock_port_memory_for_tests()
        browser.remember_dock_cdp_port(9333)
        _last_dock_cdp_port.clear()
        argvs = _spawn_agent_open(monkeypatch, session)
        assert "--cdp" not in argvs[-1]
        assert browser.dock_cdp_attach_target(9222) == "9222"
    finally:
        v6.close()
        v4.close()


def _ipv6_only_dock(tmp_path, monkeypatch):
    """Live ::1 dock plus a sibling squat on 127.0.0.1:same. Yields (port, profile)."""
    v6 = socket.socket(socket.AF_INET6, socket.SOCK_STREAM)
    v6.setsockopt(socket.IPPROTO_IPV6, socket.IPV6_V6ONLY, 1)
    v6.bind(("::1", 0))
    v6.listen(1)
    port = v6.getsockname()[1]
    v4 = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    v4.bind(("127.0.0.1", port))
    v4.listen(1)
    profile = tmp_path / "browser-profile"
    profile.mkdir()
    os.symlink(f"host-{os.getpid()}", profile / "SingletonLock")
    monkeypatch.setattr(runtime, "state_dir", lambda: tmp_path)
    monkeypatch.setattr(browser, "profile_dir", lambda: profile)
    monkeypatch.setattr(
        browser,
        "_chromium_cmdline_tokens",
        lambda pid: ["chrome", f"--user-data-dir={profile}", "--remote-debugging-port=0"],
    )
    monkeypatch.setattr(browser, "_loopback_listen_ports_for_pid", lambda pid: {port})
    monkeypatch.setattr(
        browser, "_loopback_listen_targets_for_pid", lambda pid: {("::1", port)},
    )
    return v6, v4, port, profile


def test_cdp_identity_rejects_other_family_sibling_on_same_port(tmp_path, monkeypatch):
    """Leftover ``http://127.0.0.1:<dock>`` is the squat, not a ::1-only jar."""
    from tools.browser_tool_session import (
        _cdp_url_is_bot_desktop_browser,
        _leftover_cdp_aims_at_dock,
        _reset_dock_port_memory_for_tests,
    )

    v6, v4, port, profile = _ipv6_only_dock(tmp_path, monkeypatch)
    try:
        _reset_dock_port_memory_for_tests()
        assert browser.running_instance_cdp_port(str(profile)) == port
        assert browser._listen_connect_hosts(os.getpid(), port) == ("::1",)
        assert _cdp_url_is_bot_desktop_browser(f"http://[::1]:{port}") is True
        assert _cdp_url_is_bot_desktop_browser(f"http://127.0.0.1:{port}") is False
        assert _cdp_url_is_bot_desktop_browser(f"http://127.1:{port}") is False
        assert _cdp_url_is_bot_desktop_browser(f"http://[::ffff:127.0.0.1]:{port}") is False
        assert _cdp_url_is_bot_desktop_browser(f"http://localhost:{port}") is True
        assert _cdp_url_is_bot_desktop_browser(str(port)) is True
        assert _leftover_cdp_aims_at_dock(f"http://127.0.0.1:{port}", port) is False
        assert _leftover_cdp_aims_at_dock(f"http://[::1]:{port}", port) is True
        assert _leftover_cdp_aims_at_dock(str(port), port) is True
    finally:
        v6.close()
        v4.close()


def test_cdp_identity_port_only_when_listen_unknown(tmp_path, monkeypatch):
    """Planted 9333 / no SingletonLock stays port-only (leftover-CLI fixtures)."""
    from tools.browser_tool_session import (
        _cdp_url_is_bot_desktop_browser,
        _leftover_cdp_aims_at_dock,
        _reset_dock_port_memory_for_tests,
    )

    monkeypatch.setattr(runtime, "state_dir", lambda: tmp_path)
    monkeypatch.setattr(browser, "profile_dir", lambda: tmp_path / "browser-profile")
    monkeypatch.setattr(browser, "running_instance_cdp_port", lambda *a, **k: None)
    _reset_dock_port_memory_for_tests()
    browser.remember_dock_cdp_port(9333)
    assert _cdp_url_is_bot_desktop_browser("http://127.0.0.1:9333") is True
    assert _leftover_cdp_aims_at_dock("http://127.0.0.1:9333", 9333) is True
    assert _leftover_cdp_aims_at_dock("http://10.0.0.5:9333", 9333) is False
