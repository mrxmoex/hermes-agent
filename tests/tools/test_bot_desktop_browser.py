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


def test_file_named_helpers_do_not_hide_lock_listed_persist(tmp_path, monkeypatch):
    """Finding 174: 164 must not overwrite persist the lock pid still lists.

    Unique-listen recover is unknown when Chromium has several specific
    loopbacks (finding 85). Finding 164 then persisted a stale
    ``DevToolsActivePort`` that this-jar helpers still inode-hold
    (inherited leftover fd) and overwrote ``dock-cdp-port``. Finding
    172's attach tie-break never ran because recover / 164 succeeded.
    Persist already named the listen this pid still holds — stamp that.
    Lock listing persist and the file is finding 172. Dropped-listen
    helpers (164) stay. Persist TCP miss must not shop the file.
    9222 stays unknown.
    """
    from tools.browser_tool_session import (
        _cdp_url_is_bot_desktop_browser,
        _remembered_dock_attach_port,
        _reset_dock_port_memory_for_tests,
    )

    monkeypatch.setattr(runtime, "state_dir", lambda: tmp_path)
    monkeypatch.setattr(browser, "profile_dir", lambda: tmp_path)
    monkeypatch.setattr(browser, "_lock_pid", lambda d: 4240)
    monkeypatch.setattr(
        browser,
        "_chromium_cmdline_tokens",
        lambda pid: ["chrome", f"--user-data-dir={tmp_path}"],
    )
    monkeypatch.setattr(
        browser, "_loopback_listen_ports_for_pid", lambda pid: {9333, 40142},
    )
    monkeypatch.setattr(
        browser,
        "_loopback_listen_targets_for_pid",
        lambda pid: {("::1", 9333), ("::1", 40142)},
    )
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
        lambda port, hosts: port in (40141, 9333) and "::1" in hosts,
    )
    _reset_dock_port_memory_for_tests()
    browser.remember_dock_cdp_port(9333)
    (tmp_path / "DevToolsActivePort").write_text(
        "40141\n/devtools/browser/abc\n", encoding="utf-8",
    )
    assert browser.file_named_dock_listen_port() == 40141
    assert browser._this_jar_listen_connect_hosts(9333) == ("::1",)
    assert browser._this_jar_listen_connect_hosts(40141) == ("::1",)
    assert browser.running_instance_cdp_port(str(tmp_path)) == 9333
    assert browser.last_known_dock_cdp_port() == 9333
    assert _remembered_dock_attach_port() == 9333
    assert _cdp_url_is_bot_desktop_browser("http://[::1]:9333") is True
    assert _cdp_url_is_bot_desktop_browser("http://[::1]:40141") is True
    assert _cdp_url_is_bot_desktop_browser("http://127.0.0.1:9333") is False
    assert _cdp_url_is_bot_desktop_browser("9222") is False

    monkeypatch.setattr(
        browser,
        "_cdp_port_reachable",
        lambda port, hosts: port == 40141 and "::1" in hosts,
    )
    _reset_dock_port_memory_for_tests()
    browser.remember_dock_cdp_port(9333)
    (tmp_path / "DevToolsActivePort").write_text(
        "40141\n/devtools/browser/abc\n", encoding="utf-8",
    )
    assert browser.running_instance_cdp_port(str(tmp_path)) is None
    assert browser.last_known_dock_cdp_port() == 9333
    assert _remembered_dock_attach_port() == 9333
    # Finding 175: named-listen identity after this miss must not stamp
    # stale file helpers over lock-listed persist.
    assert _cdp_url_is_bot_desktop_browser("http://[::1]:40141") is True
    assert browser.last_known_dock_cdp_port() == 9333
    assert _remembered_dock_attach_port() == 9333
    assert _cdp_url_is_bot_desktop_browser("http://[::1]:9333") is True
    assert _cdp_url_is_bot_desktop_browser("http://127.0.0.1:40141") is False
    assert _cdp_url_is_bot_desktop_browser("9222") is False
    assert browser.dock_cdp_attach_target(9222) == "9222"


def test_configured_listen_does_not_stamp_over_lock_listed_persist(tmp_path, monkeypatch):
    """Finding 176: persist_live must not stamp leftover file helpers.

    Finding 175 closed the identity stamp door. persist_live still
    falls through to ``_configured_listen_port_for_this_jar`` after a
    174 persist-TCP miss. ``/browser connect`` / ``BROWSER_CDP_URL``
    naming stale ``DevToolsActivePort`` helpers is a this-jar listen
    (finding 86) and used to overwrite lock-listed persist. Refuse
    that stamp; finding 86 still writes when persist is *not* on the
    lock. 9222 and the other family stay unknown.
    """
    from tools.browser_tool_session import (
        _cdp_url_is_bot_desktop_browser,
        _remembered_dock_attach_port,
        _reset_dock_port_memory_for_tests,
    )

    monkeypatch.setattr(runtime, "state_dir", lambda: tmp_path)
    monkeypatch.setattr(browser, "profile_dir", lambda: tmp_path)
    monkeypatch.setattr(browser, "_lock_pid", lambda d: 4240)
    monkeypatch.setattr(
        browser,
        "_chromium_cmdline_tokens",
        lambda pid: ["chrome", f"--user-data-dir={tmp_path}"],
    )
    monkeypatch.setattr(
        browser, "_loopback_listen_ports_for_pid", lambda pid: {9333, 40142},
    )
    monkeypatch.setattr(
        browser,
        "_loopback_listen_targets_for_pid",
        lambda pid: {("::1", 9333), ("::1", 40142)},
    )
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
    monkeypatch.setattr(
        browser, "_configured_cdp_override_url",
        lambda: "http://[::1]:40141",
    )
    _reset_dock_port_memory_for_tests()
    browser.remember_dock_cdp_port(9333)
    (tmp_path / "DevToolsActivePort").write_text(
        "40141\n/devtools/browser/abc\n", encoding="utf-8",
    )
    assert browser.lock_listed_persist_port() == 9333
    assert browser.running_instance_cdp_port(str(tmp_path)) is None
    assert browser._this_jar_listens_on_port(40141) is True
    assert browser._configured_listen_port_for_this_jar() is None
    assert browser.persist_live_dock_cdp_port() is None
    assert browser.last_known_dock_cdp_port() == 9333
    assert _remembered_dock_attach_port() == 9333
    assert _cdp_url_is_bot_desktop_browser("http://[::1]:40141") is True
    assert browser.last_known_dock_cdp_port() == 9333
    assert _cdp_url_is_bot_desktop_browser("http://[::1]:9333") is True
    assert _cdp_url_is_bot_desktop_browser("http://127.0.0.1:40141") is False
    assert _cdp_url_is_bot_desktop_browser("9222") is False


def test_memory_only_persist_does_not_shop_file_helpers(tmp_path, monkeypatch):
    """Finding 177: persist file miss must not let 164 stamp leftover helpers.

    Finding 169 already treats in-process memory as a persist candidate
    when ``dock-cdp-port`` is missing. ``lock_listed_persist_port``
    read the file only, so a remember miss (or unlinked persist)
    made 164 / configured treat lock-listed chrome as empty and
    stamp leftover ``DevToolsActivePort`` helpers. Memory still on
    the lock is persist. Finding 86 still stamps when memory is
    empty. 9222 and the other family stay unknown.
    """
    from hermes_constants import hermes_home_key
    from tools.browser_tool_session import (
        _cdp_url_is_bot_desktop_browser,
        _last_dock_cdp_port,
        _remembered_dock_attach_port,
        _reset_dock_port_memory_for_tests,
    )

    monkeypatch.setattr(runtime, "state_dir", lambda: tmp_path)
    monkeypatch.setattr(browser, "profile_dir", lambda: tmp_path)
    monkeypatch.setattr(browser, "_lock_pid", lambda d: 4240)
    monkeypatch.setattr(
        browser,
        "_chromium_cmdline_tokens",
        lambda pid: ["chrome", f"--user-data-dir={tmp_path}"],
    )
    monkeypatch.setattr(
        browser, "_loopback_listen_ports_for_pid", lambda pid: {9333, 40142},
    )
    monkeypatch.setattr(
        browser,
        "_loopback_listen_targets_for_pid",
        lambda pid: {("::1", 9333), ("::1", 40142)},
    )
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
    monkeypatch.setattr(
        browser, "_configured_cdp_override_url",
        lambda: "http://[::1]:40141",
    )
    _reset_dock_port_memory_for_tests()
    _last_dock_cdp_port[hermes_home_key()] = 9333
    (tmp_path / "DevToolsActivePort").write_text(
        "40141\n/devtools/browser/abc\n", encoding="utf-8",
    )
    assert browser.last_known_dock_cdp_port() is None
    assert browser.lock_listed_persist_port() == 9333
    assert browser.running_instance_cdp_port(str(tmp_path)) is None
    assert browser._configured_listen_port_for_this_jar() is None
    assert browser.persist_live_dock_cdp_port() is None
    assert browser.last_known_dock_cdp_port() is None
    assert _last_dock_cdp_port.get(hermes_home_key()) == 9333
    assert _remembered_dock_attach_port() == 9333
    assert _cdp_url_is_bot_desktop_browser("http://[::1]:40141") is True
    assert browser.last_known_dock_cdp_port() is None
    assert _last_dock_cdp_port.get(hermes_home_key()) == 9333
    assert _cdp_url_is_bot_desktop_browser("http://[::1]:9333") is True
    assert _cdp_url_is_bot_desktop_browser("http://127.0.0.1:40141") is False
    assert _cdp_url_is_bot_desktop_browser("9222") is False

    # File names leftover helpers (not on the lock). Memory still names
    # lock-listed chrome. File-first lock_listed used to miss and 164
    # restamped the leftover.
    browser.remember_dock_cdp_port(40141)
    _last_dock_cdp_port[hermes_home_key()] = 9333
    assert browser.last_known_dock_cdp_port() == 40141
    assert browser.lock_listed_persist_port() == 9333
    assert browser.running_instance_cdp_port(str(tmp_path)) is None
    assert browser.persist_live_dock_cdp_port() is None
    assert browser.last_known_dock_cdp_port() == 40141
    assert _last_dock_cdp_port.get(hermes_home_key()) == 9333
    assert _cdp_url_is_bot_desktop_browser("http://[::1]:40141") is True
    assert _last_dock_cdp_port.get(hermes_home_key()) == 9333


def test_poisoned_persist_file_does_not_hide_lock_listed_attach(
    tmp_path, monkeypatch,
):
    """Finding 178: poisoned persist file must not attach leftover helpers.

    ``remember_dock_cdp_port`` writes the file only. Pre-177
    persist_live left ``dock-cdp-port`` on leftover file helpers
    while memory still named lock-listed chrome. Finding 177 stops
    new stamps; attach still preferred the file. Lock-listed persist
    first. Finding 172 still prefers a different file-named live
    listen. Finding 86 still attaches helpers when persist is not
    on the lock. 9222 and the other family stay unknown.
    """
    from hermes_constants import hermes_home_key
    from tools.browser_tool_session import (
        _cdp_url_is_bot_desktop_browser,
        _last_dock_cdp_port,
        _remembered_dock_attach_port,
        _reset_dock_port_memory_for_tests,
    )

    monkeypatch.setattr(runtime, "state_dir", lambda: tmp_path)
    monkeypatch.setattr(browser, "profile_dir", lambda: tmp_path)
    monkeypatch.setattr(browser, "_lock_pid", lambda d: 4240)
    monkeypatch.setattr(
        browser,
        "_chromium_cmdline_tokens",
        lambda pid: ["chrome", f"--user-data-dir={tmp_path}"],
    )
    monkeypatch.setattr(
        browser, "_loopback_listen_ports_for_pid", lambda pid: {9333, 40142},
    )
    monkeypatch.setattr(
        browser,
        "_loopback_listen_targets_for_pid",
        lambda pid: {("::1", 9333), ("::1", 40142)},
    )
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
    monkeypatch.setattr(browser, "_configured_cdp_override_url", lambda: "")
    _reset_dock_port_memory_for_tests()
    browser.remember_dock_cdp_port(40141)
    _last_dock_cdp_port[hermes_home_key()] = 9333
    (tmp_path / "DevToolsActivePort").write_text(
        "40141\n/devtools/browser/abc\n", encoding="utf-8",
    )
    assert browser.last_known_dock_cdp_port() == 40141
    assert browser.lock_listed_persist_port() == 9333
    assert browser.file_named_dock_listen_port() == 40141
    assert browser.running_instance_cdp_port(str(tmp_path)) is None
    assert browser.persist_live_dock_cdp_port() is None
    assert browser.last_known_dock_cdp_port() == 40141
    assert _last_dock_cdp_port.get(hermes_home_key()) == 9333
    assert _remembered_dock_attach_port() == 9333
    assert _cdp_url_is_bot_desktop_browser("http://[::1]:40141") is True
    assert _remembered_dock_attach_port() == 9333
    assert _cdp_url_is_bot_desktop_browser("http://[::1]:9333") is True
    assert _cdp_url_is_bot_desktop_browser("http://127.0.0.1:40141") is False
    assert _cdp_url_is_bot_desktop_browser("9222") is False
    assert browser.last_known_dock_cdp_port() == 40141
    assert _last_dock_cdp_port.get(hermes_home_key()) == 9333


def test_missing_lock_does_not_shop_file_helpers_over_memory(
    tmp_path, monkeypatch,
):
    """Finding 179: missing SingletonLock must not hide lock-listed memory.

    Finding 161: Take over can unlink the lock while chrome still
    inode-listens. Finding 177 / 178 required that pid, so a poisoned
    persist file plus leftover ``DevToolsActivePort`` helpers made
    164 / persist_live / attach treat memory-named chrome as empty.
    Memory that this jar still inode-listens on is persist. Finding
    86 still stamps helpers when memory is missing. Finding 172
    still prefers a different file-named live listen. 9222 and the
    other family stay unknown.
    """
    from hermes_constants import hermes_home_key
    from tools.browser_tool_session import (
        _cdp_url_is_bot_desktop_browser,
        _last_dock_cdp_port,
        _remembered_dock_attach_port,
        _reset_dock_port_memory_for_tests,
    )

    monkeypatch.setattr(runtime, "state_dir", lambda: tmp_path)
    monkeypatch.setattr(browser, "profile_dir", lambda: tmp_path)
    monkeypatch.setattr(browser, "_lock_pid", lambda d: None)
    monkeypatch.setattr(
        browser,
        "_chromium_cmdline_tokens",
        lambda pid: ["chrome", f"--user-data-dir={tmp_path}"],
    )
    monkeypatch.setattr(
        browser, "_loopback_listen_ports_for_pid", lambda pid: set(),
    )
    monkeypatch.setattr(
        browser, "_loopback_listen_targets_for_pid", lambda pid: set(),
    )
    monkeypatch.setattr(browser, "_recover_cdp_port_from_singleton", lambda *a, **k: None)

    def _inodes(port):
        if port == 40141:
            return {7: {"::1"}}
        if port == 9333:
            return {8: {"::1"}}
        return {}

    def _holders(want):
        out = {}
        if 7 in want:
            out[4242] = {7}
            out[4243] = {7}
        if 8 in want:
            out[4240] = {8}
        return out

    monkeypatch.setattr(browser, "_loopback_listen_inodes_for_port", _inodes)
    monkeypatch.setattr(browser, "_pids_holding_socket_inodes", _holders)
    monkeypatch.setattr(
        browser,
        "_cdp_port_reachable",
        lambda port, hosts: port == 40141 and "::1" in hosts,
    )
    monkeypatch.setattr(
        browser, "_configured_cdp_override_url",
        lambda: "http://[::1]:40141",
    )
    _reset_dock_port_memory_for_tests()
    browser.remember_dock_cdp_port(40141)
    _last_dock_cdp_port[hermes_home_key()] = 9333
    (tmp_path / "DevToolsActivePort").write_text(
        "40141\n/devtools/browser/abc\n", encoding="utf-8",
    )
    assert browser.last_known_dock_cdp_port() == 40141
    assert browser.lock_listed_persist_port() == 9333
    assert browser.file_named_dock_listen_port() == 40141
    assert browser.running_instance_cdp_port(str(tmp_path)) is None
    assert browser._configured_listen_port_for_this_jar() is None
    assert browser.persist_live_dock_cdp_port() is None
    assert browser.last_known_dock_cdp_port() == 40141
    assert _last_dock_cdp_port.get(hermes_home_key()) == 9333
    assert _remembered_dock_attach_port() == 9333
    assert _cdp_url_is_bot_desktop_browser("http://[::1]:40141") is True
    assert _remembered_dock_attach_port() == 9333
    assert _cdp_url_is_bot_desktop_browser("http://[::1]:9333") is True
    assert _cdp_url_is_bot_desktop_browser("http://127.0.0.1:40141") is False
    assert _cdp_url_is_bot_desktop_browser("9222") is False
    assert browser.last_known_dock_cdp_port() == 40141
    assert _last_dock_cdp_port.get(hermes_home_key()) == 9333

    _reset_dock_port_memory_for_tests()
    browser.remember_dock_cdp_port(40141)
    (tmp_path / "DevToolsActivePort").write_text(
        "40141\n/devtools/browser/abc\n", encoding="utf-8",
    )
    assert browser.lock_listed_persist_port() is None
    assert browser.running_instance_cdp_port(str(tmp_path)) == 40141
    assert browser.last_known_dock_cdp_port() == 40141


def test_persist_live_syncs_memory_so_missing_lock_keeps_chrome(
    tmp_path, monkeypatch,
):
    """Finding 180: persist_live must sync leftover memory before unlink.

    Finding 174 stamps chrome while the lock still lists persist and
    not leftover ``DevToolsActivePort`` helpers. ``remember`` writes
    the file only, so memory stayed on the leftover. Finding 179 then
    treated that leftover as lock-listed after Take over unlinked
    SingletonLock, and finding 172 preferred leftover file-named
    helpers. Sync memory on a live stamp. A persist_live miss must
    not clobber chrome memory. 9222 and the other family stay
    unknown.
    """
    from hermes_constants import hermes_home_key
    from tools.browser_tool_session import (
        _cdp_url_is_bot_desktop_browser,
        _last_dock_cdp_port,
        _remembered_dock_attach_port,
        _reset_dock_port_memory_for_tests,
    )

    monkeypatch.setattr(runtime, "state_dir", lambda: tmp_path)
    monkeypatch.setattr(browser, "profile_dir", lambda: tmp_path)
    monkeypatch.setattr(browser, "_lock_pid", lambda d: 4240)
    monkeypatch.setattr(
        browser,
        "_chromium_cmdline_tokens",
        lambda pid: ["chrome", f"--user-data-dir={tmp_path}"],
    )
    monkeypatch.setattr(
        browser, "_loopback_listen_ports_for_pid",
        lambda pid: {9333} if pid == 4240 else set(),
    )
    monkeypatch.setattr(
        browser, "_loopback_listen_targets_for_pid",
        lambda pid: {("::1", 9333)} if pid == 4240 else set(),
    )
    monkeypatch.setattr(browser, "_recover_cdp_port_from_singleton", lambda *a, **k: None)

    def _inodes(port):
        if port == 40141:
            return {7: {"::1"}}
        if port == 9333:
            return {8: {"::1"}}
        return {}

    def _holders(want):
        out = {}
        if 7 in want:
            out[4242] = {7}
            out[4243] = {7}
        if 8 in want:
            out[4240] = {8}
        return out

    monkeypatch.setattr(browser, "_loopback_listen_inodes_for_port", _inodes)
    monkeypatch.setattr(browser, "_pids_holding_socket_inodes", _holders)
    monkeypatch.setattr(
        browser,
        "_cdp_port_reachable",
        lambda port, hosts: port == 9333 and "::1" in hosts,
    )
    monkeypatch.setattr(
        browser, "_configured_cdp_override_url",
        lambda: "http://[::1]:40141",
    )
    _reset_dock_port_memory_for_tests()
    browser.remember_dock_cdp_port(9333)
    _last_dock_cdp_port[hermes_home_key()] = 40141
    (tmp_path / "DevToolsActivePort").write_text(
        "40141\n/devtools/browser/abc\n", encoding="utf-8",
    )
    assert browser.lock_listed_persist_port() == 9333
    assert browser.file_named_dock_listen_port() == 40141
    assert browser.running_instance_cdp_port(str(tmp_path)) == 9333
    assert browser.persist_live_dock_cdp_port() == 9333
    assert browser.last_known_dock_cdp_port() == 9333
    assert _last_dock_cdp_port.get(hermes_home_key()) == 9333
    assert _remembered_dock_attach_port() == 9333

    monkeypatch.setattr(browser, "_lock_pid", lambda d: None)
    monkeypatch.setattr(
        browser, "_loopback_listen_ports_for_pid", lambda pid: set(),
    )
    monkeypatch.setattr(
        browser, "_loopback_listen_targets_for_pid", lambda pid: set(),
    )
    assert browser.lock_listed_persist_port() == 9333
    assert browser.file_named_dock_listen_port() == 40141
    # Finding 185: chrome still inode-listens and TCP works. Stamp
    # that persist; do not shop leftover helpers.
    assert browser.running_instance_cdp_port(str(tmp_path)) == 9333
    assert browser.persist_live_dock_cdp_port() == 9333
    assert _remembered_dock_attach_port() == 9333
    assert _cdp_url_is_bot_desktop_browser("http://[::1]:40141") is True
    assert _cdp_url_is_bot_desktop_browser("http://[::1]:9333") is True
    assert _cdp_url_is_bot_desktop_browser("http://127.0.0.1:40141") is False
    assert _cdp_url_is_bot_desktop_browser("9222") is False
    assert browser.last_known_dock_cdp_port() == 9333
    assert _last_dock_cdp_port.get(hermes_home_key()) == 9333


def test_restart_empty_memory_does_not_stamp_helpers_over_persist_file(
    tmp_path, monkeypatch,
):
    """Finding 181: process restart must not hide persist-file chrome.

    persist_live / finding 174 stamp chrome into ``dock-cdp-port``.
    A new process has empty ``_last_dock_cdp_port``. Finding 179 then
    treated file-only persist as empty, so 164 / persist_live stamped
    leftover ``DevToolsActivePort`` helpers over chrome. Persist unique
    + leftover several is chrome. Finding 86 still stamps when persist
    equals DevTools. Finding 172 still prefers a different file-named
    live listen. 9222 and the other family stay unknown.
    """
    from hermes_constants import hermes_home_key
    from tools.browser_tool_session import (
        _cdp_url_is_bot_desktop_browser,
        _last_dock_cdp_port,
        _remembered_dock_attach_port,
        _reset_dock_port_memory_for_tests,
    )

    monkeypatch.setattr(runtime, "state_dir", lambda: tmp_path)
    monkeypatch.setattr(browser, "profile_dir", lambda: tmp_path)
    monkeypatch.setattr(browser, "_lock_pid", lambda d: None)
    monkeypatch.setattr(
        browser,
        "_chromium_cmdline_tokens",
        lambda pid: ["chrome", f"--user-data-dir={tmp_path}"],
    )
    monkeypatch.setattr(
        browser, "_loopback_listen_ports_for_pid", lambda pid: set(),
    )
    monkeypatch.setattr(
        browser, "_loopback_listen_targets_for_pid", lambda pid: set(),
    )
    monkeypatch.setattr(browser, "_recover_cdp_port_from_singleton", lambda *a, **k: None)

    def _inodes(port):
        if port == 40141:
            return {7: {"::1"}}
        if port == 9333:
            return {8: {"::1"}}
        return {}

    def _holders(want):
        out = {}
        if 7 in want:
            out[4242] = {7}
            out[4243] = {7}
        if 8 in want:
            out[4240] = {8}
        return out

    monkeypatch.setattr(browser, "_loopback_listen_inodes_for_port", _inodes)
    monkeypatch.setattr(browser, "_pids_holding_socket_inodes", _holders)
    monkeypatch.setattr(
        browser,
        "_cdp_port_reachable",
        lambda port, hosts: port == 40141 and "::1" in hosts,
    )
    monkeypatch.setattr(
        browser, "_configured_cdp_override_url",
        lambda: "http://[::1]:40141",
    )
    _reset_dock_port_memory_for_tests()
    browser.remember_dock_cdp_port(9333)
    (tmp_path / "DevToolsActivePort").write_text(
        "40141\n/devtools/browser/abc\n", encoding="utf-8",
    )
    assert _last_dock_cdp_port.get(hermes_home_key()) is None
    assert browser.last_known_dock_cdp_port() == 9333
    assert browser.lock_listed_persist_port() == 9333
    assert browser.file_named_dock_listen_port() == 40141
    assert browser.running_instance_cdp_port(str(tmp_path)) is None
    assert browser._configured_listen_port_for_this_jar() is None
    assert browser.persist_live_dock_cdp_port() is None
    assert browser.last_known_dock_cdp_port() == 9333
    assert _last_dock_cdp_port.get(hermes_home_key()) is None
    assert _remembered_dock_attach_port() == 9333
    assert _cdp_url_is_bot_desktop_browser("http://[::1]:40141") is True
    assert _remembered_dock_attach_port() == 9333
    assert _cdp_url_is_bot_desktop_browser("http://[::1]:9333") is True
    assert _cdp_url_is_bot_desktop_browser("http://127.0.0.1:40141") is False
    assert _cdp_url_is_bot_desktop_browser("9222") is False
    assert browser.last_known_dock_cdp_port() == 9333
    assert _last_dock_cdp_port.get(hermes_home_key()) is None

    _reset_dock_port_memory_for_tests()
    browser.remember_dock_cdp_port(40141)
    (tmp_path / "DevToolsActivePort").write_text(
        "9333\n/devtools/browser/abc\n", encoding="utf-8",
    )
    monkeypatch.setattr(
        browser,
        "_cdp_port_reachable",
        lambda port, hosts: port == 9333 and "::1" in hosts,
    )
    assert browser.lock_listed_persist_port() is None
    assert browser.file_named_dock_listen_port() == 9333
    assert browser.running_instance_cdp_port(str(tmp_path)) == 9333
    assert _remembered_dock_attach_port() == 9333

    _reset_dock_port_memory_for_tests()
    browser.remember_dock_cdp_port(40141)
    (tmp_path / "DevToolsActivePort").write_text(
        "40141\n/devtools/browser/abc\n", encoding="utf-8",
    )
    monkeypatch.setattr(
        browser,
        "_cdp_port_reachable",
        lambda port, hosts: port == 40141 and "::1" in hosts,
    )
    assert browser.lock_listed_persist_port() is None
    assert browser.running_instance_cdp_port(str(tmp_path)) == 40141
    assert browser.last_known_dock_cdp_port() == 40141


def test_running_instance_syncs_memory_so_missing_lock_keeps_chrome(
    tmp_path, monkeypatch,
):
    """Finding 182: running_instance must sync leftover memory before unlink.

    Finding 174 stamps chrome while the lock still lists persist and
    not leftover ``DevToolsActivePort`` helpers. Agent attach calls
    ``running_instance_cdp_port`` without persist_live. ``remember``
    writes the file only, so memory stayed on the leftover. Finding
    179 then treated that leftover as lock-listed after Take over
    unlinked SingletonLock, and attach followed leftover helpers.
    Sync memory on a live stamp. A miss must not clobber chrome
    memory. 9222 and the other family stay unknown.
    """
    from hermes_constants import hermes_home_key
    from tools.browser_tool_session import (
        _cdp_url_is_bot_desktop_browser,
        _last_dock_cdp_port,
        _remembered_dock_attach_port,
        _reset_dock_port_memory_for_tests,
    )

    monkeypatch.setattr(runtime, "state_dir", lambda: tmp_path)
    monkeypatch.setattr(browser, "profile_dir", lambda: tmp_path)
    monkeypatch.setattr(browser, "_lock_pid", lambda d: 4240)
    monkeypatch.setattr(
        browser,
        "_chromium_cmdline_tokens",
        lambda pid: ["chrome", f"--user-data-dir={tmp_path}"],
    )
    monkeypatch.setattr(
        browser, "_loopback_listen_ports_for_pid",
        lambda pid: {9333} if pid == 4240 else set(),
    )
    monkeypatch.setattr(
        browser, "_loopback_listen_targets_for_pid",
        lambda pid: {("::1", 9333)} if pid == 4240 else set(),
    )
    monkeypatch.setattr(browser, "_recover_cdp_port_from_singleton", lambda *a, **k: None)

    def _inodes(port):
        if port == 40141:
            return {7: {"::1"}}
        if port == 9333:
            return {8: {"::1"}}
        return {}

    def _holders(want):
        out = {}
        if 7 in want:
            out[4242] = {7}
            out[4243] = {7}
        if 8 in want:
            out[4240] = {8}
        return out

    monkeypatch.setattr(browser, "_loopback_listen_inodes_for_port", _inodes)
    monkeypatch.setattr(browser, "_pids_holding_socket_inodes", _holders)
    monkeypatch.setattr(
        browser,
        "_cdp_port_reachable",
        lambda port, hosts: port == 9333 and "::1" in hosts,
    )
    monkeypatch.setattr(
        browser, "_configured_cdp_override_url",
        lambda: "http://[::1]:40141",
    )
    _reset_dock_port_memory_for_tests()
    browser.remember_dock_cdp_port(9333)
    _last_dock_cdp_port[hermes_home_key()] = 40141
    (tmp_path / "DevToolsActivePort").write_text(
        "40141\n/devtools/browser/abc\n", encoding="utf-8",
    )
    assert browser.lock_listed_persist_port() == 9333
    assert browser.file_named_dock_listen_port() == 40141
    assert browser.running_instance_cdp_port(str(tmp_path)) == 9333
    assert browser.last_known_dock_cdp_port() == 9333
    assert _last_dock_cdp_port.get(hermes_home_key()) == 9333
    assert _remembered_dock_attach_port() == 9333

    monkeypatch.setattr(browser, "_lock_pid", lambda d: None)
    monkeypatch.setattr(
        browser, "_loopback_listen_ports_for_pid", lambda pid: set(),
    )
    monkeypatch.setattr(
        browser, "_loopback_listen_targets_for_pid", lambda pid: set(),
    )
    assert browser.lock_listed_persist_port() == 9333
    assert browser.file_named_dock_listen_port() == 40141
    # Finding 185: chrome still inode-listens and TCP works. Stamp
    # that persist; do not shop leftover helpers.
    assert browser.running_instance_cdp_port(str(tmp_path)) == 9333
    assert browser.persist_live_dock_cdp_port() == 9333
    assert _remembered_dock_attach_port() == 9333
    assert _cdp_url_is_bot_desktop_browser("http://[::1]:40141") is True
    assert _cdp_url_is_bot_desktop_browser("http://[::1]:9333") is True
    assert _cdp_url_is_bot_desktop_browser("http://127.0.0.1:40141") is False
    assert _cdp_url_is_bot_desktop_browser("9222") is False
    assert browser.last_known_dock_cdp_port() == 9333
    assert _last_dock_cdp_port.get(hermes_home_key()) == 9333


def test_lock_listed_leftover_file_does_not_hide_persist_chrome(
    tmp_path, monkeypatch,
):
    """Finding 183: lock listing leftover DevTools hid persist chrome.

    Chrome inherited leftover ``DevToolsActivePort`` fd, so the lock
    lists persist chrome and the leftover file. File TCP then stamped
    helpers over unique persist chrome. Finding 174 skipped persist
    when leftover already held that socket (166). Attach 172 preferred
    the file whenever the lock listed both. Persist unique + named
    several is leftover hiding chrome. Finding 172 chrome-switch
    (persist several / named unique) and unique+unique stay file.
    9222 and the other family stay unknown.
    """
    from hermes_constants import hermes_home_key
    from tools.browser_tool_session import (
        _cdp_url_is_bot_desktop_browser,
        _last_dock_cdp_port,
        _remembered_dock_attach_port,
        _reset_dock_port_memory_for_tests,
    )

    monkeypatch.setattr(runtime, "state_dir", lambda: tmp_path)
    monkeypatch.setattr(browser, "profile_dir", lambda: tmp_path)
    monkeypatch.setattr(browser, "_lock_pid", lambda d: 4240)
    monkeypatch.setattr(
        browser,
        "_chromium_cmdline_tokens",
        lambda pid: ["chrome", f"--user-data-dir={tmp_path}"],
    )
    monkeypatch.setattr(
        browser, "_loopback_listen_ports_for_pid",
        lambda pid: {9333, 40141} if pid == 4240 else set(),
    )
    monkeypatch.setattr(
        browser, "_loopback_listen_targets_for_pid",
        lambda pid: {("::1", 9333), ("::1", 40141)} if pid == 4240 else set(),
    )
    monkeypatch.setattr(browser, "_recover_cdp_port_from_singleton", lambda *a, **k: None)

    def _inodes(port):
        if port == 40141:
            return {7: {"::1"}}
        if port == 9333:
            return {8: {"::1"}}
        return {}

    def _holders(want):
        out = {}
        if 7 in want:
            out[4240] = {7}
            out[4242] = {7}
            out[4243] = {7}
        if 8 in want:
            out[4240] = {8}
        return out

    monkeypatch.setattr(browser, "_loopback_listen_inodes_for_port", _inodes)
    monkeypatch.setattr(browser, "_pids_holding_socket_inodes", _holders)
    monkeypatch.setattr(
        browser,
        "_cdp_port_reachable",
        lambda port, hosts: port in (9333, 40141) and "::1" in hosts,
    )
    monkeypatch.setattr(
        browser, "_configured_cdp_override_url",
        lambda: "http://[::1]:40141",
    )
    _reset_dock_port_memory_for_tests()
    browser.remember_dock_cdp_port(9333)
    _last_dock_cdp_port[hermes_home_key()] = 9333
    (tmp_path / "DevToolsActivePort").write_text(
        "40141\n/devtools/browser/abc\n", encoding="utf-8",
    )
    assert browser.lock_listed_persist_port() == 9333
    assert browser.file_named_dock_listen_port() == 40141
    assert browser.leftover_helpers_hide_persist_chrome(9333, 40141, str(tmp_path))
    assert browser.running_instance_cdp_port(str(tmp_path)) == 9333
    assert browser.last_known_dock_cdp_port() == 9333
    assert _last_dock_cdp_port.get(hermes_home_key()) == 9333
    assert _remembered_dock_attach_port() == 9333
    assert browser.persist_live_dock_cdp_port() == 9333
    assert browser.last_known_dock_cdp_port() == 9333
    assert _cdp_url_is_bot_desktop_browser("http://[::1]:40141") is True
    assert _cdp_url_is_bot_desktop_browser("http://[::1]:9333") is True
    assert _cdp_url_is_bot_desktop_browser("http://127.0.0.1:40141") is False
    assert _cdp_url_is_bot_desktop_browser("9222") is False
    assert _remembered_dock_attach_port() == 9333

    monkeypatch.setattr(
        browser,
        "_cdp_port_reachable",
        lambda port, hosts: port == 9333 and "::1" in hosts,
    )
    _reset_dock_port_memory_for_tests()
    browser.remember_dock_cdp_port(9333)
    _last_dock_cdp_port[hermes_home_key()] = 9333
    assert browser.running_instance_cdp_port(str(tmp_path)) == 9333
    assert browser.last_known_dock_cdp_port() == 9333
    assert _remembered_dock_attach_port() == 9333

    def _switch_holders(want):
        out = {}
        if 7 in want:
            out[4242] = {7}
            out[4243] = {7}
        if 8 in want:
            out[4240] = {8}
        return out

    monkeypatch.setattr(browser, "_pids_holding_socket_inodes", _switch_holders)
    monkeypatch.setattr(
        browser,
        "_cdp_port_reachable",
        lambda port, hosts: port in (9333, 40141) and "::1" in hosts,
    )
    _reset_dock_port_memory_for_tests()
    browser.remember_dock_cdp_port(40141)
    _last_dock_cdp_port[hermes_home_key()] = 40141
    (tmp_path / "DevToolsActivePort").write_text(
        "9333\n/devtools/browser/abc\n", encoding="utf-8",
    )
    assert browser.leftover_helpers_hide_persist_chrome(
        40141, 9333, str(tmp_path),
    ) is False
    assert browser.running_instance_cdp_port(str(tmp_path)) == 9333
    assert _remembered_dock_attach_port() == 9333

    def _unique_holders(want):
        out = {}
        if 7 in want:
            out[4242] = {7}
        if 8 in want:
            out[4240] = {8}
        return out

    monkeypatch.setattr(browser, "_pids_holding_socket_inodes", _unique_holders)
    _reset_dock_port_memory_for_tests()
    browser.remember_dock_cdp_port(9333)
    _last_dock_cdp_port[hermes_home_key()] = 9333
    (tmp_path / "DevToolsActivePort").write_text(
        "40141\n/devtools/browser/abc\n", encoding="utf-8",
    )
    assert browser.leftover_helpers_hide_persist_chrome(
        9333, 40141, str(tmp_path),
    ) is False
    assert browser.running_instance_cdp_port(str(tmp_path)) == 40141
    assert _remembered_dock_attach_port() == 40141


def test_first_persist_does_not_stamp_leftover_over_unique_lock_chrome(
    tmp_path, monkeypatch,
):
    """Finding 184: first persist must not stamp leftover over unique chrome.

    Persist may never have been stamped. Unique-listen recover is
    unknown when Chromium has several specific loopbacks (85). File
    TCP / finding 164 then stamped leftover ``DevToolsActivePort``
    helpers (several holders) as the first persist, even when the
    lock pid still had exactly one other unique this-jar listen.
    That listen is chrome. Finding 86 still stamps leftover when
    the lock has no other unique listen. Finding 172 chrome-switch
    and unique+unique stay file. 9222 and the other family stay
    unknown.
    """
    from hermes_constants import hermes_home_key
    from tools.browser_tool_session import (
        _cdp_url_is_bot_desktop_browser,
        _last_dock_cdp_port,
        _remembered_dock_attach_port,
        _reset_dock_port_memory_for_tests,
    )

    monkeypatch.setattr(runtime, "state_dir", lambda: tmp_path)
    monkeypatch.setattr(browser, "profile_dir", lambda: tmp_path)
    monkeypatch.setattr(browser, "_lock_pid", lambda d: 4240)
    monkeypatch.setattr(
        browser,
        "_chromium_cmdline_tokens",
        lambda pid: ["chrome", f"--user-data-dir={tmp_path}"],
    )
    monkeypatch.setattr(
        browser, "_loopback_listen_ports_for_pid",
        lambda pid: {9333, 40141} if pid == 4240 else set(),
    )
    monkeypatch.setattr(
        browser, "_loopback_listen_targets_for_pid",
        lambda pid: {("::1", 9333), ("::1", 40141)} if pid == 4240 else set(),
    )
    monkeypatch.setattr(browser, "_recover_cdp_port_from_singleton", lambda *a, **k: None)

    def _inodes(port):
        if port == 40141:
            return {7: {"::1"}}
        if port == 9333:
            return {8: {"::1"}}
        return {}

    def _holders(want):
        out = {}
        if 7 in want:
            out[4240] = {7}
            out[4242] = {7}
            out[4243] = {7}
        if 8 in want:
            out[4240] = {8}
        return out

    monkeypatch.setattr(browser, "_loopback_listen_inodes_for_port", _inodes)
    monkeypatch.setattr(browser, "_pids_holding_socket_inodes", _holders)
    monkeypatch.setattr(
        browser,
        "_cdp_port_reachable",
        lambda port, hosts: port in (9333, 40141) and "::1" in hosts,
    )
    monkeypatch.setattr(
        browser, "_configured_cdp_override_url",
        lambda: "http://[::1]:40141",
    )
    _reset_dock_port_memory_for_tests()
    (tmp_path / "DevToolsActivePort").write_text(
        "40141\n/devtools/browser/abc\n", encoding="utf-8",
    )
    assert browser.last_known_dock_cdp_port() is None
    assert _last_dock_cdp_port.get(hermes_home_key()) is None
    assert browser.unique_lock_chrome_hidden_by_leftover_file(
        40141, str(tmp_path),
    ) == 9333
    assert browser.lock_listed_persist_port() == 9333
    assert browser._this_jar_chromium_pid(str(tmp_path)) == 4240
    assert browser.file_named_dock_listen_port() == 40141
    assert browser.running_instance_cdp_port(str(tmp_path)) == 9333
    assert browser.last_known_dock_cdp_port() == 9333
    assert _last_dock_cdp_port.get(hermes_home_key()) == 9333
    assert _remembered_dock_attach_port() == 9333
    assert _cdp_url_is_bot_desktop_browser("http://[::1]:40141") is True
    assert _cdp_url_is_bot_desktop_browser("http://[::1]:9333") is True
    assert _cdp_url_is_bot_desktop_browser("http://127.0.0.1:40141") is False
    assert _cdp_url_is_bot_desktop_browser("9222") is False
    assert _remembered_dock_attach_port() == 9333

    monkeypatch.setattr(
        browser,
        "_cdp_port_reachable",
        lambda port, hosts: port == 9333 and "::1" in hosts,
    )
    _reset_dock_port_memory_for_tests()
    assert browser.running_instance_cdp_port(str(tmp_path)) == 9333
    assert _remembered_dock_attach_port() == 9333

    monkeypatch.setattr(
        browser, "_loopback_listen_ports_for_pid",
        lambda pid: {9333} if pid == 4240 else set(),
    )
    monkeypatch.setattr(
        browser, "_loopback_listen_targets_for_pid",
        lambda pid: {("::1", 9333)} if pid == 4240 else set(),
    )

    def _helpers_not_on_lock(want):
        out = {}
        if 7 in want:
            out[4242] = {7}
            out[4243] = {7}
        if 8 in want:
            out[4240] = {8}
        return out

    monkeypatch.setattr(browser, "_pids_holding_socket_inodes", _helpers_not_on_lock)
    monkeypatch.setattr(
        browser,
        "_cdp_port_reachable",
        lambda port, hosts: port in (9333, 40141) and "::1" in hosts,
    )
    _reset_dock_port_memory_for_tests()
    assert browser.lock_listed_persist_port() == 9333
    assert browser.running_instance_cdp_port(str(tmp_path)) == 9333
    assert browser.last_known_dock_cdp_port() == 9333
    assert _remembered_dock_attach_port() == 9333

    monkeypatch.setattr(
        browser, "_loopback_listen_ports_for_pid",
        lambda pid: {40141} if pid == 4240 else set(),
    )
    monkeypatch.setattr(
        browser, "_loopback_listen_targets_for_pid",
        lambda pid: {("::1", 40141)} if pid == 4240 else set(),
    )
    monkeypatch.setattr(
        browser,
        "_cdp_port_reachable",
        lambda port, hosts: port == 40141 and "::1" in hosts,
    )
    _reset_dock_port_memory_for_tests()
    assert browser.unique_lock_chrome_hidden_by_leftover_file(
        40141, str(tmp_path),
    ) is None
    assert browser.lock_listed_persist_port() is None
    assert browser.running_instance_cdp_port(str(tmp_path)) == 40141
    assert _remembered_dock_attach_port() == 40141


def test_missing_lock_first_persist_does_not_stamp_leftover_helpers(
    tmp_path, monkeypatch,
):
    """Finding 185: missing lock must not first-persist leftover helpers.

    Finding 184 needs the lock pid. Overlay / Take over can unlink
    ``SingletonLock`` (finding 161) before any live stamp. Persist
    never stamped. Unique-listen recover is unknown (85). File TCP /
    finding 164 then stamped leftover ``DevToolsActivePort`` helpers
    (several holders) as the first persist even though leftover
    holders still advertised chrome's other unique listen. Finding
    86 still stamps leftover when leftover holders name no other
    unique listen. Unique+unique stays file. 9222 and the other
    family stay unknown.
    """
    from hermes_constants import hermes_home_key
    from tools.browser_tool_session import (
        _cdp_url_is_bot_desktop_browser,
        _last_dock_cdp_port,
        _remembered_dock_attach_port,
        _reset_dock_port_memory_for_tests,
    )

    monkeypatch.setattr(runtime, "state_dir", lambda: tmp_path)
    monkeypatch.setattr(browser, "profile_dir", lambda: tmp_path)
    monkeypatch.setattr(browser, "_lock_pid", lambda d: None)
    monkeypatch.setattr(
        browser,
        "_chromium_cmdline_tokens",
        lambda pid: ["chrome", f"--user-data-dir={tmp_path}"],
    )
    monkeypatch.setattr(
        browser, "_loopback_listen_ports_for_pid",
        lambda pid: {9333, 40141} if pid == 4240 else set(),
    )
    monkeypatch.setattr(
        browser, "_loopback_listen_targets_for_pid",
        lambda pid: {("::1", 9333), ("::1", 40141)} if pid == 4240 else set(),
    )
    monkeypatch.setattr(browser, "_recover_cdp_port_from_singleton", lambda *a, **k: None)

    def _inodes(port):
        if port == 40141:
            return {7: {"::1"}}
        if port == 9333:
            return {8: {"::1"}}
        return {}

    def _holders(want):
        out = {}
        if 7 in want:
            out[4240] = {7}
            out[4242] = {7}
            out[4243] = {7}
        if 8 in want:
            out[4240] = {8}
        return out

    monkeypatch.setattr(browser, "_loopback_listen_inodes_for_port", _inodes)
    monkeypatch.setattr(browser, "_pids_holding_socket_inodes", _holders)
    monkeypatch.setattr(
        browser,
        "_cdp_port_reachable",
        lambda port, hosts: port in (9333, 40141) and "::1" in hosts,
    )
    monkeypatch.setattr(
        browser, "_configured_cdp_override_url",
        lambda: "http://[::1]:40141",
    )
    _reset_dock_port_memory_for_tests()
    (tmp_path / "DevToolsActivePort").write_text(
        "40141\n/devtools/browser/abc\n", encoding="utf-8",
    )
    assert browser.last_known_dock_cdp_port() is None
    assert _last_dock_cdp_port.get(hermes_home_key()) is None
    assert browser.unique_lock_chrome_hidden_by_leftover_file(
        40141, str(tmp_path),
    ) == 9333
    assert browser.lock_listed_persist_port() == 9333
    assert browser._this_jar_chromium_pid(str(tmp_path)) == 4240
    assert browser.file_named_dock_listen_port() == 40141
    assert browser.running_instance_cdp_port(str(tmp_path)) == 9333
    assert browser.last_known_dock_cdp_port() == 9333
    assert _last_dock_cdp_port.get(hermes_home_key()) == 9333
    assert _remembered_dock_attach_port() == 9333
    assert _cdp_url_is_bot_desktop_browser("http://[::1]:40141") is True
    assert _cdp_url_is_bot_desktop_browser("http://[::1]:9333") is True
    assert _cdp_url_is_bot_desktop_browser("http://127.0.0.1:40141") is False
    assert _cdp_url_is_bot_desktop_browser("9222") is False
    assert _remembered_dock_attach_port() == 9333

    monkeypatch.setattr(
        browser,
        "_cdp_port_reachable",
        lambda port, hosts: port == 40141 and "::1" in hosts,
    )
    _reset_dock_port_memory_for_tests()
    (tmp_path / "DevToolsActivePort").write_text(
        "40141\n/devtools/browser/abc\n", encoding="utf-8",
    )
    assert browser.lock_listed_persist_port() == 9333
    assert browser.running_instance_cdp_port(str(tmp_path)) is None
    assert browser.last_known_dock_cdp_port() is None
    assert _remembered_dock_attach_port() == 9333

    monkeypatch.setattr(
        browser,
        "_cdp_port_reachable",
        lambda port, hosts: port == 40141 and "::1" in hosts,
    )
    monkeypatch.setattr(
        browser, "_loopback_listen_ports_for_pid",
        lambda pid: {40141} if pid == 4240 else set(),
    )
    monkeypatch.setattr(
        browser, "_loopback_listen_targets_for_pid",
        lambda pid: {("::1", 40141)} if pid == 4240 else set(),
    )

    def _leftover_only(want):
        out = {}
        if 7 in want:
            out[4240] = {7}
            out[4242] = {7}
            out[4243] = {7}
        return out

    monkeypatch.setattr(browser, "_pids_holding_socket_inodes", _leftover_only)
    _reset_dock_port_memory_for_tests()
    (tmp_path / "DevToolsActivePort").write_text(
        "40141\n/devtools/browser/abc\n", encoding="utf-8",
    )
    assert browser.unique_lock_chrome_hidden_by_leftover_file(
        40141, str(tmp_path),
    ) is None
    assert browser.lock_listed_persist_port() is None
    assert browser.running_instance_cdp_port(str(tmp_path)) == 40141
    assert _remembered_dock_attach_port() == 40141

    def _unique_unique(want):
        out = {}
        if 7 in want:
            out[4242] = {7}
        if 8 in want:
            out[4240] = {8}
        return out

    monkeypatch.setattr(browser, "_pids_holding_socket_inodes", _unique_unique)
    monkeypatch.setattr(
        browser, "_loopback_listen_ports_for_pid",
        lambda pid: {9333, 40141} if pid == 4240 else set(),
    )
    monkeypatch.setattr(
        browser, "_loopback_listen_targets_for_pid",
        lambda pid: {("::1", 9333), ("::1", 40141)} if pid == 4240 else set(),
    )
    monkeypatch.setattr(
        browser,
        "_cdp_port_reachable",
        lambda port, hosts: port in (9333, 40141) and "::1" in hosts,
    )
    _reset_dock_port_memory_for_tests()
    (tmp_path / "DevToolsActivePort").write_text(
        "40141\n/devtools/browser/abc\n", encoding="utf-8",
    )
    assert browser.unique_lock_chrome_hidden_by_leftover_file(
        40141, str(tmp_path),
    ) is None
    assert browser.lock_listed_persist_port() is None
    assert browser.running_instance_cdp_port(str(tmp_path)) == 40141
    assert _remembered_dock_attach_port() == 40141


def test_missing_lock_first_persist_does_not_stamp_parent_chrome_as_leftover(
    tmp_path, monkeypatch,
):
    """Finding 186: leftover helpers that dropped chrome's DevTools fd.

    Finding 185 scans leftover holders' listens. Overlay can unlink
    ``SingletonLock`` before any persist stamp. Helpers that dropped
    chrome's inherited fd no longer advertise that unique listen, so
    finding 185 misses and file TCP / finding 164 stamped leftover
    ``DevToolsActivePort`` helpers as the first persist. Those helpers
    still have chrome as PPID. That parent's other unique this-jar
    listen is chrome. A parent that does not name this jar stays
    finding 86. Several unique parent ports stay unknown. Unique+unique
    stays file. 9222 and the other family stay unknown.
    """
    from hermes_constants import hermes_home_key
    from tools.browser_tool_session import (
        _cdp_url_is_bot_desktop_browser,
        _last_dock_cdp_port,
        _remembered_dock_attach_port,
        _reset_dock_port_memory_for_tests,
    )

    leftover_helpers = {4242, 4243}

    monkeypatch.setattr(runtime, "state_dir", lambda: tmp_path)
    monkeypatch.setattr(browser, "profile_dir", lambda: tmp_path)
    monkeypatch.setattr(browser, "_lock_pid", lambda d: None)
    monkeypatch.setattr(
        browser,
        "_chromium_cmdline_tokens",
        lambda pid: ["chrome", f"--user-data-dir={tmp_path}"],
    )
    monkeypatch.setattr(
        browser, "_loopback_listen_ports_for_pid",
        lambda pid: {9333} if pid == 4240 else (
            {40141} if pid in leftover_helpers else set()
        ),
    )
    monkeypatch.setattr(
        browser, "_loopback_listen_targets_for_pid",
        lambda pid: {("::1", 9333)} if pid == 4240 else (
            {("::1", 40141)} if pid in leftover_helpers else set()
        ),
    )
    monkeypatch.setattr(browser, "_recover_cdp_port_from_singleton", lambda *a, **k: None)
    monkeypatch.setattr(
        browser,
        "_proc_ppid",
        lambda pid: 4240 if pid in leftover_helpers else None,
    )

    def _inodes(port):
        if port == 40141:
            return {7: {"::1"}}
        if port == 9333:
            return {8: {"::1"}}
        return {}

    def _holders(want):
        out = {}
        if 7 in want:
            out[4242] = {7}
            out[4243] = {7}
        if 8 in want:
            out[4240] = {8}
        return out

    monkeypatch.setattr(browser, "_loopback_listen_inodes_for_port", _inodes)
    monkeypatch.setattr(browser, "_pids_holding_socket_inodes", _holders)
    monkeypatch.setattr(
        browser,
        "_cdp_port_reachable",
        lambda port, hosts: port in (9333, 40141) and "::1" in hosts,
    )
    monkeypatch.setattr(
        browser, "_configured_cdp_override_url",
        lambda: "http://[::1]:40141",
    )
    _reset_dock_port_memory_for_tests()
    (tmp_path / "DevToolsActivePort").write_text(
        "40141\n/devtools/browser/abc\n", encoding="utf-8",
    )
    assert browser.last_known_dock_cdp_port() is None
    assert _last_dock_cdp_port.get(hermes_home_key()) is None
    assert browser.unique_lock_chrome_hidden_by_leftover_file(
        40141, str(tmp_path),
    ) == 9333
    assert browser.lock_listed_persist_port() == 9333
    assert browser._this_jar_chromium_pid(str(tmp_path)) == 4240
    assert browser.file_named_dock_listen_port() == 40141
    assert browser.running_instance_cdp_port(str(tmp_path)) == 9333
    assert browser.last_known_dock_cdp_port() == 9333
    assert _last_dock_cdp_port.get(hermes_home_key()) == 9333
    assert _remembered_dock_attach_port() == 9333
    assert browser.persist_live_dock_cdp_port() == 9333
    assert (tmp_path / "dock-cdp-port").read_text(encoding="utf-8") == "9333"
    assert _cdp_url_is_bot_desktop_browser("http://[::1]:40141") is True
    assert _cdp_url_is_bot_desktop_browser("http://[::1]:9333") is True
    assert _cdp_url_is_bot_desktop_browser("http://127.0.0.1:40141") is False
    assert _cdp_url_is_bot_desktop_browser("9222") is False
    assert _remembered_dock_attach_port() == 9333

    monkeypatch.setattr(
        browser,
        "_cdp_port_reachable",
        lambda port, hosts: port == 40141 and "::1" in hosts,
    )
    _reset_dock_port_memory_for_tests()
    (tmp_path / "DevToolsActivePort").write_text(
        "40141\n/devtools/browser/abc\n", encoding="utf-8",
    )
    assert browser.lock_listed_persist_port() == 9333
    assert browser.running_instance_cdp_port(str(tmp_path)) is None
    assert browser.last_known_dock_cdp_port() is None
    assert _remembered_dock_attach_port() == 9333

    monkeypatch.setattr(
        browser,
        "_cdp_port_reachable",
        lambda port, hosts: port in (9333, 40141) and "::1" in hosts,
    )
    monkeypatch.setattr(
        browser,
        "_proc_ppid",
        lambda pid: 4240 if pid == 4242 else (4250 if pid == 4243 else None),
    )
    monkeypatch.setattr(
        browser, "_loopback_listen_ports_for_pid",
        lambda pid: {9333} if pid == 4240 else (
            {9444} if pid == 4250 else (
                {40141} if pid in leftover_helpers else set()
            )
        ),
    )
    monkeypatch.setattr(
        browser, "_loopback_listen_targets_for_pid",
        lambda pid: {("::1", 9333)} if pid == 4240 else (
            {("::1", 9444)} if pid == 4250 else (
                {("::1", 40141)} if pid in leftover_helpers else set()
            )
        ),
    )

    def _several_parent_inodes(port):
        if port == 40141:
            return {7: {"::1"}}
        if port == 9333:
            return {8: {"::1"}}
        if port == 9444:
            return {9: {"::1"}}
        return {}

    def _several_parent_holders(want):
        out = {}
        if 7 in want:
            out[4242] = {7}
            out[4243] = {7}
        if 8 in want:
            out[4240] = {8}
        if 9 in want:
            out[4250] = {9}
        return out

    monkeypatch.setattr(browser, "_loopback_listen_inodes_for_port", _several_parent_inodes)
    monkeypatch.setattr(browser, "_pids_holding_socket_inodes", _several_parent_holders)
    _reset_dock_port_memory_for_tests()
    (tmp_path / "DevToolsActivePort").write_text(
        "40141\n/devtools/browser/abc\n", encoding="utf-8",
    )
    assert browser.unique_lock_chrome_hidden_by_leftover_file(
        40141, str(tmp_path),
    ) is None
    assert browser.lock_listed_persist_port() is None

    monkeypatch.setattr(
        browser,
        "_proc_ppid",
        lambda pid: 9999 if pid in leftover_helpers else None,
    )
    monkeypatch.setattr(
        browser,
        "_chromium_cmdline_tokens",
        lambda pid: (
            ["chrome", "--user-data-dir=/other"]
            if pid == 9999
            else ["chrome", f"--user-data-dir={tmp_path}"]
        ),
    )
    monkeypatch.setattr(browser, "_loopback_listen_inodes_for_port", _inodes)
    monkeypatch.setattr(browser, "_pids_holding_socket_inodes", _holders)
    monkeypatch.setattr(
        browser, "_loopback_listen_ports_for_pid",
        lambda pid: {9333} if pid == 4240 else (
            {40141} if pid in leftover_helpers else set()
        ),
    )
    monkeypatch.setattr(
        browser, "_loopback_listen_targets_for_pid",
        lambda pid: {("::1", 9333)} if pid == 4240 else (
            {("::1", 40141)} if pid in leftover_helpers else set()
        ),
    )
    _reset_dock_port_memory_for_tests()
    (tmp_path / "DevToolsActivePort").write_text(
        "40141\n/devtools/browser/abc\n", encoding="utf-8",
    )
    assert browser.unique_lock_chrome_hidden_by_leftover_file(
        40141, str(tmp_path),
    ) is None
    assert browser.lock_listed_persist_port() is None
    assert browser.running_instance_cdp_port(str(tmp_path)) == 40141
    assert _remembered_dock_attach_port() == 40141

    def _unique_unique(want):
        out = {}
        if 7 in want:
            out[4242] = {7}
        if 8 in want:
            out[4240] = {8}
        return out

    monkeypatch.setattr(browser, "_pids_holding_socket_inodes", _unique_unique)
    monkeypatch.setattr(
        browser,
        "_chromium_cmdline_tokens",
        lambda pid: ["chrome", f"--user-data-dir={tmp_path}"],
    )
    monkeypatch.setattr(
        browser, "_loopback_listen_ports_for_pid",
        lambda pid: {9333, 40141} if pid == 4240 else set(),
    )
    monkeypatch.setattr(
        browser, "_loopback_listen_targets_for_pid",
        lambda pid: {("::1", 9333), ("::1", 40141)} if pid == 4240 else set(),
    )
    monkeypatch.setattr(
        browser,
        "_cdp_port_reachable",
        lambda port, hosts: port in (9333, 40141) and "::1" in hosts,
    )
    _reset_dock_port_memory_for_tests()
    (tmp_path / "DevToolsActivePort").write_text(
        "40141\n/devtools/browser/abc\n", encoding="utf-8",
    )
    assert browser.unique_lock_chrome_hidden_by_leftover_file(
        40141, str(tmp_path),
    ) is None
    assert browser.lock_listed_persist_port() is None
    assert browser.running_instance_cdp_port(str(tmp_path)) == 40141
    assert _remembered_dock_attach_port() == 40141


def test_missing_lock_does_not_hide_parent_chrome_pid_from_owner_session(
    tmp_path, monkeypatch,
):
    """Finding 187: leftover helpers hid chrome pid after lock unlink.

    Findings 185 / 186 already named unique chrome. Skip-kill /
    ``shared_chromium_owner_session`` still used unique leftover-file
    holders (161). Several leftover ``DevToolsActivePort`` helpers
    made chrome unknown, so Take over treated the daemon that
    spawned chrome as attach-only leftover and tree-killed the
    Browser a human is typing into. Unique holder of hidden chrome
    is that pid. Unique leftover file stays 161. Leftover-only
    several stays unknown. Unique+unique stays the file holder.
    """
    leftover_helpers = {4242, 4243}

    monkeypatch.setattr(runtime, "state_dir", lambda: tmp_path)
    monkeypatch.setattr(browser, "profile_dir", lambda: tmp_path)
    monkeypatch.setattr(browser, "_lock_pid", lambda d: None)
    monkeypatch.setattr(
        browser,
        "_chromium_cmdline_tokens",
        lambda pid: ["chrome", f"--user-data-dir={tmp_path}"],
    )
    monkeypatch.setattr(
        browser, "_loopback_listen_ports_for_pid",
        lambda pid: {9333} if pid == 4240 else (
            {40141} if pid in leftover_helpers else set()
        ),
    )
    monkeypatch.setattr(
        browser, "_loopback_listen_targets_for_pid",
        lambda pid: {("::1", 9333)} if pid == 4240 else (
            {("::1", 40141)} if pid in leftover_helpers else set()
        ),
    )
    monkeypatch.setattr(browser, "_recover_cdp_port_from_singleton", lambda *a, **k: None)
    monkeypatch.setattr(
        browser,
        "_proc_ppid",
        lambda pid: 4240 if pid in leftover_helpers else None,
    )
    monkeypatch.setattr(
        browser,
        "_launched_by_session",
        lambda pid: "h_review" if pid == 4240 else None,
    )

    def _inodes(port):
        if port == 40141:
            return {7: {"::1"}}
        if port == 9333:
            return {8: {"::1"}}
        return {}

    def _holders(want):
        out = {}
        if 7 in want:
            out[4242] = {7}
            out[4243] = {7}
        if 8 in want:
            out[4240] = {8}
        return out

    monkeypatch.setattr(browser, "_loopback_listen_inodes_for_port", _inodes)
    monkeypatch.setattr(browser, "_pids_holding_socket_inodes", _holders)
    monkeypatch.setattr(
        browser,
        "_cdp_port_reachable",
        lambda port, hosts: port in (9333, 40141) and "::1" in hosts,
    )
    monkeypatch.setattr(browser, "_configured_cdp_override_url", lambda: "")
    (tmp_path / "DevToolsActivePort").write_text(
        "40141\n/devtools/browser/abc\n", encoding="utf-8",
    )
    assert browser.unique_lock_chrome_hidden_by_leftover_file(
        40141, str(tmp_path),
    ) == 9333
    assert browser._this_jar_chromium_pid(str(tmp_path)) == 4240
    assert browser.shared_chromium_owner_session() == "h_review"

    def _leftover_only(want):
        out = {}
        if 7 in want:
            out[4240] = {7}
            out[4242] = {7}
            out[4243] = {7}
        return out

    monkeypatch.setattr(browser, "_pids_holding_socket_inodes", _leftover_only)
    monkeypatch.setattr(
        browser, "_loopback_listen_ports_for_pid",
        lambda pid: {40141} if pid == 4240 else (
            {40141} if pid in leftover_helpers else set()
        ),
    )
    monkeypatch.setattr(
        browser, "_loopback_listen_targets_for_pid",
        lambda pid: {("::1", 40141)} if pid == 4240 else (
            {("::1", 40141)} if pid in leftover_helpers else set()
        ),
    )
    assert browser.unique_lock_chrome_hidden_by_leftover_file(
        40141, str(tmp_path),
    ) is None
    assert browser._this_jar_chromium_pid(str(tmp_path)) is None
    assert browser.shared_chromium_owner_session() is None

    def _unique_file(want):
        out = {}
        if 7 in want:
            out[4242] = {7}
        return out

    monkeypatch.setattr(browser, "_pids_holding_socket_inodes", _unique_file)
    monkeypatch.setattr(
        browser,
        "_launched_by_session",
        lambda pid: "h_helper" if pid == 4242 else None,
    )
    assert browser._this_jar_chromium_pid(str(tmp_path)) == 4242
    assert browser.shared_chromium_owner_session() == "h_helper"


def test_leftover_inherited_chrome_listen_does_not_stamp_helpers(
    tmp_path, monkeypatch,
):
    """Finding 188: leftover inherited chrome's unique CDP listen.

    Findings 184 / 185 / 186 required n==1 this-jar holders on
    chrome's other listen. Real Chromium children inherit that
    listen fd and still name ``--user-data-dir`` (finding 163), so
    n==1 misses and file TCP / finding 164 stamped leftover
    ``DevToolsActivePort`` helpers as the first persist. Overlay
    can unlink SingletonLock, so skip-kill also missed chrome
    (finding 187). Exactly one leftover-shared listen with chrome
    is chrome. Leftover-only several stays unknown. Unique+unique
    stays file. Several leftover-shared listens stay unknown.
    """
    from hermes_constants import hermes_home_key
    from tools.browser_tool_session import (
        _cdp_url_is_bot_desktop_browser,
        _last_dock_cdp_port,
        _remembered_dock_attach_port,
        _reset_dock_port_memory_for_tests,
    )

    leftover_helpers = {4242, 4243}

    monkeypatch.setattr(runtime, "state_dir", lambda: tmp_path)
    monkeypatch.setattr(browser, "profile_dir", lambda: tmp_path)
    monkeypatch.setattr(browser, "_lock_pid", lambda d: None)
    monkeypatch.setattr(
        browser,
        "_chromium_cmdline_tokens",
        lambda pid: ["chrome", f"--user-data-dir={tmp_path}"],
    )
    monkeypatch.setattr(
        browser, "_loopback_listen_ports_for_pid",
        lambda pid: {9333, 40141} if pid in leftover_helpers else (
            {9333} if pid == 4240 else set()
        ),
    )
    monkeypatch.setattr(
        browser, "_loopback_listen_targets_for_pid",
        lambda pid: {("::1", 9333), ("::1", 40141)} if pid in leftover_helpers else (
            {("::1", 9333)} if pid == 4240 else set()
        ),
    )
    monkeypatch.setattr(browser, "_recover_cdp_port_from_singleton", lambda *a, **k: None)
    monkeypatch.setattr(
        browser,
        "_proc_ppid",
        lambda pid: 4240 if pid in leftover_helpers else None,
    )
    monkeypatch.setattr(
        browser,
        "_launched_by_session",
        lambda pid: "h_review" if pid == 4240 else None,
    )

    def _inodes(port):
        if port == 40141:
            return {7: {"::1"}}
        if port == 9333:
            return {8: {"::1"}}
        if port == 9444:
            return {9: {"::1"}}
        return {}

    def _holders(want):
        out = {}
        if 7 in want:
            out[4242] = {7}
            out[4243] = {7}
        if 8 in want:
            out[4240] = {8}
            out[4242] = {8}
            out[4243] = {8}
        return out

    monkeypatch.setattr(browser, "_loopback_listen_inodes_for_port", _inodes)
    monkeypatch.setattr(browser, "_pids_holding_socket_inodes", _holders)
    monkeypatch.setattr(
        browser,
        "_cdp_port_reachable",
        lambda port, hosts: port in (9333, 40141) and "::1" in hosts,
    )
    monkeypatch.setattr(
        browser, "_configured_cdp_override_url",
        lambda: "http://[::1]:40141",
    )
    _reset_dock_port_memory_for_tests()
    (tmp_path / "DevToolsActivePort").write_text(
        "40141\n/devtools/browser/abc\n", encoding="utf-8",
    )
    assert browser.last_known_dock_cdp_port() is None
    assert _last_dock_cdp_port.get(hermes_home_key()) is None
    assert browser.unique_lock_chrome_hidden_by_leftover_file(
        40141, str(tmp_path),
    ) == 9333
    assert browser.lock_listed_persist_port() == 9333
    assert browser._this_jar_chromium_pid(str(tmp_path)) == 4240
    assert browser.shared_chromium_owner_session() == "h_review"
    assert browser.file_named_dock_listen_port() == 40141
    assert browser.running_instance_cdp_port(str(tmp_path)) == 9333
    assert browser.last_known_dock_cdp_port() == 9333
    assert _last_dock_cdp_port.get(hermes_home_key()) == 9333
    assert _remembered_dock_attach_port() == 9333
    assert browser.persist_live_dock_cdp_port() == 9333
    assert (tmp_path / "dock-cdp-port").read_text(encoding="utf-8") == "9333"
    assert _cdp_url_is_bot_desktop_browser("http://[::1]:40141") is True
    assert _cdp_url_is_bot_desktop_browser("http://[::1]:9333") is True
    assert _cdp_url_is_bot_desktop_browser("http://127.0.0.1:40141") is False
    assert _cdp_url_is_bot_desktop_browser("9222") is False
    assert _remembered_dock_attach_port() == 9333

    monkeypatch.setattr(
        browser,
        "_cdp_port_reachable",
        lambda port, hosts: port == 40141 and "::1" in hosts,
    )
    _reset_dock_port_memory_for_tests()
    (tmp_path / "DevToolsActivePort").write_text(
        "40141\n/devtools/browser/abc\n", encoding="utf-8",
    )
    assert browser.lock_listed_persist_port() == 9333
    assert browser._this_jar_chromium_pid(str(tmp_path)) == 4240
    assert browser.running_instance_cdp_port(str(tmp_path)) is None
    assert browser.last_known_dock_cdp_port() is None
    assert _remembered_dock_attach_port() == 9333

    monkeypatch.setattr(
        browser,
        "_cdp_port_reachable",
        lambda port, hosts: port in (9333, 40141) and "::1" in hosts,
    )
    monkeypatch.setattr(browser, "_lock_pid", lambda d: 4240)
    monkeypatch.setattr(
        browser, "_loopback_listen_ports_for_pid",
        lambda pid: {9333, 40141} if pid == 4240 else (
            {9333, 40141} if pid in leftover_helpers else set()
        ),
    )
    monkeypatch.setattr(
        browser, "_loopback_listen_targets_for_pid",
        lambda pid: {("::1", 9333), ("::1", 40141)} if pid in (
            4240, 4242, 4243,
        ) else set(),
    )
    _reset_dock_port_memory_for_tests()
    (tmp_path / "DevToolsActivePort").write_text(
        "40141\n/devtools/browser/abc\n", encoding="utf-8",
    )
    assert browser.unique_lock_chrome_hidden_by_leftover_file(
        40141, str(tmp_path),
    ) == 9333
    assert browser.lock_listed_persist_port() == 9333
    assert browser._this_jar_chromium_pid(str(tmp_path)) == 4240
    assert browser.running_instance_cdp_port(str(tmp_path)) == 9333
    assert _remembered_dock_attach_port() == 9333

    def _both_fds(want):
        out = {}
        if 7 in want:
            out[4240] = {7}
            out[4242] = {7}
            out[4243] = {7}
        if 8 in want:
            out[4240] = {8}
            out[4242] = {8}
            out[4243] = {8}
        return out

    monkeypatch.setattr(browser, "_lock_pid", lambda d: None)
    monkeypatch.setattr(browser, "_pids_holding_socket_inodes", _both_fds)
    monkeypatch.setattr(
        browser, "_loopback_listen_ports_for_pid",
        lambda pid: {9333, 40141} if pid in (4240, 4242, 4243) else set(),
    )
    monkeypatch.setattr(
        browser, "_loopback_listen_targets_for_pid",
        lambda pid: {("::1", 9333), ("::1", 40141)} if pid in (
            4240, 4242, 4243,
        ) else set(),
    )
    _reset_dock_port_memory_for_tests()
    (tmp_path / "DevToolsActivePort").write_text(
        "40141\n/devtools/browser/abc\n", encoding="utf-8",
    )
    assert browser.unique_lock_chrome_hidden_by_leftover_file(
        40141, str(tmp_path),
    ) == 9333
    assert browser.lock_listed_persist_port() == 9333
    assert browser._this_jar_chromium_pid(str(tmp_path)) == 4240
    assert browser.running_instance_cdp_port(str(tmp_path)) == 9333
    assert _remembered_dock_attach_port() == 9333

    monkeypatch.setattr(browser, "_lock_pid", lambda d: None)
    monkeypatch.setattr(
        browser, "_loopback_listen_ports_for_pid",
        lambda pid: {9333, 9444, 40141} if pid in leftover_helpers else (
            {9333, 9444} if pid == 4240 else set()
        ),
    )
    monkeypatch.setattr(
        browser, "_loopback_listen_targets_for_pid",
        lambda pid: {("::1", 9333), ("::1", 9444), ("::1", 40141)} if pid in leftover_helpers else (
            {("::1", 9333), ("::1", 9444)} if pid == 4240 else set()
        ),
    )

    def _several_shared(want):
        out = {}
        if 7 in want:
            out[4242] = {7}
            out[4243] = {7}
        if 8 in want:
            out[4240] = {8}
            out[4242] = {8}
            out[4243] = {8}
        if 9 in want:
            out[4240] = {9}
            out[4242] = {9}
            out[4243] = {9}
        return out

    monkeypatch.setattr(browser, "_pids_holding_socket_inodes", _several_shared)
    monkeypatch.setattr(
        browser,
        "_cdp_port_reachable",
        lambda port, hosts: port in (9333, 40141, 9444) and "::1" in hosts,
    )
    _reset_dock_port_memory_for_tests()
    (tmp_path / "DevToolsActivePort").write_text(
        "40141\n/devtools/browser/abc\n", encoding="utf-8",
    )
    assert browser.unique_lock_chrome_hidden_by_leftover_file(
        40141, str(tmp_path),
    ) is None
    assert browser.lock_listed_persist_port() is None
    assert browser._this_jar_chromium_pid(str(tmp_path)) is None
    assert browser.running_instance_cdp_port(str(tmp_path)) == 40141
    assert _remembered_dock_attach_port() == 40141

    def _leftover_only(want):
        out = {}
        if 7 in want:
            out[4240] = {7}
            out[4242] = {7}
            out[4243] = {7}
        return out

    monkeypatch.setattr(browser, "_pids_holding_socket_inodes", _leftover_only)
    monkeypatch.setattr(
        browser, "_loopback_listen_ports_for_pid",
        lambda pid: {40141} if pid in (4240, 4242, 4243) else set(),
    )
    monkeypatch.setattr(
        browser, "_loopback_listen_targets_for_pid",
        lambda pid: {("::1", 40141)} if pid in (4240, 4242, 4243) else set(),
    )
    monkeypatch.setattr(
        browser,
        "_cdp_port_reachable",
        lambda port, hosts: port == 40141 and "::1" in hosts,
    )
    _reset_dock_port_memory_for_tests()
    (tmp_path / "DevToolsActivePort").write_text(
        "40141\n/devtools/browser/abc\n", encoding="utf-8",
    )
    assert browser.unique_lock_chrome_hidden_by_leftover_file(
        40141, str(tmp_path),
    ) is None
    assert browser.lock_listed_persist_port() is None
    assert browser._this_jar_chromium_pid(str(tmp_path)) is None
    assert browser.running_instance_cdp_port(str(tmp_path)) == 40141
    assert _remembered_dock_attach_port() == 40141

    def _unique_unique(want):
        out = {}
        if 7 in want:
            out[4242] = {7}
        if 8 in want:
            out[4240] = {8}
        return out

    monkeypatch.setattr(browser, "_pids_holding_socket_inodes", _unique_unique)
    monkeypatch.setattr(
        browser, "_loopback_listen_ports_for_pid",
        lambda pid: {9333, 40141} if pid == 4240 else (
            {40141} if pid == 4242 else set()
        ),
    )
    monkeypatch.setattr(
        browser, "_loopback_listen_targets_for_pid",
        lambda pid: {("::1", 9333), ("::1", 40141)} if pid == 4240 else (
            {("::1", 40141)} if pid == 4242 else set()
        ),
    )
    monkeypatch.setattr(
        browser,
        "_cdp_port_reachable",
        lambda port, hosts: port in (9333, 40141) and "::1" in hosts,
    )
    _reset_dock_port_memory_for_tests()
    (tmp_path / "DevToolsActivePort").write_text(
        "40141\n/devtools/browser/abc\n", encoding="utf-8",
    )
    assert browser.unique_lock_chrome_hidden_by_leftover_file(
        40141, str(tmp_path),
    ) is None
    assert browser.lock_listed_persist_port() is None
    assert browser.running_instance_cdp_port(str(tmp_path)) == 40141
    assert _remembered_dock_attach_port() == 40141


def test_leftover_connect_only_chrome_zygote_does_not_stamp_helpers(
    tmp_path, monkeypatch,
):
    """Finding 189: leftover is connect-only; chrome + zygote hold CDP.

    Finding 188 required holders == leftover ∪ {chrome}. Real leftover
    CRI / lighthouse clients connect to ``DevToolsActivePort`` without
    inheriting chrome's listen, and Chromium's zygote / GPU / utility
    children inherit that listen and name ``--user-data-dir``. n==1
    and leftover-shared equality both miss, so first persist stamped
    leftover helpers. Overlay can unlink SingletonLock, so skip-kill
    also missed chrome (finding 187). Chrome plus this-jar children
    of chrome is still chrome. Leftover-only several stays unknown.
    Unique+unique stays file. Several leftover-shared listens stay
    unknown.
    """
    from hermes_constants import hermes_home_key
    from tools.browser_tool_session import (
        _cdp_url_is_bot_desktop_browser,
        _last_dock_cdp_port,
        _remembered_dock_attach_port,
        _reset_dock_port_memory_for_tests,
    )

    leftover_helpers = {4242, 4243}
    chrome_family = {4240, 4241}

    monkeypatch.setattr(runtime, "state_dir", lambda: tmp_path)
    monkeypatch.setattr(browser, "profile_dir", lambda: tmp_path)
    monkeypatch.setattr(browser, "_lock_pid", lambda d: None)
    monkeypatch.setattr(
        browser,
        "_chromium_cmdline_tokens",
        lambda pid: ["chrome", f"--user-data-dir={tmp_path}"],
    )
    monkeypatch.setattr(
        browser, "_loopback_listen_ports_for_pid",
        lambda pid: {40141} if pid in leftover_helpers else (
            {9333} if pid in chrome_family else set()
        ),
    )
    monkeypatch.setattr(
        browser, "_loopback_listen_targets_for_pid",
        lambda pid: {("::1", 40141)} if pid in leftover_helpers else (
            {("::1", 9333)} if pid in chrome_family else set()
        ),
    )
    monkeypatch.setattr(browser, "_recover_cdp_port_from_singleton", lambda *a, **k: None)
    monkeypatch.setattr(
        browser,
        "_proc_ppid",
        lambda pid: 4240 if pid in (4241, 4242, 4243) else None,
    )
    monkeypatch.setattr(
        browser,
        "_launched_by_session",
        lambda pid: "h_review" if pid == 4240 else None,
    )

    def _inodes(port):
        if port == 40141:
            return {7: {"::1"}}
        if port == 9333:
            return {8: {"::1"}}
        if port == 9444:
            return {9: {"::1"}}
        return {}

    def _connect_only(want):
        out = {}
        if 7 in want:
            out[4242] = {7}
            out[4243] = {7}
        if 8 in want:
            out[4240] = {8}
            out[4241] = {8}
        return out

    monkeypatch.setattr(browser, "_loopback_listen_inodes_for_port", _inodes)
    monkeypatch.setattr(browser, "_pids_holding_socket_inodes", _connect_only)
    monkeypatch.setattr(
        browser,
        "_cdp_port_reachable",
        lambda port, hosts: port in (9333, 40141) and "::1" in hosts,
    )
    monkeypatch.setattr(
        browser, "_configured_cdp_override_url",
        lambda: "http://[::1]:40141",
    )
    _reset_dock_port_memory_for_tests()
    (tmp_path / "DevToolsActivePort").write_text(
        "40141\n/devtools/browser/abc\n", encoding="utf-8",
    )
    assert browser.last_known_dock_cdp_port() is None
    assert _last_dock_cdp_port.get(hermes_home_key()) is None
    assert browser.unique_lock_chrome_hidden_by_leftover_file(
        40141, str(tmp_path),
    ) == 9333
    assert browser.lock_listed_persist_port() == 9333
    assert browser._this_jar_chromium_pid(str(tmp_path)) == 4240
    assert browser.shared_chromium_owner_session() == "h_review"
    assert browser.file_named_dock_listen_port() == 40141
    assert browser.running_instance_cdp_port(str(tmp_path)) == 9333
    assert browser.last_known_dock_cdp_port() == 9333
    assert _last_dock_cdp_port.get(hermes_home_key()) == 9333
    assert _remembered_dock_attach_port() == 9333
    assert browser.persist_live_dock_cdp_port() == 9333
    assert (tmp_path / "dock-cdp-port").read_text(encoding="utf-8") == "9333"
    assert _cdp_url_is_bot_desktop_browser("http://[::1]:40141") is True
    assert _cdp_url_is_bot_desktop_browser("http://[::1]:9333") is True
    assert _cdp_url_is_bot_desktop_browser("http://127.0.0.1:40141") is False
    assert _cdp_url_is_bot_desktop_browser("9222") is False
    assert _remembered_dock_attach_port() == 9333

    def _inherited_plus_zygote(want):
        out = {}
        if 7 in want:
            out[4242] = {7}
            out[4243] = {7}
        if 8 in want:
            out[4240] = {8}
            out[4241] = {8}
            out[4242] = {8}
            out[4243] = {8}
        return out

    monkeypatch.setattr(browser, "_pids_holding_socket_inodes", _inherited_plus_zygote)
    monkeypatch.setattr(
        browser, "_loopback_listen_ports_for_pid",
        lambda pid: {9333, 40141} if pid in leftover_helpers else (
            {9333} if pid in chrome_family else set()
        ),
    )
    monkeypatch.setattr(
        browser, "_loopback_listen_targets_for_pid",
        lambda pid: {("::1", 9333), ("::1", 40141)} if pid in leftover_helpers else (
            {("::1", 9333)} if pid in chrome_family else set()
        ),
    )
    _reset_dock_port_memory_for_tests()
    (tmp_path / "DevToolsActivePort").write_text(
        "40141\n/devtools/browser/abc\n", encoding="utf-8",
    )
    assert browser.unique_lock_chrome_hidden_by_leftover_file(
        40141, str(tmp_path),
    ) == 9333
    assert browser.lock_listed_persist_port() == 9333
    assert browser._this_jar_chromium_pid(str(tmp_path)) == 4240
    assert browser.running_instance_cdp_port(str(tmp_path)) == 9333
    assert _remembered_dock_attach_port() == 9333

    monkeypatch.setattr(browser, "_lock_pid", lambda d: 4240)
    monkeypatch.setattr(browser, "_pids_holding_socket_inodes", _connect_only)
    monkeypatch.setattr(
        browser, "_loopback_listen_ports_for_pid",
        lambda pid: {9333} if pid in chrome_family else (
            {40141} if pid in leftover_helpers else set()
        ),
    )
    monkeypatch.setattr(
        browser, "_loopback_listen_targets_for_pid",
        lambda pid: {("::1", 9333)} if pid in chrome_family else (
            {("::1", 40141)} if pid in leftover_helpers else set()
        ),
    )
    _reset_dock_port_memory_for_tests()
    (tmp_path / "DevToolsActivePort").write_text(
        "40141\n/devtools/browser/abc\n", encoding="utf-8",
    )
    assert browser.unique_lock_chrome_hidden_by_leftover_file(
        40141, str(tmp_path),
    ) == 9333
    assert browser.lock_listed_persist_port() == 9333
    assert browser._this_jar_chromium_pid(str(tmp_path)) == 4240
    assert browser.running_instance_cdp_port(str(tmp_path)) == 9333
    assert _remembered_dock_attach_port() == 9333

    def _leftover_only(want):
        out = {}
        if 7 in want:
            out[4240] = {7}
            out[4242] = {7}
            out[4243] = {7}
        return out

    monkeypatch.setattr(browser, "_lock_pid", lambda d: None)
    monkeypatch.setattr(browser, "_pids_holding_socket_inodes", _leftover_only)
    monkeypatch.setattr(
        browser, "_loopback_listen_ports_for_pid",
        lambda pid: {40141} if pid in (4240, 4242, 4243) else set(),
    )
    monkeypatch.setattr(
        browser, "_loopback_listen_targets_for_pid",
        lambda pid: {("::1", 40141)} if pid in (4240, 4242, 4243) else set(),
    )
    monkeypatch.setattr(
        browser,
        "_cdp_port_reachable",
        lambda port, hosts: port == 40141 and "::1" in hosts,
    )
    _reset_dock_port_memory_for_tests()
    (tmp_path / "DevToolsActivePort").write_text(
        "40141\n/devtools/browser/abc\n", encoding="utf-8",
    )
    assert browser.unique_lock_chrome_hidden_by_leftover_file(
        40141, str(tmp_path),
    ) is None
    assert browser.lock_listed_persist_port() is None
    assert browser._this_jar_chromium_pid(str(tmp_path)) is None
    assert browser.running_instance_cdp_port(str(tmp_path)) == 40141
    assert _remembered_dock_attach_port() == 40141

    def _unique_unique(want):
        out = {}
        if 7 in want:
            out[4242] = {7}
        if 8 in want:
            out[4240] = {8}
        return out

    monkeypatch.setattr(browser, "_pids_holding_socket_inodes", _unique_unique)
    monkeypatch.setattr(
        browser, "_loopback_listen_ports_for_pid",
        lambda pid: {9333, 40141} if pid == 4240 else (
            {40141} if pid == 4242 else set()
        ),
    )
    monkeypatch.setattr(
        browser, "_loopback_listen_targets_for_pid",
        lambda pid: {("::1", 9333), ("::1", 40141)} if pid == 4240 else (
            {("::1", 40141)} if pid == 4242 else set()
        ),
    )
    monkeypatch.setattr(
        browser,
        "_cdp_port_reachable",
        lambda port, hosts: port in (9333, 40141) and "::1" in hosts,
    )
    _reset_dock_port_memory_for_tests()
    (tmp_path / "DevToolsActivePort").write_text(
        "40141\n/devtools/browser/abc\n", encoding="utf-8",
    )
    assert browser.unique_lock_chrome_hidden_by_leftover_file(
        40141, str(tmp_path),
    ) is None
    assert browser.lock_listed_persist_port() is None
    assert browser.running_instance_cdp_port(str(tmp_path)) == 40141
    assert _remembered_dock_attach_port() == 40141

    def _several_shared(want):
        out = {}
        if 7 in want:
            out[4242] = {7}
            out[4243] = {7}
        if 8 in want:
            out[4240] = {8}
            out[4241] = {8}
            out[4242] = {8}
            out[4243] = {8}
        if 9 in want:
            out[4240] = {9}
            out[4241] = {9}
            out[4242] = {9}
            out[4243] = {9}
        return out

    monkeypatch.setattr(browser, "_pids_holding_socket_inodes", _several_shared)
    monkeypatch.setattr(
        browser, "_loopback_listen_ports_for_pid",
        lambda pid: {9333, 9444, 40141} if pid in leftover_helpers else (
            {9333, 9444} if pid in chrome_family else set()
        ),
    )
    monkeypatch.setattr(
        browser, "_loopback_listen_targets_for_pid",
        lambda pid: {("::1", 9333), ("::1", 9444), ("::1", 40141)} if pid in leftover_helpers else (
            {("::1", 9333), ("::1", 9444)} if pid in chrome_family else set()
        ),
    )
    monkeypatch.setattr(
        browser,
        "_cdp_port_reachable",
        lambda port, hosts: port in (9333, 40141, 9444) and "::1" in hosts,
    )
    _reset_dock_port_memory_for_tests()
    (tmp_path / "DevToolsActivePort").write_text(
        "40141\n/devtools/browser/abc\n", encoding="utf-8",
    )
    assert browser.unique_lock_chrome_hidden_by_leftover_file(
        40141, str(tmp_path),
    ) is None
    assert browser.lock_listed_persist_port() is None
    assert browser._this_jar_chromium_pid(str(tmp_path)) is None
    assert browser.running_instance_cdp_port(str(tmp_path)) == 40141
    assert _remembered_dock_attach_port() == 40141


def test_leftover_connect_only_chrome_zygote_grandchild_does_not_stamp_helpers(
    tmp_path, monkeypatch,
):
    """Finding 190: leftover is connect-only; zygote grandchild holds CDP.

    Finding 189 allowed chrome plus this-jar children of chrome.
    Chromium zygote forks GPU / utility / renderer children that
    inherit chrome's listen; their PPID is zygote, not chrome.
    n==1 and child-only family both miss, so first persist stamped
    leftover helpers. Overlay can unlink SingletonLock, so
    skip-kill also missed chrome. Chrome plus this-jar descendants
    of chrome is still chrome. An unrelated this-jar holder is not.
    Leftover-only several stays unknown. Unique leftover file
    stays 161.
    """
    from hermes_constants import hermes_home_key
    from tools.browser_tool_session import (
        _cdp_url_is_bot_desktop_browser,
        _last_dock_cdp_port,
        _remembered_dock_attach_port,
        _reset_dock_port_memory_for_tests,
    )

    leftover_helpers = {4242, 4243}
    chrome_family = {4240, 4241, 4245}

    monkeypatch.setattr(runtime, "state_dir", lambda: tmp_path)
    monkeypatch.setattr(browser, "profile_dir", lambda: tmp_path)
    monkeypatch.setattr(browser, "_lock_pid", lambda d: None)
    monkeypatch.setattr(
        browser,
        "_chromium_cmdline_tokens",
        lambda pid: ["chrome", f"--user-data-dir={tmp_path}"],
    )
    monkeypatch.setattr(
        browser, "_loopback_listen_ports_for_pid",
        lambda pid: {40141} if pid in leftover_helpers else (
            {9333} if pid in chrome_family else set()
        ),
    )
    monkeypatch.setattr(
        browser, "_loopback_listen_targets_for_pid",
        lambda pid: {("::1", 40141)} if pid in leftover_helpers else (
            {("::1", 9333)} if pid in chrome_family else set()
        ),
    )
    monkeypatch.setattr(browser, "_recover_cdp_port_from_singleton", lambda *a, **k: None)
    monkeypatch.setattr(
        browser,
        "_proc_ppid",
        lambda pid: {4241: 4240, 4245: 4241, 4242: 4240, 4243: 4240}.get(pid),
    )
    monkeypatch.setattr(
        browser,
        "_launched_by_session",
        lambda pid: "h_review" if pid == 4240 else None,
    )

    def _inodes(port):
        if port == 40141:
            return {7: {"::1"}}
        if port == 9333:
            return {8: {"::1"}}
        return {}

    def _connect_only(want):
        out = {}
        if 7 in want:
            out[4242] = {7}
            out[4243] = {7}
        if 8 in want:
            out[4240] = {8}
            out[4241] = {8}
            out[4245] = {8}
        return out

    monkeypatch.setattr(browser, "_loopback_listen_inodes_for_port", _inodes)
    monkeypatch.setattr(browser, "_pids_holding_socket_inodes", _connect_only)
    monkeypatch.setattr(
        browser,
        "_cdp_port_reachable",
        lambda port, hosts: port in (9333, 40141) and "::1" in hosts,
    )
    monkeypatch.setattr(
        browser, "_configured_cdp_override_url",
        lambda: "http://[::1]:40141",
    )
    _reset_dock_port_memory_for_tests()
    (tmp_path / "DevToolsActivePort").write_text(
        "40141\n/devtools/browser/abc\n", encoding="utf-8",
    )
    assert browser.unique_lock_chrome_hidden_by_leftover_file(
        40141, str(tmp_path),
    ) == 9333
    assert browser.lock_listed_persist_port() == 9333
    assert browser._this_jar_chromium_pid(str(tmp_path)) == 4240
    assert browser.shared_chromium_owner_session() == "h_review"
    assert browser.running_instance_cdp_port(str(tmp_path)) == 9333
    assert browser.last_known_dock_cdp_port() == 9333
    assert _last_dock_cdp_port.get(hermes_home_key()) == 9333
    assert _remembered_dock_attach_port() == 9333
    assert browser.persist_live_dock_cdp_port() == 9333
    assert (tmp_path / "dock-cdp-port").read_text(encoding="utf-8") == "9333"
    assert _cdp_url_is_bot_desktop_browser("http://[::1]:40141") is True
    assert _cdp_url_is_bot_desktop_browser("http://[::1]:9333") is True
    assert _cdp_url_is_bot_desktop_browser("9222") is False
    assert _remembered_dock_attach_port() == 9333

    monkeypatch.setattr(browser, "_lock_pid", lambda d: 4240)
    _reset_dock_port_memory_for_tests()
    (tmp_path / "DevToolsActivePort").write_text(
        "40141\n/devtools/browser/abc\n", encoding="utf-8",
    )
    assert browser.unique_lock_chrome_hidden_by_leftover_file(
        40141, str(tmp_path),
    ) == 9333
    assert browser.lock_listed_persist_port() == 9333
    assert browser._this_jar_chromium_pid(str(tmp_path)) == 4240
    assert browser.running_instance_cdp_port(str(tmp_path)) == 9333
    assert _remembered_dock_attach_port() == 9333

    def _unrelated(want):
        out = {}
        if 7 in want:
            out[4242] = {7}
            out[4243] = {7}
        if 8 in want:
            out[4240] = {8}
            out[4241] = {8}
            out[4299] = {8}
        return out

    monkeypatch.setattr(browser, "_lock_pid", lambda d: None)
    monkeypatch.setattr(browser, "_pids_holding_socket_inodes", _unrelated)
    monkeypatch.setattr(
        browser, "_loopback_listen_ports_for_pid",
        lambda pid: {40141} if pid in leftover_helpers else (
            {9333} if pid in (4240, 4241, 4299) else set()
        ),
    )
    monkeypatch.setattr(
        browser,
        "_proc_ppid",
        lambda pid: {4241: 4240, 4299: 50, 4242: 4240, 4243: 4240}.get(pid),
    )
    _reset_dock_port_memory_for_tests()
    (tmp_path / "DevToolsActivePort").write_text(
        "40141\n/devtools/browser/abc\n", encoding="utf-8",
    )
    assert browser.unique_lock_chrome_hidden_by_leftover_file(
        40141, str(tmp_path),
    ) is None
    assert browser.lock_listed_persist_port() is None
    assert browser._this_jar_chromium_pid(str(tmp_path)) is None
    assert browser.running_instance_cdp_port(str(tmp_path)) == 40141
    assert _remembered_dock_attach_port() == 40141


def test_leftover_daemon_parent_sibling_chrome_does_not_stamp_helpers(
    tmp_path, monkeypatch,
):
    """Finding 191: leftover helpers' parent is leftover daemon.

    Findings 186 / 189 / 190 treat leftover's unique this-jar parent
    as chrome. Real leftover ``fill`` / CRI clients are children of
    leftover daemon; chrome is their sibling. Lock-gone then used
    leftover daemon as chrome_pid, so leftover-shared equality and
    chrome-in-holders both missed. First persist stamped leftover
    helpers; skip-kill missed chrome. Unique browser-process root
    of extra holders is chrome. Connect-only leftover does not
    advertise chrome's listen — scan leftover daemon's this-jar
    children (siblings), not every this-jar pid. Leftover-only
    several stays unknown. A zygote root (chrome dropped the
    inode) is not chrome.
    """
    from hermes_constants import hermes_home_key
    from tools.browser_tool_session import (
        _cdp_url_is_bot_desktop_browser,
        _last_dock_cdp_port,
        _remembered_dock_attach_port,
        _reset_dock_port_memory_for_tests,
    )

    leftover_helpers = {4242, 4243}
    chrome_family = {4240, 4241, 4245}

    monkeypatch.setattr(runtime, "state_dir", lambda: tmp_path)
    monkeypatch.setattr(browser, "profile_dir", lambda: tmp_path)
    monkeypatch.setattr(browser, "_lock_pid", lambda d: None)
    monkeypatch.setattr(
        browser,
        "_chromium_cmdline_tokens",
        lambda pid: (
            ["chrome", "--type=zygote", f"--user-data-dir={tmp_path}"]
            if pid == 4241 else
            ["chrome", "--type=utility", f"--user-data-dir={tmp_path}"]
            if pid == 4245 else
            ["agent-browser", "daemon", f"--user-data-dir={tmp_path}"]
            if pid == 4300 else
            ["chrome", f"--user-data-dir={tmp_path}"]
        ),
    )
    monkeypatch.setattr(
        browser,
        "_proc_ppid",
        lambda pid: {
            4240: 4300, 4241: 4240, 4245: 4241, 4242: 4300, 4243: 4300,
        }.get(pid),
    )
    monkeypatch.setattr(
        browser,
        "_launched_by_session",
        lambda pid: "h_review" if pid == 4240 else None,
    )
    monkeypatch.setattr(browser, "_recover_cdp_port_from_singleton", lambda *a, **k: None)
    monkeypatch.setattr(
        browser, "_this_jar_children",
        lambda parent, user_data_dir: (
            {4240, 4242, 4243} if parent == 4300 else set()
        ),
    )

    def _inodes(port):
        if port == 40141:
            return {7: {"::1"}}
        if port == 9333:
            return {8: {"::1"}}
        return {}

    def _inherited(want):
        out = {}
        if 7 in want:
            out[4242] = {7}
            out[4243] = {7}
        if 8 in want:
            out[4240] = {8}
            out[4241] = {8}
            out[4245] = {8}
            out[4242] = {8}
            out[4243] = {8}
        return out

    monkeypatch.setattr(browser, "_loopback_listen_inodes_for_port", _inodes)
    monkeypatch.setattr(browser, "_pids_holding_socket_inodes", _inherited)
    monkeypatch.setattr(
        browser, "_loopback_listen_ports_for_pid",
        lambda pid: {9333, 40141} if pid in leftover_helpers else (
            {9333} if pid in chrome_family else (
                {40141} if pid == 4300 else set()
            )
        ),
    )
    monkeypatch.setattr(
        browser, "_loopback_listen_targets_for_pid",
        lambda pid: {("::1", 9333), ("::1", 40141)} if pid in leftover_helpers else (
            {("::1", 9333)} if pid in chrome_family else (
                {("::1", 40141)} if pid == 4300 else set()
            )
        ),
    )
    monkeypatch.setattr(
        browser,
        "_cdp_port_reachable",
        lambda port, hosts: port in (9333, 40141) and "::1" in hosts,
    )
    monkeypatch.setattr(
        browser, "_configured_cdp_override_url",
        lambda: "http://[::1]:40141",
    )
    _reset_dock_port_memory_for_tests()
    (tmp_path / "DevToolsActivePort").write_text(
        "40141\n/devtools/browser/abc\n", encoding="utf-8",
    )
    assert browser.unique_lock_chrome_hidden_by_leftover_file(
        40141, str(tmp_path),
    ) == 9333
    assert browser.lock_listed_persist_port() == 9333
    assert browser._this_jar_chromium_pid(str(tmp_path)) == 4240
    assert browser.shared_chromium_owner_session() == "h_review"
    assert browser.running_instance_cdp_port(str(tmp_path)) == 9333
    assert browser.last_known_dock_cdp_port() == 9333
    assert _last_dock_cdp_port.get(hermes_home_key()) == 9333
    assert _remembered_dock_attach_port() == 9333
    assert browser.persist_live_dock_cdp_port() == 9333
    assert (tmp_path / "dock-cdp-port").read_text(encoding="utf-8") == "9333"
    assert _cdp_url_is_bot_desktop_browser("http://[::1]:40141") is True
    assert _cdp_url_is_bot_desktop_browser("http://[::1]:9333") is True
    assert _cdp_url_is_bot_desktop_browser("9222") is False

    def _connect_only(want):
        out = {}
        if 7 in want:
            out[4242] = {7}
            out[4243] = {7}
        if 8 in want:
            out[4240] = {8}
            out[4241] = {8}
            out[4245] = {8}
        return out

    monkeypatch.setattr(browser, "_pids_holding_socket_inodes", _connect_only)
    monkeypatch.setattr(
        browser, "_loopback_listen_ports_for_pid",
        lambda pid: {40141} if pid in leftover_helpers or pid == 4300 else (
            {9333} if pid in chrome_family else set()
        ),
    )
    monkeypatch.setattr(
        browser, "_loopback_listen_targets_for_pid",
        lambda pid: {("::1", 40141)} if pid in leftover_helpers or pid == 4300 else (
            {("::1", 9333)} if pid in chrome_family else set()
        ),
    )
    _reset_dock_port_memory_for_tests()
    (tmp_path / "DevToolsActivePort").write_text(
        "40141\n/devtools/browser/abc\n", encoding="utf-8",
    )
    assert browser.unique_lock_chrome_hidden_by_leftover_file(
        40141, str(tmp_path),
    ) == 9333
    assert browser.lock_listed_persist_port() == 9333
    assert browser._this_jar_chromium_pid(str(tmp_path)) == 4240
    assert browser.running_instance_cdp_port(str(tmp_path)) == 9333
    assert _remembered_dock_attach_port() == 9333

    def _daemon_inherited(want):
        out = {}
        if 7 in want:
            out[4242] = {7}
            out[4243] = {7}
        if 8 in want:
            out[4240] = {8}
            out[4241] = {8}
            out[4245] = {8}
            out[4300] = {8}
        return out

    monkeypatch.setattr(browser, "_pids_holding_socket_inodes", _daemon_inherited)
    monkeypatch.setattr(
        browser, "_loopback_listen_ports_for_pid",
        lambda pid: {40141} if pid in leftover_helpers else (
            {9333} if pid in chrome_family or pid == 4300 else set()
        ),
    )
    monkeypatch.setattr(
        browser, "_loopback_listen_targets_for_pid",
        lambda pid: {("::1", 40141)} if pid in leftover_helpers else (
            {("::1", 9333)} if pid in chrome_family or pid == 4300 else set()
        ),
    )
    _reset_dock_port_memory_for_tests()
    (tmp_path / "DevToolsActivePort").write_text(
        "40141\n/devtools/browser/abc\n", encoding="utf-8",
    )
    assert browser.unique_lock_chrome_hidden_by_leftover_file(
        40141, str(tmp_path),
    ) == 9333
    assert browser.lock_listed_persist_port() == 9333
    assert browser._this_jar_chromium_pid(str(tmp_path)) == 4240
    assert browser.shared_chromium_owner_session() == "h_review"
    assert browser.running_instance_cdp_port(str(tmp_path)) == 9333
    assert _remembered_dock_attach_port() == 9333

    def _leftover_only(want):
        out = {}
        if 7 in want:
            out[4240] = {7}
            out[4242] = {7}
            out[4243] = {7}
        return out

    monkeypatch.setattr(browser, "_pids_holding_socket_inodes", _leftover_only)
    monkeypatch.setattr(
        browser, "_loopback_listen_ports_for_pid",
        lambda pid: {40141} if pid in (4240, 4242, 4243, 4300) else set(),
    )
    monkeypatch.setattr(
        browser,
        "_cdp_port_reachable",
        lambda port, hosts: port == 40141 and "::1" in hosts,
    )
    _reset_dock_port_memory_for_tests()
    (tmp_path / "DevToolsActivePort").write_text(
        "40141\n/devtools/browser/abc\n", encoding="utf-8",
    )
    assert browser.unique_lock_chrome_hidden_by_leftover_file(
        40141, str(tmp_path),
    ) is None
    assert browser.lock_listed_persist_port() is None
    assert browser._this_jar_chromium_pid(str(tmp_path)) is None
    assert browser.running_instance_cdp_port(str(tmp_path)) == 40141
    assert _remembered_dock_attach_port() == 40141

    def _dropped(want):
        out = {}
        if 7 in want:
            out[4242] = {7}
            out[4243] = {7}
        if 8 in want:
            out[4241] = {8}
            out[4245] = {8}
        return out

    monkeypatch.setattr(browser, "_pids_holding_socket_inodes", _dropped)
    monkeypatch.setattr(
        browser,
        "_proc_ppid",
        lambda pid: {4241: 4240, 4245: 4241, 4242: 4240, 4243: 4240}.get(pid),
    )
    monkeypatch.setattr(
        browser, "_loopback_listen_ports_for_pid",
        lambda pid: {9333, 40141} if pid in leftover_helpers else (
            {9333} if pid in (4241, 4245) else (
                {40141} if pid == 4240 else set()
            )
        ),
    )
    monkeypatch.setattr(
        browser,
        "_cdp_port_reachable",
        lambda port, hosts: port in (9333, 40141) and "::1" in hosts,
    )
    monkeypatch.setattr(
        browser, "_this_jar_children",
        lambda parent, user_data_dir: (
            {4241, 4242, 4243} if parent == 4240 else set()
        ),
    )
    _reset_dock_port_memory_for_tests()
    (tmp_path / "DevToolsActivePort").write_text(
        "40141\n/devtools/browser/abc\n", encoding="utf-8",
    )
    assert browser.unique_lock_chrome_hidden_by_leftover_file(
        40141, str(tmp_path),
    ) is None
    assert browser.lock_listed_persist_port() is None
    assert browser._this_jar_chromium_pid(str(tmp_path)) is None
    assert browser.running_instance_cdp_port(str(tmp_path)) == 40141
    assert _remembered_dock_attach_port() == 40141


def test_leftover_memory_hides_sibling_chrome_does_not_stamp_helpers(
    tmp_path, monkeypatch,
):
    """Finding 192: leftover memory hid sibling chrome.

    Finding 179 returns lock-gone memory that this jar still
    inode-listens on. A pre-191 leftover stamp left
    ``_last_dock_cdp_port`` / ``dock-cdp-port`` on leftover
    ``DevToolsActivePort`` helpers. Those helpers still have
    hosts, so lock_listed returned leftover and persist_live /
    attach followed it even after 191 named sibling chrome.
    Hidden chrome that differs from leftover memory is chrome.
    Leftover-only several with leftover memory stays 86.
    Chrome memory still wins when hidden agrees.
    """
    from hermes_constants import hermes_home_key
    from tools.browser_tool_session import (
        _last_dock_cdp_port,
        _remembered_dock_attach_port,
        _reset_dock_port_memory_for_tests,
    )

    leftover_helpers = {4242, 4243}
    chrome_family = {4240, 4241, 4245}

    monkeypatch.setattr(runtime, "state_dir", lambda: tmp_path)
    monkeypatch.setattr(browser, "profile_dir", lambda: tmp_path)
    monkeypatch.setattr(browser, "_lock_pid", lambda d: None)
    monkeypatch.setattr(
        browser,
        "_chromium_cmdline_tokens",
        lambda pid: (
            ["chrome", "--type=zygote", f"--user-data-dir={tmp_path}"]
            if pid == 4241 else
            ["chrome", "--type=utility", f"--user-data-dir={tmp_path}"]
            if pid == 4245 else
            ["agent-browser", "daemon", f"--user-data-dir={tmp_path}"]
            if pid == 4300 else
            ["chrome", f"--user-data-dir={tmp_path}"]
        ),
    )
    monkeypatch.setattr(
        browser,
        "_proc_ppid",
        lambda pid: {
            4240: 4300, 4241: 4240, 4245: 4241, 4242: 4300, 4243: 4300,
        }.get(pid),
    )
    monkeypatch.setattr(
        browser,
        "_launched_by_session",
        lambda pid: "h_review" if pid == 4240 else None,
    )
    monkeypatch.setattr(browser, "_recover_cdp_port_from_singleton", lambda *a, **k: None)
    monkeypatch.setattr(
        browser, "_this_jar_children",
        lambda parent, user_data_dir: (
            {4240, 4242, 4243} if parent == 4300 else set()
        ),
    )

    def _inodes(port):
        if port == 40141:
            return {7: {"::1"}}
        if port == 9333:
            return {8: {"::1"}}
        return {}

    def _connect_only(want):
        out = {}
        if 7 in want:
            out[4242] = {7}
            out[4243] = {7}
        if 8 in want:
            out[4240] = {8}
            out[4241] = {8}
            out[4245] = {8}
        return out

    monkeypatch.setattr(browser, "_loopback_listen_inodes_for_port", _inodes)
    monkeypatch.setattr(browser, "_pids_holding_socket_inodes", _connect_only)
    monkeypatch.setattr(
        browser, "_loopback_listen_ports_for_pid",
        lambda pid: {40141} if pid in leftover_helpers or pid == 4300 else (
            {9333} if pid in chrome_family else set()
        ),
    )
    monkeypatch.setattr(
        browser, "_loopback_listen_targets_for_pid",
        lambda pid: {("::1", 40141)} if pid in leftover_helpers or pid == 4300 else (
            {("::1", 9333)} if pid in chrome_family else set()
        ),
    )
    monkeypatch.setattr(
        browser,
        "_cdp_port_reachable",
        lambda port, hosts: port in (9333, 40141) and "::1" in hosts,
    )
    monkeypatch.setattr(browser, "_configured_cdp_override_url", lambda: "")
    (tmp_path / "DevToolsActivePort").write_text(
        "40141\n/devtools/browser/abc\n", encoding="utf-8",
    )

    _reset_dock_port_memory_for_tests()
    _last_dock_cdp_port[hermes_home_key()] = 40141
    (tmp_path / "dock-cdp-port").write_text("40141\n", encoding="utf-8")
    assert browser.unique_lock_chrome_hidden_by_leftover_file(
        40141, str(tmp_path),
    ) == 9333
    assert browser.lock_listed_persist_port() == 9333
    assert browser._this_jar_chromium_pid(str(tmp_path)) == 4240
    assert browser.shared_chromium_owner_session() == "h_review"
    assert browser.running_instance_cdp_port(str(tmp_path)) == 9333
    assert _remembered_dock_attach_port() == 9333
    assert browser.persist_live_dock_cdp_port() == 9333
    assert (tmp_path / "dock-cdp-port").read_text(encoding="utf-8") == "9333"
    assert _last_dock_cdp_port.get(hermes_home_key()) == 9333

    _reset_dock_port_memory_for_tests()
    _last_dock_cdp_port[hermes_home_key()] = 40141
    assert browser.lock_listed_persist_port() == 9333
    assert browser.running_instance_cdp_port(str(tmp_path)) == 9333
    assert _remembered_dock_attach_port() == 9333

    _reset_dock_port_memory_for_tests()
    _last_dock_cdp_port[hermes_home_key()] = 9333
    (tmp_path / "dock-cdp-port").write_text("9333\n", encoding="utf-8")
    assert browser.lock_listed_persist_port() == 9333
    assert browser.running_instance_cdp_port(str(tmp_path)) == 9333
    assert _remembered_dock_attach_port() == 9333

    def _leftover_only(want):
        out = {}
        if 7 in want:
            out[4240] = {7}
            out[4242] = {7}
            out[4243] = {7}
        return out

    monkeypatch.setattr(browser, "_pids_holding_socket_inodes", _leftover_only)
    monkeypatch.setattr(
        browser, "_loopback_listen_ports_for_pid",
        lambda pid: {40141} if pid in (4240, 4242, 4243, 4300) else set(),
    )
    monkeypatch.setattr(
        browser,
        "_cdp_port_reachable",
        lambda port, hosts: port == 40141 and "::1" in hosts,
    )
    _reset_dock_port_memory_for_tests()
    _last_dock_cdp_port[hermes_home_key()] = 40141
    (tmp_path / "dock-cdp-port").write_text("40141\n", encoding="utf-8")
    assert browser.unique_lock_chrome_hidden_by_leftover_file(
        40141, str(tmp_path),
    ) is None
    assert browser.lock_listed_persist_port() == 40141
    assert browser._this_jar_chromium_pid(str(tmp_path)) is None
    assert browser.running_instance_cdp_port(str(tmp_path)) == 40141
    assert _remembered_dock_attach_port() == 40141


def test_leftover_persist_unique_hides_sibling_chrome_does_not_stamp_helpers(
    tmp_path, monkeypatch,
):
    """Finding 193: leftover persist unique hid sibling chrome.

    Finding 181 treats persist unique + leftover DevTools several as
    chrome after a process restart (empty memory). Leftover identity
    can stamp a different unique leftover listen (CRI / ``--cdp``)
    into that file. Unique leftover persist then hid sibling chrome
    and attach followed leftover DevTools after 192 flipped
    lock-listed. Hidden chrome that differs from leftover persist
    is chrome. Leftover-only unique persist + leftover several
    stays 86. Chrome persist unique still wins when hidden agrees.
    """
    from hermes_constants import hermes_home_key
    from tools.browser_tool_session import (
        _last_dock_cdp_port,
        _remembered_dock_attach_port,
        _reset_dock_port_memory_for_tests,
    )

    leftover_helpers = {4242, 4243}
    chrome_family = {4240, 4241, 4245}
    leftover_cri = 4244

    monkeypatch.setattr(runtime, "state_dir", lambda: tmp_path)
    monkeypatch.setattr(browser, "profile_dir", lambda: tmp_path)
    monkeypatch.setattr(browser, "_lock_pid", lambda d: None)
    monkeypatch.setattr(
        browser,
        "_chromium_cmdline_tokens",
        lambda pid: (
            ["chrome", "--type=zygote", f"--user-data-dir={tmp_path}"]
            if pid == 4241 else
            ["chrome", "--type=utility", f"--user-data-dir={tmp_path}"]
            if pid == 4245 else
            ["agent-browser", "daemon", f"--user-data-dir={tmp_path}"]
            if pid == 4300 else
            ["agent-browser", "cri", f"--user-data-dir={tmp_path}"]
            if pid == leftover_cri else
            ["chrome", f"--user-data-dir={tmp_path}"]
        ),
    )
    monkeypatch.setattr(
        browser,
        "_proc_ppid",
        lambda pid: {
            4240: 4300, 4241: 4240, 4245: 4241, 4242: 4300, 4243: 4300,
            4244: 4300,
        }.get(pid),
    )
    monkeypatch.setattr(
        browser,
        "_launched_by_session",
        lambda pid: "h_review" if pid == 4240 else None,
    )
    monkeypatch.setattr(browser, "_recover_cdp_port_from_singleton", lambda *a, **k: None)
    monkeypatch.setattr(
        browser, "_this_jar_children",
        lambda parent, user_data_dir: (
            {4240, 4242, 4243, 4244} if parent == 4300 else set()
        ),
    )

    def _inodes(port):
        if port == 40141:
            return {7: {"::1"}}
        if port == 9333:
            return {8: {"::1"}}
        if port == 18888:
            return {9: {"::1"}}
        return {}

    def _holders(want):
        out = {}
        if 7 in want:
            out[4242] = {7}
            out[4243] = {7}
        if 8 in want:
            out[4240] = {8}
            out[4241] = {8}
            out[4245] = {8}
        if 9 in want:
            out[4244] = {9}
        return out

    monkeypatch.setattr(browser, "_loopback_listen_inodes_for_port", _inodes)
    monkeypatch.setattr(browser, "_pids_holding_socket_inodes", _holders)
    monkeypatch.setattr(
        browser, "_loopback_listen_ports_for_pid",
        lambda pid: {40141} if pid in leftover_helpers or pid == 4300 else (
            {18888} if pid == leftover_cri else (
                {9333} if pid in chrome_family else set()
            )
        ),
    )
    monkeypatch.setattr(
        browser, "_loopback_listen_targets_for_pid",
        lambda pid: {("::1", 40141)} if pid in leftover_helpers or pid == 4300 else (
            {("::1", 18888)} if pid == leftover_cri else (
                {("::1", 9333)} if pid in chrome_family else set()
            )
        ),
    )
    monkeypatch.setattr(
        browser,
        "_cdp_port_reachable",
        lambda port, hosts: port in (9333, 40141, 18888) and "::1" in hosts,
    )
    monkeypatch.setattr(browser, "_configured_cdp_override_url", lambda: "")
    (tmp_path / "DevToolsActivePort").write_text(
        "40141\n/devtools/browser/abc\n", encoding="utf-8",
    )

    _reset_dock_port_memory_for_tests()
    (tmp_path / "dock-cdp-port").write_text("18888\n", encoding="utf-8")
    assert browser.leftover_helpers_hide_persist_chrome(
        18888, 40141, str(tmp_path),
    ) is True
    assert browser.unique_lock_chrome_hidden_by_leftover_file(
        40141, str(tmp_path),
    ) == 9333
    assert browser.lock_listed_persist_port() == 9333
    assert browser._this_jar_chromium_pid(str(tmp_path)) == 4240
    assert browser.shared_chromium_owner_session() == "h_review"
    assert _remembered_dock_attach_port() == 9333
    assert (tmp_path / "dock-cdp-port").read_text(encoding="utf-8").strip() == "18888"
    assert browser.running_instance_cdp_port(str(tmp_path)) == 9333
    assert _remembered_dock_attach_port() == 9333
    assert browser.persist_live_dock_cdp_port() == 9333
    assert (tmp_path / "dock-cdp-port").read_text(encoding="utf-8") == "9333"
    assert _last_dock_cdp_port.get(hermes_home_key()) == 9333

    _reset_dock_port_memory_for_tests()
    (tmp_path / "dock-cdp-port").write_text("9333\n", encoding="utf-8")
    assert browser.lock_listed_persist_port() == 9333
    assert browser.running_instance_cdp_port(str(tmp_path)) == 9333
    assert _remembered_dock_attach_port() == 9333

    def _leftover_only(want):
        out = {}
        if 7 in want:
            out[4242] = {7}
            out[4243] = {7}
        if 9 in want:
            out[4244] = {9}
        return out

    monkeypatch.setattr(browser, "_pids_holding_socket_inodes", _leftover_only)
    monkeypatch.setattr(
        browser, "_loopback_listen_ports_for_pid",
        lambda pid: {40141} if pid in leftover_helpers or pid == 4300 else (
            {18888} if pid == leftover_cri else set()
        ),
    )
    monkeypatch.setattr(
        browser,
        "_cdp_port_reachable",
        lambda port, hosts: port in (40141, 18888) and "::1" in hosts,
    )
    _reset_dock_port_memory_for_tests()
    (tmp_path / "dock-cdp-port").write_text("18888\n", encoding="utf-8")
    assert browser.unique_lock_chrome_hidden_by_leftover_file(
        40141, str(tmp_path),
    ) is None
    assert browser.lock_listed_persist_port() == 18888
    assert browser._this_jar_chromium_pid(str(tmp_path)) is None
    assert browser.running_instance_cdp_port(str(tmp_path)) == 18888
    assert _remembered_dock_attach_port() == 18888


def test_leftover_persist_unique_does_not_hide_attach_behind_devtools(
    tmp_path, monkeypatch,
):
    """Finding 194: leftover persist unique hid attach behind leftover DevTools.

    Finding 193 already prefers hidden chrome for lock-listed /
    running_instance. Agent attach after a live TCP miss (leftover
    holds chrome's CDP socket — finding 166) used remembered attach.
    Finding 172 then preferred leftover ``DevToolsActivePort`` because
    the persist file was leftover CRI, not named leftover helpers and
    not lock-listed chrome. Hidden chrome that equals lock-listed is
    chrome. Attach must not stamp persist. Leftover-only unique persist
    + leftover several stays 86.
    """
    from hermes_constants import hermes_home_key
    from tools.browser_tool_session import (
        _last_dock_cdp_port,
        _remembered_dock_attach_port,
        _reset_dock_port_memory_for_tests,
    )

    leftover_helpers = {4242, 4243}
    chrome_family = {4240, 4241, 4245}
    leftover_cri = 4244

    monkeypatch.setattr(runtime, "state_dir", lambda: tmp_path)
    monkeypatch.setattr(browser, "profile_dir", lambda: tmp_path)
    monkeypatch.setattr(browser, "_lock_pid", lambda d: None)
    monkeypatch.setattr(
        browser,
        "_chromium_cmdline_tokens",
        lambda pid: (
            ["chrome", "--type=zygote", f"--user-data-dir={tmp_path}"]
            if pid == 4241 else
            ["chrome", "--type=utility", f"--user-data-dir={tmp_path}"]
            if pid == 4245 else
            ["agent-browser", "daemon", f"--user-data-dir={tmp_path}"]
            if pid == 4300 else
            ["agent-browser", "cri", f"--user-data-dir={tmp_path}"]
            if pid == leftover_cri else
            ["chrome", f"--user-data-dir={tmp_path}"]
        ),
    )
    monkeypatch.setattr(
        browser,
        "_proc_ppid",
        lambda pid: {
            4240: 4300, 4241: 4240, 4245: 4241, 4242: 4300, 4243: 4300,
            4244: 4300,
        }.get(pid),
    )
    monkeypatch.setattr(
        browser,
        "_launched_by_session",
        lambda pid: "h_review" if pid == 4240 else None,
    )
    monkeypatch.setattr(browser, "_recover_cdp_port_from_singleton", lambda *a, **k: None)
    monkeypatch.setattr(
        browser, "_this_jar_children",
        lambda parent, user_data_dir: (
            {4240, 4242, 4243, 4244} if parent == 4300 else set()
        ),
    )

    def _inodes(port):
        if port == 40141:
            return {7: {"::1"}}
        if port == 9333:
            return {8: {"::1"}}
        if port == 18888:
            return {9: {"::1"}}
        return {}

    def _holders(want):
        out = {}
        if 7 in want:
            out[4242] = {7}
            out[4243] = {7}
        if 8 in want:
            out[4240] = {8}
            out[4241] = {8}
            out[4245] = {8}
        if 9 in want:
            out[4244] = {9}
        return out

    monkeypatch.setattr(browser, "_loopback_listen_inodes_for_port", _inodes)
    monkeypatch.setattr(browser, "_pids_holding_socket_inodes", _holders)
    monkeypatch.setattr(
        browser, "_loopback_listen_ports_for_pid",
        lambda pid: {40141} if pid in leftover_helpers or pid == 4300 else (
            {18888} if pid == leftover_cri else (
                {9333} if pid in chrome_family else set()
            )
        ),
    )
    monkeypatch.setattr(
        browser, "_loopback_listen_targets_for_pid",
        lambda pid: {("::1", 40141)} if pid in leftover_helpers or pid == 4300 else (
            {("::1", 18888)} if pid == leftover_cri else (
                {("::1", 9333)} if pid in chrome_family else set()
            )
        ),
    )
    monkeypatch.setattr(
        browser,
        "_cdp_port_reachable",
        lambda port, hosts: port in (40141, 18888) and "::1" in hosts,
    )
    monkeypatch.setattr(browser, "_configured_cdp_override_url", lambda: "")
    (tmp_path / "DevToolsActivePort").write_text(
        "40141\n/devtools/browser/abc\n", encoding="utf-8",
    )
    _reset_dock_port_memory_for_tests()
    (tmp_path / "dock-cdp-port").write_text("18888\n", encoding="utf-8")
    assert browser.lock_listed_persist_port() == 9333
    assert browser._this_jar_chromium_pid(str(tmp_path)) == 4240
    assert browser.shared_chromium_owner_session() == "h_review"
    assert browser.running_instance_cdp_port(str(tmp_path)) is None
    assert _remembered_dock_attach_port() == 9333
    assert (tmp_path / "dock-cdp-port").read_text(encoding="utf-8").strip() == "18888"
    assert _last_dock_cdp_port.get(hermes_home_key()) is None

    def _leftover_only(want):
        out = {}
        if 7 in want:
            out[4242] = {7}
            out[4243] = {7}
        if 9 in want:
            out[4244] = {9}
        return out

    monkeypatch.setattr(browser, "_pids_holding_socket_inodes", _leftover_only)
    monkeypatch.setattr(
        browser, "_loopback_listen_ports_for_pid",
        lambda pid: {40141} if pid in leftover_helpers or pid == 4300 else (
            {18888} if pid == leftover_cri else set()
        ),
    )
    _reset_dock_port_memory_for_tests()
    (tmp_path / "dock-cdp-port").write_text("18888\n", encoding="utf-8")
    assert browser.unique_lock_chrome_hidden_by_leftover_file(
        40141, str(tmp_path),
    ) is None
    assert browser.lock_listed_persist_port() == 18888
    assert _remembered_dock_attach_port() == 18888


def test_leftover_daemon_unique_cri_does_not_hide_sibling_chrome(
    tmp_path, monkeypatch,
):
    """Finding 195: leftover daemon unique CRI hid sibling chrome.

    Finding 193 prefers hidden chrome when leftover persist unique
    differs. Lock-gone n==1 then treated leftover daemon that
    inherited leftover CRI / ``--cdp`` as that hidden chrome and
    never reached sibling chrome via leftover daemon's children.
    Leftover daemon / CRI is not chrome. Leftover-only unique
    persist + leftover several stays 86. Attach must not stamp
    persist.
    """
    from hermes_constants import hermes_home_key
    from tools.browser_tool_session import (
        _last_dock_cdp_port,
        _remembered_dock_attach_port,
        _reset_dock_port_memory_for_tests,
    )

    leftover_helpers = {4242, 4243}
    chrome_family = {4240, 4241, 4245}
    leftover_cri = 4244

    monkeypatch.setattr(runtime, "state_dir", lambda: tmp_path)
    monkeypatch.setattr(browser, "profile_dir", lambda: tmp_path)
    monkeypatch.setattr(browser, "_lock_pid", lambda d: None)
    monkeypatch.setattr(
        browser,
        "_chromium_cmdline_tokens",
        lambda pid: (
            ["chrome", "--type=zygote", f"--user-data-dir={tmp_path}"]
            if pid == 4241 else
            ["chrome", "--type=utility", f"--user-data-dir={tmp_path}"]
            if pid == 4245 else
            ["agent-browser", "daemon", f"--user-data-dir={tmp_path}"]
            if pid == 4300 else
            ["agent-browser", "cri", f"--user-data-dir={tmp_path}"]
            if pid == leftover_cri else
            ["chrome", f"--user-data-dir={tmp_path}"]
        ),
    )
    monkeypatch.setattr(
        browser,
        "_proc_ppid",
        lambda pid: {
            4240: 4300, 4241: 4240, 4245: 4241, 4242: 4300, 4243: 4300,
            4244: 4300,
        }.get(pid),
    )
    monkeypatch.setattr(
        browser,
        "_launched_by_session",
        lambda pid: "h_review" if pid == 4240 else None,
    )
    monkeypatch.setattr(browser, "_recover_cdp_port_from_singleton", lambda *a, **k: None)
    monkeypatch.setattr(
        browser, "_this_jar_children",
        lambda parent, user_data_dir: (
            {4240, 4242, 4243, 4244} if parent == 4300 else set()
        ),
    )

    def _inodes(port):
        if port == 40141:
            return {7: {"::1"}}
        if port == 9333:
            return {8: {"::1"}}
        if port == 18888:
            return {9: {"::1"}}
        return {}

    def _holders(want):
        out = {}
        if 7 in want:
            out[4242] = {7}
            out[4243] = {7}
        if 8 in want:
            out[4240] = {8}
            out[4241] = {8}
            out[4245] = {8}
        if 9 in want:
            out[4300] = {9}
        return out

    monkeypatch.setattr(browser, "_loopback_listen_inodes_for_port", _inodes)
    monkeypatch.setattr(browser, "_pids_holding_socket_inodes", _holders)
    monkeypatch.setattr(
        browser, "_loopback_listen_ports_for_pid",
        lambda pid: {40141, 18888} if pid == 4300 else (
            {40141} if pid in leftover_helpers else (
                {9333} if pid in chrome_family else set()
            )
        ),
    )
    monkeypatch.setattr(
        browser, "_loopback_listen_targets_for_pid",
        lambda pid: {("::1", 40141), ("::1", 18888)} if pid == 4300 else (
            {("::1", 40141)} if pid in leftover_helpers else (
                {("::1", 9333)} if pid in chrome_family else set()
            )
        ),
    )
    monkeypatch.setattr(
        browser,
        "_cdp_port_reachable",
        lambda port, hosts: port in (9333, 40141, 18888) and "::1" in hosts,
    )
    monkeypatch.setattr(browser, "_configured_cdp_override_url", lambda: "")
    (tmp_path / "DevToolsActivePort").write_text(
        "40141\n/devtools/browser/abc\n", encoding="utf-8",
    )

    _reset_dock_port_memory_for_tests()
    (tmp_path / "dock-cdp-port").write_text("18888\n", encoding="utf-8")
    assert browser._unique_chromium_browser_holder(18888, str(tmp_path)) is None
    assert browser._pid_is_chromium_browser(4300) is False
    assert browser.leftover_helpers_hide_persist_chrome(
        18888, 40141, str(tmp_path),
    ) is True
    assert browser.unique_lock_chrome_hidden_by_leftover_file(
        40141, str(tmp_path),
    ) == 9333
    assert browser.lock_listed_persist_port() == 9333
    assert browser._this_jar_chromium_pid(str(tmp_path)) == 4240
    assert browser.shared_chromium_owner_session() == "h_review"
    assert _remembered_dock_attach_port() == 9333
    assert (tmp_path / "dock-cdp-port").read_text(encoding="utf-8").strip() == "18888"
    assert browser.running_instance_cdp_port(str(tmp_path)) == 9333
    assert _remembered_dock_attach_port() == 9333
    assert browser.persist_live_dock_cdp_port() == 9333
    assert (tmp_path / "dock-cdp-port").read_text(encoding="utf-8") == "9333"
    assert _last_dock_cdp_port.get(hermes_home_key()) == 9333

    def _leftover_only(want):
        out = {}
        if 7 in want:
            out[4242] = {7}
            out[4243] = {7}
        if 9 in want:
            out[4300] = {9}
        return out

    monkeypatch.setattr(browser, "_pids_holding_socket_inodes", _leftover_only)
    monkeypatch.setattr(
        browser, "_loopback_listen_ports_for_pid",
        lambda pid: {40141, 18888} if pid == 4300 else (
            {40141} if pid in leftover_helpers else set()
        ),
    )
    monkeypatch.setattr(
        browser,
        "_cdp_port_reachable",
        lambda port, hosts: port in (40141, 18888) and "::1" in hosts,
    )
    _reset_dock_port_memory_for_tests()
    (tmp_path / "dock-cdp-port").write_text("18888\n", encoding="utf-8")
    assert browser.unique_lock_chrome_hidden_by_leftover_file(
        40141, str(tmp_path),
    ) is None
    assert browser.lock_listed_persist_port() == 18888
    assert browser._this_jar_chromium_pid(str(tmp_path)) is None
    assert browser.running_instance_cdp_port(str(tmp_path)) == 18888
    assert _remembered_dock_attach_port() == 18888


def test_leftover_shared_cri_does_not_hide_sibling_chrome(
    tmp_path, monkeypatch,
):
    """Finding 196: leftover-shared CRI hid sibling chrome.

    Leftover daemon inherited leftover CRI / ``--cdp`` and chrome
    inherited that same listen, so leftover persist and chrome CDP
    both looked leftover-shared (85). Hidden missed; finding 164
    stamped leftover DevTools. Chrome's own listen is the
    leftover-shared port leftover daemon does not inode-hold.
    Several ports leftover daemon also holds stay unknown.
    Leftover-only unique persist + leftover several stays 86.
    Attach must not stamp persist.
    """
    from hermes_constants import hermes_home_key
    from tools.browser_tool_session import (
        _last_dock_cdp_port,
        _remembered_dock_attach_port,
        _reset_dock_port_memory_for_tests,
    )

    leftover_helpers = {4242, 4243}
    chrome_family = {4240, 4241, 4245}
    leftover_cri = 4244

    monkeypatch.setattr(runtime, "state_dir", lambda: tmp_path)
    monkeypatch.setattr(browser, "profile_dir", lambda: tmp_path)
    monkeypatch.setattr(browser, "_lock_pid", lambda d: None)
    monkeypatch.setattr(
        browser,
        "_chromium_cmdline_tokens",
        lambda pid: (
            ["chrome", "--type=zygote", f"--user-data-dir={tmp_path}"]
            if pid == 4241 else
            ["chrome", "--type=utility", f"--user-data-dir={tmp_path}"]
            if pid == 4245 else
            ["agent-browser", "daemon", f"--user-data-dir={tmp_path}"]
            if pid == 4300 else
            ["agent-browser", "cri", f"--user-data-dir={tmp_path}"]
            if pid == leftover_cri else
            ["chrome", f"--user-data-dir={tmp_path}"]
        ),
    )
    monkeypatch.setattr(
        browser,
        "_proc_ppid",
        lambda pid: {
            4240: 4300, 4241: 4240, 4245: 4241, 4242: 4300, 4243: 4300,
            4244: 4300,
        }.get(pid),
    )
    monkeypatch.setattr(
        browser,
        "_launched_by_session",
        lambda pid: "h_review" if pid == 4240 else None,
    )
    monkeypatch.setattr(browser, "_recover_cdp_port_from_singleton", lambda *a, **k: None)
    monkeypatch.setattr(
        browser, "_this_jar_children",
        lambda parent, user_data_dir: (
            {4240, 4242, 4243, 4244} if parent == 4300 else set()
        ),
    )

    def _inodes(port):
        if port == 40141:
            return {7: {"::1"}}
        if port == 9333:
            return {8: {"::1"}}
        if port == 18888:
            return {9: {"::1"}}
        return {}

    def _holders(want):
        out = {}
        if 7 in want:
            out[4242] = {7}
            out[4243] = {7}
        if 8 in want:
            out[4240] = {8}
            out[4241] = {8}
            out[4245] = {8}
        if 9 in want:
            out[4300] = {9}
            out[4240] = {9}
        return out

    monkeypatch.setattr(browser, "_loopback_listen_inodes_for_port", _inodes)
    monkeypatch.setattr(browser, "_pids_holding_socket_inodes", _holders)
    monkeypatch.setattr(
        browser, "_loopback_listen_ports_for_pid",
        lambda pid: {40141, 18888} if pid == 4300 else (
            {40141} if pid in leftover_helpers else (
                {9333, 18888} if pid == 4240 else (
                    {9333} if pid in chrome_family else set()
                )
            )
        ),
    )
    monkeypatch.setattr(
        browser, "_loopback_listen_targets_for_pid",
        lambda pid: {("::1", 40141), ("::1", 18888)} if pid == 4300 else (
            {("::1", 40141)} if pid in leftover_helpers else (
                {("::1", 9333), ("::1", 18888)} if pid == 4240 else (
                    {("::1", 9333)} if pid in chrome_family else set()
                )
            )
        ),
    )
    monkeypatch.setattr(
        browser,
        "_cdp_port_reachable",
        lambda port, hosts: port in (9333, 40141, 18888) and "::1" in hosts,
    )
    monkeypatch.setattr(browser, "_configured_cdp_override_url", lambda: "")
    (tmp_path / "DevToolsActivePort").write_text(
        "40141\n/devtools/browser/abc\n", encoding="utf-8",
    )

    _reset_dock_port_memory_for_tests()
    (tmp_path / "dock-cdp-port").write_text("18888\n", encoding="utf-8")
    assert browser.leftover_helpers_hide_persist_chrome(
        18888, 40141, str(tmp_path),
    ) is False
    assert browser.unique_lock_chrome_hidden_by_leftover_file(
        40141, str(tmp_path),
    ) == 9333
    assert browser.lock_listed_persist_port() == 9333
    assert browser._this_jar_chromium_pid(str(tmp_path)) == 4240
    assert browser.shared_chromium_owner_session() == "h_review"
    assert _remembered_dock_attach_port() == 9333
    assert (tmp_path / "dock-cdp-port").read_text(encoding="utf-8").strip() == "18888"
    assert browser.running_instance_cdp_port(str(tmp_path)) == 9333
    assert _remembered_dock_attach_port() == 9333
    assert (tmp_path / "dock-cdp-port").read_text(encoding="utf-8") == "9333"
    assert _last_dock_cdp_port.get(hermes_home_key()) == 9333

    def _leftover_only(want):
        out = {}
        if 7 in want:
            out[4242] = {7}
            out[4243] = {7}
        if 9 in want:
            out[4300] = {9}
        return out

    monkeypatch.setattr(browser, "_pids_holding_socket_inodes", _leftover_only)
    monkeypatch.setattr(
        browser, "_loopback_listen_ports_for_pid",
        lambda pid: {40141, 18888} if pid == 4300 else (
            {40141} if pid in leftover_helpers else set()
        ),
    )
    monkeypatch.setattr(
        browser,
        "_cdp_port_reachable",
        lambda port, hosts: port in (40141, 18888) and "::1" in hosts,
    )
    _reset_dock_port_memory_for_tests()
    (tmp_path / "dock-cdp-port").write_text("18888\n", encoding="utf-8")
    assert browser.unique_lock_chrome_hidden_by_leftover_file(
        40141, str(tmp_path),
    ) is None
    assert browser.lock_listed_persist_port() == 18888
    assert browser._this_jar_chromium_pid(str(tmp_path)) is None
    assert browser.running_instance_cdp_port(str(tmp_path)) == 18888
    assert _remembered_dock_attach_port() == 18888


def test_unique_leftover_daemon_devtools_does_not_hide_sibling_chrome(
    tmp_path, monkeypatch,
):
    """Finding 197: unique leftover-daemon DevTools hid sibling chrome.

    Leftover fill clients exited and chrome dropped the inherited
    DevTools listen, so leftover daemon uniquely holds stale
    ``DevToolsActivePort``. 184-196 required several leftover
    holders and treated that unique leftover file as chrome
    (161). Persist / attach / skip-kill / Take over followed
    leftover DevTools; chrome pid became leftover daemon
    (``owner`` None) so Take over tree-killed the Browser a
    human is typing into. Unique leftover daemon is not chrome.
    Sibling chrome is still a this-jar child of leftover daemon.
    Leftover-only unique leftover daemon DevTools stays 86.
    Attach must not stamp persist.
    """
    from hermes_constants import hermes_home_key
    from tools.browser_tool_session import (
        _last_dock_cdp_port,
        _remembered_dock_attach_port,
        _reset_dock_port_memory_for_tests,
    )

    chrome_family = {4240, 4241, 4245}
    leftover_cri = 4244

    monkeypatch.setattr(runtime, "state_dir", lambda: tmp_path)
    monkeypatch.setattr(browser, "profile_dir", lambda: tmp_path)
    monkeypatch.setattr(browser, "_lock_pid", lambda d: None)
    monkeypatch.setattr(
        browser,
        "_chromium_cmdline_tokens",
        lambda pid: (
            ["chrome", "--type=zygote", f"--user-data-dir={tmp_path}"]
            if pid == 4241 else
            ["chrome", "--type=utility", f"--user-data-dir={tmp_path}"]
            if pid == 4245 else
            ["agent-browser", "daemon", f"--user-data-dir={tmp_path}"]
            if pid == 4300 else
            ["agent-browser", "cri", f"--user-data-dir={tmp_path}"]
            if pid == leftover_cri else
            ["chrome", f"--user-data-dir={tmp_path}"]
        ),
    )
    monkeypatch.setattr(
        browser,
        "_proc_ppid",
        lambda pid: {
            4240: 4300, 4241: 4240, 4245: 4241, 4244: 4300,
        }.get(pid),
    )
    monkeypatch.setattr(
        browser,
        "_launched_by_session",
        lambda pid: "h_review" if pid == 4240 else None,
    )
    monkeypatch.setattr(browser, "_recover_cdp_port_from_singleton", lambda *a, **k: None)
    monkeypatch.setattr(
        browser, "_this_jar_children",
        lambda parent, user_data_dir: (
            {4240, 4244} if parent == 4300 else set()
        ),
    )

    def _inodes(port):
        if port == 40141:
            return {7: {"::1"}}
        if port == 9333:
            return {8: {"::1"}}
        if port == 18888:
            return {9: {"::1"}}
        return {}

    def _holders(want):
        out = {}
        if 7 in want:
            out[4300] = {7}
        if 8 in want:
            out[4240] = {8}
            out[4241] = {8}
            out[4245] = {8}
        if 9 in want:
            out[4244] = {9}
        return out

    monkeypatch.setattr(browser, "_loopback_listen_inodes_for_port", _inodes)
    monkeypatch.setattr(browser, "_pids_holding_socket_inodes", _holders)
    monkeypatch.setattr(
        browser, "_loopback_listen_ports_for_pid",
        lambda pid: {40141} if pid == 4300 else (
            {18888} if pid == leftover_cri else (
                {9333} if pid in chrome_family else set()
            )
        ),
    )
    monkeypatch.setattr(
        browser, "_loopback_listen_targets_for_pid",
        lambda pid: {("::1", 40141)} if pid == 4300 else (
            {("::1", 18888)} if pid == leftover_cri else (
                {("::1", 9333)} if pid in chrome_family else set()
            )
        ),
    )
    monkeypatch.setattr(
        browser,
        "_cdp_port_reachable",
        lambda port, hosts: port in (9333, 40141, 18888) and "::1" in hosts,
    )
    monkeypatch.setattr(browser, "_configured_cdp_override_url", lambda: "")
    (tmp_path / "DevToolsActivePort").write_text(
        "40141\n/devtools/browser/abc\n", encoding="utf-8",
    )

    _reset_dock_port_memory_for_tests()
    (tmp_path / "dock-cdp-port").write_text("18888\n", encoding="utf-8")
    assert browser._this_jar_holder_pids(40141, str(tmp_path)) == {4300}
    assert browser._pid_is_chromium_browser(4300) is False
    assert browser.leftover_helpers_hide_persist_chrome(
        18888, 40141, str(tmp_path),
    ) is False
    assert browser.unique_lock_chrome_hidden_by_leftover_file(
        40141, str(tmp_path),
    ) == 9333
    assert browser.lock_listed_persist_port() == 9333
    assert browser._this_jar_chromium_pid(str(tmp_path)) == 4240
    assert browser.shared_chromium_owner_session() == "h_review"
    assert _remembered_dock_attach_port() == 9333
    assert (tmp_path / "dock-cdp-port").read_text(encoding="utf-8").strip() == "18888"
    assert browser.running_instance_cdp_port(str(tmp_path)) == 9333
    assert _remembered_dock_attach_port() == 9333
    assert browser.persist_live_dock_cdp_port() == 9333
    assert (tmp_path / "dock-cdp-port").read_text(encoding="utf-8") == "9333"
    assert _last_dock_cdp_port.get(hermes_home_key()) == 9333

    def _leftover_only(want):
        out = {}
        if 7 in want:
            out[4300] = {7}
        if 9 in want:
            out[4244] = {9}
        return out

    monkeypatch.setattr(browser, "_pids_holding_socket_inodes", _leftover_only)
    monkeypatch.setattr(
        browser, "_this_jar_children",
        lambda parent, user_data_dir: (
            {4244} if parent == 4300 else set()
        ),
    )
    monkeypatch.setattr(
        browser, "_loopback_listen_ports_for_pid",
        lambda pid: {40141} if pid == 4300 else (
            {18888} if pid == leftover_cri else set()
        ),
    )
    monkeypatch.setattr(
        browser,
        "_cdp_port_reachable",
        lambda port, hosts: port in (40141, 18888) and "::1" in hosts,
    )
    _reset_dock_port_memory_for_tests()
    (tmp_path / "dock-cdp-port").write_text("18888\n", encoding="utf-8")
    assert browser.unique_lock_chrome_hidden_by_leftover_file(
        40141, str(tmp_path),
    ) is None
    assert browser.lock_listed_persist_port() is None
    assert browser._this_jar_chromium_pid(str(tmp_path)) is None
    assert browser.running_instance_cdp_port(str(tmp_path)) == 40141
    assert _remembered_dock_attach_port() == 40141


def test_unique_leftover_fill_devtools_does_not_hide_sibling_chrome(
    tmp_path, monkeypatch,
):
    """Finding 198: unique leftover-fill DevTools hid sibling chrome.

    Leftover fill clients exited except one leftover fill that
    uniquely inherited stale ``DevToolsActivePort``. Leftover
    daemon dropped that listen. Finding 197 scanned children of
    the unique leftover-file holder; unique leftover fill has
    no chrome children, so hidden missed sibling chrome. Take
    over then treated chrome pid None as leftover and tree-killed
    the Browser a human is typing into. Unique leftover fill is
    not chrome. Sibling chrome is still a this-jar child of
    leftover fill's this-jar parent. Cousin leftover fill whose
    parent is leftover CRI stays 86. Leftover-only unique
    leftover fill DevTools stays 86. Attach must not stamp
    persist.
    """
    from hermes_constants import hermes_home_key
    from tools.browser_tool_session import (
        _last_dock_cdp_port,
        _remembered_dock_attach_port,
        _reset_dock_port_memory_for_tests,
    )

    chrome_family = {4240, 4241, 4245}
    leftover_fill = 4242
    leftover_cri = 4244

    monkeypatch.setattr(runtime, "state_dir", lambda: tmp_path)
    monkeypatch.setattr(browser, "profile_dir", lambda: tmp_path)
    monkeypatch.setattr(browser, "_lock_pid", lambda d: None)
    monkeypatch.setattr(
        browser,
        "_chromium_cmdline_tokens",
        lambda pid: (
            ["chrome", "--type=zygote", f"--user-data-dir={tmp_path}"]
            if pid == 4241 else
            ["chrome", "--type=utility", f"--user-data-dir={tmp_path}"]
            if pid == 4245 else
            ["agent-browser", "daemon", f"--user-data-dir={tmp_path}"]
            if pid == 4300 else
            ["agent-browser", "fill", f"--user-data-dir={tmp_path}"]
            if pid == leftover_fill else
            ["agent-browser", "cri", f"--user-data-dir={tmp_path}"]
            if pid == leftover_cri else
            ["chrome", f"--user-data-dir={tmp_path}"]
        ),
    )
    monkeypatch.setattr(
        browser,
        "_proc_ppid",
        lambda pid: {
            4240: 4300, 4241: 4240, 4245: 4241,
            leftover_fill: 4300, leftover_cri: 4300,
        }.get(pid),
    )
    monkeypatch.setattr(
        browser,
        "_launched_by_session",
        lambda pid: "h_review" if pid == 4240 else None,
    )
    monkeypatch.setattr(browser, "_recover_cdp_port_from_singleton", lambda *a, **k: None)
    monkeypatch.setattr(
        browser, "_this_jar_children",
        lambda parent, user_data_dir: (
            {4240, leftover_fill, leftover_cri} if parent == 4300 else set()
        ),
    )

    def _inodes(port):
        if port == 40141:
            return {7: {"::1"}}
        if port == 9333:
            return {8: {"::1"}}
        if port == 18888:
            return {9: {"::1"}}
        return {}

    def _holders(want):
        out = {}
        if 7 in want:
            out[leftover_fill] = {7}
        if 8 in want:
            out[4240] = {8}
            out[4241] = {8}
            out[4245] = {8}
        if 9 in want:
            out[leftover_cri] = {9}
        return out

    monkeypatch.setattr(browser, "_loopback_listen_inodes_for_port", _inodes)
    monkeypatch.setattr(browser, "_pids_holding_socket_inodes", _holders)
    monkeypatch.setattr(
        browser, "_loopback_listen_ports_for_pid",
        lambda pid: {40141} if pid == leftover_fill else (
            {18888} if pid == leftover_cri else (
                {9333} if pid in chrome_family else set()
            )
        ),
    )
    monkeypatch.setattr(
        browser, "_loopback_listen_targets_for_pid",
        lambda pid: {("::1", 40141)} if pid == leftover_fill else (
            {("::1", 18888)} if pid == leftover_cri else (
                {("::1", 9333)} if pid in chrome_family else set()
            )
        ),
    )
    monkeypatch.setattr(
        browser,
        "_cdp_port_reachable",
        lambda port, hosts: port in (9333, 40141, 18888) and "::1" in hosts,
    )
    monkeypatch.setattr(browser, "_configured_cdp_override_url", lambda: "")
    (tmp_path / "DevToolsActivePort").write_text(
        "40141\n/devtools/browser/abc\n", encoding="utf-8",
    )

    _reset_dock_port_memory_for_tests()
    (tmp_path / "dock-cdp-port").write_text("18888\n", encoding="utf-8")
    assert browser._this_jar_holder_pids(40141, str(tmp_path)) == {leftover_fill}
    assert browser._pid_is_chromium_browser(leftover_fill) is False
    assert browser.leftover_helpers_hide_persist_chrome(
        18888, 40141, str(tmp_path),
    ) is False
    assert browser.unique_lock_chrome_hidden_by_leftover_file(
        40141, str(tmp_path),
    ) == 9333
    assert browser.lock_listed_persist_port() == 9333
    assert browser._this_jar_chromium_pid(str(tmp_path)) == 4240
    assert browser.shared_chromium_owner_session() == "h_review"
    assert _remembered_dock_attach_port() == 9333
    assert (tmp_path / "dock-cdp-port").read_text(encoding="utf-8").strip() == "18888"
    assert browser.running_instance_cdp_port(str(tmp_path)) == 9333
    assert _remembered_dock_attach_port() == 9333
    assert browser.persist_live_dock_cdp_port() == 9333
    assert (tmp_path / "dock-cdp-port").read_text(encoding="utf-8") == "9333"
    assert _last_dock_cdp_port.get(hermes_home_key()) == 9333

    def _leftover_only(want):
        out = {}
        if 7 in want:
            out[leftover_fill] = {7}
        if 9 in want:
            out[leftover_cri] = {9}
        return out

    monkeypatch.setattr(browser, "_pids_holding_socket_inodes", _leftover_only)
    monkeypatch.setattr(
        browser, "_this_jar_children",
        lambda parent, user_data_dir: (
            {leftover_fill, leftover_cri} if parent == 4300 else set()
        ),
    )
    monkeypatch.setattr(
        browser, "_loopback_listen_ports_for_pid",
        lambda pid: {40141} if pid == leftover_fill else (
            {18888} if pid == leftover_cri else set()
        ),
    )
    monkeypatch.setattr(
        browser,
        "_cdp_port_reachable",
        lambda port, hosts: port in (40141, 18888) and "::1" in hosts,
    )
    _reset_dock_port_memory_for_tests()
    (tmp_path / "dock-cdp-port").write_text("18888\n", encoding="utf-8")
    assert browser.unique_lock_chrome_hidden_by_leftover_file(
        40141, str(tmp_path),
    ) is None
    assert browser.lock_listed_persist_port() is None
    assert browser._this_jar_chromium_pid(str(tmp_path)) is None
    assert browser.running_instance_cdp_port(str(tmp_path)) == 40141
    assert _remembered_dock_attach_port() == 40141


def test_leftover_fill_inherited_persist_does_not_hide_sibling_chrome(
    tmp_path, monkeypatch,
):
    """Finding 199: leftover fill inherited leftover persist hid sibling chrome.

    Leftover fill uniquely holds stale ``DevToolsActivePort`` and
    also inherited leftover persist with chrome. Chrome family
    several on chrome's own CDP. Leftover daemon holds neither
    listen, so finding 196's leftover-daemon-miss list is several
    and hidden missed. Persist / attach / skip-kill / Take over
    followed leftover DevTools; chrome pid became None
    (``owner`` None) so Take over tree-killed the Browser a
    human is typing into. Prefer the inherited listen leftover
    file holders do not inode-hold — that is chrome's own CDP.
    Leftover fill that inherited both leftover persist and
    chrome's CDP stays 85. Leftover-only leftover fill plus
    leftover CRI leftover persist stays 86. Attach must not
    stamp persist.
    """
    from hermes_constants import hermes_home_key
    from tools.browser_tool_session import (
        _last_dock_cdp_port,
        _remembered_dock_attach_port,
        _reset_dock_port_memory_for_tests,
    )

    chrome_family = {4240, 4241, 4245}
    leftover_fill = 4242
    leftover_cri = 4244

    monkeypatch.setattr(runtime, "state_dir", lambda: tmp_path)
    monkeypatch.setattr(browser, "profile_dir", lambda: tmp_path)
    monkeypatch.setattr(browser, "_lock_pid", lambda d: None)
    monkeypatch.setattr(
        browser,
        "_chromium_cmdline_tokens",
        lambda pid: (
            ["chrome", "--type=zygote", f"--user-data-dir={tmp_path}"]
            if pid == 4241 else
            ["chrome", "--type=utility", f"--user-data-dir={tmp_path}"]
            if pid == 4245 else
            ["agent-browser", "daemon", f"--user-data-dir={tmp_path}"]
            if pid == 4300 else
            ["agent-browser", "fill", f"--user-data-dir={tmp_path}"]
            if pid == leftover_fill else
            ["agent-browser", "cri", f"--user-data-dir={tmp_path}"]
            if pid == leftover_cri else
            ["chrome", f"--user-data-dir={tmp_path}"]
        ),
    )
    monkeypatch.setattr(
        browser,
        "_proc_ppid",
        lambda pid: {
            4240: 4300, 4241: 4240, 4245: 4241,
            leftover_fill: 4300, leftover_cri: 4300,
        }.get(pid),
    )
    monkeypatch.setattr(
        browser,
        "_launched_by_session",
        lambda pid: "h_review" if pid == 4240 else None,
    )
    monkeypatch.setattr(browser, "_recover_cdp_port_from_singleton", lambda *a, **k: None)
    monkeypatch.setattr(
        browser, "_this_jar_children",
        lambda parent, user_data_dir: (
            {4240, leftover_fill, leftover_cri} if parent == 4300 else set()
        ),
    )

    def _inodes(port):
        if port == 40141:
            return {7: {"::1"}}
        if port == 9333:
            return {8: {"::1"}}
        if port == 18888:
            return {9: {"::1"}}
        return {}

    def _holders(want):
        out = {}
        if 7 in want:
            out[leftover_fill] = {7}
        if 8 in want:
            out[4240] = {8}
            out[4241] = {8}
            out[4245] = {8}
        if 9 in want:
            out[leftover_fill] = {9}
            out[4240] = {9}
        return out

    monkeypatch.setattr(browser, "_loopback_listen_inodes_for_port", _inodes)
    monkeypatch.setattr(browser, "_pids_holding_socket_inodes", _holders)
    monkeypatch.setattr(
        browser, "_loopback_listen_ports_for_pid",
        lambda pid: {40141, 18888} if pid == leftover_fill else (
            {9333, 18888} if pid == 4240 else (
                {9333} if pid in {4241, 4245} else set()
            )
        ),
    )
    monkeypatch.setattr(
        browser, "_loopback_listen_targets_for_pid",
        lambda pid: {("::1", 40141), ("::1", 18888)} if pid == leftover_fill else (
            {("::1", 9333), ("::1", 18888)} if pid == 4240 else (
                {("::1", 9333)} if pid in {4241, 4245} else set()
            )
        ),
    )
    monkeypatch.setattr(
        browser,
        "_cdp_port_reachable",
        lambda port, hosts: port in (9333, 40141, 18888) and "::1" in hosts,
    )
    monkeypatch.setattr(browser, "_configured_cdp_override_url", lambda: "")
    (tmp_path / "DevToolsActivePort").write_text(
        "40141\n/devtools/browser/abc\n", encoding="utf-8",
    )

    _reset_dock_port_memory_for_tests()
    (tmp_path / "dock-cdp-port").write_text("18888\n", encoding="utf-8")
    assert browser._this_jar_holder_pids(40141, str(tmp_path)) == {leftover_fill}
    assert browser._this_jar_holder_pids(18888, str(tmp_path)) == {leftover_fill, 4240}
    assert browser._pid_is_chromium_browser(leftover_fill) is False
    assert browser.unique_lock_chrome_hidden_by_leftover_file(
        40141, str(tmp_path),
    ) == 9333
    assert browser.lock_listed_persist_port() == 9333
    assert browser._this_jar_chromium_pid(str(tmp_path)) == 4240
    assert browser.shared_chromium_owner_session() == "h_review"
    assert _remembered_dock_attach_port() == 9333
    assert (tmp_path / "dock-cdp-port").read_text(encoding="utf-8").strip() == "18888"
    assert browser.running_instance_cdp_port(str(tmp_path)) == 9333
    assert _remembered_dock_attach_port() == 9333
    assert browser.persist_live_dock_cdp_port() == 9333
    assert (tmp_path / "dock-cdp-port").read_text(encoding="utf-8") == "9333"
    assert _last_dock_cdp_port.get(hermes_home_key()) == 9333

    def _leftover_only(want):
        out = {}
        if 7 in want:
            out[leftover_fill] = {7}
        if 9 in want:
            out[leftover_fill] = {9}
            out[leftover_cri] = {9}
        return out

    monkeypatch.setattr(browser, "_pids_holding_socket_inodes", _leftover_only)
    monkeypatch.setattr(
        browser, "_this_jar_children",
        lambda parent, user_data_dir: (
            {leftover_fill, leftover_cri} if parent == 4300 else set()
        ),
    )
    monkeypatch.setattr(
        browser, "_loopback_listen_ports_for_pid",
        lambda pid: {40141, 18888} if pid == leftover_fill else (
            {18888} if pid == leftover_cri else set()
        ),
    )
    monkeypatch.setattr(
        browser,
        "_cdp_port_reachable",
        lambda port, hosts: port in (40141, 18888) and "::1" in hosts,
    )
    _reset_dock_port_memory_for_tests()
    (tmp_path / "dock-cdp-port").write_text("18888\n", encoding="utf-8")
    assert browser.unique_lock_chrome_hidden_by_leftover_file(
        40141, str(tmp_path),
    ) is None
    assert browser.lock_listed_persist_port() is None
    assert browser._this_jar_chromium_pid(str(tmp_path)) is None
    assert browser.running_instance_cdp_port(str(tmp_path)) == 40141
    assert _remembered_dock_attach_port() == 40141


def test_leftover_python_inherited_chrome_cdp_does_not_hide_sibling_chrome(
    tmp_path, monkeypatch,
):
    """Finding 200: leftover python inherited chrome CDP hid sibling chrome.

    Leftover fill uniquely holds stale ``DevToolsActivePort``.
    Leftover python inherited chrome's CDP with the chrome
    family. Leftover python is extra — not a leftover file
    holder and not a chrome descendant — so leftover-shared
    / extra-root treated chrome as unknown. Persist / attach
    / skip-kill / Take over followed leftover DevTools;
    chrome pid became None (``owner`` None) so Take over
    tree-killed the Browser a human is typing into. Non-
    browser extras stay leftover. Leftover-only leftover
    fill plus leftover python leftover persist stays 86.
    Attach must not stamp persist. ``remember()`` still
    does not update memory (finding 178). Identity
    candidate order stays file-then-memory (finding 169).
    """
    from hermes_constants import hermes_home_key
    from tools.browser_tool_session import (
        _last_dock_cdp_port,
        _remembered_dock_attach_port,
        _reset_dock_port_memory_for_tests,
    )

    leftover_fill = 4242
    leftover_cri = 4244
    leftover_py = 4246

    monkeypatch.setattr(runtime, "state_dir", lambda: tmp_path)
    monkeypatch.setattr(browser, "profile_dir", lambda: tmp_path)
    monkeypatch.setattr(browser, "_lock_pid", lambda d: None)
    monkeypatch.setattr(
        browser,
        "_chromium_cmdline_tokens",
        lambda pid: (
            ["chrome", "--type=zygote", f"--user-data-dir={tmp_path}"]
            if pid == 4241 else
            ["chrome", "--type=utility", f"--user-data-dir={tmp_path}"]
            if pid == 4245 else
            ["agent-browser", "daemon", f"--user-data-dir={tmp_path}"]
            if pid == 4300 else
            ["agent-browser", "fill", f"--user-data-dir={tmp_path}"]
            if pid == leftover_fill else
            ["agent-browser", "cri", f"--user-data-dir={tmp_path}"]
            if pid == leftover_cri else
            ["agent-browser", "python", f"--user-data-dir={tmp_path}"]
            if pid == leftover_py else
            ["chrome", f"--user-data-dir={tmp_path}"]
        ),
    )
    monkeypatch.setattr(
        browser,
        "_proc_ppid",
        lambda pid: {
            4240: 4300, 4241: 4240, 4245: 4241,
            leftover_fill: 4300, leftover_cri: 4300, leftover_py: 4300,
        }.get(pid),
    )
    monkeypatch.setattr(
        browser,
        "_launched_by_session",
        lambda pid: "h_review" if pid == 4240 else None,
    )
    monkeypatch.setattr(browser, "_recover_cdp_port_from_singleton", lambda *a, **k: None)
    monkeypatch.setattr(
        browser, "_this_jar_children",
        lambda parent, user_data_dir: (
            {4240, leftover_fill, leftover_cri, leftover_py}
            if parent == 4300 else set()
        ),
    )

    def _inodes(port):
        if port == 40141:
            return {7: {"::1"}}
        if port == 9333:
            return {8: {"::1"}}
        if port == 18888:
            return {9: {"::1"}}
        return {}

    def _holders(want):
        out = {}
        if 7 in want:
            out[leftover_fill] = {7}
        if 8 in want:
            out[4240] = {8}
            out[4241] = {8}
            out[4245] = {8}
            out[leftover_py] = {8}
        return out

    monkeypatch.setattr(browser, "_loopback_listen_inodes_for_port", _inodes)
    monkeypatch.setattr(browser, "_pids_holding_socket_inodes", _holders)
    monkeypatch.setattr(
        browser, "_loopback_listen_ports_for_pid",
        lambda pid: {40141} if pid == leftover_fill else (
            {9333} if pid == leftover_py else (
                {9333} if pid in {4240, 4241, 4245} else set()
            )
        ),
    )
    monkeypatch.setattr(
        browser, "_loopback_listen_targets_for_pid",
        lambda pid: {("::1", 40141)} if pid == leftover_fill else (
            {("::1", 9333)} if pid in {4240, 4241, 4245, leftover_py} else set()
        ),
    )
    monkeypatch.setattr(
        browser,
        "_cdp_port_reachable",
        lambda port, hosts: port in (9333, 40141) and "::1" in hosts,
    )
    monkeypatch.setattr(browser, "_configured_cdp_override_url", lambda: "")
    (tmp_path / "DevToolsActivePort").write_text(
        "40141\n/devtools/browser/abc\n", encoding="utf-8",
    )

    _reset_dock_port_memory_for_tests()
    (tmp_path / "dock-cdp-port").write_text("18888\n", encoding="utf-8")
    assert browser._this_jar_holder_pids(40141, str(tmp_path)) == {leftover_fill}
    assert browser._this_jar_holder_pids(9333, str(tmp_path)) == {
        4240, 4241, 4245, leftover_py,
    }
    assert browser._pid_is_chromium_browser(leftover_py) is False
    assert browser.unique_lock_chrome_hidden_by_leftover_file(
        40141, str(tmp_path),
    ) == 9333
    assert browser.lock_listed_persist_port() == 9333
    assert browser._this_jar_chromium_pid(str(tmp_path)) == 4240
    assert browser.shared_chromium_owner_session() == "h_review"
    assert _remembered_dock_attach_port() == 9333
    assert (tmp_path / "dock-cdp-port").read_text(encoding="utf-8").strip() == "18888"
    assert browser.running_instance_cdp_port(str(tmp_path)) == 9333
    assert _remembered_dock_attach_port() == 9333
    assert browser.persist_live_dock_cdp_port() == 9333
    assert (tmp_path / "dock-cdp-port").read_text(encoding="utf-8") == "9333"
    assert _last_dock_cdp_port.get(hermes_home_key()) == 9333

    def _leftover_only(want):
        out = {}
        if 7 in want:
            out[leftover_fill] = {7}
        if 9 in want:
            out[leftover_py] = {9}
        return out

    monkeypatch.setattr(browser, "_pids_holding_socket_inodes", _leftover_only)
    monkeypatch.setattr(
        browser, "_this_jar_children",
        lambda parent, user_data_dir: (
            {leftover_fill, leftover_cri, leftover_py} if parent == 4300 else set()
        ),
    )
    monkeypatch.setattr(
        browser, "_loopback_listen_ports_for_pid",
        lambda pid: {40141} if pid == leftover_fill else (
            {18888} if pid == leftover_py else set()
        ),
    )
    monkeypatch.setattr(
        browser,
        "_cdp_port_reachable",
        lambda port, hosts: port in (40141, 18888) and "::1" in hosts,
    )
    _reset_dock_port_memory_for_tests()
    (tmp_path / "dock-cdp-port").write_text("18888\n", encoding="utf-8")
    assert browser.unique_lock_chrome_hidden_by_leftover_file(
        40141, str(tmp_path),
    ) is None
    assert browser.lock_listed_persist_port() is None
    assert browser._this_jar_chromium_pid(str(tmp_path)) is None
    assert browser.running_instance_cdp_port(str(tmp_path)) == 40141
    assert _remembered_dock_attach_port() == 40141


def test_leftover_python_inherited_persist_does_not_hide_sibling_chrome(
    tmp_path, monkeypatch,
):
    """Finding 201: leftover python inherited leftover persist hid sibling chrome.

    Leftover fill uniquely holds stale ``DevToolsActivePort``.
    Leftover python inherited leftover persist with chrome.
    Leftover fill holds neither leftover persist nor chrome's
    own CDP, so finding 199 leftover-file-miss is several
    and hidden missed. Persist / attach / skip-kill / Take
    over followed leftover DevTools; chrome pid became None
    (``owner`` None) so Take over tree-killed the Browser a
    human is typing into. Prefer the leftover-file-miss
    listen leftover siblings do not inode-hold — that is
    chrome's own CDP. Leftover python that inherited both
    leftover persist and chrome's CDP stays 85. Leftover-
    only leftover fill plus leftover python leftover persist
    stays 86. Attach must not stamp persist. ``remember()``
    still does not update memory (finding 178). Identity
    candidate order stays file-then-memory (finding 169).
    """
    from hermes_constants import hermes_home_key
    from tools.browser_tool_session import (
        _last_dock_cdp_port,
        _remembered_dock_attach_port,
        _reset_dock_port_memory_for_tests,
    )

    leftover_fill = 4242
    leftover_cri = 4244
    leftover_py = 4246

    monkeypatch.setattr(runtime, "state_dir", lambda: tmp_path)
    monkeypatch.setattr(browser, "profile_dir", lambda: tmp_path)
    monkeypatch.setattr(browser, "_lock_pid", lambda d: None)
    monkeypatch.setattr(
        browser,
        "_chromium_cmdline_tokens",
        lambda pid: (
            ["chrome", "--type=zygote", f"--user-data-dir={tmp_path}"]
            if pid == 4241 else
            ["chrome", "--type=utility", f"--user-data-dir={tmp_path}"]
            if pid == 4245 else
            ["agent-browser", "daemon", f"--user-data-dir={tmp_path}"]
            if pid == 4300 else
            ["agent-browser", "fill", f"--user-data-dir={tmp_path}"]
            if pid == leftover_fill else
            ["agent-browser", "cri", f"--user-data-dir={tmp_path}"]
            if pid == leftover_cri else
            ["agent-browser", "python", f"--user-data-dir={tmp_path}"]
            if pid == leftover_py else
            ["chrome", f"--user-data-dir={tmp_path}"]
        ),
    )
    monkeypatch.setattr(
        browser,
        "_proc_ppid",
        lambda pid: {
            4240: 4300, 4241: 4240, 4245: 4241,
            leftover_fill: 4300, leftover_cri: 4300, leftover_py: 4300,
        }.get(pid),
    )
    monkeypatch.setattr(
        browser,
        "_launched_by_session",
        lambda pid: "h_review" if pid == 4240 else None,
    )
    monkeypatch.setattr(browser, "_recover_cdp_port_from_singleton", lambda *a, **k: None)
    monkeypatch.setattr(
        browser, "_this_jar_children",
        lambda parent, user_data_dir: (
            {4240, leftover_fill, leftover_cri, leftover_py}
            if parent == 4300 else set()
        ),
    )

    def _inodes(port):
        if port == 40141:
            return {7: {"::1"}}
        if port == 9333:
            return {8: {"::1"}}
        if port == 18888:
            return {9: {"::1"}}
        return {}

    def _holders(want):
        out = {}
        if 7 in want:
            out[leftover_fill] = {7}
        if 8 in want:
            out[4240] = {8}
            out[4241] = {8}
            out[4245] = {8}
        if 9 in want:
            out[leftover_py] = {9}
            out[4240] = out.get(4240, set()) | {9}
        return out

    monkeypatch.setattr(browser, "_loopback_listen_inodes_for_port", _inodes)
    monkeypatch.setattr(browser, "_pids_holding_socket_inodes", _holders)
    monkeypatch.setattr(
        browser, "_loopback_listen_ports_for_pid",
        lambda pid: {40141} if pid == leftover_fill else (
            {18888} if pid == leftover_py else (
                {9333, 18888} if pid == 4240 else (
                    {9333} if pid in {4241, 4245} else set()
                )
            )
        ),
    )
    monkeypatch.setattr(
        browser, "_loopback_listen_targets_for_pid",
        lambda pid: {("::1", 40141)} if pid == leftover_fill else (
            {("::1", 18888)} if pid == leftover_py else (
                {("::1", 9333), ("::1", 18888)} if pid == 4240 else (
                    {("::1", 9333)} if pid in {4241, 4245} else set()
                )
            )
        ),
    )
    monkeypatch.setattr(
        browser,
        "_cdp_port_reachable",
        lambda port, hosts: port in (9333, 40141, 18888) and "::1" in hosts,
    )
    monkeypatch.setattr(browser, "_configured_cdp_override_url", lambda: "")
    (tmp_path / "DevToolsActivePort").write_text(
        "40141\n/devtools/browser/abc\n", encoding="utf-8",
    )

    _reset_dock_port_memory_for_tests()
    (tmp_path / "dock-cdp-port").write_text("18888\n", encoding="utf-8")
    assert browser._this_jar_holder_pids(40141, str(tmp_path)) == {leftover_fill}
    assert browser._this_jar_holder_pids(18888, str(tmp_path)) == {leftover_py, 4240}
    assert browser._this_jar_holder_pids(9333, str(tmp_path)) == {4240, 4241, 4245}
    assert browser._pid_is_chromium_browser(leftover_py) is False
    assert browser.unique_lock_chrome_hidden_by_leftover_file(
        40141, str(tmp_path),
    ) == 9333
    assert browser.lock_listed_persist_port() == 9333
    assert browser._this_jar_chromium_pid(str(tmp_path)) == 4240
    assert browser.shared_chromium_owner_session() == "h_review"
    assert _remembered_dock_attach_port() == 9333
    assert (tmp_path / "dock-cdp-port").read_text(encoding="utf-8").strip() == "18888"
    assert browser.running_instance_cdp_port(str(tmp_path)) == 9333
    assert _remembered_dock_attach_port() == 9333
    assert browser.persist_live_dock_cdp_port() == 9333
    assert (tmp_path / "dock-cdp-port").read_text(encoding="utf-8") == "9333"
    assert _last_dock_cdp_port.get(hermes_home_key()) == 9333

    def _leftover_only(want):
        out = {}
        if 7 in want:
            out[leftover_fill] = {7}
        if 9 in want:
            out[leftover_py] = {9}
        return out

    monkeypatch.setattr(browser, "_pids_holding_socket_inodes", _leftover_only)
    monkeypatch.setattr(
        browser, "_this_jar_children",
        lambda parent, user_data_dir: (
            {leftover_fill, leftover_cri, leftover_py} if parent == 4300 else set()
        ),
    )
    monkeypatch.setattr(
        browser, "_loopback_listen_ports_for_pid",
        lambda pid: {40141} if pid == leftover_fill else (
            {18888} if pid == leftover_py else set()
        ),
    )
    monkeypatch.setattr(
        browser,
        "_cdp_port_reachable",
        lambda port, hosts: port in (40141, 18888) and "::1" in hosts,
    )
    _reset_dock_port_memory_for_tests()
    (tmp_path / "dock-cdp-port").write_text("18888\n", encoding="utf-8")
    assert browser.unique_lock_chrome_hidden_by_leftover_file(
        40141, str(tmp_path),
    ) is None
    assert browser.lock_listed_persist_port() is None
    assert browser._this_jar_chromium_pid(str(tmp_path)) is None
    assert browser.running_instance_cdp_port(str(tmp_path)) == 40141
    assert _remembered_dock_attach_port() == 40141


def test_leftover_fill_child_python_inherited_persist_does_not_hide_sibling_chrome(
    tmp_path, monkeypatch,
):
    """Finding 202: leftover fill's leftover python hid sibling chrome.

    Leftover fill uniquely holds stale ``DevToolsActivePort``.
    Leftover python spawned by leftover fill inherited leftover
    persist with chrome. Finding 201 leftover-sibling-miss only
    walked leftover daemon's children, so leftover fill's leftover
    child hid sibling chrome. Persist / attach / skip-kill / Take
    over followed leftover DevTools; chrome pid became None
    (``owner`` None) so Take over tree-killed the Browser a
    human is typing into. One hop of leftover siblings' this-jar
    children is leftover fill's leftover child, not a leftover-
    holder grandparent walk. Leftover python that inherited both
    leftover persist and chrome's CDP stays 85. Leftover-only
    leftover fill plus leftover fill's leftover python leftover
    persist stays 86. Orphan leftover python stays 86. Attach
    must not stamp persist. ``remember()`` still does not update
    memory (finding 178). Identity candidate order stays
    file-then-memory (finding 169).
    """
    from hermes_constants import hermes_home_key
    from tools.browser_tool_session import (
        _last_dock_cdp_port,
        _remembered_dock_attach_port,
        _reset_dock_port_memory_for_tests,
    )

    leftover_fill = 4242
    leftover_cri = 4244
    leftover_py = 4246

    monkeypatch.setattr(runtime, "state_dir", lambda: tmp_path)
    monkeypatch.setattr(browser, "profile_dir", lambda: tmp_path)
    monkeypatch.setattr(browser, "_lock_pid", lambda d: None)
    monkeypatch.setattr(
        browser,
        "_chromium_cmdline_tokens",
        lambda pid: (
            ["chrome", "--type=zygote", f"--user-data-dir={tmp_path}"]
            if pid == 4241 else
            ["chrome", "--type=utility", f"--user-data-dir={tmp_path}"]
            if pid == 4245 else
            ["agent-browser", "daemon", f"--user-data-dir={tmp_path}"]
            if pid == 4300 else
            ["agent-browser", "fill", f"--user-data-dir={tmp_path}"]
            if pid == leftover_fill else
            ["agent-browser", "cri", f"--user-data-dir={tmp_path}"]
            if pid == leftover_cri else
            ["agent-browser", "python", f"--user-data-dir={tmp_path}"]
            if pid == leftover_py else
            ["chrome", f"--user-data-dir={tmp_path}"]
        ),
    )
    monkeypatch.setattr(
        browser,
        "_proc_ppid",
        lambda pid: {
            4240: 4300, 4241: 4240, 4245: 4241,
            leftover_fill: 4300, leftover_cri: 4300, leftover_py: leftover_fill,
        }.get(pid),
    )
    monkeypatch.setattr(
        browser,
        "_launched_by_session",
        lambda pid: "h_review" if pid == 4240 else None,
    )
    monkeypatch.setattr(browser, "_recover_cdp_port_from_singleton", lambda *a, **k: None)
    monkeypatch.setattr(
        browser, "_this_jar_children",
        lambda parent, user_data_dir: (
            {4240, leftover_fill, leftover_cri} if parent == 4300 else (
                {leftover_py} if parent == leftover_fill else set()
            )
        ),
    )

    def _inodes(port):
        if port == 40141:
            return {7: {"::1"}}
        if port == 9333:
            return {8: {"::1"}}
        if port == 18888:
            return {9: {"::1"}}
        return {}

    def _holders(want):
        out = {}
        if 7 in want:
            out[leftover_fill] = {7}
        if 8 in want:
            out[4240] = {8}
            out[4241] = {8}
            out[4245] = {8}
        if 9 in want:
            out[leftover_py] = {9}
            out[4240] = out.get(4240, set()) | {9}
        return out

    monkeypatch.setattr(browser, "_loopback_listen_inodes_for_port", _inodes)
    monkeypatch.setattr(browser, "_pids_holding_socket_inodes", _holders)
    monkeypatch.setattr(
        browser, "_loopback_listen_ports_for_pid",
        lambda pid: {40141} if pid == leftover_fill else (
            {18888} if pid == leftover_py else (
                {9333, 18888} if pid == 4240 else (
                    {9333} if pid in {4241, 4245} else set()
                )
            )
        ),
    )
    monkeypatch.setattr(
        browser, "_loopback_listen_targets_for_pid",
        lambda pid: {("::1", 40141)} if pid == leftover_fill else (
            {("::1", 18888)} if pid == leftover_py else (
                {("::1", 9333), ("::1", 18888)} if pid == 4240 else (
                    {("::1", 9333)} if pid in {4241, 4245} else set()
                )
            )
        ),
    )
    monkeypatch.setattr(
        browser,
        "_cdp_port_reachable",
        lambda port, hosts: port in (9333, 40141, 18888) and "::1" in hosts,
    )
    monkeypatch.setattr(browser, "_configured_cdp_override_url", lambda: "")
    (tmp_path / "DevToolsActivePort").write_text(
        "40141\n/devtools/browser/abc\n", encoding="utf-8",
    )

    _reset_dock_port_memory_for_tests()
    (tmp_path / "dock-cdp-port").write_text("18888\n", encoding="utf-8")
    assert browser._this_jar_holder_pids(40141, str(tmp_path)) == {leftover_fill}
    assert browser._this_jar_holder_pids(18888, str(tmp_path)) == {leftover_py, 4240}
    assert browser._this_jar_holder_pids(9333, str(tmp_path)) == {4240, 4241, 4245}
    assert browser._pid_is_chromium_browser(leftover_py) is False
    assert browser._proc_ppid(leftover_py) == leftover_fill
    assert browser.unique_lock_chrome_hidden_by_leftover_file(
        40141, str(tmp_path),
    ) == 9333
    assert browser.lock_listed_persist_port() == 9333
    assert browser._this_jar_chromium_pid(str(tmp_path)) == 4240
    assert browser.shared_chromium_owner_session() == "h_review"
    assert _remembered_dock_attach_port() == 9333
    assert (tmp_path / "dock-cdp-port").read_text(encoding="utf-8").strip() == "18888"
    assert browser.running_instance_cdp_port(str(tmp_path)) == 9333
    assert _remembered_dock_attach_port() == 9333
    assert browser.persist_live_dock_cdp_port() == 9333
    assert (tmp_path / "dock-cdp-port").read_text(encoding="utf-8") == "9333"
    assert _last_dock_cdp_port.get(hermes_home_key()) == 9333

    def _both(want):
        out = {}
        if 7 in want:
            out[leftover_fill] = {7}
        if 8 in want:
            out[4240] = {8}
            out[4241] = {8}
            out[4245] = {8}
            out[leftover_py] = {8}
        if 9 in want:
            out[leftover_py] = out.get(leftover_py, set()) | {9}
            out[4240] = out.get(4240, set()) | {9}
        return out

    monkeypatch.setattr(browser, "_pids_holding_socket_inodes", _both)
    monkeypatch.setattr(
        browser, "_loopback_listen_ports_for_pid",
        lambda pid: {40141} if pid == leftover_fill else (
            {9333, 18888} if pid == leftover_py else (
                {9333, 18888} if pid == 4240 else (
                    {9333} if pid in {4241, 4245} else set()
                )
            )
        ),
    )
    _reset_dock_port_memory_for_tests()
    (tmp_path / "dock-cdp-port").write_text("18888\n", encoding="utf-8")
    assert browser.unique_lock_chrome_hidden_by_leftover_file(
        40141, str(tmp_path),
    ) is None
    assert browser.lock_listed_persist_port() is None
    assert browser._this_jar_chromium_pid(str(tmp_path)) is None

    def _leftover_only(want):
        out = {}
        if 7 in want:
            out[leftover_fill] = {7}
        if 9 in want:
            out[leftover_py] = {9}
        return out

    monkeypatch.setattr(browser, "_pids_holding_socket_inodes", _leftover_only)
    monkeypatch.setattr(
        browser, "_this_jar_children",
        lambda parent, user_data_dir: (
            {leftover_fill, leftover_cri} if parent == 4300 else (
                {leftover_py} if parent == leftover_fill else set()
            )
        ),
    )
    monkeypatch.setattr(
        browser, "_loopback_listen_ports_for_pid",
        lambda pid: {40141} if pid == leftover_fill else (
            {18888} if pid == leftover_py else set()
        ),
    )
    monkeypatch.setattr(
        browser,
        "_cdp_port_reachable",
        lambda port, hosts: port in (40141, 18888) and "::1" in hosts,
    )
    _reset_dock_port_memory_for_tests()
    (tmp_path / "dock-cdp-port").write_text("18888\n", encoding="utf-8")
    assert browser.unique_lock_chrome_hidden_by_leftover_file(
        40141, str(tmp_path),
    ) is None
    assert browser.lock_listed_persist_port() is None
    assert browser._this_jar_chromium_pid(str(tmp_path)) is None
    assert browser.running_instance_cdp_port(str(tmp_path)) == 40141
    assert _remembered_dock_attach_port() == 40141


def test_leftover_fill_grandchild_python_inherited_persist_does_not_hide_sibling_chrome(
    tmp_path, monkeypatch,
):
    """Finding 203: leftover fill's leftover grandchild hid sibling chrome.

    Leftover fill uniquely holds stale ``DevToolsActivePort``.
    Leftover fill spawned leftover CRI which spawned leftover
    python that inherited leftover persist with chrome.
    Finding 202 leftover-siblings'-children hop is leftover
    CRI, not leftover python, so leftover-sibling-miss missed
    leftover fill's leftover grandchild. Persist / attach /
    skip-kill / Take over followed leftover DevTools; chrome
    pid became None (``owner`` None) so Take over tree-killed
    the Browser a human is typing into. One hop of leftover
    siblings' grandchildren is leftover fill's leftover
    grandchild, not a leftover-holder grandparent walk.
    Leftover python that inherited both leftover persist and
    chrome's CDP stays 85. Leftover-only leftover fill plus
    leftover fill's leftover grandchild leftover persist stays
    86. Leftover fill great-grandchild stays 86. Cousin chrome
    under leftover CRI stays 86. Attach must not stamp persist.
    ``remember()`` still does not update memory (finding 178).
    Identity candidate order stays file-then-memory (finding 169).
    """
    from hermes_constants import hermes_home_key
    from tools.browser_tool_session import (
        _last_dock_cdp_port,
        _remembered_dock_attach_port,
        _reset_dock_port_memory_for_tests,
    )

    leftover_fill = 4242
    leftover_cri = 4244
    leftover_py = 4246

    monkeypatch.setattr(runtime, "state_dir", lambda: tmp_path)
    monkeypatch.setattr(browser, "profile_dir", lambda: tmp_path)
    monkeypatch.setattr(browser, "_lock_pid", lambda d: None)
    monkeypatch.setattr(
        browser,
        "_chromium_cmdline_tokens",
        lambda pid: (
            ["chrome", "--type=zygote", f"--user-data-dir={tmp_path}"]
            if pid == 4241 else
            ["chrome", "--type=utility", f"--user-data-dir={tmp_path}"]
            if pid == 4245 else
            ["agent-browser", "daemon", f"--user-data-dir={tmp_path}"]
            if pid == 4300 else
            ["agent-browser", "fill", f"--user-data-dir={tmp_path}"]
            if pid == leftover_fill else
            ["agent-browser", "cri", f"--user-data-dir={tmp_path}"]
            if pid == leftover_cri else
            ["agent-browser", "python", f"--user-data-dir={tmp_path}"]
            if pid == leftover_py else
            ["chrome", f"--user-data-dir={tmp_path}"]
        ),
    )
    monkeypatch.setattr(
        browser,
        "_proc_ppid",
        lambda pid: {
            4240: 4300, 4241: 4240, 4245: 4241,
            leftover_fill: 4300, leftover_cri: leftover_fill, leftover_py: leftover_cri,
        }.get(pid),
    )
    monkeypatch.setattr(
        browser,
        "_launched_by_session",
        lambda pid: "h_review" if pid == 4240 else None,
    )
    monkeypatch.setattr(browser, "_recover_cdp_port_from_singleton", lambda *a, **k: None)
    monkeypatch.setattr(
        browser, "_this_jar_children",
        lambda parent, user_data_dir: (
            {4240, leftover_fill} if parent == 4300 else (
                {leftover_cri} if parent == leftover_fill else (
                    {leftover_py} if parent == leftover_cri else set()
                )
            )
        ),
    )

    def _inodes(port):
        if port == 40141:
            return {7: {"::1"}}
        if port == 9333:
            return {8: {"::1"}}
        if port == 18888:
            return {9: {"::1"}}
        return {}

    def _holders(want):
        out = {}
        if 7 in want:
            out[leftover_fill] = {7}
        if 8 in want:
            out[4240] = {8}
            out[4241] = {8}
            out[4245] = {8}
        if 9 in want:
            out[leftover_py] = {9}
            out[4240] = out.get(4240, set()) | {9}
        return out

    monkeypatch.setattr(browser, "_loopback_listen_inodes_for_port", _inodes)
    monkeypatch.setattr(browser, "_pids_holding_socket_inodes", _holders)
    monkeypatch.setattr(
        browser, "_loopback_listen_ports_for_pid",
        lambda pid: {40141} if pid == leftover_fill else (
            {18888} if pid == leftover_py else (
                {9333, 18888} if pid == 4240 else (
                    {9333} if pid in {4241, 4245} else set()
                )
            )
        ),
    )
    monkeypatch.setattr(
        browser, "_loopback_listen_targets_for_pid",
        lambda pid: {("::1", 40141)} if pid == leftover_fill else (
            {("::1", 18888)} if pid == leftover_py else (
                {("::1", 9333), ("::1", 18888)} if pid == 4240 else (
                    {("::1", 9333)} if pid in {4241, 4245} else set()
                )
            )
        ),
    )
    monkeypatch.setattr(
        browser,
        "_cdp_port_reachable",
        lambda port, hosts: port in (9333, 40141, 18888) and "::1" in hosts,
    )
    monkeypatch.setattr(browser, "_configured_cdp_override_url", lambda: "")
    (tmp_path / "DevToolsActivePort").write_text(
        "40141\n/devtools/browser/abc\n", encoding="utf-8",
    )

    _reset_dock_port_memory_for_tests()
    (tmp_path / "dock-cdp-port").write_text("18888\n", encoding="utf-8")
    assert browser._this_jar_holder_pids(40141, str(tmp_path)) == {leftover_fill}
    assert browser._this_jar_holder_pids(18888, str(tmp_path)) == {leftover_py, 4240}
    assert browser._this_jar_holder_pids(9333, str(tmp_path)) == {4240, 4241, 4245}
    assert browser._pid_is_chromium_browser(leftover_py) is False
    assert browser._proc_ppid(leftover_py) == leftover_cri
    assert browser._proc_ppid(leftover_cri) == leftover_fill
    assert 4240 in browser._this_jar_children(4300, str(tmp_path))
    assert leftover_cri not in browser._this_jar_children(4300, str(tmp_path))
    assert leftover_py not in browser._this_jar_children(4300, str(tmp_path))
    assert leftover_py not in browser._this_jar_children(leftover_fill, str(tmp_path))
    assert browser._this_jar_children(leftover_fill, str(tmp_path)) == {leftover_cri}
    assert browser._this_jar_children(leftover_cri, str(tmp_path)) == {leftover_py}
    assert browser.unique_lock_chrome_hidden_by_leftover_file(
        40141, str(tmp_path),
    ) == 9333
    assert browser.lock_listed_persist_port() == 9333
    assert browser._this_jar_chromium_pid(str(tmp_path)) == 4240
    assert browser.shared_chromium_owner_session() == "h_review"
    assert _remembered_dock_attach_port() == 9333
    assert (tmp_path / "dock-cdp-port").read_text(encoding="utf-8").strip() == "18888"
    assert browser.running_instance_cdp_port(str(tmp_path)) == 9333
    assert _remembered_dock_attach_port() == 9333
    assert browser.persist_live_dock_cdp_port() == 9333
    assert (tmp_path / "dock-cdp-port").read_text(encoding="utf-8") == "9333"
    assert _last_dock_cdp_port.get(hermes_home_key()) == 9333

    def _both(want):
        out = {}
        if 7 in want:
            out[leftover_fill] = {7}
        if 8 in want:
            out[4240] = {8}
            out[4241] = {8}
            out[4245] = {8}
            out[leftover_py] = {8}
        if 9 in want:
            out[leftover_py] = out.get(leftover_py, set()) | {9}
            out[4240] = out.get(4240, set()) | {9}
        return out

    monkeypatch.setattr(browser, "_pids_holding_socket_inodes", _both)
    monkeypatch.setattr(
        browser, "_loopback_listen_ports_for_pid",
        lambda pid: {40141} if pid == leftover_fill else (
            {9333, 18888} if pid == leftover_py else (
                {9333, 18888} if pid == 4240 else (
                    {9333} if pid in {4241, 4245} else set()
                )
            )
        ),
    )
    _reset_dock_port_memory_for_tests()
    (tmp_path / "dock-cdp-port").write_text("18888\n", encoding="utf-8")
    assert browser.unique_lock_chrome_hidden_by_leftover_file(
        40141, str(tmp_path),
    ) is None
    assert browser.lock_listed_persist_port() is None
    assert browser._this_jar_chromium_pid(str(tmp_path)) is None

    def _leftover_only(want):
        out = {}
        if 7 in want:
            out[leftover_fill] = {7}
        if 9 in want:
            out[leftover_py] = {9}
        return out

    monkeypatch.setattr(browser, "_pids_holding_socket_inodes", _leftover_only)
    monkeypatch.setattr(
        browser, "_this_jar_children",
        lambda parent, user_data_dir: (
            {leftover_fill} if parent == 4300 else (
                {leftover_cri} if parent == leftover_fill else (
                    {leftover_py} if parent == leftover_cri else set()
                )
            )
        ),
    )
    monkeypatch.setattr(
        browser, "_loopback_listen_ports_for_pid",
        lambda pid: {40141} if pid == leftover_fill else (
            {18888} if pid == leftover_py else set()
        ),
    )
    monkeypatch.setattr(
        browser,
        "_cdp_port_reachable",
        lambda port, hosts: port in (40141, 18888) and "::1" in hosts,
    )
    _reset_dock_port_memory_for_tests()
    (tmp_path / "dock-cdp-port").write_text("18888\n", encoding="utf-8")
    assert browser.unique_lock_chrome_hidden_by_leftover_file(
        40141, str(tmp_path),
    ) is None
    assert browser.lock_listed_persist_port() is None
    assert browser._this_jar_chromium_pid(str(tmp_path)) is None
    assert browser.running_instance_cdp_port(str(tmp_path)) == 40141
    assert _remembered_dock_attach_port() == 40141


def test_leftover_fill_and_cri_devtools_grandchild_persist_does_not_hide_sibling_chrome(
    tmp_path, monkeypatch,
):
    """Finding 204: leftover fill + leftover CRI leftover DevTools hid sibling chrome.

    Leftover fill and leftover fill's leftover CRI both hold stale
    ``DevToolsActivePort``. leftover CRI's leftover parent leftover
    fill is leftover file holder, so unique leftover-file parent
    was several and leftover-inherited never ran. Leftover python
    leftover CRI's leftover child inherited leftover persist with
    chrome. Persist / attach / skip-kill / Take over followed
    leftover DevTools; chrome pid became None (``owner`` None) so
    Take over tree-killed the Browser a human is typing into.
    Unique leftover parent among leftover holders' leftover parents
    that are not leftover file holders is leftover daemon, not a
    leftover-holder grandparent walk. Leftover python that inherited
    both leftover persist and chrome's CDP stays 85. Leftover-only
    leftover fill plus leftover CRI leftover DevTools plus leftover
    python leftover persist stays 86. Leftover CRI that does not
    name this jar stays 86. Leftover CRI inherited chrome CDP plus
    leftover python leftover persist stays 85. Attach must not
    stamp persist. ``remember()`` still does not update memory
    (finding 178). Identity candidate order stays file-then-memory
    (finding 169).
    """
    from hermes_constants import hermes_home_key
    from tools.browser_tool_session import (
        _last_dock_cdp_port,
        _remembered_dock_attach_port,
        _reset_dock_port_memory_for_tests,
    )

    leftover_fill = 4242
    leftover_cri = 4244
    leftover_py = 4246

    monkeypatch.setattr(runtime, "state_dir", lambda: tmp_path)
    monkeypatch.setattr(browser, "profile_dir", lambda: tmp_path)
    monkeypatch.setattr(browser, "_lock_pid", lambda d: None)
    monkeypatch.setattr(
        browser,
        "_chromium_cmdline_tokens",
        lambda pid: (
            ["chrome", "--type=zygote", f"--user-data-dir={tmp_path}"]
            if pid == 4241 else
            ["chrome", "--type=utility", f"--user-data-dir={tmp_path}"]
            if pid == 4245 else
            ["agent-browser", "daemon", f"--user-data-dir={tmp_path}"]
            if pid == 4300 else
            ["agent-browser", "fill", f"--user-data-dir={tmp_path}"]
            if pid == leftover_fill else
            ["agent-browser", "cri", f"--user-data-dir={tmp_path}"]
            if pid == leftover_cri else
            ["agent-browser", "python", f"--user-data-dir={tmp_path}"]
            if pid == leftover_py else
            ["chrome", f"--user-data-dir={tmp_path}"]
        ),
    )
    monkeypatch.setattr(
        browser,
        "_proc_ppid",
        lambda pid: {
            4240: 4300, 4241: 4240, 4245: 4241,
            leftover_fill: 4300, leftover_cri: leftover_fill, leftover_py: leftover_cri,
        }.get(pid),
    )
    monkeypatch.setattr(
        browser,
        "_launched_by_session",
        lambda pid: "h_review" if pid == 4240 else None,
    )
    monkeypatch.setattr(browser, "_recover_cdp_port_from_singleton", lambda *a, **k: None)
    monkeypatch.setattr(
        browser, "_this_jar_children",
        lambda parent, user_data_dir: (
            {4240, leftover_fill} if parent == 4300 else (
                {leftover_cri} if parent == leftover_fill else (
                    {leftover_py} if parent == leftover_cri else set()
                )
            )
        ),
    )

    def _inodes(port):
        if port == 40141:
            return {7: {"::1"}}
        if port == 9333:
            return {8: {"::1"}}
        if port == 18888:
            return {9: {"::1"}}
        return {}

    def _holders(want):
        out = {}
        if 7 in want:
            out[leftover_fill] = {7}
            out[leftover_cri] = {7}
        if 8 in want:
            out[4240] = {8}
            out[4241] = {8}
            out[4245] = {8}
        if 9 in want:
            out[leftover_py] = {9}
            out[4240] = out.get(4240, set()) | {9}
        return out

    monkeypatch.setattr(browser, "_loopback_listen_inodes_for_port", _inodes)
    monkeypatch.setattr(browser, "_pids_holding_socket_inodes", _holders)
    monkeypatch.setattr(
        browser, "_loopback_listen_ports_for_pid",
        lambda pid: {40141} if pid in {leftover_fill, leftover_cri} else (
            {18888} if pid == leftover_py else (
                {9333, 18888} if pid == 4240 else (
                    {9333} if pid in {4241, 4245} else set()
                )
            )
        ),
    )
    monkeypatch.setattr(
        browser, "_loopback_listen_targets_for_pid",
        lambda pid: {("::1", 40141)} if pid in {leftover_fill, leftover_cri} else (
            {("::1", 18888)} if pid == leftover_py else (
                {("::1", 9333), ("::1", 18888)} if pid == 4240 else (
                    {("::1", 9333)} if pid in {4241, 4245} else set()
                )
            )
        ),
    )
    monkeypatch.setattr(
        browser,
        "_cdp_port_reachable",
        lambda port, hosts: port in (9333, 40141, 18888) and "::1" in hosts,
    )
    monkeypatch.setattr(browser, "_configured_cdp_override_url", lambda: "")
    (tmp_path / "DevToolsActivePort").write_text(
        "40141\n/devtools/browser/abc\n", encoding="utf-8",
    )

    _reset_dock_port_memory_for_tests()
    (tmp_path / "dock-cdp-port").write_text("18888\n", encoding="utf-8")
    assert browser._this_jar_holder_pids(40141, str(tmp_path)) == {
        leftover_fill, leftover_cri,
    }
    assert browser._this_jar_holder_pids(18888, str(tmp_path)) == {leftover_py, 4240}
    assert browser._this_jar_holder_pids(9333, str(tmp_path)) == {4240, 4241, 4245}
    assert browser._pid_is_chromium_browser(leftover_cri) is False
    assert browser._proc_ppid(leftover_cri) == leftover_fill
    assert browser._proc_ppid(leftover_py) == leftover_cri
    assert browser._unique_this_jar_parent(
        {leftover_fill, leftover_cri}, str(tmp_path),
    ) == 4300
    assert browser.unique_lock_chrome_hidden_by_leftover_file(
        40141, str(tmp_path),
    ) == 9333
    assert browser.lock_listed_persist_port() == 9333
    assert browser._this_jar_chromium_pid(str(tmp_path)) == 4240
    assert browser.shared_chromium_owner_session() == "h_review"
    assert _remembered_dock_attach_port() == 9333
    assert (tmp_path / "dock-cdp-port").read_text(encoding="utf-8").strip() == "18888"
    assert browser.running_instance_cdp_port(str(tmp_path)) == 9333
    assert _remembered_dock_attach_port() == 9333
    assert browser.persist_live_dock_cdp_port() == 9333
    assert (tmp_path / "dock-cdp-port").read_text(encoding="utf-8") == "9333"
    assert _last_dock_cdp_port.get(hermes_home_key()) == 9333

    def _both(want):
        out = {}
        if 7 in want:
            out[leftover_fill] = {7}
            out[leftover_cri] = {7}
        if 8 in want:
            out[4240] = {8}
            out[4241] = {8}
            out[4245] = {8}
            out[leftover_py] = {8}
        if 9 in want:
            out[leftover_py] = out.get(leftover_py, set()) | {9}
            out[4240] = out.get(4240, set()) | {9}
        return out

    monkeypatch.setattr(browser, "_pids_holding_socket_inodes", _both)
    monkeypatch.setattr(
        browser, "_loopback_listen_ports_for_pid",
        lambda pid: {40141} if pid in {leftover_fill, leftover_cri} else (
            {9333, 18888} if pid == leftover_py else (
                {9333, 18888} if pid == 4240 else (
                    {9333} if pid in {4241, 4245} else set()
                )
            )
        ),
    )
    _reset_dock_port_memory_for_tests()
    (tmp_path / "dock-cdp-port").write_text("18888\n", encoding="utf-8")
    assert browser.unique_lock_chrome_hidden_by_leftover_file(
        40141, str(tmp_path),
    ) is None
    assert browser.lock_listed_persist_port() is None
    assert browser._this_jar_chromium_pid(str(tmp_path)) is None

    def _leftover_only(want):
        out = {}
        if 7 in want:
            out[leftover_fill] = {7}
            out[leftover_cri] = {7}
        if 9 in want:
            out[leftover_py] = {9}
            out[leftover_cri] = out.get(leftover_cri, set()) | {9}
        return out

    monkeypatch.setattr(browser, "_pids_holding_socket_inodes", _leftover_only)
    monkeypatch.setattr(
        browser, "_this_jar_children",
        lambda parent, user_data_dir: (
            {leftover_fill} if parent == 4300 else (
                {leftover_cri} if parent == leftover_fill else (
                    {leftover_py} if parent == leftover_cri else set()
                )
            )
        ),
    )
    monkeypatch.setattr(
        browser, "_loopback_listen_ports_for_pid",
        lambda pid: {40141, 18888} if pid == leftover_cri else (
            {40141} if pid == leftover_fill else (
                {18888} if pid == leftover_py else set()
            )
        ),
    )
    monkeypatch.setattr(
        browser,
        "_cdp_port_reachable",
        lambda port, hosts: port in (40141, 18888) and "::1" in hosts,
    )
    _reset_dock_port_memory_for_tests()
    (tmp_path / "dock-cdp-port").write_text("18888\n", encoding="utf-8")
    assert browser.unique_lock_chrome_hidden_by_leftover_file(
        40141, str(tmp_path),
    ) is None
    assert browser.lock_listed_persist_port() is None
    assert browser._this_jar_chromium_pid(str(tmp_path)) is None
    assert browser.running_instance_cdp_port(str(tmp_path)) == 40141
    assert _remembered_dock_attach_port() == 40141


def test_leftover_daemon_fill_and_cri_devtools_grandchild_persist_does_not_hide_sibling_chrome(
    tmp_path, monkeypatch,
):
    """Finding 205: leftover daemon + leftover fill + leftover CRI leftover DevTools hid sibling chrome.

    Leftover daemon, leftover fill, and leftover fill's leftover CRI
    all hold stale ``DevToolsActivePort``. leftover fill's leftover
    parent leftover daemon is leftover file holder, so leftover-
    outside is empty and leftover-inherited never ran. Leftover
    python leftover CRI's leftover child inherited leftover persist
    with chrome. Persist / attach / skip-kill / Take over followed
    leftover DevTools; chrome pid became None (``owner`` None) so
    Take over tree-killed the Browser a human is typing into.
    Unique leftover parent among leftover holders' leftover parents
    whose leftover parent is not a leftover file holder is leftover
    daemon, not a leftover-holder grandparent walk. Leftover python
    that inherited both leftover persist and chrome's CDP stays 85.
    Leftover-only leftover daemon plus leftover fill plus leftover
    CRI leftover DevTools plus leftover python leftover persist
    leftover-shared stays 86. Leftover CRI that does not name this
    jar stays 86. Leftover fill great-grandchild leftover persist
    leftover-shared stays 86. Attach must not stamp persist.
    ``remember()`` still does not update memory (finding 178).
    Identity candidate order stays file-then-memory (finding 169).
    """
    from hermes_constants import hermes_home_key
    from tools.browser_tool_session import (
        _last_dock_cdp_port,
        _remembered_dock_attach_port,
        _reset_dock_port_memory_for_tests,
    )

    leftover_fill = 4242
    leftover_cri = 4244
    leftover_py = 4246

    monkeypatch.setattr(runtime, "state_dir", lambda: tmp_path)
    monkeypatch.setattr(browser, "profile_dir", lambda: tmp_path)
    monkeypatch.setattr(browser, "_lock_pid", lambda d: None)
    monkeypatch.setattr(
        browser,
        "_chromium_cmdline_tokens",
        lambda pid: (
            ["chrome", "--type=zygote", f"--user-data-dir={tmp_path}"]
            if pid == 4241 else
            ["chrome", "--type=utility", f"--user-data-dir={tmp_path}"]
            if pid == 4245 else
            ["agent-browser", "daemon", f"--user-data-dir={tmp_path}"]
            if pid == 4300 else
            ["agent-browser", "fill", f"--user-data-dir={tmp_path}"]
            if pid == leftover_fill else
            ["agent-browser", "cri", f"--user-data-dir={tmp_path}"]
            if pid == leftover_cri else
            ["agent-browser", "python", f"--user-data-dir={tmp_path}"]
            if pid == leftover_py else
            ["chrome", f"--user-data-dir={tmp_path}"]
        ),
    )
    monkeypatch.setattr(
        browser,
        "_proc_ppid",
        lambda pid: {
            4240: 4300, 4241: 4240, 4245: 4241,
            leftover_fill: 4300, leftover_cri: leftover_fill, leftover_py: leftover_cri,
        }.get(pid),
    )
    monkeypatch.setattr(
        browser,
        "_launched_by_session",
        lambda pid: "h_review" if pid == 4240 else None,
    )
    monkeypatch.setattr(browser, "_recover_cdp_port_from_singleton", lambda *a, **k: None)
    monkeypatch.setattr(
        browser, "_this_jar_children",
        lambda parent, user_data_dir: (
            {4240, leftover_fill} if parent == 4300 else (
                {leftover_cri} if parent == leftover_fill else (
                    {leftover_py} if parent == leftover_cri else set()
                )
            )
        ),
    )

    def _inodes(port):
        if port == 40141:
            return {7: {"::1"}}
        if port == 9333:
            return {8: {"::1"}}
        if port == 18888:
            return {9: {"::1"}}
        return {}

    def _holders(want):
        out = {}
        if 7 in want:
            out[4300] = {7}
            out[leftover_fill] = {7}
            out[leftover_cri] = {7}
        if 8 in want:
            out[4240] = {8}
            out[4241] = {8}
            out[4245] = {8}
        if 9 in want:
            out[leftover_py] = {9}
            out[4240] = out.get(4240, set()) | {9}
        return out

    monkeypatch.setattr(browser, "_loopback_listen_inodes_for_port", _inodes)
    monkeypatch.setattr(browser, "_pids_holding_socket_inodes", _holders)
    monkeypatch.setattr(
        browser, "_loopback_listen_ports_for_pid",
        lambda pid: {40141} if pid in {4300, leftover_fill, leftover_cri} else (
            {18888} if pid == leftover_py else (
                {9333, 18888} if pid == 4240 else (
                    {9333} if pid in {4241, 4245} else set()
                )
            )
        ),
    )
    monkeypatch.setattr(
        browser, "_loopback_listen_targets_for_pid",
        lambda pid: {("::1", 40141)} if pid in {4300, leftover_fill, leftover_cri} else (
            {("::1", 18888)} if pid == leftover_py else (
                {("::1", 9333), ("::1", 18888)} if pid == 4240 else (
                    {("::1", 9333)} if pid in {4241, 4245} else set()
                )
            )
        ),
    )
    monkeypatch.setattr(
        browser,
        "_cdp_port_reachable",
        lambda port, hosts: port in (9333, 40141, 18888) and "::1" in hosts,
    )
    monkeypatch.setattr(browser, "_configured_cdp_override_url", lambda: "")
    (tmp_path / "DevToolsActivePort").write_text(
        "40141\n/devtools/browser/abc\n", encoding="utf-8",
    )

    _reset_dock_port_memory_for_tests()
    (tmp_path / "dock-cdp-port").write_text("18888\n", encoding="utf-8")
    assert browser._this_jar_holder_pids(40141, str(tmp_path)) == {
        4300, leftover_fill, leftover_cri,
    }
    assert browser._this_jar_holder_pids(18888, str(tmp_path)) == {leftover_py, 4240}
    assert browser._this_jar_holder_pids(9333, str(tmp_path)) == {4240, 4241, 4245}
    assert browser._pid_is_chromium_browser(4300) is False
    assert browser._proc_ppid(leftover_fill) == 4300
    assert browser._proc_ppid(leftover_cri) == leftover_fill
    assert browser._unique_this_jar_parent(
        {4300, leftover_fill, leftover_cri}, str(tmp_path),
    ) == 4300
    assert browser.unique_lock_chrome_hidden_by_leftover_file(
        40141, str(tmp_path),
    ) == 9333
    assert browser.lock_listed_persist_port() == 9333
    assert browser._this_jar_chromium_pid(str(tmp_path)) == 4240
    assert browser.shared_chromium_owner_session() == "h_review"
    assert _remembered_dock_attach_port() == 9333
    assert (tmp_path / "dock-cdp-port").read_text(encoding="utf-8").strip() == "18888"
    assert browser.running_instance_cdp_port(str(tmp_path)) == 9333
    assert _remembered_dock_attach_port() == 9333
    assert browser.persist_live_dock_cdp_port() == 9333
    assert (tmp_path / "dock-cdp-port").read_text(encoding="utf-8") == "9333"
    assert _last_dock_cdp_port.get(hermes_home_key()) == 9333

    def _both(want):
        out = {}
        if 7 in want:
            out[4300] = {7}
            out[leftover_fill] = {7}
            out[leftover_cri] = {7}
        if 8 in want:
            out[4240] = {8}
            out[4241] = {8}
            out[4245] = {8}
            out[leftover_py] = {8}
        if 9 in want:
            out[leftover_py] = out.get(leftover_py, set()) | {9}
            out[4240] = out.get(4240, set()) | {9}
        return out

    monkeypatch.setattr(browser, "_pids_holding_socket_inodes", _both)
    monkeypatch.setattr(
        browser, "_loopback_listen_ports_for_pid",
        lambda pid: {40141} if pid in {4300, leftover_fill, leftover_cri} else (
            {9333, 18888} if pid == leftover_py else (
                {9333, 18888} if pid == 4240 else (
                    {9333} if pid in {4241, 4245} else set()
                )
            )
        ),
    )
    _reset_dock_port_memory_for_tests()
    (tmp_path / "dock-cdp-port").write_text("18888\n", encoding="utf-8")
    assert browser.unique_lock_chrome_hidden_by_leftover_file(
        40141, str(tmp_path),
    ) is None
    assert browser.lock_listed_persist_port() is None
    assert browser._this_jar_chromium_pid(str(tmp_path)) is None

    def _leftover_only(want):
        out = {}
        if 7 in want:
            out[4300] = {7}
            out[leftover_fill] = {7}
            out[leftover_cri] = {7}
        if 9 in want:
            out[leftover_py] = {9}
            out[leftover_cri] = out.get(leftover_cri, set()) | {9}
        return out

    monkeypatch.setattr(browser, "_pids_holding_socket_inodes", _leftover_only)
    monkeypatch.setattr(
        browser, "_this_jar_children",
        lambda parent, user_data_dir: (
            {leftover_fill} if parent == 4300 else (
                {leftover_cri} if parent == leftover_fill else (
                    {leftover_py} if parent == leftover_cri else set()
                )
            )
        ),
    )
    monkeypatch.setattr(
        browser, "_loopback_listen_ports_for_pid",
        lambda pid: {40141, 18888} if pid == leftover_cri else (
            {40141} if pid in {4300, leftover_fill} else (
                {18888} if pid == leftover_py else set()
            )
        ),
    )
    monkeypatch.setattr(
        browser,
        "_cdp_port_reachable",
        lambda port, hosts: port in (40141, 18888) and "::1" in hosts,
    )
    _reset_dock_port_memory_for_tests()
    (tmp_path / "dock-cdp-port").write_text("18888\n", encoding="utf-8")
    assert browser._unique_this_jar_parent(
        {4300, leftover_fill, leftover_cri}, str(tmp_path),
    ) == 4300
    assert browser.unique_lock_chrome_hidden_by_leftover_file(
        40141, str(tmp_path),
    ) is None
    assert browser.lock_listed_persist_port() is None
    assert browser._this_jar_chromium_pid(str(tmp_path)) is None
    assert browser.running_instance_cdp_port(str(tmp_path)) == 40141
    assert _remembered_dock_attach_port() == 40141


def test_leftover_supervisor_parent_devtools_grandchild_persist_does_not_hide_sibling_chrome(
    tmp_path, monkeypatch,
):
    """Finding 206: leftover daemon leftover parent leftover supervisor hid sibling chrome.

    leftover daemon leftover parent leftover supervisor names this
    jar. leftover daemon, leftover fill, and leftover CRI leftover
    DevTools. leftover-outside leftover supervisor. leftover-
    inherited leftover supervisor leftover children leftover daemon
    missed leftover daemon leftover children (chrome). Leftover
    python leftover CRI's leftover child inherited leftover persist
    with chrome. Persist / attach / skip-kill / Take over followed
    leftover DevTools; chrome pid became None (``owner`` None) so
    Take over tree-killed the Browser a human is typing into.
    One hop of leftover unique leftover parent's leftover children
    leftover children is leftover daemon leftover children, not a
    leftover-holder leftover grandparent walk of leftover fill.
    Leftover python that inherited both leftover persist and
    chrome's CDP stays 85. Leftover-only leftover daemon plus
    leftover fill plus leftover CRI leftover DevTools plus leftover
    python leftover persist leftover-shared stays 86. Leftover fill
    great-grandchild leftover persist leftover-shared stays 86.
    Cousin chrome under leftover CRI stays 86. Attach must not
    stamp persist. ``remember()`` still does not update memory
    (finding 178). Identity candidate order stays file-then-memory
    (finding 169).
    """
    from hermes_constants import hermes_home_key
    from tools.browser_tool_session import (
        _last_dock_cdp_port,
        _remembered_dock_attach_port,
        _reset_dock_port_memory_for_tests,
    )

    leftover_fill = 4242
    leftover_cri = 4244
    leftover_py = 4246
    leftover_sup = 4290

    monkeypatch.setattr(runtime, "state_dir", lambda: tmp_path)
    monkeypatch.setattr(browser, "profile_dir", lambda: tmp_path)
    monkeypatch.setattr(browser, "_lock_pid", lambda d: None)
    monkeypatch.setattr(
        browser,
        "_chromium_cmdline_tokens",
        lambda pid: (
            ["chrome", "--type=zygote", f"--user-data-dir={tmp_path}"]
            if pid == 4241 else
            ["chrome", "--type=utility", f"--user-data-dir={tmp_path}"]
            if pid == 4245 else
            ["agent-browser", "supervisor", f"--user-data-dir={tmp_path}"]
            if pid == leftover_sup else
            ["agent-browser", "daemon", f"--user-data-dir={tmp_path}"]
            if pid == 4300 else
            ["agent-browser", "fill", f"--user-data-dir={tmp_path}"]
            if pid == leftover_fill else
            ["agent-browser", "cri", f"--user-data-dir={tmp_path}"]
            if pid == leftover_cri else
            ["agent-browser", "python", f"--user-data-dir={tmp_path}"]
            if pid == leftover_py else
            ["chrome", f"--user-data-dir={tmp_path}"]
        ),
    )
    monkeypatch.setattr(
        browser,
        "_proc_ppid",
        lambda pid: {
            4240: 4300, 4241: 4240, 4245: 4241, 4300: leftover_sup,
            leftover_fill: 4300, leftover_cri: leftover_fill, leftover_py: leftover_cri,
        }.get(pid),
    )
    monkeypatch.setattr(
        browser,
        "_launched_by_session",
        lambda pid: "h_review" if pid == 4240 else None,
    )
    monkeypatch.setattr(browser, "_recover_cdp_port_from_singleton", lambda *a, **k: None)
    monkeypatch.setattr(
        browser, "_this_jar_children",
        lambda parent, user_data_dir: (
            {4300} if parent == leftover_sup else (
                {4240, leftover_fill} if parent == 4300 else (
                    {leftover_cri} if parent == leftover_fill else (
                        {leftover_py} if parent == leftover_cri else set()
                    )
                )
            )
        ),
    )

    def _inodes(port):
        if port == 40141:
            return {7: {"::1"}}
        if port == 9333:
            return {8: {"::1"}}
        if port == 18888:
            return {9: {"::1"}}
        return {}

    def _holders(want):
        out = {}
        if 7 in want:
            out[4300] = {7}
            out[leftover_fill] = {7}
            out[leftover_cri] = {7}
        if 8 in want:
            out[4240] = {8}
            out[4241] = {8}
            out[4245] = {8}
        if 9 in want:
            out[leftover_py] = {9}
            out[4240] = out.get(4240, set()) | {9}
        return out

    monkeypatch.setattr(browser, "_loopback_listen_inodes_for_port", _inodes)
    monkeypatch.setattr(browser, "_pids_holding_socket_inodes", _holders)
    monkeypatch.setattr(
        browser, "_loopback_listen_ports_for_pid",
        lambda pid: {40141} if pid in {4300, leftover_fill, leftover_cri} else (
            {18888} if pid == leftover_py else (
                {9333, 18888} if pid == 4240 else (
                    {9333} if pid in {4241, 4245} else set()
                )
            )
        ),
    )
    monkeypatch.setattr(
        browser, "_loopback_listen_targets_for_pid",
        lambda pid: {("::1", 40141)} if pid in {4300, leftover_fill, leftover_cri} else (
            {("::1", 18888)} if pid == leftover_py else (
                {("::1", 9333), ("::1", 18888)} if pid == 4240 else (
                    {("::1", 9333)} if pid in {4241, 4245} else set()
                )
            )
        ),
    )
    monkeypatch.setattr(
        browser,
        "_cdp_port_reachable",
        lambda port, hosts: port in (9333, 40141, 18888) and "::1" in hosts,
    )
    monkeypatch.setattr(browser, "_configured_cdp_override_url", lambda: "")
    (tmp_path / "DevToolsActivePort").write_text(
        "40141\n/devtools/browser/abc\n", encoding="utf-8",
    )

    _reset_dock_port_memory_for_tests()
    (tmp_path / "dock-cdp-port").write_text("18888\n", encoding="utf-8")
    assert browser._this_jar_holder_pids(40141, str(tmp_path)) == {
        4300, leftover_fill, leftover_cri,
    }
    assert browser._this_jar_holder_pids(18888, str(tmp_path)) == {leftover_py, 4240}
    assert browser._this_jar_holder_pids(9333, str(tmp_path)) == {4240, 4241, 4245}
    assert browser._pid_is_chromium_browser(leftover_sup) is False
    assert browser._pid_is_chromium_browser(4300) is False
    assert browser._proc_ppid(4300) == leftover_sup
    assert browser._unique_this_jar_parent(
        {4300, leftover_fill, leftover_cri}, str(tmp_path),
    ) == leftover_sup
    assert browser.unique_lock_chrome_hidden_by_leftover_file(
        40141, str(tmp_path),
    ) == 9333
    assert browser.lock_listed_persist_port() == 9333
    assert browser._this_jar_chromium_pid(str(tmp_path)) == 4240
    assert browser.shared_chromium_owner_session() == "h_review"
    assert _remembered_dock_attach_port() == 9333
    assert (tmp_path / "dock-cdp-port").read_text(encoding="utf-8").strip() == "18888"
    assert browser.running_instance_cdp_port(str(tmp_path)) == 9333
    assert _remembered_dock_attach_port() == 9333
    assert browser.persist_live_dock_cdp_port() == 9333
    assert (tmp_path / "dock-cdp-port").read_text(encoding="utf-8") == "9333"
    assert _last_dock_cdp_port.get(hermes_home_key()) == 9333

    def _both(want):
        out = {}
        if 7 in want:
            out[4300] = {7}
            out[leftover_fill] = {7}
            out[leftover_cri] = {7}
        if 8 in want:
            out[4240] = {8}
            out[4241] = {8}
            out[4245] = {8}
            out[leftover_py] = {8}
        if 9 in want:
            out[leftover_py] = out.get(leftover_py, set()) | {9}
            out[4240] = out.get(4240, set()) | {9}
        return out

    monkeypatch.setattr(browser, "_pids_holding_socket_inodes", _both)
    monkeypatch.setattr(
        browser, "_loopback_listen_ports_for_pid",
        lambda pid: {40141} if pid in {4300, leftover_fill, leftover_cri} else (
            {9333, 18888} if pid == leftover_py else (
                {9333, 18888} if pid == 4240 else (
                    {9333} if pid in {4241, 4245} else set()
                )
            )
        ),
    )
    _reset_dock_port_memory_for_tests()
    (tmp_path / "dock-cdp-port").write_text("18888\n", encoding="utf-8")
    assert browser.unique_lock_chrome_hidden_by_leftover_file(
        40141, str(tmp_path),
    ) is None
    assert browser.lock_listed_persist_port() is None
    assert browser._this_jar_chromium_pid(str(tmp_path)) is None

    def _leftover_only(want):
        out = {}
        if 7 in want:
            out[4300] = {7}
            out[leftover_fill] = {7}
            out[leftover_cri] = {7}
        if 9 in want:
            out[leftover_py] = {9}
            out[leftover_cri] = out.get(leftover_cri, set()) | {9}
        return out

    monkeypatch.setattr(browser, "_pids_holding_socket_inodes", _leftover_only)
    monkeypatch.setattr(
        browser, "_this_jar_children",
        lambda parent, user_data_dir: (
            {4300} if parent == leftover_sup else (
                {leftover_fill} if parent == 4300 else (
                    {leftover_cri} if parent == leftover_fill else (
                        {leftover_py} if parent == leftover_cri else set()
                    )
                )
            )
        ),
    )
    monkeypatch.setattr(
        browser, "_loopback_listen_ports_for_pid",
        lambda pid: {40141, 18888} if pid == leftover_cri else (
            {40141} if pid in {4300, leftover_fill} else (
                {18888} if pid == leftover_py else set()
            )
        ),
    )
    monkeypatch.setattr(
        browser,
        "_cdp_port_reachable",
        lambda port, hosts: port in (40141, 18888) and "::1" in hosts,
    )
    _reset_dock_port_memory_for_tests()
    (tmp_path / "dock-cdp-port").write_text("18888\n", encoding="utf-8")
    assert browser._unique_this_jar_parent(
        {4300, leftover_fill, leftover_cri}, str(tmp_path),
    ) == leftover_sup
    assert browser.unique_lock_chrome_hidden_by_leftover_file(
        40141, str(tmp_path),
    ) is None
    assert browser.lock_listed_persist_port() is None
    assert browser._this_jar_chromium_pid(str(tmp_path)) is None
    assert browser.running_instance_cdp_port(str(tmp_path)) == 40141
    assert _remembered_dock_attach_port() == 40141


def test_leftover_supervisor_root_devtools_great_grandchild_persist_does_not_hide_sibling_chrome(
    tmp_path, monkeypatch,
):
    """Finding 207: leftover daemon leftover parent leftover supervisor hid sibling chrome.

    leftover supervisor leftover parent leftover supervisor names this
    jar and leftover supervisor leftover DevTools. leftover daemon, leftover fill, and leftover CRI leftover
    DevTools. leftover-outside leftover supervisor. leftover-
    inherited leftover supervisor leftover children leftover daemon
    missed leftover daemon leftover children (chrome). Leftover
    python leftover CRI's leftover child inherited leftover persist
    with chrome. Persist / attach / skip-kill / Take over followed
    leftover DevTools; chrome pid became None (``owner`` None) so
    Take over tree-killed the Browser a human is typing into.
    One hop of leftover unique leftover parent's leftover children
    leftover children is leftover daemon leftover children, not a
    leftover-holder leftover grandparent walk of leftover fill.
    Leftover python that inherited both leftover persist and
    chrome's CDP stays 85. Leftover-only leftover daemon plus
    leftover fill plus leftover CRI leftover DevTools plus leftover
    python leftover persist leftover-shared stays 86. Leftover fill
    great-grandchild leftover persist leftover-shared stays 86.
    Cousin chrome under leftover CRI stays 86. Attach must not
    stamp persist. ``remember()`` still does not update memory
    (finding 178). Identity candidate order stays file-then-memory
    (finding 169).
    """
    from hermes_constants import hermes_home_key
    from tools.browser_tool_session import (
        _last_dock_cdp_port,
        _remembered_dock_attach_port,
        _reset_dock_port_memory_for_tests,
    )

    leftover_fill = 4242
    leftover_cri = 4244
    leftover_py = 4246
    leftover_sup = 4290
    leftover_root = 4280

    monkeypatch.setattr(runtime, "state_dir", lambda: tmp_path)
    monkeypatch.setattr(browser, "profile_dir", lambda: tmp_path)
    monkeypatch.setattr(browser, "_lock_pid", lambda d: None)
    monkeypatch.setattr(
        browser,
        "_chromium_cmdline_tokens",
        lambda pid: (
            ["chrome", "--type=zygote", f"--user-data-dir={tmp_path}"]
            if pid == 4241 else
            ["chrome", "--type=utility", f"--user-data-dir={tmp_path}"]
            if pid == 4245 else
            ["agent-browser", "supervisor", f"--user-data-dir={tmp_path}"]
            if pid == leftover_root else
            ["agent-browser", "supervisor", f"--user-data-dir={tmp_path}"]
            if pid == leftover_sup else
            ["agent-browser", "daemon", f"--user-data-dir={tmp_path}"]
            if pid == 4300 else
            ["agent-browser", "fill", f"--user-data-dir={tmp_path}"]
            if pid == leftover_fill else
            ["agent-browser", "cri", f"--user-data-dir={tmp_path}"]
            if pid == leftover_cri else
            ["agent-browser", "python", f"--user-data-dir={tmp_path}"]
            if pid == leftover_py else
            ["chrome", f"--user-data-dir={tmp_path}"]
        ),
    )
    monkeypatch.setattr(
        browser,
        "_proc_ppid",
        lambda pid: {
            4240: 4300, 4241: 4240, 4245: 4241, leftover_sup: leftover_root, 4300: leftover_sup,
            leftover_fill: 4300, leftover_cri: leftover_fill, leftover_py: leftover_cri,
        }.get(pid),
    )
    monkeypatch.setattr(
        browser,
        "_launched_by_session",
        lambda pid: "h_review" if pid == 4240 else None,
    )
    monkeypatch.setattr(browser, "_recover_cdp_port_from_singleton", lambda *a, **k: None)
    monkeypatch.setattr(
        browser, "_this_jar_children",
        lambda parent, user_data_dir: (
            {leftover_sup} if parent == leftover_root else (
                {4300} if parent == leftover_sup else (
                    {4240, leftover_fill} if parent == 4300 else (
                        {leftover_cri} if parent == leftover_fill else (
                            {leftover_py} if parent == leftover_cri else set()
                        )
                    )
                )
            )
        ),
    )

    def _inodes(port):
        if port == 40141:
            return {7: {"::1"}}
        if port == 9333:
            return {8: {"::1"}}
        if port == 18888:
            return {9: {"::1"}}
        return {}

    def _holders(want):
        out = {}
        if 7 in want:
            out[leftover_sup] = {7}
            out[4300] = {7}
            out[leftover_fill] = {7}
            out[leftover_cri] = {7}
        if 8 in want:
            out[4240] = {8}
            out[4241] = {8}
            out[4245] = {8}
        if 9 in want:
            out[leftover_py] = {9}
            out[4240] = out.get(4240, set()) | {9}
        return out

    monkeypatch.setattr(browser, "_loopback_listen_inodes_for_port", _inodes)
    monkeypatch.setattr(browser, "_pids_holding_socket_inodes", _holders)
    monkeypatch.setattr(
        browser, "_loopback_listen_ports_for_pid",
        lambda pid: {40141} if pid in {leftover_sup, 4300, leftover_fill, leftover_cri} else (
            {18888} if pid == leftover_py else (
                {9333, 18888} if pid == 4240 else (
                    {9333} if pid in {4241, 4245} else set()
                )
            )
        ),
    )
    monkeypatch.setattr(
        browser, "_loopback_listen_targets_for_pid",
        lambda pid: {("::1", 40141)} if pid in {leftover_sup, 4300, leftover_fill, leftover_cri} else (
            {("::1", 18888)} if pid == leftover_py else (
                {("::1", 9333), ("::1", 18888)} if pid == 4240 else (
                    {("::1", 9333)} if pid in {4241, 4245} else set()
                )
            )
        ),
    )
    monkeypatch.setattr(
        browser,
        "_cdp_port_reachable",
        lambda port, hosts: port in (9333, 40141, 18888) and "::1" in hosts,
    )
    monkeypatch.setattr(browser, "_configured_cdp_override_url", lambda: "")
    (tmp_path / "DevToolsActivePort").write_text(
        "40141\n/devtools/browser/abc\n", encoding="utf-8",
    )

    _reset_dock_port_memory_for_tests()
    (tmp_path / "dock-cdp-port").write_text("18888\n", encoding="utf-8")
    assert browser._this_jar_holder_pids(40141, str(tmp_path)) == {
        leftover_sup, 4300, leftover_fill, leftover_cri,
    }
    assert browser._this_jar_holder_pids(18888, str(tmp_path)) == {leftover_py, 4240}
    assert browser._this_jar_holder_pids(9333, str(tmp_path)) == {4240, 4241, 4245}
    assert browser._pid_is_chromium_browser(leftover_root) is False
    assert browser._pid_is_chromium_browser(leftover_sup) is False
    assert browser._pid_is_chromium_browser(4300) is False
    assert browser._proc_ppid(leftover_sup) == leftover_root
    assert browser._proc_ppid(4300) == leftover_sup
    assert browser._unique_this_jar_parent(
        {leftover_sup, 4300, leftover_fill, leftover_cri}, str(tmp_path),
    ) == leftover_root
    assert browser.unique_lock_chrome_hidden_by_leftover_file(
        40141, str(tmp_path),
    ) == 9333
    assert browser.lock_listed_persist_port() == 9333
    assert browser._this_jar_chromium_pid(str(tmp_path)) == 4240
    assert browser.shared_chromium_owner_session() == "h_review"
    assert _remembered_dock_attach_port() == 9333
    assert (tmp_path / "dock-cdp-port").read_text(encoding="utf-8").strip() == "18888"
    assert browser.running_instance_cdp_port(str(tmp_path)) == 9333
    assert _remembered_dock_attach_port() == 9333
    assert browser.persist_live_dock_cdp_port() == 9333
    assert (tmp_path / "dock-cdp-port").read_text(encoding="utf-8") == "9333"
    assert _last_dock_cdp_port.get(hermes_home_key()) == 9333

    def _both(want):
        out = {}
        if 7 in want:
            out[leftover_sup] = {7}
            out[4300] = {7}
            out[leftover_fill] = {7}
            out[leftover_cri] = {7}
        if 8 in want:
            out[4240] = {8}
            out[4241] = {8}
            out[4245] = {8}
            out[leftover_py] = {8}
        if 9 in want:
            out[leftover_py] = out.get(leftover_py, set()) | {9}
            out[4240] = out.get(4240, set()) | {9}
        return out

    monkeypatch.setattr(browser, "_pids_holding_socket_inodes", _both)
    monkeypatch.setattr(
        browser, "_loopback_listen_ports_for_pid",
        lambda pid: {40141} if pid in {leftover_sup, 4300, leftover_fill, leftover_cri} else (
            {9333, 18888} if pid == leftover_py else (
                {9333, 18888} if pid == 4240 else (
                    {9333} if pid in {4241, 4245} else set()
                )
            )
        ),
    )
    _reset_dock_port_memory_for_tests()
    (tmp_path / "dock-cdp-port").write_text("18888\n", encoding="utf-8")
    assert browser.unique_lock_chrome_hidden_by_leftover_file(
        40141, str(tmp_path),
    ) is None
    assert browser.lock_listed_persist_port() is None
    assert browser._this_jar_chromium_pid(str(tmp_path)) is None

    def _leftover_only(want):
        out = {}
        if 7 in want:
            out[leftover_sup] = {7}
            out[4300] = {7}
            out[leftover_fill] = {7}
            out[leftover_cri] = {7}
        if 9 in want:
            out[leftover_py] = {9}
            out[leftover_cri] = out.get(leftover_cri, set()) | {9}
        return out

    monkeypatch.setattr(browser, "_pids_holding_socket_inodes", _leftover_only)
    monkeypatch.setattr(
        browser, "_this_jar_children",
        lambda parent, user_data_dir: (
            {leftover_sup} if parent == leftover_root else (
                {4300} if parent == leftover_sup else (
                    {leftover_fill} if parent == 4300 else (
                        {leftover_cri} if parent == leftover_fill else (
                            {leftover_py} if parent == leftover_cri else set()
                        )
                    )
                )
            )
        ),
    )
    monkeypatch.setattr(
        browser, "_loopback_listen_ports_for_pid",
        lambda pid: {40141, 18888} if pid == leftover_cri else (
            {40141} if pid in {leftover_sup, 4300, leftover_fill} else (
                {18888} if pid == leftover_py else set()
            )
        ),
    )
    monkeypatch.setattr(
        browser,
        "_cdp_port_reachable",
        lambda port, hosts: port in (40141, 18888) and "::1" in hosts,
    )
    _reset_dock_port_memory_for_tests()
    (tmp_path / "dock-cdp-port").write_text("18888\n", encoding="utf-8")
    assert browser._unique_this_jar_parent(
        {leftover_sup, 4300, leftover_fill, leftover_cri}, str(tmp_path),
    ) == leftover_root
    assert browser.unique_lock_chrome_hidden_by_leftover_file(
        40141, str(tmp_path),
    ) is None
    assert browser.lock_listed_persist_port() is None
    assert browser._this_jar_chromium_pid(str(tmp_path)) is None
    assert browser.running_instance_cdp_port(str(tmp_path)) == 40141
    assert _remembered_dock_attach_port() == 40141



def test_leftover_supervisor_parent_root_devtools_great_great_grandchild_persist_does_not_hide_sibling_chrome(
    tmp_path, monkeypatch,
):
    """Finding 208: leftover supervisor leftover parent leftover supervisor hid sibling chrome.

    leftover supervisor leftover parent leftover supervisor leftover
    parent leftover supervisor names this jar and leftover supervisor
    leftover parent leftover supervisor leftover DevTools. leftover
    daemon, leftover fill, leftover CRI, leftover supervisor, and
    leftover supervisor leftover parent leftover supervisor leftover
    DevTools. leftover-outside leftover supervisor leftover parent
    leftover parent. leftover unique leftover parent leftover children
    leftover children leftover children is leftover supervisor leftover
    children leftover supervisor leftover children leftover daemon,
    missed leftover daemon leftover children (chrome). Leftover python
    leftover CRI's leftover child inherited leftover persist with
    chrome. Persist / attach / skip-kill / Take over followed leftover
    DevTools; chrome pid became None (``owner`` None) so Take over
    tree-killed the Browser a human is typing into. One hop of leftover
    unique leftover parent's leftover children leftover children leftover
    children leftover children is leftover daemon leftover children, not
    a leftover-holder leftover grandparent walk of leftover fill.
    Leftover supervisor leftover parent leftover parent that does not
    name this jar stays 207. Leftover python that inherited both leftover
    persist and chrome's CDP stays 85. Leftover-only leftover supervisor
    leftover parent leftover supervisor plus leftover supervisor plus
    leftover daemon plus leftover fill plus leftover CRI leftover
    DevTools plus leftover python leftover persist leftover-shared stays
    86. Leftover fill great-grandchild leftover persist leftover-shared
    stays 86. Cousin chrome under leftover CRI stays 86. Attach must not
    stamp persist. ``remember()`` still does not update memory
    (finding 178). Identity candidate order stays file-then-memory
    (finding 169).
    """
    from hermes_constants import hermes_home_key
    from tools.browser_tool_session import (
        _last_dock_cdp_port,
        _remembered_dock_attach_port,
        _reset_dock_port_memory_for_tests,
    )

    leftover_fill = 4242
    leftover_cri = 4244
    leftover_py = 4246
    leftover_sup = 4290
    leftover_root = 4280
    leftover_parent = 4270

    monkeypatch.setattr(runtime, "state_dir", lambda: tmp_path)
    monkeypatch.setattr(browser, "profile_dir", lambda: tmp_path)
    monkeypatch.setattr(browser, "_lock_pid", lambda d: None)
    monkeypatch.setattr(
        browser,
        "_chromium_cmdline_tokens",
        lambda pid: (
            ["chrome", "--type=zygote", f"--user-data-dir={tmp_path}"]
            if pid == 4241 else
            ["chrome", "--type=utility", f"--user-data-dir={tmp_path}"]
            if pid == 4245 else
            ["agent-browser", "supervisor", f"--user-data-dir={tmp_path}"]
            if pid == leftover_parent else
            ["agent-browser", "supervisor", f"--user-data-dir={tmp_path}"]
            if pid == leftover_root else
            ["agent-browser", "supervisor", f"--user-data-dir={tmp_path}"]
            if pid == leftover_sup else
            ["agent-browser", "daemon", f"--user-data-dir={tmp_path}"]
            if pid == 4300 else
            ["agent-browser", "fill", f"--user-data-dir={tmp_path}"]
            if pid == leftover_fill else
            ["agent-browser", "cri", f"--user-data-dir={tmp_path}"]
            if pid == leftover_cri else
            ["agent-browser", "python", f"--user-data-dir={tmp_path}"]
            if pid == leftover_py else
            ["chrome", f"--user-data-dir={tmp_path}"]
        ),
    )
    monkeypatch.setattr(
        browser,
        "_proc_ppid",
        lambda pid: {
            4240: 4300, 4241: 4240, 4245: 4241,
            leftover_root: leftover_parent, leftover_sup: leftover_root, 4300: leftover_sup,
            leftover_fill: 4300, leftover_cri: leftover_fill, leftover_py: leftover_cri,
        }.get(pid),
    )
    monkeypatch.setattr(
        browser,
        "_launched_by_session",
        lambda pid: "h_review" if pid == 4240 else None,
    )
    monkeypatch.setattr(browser, "_recover_cdp_port_from_singleton", lambda *a, **k: None)
    monkeypatch.setattr(
        browser, "_this_jar_children",
        lambda parent, user_data_dir: (
            {leftover_root} if parent == leftover_parent else (
                {leftover_sup} if parent == leftover_root else (
                    {4300} if parent == leftover_sup else (
                        {4240, leftover_fill} if parent == 4300 else (
                            {leftover_cri} if parent == leftover_fill else (
                                {leftover_py} if parent == leftover_cri else set()
                            )
                        )
                    )
                )
            )
        ),
    )

    def _inodes(port):
        if port == 40141:
            return {7: {"::1"}}
        if port == 9333:
            return {8: {"::1"}}
        if port == 18888:
            return {9: {"::1"}}
        return {}

    def _holders(want):
        out = {}
        if 7 in want:
            out[leftover_root] = {7}
            out[leftover_sup] = {7}
            out[4300] = {7}
            out[leftover_fill] = {7}
            out[leftover_cri] = {7}
        if 8 in want:
            out[4240] = {8}
            out[4241] = {8}
            out[4245] = {8}
        if 9 in want:
            out[leftover_py] = {9}
            out[4240] = out.get(4240, set()) | {9}
        return out

    monkeypatch.setattr(browser, "_loopback_listen_inodes_for_port", _inodes)
    monkeypatch.setattr(browser, "_pids_holding_socket_inodes", _holders)
    monkeypatch.setattr(
        browser, "_loopback_listen_ports_for_pid",
        lambda pid: {40141} if pid in {leftover_root, leftover_sup, 4300, leftover_fill, leftover_cri} else (
            {18888} if pid == leftover_py else (
                {9333, 18888} if pid == 4240 else (
                    {9333} if pid in {4241, 4245} else set()
                )
            )
        ),
    )
    monkeypatch.setattr(
        browser, "_loopback_listen_targets_for_pid",
        lambda pid: {("::1", 40141)} if pid in {leftover_root, leftover_sup, 4300, leftover_fill, leftover_cri} else (
            {("::1", 18888)} if pid == leftover_py else (
                {("::1", 9333), ("::1", 18888)} if pid == 4240 else (
                    {("::1", 9333)} if pid in {4241, 4245} else set()
                )
            )
        ),
    )
    monkeypatch.setattr(
        browser,
        "_cdp_port_reachable",
        lambda port, hosts: port in (9333, 40141, 18888) and "::1" in hosts,
    )
    monkeypatch.setattr(browser, "_configured_cdp_override_url", lambda: "")
    (tmp_path / "DevToolsActivePort").write_text(
        "40141\n/devtools/browser/abc\n", encoding="utf-8",
    )

    _reset_dock_port_memory_for_tests()
    (tmp_path / "dock-cdp-port").write_text("18888\n", encoding="utf-8")
    assert browser._this_jar_holder_pids(40141, str(tmp_path)) == {
        leftover_root, leftover_sup, 4300, leftover_fill, leftover_cri,
    }
    assert browser._this_jar_holder_pids(18888, str(tmp_path)) == {leftover_py, 4240}
    assert browser._this_jar_holder_pids(9333, str(tmp_path)) == {4240, 4241, 4245}
    assert browser._pid_is_chromium_browser(leftover_parent) is False
    assert browser._pid_is_chromium_browser(leftover_root) is False
    assert browser._pid_is_chromium_browser(leftover_sup) is False
    assert browser._pid_is_chromium_browser(4300) is False
    assert browser._proc_ppid(leftover_root) == leftover_parent
    assert browser._proc_ppid(leftover_sup) == leftover_root
    assert browser._proc_ppid(4300) == leftover_sup
    assert browser._unique_this_jar_parent(
        {leftover_root, leftover_sup, 4300, leftover_fill, leftover_cri}, str(tmp_path),
    ) == leftover_parent
    assert browser.unique_lock_chrome_hidden_by_leftover_file(
        40141, str(tmp_path),
    ) == 9333
    assert browser.lock_listed_persist_port() == 9333
    assert browser._this_jar_chromium_pid(str(tmp_path)) == 4240
    assert browser.shared_chromium_owner_session() == "h_review"
    assert _remembered_dock_attach_port() == 9333
    assert (tmp_path / "dock-cdp-port").read_text(encoding="utf-8").strip() == "18888"
    assert browser.running_instance_cdp_port(str(tmp_path)) == 9333
    assert _remembered_dock_attach_port() == 9333
    assert browser.persist_live_dock_cdp_port() == 9333
    assert (tmp_path / "dock-cdp-port").read_text(encoding="utf-8") == "9333"
    assert _last_dock_cdp_port.get(hermes_home_key()) == 9333

    def _both(want):
        out = {}
        if 7 in want:
            out[leftover_root] = {7}
            out[leftover_sup] = {7}
            out[4300] = {7}
            out[leftover_fill] = {7}
            out[leftover_cri] = {7}
        if 8 in want:
            out[4240] = {8}
            out[4241] = {8}
            out[4245] = {8}
            out[leftover_py] = {8}
        if 9 in want:
            out[leftover_py] = out.get(leftover_py, set()) | {9}
            out[4240] = out.get(4240, set()) | {9}
        return out

    monkeypatch.setattr(browser, "_pids_holding_socket_inodes", _both)
    monkeypatch.setattr(
        browser, "_loopback_listen_ports_for_pid",
        lambda pid: {40141} if pid in {leftover_root, leftover_sup, 4300, leftover_fill, leftover_cri} else (
            {9333, 18888} if pid == leftover_py else (
                {9333, 18888} if pid == 4240 else (
                    {9333} if pid in {4241, 4245} else set()
                )
            )
        ),
    )
    _reset_dock_port_memory_for_tests()
    (tmp_path / "dock-cdp-port").write_text("18888\n", encoding="utf-8")
    assert browser.unique_lock_chrome_hidden_by_leftover_file(
        40141, str(tmp_path),
    ) is None
    assert browser.lock_listed_persist_port() is None
    assert browser._this_jar_chromium_pid(str(tmp_path)) is None

    def _leftover_only(want):
        out = {}
        if 7 in want:
            out[leftover_root] = {7}
            out[leftover_sup] = {7}
            out[4300] = {7}
            out[leftover_fill] = {7}
            out[leftover_cri] = {7}
        if 9 in want:
            out[leftover_py] = {9}
            out[leftover_cri] = out.get(leftover_cri, set()) | {9}
        return out

    monkeypatch.setattr(browser, "_pids_holding_socket_inodes", _leftover_only)
    monkeypatch.setattr(
        browser, "_this_jar_children",
        lambda parent, user_data_dir: (
            {leftover_root} if parent == leftover_parent else (
                {leftover_sup} if parent == leftover_root else (
                    {4300} if parent == leftover_sup else (
                        {leftover_fill} if parent == 4300 else (
                            {leftover_cri} if parent == leftover_fill else (
                                {leftover_py} if parent == leftover_cri else set()
                            )
                        )
                    )
                )
            )
        ),
    )
    monkeypatch.setattr(
        browser, "_loopback_listen_ports_for_pid",
        lambda pid: {40141, 18888} if pid == leftover_cri else (
            {40141} if pid in {leftover_root, leftover_sup, 4300, leftover_fill} else (
                {18888} if pid == leftover_py else set()
            )
        ),
    )
    monkeypatch.setattr(
        browser,
        "_cdp_port_reachable",
        lambda port, hosts: port in (40141, 18888) and "::1" in hosts,
    )
    _reset_dock_port_memory_for_tests()
    (tmp_path / "dock-cdp-port").write_text("18888\n", encoding="utf-8")
    assert browser._unique_this_jar_parent(
        {leftover_root, leftover_sup, 4300, leftover_fill, leftover_cri}, str(tmp_path),
    ) == leftover_parent
    assert browser.unique_lock_chrome_hidden_by_leftover_file(
        40141, str(tmp_path),
    ) is None
    assert browser.lock_listed_persist_port() is None
    assert browser._this_jar_chromium_pid(str(tmp_path)) is None
    assert browser.running_instance_cdp_port(str(tmp_path)) == 40141
    assert _remembered_dock_attach_port() == 40141


def test_leftover_supervisor_parent_grand_devtools_great_great_great_grandchild_persist_does_not_hide_sibling_chrome(
    tmp_path, monkeypatch,
):
    """Finding 209: leftover supervisor leftover parent leftover parent leftover supervisor hid sibling chrome.

    leftover supervisor leftover parent leftover supervisor leftover
    parent leftover supervisor leftover parent leftover supervisor
    names this jar and leftover supervisor leftover parent leftover
    supervisor leftover parent leftover supervisor leftover DevTools.
    leftover daemon, leftover fill, leftover CRI, leftover supervisor,
    leftover supervisor leftover parent leftover supervisor leftover
    DevTools, and leftover supervisor leftover parent leftover
    supervisor leftover parent leftover supervisor leftover DevTools.
    leftover-outside leftover supervisor leftover parent leftover
    parent leftover parent. leftover unique leftover parent leftover
    children leftover children leftover children leftover children is
    leftover supervisor leftover children leftover supervisor leftover
    children leftover supervisor leftover children leftover daemon,
    missed leftover daemon leftover children (chrome). Leftover python
    leftover CRI's leftover child inherited leftover persist with
    chrome. Persist / attach / skip-kill / Take over followed leftover
    DevTools; chrome pid became None (``owner`` None) so Take over
    tree-killed the Browser a human is typing into. One hop of leftover
    unique leftover parent's leftover children leftover children leftover
    children leftover children leftover children is leftover daemon
    leftover children, not a leftover-holder leftover grandparent walk
    of leftover fill. Leftover supervisor leftover parent leftover
    parent leftover parent that does not name this jar stays 208.
    Leftover python that inherited both leftover persist and chrome's
    CDP stays 85. Leftover-only leftover supervisor leftover parent
    leftover parent leftover supervisor plus leftover supervisor leftover
    parent leftover supervisor plus leftover supervisor plus leftover
    daemon plus leftover fill plus leftover CRI leftover DevTools plus
    leftover python leftover persist leftover-shared stays 86. Leftover
    fill great-grandchild leftover persist leftover-shared stays 86.
    Cousin chrome under leftover CRI stays 86. Attach must not stamp
    persist. ``remember()`` still does not update memory (finding 178).
    Identity candidate order stays file-then-memory (finding 169).
    """
    from hermes_constants import hermes_home_key
    from tools.browser_tool_session import (
        _last_dock_cdp_port,
        _remembered_dock_attach_port,
        _reset_dock_port_memory_for_tests,
    )

    leftover_fill = 4242
    leftover_cri = 4244
    leftover_py = 4246
    leftover_sup = 4290
    leftover_root = 4280
    leftover_parent = 4270
    leftover_grand = 4260

    monkeypatch.setattr(runtime, "state_dir", lambda: tmp_path)
    monkeypatch.setattr(browser, "profile_dir", lambda: tmp_path)
    monkeypatch.setattr(browser, "_lock_pid", lambda d: None)
    monkeypatch.setattr(
        browser,
        "_chromium_cmdline_tokens",
        lambda pid: (
            ["chrome", "--type=zygote", f"--user-data-dir={tmp_path}"]
            if pid == 4241 else
            ["chrome", "--type=utility", f"--user-data-dir={tmp_path}"]
            if pid == 4245 else
            ["agent-browser", "supervisor", f"--user-data-dir={tmp_path}"]
            if pid == leftover_grand else
            ["agent-browser", "supervisor", f"--user-data-dir={tmp_path}"]
            if pid == leftover_parent else
            ["agent-browser", "supervisor", f"--user-data-dir={tmp_path}"]
            if pid == leftover_root else
            ["agent-browser", "supervisor", f"--user-data-dir={tmp_path}"]
            if pid == leftover_sup else
            ["agent-browser", "daemon", f"--user-data-dir={tmp_path}"]
            if pid == 4300 else
            ["agent-browser", "fill", f"--user-data-dir={tmp_path}"]
            if pid == leftover_fill else
            ["agent-browser", "cri", f"--user-data-dir={tmp_path}"]
            if pid == leftover_cri else
            ["agent-browser", "python", f"--user-data-dir={tmp_path}"]
            if pid == leftover_py else
            ["chrome", f"--user-data-dir={tmp_path}"]
        ),
    )
    monkeypatch.setattr(
        browser,
        "_proc_ppid",
        lambda pid: {
            4240: 4300, 4241: 4240, 4245: 4241,
            leftover_parent: leftover_grand, leftover_root: leftover_parent, leftover_sup: leftover_root, 4300: leftover_sup,
            leftover_fill: 4300, leftover_cri: leftover_fill, leftover_py: leftover_cri,
        }.get(pid),
    )
    monkeypatch.setattr(
        browser,
        "_launched_by_session",
        lambda pid: "h_review" if pid == 4240 else None,
    )
    monkeypatch.setattr(browser, "_recover_cdp_port_from_singleton", lambda *a, **k: None)
    monkeypatch.setattr(
        browser, "_this_jar_children",
        lambda parent, user_data_dir: (
            {leftover_parent} if parent == leftover_grand else (
            {leftover_root} if parent == leftover_parent else (
                {leftover_sup} if parent == leftover_root else (
                    {4300} if parent == leftover_sup else (
                        {4240, leftover_fill} if parent == 4300 else (
                            {leftover_cri} if parent == leftover_fill else (
                                {leftover_py} if parent == leftover_cri else set()
                            )
                        )
                    )
                )
            )
            )
        ),
    )

    def _inodes(port):
        if port == 40141:
            return {7: {"::1"}}
        if port == 9333:
            return {8: {"::1"}}
        if port == 18888:
            return {9: {"::1"}}
        return {}

    def _holders(want):
        out = {}
        if 7 in want:
            out[leftover_parent] = {7}
            out[leftover_root] = {7}
            out[leftover_sup] = {7}
            out[4300] = {7}
            out[leftover_fill] = {7}
            out[leftover_cri] = {7}
        if 8 in want:
            out[4240] = {8}
            out[4241] = {8}
            out[4245] = {8}
        if 9 in want:
            out[leftover_py] = {9}
            out[4240] = out.get(4240, set()) | {9}
        return out

    monkeypatch.setattr(browser, "_loopback_listen_inodes_for_port", _inodes)
    monkeypatch.setattr(browser, "_pids_holding_socket_inodes", _holders)
    monkeypatch.setattr(
        browser, "_loopback_listen_ports_for_pid",
        lambda pid: {40141} if pid in {leftover_parent, leftover_root, leftover_sup, 4300, leftover_fill, leftover_cri} else (
            {18888} if pid == leftover_py else (
                {9333, 18888} if pid == 4240 else (
                    {9333} if pid in {4241, 4245} else set()
                )
            )
        ),
    )
    monkeypatch.setattr(
        browser, "_loopback_listen_targets_for_pid",
        lambda pid: {("::1", 40141)} if pid in {leftover_parent, leftover_root, leftover_sup, 4300, leftover_fill, leftover_cri} else (
            {("::1", 18888)} if pid == leftover_py else (
                {("::1", 9333), ("::1", 18888)} if pid == 4240 else (
                    {("::1", 9333)} if pid in {4241, 4245} else set()
                )
            )
        ),
    )
    monkeypatch.setattr(
        browser,
        "_cdp_port_reachable",
        lambda port, hosts: port in (9333, 40141, 18888) and "::1" in hosts,
    )
    monkeypatch.setattr(browser, "_configured_cdp_override_url", lambda: "")
    (tmp_path / "DevToolsActivePort").write_text(
        "40141\n/devtools/browser/abc\n", encoding="utf-8",
    )

    _reset_dock_port_memory_for_tests()
    (tmp_path / "dock-cdp-port").write_text("18888\n", encoding="utf-8")
    assert browser._this_jar_holder_pids(40141, str(tmp_path)) == {
        leftover_parent, leftover_root, leftover_sup, 4300, leftover_fill, leftover_cri,
    }
    assert browser._this_jar_holder_pids(18888, str(tmp_path)) == {leftover_py, 4240}
    assert browser._this_jar_holder_pids(9333, str(tmp_path)) == {4240, 4241, 4245}
    assert browser._pid_is_chromium_browser(leftover_grand) is False
    assert browser._pid_is_chromium_browser(leftover_parent) is False
    assert browser._pid_is_chromium_browser(leftover_root) is False
    assert browser._pid_is_chromium_browser(leftover_sup) is False
    assert browser._pid_is_chromium_browser(4300) is False
    assert browser._proc_ppid(leftover_parent) == leftover_grand
    assert browser._proc_ppid(leftover_root) == leftover_parent
    assert browser._proc_ppid(leftover_sup) == leftover_root
    assert browser._proc_ppid(4300) == leftover_sup
    assert browser._unique_this_jar_parent(
        {leftover_parent, leftover_root, leftover_sup, 4300, leftover_fill, leftover_cri}, str(tmp_path),
    ) == leftover_grand
    assert browser.unique_lock_chrome_hidden_by_leftover_file(
        40141, str(tmp_path),
    ) == 9333
    assert browser.lock_listed_persist_port() == 9333
    assert browser._this_jar_chromium_pid(str(tmp_path)) == 4240
    assert browser.shared_chromium_owner_session() == "h_review"
    assert _remembered_dock_attach_port() == 9333
    assert (tmp_path / "dock-cdp-port").read_text(encoding="utf-8").strip() == "18888"
    assert browser.running_instance_cdp_port(str(tmp_path)) == 9333
    assert _remembered_dock_attach_port() == 9333
    assert browser.persist_live_dock_cdp_port() == 9333
    assert (tmp_path / "dock-cdp-port").read_text(encoding="utf-8") == "9333"
    assert _last_dock_cdp_port.get(hermes_home_key()) == 9333

    def _both(want):
        out = {}
        if 7 in want:
            out[leftover_parent] = {7}
            out[leftover_root] = {7}
            out[leftover_sup] = {7}
            out[4300] = {7}
            out[leftover_fill] = {7}
            out[leftover_cri] = {7}
        if 8 in want:
            out[4240] = {8}
            out[4241] = {8}
            out[4245] = {8}
            out[leftover_py] = {8}
        if 9 in want:
            out[leftover_py] = out.get(leftover_py, set()) | {9}
            out[4240] = out.get(4240, set()) | {9}
        return out

    monkeypatch.setattr(browser, "_pids_holding_socket_inodes", _both)
    monkeypatch.setattr(
        browser, "_loopback_listen_ports_for_pid",
        lambda pid: {40141} if pid in {leftover_parent, leftover_root, leftover_sup, 4300, leftover_fill, leftover_cri} else (
            {9333, 18888} if pid == leftover_py else (
                {9333, 18888} if pid == 4240 else (
                    {9333} if pid in {4241, 4245} else set()
                )
            )
        ),
    )
    _reset_dock_port_memory_for_tests()
    (tmp_path / "dock-cdp-port").write_text("18888\n", encoding="utf-8")
    assert browser.unique_lock_chrome_hidden_by_leftover_file(
        40141, str(tmp_path),
    ) is None
    assert browser.lock_listed_persist_port() is None
    assert browser._this_jar_chromium_pid(str(tmp_path)) is None

    def _leftover_only(want):
        out = {}
        if 7 in want:
            out[leftover_parent] = {7}
            out[leftover_root] = {7}
            out[leftover_sup] = {7}
            out[4300] = {7}
            out[leftover_fill] = {7}
            out[leftover_cri] = {7}
        if 9 in want:
            out[leftover_py] = {9}
            out[leftover_cri] = out.get(leftover_cri, set()) | {9}
        return out

    monkeypatch.setattr(browser, "_pids_holding_socket_inodes", _leftover_only)
    monkeypatch.setattr(
        browser, "_this_jar_children",
        lambda parent, user_data_dir: (
            {leftover_parent} if parent == leftover_grand else (
            {leftover_root} if parent == leftover_parent else (
                {leftover_sup} if parent == leftover_root else (
                    {4300} if parent == leftover_sup else (
                        {leftover_fill} if parent == 4300 else (
                            {leftover_cri} if parent == leftover_fill else (
                                {leftover_py} if parent == leftover_cri else set()
                            )
                        )
                    )
                )
            )
            )
        ),
    )
    monkeypatch.setattr(
        browser, "_loopback_listen_ports_for_pid",
        lambda pid: {40141, 18888} if pid == leftover_cri else (
            {40141} if pid in {leftover_parent, leftover_root, leftover_sup, 4300, leftover_fill} else (
                {18888} if pid == leftover_py else set()
            )
        ),
    )
    monkeypatch.setattr(
        browser,
        "_cdp_port_reachable",
        lambda port, hosts: port in (40141, 18888) and "::1" in hosts,
    )
    _reset_dock_port_memory_for_tests()
    (tmp_path / "dock-cdp-port").write_text("18888\n", encoding="utf-8")
    assert browser._unique_this_jar_parent(
        {leftover_parent, leftover_root, leftover_sup, 4300, leftover_fill, leftover_cri}, str(tmp_path),
    ) == leftover_grand
    assert browser.unique_lock_chrome_hidden_by_leftover_file(
        40141, str(tmp_path),
    ) is None
    assert browser.lock_listed_persist_port() is None
    assert browser._this_jar_chromium_pid(str(tmp_path)) is None
    assert browser.running_instance_cdp_port(str(tmp_path)) == 40141
    assert _remembered_dock_attach_port() == 40141


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


def test_agent_attach_uses_persist_when_memory_is_stale(tmp_path, monkeypatch):
    """Finding 169: stale ``_last_dock_cdp_port`` hid persist after a live miss.

    ``remember_dock_cdp_port`` / ``running_instance_cdp_port`` write the
    file only. Leftover identity caches the previous ephemeral port in
    ``_last_dock_cdp_port``. Finding 168 then used memory first: empty
    hosts on the stale number launched ``--session`` and Chromium
    singleton-forwarded into the jar a human holds. Persist file first.
    Memory-only (persist unlinked) still attaches. Empty hosts stay a
    launch. Planted persist still identifies leftover when hosts are
    empty. 9222 stays unknown.
    """
    from hermes_constants import hermes_home_key
    from tools import browser_tool_session as session
    from tools.browser_tool_session import (
        _cdp_url_is_bot_desktop_browser,
        _last_dock_cdp_port,
        _remembered_dock_attach_port,
        _reset_dock_port_memory_for_tests,
    )

    v6, v4, port, profile = _ipv6_only_dock(tmp_path, monkeypatch)
    try:
        _reset_dock_port_memory_for_tests()
        browser.remember_dock_cdp_port(port)
        _last_dock_cdp_port[hermes_home_key()] = 11111
        monkeypatch.setattr(browser, "running_instance_cdp_port", lambda *a, **k: None)
        assert browser.last_known_dock_cdp_port() == port
        assert browser._this_jar_listen_connect_hosts(port) == ("::1",)
        assert browser._this_jar_listen_connect_hosts(11111) == ()
        assert _remembered_dock_attach_port() == port
        assert _cdp_url_is_bot_desktop_browser(f"http://[::1]:{port}") is True
        assert _cdp_url_is_bot_desktop_browser(f"http://127.0.0.1:{port}") is False

        argvs = _spawn_agent_open(monkeypatch, session)
        assert argvs[-1][:5] == [
            "agent-browser", "--session", "h_abc", "--cdp", f"http://[::1]:{port}",
        ]
        assert "127.0.0.1" not in argvs[-1][4]

        _reset_dock_port_memory_for_tests()
        browser.remember_dock_cdp_port(9333)
        _last_dock_cdp_port[hermes_home_key()] = 11111
        assert _cdp_url_is_bot_desktop_browser("http://127.0.0.1:9333") is True
        assert _remembered_dock_attach_port() is None
        assert browser.dock_cdp_attach_target(9222) == "9222"
    finally:
        v6.close()
        v4.close()


def test_agent_attach_uses_file_named_port_when_persist_never_stamped(tmp_path, monkeypatch):
    """Finding 170: persist-never-ran hid file-named DevTools after a live miss.

    ``running_instance_cdp_port`` requires a fresh TCP accept. Leftover
    identity already trusts ``DevToolsActivePort`` when this jar still
    inode-listens (finding 166). Agent attach did not — persist may
    never have been stamped, so it launched ``--session`` and Chromium
    singleton-forwarded into the jar a human holds. Attach from the
    file-named listen. Empty-hosts persist must not hide that file.
    Empty hosts stay a launch. 9222 stays unknown.
    """
    from tools import browser_tool_session as session
    from tools.browser_tool_session import (
        _cdp_url_is_bot_desktop_browser,
        _last_dock_cdp_port,
        _remembered_dock_attach_port,
        _reset_dock_port_memory_for_tests,
    )

    v6, v4, port, profile = _ipv6_only_dock(tmp_path, monkeypatch)
    try:
        _reset_dock_port_memory_for_tests()
        (profile / "DevToolsActivePort").write_text(
            f"{port}\n/devtools/browser/abc\n", encoding="utf-8",
        )
        monkeypatch.setattr(browser, "running_instance_cdp_port", lambda *a, **k: None)
        assert browser.last_known_dock_cdp_port() is None
        assert browser.file_named_dock_listen_port() == port
        assert browser._this_jar_listen_connect_hosts(port) == ("::1",)
        assert _remembered_dock_attach_port() == port
        assert _cdp_url_is_bot_desktop_browser(f"http://[::1]:{port}") is True
        assert _cdp_url_is_bot_desktop_browser(f"http://127.0.0.1:{port}") is False

        argvs = _spawn_agent_open(monkeypatch, session)
        assert argvs[-1][:5] == [
            "agent-browser", "--session", "h_abc", "--cdp", f"http://[::1]:{port}",
        ]
        assert "127.0.0.1" not in argvs[-1][4]

        browser.remember_dock_cdp_port(9333)
        _last_dock_cdp_port.clear()
        assert browser.last_known_dock_cdp_port() == 9333
        assert _remembered_dock_attach_port() == port
        argvs = _spawn_agent_open(monkeypatch, session)
        assert argvs[-1][4] == f"http://[::1]:{port}"

        (profile / "DevToolsActivePort").unlink()
        _reset_dock_port_memory_for_tests()
        browser.remember_dock_cdp_port(9333)
        assert _remembered_dock_attach_port() is None
        assert browser.dock_cdp_attach_target(9222) == "9222"
    finally:
        v6.close()
        v4.close()


def test_agent_attach_prefers_file_named_when_persist_still_has_hosts(
    tmp_path, monkeypatch,
):
    """Finding 172: persist-with-hosts hid a different file-named live listen.

    Unique-listen recover is unknown when Chromium has several specific
    loopbacks (finding 85) — inherited old fd + new DevTools. Leftover
    holding the file port is the TCP miss that skips finding 164
    (finding 165), so persist stays the previous stamp. Finding 170
    only skipped empty-hosts persist. Attach the file-named listen.
    Lock pid that still lists persist and not the file keeps persist
    (file is then the inherited leftover). Empty hosts stay a launch.
    The other family squat and 9222 stay unknown.
    """
    from tools import browser_tool_session as session
    from tools.browser_tool_session import (
        _cdp_url_is_bot_desktop_browser,
        _last_dock_cdp_port,
        _remembered_dock_attach_port,
        _reset_dock_port_memory_for_tests,
    )

    live6, live4, live, profile = _ipv6_only_dock(tmp_path, monkeypatch)
    stale6 = socket.socket(socket.AF_INET6, socket.SOCK_STREAM)
    stale6.setsockopt(socket.IPPROTO_IPV6, socket.IPV6_V6ONLY, 1)
    stale6.bind(("::1", 0))
    stale6.listen(1)
    stale = stale6.getsockname()[1]
    stale4 = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    stale4.bind(("127.0.0.1", stale))
    stale4.listen(1)
    monkeypatch.setattr(
        browser, "_loopback_listen_ports_for_pid", lambda pid: {live, stale},
    )
    monkeypatch.setattr(
        browser,
        "_loopback_listen_targets_for_pid",
        lambda pid: {("::1", live), ("::1", stale)},
    )
    try:
        _reset_dock_port_memory_for_tests()
        (profile / "DevToolsActivePort").write_text(
            f"{live}\n/devtools/browser/abc\n", encoding="utf-8",
        )
        browser.remember_dock_cdp_port(stale)
        _last_dock_cdp_port.clear()
        monkeypatch.setattr(browser, "running_instance_cdp_port", lambda *a, **k: None)
        assert browser.last_known_dock_cdp_port() == stale
        assert browser.file_named_dock_listen_port() == live
        assert browser._this_jar_listen_connect_hosts(stale) == ("::1",)
        assert browser._this_jar_listen_connect_hosts(live) == ("::1",)
        assert _remembered_dock_attach_port() == live
        argvs = _spawn_agent_open(monkeypatch, session)
        assert argvs[-1][:5] == [
            "agent-browser", "--session", "h_abc", "--cdp", f"http://[::1]:{live}",
        ]
        assert "127.0.0.1" not in argvs[-1][4]
        assert argvs[-1][4] != str(stale)
        assert browser.last_known_dock_cdp_port() == stale
        assert _cdp_url_is_bot_desktop_browser(f"http://[::1]:{live}") is True
        assert _cdp_url_is_bot_desktop_browser(f"http://127.0.0.1:{live}") is False

        monkeypatch.setattr(
            browser, "_loopback_listen_ports_for_pid", lambda pid: {live},
        )
        monkeypatch.setattr(
            browser,
            "_loopback_listen_targets_for_pid",
            lambda pid: {("::1", live)},
        )
        _reset_dock_port_memory_for_tests()
        browser.remember_dock_cdp_port(live)
        (profile / "DevToolsActivePort").write_text(
            f"{stale}\n/devtools/browser/abc\n", encoding="utf-8",
        )
        assert browser.file_named_dock_listen_port() == stale
        assert browser._this_jar_listen_connect_hosts(live) == ("::1",)
        assert _remembered_dock_attach_port() == live
        assert browser.dock_cdp_attach_target(9222) == "9222"
    finally:
        live6.close()
        live4.close()
        stale6.close()
        stale4.close()


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
