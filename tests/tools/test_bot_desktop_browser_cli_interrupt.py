"""Leftover browser CLI input after Take over must be interrupted, not waited out.

``_run_browser_command`` and ``browser_exec`` only discard the result after the
CLI finishes. Leftover ``fill`` / Playwright keystrokes still land in the field
a human just took over — the same class as leftover CDP ``ws.send`` and leftover
``computer_use`` ``type_text``.
"""

from __future__ import annotations

import json
import threading
from pathlib import Path

import pytest

from tools.bot_desktop import lease, runtime


@pytest.fixture(autouse=True)
def _fresh(monkeypatch):
    from tools.browser_tool_session import _reset_dock_port_memory_for_tests

    lease._reset_for_tests()
    _reset_dock_port_memory_for_tests()
    monkeypatch.setattr(runtime, "published_env", lambda: {"DISPLAY": ":37"})
    yield
    lease._reset_for_tests()
    _reset_dock_port_memory_for_tests()


class _BlockingProc:
    """CLI whose ``wait`` holds until ``kill`` — leftover fill if we wait it out."""

    def __init__(self):
        self.started = threading.Event()
        self.released = threading.Event()
        self.killed = 0
        self.returncode = None

    def wait(self, timeout=None):
        self.started.set()
        if not self.released.wait(timeout=3.0):
            self.returncode = 0
            return 0
        self.returncode = -9
        return self.returncode

    def communicate(self, input=None, timeout=None):
        self.wait(timeout=timeout)
        return ("typed secret", "")

    def kill(self):
        self.killed += 1
        self.released.set()

    def poll(self):
        return self.returncode


def _wire_local_browser(monkeypatch, proc, commands=None):
    from tools import browser_tool as browser
    from tools import browser_tool_session as session

    monkeypatch.delenv("AGENT_BROWSER_PROFILE", raising=False)
    monkeypatch.setattr(browser, "_is_camofox_mode", lambda: False)
    monkeypatch.setattr(browser, "_blocked_private_page_action", lambda *a: None)
    monkeypatch.setattr(session, "_browser_command_preflight", lambda: {"browser_cmd": "agent-browser"})
    monkeypatch.setattr(session, "_get_session_info", lambda *a: {
        "session_name": "review", "cdp_url": None, "features": {"local": True},
    })
    monkeypatch.setattr(session._cloud, "_get_browser_engine", lambda: "chrome")
    monkeypatch.setattr(session._cloud, "_is_headed_mode", lambda: True)
    monkeypatch.setattr(session._cdp, "_ensure_cdp_supervisor", lambda *a, **k: None)

    def popen(argv, env, socket_dir, tag):
        if commands is not None:
            commands.append(tag)
        Path(socket_dir).mkdir(parents=True, exist_ok=True)
        Path(socket_dir, f"_stdout_{tag}").write_text('{"success":true,"data":{"typed":"secret"}}\n')
        Path(socket_dir, f"_stderr_{tag}").write_text("")
        return proc

    monkeypatch.setattr(session, "_popen_agent_browser", popen)
    return browser, session


def test_takeover_kills_inflight_agent_browser_fill_without_delivering_it(monkeypatch):
    proc = _BlockingProc()
    _browser, session = _wire_local_browser(monkeypatch, proc)
    result_box: list[dict] = []

    def _run():
        result_box.append(session._run_browser_command(
            "review", "fill", ["@e1", "secret-password"]))

    worker = threading.Thread(target=_run, name="ab-inflight-fill")
    worker.start()
    assert proc.started.wait(timeout=3.0), "agent-browser fill never started"
    lease.acquire("human")
    worker.join(timeout=3.0)
    assert not worker.is_alive(), "in-flight fill did not unblock after Take over"
    assert proc.killed >= 1
    assert result_box and result_box[0].get("code") == "human_has_control"
    assert "secret" not in json.dumps(result_box[0].get("data") or {})


def test_interrupt_spares_a_cloud_browser_cli(monkeypatch):
    from tools.browser_tool_session import interrupt_reserved_browser_cli, register_inflight_dock_cli

    dock = _BlockingProc()
    cloud = _BlockingProc()
    register_inflight_dock_cli(dock)
    # Cloud leftover is not registered — interrupt must not invent a kill.
    lease.acquire("human")
    interrupt_reserved_browser_cli()
    assert dock.killed >= 1
    assert cloud.killed == 0


def test_takeover_does_not_kill_inflight_cloud_cli(monkeypatch):
    """A Browserbase / other-Chrome session is not this screen's leftover writer."""
    proc = _BlockingProc()
    _browser, session = _wire_local_browser(monkeypatch, proc)
    monkeypatch.setattr(session, "_get_session_info", lambda *a: {
        "session_name": "cloud", "cdp_url": "wss://browserbase.example/cdp", "features": {},
    })
    result_box: list[dict] = []

    def _run():
        result_box.append(session._run_browser_command("review", "fill", ["@e1", "secret"]))

    worker = threading.Thread(target=_run, name="ab-cloud-fill")
    worker.start()
    assert proc.started.wait(timeout=3.0)
    lease.acquire("human")
    worker.join(timeout=0.4)
    assert worker.is_alive(), "cloud CLI was interrupted as if it were the dock"
    assert proc.killed == 0
    proc.kill()
    worker.join(timeout=3.0)
    assert not worker.is_alive()


def test_interrupt_spares_a_sibling_profile_cli(tmp_path: Path):
    from hermes_constants import reset_hermes_home_override, set_hermes_home_override
    from tools.browser_tool_session import interrupt_reserved_browser_cli, register_inflight_dock_cli

    home_a = tmp_path / "profile-a"
    home_b = tmp_path / "profile-b"
    home_a.mkdir()
    home_b.mkdir()
    proc_a = _BlockingProc()
    proc_b = _BlockingProc()
    register_inflight_dock_cli(proc_a, str(home_a))
    register_inflight_dock_cli(proc_b, str(home_b))

    token = set_hermes_home_override(home_a)
    try:
        lease.acquire("human")
        interrupt_reserved_browser_cli(home=str(home_a))
    finally:
        lease.release("human")
        reset_hermes_home_override(token)

    assert proc_a.killed == 1
    assert proc_b.killed == 0


def test_interrupt_is_noop_while_agent_holds():
    from tools.browser_tool_session import interrupt_reserved_browser_cli, register_inflight_dock_cli

    proc = _BlockingProc()
    register_inflight_dock_cli(proc)
    interrupt_reserved_browser_cli()
    assert proc.killed == 0


def test_takeover_kills_inflight_browser_exec_without_delivering_it(monkeypatch):
    from tools import browser_use_cli as bu
    from tools.browser_tool_session import _stamp_admitted

    proc = _BlockingProc()
    killed = []

    def fake_kill(p):
        killed.append(p)
        p.kill()

    monkeypatch.setattr(bu, "_find_cli", lambda: ["browser-use"])
    monkeypatch.setattr(bu, "_blocked_url_in_code", lambda code: None)
    monkeypatch.setattr(bu, "_route_backend", lambda env, *a, **k: env.__setitem__(
        "BU_CDP_WS", "ws://127.0.0.1:9333/devtools/browser/x") or None)
    monkeypatch.setattr(
        "tools.browser_tool_session._admit_shared_browser",
        lambda *a, **k: _stamp_admitted(lease.assert_agent_may_act()),
    )
    monkeypatch.setattr(bu, "_attach_vault_supervisor", lambda *a, **k: None)
    monkeypatch.setattr(bu.subprocess, "Popen", lambda *a, **k: proc)
    monkeypatch.setattr(bu, "_kill_cli_process_group", fake_kill)
    monkeypatch.setattr(bu, "_group_popen_kwargs", lambda: {})

    result_box: list[str] = []

    def _run():
        result_box.append(bu.browser_exec(
            "fill_input('#pw', 'secret-password')", task_id="review"))

    worker = threading.Thread(target=_run, name="bu-inflight-fill")
    worker.start()
    assert proc.started.wait(timeout=3.0), "browser_exec never started"
    lease.acquire("human")
    worker.join(timeout=3.0)
    assert not worker.is_alive(), "in-flight browser_exec did not unblock after Take over"
    assert killed and killed[0] is proc
    assert proc.killed >= 1
    payload = result_box[0] if result_box else ""
    assert "human_has_control" in payload
    assert "secret-password" not in payload
    assert "typed secret" not in payload


class _FakeProc:
    """psutil-shaped process for leftover CLI interrupt tests. No live scan."""

    def __init__(self, pid, cmdline, environ=None, cwd=None):
        self.pid = pid
        self._cmdline = list(cmdline)
        self._environ = dict(environ or {})
        self._cwd = cwd
        self.killed = 0

    def cmdline(self):
        return list(self._cmdline)

    def environ(self):
        return dict(self._environ)

    def cwd(self):
        if self._cwd is None:
            raise OSError("no cwd")
        return str(self._cwd)

    def kill(self):
        self.killed += 1


def test_agent_browser_invocation_is_token_match_not_substring():
    from tools.browser_tool_session import (
        _is_agent_browser_invocation,
        _is_browser_use_invocation,
    )

    assert _is_agent_browser_invocation(["/usr/bin/agent-browser", "open"])
    assert _is_agent_browser_invocation(["agent-browser.exe", "fill", "@e1", "x"])
    assert _is_agent_browser_invocation(["npx", "--yes", "agent-browser@latest", "fill"])
    assert _is_agent_browser_invocation(["pnpx", "agent-browser", "open"])
    assert not _is_agent_browser_invocation(["/usr/bin/cat", "agent-browser.log"])
    assert not _is_agent_browser_invocation(
        ["chrome", "--user-data-dir=/tmp/agent-browser"])
    assert not _is_agent_browser_invocation(["npx", "playwright", "install"])
    assert not _is_agent_browser_invocation(["agent-browser-mcp", "serve"])
    # Linux shebang: leftover writer is node, not argv0 agent-browser.
    assert _is_agent_browser_invocation(
        ["/usr/bin/node", "/usr/bin/agent-browser", "--cdp", "http://127.0.0.1:9333"])
    assert _is_agent_browser_invocation(
        ["node", "/home/x/node_modules/agent-browser/dist/cli.js", "fill"])
    assert _is_agent_browser_invocation(
        ["/usr/bin/env", "node", "/usr/bin/agent-browser", "fill"])
    assert not _is_agent_browser_invocation(
        ["node", "/tmp/other.js", "--cdp", "http://127.0.0.1:9333"])
    assert not _is_agent_browser_invocation(
        ["node", "/tmp/agent-browser/malware.js"])
    assert not _is_agent_browser_invocation(
        ["/bin/bash", "-c", "agent-browser --cdp http://127.0.0.1:9333 fill"])
    assert _is_browser_use_invocation(["browser-use", "exec"])
    assert _is_browser_use_invocation(["uvx", "browser-use"])
    assert _is_browser_use_invocation(["uvx", "--from", "browser-use==1", "browser-use"])
    assert _is_browser_use_invocation(["uv", "tool", "run", "browser-use"])
    assert _is_browser_use_invocation(["uv", "run", "browser-use"])
    assert _is_browser_use_invocation(
        ["python3", "/home/x/.hermes/bin/browser-use", "exec"])
    # Official leftover when the console script is missing: ``python -m``.
    assert _is_browser_use_invocation(["python3", "-m", "browser_use", "exec"])
    assert _is_browser_use_invocation(
        ["/usr/bin/python3", "-m", "browser_use", "--cdp-url",
         "http://127.0.0.1:9333"])
    assert _is_browser_use_invocation(["python3.12", "-m", "browser_use"])
    assert _is_browser_use_invocation(["python", "-m", "browser_use"])
    assert not _is_browser_use_invocation(["python3", "-m", "ruff"])
    assert not _is_browser_use_invocation(["python3", "-m", "browser_use_cli"])
    assert not _is_browser_use_invocation(["python3", "-m", "browser_usage"])
    assert not _is_browser_use_invocation(["python3", "/tmp/browser_use"])
    assert not _is_browser_use_invocation(
        ["/bin/bash", "-c",
         "python3 -m browser_use --cdp-url http://127.0.0.1:9333"])
    assert not _is_browser_use_invocation(["/usr/bin/cat", "browser-use.log"])
    assert not _is_browser_use_invocation(["uvx", "ruff", "check"])
    from tools.browser_tool_session import _unregistered_cli_aims_at_dock
    # Official leftover attach is ``--cdp-url`` (finding 110). Env-only
    # hid that writer. ``--connect`` cannot prove this jar.
    assert _unregistered_cli_aims_at_dock(
        ["browser-use", "--cdp-url", "http://127.0.0.1:9333", "open"],
        {}, None, 9333,
    )
    assert _unregistered_cli_aims_at_dock(
        ["uvx", "browser-use", "--cdp-url=ws://127.0.0.1:9333/devtools/browser/x"],
        {}, None, 9333,
    )
    assert not _unregistered_cli_aims_at_dock(
        ["browser-use", "--cdp-url", "http://127.0.0.1:9222", "open"],
        {"BU_CDP_URL": "http://127.0.0.1:9333"}, None, 9333,
    )
    assert not _unregistered_cli_aims_at_dock(
        ["browser-use", "--connect", "open", "https://example.com"],
        {}, None, 9333,
    )
    from tools.bot_desktop import browser as _bdb
    _jar = _bdb.profile_dir()
    # Official leftover jar pin is ``--profile`` / ``--user-data-dir``
    # (finding 111). Env-only hid those writers. ``--cdp`` still wins.
    assert _unregistered_cli_aims_at_dock(
        ["agent-browser", "--profile", str(_jar), "snapshot"],
        {}, _jar, None,
    )
    # Official leftover expands ``~/`` on --profile / AGENT_BROWSER_PROFILE
    # (finding 129). Finding 111 compared the literal ``~/…`` path.
    # Chromium / Playwright / lighthouse do not expand --user-data-dir.
    from hermes_constants import get_hermes_home as _get_home
    _ab_home = {"HOME": str(_get_home())}
    assert _unregistered_cli_aims_at_dock(
        ["agent-browser", "--profile", "~/bot-desktop/browser-profile",
         "snapshot"],
        _ab_home, _jar, None,
    )
    assert _unregistered_cli_aims_at_dock(
        ["agent-browser",
         "--profile=~/bot-desktop/browser-profile", "snapshot"],
        _ab_home, _jar, None,
    )
    assert _unregistered_cli_aims_at_dock(
        ["agent-browser", "snapshot"],
        {**_ab_home, "AGENT_BROWSER_PROFILE": "~/bot-desktop/browser-profile"},
        _jar, None,
    )
    assert not _unregistered_cli_aims_at_dock(
        ["agent-browser", "--profile", "~/other-chrome", "snapshot"],
        _ab_home, _jar, None,
    )
    assert not _unregistered_cli_aims_at_dock(
        ["npx", "playwright", "codegen",
         "--user-data-dir", "~/bot-desktop/browser-profile"],
        _ab_home, _jar, None,
    )
    assert not _unregistered_cli_aims_at_dock(
        ["chrome-devtools", "start",
         "--userDataDir", "~/bot-desktop/browser-profile"],
        _ab_home, _jar, None,
    )
    assert not _unregistered_cli_aims_at_dock(
        ["npx", "lighthouse", "https://example.com",
         "--chrome-flags",
         "--user-data-dir=~/bot-desktop/browser-profile"],
        _ab_home, _jar, None,
    )
    assert not _unregistered_cli_aims_at_dock(
        ["agent-browser", "--cdp", "http://127.0.0.1:9222",
         "--profile", "~/bot-desktop/browser-profile", "snapshot"],
        _ab_home, _jar, 9333,
    )
    # Official leftover also forwards Chromium --user-data-dir via
    # --args / AGENT_BROWSER_ARGS (finding 130). Finding 111 only
    # checked --profile. CLI --args overrides the env key. --cdp
    # still wins. Playwright launch last-wins the dock jar.
    assert _unregistered_cli_aims_at_dock(
        ["agent-browser", "--args", f"--user-data-dir={_jar}", "fill"],
        {}, _jar, None,
    )
    assert _unregistered_cli_aims_at_dock(
        ["agent-browser", f"--args=--user-data-dir={_jar}", "fill"],
        {}, _jar, None,
    )
    assert _unregistered_cli_aims_at_dock(
        ["agent-browser", "--args",
         f"--headless,--user-data-dir={_jar}", "fill"],
        {}, _jar, None,
    )
    assert _unregistered_cli_aims_at_dock(
        ["agent-browser", "fill"],
        {"AGENT_BROWSER_ARGS": f"--user-data-dir={_jar}"}, _jar, None,
    )
    assert _unregistered_cli_aims_at_dock(
        ["agent-browser", "fill"],
        {"AGENT_BROWSER_CHROME_FLAGS": f"--user-data-dir={_jar}"},
        _jar, None,
    )
    assert _unregistered_cli_aims_at_dock(
        ["agent-browser", "--args", "--user-data-dir=browser-profile",
         "fill"],
        {}, _jar, None, cwd=_jar.parent,
    )
    assert _unregistered_cli_aims_at_dock(
        ["agent-browser", "--args",
         f"--user-data-dir=/tmp/other --user-data-dir={_jar}", "fill"],
        {}, _jar, None,
    )
    assert _unregistered_cli_aims_at_dock(
        ["agent-browser", "--profile", "/tmp/other-chrome",
         "--args", f"--user-data-dir={_jar}", "fill"],
        {}, _jar, None,
    )
    assert not _unregistered_cli_aims_at_dock(
        ["agent-browser", "--args", "--user-data-dir=/tmp/other-chrome",
         "fill"],
        {}, _jar, None,
    )
    assert not _unregistered_cli_aims_at_dock(
        ["agent-browser", "--args", "--headless", "fill"],
        {"AGENT_BROWSER_ARGS": f"--user-data-dir={_jar}"}, _jar, None,
    )
    assert not _unregistered_cli_aims_at_dock(
        ["agent-browser", "--cdp", "http://127.0.0.1:9222",
         "--args", f"--user-data-dir={_jar}", "fill"],
        {}, _jar, 9333,
    )
    assert _unregistered_cli_aims_at_dock(
        ["npx", "playwright", "codegen", "--user-data-dir", str(_jar)],
        {}, _jar, None,
    )
    assert _unregistered_cli_aims_at_dock(
        ["playwright-cli", "open", f"--profile={_jar}"],
        {}, _jar, None,
    )
    assert not _unregistered_cli_aims_at_dock(
        ["agent-browser", "--cdp", "http://127.0.0.1:9222",
         "--profile", str(_jar), "snapshot"],
        {}, _jar, 9333,
    )
    assert not _unregistered_cli_aims_at_dock(
        ["playwright-cli", "attach", "--cdp", "chrome",
         "--profile", str(_jar)],
        {}, _jar, 9333,
    )
    # Official MCP leftover also pins off argv (finding 127).
    # PLAYWRIGHT_MCP_USER_DATA_DIR / --config browser.userDataDir /
    # cdpEndpoint. Regular Playwright CLI does not read those keys.
    _mcp_cfg = _jar.parent / "pw-mcp-dock.json"
    _mcp_cfg.parent.mkdir(parents=True, exist_ok=True)
    _mcp_cfg.write_text(json.dumps({"browser": {"userDataDir": str(_jar)}}))
    _mcp_cdp = _jar.parent / "pw-mcp-cdp.json"
    _mcp_cdp.write_text(json.dumps({
        "browser": {"cdpEndpoint": "http://127.0.0.1:9333"},
    }))
    _mcp_other = _jar.parent / "pw-mcp-other.json"
    _mcp_other.write_text(json.dumps({
        "browser": {"userDataDir": "/tmp/other-chrome"},
    }))
    assert _unregistered_cli_aims_at_dock(
        ["npx", "@playwright/mcp"],
        {"PLAYWRIGHT_MCP_USER_DATA_DIR": str(_jar)}, _jar, None,
    )
    assert _unregistered_cli_aims_at_dock(
        ["npx", "@playwright/mcp"],
        {"PLAYWRIGHT_MCP_USER_DATA_DIR": "browser-profile"},
        _jar, None, cwd=_jar.parent,
    )
    assert _unregistered_cli_aims_at_dock(
        ["npx", "@playwright/mcp", "--config", str(_mcp_cfg)],
        {}, _jar, None,
    )
    assert _unregistered_cli_aims_at_dock(
        ["npx", "@playwright/mcp"],
        {"PLAYWRIGHT_MCP_CONFIG": str(_mcp_cfg)}, _jar, None,
    )
    assert _unregistered_cli_aims_at_dock(
        ["npx", "@playwright/mcp", "--config", "pw-mcp-dock.json"],
        {}, _jar, None, cwd=_jar.parent,
    )
    assert _unregistered_cli_aims_at_dock(
        ["npx", "@playwright/mcp", "--config", str(_mcp_cdp)],
        {}, _jar, 9333,
    )
    assert not _unregistered_cli_aims_at_dock(
        ["npx", "playwright", "codegen"],
        {"PLAYWRIGHT_MCP_USER_DATA_DIR": str(_jar)}, _jar, None,
    )
    assert not _unregistered_cli_aims_at_dock(
        ["npx", "@playwright/mcp"],
        {"PLAYWRIGHT_MCP_USER_DATA_DIR": "/tmp/other-chrome"}, _jar, None,
    )
    assert not _unregistered_cli_aims_at_dock(
        ["npx", "@playwright/mcp", "--config", str(_mcp_other)],
        {}, _jar, None,
    )
    assert not _unregistered_cli_aims_at_dock(
        ["npx", "@playwright/mcp", "--isolated"],
        {}, _jar, None,
    )
    assert not _unregistered_cli_aims_at_dock(
        ["npx", "@playwright/mcp",
         "--cdp-endpoint", "http://127.0.0.1:9222"],
        {"PLAYWRIGHT_MCP_USER_DATA_DIR": str(_jar)}, _jar, 9333,
    )
    # Leftover daemon freezes AGENT_BROWSER_CDP; connect <port> is the
    # in-flight attach (finding 112). --auto-connect cannot prove this jar.
    assert _unregistered_cli_aims_at_dock(
        ["agent-browser", "snapshot"],
        {"AGENT_BROWSER_CDP": "9333"}, None, 9333,
    )
    assert _unregistered_cli_aims_at_dock(
        ["agent-browser", "connect", "9333"],
        {}, None, 9333,
    )
    assert _unregistered_cli_aims_at_dock(
        ["agent-browser", "--session", "foo", "connect",
         "http://127.0.0.1:9333"],
        {}, None, 9333,
    )
    assert not _unregistered_cli_aims_at_dock(
        ["agent-browser", "--auto-connect", "snapshot"],
        {}, None, 9333,
    )
    assert not _unregistered_cli_aims_at_dock(
        ["agent-browser", "--cdp", "9222", "snapshot"],
        {"AGENT_BROWSER_CDP": "9333"}, None, 9333,
    )
    from tools.browser_tool_session import _is_playwright_invocation
    assert _is_playwright_invocation(["playwright", "codegen"])
    assert _is_playwright_invocation(["npx", "--yes", "playwright", "install"])
    assert _is_playwright_invocation(
        ["node", "/home/x/node_modules/playwright/cli.js", "codegen"])
    assert not _is_playwright_invocation(
        ["node", "/home/x/node_modules/@playwright/mcp/cli.js"])
    assert not _is_playwright_invocation(["/usr/bin/cat", "playwright.log"])
    assert not _is_playwright_invocation(
        ["/bin/bash", "-c", "npx playwright codegen --cdp-endpoint http://127.0.0.1:9333"])
    # Playwright Agent CLI leftover attach (finding 109).
    assert _is_playwright_invocation(
        ["playwright-cli", "attach", "--cdp", "http://127.0.0.1:9333"])
    assert _is_playwright_invocation(
        ["npx", "playwright-cli", "attach", "--cdp", "http://127.0.0.1:9333"])
    assert _is_playwright_invocation(
        ["npx", "--package=playwright-cli", "--",
         "attach", "--cdp", "http://127.0.0.1:9333"])
    assert _is_playwright_invocation(
        ["node", "/home/x/node_modules/playwright-cli/cli.js",
         "attach", "--cdp=http://127.0.0.1:9333"])
    assert _is_playwright_invocation(
        ["npm", "exec", "playwright-cli", "--",
         "attach", "--cdp", "http://127.0.0.1:9333"])
    assert not _is_playwright_invocation(
        ["npx", "playwright-core", "attach", "--cdp", "http://127.0.0.1:9333"])
    assert not _is_playwright_invocation(
        ["/bin/bash", "-c", "playwright-cli attach --cdp http://127.0.0.1:9333"])
    from tools.browser_tool_session import _is_playwright_mcp_invocation
    assert _is_playwright_mcp_invocation(["npx", "-y", "@playwright/mcp@latest"])
    assert _is_playwright_mcp_invocation(
        ["node", "/home/x/node_modules/@playwright/mcp/cli.js"])
    assert _is_playwright_mcp_invocation(
        ["/usr/bin/env", "node", "/home/x/node_modules/@playwright/mcp/cli.js"])
    assert not _is_playwright_mcp_invocation(
        ["node", "/home/x/node_modules/playwright/cli.js"])
    assert not _is_playwright_mcp_invocation(["npx", "playwright", "codegen"])
    assert not _is_playwright_mcp_invocation(["/usr/bin/mcp"])
    assert not _is_playwright_mcp_invocation(
        ["/bin/bash", "-c", "npx @playwright/mcp --cdp-endpoint http://127.0.0.1:9333"])
    from tools.browser_tool_session import _is_chrome_remote_interface_invocation
    assert _is_chrome_remote_interface_invocation(
        ["npx", "chrome-remote-interface", "--port", "9333", "inspect"])
    assert _is_chrome_remote_interface_invocation(
        ["node", "/home/x/node_modules/chrome-remote-interface/bin/client.js"])
    assert not _is_chrome_remote_interface_invocation(
        ["node", "/tmp/cdp-debug.js"])
    assert not _is_chrome_remote_interface_invocation(
        ["/bin/bash", "-c", "npx chrome-remote-interface --port 9333 inspect"])
    from tools.browser_tool_session import _is_chrome_devtools_mcp_invocation
    assert _is_chrome_devtools_mcp_invocation(
        ["npx", "-y", "chrome-devtools-mcp@latest", "--browserUrl", "http://127.0.0.1:9333"])
    assert _is_chrome_devtools_mcp_invocation(
        ["node", "/home/x/node_modules/chrome-devtools-mcp/build/src/index.js"])
    # Official leftover CLI bin from the same package (finding 124).
    assert _is_chrome_devtools_mcp_invocation(["npx", "chrome-devtools"])
    assert _is_chrome_devtools_mcp_invocation(
        ["chrome-devtools", "start", "--userDataDir", "/tmp/jar"])
    assert _is_chrome_devtools_mcp_invocation(
        ["node", "/home/x/node_modules/chrome-devtools-mcp/build/src/bin/chrome-devtools.js",
         "start", "--browserUrl", "http://127.0.0.1:9333"])
    assert not _is_chrome_devtools_mcp_invocation(["npx", "chrome-devtools-frontend"])
    assert not _is_chrome_devtools_mcp_invocation(
        ["/bin/bash", "-c", "npx chrome-devtools-mcp --browserUrl http://127.0.0.1:9333"])
    assert _is_chrome_devtools_mcp_invocation(
        ["pnpm", "exec", "chrome-devtools-mcp", "--browserUrl", "http://127.0.0.1:9333"])
    assert _is_chrome_devtools_mcp_invocation(
        ["npm", "exec", "--package=chrome-devtools-mcp", "--",
         "--browserUrl", "http://127.0.0.1:9333"])
    # Official leftover launch pin (finding 123). Conflicts with attach
    # flags. ``--autoConnect`` / other jar / attach-to-other still win.
    assert _unregistered_cli_aims_at_dock(
        ["npx", "chrome-devtools-mcp", "--userDataDir", str(_jar)],
        {}, _jar, None,
    )
    assert _unregistered_cli_aims_at_dock(
        ["npx", "chrome-devtools-mcp", f"--user-data-dir={_jar}"],
        {}, _jar, None,
    )
    assert _unregistered_cli_aims_at_dock(
        ["npx", "chrome-devtools-mcp", "--userDataDir", "browser-profile"],
        {}, _jar, None, cwd=_jar.parent,
    )
    assert _unregistered_cli_aims_at_dock(
        ["npm", "exec", "--package=chrome-devtools-mcp", "--",
         "--userDataDir", str(_jar)],
        {}, _jar, None,
    )
    assert not _unregistered_cli_aims_at_dock(
        ["npx", "chrome-devtools-mcp", "--userDataDir", "/tmp/other-chrome"],
        {}, _jar, None,
    )
    assert not _unregistered_cli_aims_at_dock(
        ["npx", "chrome-devtools-mcp", "--autoConnect"],
        {}, _jar, None,
    )
    assert not _unregistered_cli_aims_at_dock(
        ["npx", "chrome-devtools-mcp", "--isolated"],
        {}, _jar, None,
    )
    assert not _unregistered_cli_aims_at_dock(
        ["npx", "chrome-devtools-mcp",
         "--browserUrl", "http://127.0.0.1:9222",
         "--userDataDir", str(_jar)],
        {}, _jar, 9333,
    )
    # Official leftover CLI wrapper (finding 124). fill / no pin stay unknown.
    assert _unregistered_cli_aims_at_dock(
        ["chrome-devtools", "start", "--userDataDir", str(_jar)],
        {}, _jar, None,
    )
    assert _unregistered_cli_aims_at_dock(
        ["npx", "chrome-devtools", "start",
         "--browserUrl", "http://127.0.0.1:9333"],
        {}, _jar, 9333,
    )
    assert _unregistered_cli_aims_at_dock(
        ["node", "/home/x/node_modules/chrome-devtools-mcp/build/src/bin/chrome-devtools.js",
         "start", f"--user-data-dir={_jar}"],
        {}, _jar, None,
    )
    assert not _unregistered_cli_aims_at_dock(
        ["chrome-devtools", "fill", "1", "uid", "secret"],
        {}, _jar, 9333,
    )
    assert not _unregistered_cli_aims_at_dock(
        ["chrome-devtools", "start", "--userDataDir", "/tmp/other-chrome"],
        {}, _jar, None,
    )
    # Official leftover also hides the pin off argv (finding 131).
    # ``--chrome-arg=--user-data-dir`` / ``--config`` flat userDataDir /
    # browserUrl / chromeArg. Finding 123 only checked argv
    # ``--userDataDir``. yargs CLI still overrides the file per key.
    _cd_cfg = _jar.parent / "cd-mcp-dock.json"
    _cd_cfg.parent.mkdir(parents=True, exist_ok=True)
    _cd_cfg.write_text(json.dumps({"userDataDir": str(_jar)}))
    _cd_cdp = _jar.parent / "cd-mcp-cdp.json"
    _cd_cdp.write_text(json.dumps({"browserUrl": "http://127.0.0.1:9333"}))
    _cd_arg = _jar.parent / "cd-mcp-arg.json"
    _cd_arg.write_text(json.dumps({
        "chromeArg": [f"--user-data-dir={_jar}"],
    }))
    _cd_other = _jar.parent / "cd-mcp-other.json"
    _cd_other.write_text(json.dumps({"userDataDir": "/tmp/other-chrome"}))
    _cd_kebab = _jar.parent / "cd-mcp-kebab.json"
    _cd_kebab.write_text(json.dumps({"user-data-dir": str(_jar)}))
    assert _unregistered_cli_aims_at_dock(
        ["npx", "chrome-devtools-mcp",
         f"--chrome-arg=--user-data-dir={_jar}"],
        {}, _jar, None,
    )
    assert _unregistered_cli_aims_at_dock(
        ["npx", "chrome-devtools-mcp",
         f"--chromeArg=--user-data-dir={_jar}"],
        {}, _jar, None,
    )
    assert _unregistered_cli_aims_at_dock(
        ["npx", "chrome-devtools-mcp", "--chromeArg", "--headless",
         "--chromeArg", f"--user-data-dir={_jar}"],
        {}, _jar, None,
    )
    assert _unregistered_cli_aims_at_dock(
        ["npx", "chrome-devtools-mcp",
         f"--chromeArg=--user-data-dir={_jar}",
         "--chromeArg", "--headless"],
        {}, _jar, None,
    )
    assert _unregistered_cli_aims_at_dock(
        ["npx", "chrome-devtools-mcp", "--userDataDir", "/tmp/other-chrome",
         f"--chrome-arg=--user-data-dir={_jar}"],
        {}, _jar, None,
    )
    assert _unregistered_cli_aims_at_dock(
        ["npx", "chrome-devtools-mcp", "--config", str(_cd_cfg)],
        {}, _jar, None,
    )
    assert _unregistered_cli_aims_at_dock(
        ["npx", "chrome-devtools-mcp", "--config", "cd-mcp-dock.json"],
        {}, _jar, None, cwd=_jar.parent,
    )
    assert _unregistered_cli_aims_at_dock(
        ["npx", "chrome-devtools-mcp", "--config", str(_cd_cdp)],
        {}, _jar, 9333,
    )
    assert _unregistered_cli_aims_at_dock(
        ["npx", "chrome-devtools-mcp", "--config", str(_cd_arg)],
        {}, _jar, None,
    )
    assert _unregistered_cli_aims_at_dock(
        ["npx", "chrome-devtools-mcp", "--config", str(_cd_kebab)],
        {}, _jar, None,
    )
    assert _unregistered_cli_aims_at_dock(
        ["chrome-devtools", "start", "--config", str(_cd_cfg)],
        {}, _jar, None,
    )
    assert _unregistered_cli_aims_at_dock(
        ["npx", "chrome-devtools-mcp", "--chromeArg", "--headless",
         "--config", str(_cd_cfg)],
        {}, _jar, None,
    )
    assert not _unregistered_cli_aims_at_dock(
        ["npx", "chrome-devtools-mcp",
         "--chrome-arg=--user-data-dir=/tmp/other-chrome"],
        {}, _jar, None,
    )
    assert not _unregistered_cli_aims_at_dock(
        ["npx", "chrome-devtools-mcp", "--config", str(_cd_other)],
        {}, _jar, None,
    )
    assert not _unregistered_cli_aims_at_dock(
        ["npx", "chrome-devtools-mcp", "--userDataDir", "/tmp/other-chrome",
         "--config", str(_cd_cfg)],
        {}, _jar, None,
    )
    assert not _unregistered_cli_aims_at_dock(
        ["npx", "chrome-devtools-mcp", "--chromeArg", "--headless",
         "--config", str(_cd_arg)],
        {}, _jar, None,
    )
    assert not _unregistered_cli_aims_at_dock(
        ["npx", "chrome-devtools-mcp",
         "--browserUrl", "http://127.0.0.1:9222",
         "--config", str(_cd_cfg)],
        {}, _jar, 9333,
    )
    assert not _unregistered_cli_aims_at_dock(
        ["npx", "@playwright/mcp", "--config", str(_cd_cfg)],
        {}, _jar, None,
    )
    # Official leftover launch pin (finding 126). chrome-launcher
    # appends ``--chrome-flags`` after its temp dir; Chromium last-wins
    # the dock jar. ``--port`` attach still wins. LAN / other jar /
    # ``--remote-debugging-port`` inside chrome-flags stay unknown.
    assert _unregistered_cli_aims_at_dock(
        ["npx", "lighthouse", "https://example.com",
         "--chrome-flags", f"--user-data-dir={_jar}"],
        {}, _jar, None,
    )
    assert _unregistered_cli_aims_at_dock(
        ["npx", "lighthouse", "https://example.com",
         f"--chrome-flags=--user-data-dir={_jar}"],
        {}, _jar, None,
    )
    assert _unregistered_cli_aims_at_dock(
        ["npx", "lighthouse", "https://example.com",
         "--chrome-flags", f"--headless --user-data-dir={_jar}"],
        {}, _jar, None,
    )
    assert _unregistered_cli_aims_at_dock(
        ["npx", "lighthouse", "https://example.com",
         "--chromeFlags", f"--user-data-dir={_jar}"],
        {}, _jar, None,
    )
    assert _unregistered_cli_aims_at_dock(
        ["npx", "lighthouse", "https://example.com", "--port=0",
         "--chrome-flags", f"--user-data-dir={_jar}"],
        {}, _jar, None,
    )
    assert _unregistered_cli_aims_at_dock(
        ["npx", "lighthouse", "https://example.com",
         "--chrome-flags", "--user-data-dir=browser-profile"],
        {}, _jar, None, cwd=_jar.parent,
    )
    assert _unregistered_cli_aims_at_dock(
        ["npx", "lighthouse", "https://example.com",
         "--chrome-flags",
         f"--user-data-dir=/tmp/other --user-data-dir={_jar}"],
        {}, _jar, None,
    )
    assert not _unregistered_cli_aims_at_dock(
        ["npx", "lighthouse", "https://example.com",
         "--chrome-flags", "--user-data-dir=/tmp/other-chrome"],
        {}, _jar, None,
    )
    assert not _unregistered_cli_aims_at_dock(
        ["npx", "lighthouse", "https://example.com",
         "--chrome-flags", "--remote-debugging-port=9333"],
        {}, _jar, 9333,
    )
    assert not _unregistered_cli_aims_at_dock(
        ["npx", "lighthouse", "--port", "9222",
         "--chrome-flags", f"--user-data-dir={_jar}",
         "https://example.com"],
        {}, _jar, 9333,
    )
    assert not _unregistered_cli_aims_at_dock(
        ["npx", "lighthouse", "--hostname", "10.0.0.5",
         "--chrome-flags", f"--user-data-dir={_jar}",
         "https://example.com"],
        {}, _jar, None,
    )
    assert not _unregistered_cli_aims_at_dock(
        ["npx", "lighthouse", "https://example.com"],
        {}, _jar, None,
    )
    # Official leftover also hides --port / --chrome-flags in
    # --cli-flags-path JSON (finding 128). Finding 126 only checked
    # argv. CLI flags still override the file. --config-path is
    # audit config, not this file.
    _lh_flags = _jar.parent / "lh-flags.json"
    _lh_flags.parent.mkdir(parents=True, exist_ok=True)
    _lh_flags.write_text(json.dumps({
        "chromeFlags": f"--user-data-dir={_jar}",
        "quiet": True,
    }))
    _lh_kebab = _jar.parent / "lh-kebab.json"
    _lh_kebab.write_text(json.dumps({
        "chrome-flags": f"--headless --user-data-dir={_jar}",
    }))
    _lh_port = _jar.parent / "lh-port.json"
    _lh_port.write_text(json.dumps({"port": 9333}))
    _lh_rel = _jar.parent / "lh-rel.json"
    _lh_rel.write_text(json.dumps({
        "chromeFlags": "--user-data-dir=browser-profile",
    }))
    _lh_other = _jar.parent / "lh-other.json"
    _lh_other.write_text(json.dumps({
        "chromeFlags": "--user-data-dir=/tmp/other-chrome",
    }))
    _lh_lan = _jar.parent / "lh-lan.json"
    _lh_lan.write_text(json.dumps({
        "hostname": "10.0.0.5",
        "chromeFlags": f"--user-data-dir={_jar}",
    }))
    _lh_audit = _jar.parent / "lh-audit.json"
    _lh_audit.write_text(json.dumps({
        "chromeFlags": f"--user-data-dir={_jar}",
    }))
    assert _unregistered_cli_aims_at_dock(
        ["npx", "lighthouse", "https://example.com",
         "--cli-flags-path", str(_lh_flags)],
        {}, _jar, None,
    )
    assert _unregistered_cli_aims_at_dock(
        ["lighthouse", "https://example.com",
         f"--cli-flags-path={_lh_flags}"],
        {}, _jar, None,
    )
    assert _unregistered_cli_aims_at_dock(
        ["npx", "lighthouse", "https://example.com",
         "--cliFlagsPath", str(_lh_flags)],
        {}, _jar, None,
    )
    assert _unregistered_cli_aims_at_dock(
        ["npx", "lighthouse", "https://example.com",
         "--cli-flags-path", str(_lh_kebab)],
        {}, _jar, None,
    )
    assert _unregistered_cli_aims_at_dock(
        ["npx", "lighthouse", "https://example.com",
         "--cli-flags-path", "lh-rel.json"],
        {}, _jar, None, cwd=_jar.parent,
    )
    assert _unregistered_cli_aims_at_dock(
        ["npx", "lighthouse", "https://example.com",
         "--cli-flags-path", str(_lh_port)],
        {}, _jar, 9333,
    )
    assert _unregistered_cli_aims_at_dock(
        ["npx", "lighthouse", "--port=0", "https://example.com",
         "--cli-flags-path", str(_lh_flags)],
        {}, _jar, None,
    )
    assert not _unregistered_cli_aims_at_dock(
        ["npx", "lighthouse", "https://example.com",
         "--cli-flags-path", str(_lh_other)],
        {}, _jar, None,
    )
    assert not _unregistered_cli_aims_at_dock(
        ["npx", "lighthouse", "https://example.com",
         "--cli-flags-path", str(_lh_lan)],
        {}, _jar, None,
    )
    assert not _unregistered_cli_aims_at_dock(
        ["npx", "lighthouse", "--port", "9222",
         "--cli-flags-path", str(_lh_flags), "https://example.com"],
        {}, _jar, 9333,
    )
    assert not _unregistered_cli_aims_at_dock(
        ["npx", "lighthouse", "--chrome-flags",
         "--user-data-dir=/tmp/other-chrome",
         "--cli-flags-path", str(_lh_flags), "https://example.com"],
        {}, _jar, None,
    )
    assert not _unregistered_cli_aims_at_dock(
        ["npx", "lighthouse", "https://example.com",
         "--config-path", str(_lh_audit)],
        {}, _jar, None,
    )
    # Official leftover joins every --chrome-flags group and accepts
    # chromeFlags as an array (finding 132). Finding 126 / 128
    # last-wins / string-only dropped the dock pin when a later
    # group was --headless or the file used ["--user-data-dir"].
    _lh_arr = _jar.parent / "lh-arr.json"
    _lh_arr.write_text(json.dumps({
        "chromeFlags": [f"--user-data-dir={_jar}"],
    }))
    _lh_arr2 = _jar.parent / "lh-arr2.json"
    _lh_arr2.write_text(json.dumps({
        "chromeFlags": ["--headless", f"--user-data-dir={_jar}"],
    }))
    _lh_arr_other = _jar.parent / "lh-arr-other.json"
    _lh_arr_other.write_text(json.dumps({
        "chromeFlags": ["--user-data-dir=/tmp/other-chrome"],
    }))
    assert _unregistered_cli_aims_at_dock(
        ["npx", "lighthouse", "https://example.com",
         "--chrome-flags", f"--user-data-dir={_jar}",
         "--chrome-flags", "--headless"],
        {}, _jar, None,
    )
    assert _unregistered_cli_aims_at_dock(
        ["npx", "lighthouse", "https://example.com",
         f"--chrome-flags=--user-data-dir={_jar}",
         "--chrome-flags=--headless"],
        {}, _jar, None,
    )
    assert _unregistered_cli_aims_at_dock(
        ["npx", "lighthouse", "https://example.com",
         "--chrome-flags", "--headless",
         "--chrome-flags", f"--user-data-dir={_jar}"],
        {}, _jar, None,
    )
    assert _unregistered_cli_aims_at_dock(
        ["npx", "lighthouse", "https://example.com",
         "--cli-flags-path", str(_lh_arr)],
        {}, _jar, None,
    )
    assert _unregistered_cli_aims_at_dock(
        ["npx", "lighthouse", "https://example.com",
         "--cli-flags-path", str(_lh_arr2)],
        {}, _jar, None,
    )
    assert not _unregistered_cli_aims_at_dock(
        ["npx", "lighthouse", "https://example.com",
         "--cli-flags-path", str(_lh_arr_other)],
        {}, _jar, None,
    )
    assert not _unregistered_cli_aims_at_dock(
        ["npx", "lighthouse", "--chrome-flags", "--headless",
         "--cli-flags-path", str(_lh_arr), "https://example.com"],
        {}, _jar, None,
    )
    assert _is_playwright_invocation(
        ["npm", "exec", "--", "playwright", "codegen"])
    assert _is_playwright_mcp_invocation(
        ["yarn", "dlx", "@playwright/mcp@latest"])
    assert _is_chrome_remote_interface_invocation(
        ["pnpm", "dlx", "chrome-remote-interface", "--port", "9333"])
    assert _is_agent_browser_invocation(
        ["npm", "x", "agent-browser", "fill"])
    assert not _is_chrome_devtools_mcp_invocation(
        ["pnpm", "run", "chrome-devtools-mcp"])
    assert not _is_chrome_devtools_mcp_invocation(
        ["npm", "install", "chrome-devtools-mcp"])
    assert not _is_playwright_invocation(["yarn", "add", "playwright"])
    assert not _is_chrome_devtools_mcp_invocation(["pnpm", "exec", "ruff"])
    from tools.browser_tool_session import _is_lighthouse_invocation
    assert _is_lighthouse_invocation(
        ["npx", "-y", "lighthouse@latest", "--port", "9333", "https://example.com"])
    assert _is_lighthouse_invocation(
        ["npx", "--prefix", "/tmp/ws", "lighthouse",
         "--port", "9333", "https://example.com"])
    assert _is_lighthouse_invocation(
        ["npx", "-y", "--prefix", "/tmp/ws", "lighthouse@latest",
         "--port", "9333"])
    assert _is_lighthouse_invocation(
        ["pnpx", "--prefix", "/tmp/ws", "lighthouse", "--port", "9333"])
    assert _is_lighthouse_invocation(
        ["bunx", "--cwd", "/tmp/ws", "lighthouse", "--port", "9333"])
    assert _is_chrome_devtools_mcp_invocation(
        ["npx", "--prefix", "/tmp/ws", "chrome-devtools-mcp",
         "--browserUrl", "http://127.0.0.1:9333"])
    assert _is_playwright_invocation(
        ["npx", "--prefix", "/tmp/ws", "playwright", "codegen"])
    assert _is_agent_browser_invocation(
        ["npx", "--prefix", "/tmp/ws", "agent-browser", "fill"])
    # ``-p`` is the package pin, not a prefix. Do not swallow the binary.
    assert _is_lighthouse_invocation(
        ["npx", "-p", "lighthouse@latest", "lighthouse", "--port", "9333"])
    assert _is_lighthouse_invocation(
        ["npx", "-p", "lighthouse", "--port", "9333"])
    # Workspace selectors used to become the package/command (finding 104).
    assert _is_lighthouse_invocation(
        ["npx", "--workspace", "web", "lighthouse", "--port", "9333"])
    assert _is_lighthouse_invocation(
        ["npx", "-w", "web", "lighthouse", "--port", "9333"])
    assert _is_lighthouse_invocation(
        ["npm", "-w", "@scope/web", "exec", "lighthouse", "--port", "9333"])
    assert _is_lighthouse_invocation(
        ["npm", "--workspace", "@scope/web", "exec", "lighthouse", "--port", "9333"])
    assert _is_lighthouse_invocation(
        ["yarn", "workspace", "web", "exec", "lighthouse", "--port", "9333"])
    assert _is_lighthouse_invocation(
        ["yarn", "workspace", "web", "exec", "--", "lighthouse", "--port", "9333"])
    assert _is_chrome_devtools_mcp_invocation(
        ["yarn", "workspace", "web", "exec", "chrome-devtools-mcp",
         "--browserUrl", "http://127.0.0.1:9333"])
    # pnpx ``-w`` is boolean ``--workspace-root`` — do not swallow the binary.
    assert _is_lighthouse_invocation(
        ["pnpx", "-w", "lighthouse", "--port", "9333"])
    assert not _is_lighthouse_invocation(
        ["yarn", "workspace", "web", "run", "lighthouse"])
    assert not _is_lighthouse_invocation(
        ["yarn", "workspaces", "foreach", "exec", "lighthouse"])
    # ``npx --package=lighthouse -- --port`` hid invocation and aim (105).
    assert _is_lighthouse_invocation(
        ["npx", "--package=lighthouse", "--", "--port", "9333"])
    assert _is_lighthouse_invocation(
        ["npx", "--package=lighthouse", "--port", "9333"])
    assert _is_lighthouse_invocation(
        ["npx", "--package", "lighthouse", "--", "--port", "9333"])
    assert _is_lighthouse_invocation(
        ["npx", "-p", "lighthouse", "--", "--port", "9333"])
    assert _is_lighthouse_invocation(
        ["pnpx", "--package=lighthouse", "--port", "9333"])
    assert _is_chrome_devtools_mcp_invocation(
        ["npx", "--package=chrome-devtools-mcp", "--",
         "--browserUrl", "http://127.0.0.1:9333"])
    assert _is_playwright_mcp_invocation(
        ["npx", "--package=@playwright/mcp", "--",
         "--cdp-endpoint", "http://127.0.0.1:9333"])
    assert _is_playwright_invocation(
        ["npx", "--package=playwright", "--", "codegen",
         "--cdp-endpoint", "http://127.0.0.1:9333"])
    assert _is_agent_browser_invocation(
        ["npx", "--package=agent-browser", "--",
         "--cdp", "http://127.0.0.1:9333"])
    # Last-wins pin. A later ruff pin is not lighthouse.
    assert _is_lighthouse_invocation(
        ["npx", "--package=ruff", "--package=lighthouse", "--", "--port", "9333"])
    assert not _is_lighthouse_invocation(
        ["npx", "--package=lighthouse", "--package=ruff", "--", "--port", "9333"])
    assert not _is_lighthouse_invocation(
        ["npx", "--package=lighthouse-ci", "--", "--port", "9333"])
    # Yarn Berry ``yarn npm exec`` hid leftover unwrap (finding 106).
    assert _is_lighthouse_invocation(
        ["yarn", "npm", "exec", "lighthouse", "--port", "9333"])
    assert _is_lighthouse_invocation(
        ["yarn", "npm", "exec", "--", "lighthouse", "--port", "9333"])
    assert _is_lighthouse_invocation(
        ["yarn", "npm", "exec", "--package=lighthouse", "--", "--port", "9333"])
    assert _is_lighthouse_invocation(
        ["yarn", "npm", "x", "lighthouse", "--port", "9333"])
    assert _is_lighthouse_invocation(
        ["yarn", "--cwd", "/tmp/ws", "npm", "exec", "lighthouse", "--port", "9333"])
    assert _is_lighthouse_invocation(
        ["corepack", "yarn", "npm", "exec", "lighthouse", "--port", "9333"])
    assert _is_chrome_devtools_mcp_invocation(
        ["yarn", "npm", "exec", "chrome-devtools-mcp",
         "--browserUrl", "http://127.0.0.1:9333"])
    assert not _is_lighthouse_invocation(["yarn", "npm", "install", "lighthouse"])
    assert not _is_lighthouse_invocation(
        ["/bin/bash", "-c", "yarn npm exec lighthouse --port 9333"])
    # Shebang node on the PM entry hid ``--package=`` leftover pins (107).
    assert _is_lighthouse_invocation(
        ["node", "/usr/share/yarn/bin/yarn.js", "npm", "exec",
         "--package=lighthouse", "--", "--port", "9333"])
    assert _is_lighthouse_invocation(
        ["node", "/home/x/.yarn/releases/yarn-4.9.2.cjs", "npm", "exec",
         "--package=lighthouse", "--", "--port", "9333"])
    assert _is_lighthouse_invocation(
        ["node", "/usr/lib/node_modules/npm/bin/npx-cli.js",
         "--package=lighthouse", "--", "--port", "9333"])
    assert _is_lighthouse_invocation(
        ["node", "/usr/lib/node_modules/npm/bin/npx-cli.js",
         "-p", "lighthouse", "--", "--port", "9333"])
    assert _is_lighthouse_invocation(
        ["node", "/usr/lib/node_modules/npm/bin/npm-cli.js", "exec",
         "--package=lighthouse", "--", "--port", "9333"])
    assert _is_lighthouse_invocation(
        ["node", "/usr/lib/node_modules/pnpm/bin/pnpm.cjs", "exec",
         "--package=lighthouse", "--", "--port", "9333"])
    assert _is_chrome_devtools_mcp_invocation(
        ["node", "/usr/lib/node_modules/npm/bin/npx-cli.js",
         "--package=chrome-devtools-mcp", "--",
         "--browserUrl", "http://127.0.0.1:9333"])
    assert _is_agent_browser_invocation(
        ["node", "/usr/lib/node_modules/npm/bin/npx-cli.js",
         "--package=agent-browser", "--",
         "--cdp", "http://127.0.0.1:9333"])
    assert not _is_lighthouse_invocation(
        ["node", "/tmp/other.js", "--package=lighthouse", "--",
         "--port", "9333"])
    assert not _is_lighthouse_invocation(
        ["/bin/bash", "-c",
         "node /usr/share/yarn/bin/yarn.js npm exec --package=lighthouse -- --port 9333"])
    # Corepack dist shims hid leftover ``--package=`` after shebang (108).
    assert _is_lighthouse_invocation(
        ["node", "/usr/lib/node_modules/corepack/dist/corepack.js",
         "yarn", "npm", "exec", "--package=lighthouse", "--", "--port", "9333"])
    assert _is_lighthouse_invocation(
        ["node", "/usr/lib/node_modules/corepack/dist/corepack.js",
         "pnpm", "exec", "--package=lighthouse", "--", "--port", "9333"])
    assert _is_lighthouse_invocation(
        ["node", "/usr/lib/node_modules/corepack/dist/corepack.js", "npx",
         "--package=lighthouse", "--", "--port", "9333"])
    assert _is_lighthouse_invocation(
        ["node", "/usr/lib/node_modules/corepack/dist/yarn.js", "npm", "exec",
         "--package=lighthouse", "--", "--port", "9333"])
    assert _is_lighthouse_invocation(
        ["node", "/usr/lib/node_modules/corepack/dist/yarnpkg.js", "npm", "exec",
         "--package=lighthouse", "--", "--port", "9333"])
    assert _is_lighthouse_invocation(
        ["node", "/usr/lib/node_modules/corepack/dist/pnpm.js", "exec",
         "--package=lighthouse", "--", "--port", "9333"])
    assert _is_lighthouse_invocation(
        ["node", "/usr/lib/node_modules/corepack/dist/npm.js", "exec",
         "--package=lighthouse", "--", "--port", "9333"])
    assert _is_lighthouse_invocation(
        ["node", "/usr/lib/node_modules/corepack/dist/pnpx.js",
         "--package=lighthouse", "--", "--port", "9333"])
    assert _is_chrome_devtools_mcp_invocation(
        ["node", "/usr/lib/node_modules/corepack/dist/corepack.js", "npx",
         "--package=chrome-devtools-mcp", "--",
         "--browserUrl", "http://127.0.0.1:9333"])
    assert not _is_lighthouse_invocation(
        ["node", "/tmp/corepack.js", "yarn", "npm", "exec",
         "--package=lighthouse", "--", "--port", "9333"])
    assert not _is_lighthouse_invocation(
        ["node", "/usr/lib/node_modules/corepack/dist/corepack.js", "enable"])
    assert not _is_lighthouse_invocation(
        ["node", "/usr/lib/node_modules/corepack/dist/yarn.js", "npm",
         "install", "lighthouse"])
    assert _is_lighthouse_invocation(
        ["node", "/home/x/node_modules/lighthouse/cli/index.js",
         "--port=9333", "https://example.com"])
    assert _is_lighthouse_invocation(
        ["pnpm", "exec", "lighthouse", "--port", "9333", "https://example.com"])
    assert _is_lighthouse_invocation(
        ["npm", "exec", "--package=lighthouse", "--",
         "--port=9333", "https://example.com"])
    assert not _is_lighthouse_invocation(["npx", "lighthouse-ci"])
    assert not _is_lighthouse_invocation(
        ["node", "/home/x/node_modules/@lhci/cli/src/cli.js"])
    assert not _is_lighthouse_invocation(["/usr/bin/cat", "lighthouse.log"])
    assert not _is_lighthouse_invocation(
        ["/bin/bash", "-c", "npx lighthouse --port 9333 https://example.com"])
    assert not _is_lighthouse_invocation(["pnpm", "run", "lighthouse"])
    assert not _is_lighthouse_invocation(["npm", "install", "lighthouse"])
    assert not _is_lighthouse_invocation(["pnpm", "exec", "ruff"])
    assert _is_lighthouse_invocation(
        ["corepack", "pnpm", "exec", "lighthouse", "--port", "9333"])
    assert _is_lighthouse_invocation(
        ["corepack", "--yes", "npm", "exec", "--package=lighthouse", "--",
         "--port=9333", "https://example.com"])
    assert _is_chrome_devtools_mcp_invocation(
        ["corepack", "pnpm", "exec", "chrome-devtools-mcp",
         "--browserUrl", "http://127.0.0.1:9333"])
    assert not _is_lighthouse_invocation(["corepack", "enable"])
    assert not _is_lighthouse_invocation(["corepack", "use", "pnpm@10"])
    assert not _is_lighthouse_invocation(
        ["corepack", "npm", "install", "lighthouse"])
    assert not _is_lighthouse_invocation(
        ["/bin/bash", "-c", "corepack pnpm exec lighthouse --port 9333"])
    assert _is_lighthouse_invocation(
        ["bun", "x", "lighthouse", "--port", "9333", "https://example.com"])
    assert _is_lighthouse_invocation(
        ["bun", "--bun", "x", "lighthouse", "--port=9333"])
    assert _is_chrome_devtools_mcp_invocation(
        ["bun", "exec", "chrome-devtools-mcp",
         "--browserUrl", "http://127.0.0.1:9333"])
    assert not _is_lighthouse_invocation(["bun", "run", "lighthouse"])
    assert not _is_lighthouse_invocation(["bun", "install", "lighthouse"])
    assert not _is_lighthouse_invocation(["bun", "add", "lighthouse"])
    assert not _is_lighthouse_invocation(["bun", "x", "ruff"])
    assert not _is_lighthouse_invocation(
        ["/bin/bash", "-c", "bun x lighthouse --port 9333"])
    # Workspace-dir globals used to become the "command" (finding 102).
    assert _is_lighthouse_invocation(
        ["pnpm", "--dir", "/tmp/ws", "exec", "lighthouse",
         "--port", "9333", "https://example.com"])
    assert _is_lighthouse_invocation(
        ["pnpm", "-C", "/tmp/ws", "exec", "lighthouse", "--port", "9333"])
    assert _is_lighthouse_invocation(
        ["pnpm", "exec", "--dir", "/tmp/ws", "lighthouse", "--port", "9333"])
    assert _is_lighthouse_invocation(
        ["pnpm", "--filter", "@scope/pkg", "exec", "lighthouse", "--port", "9333"])
    assert _is_lighthouse_invocation(
        ["npm", "--prefix", "/tmp/ws", "exec", "lighthouse", "--port", "9333"])
    assert _is_lighthouse_invocation(
        ["yarn", "--cwd", "/tmp/ws", "dlx", "lighthouse", "--port", "9333"])
    assert _is_lighthouse_invocation(
        ["bun", "--cwd", "/tmp/ws", "x", "lighthouse", "--port", "9333"])
    assert _is_lighthouse_invocation(
        ["corepack", "pnpm", "--dir", "/tmp/ws", "exec", "lighthouse",
         "--port", "9333"])
    assert not _is_lighthouse_invocation(
        ["pnpm", "--dir", "/tmp/ws", "run", "lighthouse"])
    # Missing dir/cwd value must not swallow the subcommand.
    assert _is_lighthouse_invocation(
        ["pnpm", "--dir", "exec", "lighthouse", "--port", "9333"])
    assert _is_lighthouse_invocation(
        ["bun", "--cwd", "x", "lighthouse", "--port", "9333"])


def test_unregistered_cdp_dock_cli_killed_on_takeover():
    """terminal()-spawned agent-browser --cdp <dock> is leftover action."""
    from tools.bot_desktop import browser as bdb
    from tools.browser_tool_session import interrupt_unregistered_dock_cli

    bdb.remember_dock_cdp_port(9333)
    leftover = _FakeProc(
        4242, ["agent-browser", "--cdp", "http://127.0.0.1:9333", "fill", "@e1", "x"])
    other = _FakeProc(
        4243, ["agent-browser", "--cdp", "http://127.0.0.1:9222", "fill", "@e1", "x"])
    lease.acquire("human")
    n = interrupt_unregistered_dock_cli(
        processes=[leftover, other], chromium_pid=9999, owner_daemon_pid=9998,
    )
    assert n == 1
    assert leftover.killed == 1
    assert other.killed == 0


def test_unregistered_npx_cdp_equals_form_killed_on_takeover():
    from tools.bot_desktop import browser as bdb
    from tools.browser_tool_session import interrupt_unregistered_dock_cli

    bdb.remember_dock_cdp_port(9333)
    leftover = _FakeProc(
        4250, ["npx", "--yes", "agent-browser", "--cdp=ws://127.0.0.1:9333", "fill"])
    lease.acquire("human")
    n = interrupt_unregistered_dock_cli(
        processes=[leftover], chromium_pid=9999, owner_daemon_pid=9998,
    )
    assert n == 1
    assert leftover.killed == 1


def test_unregistered_spares_chromium_and_owner_daemon():
    from tools.bot_desktop import browser as bdb
    from tools.browser_tool_session import interrupt_unregistered_dock_cli

    bdb.remember_dock_cdp_port(9333)
    chrome = _FakeProc(7000, ["agent-browser", "--cdp", "http://127.0.0.1:9333"])
    owner = _FakeProc(7001, ["agent-browser", "--cdp", "http://127.0.0.1:9333", "open"])
    leftover = _FakeProc(
        7002, ["npx", "agent-browser", "--cdp", "http://127.0.0.1:9333", "fill"])
    lease.acquire("human")
    interrupt_unregistered_dock_cli(
        processes=[chrome, owner, leftover],
        chromium_pid=7000,
        owner_daemon_pid=7001,
    )
    assert chrome.killed == 0
    assert owner.killed == 0
    assert leftover.killed == 1


def test_unregistered_is_noop_while_agent_holds():
    from tools.bot_desktop import browser as bdb
    from tools.browser_tool_session import interrupt_unregistered_dock_cli

    bdb.remember_dock_cdp_port(9333)
    leftover = _FakeProc(
        4260, ["agent-browser", "--cdp", "http://127.0.0.1:9333", "fill"])
    n = interrupt_unregistered_dock_cli(
        processes=[leftover], chromium_pid=9999, owner_daemon_pid=9998,
    )
    assert n == 0
    assert leftover.killed == 0


def test_unregistered_spares_sibling_profile_cli(tmp_path: Path):
    from hermes_constants import reset_hermes_home_override, set_hermes_home_override
    from tools.bot_desktop import browser as bdb
    from tools.browser_tool_session import interrupt_unregistered_dock_cli

    home_a = tmp_path / "profile-a"
    home_a.mkdir()
    leftover_b = _FakeProc(
        8001, ["agent-browser", "--cdp", "http://127.0.0.1:9444", "fill"])
    token = set_hermes_home_override(home_a)
    try:
        bdb.remember_dock_cdp_port(9333)
        lease.acquire("human")
        interrupt_unregistered_dock_cli(
            home=str(home_a),
            processes=[leftover_b],
            chromium_pid=9999,
            owner_daemon_pid=9998,
        )
    finally:
        lease.release("human")
        reset_hermes_home_override(token)
    assert leftover_b.killed == 0


def test_unregistered_kills_env_pinned_profile_without_cdp():
    from tools.bot_desktop import browser as bdb
    from tools.browser_tool_session import interrupt_unregistered_dock_cli

    profile = bdb.profile_dir()
    leftover = _FakeProc(
        8100, ["agent-browser", "fill", "@e1", "x"],
        {"AGENT_BROWSER_PROFILE": str(profile)},
    )
    other = _FakeProc(
        8101, ["agent-browser", "fill", "@e1", "x"],
        {"AGENT_BROWSER_PROFILE": "/tmp/other-chrome"},
    )
    unknown = _FakeProc(8102, ["agent-browser", "fill", "@e1", "x"], {})
    lease.acquire("human")
    interrupt_unregistered_dock_cli(
        processes=[leftover, other, unknown],
        chromium_pid=9999,
        owner_daemon_pid=9998,
    )
    assert leftover.killed == 1
    assert other.killed == 0
    assert unknown.killed == 0


def test_unregistered_kills_relative_env_pinned_profile():
    """Relative ``AGENT_BROWSER_PROFILE`` is the leftover writer's cwd, not ours.

    Finding 95 fixed Chromium argv. The leftover-CLI pin still resolved
    ``browser-profile`` against the gateway cwd, so Take over left the
    writer running in the field the human is typing into.
    """
    from tools.bot_desktop import browser as bdb
    from tools.browser_tool_session import interrupt_unregistered_dock_cli

    profile = bdb.profile_dir()
    leftover = _FakeProc(
        8110, ["agent-browser", "fill", "@e1", "x"],
        {"AGENT_BROWSER_PROFILE": "browser-profile"},
        cwd=profile.parent,
    )
    dotted = _FakeProc(
        8111, ["agent-browser", "fill", "@e1", "x"],
        {"AGENT_BROWSER_PROFILE": "./browser-profile"},
        cwd=profile.parent,
    )
    here = _FakeProc(
        8112, ["agent-browser", "fill", "@e1", "x"],
        {"AGENT_BROWSER_PROFILE": "."},
        cwd=profile,
    )
    wrong_cwd = _FakeProc(
        8113, ["agent-browser", "fill", "@e1", "x"],
        {"AGENT_BROWSER_PROFILE": "browser-profile"},
        cwd=profile.parent.parent,
    )
    lease.acquire("human")
    n = interrupt_unregistered_dock_cli(
        processes=[leftover, dotted, here, wrong_cwd],
        chromium_pid=9999,
        owner_daemon_pid=9998,
    )
    assert n == 3
    assert leftover.killed == 1
    assert dotted.killed == 1
    assert here.killed == 1
    assert wrong_cwd.killed == 0


def test_unregistered_kills_argv_profile_pin_without_cdp():
    """``--profile`` / ``--user-data-dir`` hid leftover jar aim (finding 111).

    Finding 99 only checked ``AGENT_BROWSER_PROFILE``. Official leftover
    pins the dock jar on argv. Explicit ``--cdp`` still wins. Relative
    ``--profile`` uses the writer cwd. ``--cdp chrome`` stays unknown.
    Bash ``-c`` parent is still not the writer.
    """
    from tools.bot_desktop import browser as bdb
    from tools.browser_tool_session import interrupt_unregistered_dock_cli

    profile = bdb.profile_dir()
    leftover = _FakeProc(
        8120, ["agent-browser", "--profile", str(profile), "snapshot"],
    )
    equals = _FakeProc(
        8121, ["agent-browser", f"--profile={profile}", "fill", "@e1", "x"],
    )
    relative = _FakeProc(
        8122, ["agent-browser", "--profile", "browser-profile", "snapshot"],
        cwd=profile.parent,
    )
    playwright = _FakeProc(
        8123,
        ["npx", "playwright", "codegen", "--user-data-dir", str(profile)],
    )
    playwright_cli = _FakeProc(
        8124, ["playwright-cli", "open", f"--profile={profile}"],
    )
    other = _FakeProc(
        8125, ["agent-browser", "--profile", "/tmp/other-chrome", "snapshot"],
    )
    cdp_wins = _FakeProc(
        8126,
        ["agent-browser", "--cdp", "http://127.0.0.1:9222",
         "--profile", str(profile), "snapshot"],
    )
    channel = _FakeProc(
        8127,
        ["playwright-cli", "attach", "--cdp", "chrome",
         "--profile", str(profile)],
    )
    wrong_cwd = _FakeProc(
        8128, ["agent-browser", "--profile", "browser-profile", "snapshot"],
        cwd=profile.parent.parent,
    )
    bash_parent = _FakeProc(
        8129,
        ["/bin/bash", "-c", f"agent-browser --profile {profile} snapshot"],
    )
    lease.acquire("human")
    n = interrupt_unregistered_dock_cli(
        processes=[
            leftover, equals, relative, playwright, playwright_cli,
            other, cdp_wins, channel, wrong_cwd, bash_parent,
        ],
        chromium_pid=9999,
        owner_daemon_pid=9998,
    )
    assert n == 5
    assert leftover.killed == 1
    assert equals.killed == 1
    assert relative.killed == 1
    assert playwright.killed == 1
    assert playwright_cli.killed == 1
    assert other.killed == 0
    assert cdp_wins.killed == 0
    assert channel.killed == 0
    assert wrong_cwd.killed == 0
    assert bash_parent.killed == 0


def test_unregistered_agent_browser_connect_and_cdp_env_killed_on_takeover():
    """Leftover daemon ``AGENT_BROWSER_CDP`` / ``connect <dock>`` (finding 112).

    Official leftover that stays after ``connect`` is the daemon with
    ``AGENT_BROWSER_CDP`` frozen — no ``--cdp`` on argv. In-flight
    ``connect 9333`` / ``connect http://…`` is the same attach.
    ``--auto-connect`` / another loopback / LAN stay unknown. Explicit
    ``--cdp`` wins over a dock env pin. Bash ``-c`` parent is still
    not the writer.
    """
    from tools.bot_desktop import browser as bdb
    from tools.browser_tool_session import interrupt_unregistered_dock_cli

    bdb.remember_dock_cdp_port(9333)
    daemon = _FakeProc(
        8130, ["agent-browser", "snapshot"],
        {"AGENT_BROWSER_CDP": "9333", "AGENT_BROWSER_DAEMON": "1"},
    )
    daemon_url = _FakeProc(
        8131, ["agent-browser", "snapshot"],
        {"AGENT_BROWSER_CDP": "http://127.0.0.1:9333"},
    )
    connect = _FakeProc(8132, ["agent-browser", "connect", "9333"])
    connect_url = _FakeProc(
        8133,
        ["agent-browser", "connect", "ws://127.0.0.1:9333/devtools/browser/x"],
    )
    session_connect = _FakeProc(
        8134,
        ["agent-browser", "--session", "foo", "connect",
         "http://127.0.0.1:9333"],
    )
    npx_connect = _FakeProc(
        8135, ["npx", "agent-browser", "connect", "9333"],
    )
    other_env = _FakeProc(
        8136, ["agent-browser", "snapshot"],
        {"AGENT_BROWSER_CDP": "9222"},
    )
    other_connect = _FakeProc(
        8137, ["agent-browser", "connect", "9222"],
    )
    auto = _FakeProc(
        8138, ["agent-browser", "--auto-connect", "snapshot"],
    )
    cdp_wins = _FakeProc(
        8139, ["agent-browser", "--cdp", "9222", "snapshot"],
        {"AGENT_BROWSER_CDP": "9333"},
    )
    lan = _FakeProc(
        8140, ["agent-browser", "connect", "http://10.0.0.5:9333"],
    )
    bash_parent = _FakeProc(
        8141,
        ["/bin/bash", "-c", "agent-browser connect 9333"],
    )
    lease.acquire("human")
    n = interrupt_unregistered_dock_cli(
        processes=[
            daemon, daemon_url, connect, connect_url, session_connect,
            npx_connect, other_env, other_connect, auto, cdp_wins, lan,
            bash_parent,
        ],
        chromium_pid=9999,
        owner_daemon_pid=9998,
    )
    assert n == 6
    assert daemon.killed == 1
    assert daemon_url.killed == 1
    assert connect.killed == 1
    assert connect_url.killed == 1
    assert session_connect.killed == 1
    assert npx_connect.killed == 1
    assert other_env.killed == 0
    assert other_connect.killed == 0
    assert auto.killed == 0
    assert cdp_wins.killed == 0
    assert lan.killed == 0
    assert bash_parent.killed == 0


def test_unregistered_spares_lan_cdp_even_when_port_matches():
    from tools.bot_desktop import browser as bdb
    from tools.browser_tool_session import interrupt_unregistered_dock_cli

    bdb.remember_dock_cdp_port(9333)
    leftover = _FakeProc(
        8200, ["agent-browser", "--cdp", "http://10.0.0.5:9333", "fill"])
    lease.acquire("human")
    interrupt_unregistered_dock_cli(
        processes=[leftover], chromium_pid=9999, owner_daemon_pid=9998,
    )
    assert leftover.killed == 0


def test_unregistered_skips_registered_inflight_pid():
    from tools.bot_desktop import browser as bdb
    from tools.browser_tool_session import (
        interrupt_unregistered_dock_cli,
        register_inflight_dock_cli,
    )

    bdb.remember_dock_cdp_port(9333)
    lease.acquire("human")
    inflight = _FakeProc(
        8300, ["agent-browser", "--cdp", "http://127.0.0.1:9333", "fill"])
    register_inflight_dock_cli(inflight)
    # Finding 42 already SIGINT'd registered writers; do not SIGKILL them again.
    interrupt_unregistered_dock_cli(
        processes=[inflight], chromium_pid=9999, owner_daemon_pid=9998,
    )
    assert inflight.killed == 0


def test_unregistered_explicit_other_cdp_wins_over_env_pin():
    from tools.bot_desktop import browser as bdb
    from tools.browser_tool_session import interrupt_unregistered_dock_cli

    profile = bdb.profile_dir()
    bdb.remember_dock_cdp_port(9333)
    leftover = _FakeProc(
        8500,
        ["agent-browser", "--cdp", "http://127.0.0.1:9222", "fill"],
        {"AGENT_BROWSER_PROFILE": str(profile)},
    )
    lease.acquire("human")
    interrupt_unregistered_dock_cli(
        processes=[leftover], chromium_pid=9999, owner_daemon_pid=9998,
    )
    assert leftover.killed == 0


def test_unregistered_does_not_use_killpg(monkeypatch):
    import os

    from tools.bot_desktop import browser as bdb
    from tools.browser_tool_session import interrupt_unregistered_dock_cli

    killed_pg = []
    monkeypatch.setattr(os, "killpg", lambda *a, **k: killed_pg.append(a))
    bdb.remember_dock_cdp_port(9333)
    leftover = _FakeProc(
        8400, ["agent-browser", "--cdp", "http://127.0.0.1:9333", "fill"])
    lease.acquire("human")
    interrupt_unregistered_dock_cli(
        processes=[leftover], chromium_pid=9999, owner_daemon_pid=9998,
    )
    assert leftover.killed == 1
    assert killed_pg == []


def test_unregistered_node_shebang_dock_cli_killed_on_takeover():
    """After shebang, terminal leftover is node /path/agent-browser, not argv0."""
    from tools.bot_desktop import browser as bdb
    from tools.browser_tool_session import interrupt_unregistered_dock_cli

    bdb.remember_dock_cdp_port(9333)
    leftover = _FakeProc(8700, [
        "/usr/bin/node", "/usr/bin/agent-browser",
        "--cdp", "http://127.0.0.1:9333", "fill", "@e1", "x",
    ])
    packaged = _FakeProc(8701, [
        "node", "/home/x/node_modules/agent-browser/dist/cli.js",
        "--cdp=ws://127.0.0.1:9333", "fill",
    ])
    via_env = _FakeProc(8702, [
        "/usr/bin/env", "NODE_ENV=production", "node",
        "/usr/bin/agent-browser", "--cdp", "http://127.0.0.1:9333", "fill",
    ])
    other_js = _FakeProc(8703, [
        "node", "/tmp/other.js", "--cdp", "http://127.0.0.1:9333",
    ])
    bash_parent = _FakeProc(8704, [
        "/bin/bash", "-c", "agent-browser --cdp http://127.0.0.1:9333 fill",
    ])
    lease.acquire("human")
    n = interrupt_unregistered_dock_cli(
        processes=[leftover, packaged, via_env, other_js, bash_parent],
        chromium_pid=9999,
        owner_daemon_pid=9998,
    )
    assert n == 3
    assert leftover.killed == 1
    assert packaged.killed == 1
    assert via_env.killed == 1
    assert other_js.killed == 0
    assert bash_parent.killed == 0


def test_unregistered_python_browser_use_dock_env_killed_on_takeover():
    from tools.bot_desktop import browser as bdb
    from tools.browser_tool_session import interrupt_unregistered_dock_cli

    bdb.remember_dock_cdp_port(9333)
    leftover = _FakeProc(
        8800,
        ["python3", "/home/x/.local/bin/browser-use", "exec"],
        {"BU_CDP_URL": "http://127.0.0.1:9333"},
    )
    other = _FakeProc(
        8801,
        ["python3", "/home/x/.local/bin/browser-use", "exec"],
        {"BU_CDP_URL": "http://127.0.0.1:9222"},
    )
    lease.acquire("human")
    n = interrupt_unregistered_dock_cli(
        processes=[leftover, other], chromium_pid=9999, owner_daemon_pid=9998,
    )
    assert n == 1
    assert leftover.killed == 1
    assert other.killed == 0


def test_unregistered_browser_use_dock_env_killed_on_takeover():
    """terminal()-spawned browser-use with BU_CDP_* on the dock is leftover action."""
    from tools.bot_desktop import browser as bdb
    from tools.browser_tool_session import interrupt_unregistered_dock_cli

    bdb.remember_dock_cdp_port(9333)
    leftover = _FakeProc(
        8600, ["browser-use", "exec"],
        {"BU_CDP_URL": "http://127.0.0.1:9333"},
    )
    uvx = _FakeProc(
        8601, ["uvx", "browser-use"],
        {"BU_CDP_WS": "ws://127.0.0.1:9333/devtools/browser/x"},
    )
    other = _FakeProc(
        8602, ["browser-use", "exec"],
        {"BU_CDP_URL": "http://127.0.0.1:9222"},
    )
    unknown = _FakeProc(8603, ["browser-use", "exec"], {})
    lan = _FakeProc(
        8604, ["browser-use", "exec"],
        {"BU_CDP_URL": "http://10.0.0.5:9333"},
    )
    lease.acquire("human")
    n = interrupt_unregistered_dock_cli(
        processes=[leftover, uvx, other, unknown, lan],
        chromium_pid=9999,
        owner_daemon_pid=9998,
    )
    assert n == 2
    assert leftover.killed == 1
    assert uvx.killed == 1
    assert other.killed == 0
    assert unknown.killed == 0
    assert lan.killed == 0


def test_unregistered_browser_use_cdp_url_killed_on_takeover():
    """``browser-use --cdp-url <dock>`` hid leftover attach (finding 110).

    Official leftover action is the argv flag. Finding 78 only checked
    ``BU_CDP_*`` env. ``--connect`` / another loopback / LAN stay up.
    Explicit ``--cdp-url`` wins over a dock env pin. Last ``--cdp-url``
    wins. Bash ``-c`` parent is still not the writer.
    """
    from tools.bot_desktop import browser as bdb
    from tools.browser_tool_session import interrupt_unregistered_dock_cli

    bdb.remember_dock_cdp_port(9333)
    leftover = _FakeProc(
        8610,
        ["browser-use", "--cdp-url", "http://127.0.0.1:9333", "open",
         "https://example.com"],
    )
    equals = _FakeProc(
        8611,
        ["browser-use", "--cdp-url=ws://127.0.0.1:9333/devtools/browser/x",
         "state"],
    )
    uvx = _FakeProc(
        8612,
        ["uvx", "browser-use", "--cdp-url", "http://127.0.0.1:9333", "open"],
    )
    via_python = _FakeProc(
        8613,
        ["python3", "/home/x/.local/bin/browser-use", "--cdp-url",
         "http://127.0.0.1:9333", "exec"],
    )
    last_wins = _FakeProc(
        8614,
        ["browser-use", "--cdp-url", "http://127.0.0.1:1",
         "--cdp-url", "http://127.0.0.1:9333", "open"],
    )
    other = _FakeProc(
        8615,
        ["browser-use", "--cdp-url", "http://127.0.0.1:9222", "open"],
    )
    flag_wins_env = _FakeProc(
        8616,
        ["browser-use", "--cdp-url", "http://127.0.0.1:9222", "open"],
        {"BU_CDP_URL": "http://127.0.0.1:9333"},
    )
    connect = _FakeProc(
        8617, ["browser-use", "--connect", "open", "https://example.com"],
    )
    lan = _FakeProc(
        8618,
        ["browser-use", "--cdp-url", "http://10.0.0.5:9333", "open"],
    )
    bash_parent = _FakeProc(
        8619,
        ["/bin/bash", "-c",
         "browser-use --cdp-url http://127.0.0.1:9333 open"],
    )
    lease.acquire("human")
    n = interrupt_unregistered_dock_cli(
        processes=[
            leftover, equals, uvx, via_python, last_wins, other,
            flag_wins_env, connect, lan, bash_parent,
        ],
        chromium_pid=9999,
        owner_daemon_pid=9998,
    )
    assert n == 5
    assert leftover.killed == 1
    assert equals.killed == 1
    assert uvx.killed == 1
    assert via_python.killed == 1
    assert last_wins.killed == 1
    assert other.killed == 0
    assert flag_wins_env.killed == 0
    assert connect.killed == 0
    assert lan.killed == 0
    assert bash_parent.killed == 0


def test_unregistered_python_module_browser_use_killed_on_takeover():
    """``python -m browser_use --cdp-url <dock>`` hid leftover attach.

    Finding 79 matched the hyphenated console-script path
    (``python3 …/browser-use``). Official leftover when that script is
    missing is ``python -m browser_use`` — the writer that stays attached
    is python, and Take over left it typing into the jar. A random
    ``/tmp/browser_use`` script, ``browser_use_cli``, another loopback,
    ``--connect``, and the bash ``-c`` parent stay up.
    """
    from tools.bot_desktop import browser as bdb
    from tools.browser_tool_session import interrupt_unregistered_dock_cli

    bdb.remember_dock_cdp_port(9333)
    leftover = _FakeProc(
        8620,
        ["python3", "-m", "browser_use", "--cdp-url",
         "http://127.0.0.1:9333", "exec"],
    )
    equals = _FakeProc(
        8621,
        ["/usr/bin/python3", "-m", "browser_use",
         "--cdp-url=ws://127.0.0.1:9333/devtools/browser/x"],
    )
    via_env = _FakeProc(
        8622,
        ["python3.12", "-m", "browser_use", "exec"],
        {"BU_CDP_URL": "http://127.0.0.1:9333"},
    )
    via_python = _FakeProc(
        8623,
        ["python", "-m", "browser_use", "--cdp-url",
         "http://127.0.0.1:9333"],
    )
    other = _FakeProc(
        8624,
        ["python3", "-m", "browser_use", "--cdp-url",
         "http://127.0.0.1:9222"],
    )
    random_script = _FakeProc(
        8625,
        ["python3", "/tmp/browser_use", "--cdp-url",
         "http://127.0.0.1:9333"],
    )
    other_mod = _FakeProc(
        8626,
        ["python3", "-m", "browser_use_cli", "--cdp-url",
         "http://127.0.0.1:9333"],
    )
    connect = _FakeProc(
        8627, ["python3", "-m", "browser_use", "--connect", "open"],
    )
    bash_parent = _FakeProc(
        8628,
        ["/bin/bash", "-c",
         "python3 -m browser_use --cdp-url http://127.0.0.1:9333"],
    )
    lease.acquire("human")
    n = interrupt_unregistered_dock_cli(
        processes=[
            leftover, equals, via_env, via_python, other, random_script,
            other_mod, connect, bash_parent,
        ],
        chromium_pid=9999,
        owner_daemon_pid=9998,
    )
    assert n == 4
    assert leftover.killed == 1
    assert equals.killed == 1
    assert via_env.killed == 1
    assert via_python.killed == 1
    assert other.killed == 0
    assert random_script.killed == 0
    assert other_mod.killed == 0
    assert connect.killed == 0
    assert bash_parent.killed == 0


def test_unregistered_cli_whatwg_ipv4_shorthand_killed_on_takeover():
    """``--cdp-url http://127.1:9333`` hid leftover attach.

    Finding 72 recognized 127/8 and IPv4-mapped hosts via ``ipaddress``.
    Chromium / leftover CLIs also accept WHATWG dotted shorthand
    (``127.1``, ``0``, ``2130706433``). Those never extracted a port, so
    Take over left the writer typing into the jar. LAN shorthand and
    another loopback port stay up.
    """
    from tools.bot_desktop import browser as bdb
    from tools.browser_tool_session import interrupt_unregistered_dock_cli

    bdb.remember_dock_cdp_port(9333)
    leftover = _FakeProc(
        8640,
        ["python3", "-m", "browser_use", "--cdp-url",
         "http://127.1:9333", "exec"],
    )
    three = _FakeProc(
        8641,
        ["python3", "-m", "browser_use", "--cdp-url",
         "http://127.0.1:9333", "exec"],
    )
    zero = _FakeProc(
        8642,
        ["python3", "-m", "browser_use", "--cdp-url",
         "http://0:9333", "exec"],
    )
    packed = _FakeProc(
        8643,
        ["lighthouse", "https://example.com", "--port", "9333",
         "--hostname", "127.1"],
    )
    mapped = _FakeProc(
        8646,
        ["python3", "-m", "browser_use", "--cdp-url",
         "ws://[::ffff:127.1]:9333/devtools/browser/x", "exec"],
    )
    other = _FakeProc(
        8644,
        ["python3", "-m", "browser_use", "--cdp-url",
         "http://127.1:9222", "exec"],
    )
    lan = _FakeProc(
        8645,
        ["python3", "-m", "browser_use", "--cdp-url",
         "http://10.1:9333", "exec"],
    )
    lease.acquire("human")
    n = interrupt_unregistered_dock_cli(
        processes=[leftover, three, zero, packed, mapped, other, lan],
        chromium_pid=9999,
        owner_daemon_pid=9998,
    )
    assert n == 5
    assert leftover.killed == 1
    assert three.killed == 1
    assert zero.killed == 1
    assert packed.killed == 1
    assert mapped.killed == 1
    assert other.killed == 0
    assert lan.killed == 0


def test_unregistered_cli_percent_encoded_loopback_killed_on_takeover():
    """Node leftover ``--browserUrl http://127%2e1:9333`` hid attach.

    ``new URL()`` decodes percent-encoded dots / digits to 127.0.0.1.
    ``urllib.parse`` does not, so Take over left that writer typing
    into the jar. LAN ``10%2e1`` and another loopback port stay up.
    """
    from tools.bot_desktop import browser as bdb
    from tools.browser_tool_session import interrupt_unregistered_dock_cli

    bdb.remember_dock_cdp_port(9333)
    leftover = _FakeProc(
        8650,
        ["npx", "chrome-devtools-mcp", "--browserUrl",
         "http://127%2e1:9333"],
    )
    full = _FakeProc(
        8651,
        ["python3", "-m", "browser_use", "--cdp-url",
         "http://127%2e0%2e0%2e1:9333", "exec"],
    )
    digits = _FakeProc(
        8652,
        ["lighthouse", "https://example.com", "--port", "9333",
         "--hostname", "127%2e1"],
    )
    other = _FakeProc(
        8653,
        ["npx", "chrome-devtools-mcp", "--browserUrl",
         "http://127%2e1:9222"],
    )
    lan = _FakeProc(
        8654,
        ["python3", "-m", "browser_use", "--cdp-url",
         "http://10%2e1:9333", "exec"],
    )
    lease.acquire("human")
    n = interrupt_unregistered_dock_cli(
        processes=[leftover, full, digits, other, lan],
        chromium_pid=9999,
        owner_daemon_pid=9998,
    )
    assert n == 3
    assert leftover.killed == 1
    assert full.killed == 1
    assert digits.killed == 1
    assert other.killed == 0
    assert lan.killed == 0


def test_unregistered_cli_uses_session_owner_home_after_multiplex(tmp_path):
    """``interrupt_unregistered_dock_cli()`` used ambient ``human_holds()``.

    The 0.25s watch / ``stop_reserved_supervisors()`` pass no home. After
    a multiplex turn that is the launch bot: missing file fail-opens as
    agent, so leftover ``python -m browser_use --cdp-url <sibling dock>``
    kept typing. Inflight CLI already walks per-entry homes. Re-enter
    recorded session-owner homes when ``home`` is omitted.
    """
    from hermes_constants import reset_hermes_home_override, set_hermes_home_override
    from tools.bot_desktop import browser as bdb
    from tools.bot_desktop.lease import _path
    from tools import browser_tool as bt
    from tools.browser_tool_session import interrupt_unregistered_dock_cli

    launch = tmp_path / "launch"
    bot = tmp_path / "bot"
    launch.mkdir()
    bot.mkdir()
    saved = dict(bt._session_owner_homes)

    leftover = _FakeProc(
        8630,
        ["python3", "-m", "browser_use", "--cdp-url",
         "http://127.0.0.1:9333", "exec"],
    )
    other = _FakeProc(
        8631,
        ["python3", "-m", "browser_use", "--cdp-url",
         "http://127.0.0.1:9222"],
    )
    token_bot = set_hermes_home_override(str(bot))
    try:
        bdb.remember_dock_cdp_port(9333)
        lease.acquire("human-viewer")
        bt._session_owner_homes.clear()
        bt._session_owner_homes["review"] = str(bot)
    finally:
        reset_hermes_home_override(token_bot)

    token_launch = set_hermes_home_override(str(launch))
    try:
        assert lease.human_holds() is False
        n = interrupt_unregistered_dock_cli(
            processes=[leftover, other],
            chromium_pid=9999,
            owner_daemon_pid=9998,
        )
        assert n == 1
        assert leftover.killed == 1
        assert other.killed == 0
    finally:
        reset_hermes_home_override(token_launch)
        bt._session_owner_homes.clear()
        bt._session_owner_homes.update(saved)
        for home in (launch, bot):
            for f in (_path(str(home)), _path(str(home)).with_suffix(".lock")):
                try:
                    f.unlink()
                except OSError:
                    pass


def test_unregistered_cli_does_not_kill_on_the_launch_profile_lease(tmp_path):
    """A human on the launch bot must not drop leftover aimed at a sibling jar."""
    from hermes_constants import reset_hermes_home_override, set_hermes_home_override
    from tools.bot_desktop import browser as bdb
    from tools.bot_desktop.lease import _path
    from tools import browser_tool as bt
    from tools.browser_tool_session import interrupt_unregistered_dock_cli

    launch = tmp_path / "launch"
    bot = tmp_path / "bot"
    launch.mkdir()
    bot.mkdir()
    saved = dict(bt._session_owner_homes)

    leftover = _FakeProc(
        8632,
        ["python3", "-m", "browser_use", "--cdp-url",
         "http://127.0.0.1:9333", "exec"],
    )
    token_bot = set_hermes_home_override(str(bot))
    try:
        bdb.remember_dock_cdp_port(9333)
        bt._session_owner_homes.clear()
        bt._session_owner_homes["review"] = str(bot)
    finally:
        reset_hermes_home_override(token_bot)

    token_launch = set_hermes_home_override(str(launch))
    try:
        bdb.remember_dock_cdp_port(9222)
        lease.acquire("human-viewer")
        n = interrupt_unregistered_dock_cli(
            processes=[leftover],
            chromium_pid=9999,
            owner_daemon_pid=9998,
        )
        assert n == 0
        assert leftover.killed == 0
    finally:
        reset_hermes_home_override(token_launch)
        bt._session_owner_homes.clear()
        bt._session_owner_homes.update(saved)
        for home in (launch, bot):
            for f in (_path(str(home)), _path(str(home)).with_suffix(".lock")):
                try:
                    f.unlink()
                except OSError:
                    pass


def test_unregistered_playwright_dock_cdp_killed_on_takeover():
    """terminal() npx playwright --cdp-endpoint / PW_TEST_CONNECT_* is leftover action."""
    from tools.bot_desktop import browser as bdb
    from tools.browser_tool_session import interrupt_unregistered_dock_cli

    bdb.remember_dock_cdp_port(9333)
    leftover = _FakeProc(
        8900,
        ["npx", "playwright", "codegen", "--cdp-endpoint", "http://127.0.0.1:9333"],
    )
    via_env = _FakeProc(
        8901,
        ["playwright", "open", "https://example.com"],
        {"PW_TEST_CONNECT_WS_ENDPOINT": "ws://127.0.0.1:9333/devtools/browser/x"},
    )
    shebang = _FakeProc(
        8902,
        ["node", "/home/x/node_modules/playwright/cli.js",
         "--cdp-endpoint=http://127.0.0.1:9333", "codegen"],
    )
    install = _FakeProc(8903, ["npx", "playwright", "install"])
    other = _FakeProc(
        8904,
        ["npx", "playwright", "codegen", "--cdp-endpoint", "http://127.0.0.1:9222"],
    )
    bash_parent = _FakeProc(
        8906,
        ["/bin/bash", "-c", "npx playwright codegen --cdp-endpoint http://127.0.0.1:9333"],
    )
    last_wins = _FakeProc(
        8907,
        ["npx", "playwright", "codegen",
         "--cdp", "http://127.0.0.1:1",
         "--cdp-endpoint", "http://127.0.0.1:9333"],
    )
    later_other = _FakeProc(
        8908,
        ["npx", "playwright", "codegen",
         "--cdp-endpoint", "http://127.0.0.1:9333",
         "--cdp", "http://127.0.0.1:9222"],
    )
    lease.acquire("human")
    n = interrupt_unregistered_dock_cli(
        processes=[leftover, via_env, shebang, install, other, bash_parent,
                   last_wins, later_other],
        chromium_pid=9999,
        owner_daemon_pid=9998,
    )
    assert n == 4
    assert leftover.killed == 1
    assert via_env.killed == 1
    assert shebang.killed == 1
    assert last_wins.killed == 1
    assert install.killed == 0
    assert other.killed == 0
    assert bash_parent.killed == 0
    assert later_other.killed == 0


def test_unregistered_playwright_cli_dock_cdp_killed_on_takeover():
    """``playwright-cli attach --cdp=<dock>`` hid leftover Playwright attach.

    The Agent CLI is argv0 ``playwright-cli``, not ``playwright``.
    ``npm exec pkg -- --port`` hid the aim behind the manager ``--``.
    ``--cdp chrome`` / ``--extension`` are a Chrome we cannot prove is
    this jar. ``playwright-core`` is not an invocation. Bash ``-c``
    parent is not the writer.
    """
    from tools.bot_desktop import browser as bdb
    from tools.browser_tool_session import interrupt_unregistered_dock_cli

    bdb.remember_dock_cdp_port(9333)
    leftover = _FakeProc(
        10300,
        ["playwright-cli", "attach", "--cdp", "http://127.0.0.1:9333"],
    )
    equals = _FakeProc(
        10301,
        ["playwright-cli", "attach", "--cdp=http://127.0.0.1:9333"],
    )
    via_npx = _FakeProc(
        10302,
        ["npx", "playwright-cli", "attach", "--cdp", "http://127.0.0.1:9333"],
    )
    packaged = _FakeProc(
        10303,
        ["npx", "--package=playwright-cli", "--",
         "attach", "--cdp", "http://127.0.0.1:9333"],
    )
    shebang = _FakeProc(
        10304,
        ["node", "/home/x/node_modules/playwright-cli/cli.js",
         "attach", "--cdp=http://127.0.0.1:9333"],
    )
    npm_exec = _FakeProc(
        10305,
        ["npm", "exec", "playwright-cli", "--",
         "attach", "--cdp", "http://127.0.0.1:9333"],
    )
    npm_lh = _FakeProc(
        10311,
        ["npm", "exec", "lighthouse", "--",
         "--port", "9333", "https://example.com"],
    )
    pnpm_lh = _FakeProc(
        10312,
        ["pnpm", "exec", "lighthouse", "--", "--port", "9333"],
    )
    yarn_lh = _FakeProc(
        10313,
        ["yarn", "exec", "lighthouse", "--", "--port", "9333"],
    )
    bun_lh = _FakeProc(
        10314,
        ["bun", "x", "lighthouse", "--", "--port", "9333"],
    )
    channel = _FakeProc(10306, ["playwright-cli", "attach", "--cdp", "chrome"])
    extension = _FakeProc(10307, ["playwright-cli", "attach", "--extension"])
    core = _FakeProc(
        10308,
        ["npx", "playwright-core", "attach", "--cdp", "http://127.0.0.1:9333"],
    )
    other = _FakeProc(
        10309,
        ["playwright-cli", "attach", "--cdp", "http://127.0.0.1:9222"],
    )
    bash_parent = _FakeProc(
        10310,
        ["/bin/bash", "-c",
         "playwright-cli attach --cdp http://127.0.0.1:9333"],
    )
    lease.acquire("human")
    n = interrupt_unregistered_dock_cli(
        processes=[
            leftover, equals, via_npx, packaged, shebang, npm_exec,
            npm_lh, pnpm_lh, yarn_lh, bun_lh,
            channel, extension, core, other, bash_parent,
        ],
        chromium_pid=9999,
        owner_daemon_pid=9998,
    )
    assert n == 10
    assert leftover.killed == 1
    assert equals.killed == 1
    assert via_npx.killed == 1
    assert packaged.killed == 1
    assert shebang.killed == 1
    assert npm_exec.killed == 1
    assert npm_lh.killed == 1
    assert pnpm_lh.killed == 1
    assert yarn_lh.killed == 1
    assert bun_lh.killed == 1
    assert channel.killed == 0
    assert extension.killed == 0
    assert core.killed == 0
    assert other.killed == 0
    assert bash_parent.killed == 0


def test_unregistered_playwright_mcp_dock_cdp_killed_on_takeover():
    """terminal() @playwright/mcp --cdp-endpoint on the dock is leftover action.

    Finding 84 correctly refused to treat ``@playwright/mcp`` as the Playwright
    CLI (path parts are ``@playwright`` + ``mcp``). The MCP server still
    ``connectOverCDP``s and keeps writing after Take over.
    """
    from tools.bot_desktop import browser as bdb
    from tools.browser_tool_session import interrupt_unregistered_dock_cli

    bdb.remember_dock_cdp_port(9333)
    leftover = _FakeProc(
        9000,
        ["npx", "-y", "@playwright/mcp@latest",
         "--cdp-endpoint", "http://127.0.0.1:9333"],
    )
    shebang = _FakeProc(
        9001,
        ["node", "/home/x/node_modules/@playwright/mcp/cli.js",
         "--cdp-endpoint=ws://127.0.0.1:9333/devtools/browser/x"],
    )
    via_env = _FakeProc(
        9002,
        ["npx", "@playwright/mcp"],
        {"PLAYWRIGHT_WS_ENDPOINT": "ws://127.0.0.1:9333/devtools/browser/x"},
    )
    via_mcp_env = _FakeProc(
        9006,
        ["npx", "@playwright/mcp"],
        {"PLAYWRIGHT_MCP_CDP_ENDPOINT": "http://127.0.0.1:9333"},
    )
    no_cdp = _FakeProc(9003, ["npx", "@playwright/mcp"])
    other = _FakeProc(
        9004,
        ["npx", "@playwright/mcp", "--cdp-endpoint", "http://127.0.0.1:9222"],
    )
    bash_parent = _FakeProc(
        9005,
        ["/bin/bash", "-c", "npx @playwright/mcp --cdp-endpoint http://127.0.0.1:9333"],
    )
    lease.acquire("human")
    n = interrupt_unregistered_dock_cli(
        processes=[leftover, shebang, via_env, via_mcp_env, no_cdp, other, bash_parent],
        chromium_pid=9999,
        owner_daemon_pid=9998,
    )
    assert n == 4
    assert leftover.killed == 1
    assert shebang.killed == 1
    assert via_env.killed == 1
    assert via_mcp_env.killed == 1
    assert no_cdp.killed == 0
    assert other.killed == 0
    assert bash_parent.killed == 0


def test_unregistered_chrome_remote_interface_dock_port_killed_on_takeover():
    """terminal() chrome-remote-interface --port <dock> is leftover action.

    The bundled node-inspect skill teaches a require() script whose argv is
    not token-matchable. The CRI CLI is.
    """
    from tools.bot_desktop import browser as bdb
    from tools.browser_tool_session import interrupt_unregistered_dock_cli

    bdb.remember_dock_cdp_port(9333)
    leftover = _FakeProc(
        9100,
        ["npx", "chrome-remote-interface", "--port", "9333", "inspect"],
    )
    short = _FakeProc(
        9101,
        ["chrome-remote-interface", "-p", "9333", "inspect"],
    )
    shebang = _FakeProc(
        9102,
        ["node", "/home/x/node_modules/chrome-remote-interface/bin/client.js",
         "--port=9333", "inspect"],
    )
    via_ws = _FakeProc(
        9103,
        ["chrome-remote-interface", "inspect", "--web-socket",
         "ws://127.0.0.1:9333/devtools/page/1"],
    )
    no_port = _FakeProc(9104, ["npx", "chrome-remote-interface", "inspect"])
    other = _FakeProc(
        9105,
        ["npx", "chrome-remote-interface", "--port", "9222", "inspect"],
    )
    lan = _FakeProc(
        9106,
        ["chrome-remote-interface", "--host", "10.0.0.5", "--port", "9333", "inspect"],
    )
    script = _FakeProc(9107, ["node", "/tmp/cdp-debug.js"])
    bash_parent = _FakeProc(
        9108,
        ["/bin/bash", "-c", "npx chrome-remote-interface --port 9333 inspect"],
    )
    lease.acquire("human")
    n = interrupt_unregistered_dock_cli(
        processes=[leftover, short, shebang, via_ws, no_port, other, lan, script, bash_parent],
        chromium_pid=9999,
        owner_daemon_pid=9998,
    )
    assert n == 4
    assert leftover.killed == 1
    assert short.killed == 1
    assert shebang.killed == 1
    assert via_ws.killed == 1
    assert no_port.killed == 0
    assert other.killed == 0
    assert lan.killed == 0
    assert script.killed == 0
    assert bash_parent.killed == 0


def test_unregistered_chrome_devtools_mcp_dock_url_killed_on_takeover():
    """terminal() chrome-devtools-mcp --browserUrl <dock> is leftover action.

    Hermes docs teach this MCP as a live-Chrome attach. --autoConnect / no
    URL is a Chrome we cannot prove is this jar.
    """
    from tools.bot_desktop import browser as bdb
    from tools.browser_tool_session import interrupt_unregistered_dock_cli

    bdb.remember_dock_cdp_port(9333)
    leftover = _FakeProc(
        9200,
        ["npx", "-y", "chrome-devtools-mcp@latest",
         "--browserUrl", "http://127.0.0.1:9333"],
    )
    kebab = _FakeProc(
        9201,
        ["npx", "chrome-devtools-mcp",
         "--browser-url=http://127.0.0.1:9333"],
    )
    shebang = _FakeProc(
        9202,
        ["node", "/home/x/node_modules/chrome-devtools-mcp/build/src/index.js",
         "--wsEndpoint", "ws://127.0.0.1:9333/devtools/browser/x"],
    )
    short = _FakeProc(
        9203,
        ["chrome-devtools-mcp", "-u", "http://127.0.0.1:9333"],
    )
    auto = _FakeProc(
        9204,
        ["npx", "chrome-devtools-mcp", "--autoConnect"],
    )
    other = _FakeProc(
        9205,
        ["npx", "chrome-devtools-mcp", "--browserUrl", "http://127.0.0.1:9222"],
    )
    lan = _FakeProc(
        9206,
        ["npx", "chrome-devtools-mcp", "--browserUrl", "http://10.0.0.5:9333"],
    )
    bash_parent = _FakeProc(
        9207,
        ["/bin/bash", "-c",
         "npx chrome-devtools-mcp --browserUrl http://127.0.0.1:9333"],
    )
    lease.acquire("human")
    n = interrupt_unregistered_dock_cli(
        processes=[leftover, kebab, shebang, short, auto, other, lan, bash_parent],
        chromium_pid=9999,
        owner_daemon_pid=9998,
    )
    assert n == 4
    assert leftover.killed == 1
    assert kebab.killed == 1
    assert shebang.killed == 1
    assert short.killed == 1
    assert auto.killed == 0
    assert other.killed == 0
    assert lan.killed == 0
    assert bash_parent.killed == 0


def test_unregistered_chrome_devtools_mcp_user_data_dir_killed_on_takeover():
    """terminal() chrome-devtools-mcp --userDataDir <dock> is leftover launch.

    Official pin conflicts with --browserUrl / --wsEndpoint / --isolated.
    Finding 89 only checked URL attach, so Take over left the
    launch-on-jar writer on the cookie jar a human holds. Relative pin
    uses the writer cwd. Other jar / --autoConnect / attach-to-other
    stay unknown. Bash -c parent is still not the writer.
    """
    from tools.bot_desktop import browser as bdb
    from tools.browser_tool_session import interrupt_unregistered_dock_cli

    profile = bdb.profile_dir()
    leftover = _FakeProc(
        9210,
        ["npx", "chrome-devtools-mcp", "--userDataDir", str(profile)],
    )
    kebab = _FakeProc(
        9211,
        ["npx", "chrome-devtools-mcp", f"--user-data-dir={profile}"],
    )
    relative = _FakeProc(
        9212,
        ["npx", "chrome-devtools-mcp", "--userDataDir", "browser-profile"],
        cwd=profile.parent,
    )
    npm_exec = _FakeProc(
        9213,
        ["npm", "exec", "--package=chrome-devtools-mcp", "--",
         "--userDataDir", str(profile)],
    )
    shebang = _FakeProc(
        9214,
        ["node", "/home/x/node_modules/chrome-devtools-mcp/build/src/index.js",
         "--userDataDir", str(profile)],
    )
    other = _FakeProc(
        9215,
        ["npx", "chrome-devtools-mcp", "--userDataDir", "/tmp/other-chrome"],
    )
    auto = _FakeProc(
        9216,
        ["npx", "chrome-devtools-mcp", "--autoConnect"],
    )
    isolated = _FakeProc(
        9217,
        ["npx", "chrome-devtools-mcp", "--isolated"],
    )
    url_wins = _FakeProc(
        9218,
        ["npx", "chrome-devtools-mcp",
         "--browserUrl", "http://127.0.0.1:9222",
         "--userDataDir", str(profile)],
    )
    wrong_cwd = _FakeProc(
        9219,
        ["npx", "chrome-devtools-mcp", "--userDataDir", "browser-profile"],
        cwd=profile.parent.parent,
    )
    bash_parent = _FakeProc(
        9220,
        ["/bin/bash", "-c",
         f"npx chrome-devtools-mcp --userDataDir {profile}"],
    )
    lease.acquire("human")
    n = interrupt_unregistered_dock_cli(
        processes=[
            leftover, kebab, relative, npm_exec, shebang, other, auto,
            isolated, url_wins, wrong_cwd, bash_parent,
        ],
        chromium_pid=9999,
        owner_daemon_pid=9998,
    )
    assert n == 5
    assert leftover.killed == 1
    assert kebab.killed == 1
    assert relative.killed == 1
    assert npm_exec.killed == 1
    assert shebang.killed == 1
    assert other.killed == 0
    assert auto.killed == 0
    assert isolated.killed == 0
    assert url_wins.killed == 0
    assert wrong_cwd.killed == 0
    assert bash_parent.killed == 0


def test_unregistered_chrome_devtools_cli_killed_on_takeover():
    """Official leftover CLI is argv0 chrome-devtools, not only -mcp.

    chrome-devtools-mcp ships bin chrome-devtools. Docs teach
    ``chrome-devtools start --userDataDir`` / ``--browserUrl``. Finding
    89 / 123 only matched the MCP name, so Take over left that writer
    on the jar a human holds. fill / no pin / frontend / other jar and
    the bash -c parent stay up.
    """
    from tools.bot_desktop import browser as bdb
    from tools.browser_tool_session import interrupt_unregistered_dock_cli

    profile = bdb.profile_dir()
    bdb.remember_dock_cdp_port(9333)
    leftover = _FakeProc(
        9230,
        ["chrome-devtools", "start", "--userDataDir", str(profile)],
    )
    npx_cli = _FakeProc(
        9231,
        ["npx", "chrome-devtools", "start",
         "--browserUrl", "http://127.0.0.1:9333"],
    )
    shebang = _FakeProc(
        9232,
        ["node", "/home/x/node_modules/chrome-devtools-mcp/build/src/bin/chrome-devtools.js",
         "start", f"--user-data-dir={profile}"],
    )
    kebab = _FakeProc(
        9233,
        ["chrome-devtools", "start", f"--user-data-dir={profile}"],
    )
    fill = _FakeProc(
        9234,
        ["chrome-devtools", "fill", "1", "uid", "secret"],
    )
    frontend = _FakeProc(
        9235,
        ["npx", "chrome-devtools-frontend"],
    )
    other = _FakeProc(
        9236,
        ["chrome-devtools", "start", "--userDataDir", "/tmp/other-chrome"],
    )
    auto = _FakeProc(
        9237,
        ["chrome-devtools", "start", "--autoConnect"],
    )
    bash_parent = _FakeProc(
        9238,
        ["/bin/bash", "-c",
         f"chrome-devtools start --userDataDir {profile}"],
    )
    lease.acquire("human")
    n = interrupt_unregistered_dock_cli(
        processes=[
            leftover, npx_cli, shebang, kebab, fill, frontend, other,
            auto, bash_parent,
        ],
        chromium_pid=9999,
        owner_daemon_pid=9998,
    )
    assert n == 4
    assert leftover.killed == 1
    assert npx_cli.killed == 1
    assert shebang.killed == 1
    assert kebab.killed == 1
    assert fill.killed == 0
    assert frontend.killed == 0
    assert other.killed == 0
    assert auto.killed == 0
    assert bash_parent.killed == 0


def test_unregistered_package_exec_dock_cli_killed_on_takeover():
    """pnpm exec / npm exec / yarn dlx leftover writers missed npx matching."""
    from tools.bot_desktop import browser as bdb
    from tools.browser_tool_session import interrupt_unregistered_dock_cli

    bdb.remember_dock_cdp_port(9333)
    leftover = _FakeProc(
        9300,
        ["pnpm", "exec", "chrome-devtools-mcp",
         "--browserUrl", "http://127.0.0.1:9333"],
    )
    npm_pkg = _FakeProc(
        9301,
        ["npm", "exec", "--package=chrome-devtools-mcp", "--",
         "--browserUrl", "http://127.0.0.1:9333"],
    )
    yarn = _FakeProc(
        9302,
        ["yarn", "dlx", "@playwright/mcp",
         "--cdp-endpoint", "http://127.0.0.1:9333"],
    )
    playwright = _FakeProc(
        9303,
        ["npm", "exec", "--", "playwright", "codegen",
         "--cdp-endpoint", "http://127.0.0.1:9333"],
    )
    run_script = _FakeProc(
        9304,
        ["pnpm", "run", "chrome-devtools-mcp",
         "--browserUrl", "http://127.0.0.1:9333"],
    )
    other = _FakeProc(
        9305,
        ["pnpm", "exec", "chrome-devtools-mcp",
         "--browserUrl", "http://127.0.0.1:9222"],
    )
    install = _FakeProc(9306, ["npm", "install", "chrome-devtools-mcp"])
    bash_parent = _FakeProc(
        9307,
        ["/bin/bash", "-c",
         "pnpm exec chrome-devtools-mcp --browserUrl http://127.0.0.1:9333"],
    )
    lease.acquire("human")
    n = interrupt_unregistered_dock_cli(
        processes=[leftover, npm_pkg, yarn, playwright, run_script, other, install, bash_parent],
        chromium_pid=9999,
        owner_daemon_pid=9998,
    )
    assert n == 4
    assert leftover.killed == 1
    assert npm_pkg.killed == 1
    assert yarn.killed == 1
    assert playwright.killed == 1
    assert run_script.killed == 0
    assert other.killed == 0
    assert install.killed == 0
    assert bash_parent.killed == 0


def test_unregistered_package_exec_dir_flag_dock_cli_killed_on_takeover():
    """``pnpm --dir /tmp exec lighthouse --port <dock>`` missed leftover unwrap.

    ``_first_non_flag_tokens`` treated the directory as the command, so
    invocation was false and Take over left the writer running. Equals
    ``--dir=/tmp`` already matched. ``pnpm run --dir`` is not an invocation.
    """
    from tools.bot_desktop import browser as bdb
    from tools.browser_tool_session import interrupt_unregistered_dock_cli

    bdb.remember_dock_cdp_port(9333)
    pnpm_dir = _FakeProc(
        9600,
        ["pnpm", "--dir", "/tmp/ws", "exec", "lighthouse",
         "--port", "9333", "https://example.com"],
    )
    pnpm_c = _FakeProc(
        9601,
        ["pnpm", "-C", "/tmp/ws", "exec", "lighthouse",
         "--port=9333", "https://example.com"],
    )
    after_exec = _FakeProc(
        9602,
        ["pnpm", "exec", "--dir", "/tmp/ws", "lighthouse",
         "--port", "9333", "https://example.com"],
    )
    npm_prefix = _FakeProc(
        9603,
        ["npm", "--prefix", "/tmp/ws", "exec", "lighthouse",
         "--port", "9333", "https://example.com"],
    )
    yarn_cwd = _FakeProc(
        9604,
        ["yarn", "--cwd", "/tmp/ws", "dlx", "chrome-devtools-mcp",
         "--browserUrl", "http://127.0.0.1:9333"],
    )
    bun_cwd = _FakeProc(
        9605,
        ["bun", "--cwd", "/tmp/ws", "x", "lighthouse",
         "--port", "9333", "https://example.com"],
    )
    corepack_dir = _FakeProc(
        9606,
        ["corepack", "pnpm", "--dir", "/tmp/ws", "exec", "lighthouse",
         "--port", "9333", "https://example.com"],
    )
    filter_pkg = _FakeProc(
        9607,
        ["pnpm", "--filter", "@scope/pkg", "exec", "lighthouse",
         "--port", "9333", "https://example.com"],
    )
    run_dir = _FakeProc(
        9608,
        ["pnpm", "--dir", "/tmp/ws", "run", "lighthouse",
         "--port", "9333", "https://example.com"],
    )
    other = _FakeProc(
        9609,
        ["pnpm", "--dir", "/tmp/ws", "exec", "lighthouse",
         "--port", "9222", "https://example.com"],
    )
    bash_parent = _FakeProc(
        9610,
        ["/bin/bash", "-c",
         "pnpm --dir /tmp/ws exec lighthouse --port 9333"],
    )
    lease.acquire("human")
    n = interrupt_unregistered_dock_cli(
        processes=[
            pnpm_dir, pnpm_c, after_exec, npm_prefix, yarn_cwd, bun_cwd,
            corepack_dir, filter_pkg, run_dir, other, bash_parent,
        ],
        chromium_pid=9999,
        owner_daemon_pid=9998,
    )
    assert n == 8
    assert pnpm_dir.killed == 1
    assert pnpm_c.killed == 1
    assert after_exec.killed == 1
    assert npm_prefix.killed == 1
    assert yarn_cwd.killed == 1
    assert bun_cwd.killed == 1
    assert corepack_dir.killed == 1
    assert filter_pkg.killed == 1
    assert run_dir.killed == 0
    assert other.killed == 0
    assert bash_parent.killed == 0


def test_unregistered_npx_prefix_dock_cli_killed_on_takeover():
    """``npx --prefix /tmp lighthouse --port <dock>`` missed leftover unwrap.

    ``_first_non_flag_tokens`` treated the prefix as the package, so
    invocation was false and Take over left the writer running. Equals
    ``--prefix=/tmp`` already matched. ``npx -p lighthouse --port``
    (no binary repeat) must stay a leftover — do not swallow ``-p``.
    """
    from tools.bot_desktop import browser as bdb
    from tools.browser_tool_session import interrupt_unregistered_dock_cli

    bdb.remember_dock_cdp_port(9333)
    npx_prefix = _FakeProc(
        9700,
        ["npx", "--prefix", "/tmp/ws", "lighthouse",
         "--port", "9333", "https://example.com"],
    )
    npx_yes = _FakeProc(
        9701,
        ["npx", "-y", "--prefix", "/tmp/ws", "lighthouse@latest",
         "--port", "9333", "https://example.com"],
    )
    pnpx_prefix = _FakeProc(
        9702,
        ["pnpx", "--prefix", "/tmp/ws", "lighthouse",
         "--port=9333", "https://example.com"],
    )
    bunx_cwd = _FakeProc(
        9703,
        ["bunx", "--cwd", "/tmp/ws", "lighthouse",
         "--port", "9333", "https://example.com"],
    )
    mcp = _FakeProc(
        9704,
        ["npx", "--prefix", "/tmp/ws", "chrome-devtools-mcp",
         "--browserUrl", "http://127.0.0.1:9333"],
    )
    playwright = _FakeProc(
        9705,
        ["npx", "--prefix", "/tmp/ws", "playwright", "codegen",
         "--cdp-endpoint", "http://127.0.0.1:9333"],
    )
    agent_browser = _FakeProc(
        9706,
        ["npx", "--prefix", "/tmp/ws", "agent-browser",
         "--cdp", "http://127.0.0.1:9333", "fill"],
    )
    pin_no_binary = _FakeProc(
        9707,
        ["npx", "-p", "lighthouse", "--port", "9333", "https://example.com"],
    )
    other = _FakeProc(
        9708,
        ["npx", "--prefix", "/tmp/ws", "lighthouse",
         "--port", "9222", "https://example.com"],
    )
    bash_parent = _FakeProc(
        9709,
        ["/bin/bash", "-c",
         "npx --prefix /tmp/ws lighthouse --port 9333"],
    )
    lease.acquire("human")
    n = interrupt_unregistered_dock_cli(
        processes=[
            npx_prefix, npx_yes, pnpx_prefix, bunx_cwd, mcp, playwright,
            agent_browser, pin_no_binary, other, bash_parent,
        ],
        chromium_pid=9999,
        owner_daemon_pid=9998,
    )
    assert n == 8
    assert npx_prefix.killed == 1
    assert npx_yes.killed == 1
    assert pnpx_prefix.killed == 1
    assert bunx_cwd.killed == 1
    assert mcp.killed == 1
    assert playwright.killed == 1
    assert agent_browser.killed == 1
    assert pin_no_binary.killed == 1
    assert other.killed == 0
    assert bash_parent.killed == 0


def test_unregistered_workspace_selector_dock_cli_killed_on_takeover():
    """Workspace name hid leftover unwrap (npx --workspace / npm -w / yarn).

    ``npx --workspace web lighthouse --port <dock>`` treated ``web`` as
    the package. ``npm -w`` is the documented workspace short flag
    finding 102's ``--workspace`` skip missed. ``yarn workspace web
    exec`` never entered leftover exec. pnpx ``-w lighthouse`` must
    stay a leftover — pnpm ``-w`` is boolean. ``yarn workspace run``
    is not an invocation.
    """
    from tools.bot_desktop import browser as bdb
    from tools.browser_tool_session import interrupt_unregistered_dock_cli

    bdb.remember_dock_cdp_port(9333)
    npx_ws = _FakeProc(
        9800,
        ["npx", "--workspace", "web", "lighthouse",
         "--port", "9333", "https://example.com"],
    )
    npx_w = _FakeProc(
        9801,
        ["npx", "-w", "web", "lighthouse",
         "--port", "9333", "https://example.com"],
    )
    npm_w = _FakeProc(
        9802,
        ["npm", "-w", "@scope/web", "exec", "lighthouse",
         "--port", "9333", "https://example.com"],
    )
    yarn_ws = _FakeProc(
        9803,
        ["yarn", "workspace", "web", "exec", "lighthouse",
         "--port", "9333", "https://example.com"],
    )
    yarn_after_sep = _FakeProc(
        9804,
        ["yarn", "workspace", "web", "exec", "--", "lighthouse",
         "--port", "9333", "https://example.com"],
    )
    mcp = _FakeProc(
        9805,
        ["yarn", "workspace", "web", "exec", "chrome-devtools-mcp",
         "--browserUrl", "http://127.0.0.1:9333"],
    )
    pnpx_boolean_w = _FakeProc(
        9806,
        ["pnpx", "-w", "lighthouse",
         "--port", "9333", "https://example.com"],
    )
    run_script = _FakeProc(
        9807,
        ["yarn", "workspace", "web", "run", "lighthouse",
         "--port", "9333", "https://example.com"],
    )
    foreach = _FakeProc(
        9808,
        ["yarn", "workspaces", "foreach", "exec", "lighthouse",
         "--port", "9333", "https://example.com"],
    )
    other = _FakeProc(
        9809,
        ["npx", "--workspace", "web", "lighthouse",
         "--port", "9222", "https://example.com"],
    )
    bash_parent = _FakeProc(
        9810,
        ["/bin/bash", "-c",
         "yarn workspace web exec lighthouse --port 9333"],
    )
    lease.acquire("human")
    n = interrupt_unregistered_dock_cli(
        processes=[
            npx_ws, npx_w, npm_w, yarn_ws, yarn_after_sep, mcp,
            pnpx_boolean_w, run_script, foreach, other, bash_parent,
        ],
        chromium_pid=9999,
        owner_daemon_pid=9998,
    )
    assert n == 7
    assert npx_ws.killed == 1
    assert npx_w.killed == 1
    assert npm_w.killed == 1
    assert yarn_ws.killed == 1
    assert yarn_after_sep.killed == 1
    assert mcp.killed == 1
    assert pnpx_boolean_w.killed == 1
    assert run_script.killed == 0
    assert foreach.killed == 0
    assert other.killed == 0
    assert bash_parent.killed == 0


def test_unregistered_npx_package_pin_dock_cli_killed_on_takeover():
    """``npx --package=lighthouse -- --port <dock>`` hid leftover unwrap.

    Equals ``--package=`` with no binary repeat missed invocation.
    Space ``-p lighthouse -- --port`` matched invocation but
    ``_flag_value`` stopped at npx's ``--`` and missed the aim.
    ``npm exec --package=`` already peeled. Last-wins pin. Child
    ``--`` after ``--port`` must still aim. Bash ``-c`` parent is
    not the writer.
    """
    from tools.bot_desktop import browser as bdb
    from tools.browser_tool_session import interrupt_unregistered_dock_cli

    bdb.remember_dock_cdp_port(9333)
    equals_sep = _FakeProc(
        9900,
        ["npx", "--package=lighthouse", "--",
         "--port", "9333", "https://example.com"],
    )
    equals_inline = _FakeProc(
        9901,
        ["npx", "--package=lighthouse",
         "--port", "9333", "https://example.com"],
    )
    space_sep = _FakeProc(
        9902,
        ["npx", "--package", "lighthouse", "--",
         "--port", "9333", "https://example.com"],
    )
    short_sep = _FakeProc(
        9903,
        ["npx", "-p", "lighthouse", "--",
         "--port", "9333", "https://example.com"],
    )
    mcp = _FakeProc(
        9904,
        ["npx", "--package=chrome-devtools-mcp", "--",
         "--browserUrl", "http://127.0.0.1:9333"],
    )
    child_term = _FakeProc(
        9905,
        ["npx", "lighthouse", "--port", "9333", "--",
         "https://example.com"],
    )
    last_wins = _FakeProc(
        9906,
        ["npx", "--package=ruff", "--package=lighthouse", "--",
         "--port", "9333", "https://example.com"],
    )
    later_other = _FakeProc(
        9907,
        ["npx", "--package=lighthouse", "--package=ruff", "--",
         "--port", "9333", "https://example.com"],
    )
    other_port = _FakeProc(
        9908,
        ["npx", "--package=lighthouse", "--",
         "--port", "9222", "https://example.com"],
    )
    bash_parent = _FakeProc(
        9909,
        ["/bin/bash", "-c",
         "npx --package=lighthouse -- --port 9333"],
    )
    lease.acquire("human")
    n = interrupt_unregistered_dock_cli(
        processes=[
            equals_sep, equals_inline, space_sep, short_sep, mcp,
            child_term, last_wins, later_other, other_port, bash_parent,
        ],
        chromium_pid=9999,
        owner_daemon_pid=9998,
    )
    assert n == 7
    assert equals_sep.killed == 1
    assert equals_inline.killed == 1
    assert space_sep.killed == 1
    assert short_sep.killed == 1
    assert mcp.killed == 1
    assert child_term.killed == 1
    assert last_wins.killed == 1
    assert later_other.killed == 0
    assert other_port.killed == 0
    assert bash_parent.killed == 0


def test_unregistered_yarn_npm_exec_dock_cli_killed_on_takeover():
    """``yarn npm exec lighthouse --port <dock>`` missed leftover unwrap.

    Yarn Berry keeps argv0 ``yarn``, so leftover unwrap never entered
    ``npm exec``. ``yarn npm install`` is not an invocation. ``yarn
    exec`` / ``yarn dlx`` already matched. Bash ``-c`` parent is not
    the writer.
    """
    from tools.bot_desktop import browser as bdb
    from tools.browser_tool_session import interrupt_unregistered_dock_cli

    bdb.remember_dock_cdp_port(9333)
    leftover = _FakeProc(
        10000,
        ["yarn", "npm", "exec", "lighthouse",
         "--port", "9333", "https://example.com"],
    )
    after_sep = _FakeProc(
        10001,
        ["yarn", "npm", "exec", "--", "lighthouse",
         "--port", "9333", "https://example.com"],
    )
    packaged = _FakeProc(
        10002,
        ["yarn", "npm", "exec", "--package=lighthouse", "--",
         "--port", "9333", "https://example.com"],
    )
    npm_x = _FakeProc(
        10003,
        ["yarn", "npm", "x", "lighthouse",
         "--port", "9333", "https://example.com"],
    )
    cwd = _FakeProc(
        10004,
        ["yarn", "--cwd", "/tmp/ws", "npm", "exec", "lighthouse",
         "--port", "9333", "https://example.com"],
    )
    mcp = _FakeProc(
        10005,
        ["yarn", "npm", "exec", "chrome-devtools-mcp",
         "--browserUrl", "http://127.0.0.1:9333"],
    )
    corepack = _FakeProc(
        10006,
        ["corepack", "yarn", "npm", "exec", "lighthouse",
         "--port", "9333", "https://example.com"],
    )
    install = _FakeProc(10007, ["yarn", "npm", "install", "lighthouse"])
    other = _FakeProc(
        10008,
        ["yarn", "npm", "exec", "lighthouse",
         "--port", "9222", "https://example.com"],
    )
    bash_parent = _FakeProc(
        10009,
        ["/bin/bash", "-c",
         "yarn npm exec lighthouse --port 9333"],
    )
    lease.acquire("human")
    n = interrupt_unregistered_dock_cli(
        processes=[
            leftover, after_sep, packaged, npm_x, cwd, mcp, corepack,
            install, other, bash_parent,
        ],
        chromium_pid=9999,
        owner_daemon_pid=9998,
    )
    assert n == 7
    assert leftover.killed == 1
    assert after_sep.killed == 1
    assert packaged.killed == 1
    assert npm_x.killed == 1
    assert cwd.killed == 1
    assert mcp.killed == 1
    assert corepack.killed == 1
    assert install.killed == 0
    assert other.killed == 0
    assert bash_parent.killed == 0


def test_unregistered_shebang_node_pm_dock_cli_killed_on_takeover():
    """Shebang ``node …/yarn.js|npx-cli.js`` hid leftover ``--package=`` pins.

    After Linux shebang the leftover writer is ``node``, so findings
    105–106 never ran. ``node /tmp/other.js --package=lighthouse`` is
    not a package-manager entry. Bash ``-c`` parent is not the writer.
    """
    from tools.bot_desktop import browser as bdb
    from tools.browser_tool_session import interrupt_unregistered_dock_cli

    bdb.remember_dock_cdp_port(9333)
    yarn_js = _FakeProc(
        10100,
        ["node", "/usr/share/yarn/bin/yarn.js", "npm", "exec",
         "--package=lighthouse", "--",
         "--port", "9333", "https://example.com"],
    )
    berry = _FakeProc(
        10101,
        ["node", "/home/x/.yarn/releases/yarn-4.9.2.cjs", "npm", "exec",
         "--package=lighthouse", "--",
         "--port", "9333", "https://example.com"],
    )
    npx_equals = _FakeProc(
        10102,
        ["node", "/usr/lib/node_modules/npm/bin/npx-cli.js",
         "--package=lighthouse", "--",
         "--port", "9333", "https://example.com"],
    )
    npx_short = _FakeProc(
        10103,
        ["node", "/usr/lib/node_modules/npm/bin/npx-cli.js",
         "-p", "lighthouse", "--",
         "--port", "9333", "https://example.com"],
    )
    npm_cli = _FakeProc(
        10104,
        ["node", "/usr/lib/node_modules/npm/bin/npm-cli.js", "exec",
         "--package=lighthouse", "--",
         "--port", "9333", "https://example.com"],
    )
    pnpm_cjs = _FakeProc(
        10105,
        ["node", "/usr/lib/node_modules/pnpm/bin/pnpm.cjs", "exec",
         "--package=lighthouse", "--",
         "--port", "9333", "https://example.com"],
    )
    mcp = _FakeProc(
        10106,
        ["node", "/usr/lib/node_modules/npm/bin/npx-cli.js",
         "--package=chrome-devtools-mcp", "--",
         "--browserUrl", "http://127.0.0.1:9333"],
    )
    other_js = _FakeProc(
        10107,
        ["node", "/tmp/other.js", "--package=lighthouse", "--",
         "--port", "9333", "https://example.com"],
    )
    other_port = _FakeProc(
        10108,
        ["node", "/usr/lib/node_modules/npm/bin/npx-cli.js",
         "--package=lighthouse", "--",
         "--port", "9222", "https://example.com"],
    )
    bash_parent = _FakeProc(
        10109,
        ["/bin/bash", "-c",
         "node /usr/share/yarn/bin/yarn.js npm exec --package=lighthouse -- --port 9333"],
    )
    lease.acquire("human")
    n = interrupt_unregistered_dock_cli(
        processes=[
            yarn_js, berry, npx_equals, npx_short, npm_cli, pnpm_cjs, mcp,
            other_js, other_port, bash_parent,
        ],
        chromium_pid=9999,
        owner_daemon_pid=9998,
    )
    assert n == 7
    assert yarn_js.killed == 1
    assert berry.killed == 1
    assert npx_equals.killed == 1
    assert npx_short.killed == 1
    assert npm_cli.killed == 1
    assert pnpm_cjs.killed == 1
    assert mcp.killed == 1
    assert other_js.killed == 0
    assert other_port.killed == 0
    assert bash_parent.killed == 0


def test_unregistered_corepack_shim_dock_cli_killed_on_takeover():
    """Corepack ``yarn`` / ``corepack.js`` shebangs hid leftover ``--package=``.

    Field ``yarn`` on Node 16+ is ``#!/usr/bin/env node`` →
    ``…/corepack/dist/yarn.js``. Finding 107 required a ``yarn/``
    package dir, so leftover unwrap never ran. ``node /tmp/corepack.js``
    is not a package-manager entry. Bash ``-c`` parent is not the writer.
    """
    from tools.bot_desktop import browser as bdb
    from tools.browser_tool_session import interrupt_unregistered_dock_cli

    bdb.remember_dock_cdp_port(9333)
    corepack_yarn = _FakeProc(
        10200,
        ["node", "/usr/lib/node_modules/corepack/dist/corepack.js",
         "yarn", "npm", "exec", "--package=lighthouse", "--",
         "--port", "9333", "https://example.com"],
    )
    corepack_pnpm = _FakeProc(
        10201,
        ["node", "/usr/lib/node_modules/corepack/dist/corepack.js",
         "pnpm", "exec", "--package=lighthouse", "--",
         "--port", "9333", "https://example.com"],
    )
    corepack_npx = _FakeProc(
        10202,
        ["node", "/usr/lib/node_modules/corepack/dist/corepack.js", "npx",
         "--package=lighthouse", "--",
         "--port", "9333", "https://example.com"],
    )
    yarn_shim = _FakeProc(
        10203,
        ["node", "/usr/lib/node_modules/corepack/dist/yarn.js", "npm", "exec",
         "--package=lighthouse", "--",
         "--port", "9333", "https://example.com"],
    )
    yarnpkg = _FakeProc(
        10204,
        ["node", "/usr/lib/node_modules/corepack/dist/yarnpkg.js", "npm", "exec",
         "--package=lighthouse", "--",
         "--port", "9333", "https://example.com"],
    )
    pnpm_shim = _FakeProc(
        10205,
        ["node", "/usr/lib/node_modules/corepack/dist/pnpm.js", "exec",
         "--package=lighthouse", "--",
         "--port", "9333", "https://example.com"],
    )
    npm_shim = _FakeProc(
        10206,
        ["node", "/usr/lib/node_modules/corepack/dist/npm.js", "exec",
         "--package=lighthouse", "--",
         "--port", "9333", "https://example.com"],
    )
    pnpx_shim = _FakeProc(
        10207,
        ["node", "/usr/lib/node_modules/corepack/dist/pnpx.js",
         "--package=lighthouse", "--",
         "--port", "9333", "https://example.com"],
    )
    mcp = _FakeProc(
        10208,
        ["node", "/usr/lib/node_modules/corepack/dist/corepack.js", "npx",
         "--package=chrome-devtools-mcp", "--",
         "--browserUrl", "http://127.0.0.1:9333"],
    )
    tmp_js = _FakeProc(
        10209,
        ["node", "/tmp/corepack.js", "yarn", "npm", "exec",
         "--package=lighthouse", "--",
         "--port", "9333", "https://example.com"],
    )
    enable = _FakeProc(
        10210,
        ["node", "/usr/lib/node_modules/corepack/dist/corepack.js", "enable"],
    )
    install = _FakeProc(
        10211,
        ["node", "/usr/lib/node_modules/corepack/dist/yarn.js", "npm",
         "install", "lighthouse"],
    )
    other_port = _FakeProc(
        10212,
        ["node", "/usr/lib/node_modules/corepack/dist/yarn.js", "npm", "exec",
         "--package=lighthouse", "--",
         "--port", "9222", "https://example.com"],
    )
    bash_parent = _FakeProc(
        10213,
        ["/bin/bash", "-c",
         "node /usr/lib/node_modules/corepack/dist/yarn.js npm exec --package=lighthouse -- --port 9333"],
    )
    lease.acquire("human")
    n = interrupt_unregistered_dock_cli(
        processes=[
            corepack_yarn, corepack_pnpm, corepack_npx, yarn_shim, yarnpkg,
            pnpm_shim, npm_shim, pnpx_shim, mcp, tmp_js, enable, install,
            other_port, bash_parent,
        ],
        chromium_pid=9999,
        owner_daemon_pid=9998,
    )
    assert n == 9
    assert corepack_yarn.killed == 1
    assert corepack_pnpm.killed == 1
    assert corepack_npx.killed == 1
    assert yarn_shim.killed == 1
    assert yarnpkg.killed == 1
    assert pnpm_shim.killed == 1
    assert npm_shim.killed == 1
    assert pnpx_shim.killed == 1
    assert mcp.killed == 1
    assert tmp_js.killed == 0
    assert enable.killed == 0
    assert install.killed == 0
    assert other_port.killed == 0
    assert bash_parent.killed == 0


def test_unregistered_bun_x_dock_cli_killed_on_takeover():
    """bun x / bun exec leftover writers missed bunx matching."""
    from tools.bot_desktop import browser as bdb
    from tools.browser_tool_session import interrupt_unregistered_dock_cli

    bdb.remember_dock_cdp_port(9333)
    leftover = _FakeProc(
        9600,
        ["bun", "x", "lighthouse",
         "--port", "9333", "https://example.com"],
    )
    bun_flag = _FakeProc(
        9601,
        ["bun", "--bun", "x", "lighthouse",
         "--port=9333", "https://example.com"],
    )
    bun_exec = _FakeProc(
        9602,
        ["bun", "exec", "chrome-devtools-mcp",
         "--browserUrl", "http://127.0.0.1:9333"],
    )
    run_script = _FakeProc(9603, ["bun", "run", "lighthouse", "--port", "9333"])
    install = _FakeProc(9604, ["bun", "install", "lighthouse"])
    other = _FakeProc(
        9605,
        ["bun", "x", "lighthouse",
         "--port", "9222", "https://example.com"],
    )
    ruff = _FakeProc(9606, ["bun", "x", "ruff"])
    bash_parent = _FakeProc(
        9607,
        ["/bin/bash", "-c", "bun x lighthouse --port 9333 https://example.com"],
    )
    lease.acquire("human")
    n = interrupt_unregistered_dock_cli(
        processes=[
            leftover, bun_flag, bun_exec, run_script, install, other, ruff,
            bash_parent,
        ],
        chromium_pid=9999,
        owner_daemon_pid=9998,
    )
    assert n == 3
    assert leftover.killed == 1
    assert bun_flag.killed == 1
    assert bun_exec.killed == 1
    assert run_script.killed == 0
    assert install.killed == 0
    assert other.killed == 0
    assert ruff.killed == 0
    assert bash_parent.killed == 0


def test_unregistered_corepack_package_exec_dock_cli_killed_on_takeover():
    """corepack npm/pnpm/yarn exec is the leftover writer finding 91 missed."""
    from tools.bot_desktop import browser as bdb
    from tools.browser_tool_session import interrupt_unregistered_dock_cli

    bdb.remember_dock_cdp_port(9333)
    leftover = _FakeProc(
        9500,
        ["corepack", "pnpm", "exec", "lighthouse",
         "--port", "9333", "https://example.com"],
    )
    npm_yes = _FakeProc(
        9501,
        ["corepack", "--yes", "npm", "exec", "--package=lighthouse", "--",
         "--port=9333", "https://example.com"],
    )
    mcp = _FakeProc(
        9502,
        ["corepack", "yarn", "dlx", "chrome-devtools-mcp",
         "--browserUrl", "http://127.0.0.1:9333"],
    )
    enable = _FakeProc(9503, ["corepack", "enable"])
    install = _FakeProc(9504, ["corepack", "npm", "install", "lighthouse"])
    other = _FakeProc(
        9505,
        ["corepack", "pnpm", "exec", "lighthouse",
         "--port", "9222", "https://example.com"],
    )
    bash_parent = _FakeProc(
        9506,
        ["/bin/bash", "-c",
         "corepack pnpm exec lighthouse --port 9333 https://example.com"],
    )
    lease.acquire("human")
    n = interrupt_unregistered_dock_cli(
        processes=[leftover, npm_yes, mcp, enable, install, other, bash_parent],
        chromium_pid=9999,
        owner_daemon_pid=9998,
    )
    assert n == 3
    assert leftover.killed == 1
    assert npm_yes.killed == 1
    assert mcp.killed == 1
    assert enable.killed == 0
    assert install.killed == 0
    assert other.killed == 0
    assert bash_parent.killed == 0


def test_unregistered_lighthouse_dock_port_killed_on_takeover():
    """terminal() lighthouse --port <dock> is leftover CDP action.

    Official attach is ``--port`` (+ optional ``--hostname``, default
    localhost). No ``--port`` / ``--port=0`` launches its own Chrome.
    LAN ``--hostname`` is another machine. Do not treat ``-p`` as the
    port (npm pin / CRI).
    """
    from tools.bot_desktop import browser as bdb
    from tools.browser_tool_session import interrupt_unregistered_dock_cli

    bdb.remember_dock_cdp_port(9333)
    leftover = _FakeProc(
        9400,
        ["npx", "-y", "lighthouse@latest",
         "--port", "9333", "https://example.com"],
    )
    equals = _FakeProc(
        9401,
        ["lighthouse", "--port=9333", "--hostname", "127.0.0.1",
         "https://example.com"],
    )
    shebang = _FakeProc(
        9402,
        ["node", "/home/x/node_modules/lighthouse/cli/index.js",
         "--port=9333", "https://example.com"],
    )
    packaged = _FakeProc(
        9403,
        ["pnpm", "exec", "lighthouse",
         "--port", "9333", "https://example.com"],
    )
    no_port = _FakeProc(
        9404,
        ["npx", "lighthouse", "https://example.com"],
    )
    ephemeral = _FakeProc(
        9405,
        ["npx", "lighthouse", "--port=0", "https://example.com"],
    )
    other = _FakeProc(
        9406,
        ["npx", "lighthouse", "--port", "9222", "https://example.com"],
    )
    lan = _FakeProc(
        9407,
        ["npx", "lighthouse", "--hostname", "10.0.0.5",
         "--port", "9333", "https://example.com"],
    )
    cri_short = _FakeProc(
        9408,
        ["npx", "lighthouse", "-p", "9333", "https://example.com"],
    )
    bash_parent = _FakeProc(
        9409,
        ["/bin/bash", "-c",
         "npx lighthouse --port 9333 https://example.com"],
    )
    last_wins = _FakeProc(
        9410,
        ["npx", "lighthouse", "--port", "1", "--port", "9333",
         "https://example.com"],
    )
    later_other = _FakeProc(
        9411,
        ["npx", "lighthouse", "--port", "9333", "--port", "1",
         "https://example.com"],
    )
    after_terminator = _FakeProc(
        9412,
        ["npx", "lighthouse", "--port", "9333", "--",
         "--port", "1", "https://example.com"],
    )
    lease.acquire("human")
    n = interrupt_unregistered_dock_cli(
        processes=[
            leftover, equals, shebang, packaged, no_port, ephemeral,
            other, lan, cri_short, bash_parent, last_wins, later_other,
            after_terminator,
        ],
        chromium_pid=9999,
        owner_daemon_pid=9998,
    )
    assert n == 6
    assert leftover.killed == 1
    assert equals.killed == 1
    assert shebang.killed == 1
    assert packaged.killed == 1
    assert last_wins.killed == 1
    assert after_terminator.killed == 1
    assert no_port.killed == 0
    assert ephemeral.killed == 0
    assert other.killed == 0
    assert lan.killed == 0
    assert cri_short.killed == 0
    assert bash_parent.killed == 0
    assert later_other.killed == 0


def test_unregistered_lighthouse_chrome_flags_user_data_dir_killed_on_takeover():
    """terminal() lighthouse --chrome-flags --user-data-dir <dock> is leftover launch.

    Official leftover launches Chrome via chrome-launcher, which appends
    chrome-flags after its temp ``--user-data-dir``. Chromium last-wins
    the dock jar. Finding 92 only checked ``--port``, so Take over left
    that writer typing into the jar. ``--port`` to another Chrome, LAN
    hostname, ``--remote-debugging-port`` inside chrome-flags, another
    jar, and the bash ``-c`` parent stay up.
    """
    from tools.bot_desktop import browser as bdb
    from tools.browser_tool_session import interrupt_unregistered_dock_cli

    profile = bdb.profile_dir()
    leftover = _FakeProc(
        9420,
        ["npx", "lighthouse", "https://example.com",
         "--chrome-flags", f"--user-data-dir={profile}"],
    )
    equals = _FakeProc(
        9421,
        ["lighthouse", "https://example.com",
         f"--chrome-flags=--user-data-dir={profile}"],
    )
    grouped = _FakeProc(
        9422,
        ["npx", "-y", "lighthouse@latest", "https://example.com",
         "--chrome-flags", f"--headless --user-data-dir={profile}"],
    )
    camel = _FakeProc(
        9423,
        ["npx", "lighthouse", "https://example.com",
         "--chromeFlags", f"--user-data-dir={profile}"],
    )
    ephemeral = _FakeProc(
        9424,
        ["npx", "lighthouse", "--port=0", "https://example.com",
         "--chrome-flags", f"--user-data-dir={profile}"],
    )
    shebang = _FakeProc(
        9425,
        ["node", "/home/x/node_modules/lighthouse/cli/index.js",
         "https://example.com",
         f"--chrome-flags=--user-data-dir={profile}"],
    )
    relative = _FakeProc(
        9426,
        ["npx", "lighthouse", "https://example.com",
         "--chrome-flags", "--user-data-dir=browser-profile"],
        cwd=profile.parent,
    )
    last_wins = _FakeProc(
        9427,
        ["npx", "lighthouse", "https://example.com",
         "--chrome-flags",
         f"--user-data-dir=/tmp/other --user-data-dir={profile}"],
    )
    other_jar = _FakeProc(
        9428,
        ["npx", "lighthouse", "https://example.com",
         "--chrome-flags", "--user-data-dir=/tmp/other-chrome"],
    )
    other_port = _FakeProc(
        9429,
        ["npx", "lighthouse", "--port", "9222",
         "--chrome-flags", f"--user-data-dir={profile}",
         "https://example.com"],
    )
    lan = _FakeProc(
        9430,
        ["npx", "lighthouse", "--hostname", "10.0.0.5",
         "--chrome-flags", f"--user-data-dir={profile}",
         "https://example.com"],
    )
    debug_port = _FakeProc(
        9431,
        ["npx", "lighthouse", "https://example.com",
         "--chrome-flags", "--remote-debugging-port=9333"],
    )
    no_pin = _FakeProc(
        9432,
        ["npx", "lighthouse", "https://example.com"],
    )
    bash_parent = _FakeProc(
        9433,
        ["/bin/bash", "-c",
         f"npx lighthouse --chrome-flags=--user-data-dir={profile} "
         "https://example.com"],
    )
    lease.acquire("human")
    n = interrupt_unregistered_dock_cli(
        processes=[
            leftover, equals, grouped, camel, ephemeral, shebang,
            relative, last_wins, other_jar, other_port, lan, debug_port,
            no_pin, bash_parent,
        ],
        chromium_pid=9999,
        owner_daemon_pid=9998,
    )
    assert n == 8
    assert leftover.killed == 1
    assert equals.killed == 1
    assert grouped.killed == 1
    assert camel.killed == 1
    assert ephemeral.killed == 1
    assert shebang.killed == 1
    assert relative.killed == 1
    assert last_wins.killed == 1
    assert other_jar.killed == 0
    assert other_port.killed == 0
    assert lan.killed == 0
    assert debug_port.killed == 0
    assert no_pin.killed == 0
    assert bash_parent.killed == 0


def test_unregistered_lighthouse_chrome_flags_groups_and_array_killed_on_takeover():
    """terminal() lighthouse joins every --chrome-flags / array chromeFlags.

    Official leftover ``parseChromeFlags`` accepts a string *or* an
    array (repeated ``--chrome-flags`` / file ``chromeFlags: [...]``).
    Finding 126 / 128 last-wins / string-only left
    ``--chrome-flags=--user-data-dir=<dock> --chrome-flags=--headless``
    and ``{"chromeFlags": ["--user-data-dir=<dock>"]}`` typing into
    the jar. CLI ``--chrome-flags`` still overrides the file. Another
    jar and the bash ``-c`` parent stay up.
    """
    from tools.bot_desktop import browser as bdb
    from tools.browser_tool_session import interrupt_unregistered_dock_cli

    profile = bdb.profile_dir()
    arr = profile.parent / "lh-arr.json"
    arr.parent.mkdir(parents=True, exist_ok=True)
    arr.write_text(json.dumps({
        "chromeFlags": [f"--user-data-dir={profile}"],
    }))
    arr2 = profile.parent / "lh-arr2.json"
    arr2.write_text(json.dumps({
        "chromeFlags": ["--headless", f"--user-data-dir={profile}"],
    }))
    other = profile.parent / "lh-arr-other.json"
    other.write_text(json.dumps({
        "chromeFlags": ["--user-data-dir=/tmp/other-chrome"],
    }))
    leftover = _FakeProc(
        9700,
        ["npx", "lighthouse", "https://example.com",
         "--chrome-flags", f"--user-data-dir={profile}",
         "--chrome-flags", "--headless"],
    )
    equals = _FakeProc(
        9701,
        ["lighthouse", "https://example.com",
         f"--chrome-flags=--user-data-dir={profile}",
         "--chrome-flags=--headless"],
    )
    via_arr = _FakeProc(
        9702,
        ["npx", "lighthouse", "https://example.com",
         "--cli-flags-path", str(arr)],
    )
    via_arr2 = _FakeProc(
        9703,
        ["npx", "lighthouse", "https://example.com",
         "--cli-flags-path", str(arr2)],
    )
    shebang = _FakeProc(
        9704,
        ["node", "/home/x/node_modules/lighthouse/cli/index.js",
         "https://example.com",
         "--chrome-flags", f"--user-data-dir={profile}",
         "--chrome-flags", "--headless"],
    )
    other_jar = _FakeProc(
        9705,
        ["npx", "lighthouse", "https://example.com",
         "--cli-flags-path", str(other)],
    )
    cli_wins = _FakeProc(
        9706,
        ["npx", "lighthouse", "--chrome-flags", "--headless",
         "--cli-flags-path", str(arr), "https://example.com"],
    )
    bash_parent = _FakeProc(
        9707,
        ["/bin/bash", "-c",
         f"npx lighthouse --chrome-flags=--user-data-dir={profile} "
         "--chrome-flags=--headless https://example.com"],
    )
    lease.acquire("human")
    n = interrupt_unregistered_dock_cli(
        processes=[
            leftover, equals, via_arr, via_arr2, shebang, other_jar,
            cli_wins, bash_parent,
        ],
        chromium_pid=9999,
        owner_daemon_pid=9998,
    )
    assert n == 5
    assert leftover.killed == 1
    assert equals.killed == 1
    assert via_arr.killed == 1
    assert via_arr2.killed == 1
    assert shebang.killed == 1
    assert other_jar.killed == 0
    assert cli_wins.killed == 0
    assert bash_parent.killed == 0


def test_unregistered_lighthouse_cli_flags_path_killed_on_takeover():
    """terminal() lighthouse --cli-flags-path leftover hid launch/attach.

    Official leftover loads ``port`` / ``chromeFlags`` from JSON
    (yargs ``config: true``). Finding 126 only checked argv, so Take
    over left that writer typing into the jar. CLI flags still
    override the file. Another jar, LAN hostname, argv ``--port`` to
    another Chrome, ``--config-path`` audit config, and the bash
    ``-c`` parent stay up.
    """
    from tools.bot_desktop import browser as bdb
    from tools.browser_tool_session import interrupt_unregistered_dock_cli

    profile = bdb.profile_dir()
    flags = profile.parent / "lh-flags.json"
    flags.parent.mkdir(parents=True, exist_ok=True)
    flags.write_text(json.dumps({
        "chromeFlags": f"--user-data-dir={profile}",
        "quiet": True,
    }))
    kebab = profile.parent / "lh-kebab.json"
    kebab.write_text(json.dumps({
        "chrome-flags": f"--headless --user-data-dir={profile}",
    }))
    port_flags = profile.parent / "lh-port.json"
    port_flags.write_text(json.dumps({"port": 9333}))
    other = profile.parent / "lh-other.json"
    other.write_text(json.dumps({
        "chromeFlags": "--user-data-dir=/tmp/other-chrome",
    }))
    lan = profile.parent / "lh-lan.json"
    lan.write_text(json.dumps({
        "hostname": "10.0.0.5",
        "chromeFlags": f"--user-data-dir={profile}",
    }))
    audit = profile.parent / "lh-audit.json"
    audit.write_text(json.dumps({
        "chromeFlags": f"--user-data-dir={profile}",
    }))
    bdb.remember_dock_cdp_port(9333)
    leftover = _FakeProc(
        9600,
        ["npx", "lighthouse", "https://example.com",
         "--cli-flags-path", str(flags)],
    )
    equals = _FakeProc(
        9601,
        ["lighthouse", "https://example.com",
         f"--cli-flags-path={flags}"],
    )
    camel = _FakeProc(
        9602,
        ["npx", "lighthouse", "https://example.com",
         "--cliFlagsPath", str(flags)],
    )
    via_kebab = _FakeProc(
        9603,
        ["npx", "lighthouse", "https://example.com",
         "--cli-flags-path", str(kebab)],
    )
    via_port = _FakeProc(
        9604,
        ["npx", "lighthouse", "https://example.com",
         "--cli-flags-path", str(port_flags)],
    )
    relative = _FakeProc(
        9605,
        ["npx", "lighthouse", "https://example.com",
         "--cli-flags-path", "lh-flags.json"],
        cwd=profile.parent,
    )
    shebang = _FakeProc(
        9606,
        ["node", "/home/x/node_modules/lighthouse/cli/index.js",
         "https://example.com", "--cli-flags-path", str(flags)],
    )
    ephemeral = _FakeProc(
        9607,
        ["npx", "lighthouse", "--port=0", "https://example.com",
         "--cli-flags-path", str(flags)],
    )
    other_jar = _FakeProc(
        9608,
        ["npx", "lighthouse", "https://example.com",
         "--cli-flags-path", str(other)],
    )
    lan_host = _FakeProc(
        9609,
        ["npx", "lighthouse", "https://example.com",
         "--cli-flags-path", str(lan)],
    )
    other_port = _FakeProc(
        9610,
        ["npx", "lighthouse", "--port", "9222",
         "--cli-flags-path", str(flags), "https://example.com"],
    )
    argv_overrides = _FakeProc(
        9611,
        ["npx", "lighthouse", "--chrome-flags",
         "--user-data-dir=/tmp/other-chrome",
         "--cli-flags-path", str(flags), "https://example.com"],
    )
    via_audit = _FakeProc(
        9612,
        ["npx", "lighthouse", "https://example.com",
         "--config-path", str(audit)],
    )
    no_pin = _FakeProc(
        9613,
        ["npx", "lighthouse", "https://example.com"],
    )
    bash_parent = _FakeProc(
        9614,
        ["/bin/bash", "-c",
         f"npx lighthouse --cli-flags-path={flags} https://example.com"],
    )
    lease.acquire("human")
    n = interrupt_unregistered_dock_cli(
        processes=[
            leftover, equals, camel, via_kebab, via_port, relative,
            shebang, ephemeral, other_jar, lan_host, other_port,
            argv_overrides, via_audit, no_pin, bash_parent,
        ],
        chromium_pid=9999,
        owner_daemon_pid=9998,
    )
    assert n == 8
    assert leftover.killed == 1
    assert equals.killed == 1
    assert camel.killed == 1
    assert via_kebab.killed == 1
    assert via_port.killed == 1
    assert relative.killed == 1
    assert shebang.killed == 1
    assert ephemeral.killed == 1
    assert other_jar.killed == 0
    assert lan_host.killed == 0
    assert other_port.killed == 0
    assert argv_overrides.killed == 0
    assert via_audit.killed == 0
    assert no_pin.killed == 0
    assert bash_parent.killed == 0


def test_unregistered_agent_browser_tilde_profile_killed_on_takeover():
    """terminal() agent-browser --profile ~/jar leftover hid launch-on-jar.

    Official leftover expands ``~/`` on ``--profile`` /
    ``AGENT_BROWSER_PROFILE`` (leading ``~/`` becomes ``os.homedir()``).
    Finding 111 compared the literal ``~/…`` path, so Take over left
    that writer typing into the jar. Chromium / Playwright /
    lighthouse do not expand ``--user-data-dir``. Another ``~/`` pin,
    ``--cdp`` to another Chrome, and the bash ``-c`` parent stay up.
    """
    from hermes_constants import get_hermes_home
    from tools.bot_desktop import browser as bdb
    from tools.browser_tool_session import interrupt_unregistered_dock_cli

    home = {"HOME": str(get_hermes_home())}
    tilde = "~/bot-desktop/browser-profile"
    leftover = _FakeProc(
        9700,
        ["agent-browser", "--profile", tilde, "fill"],
        home,
    )
    equals = _FakeProc(
        9701,
        ["agent-browser", f"--profile={tilde}", "snapshot"],
        home,
    )
    via_env = _FakeProc(
        9702,
        ["agent-browser", "fill"],
        {**home, "AGENT_BROWSER_PROFILE": tilde},
    )
    shebang = _FakeProc(
        9703,
        ["node", "/home/x/node_modules/agent-browser/dist/cli.js",
         "--profile", tilde, "fill"],
        home,
    )
    other = _FakeProc(
        9704,
        ["agent-browser", "--profile", "~/other-chrome", "fill"],
        home,
    )
    playwright = _FakeProc(
        9705,
        ["npx", "playwright", "codegen", "--user-data-dir", tilde],
        home,
    )
    lighthouse = _FakeProc(
        9706,
        ["npx", "lighthouse", "https://example.com",
         "--chrome-flags", f"--user-data-dir={tilde}"],
        home,
    )
    other_cdp = _FakeProc(
        9707,
        ["agent-browser", "--cdp", "http://127.0.0.1:9222",
         "--profile", tilde, "fill"],
        home,
    )
    bash_parent = _FakeProc(
        9708,
        ["/bin/bash", "-c", f"agent-browser --profile {tilde} fill"],
        home,
    )
    bdb.remember_dock_cdp_port(9333)
    lease.acquire("human")
    n = interrupt_unregistered_dock_cli(
        processes=[
            leftover, equals, via_env, shebang, other, playwright,
            lighthouse, other_cdp, bash_parent,
        ],
        chromium_pid=9999,
        owner_daemon_pid=9998,
    )
    assert n == 4
    assert leftover.killed == 1
    assert equals.killed == 1
    assert via_env.killed == 1
    assert shebang.killed == 1
    assert other.killed == 0
    assert playwright.killed == 0
    assert lighthouse.killed == 0
    assert other_cdp.killed == 0
    assert bash_parent.killed == 0


def test_unregistered_agent_browser_args_user_data_dir_killed_on_takeover():
    """terminal() agent-browser --args --user-data-dir leftover hid launch.

    Official leftover forwards Chromium switches via ``--args`` /
    ``AGENT_BROWSER_ARGS`` (comma or newline separated). Finding 111
    only checked ``--profile``, so Take over left that writer typing
    into the jar. Playwright launch appends user args after its temp
    dir; Chromium last-wins the dock. CLI ``--args`` overrides env.
    Another jar, ``--cdp`` to another Chrome, and the bash ``-c``
    parent stay up.
    """
    from tools.bot_desktop import browser as bdb
    from tools.browser_tool_session import interrupt_unregistered_dock_cli

    profile = bdb.profile_dir()
    leftover = _FakeProc(
        9800,
        ["agent-browser", "--args", f"--user-data-dir={profile}", "fill"],
    )
    equals = _FakeProc(
        9801,
        ["agent-browser", f"--args=--user-data-dir={profile}", "fill"],
    )
    grouped = _FakeProc(
        9802,
        ["npx", "agent-browser", "--args",
         f"--headless,--user-data-dir={profile}", "fill"],
    )
    via_env = _FakeProc(
        9803,
        ["agent-browser", "fill"],
        {"AGENT_BROWSER_ARGS": f"--user-data-dir={profile}"},
    )
    via_hermes_env = _FakeProc(
        9804,
        ["agent-browser", "fill"],
        {"AGENT_BROWSER_CHROME_FLAGS": f"--user-data-dir={profile}"},
    )
    relative = _FakeProc(
        9805,
        ["agent-browser", "--args", "--user-data-dir=browser-profile",
         "fill"],
        cwd=profile.parent,
    )
    last_wins = _FakeProc(
        9806,
        ["agent-browser", "--args",
         f"--user-data-dir=/tmp/other --user-data-dir={profile}", "fill"],
    )
    profile_overridden = _FakeProc(
        9807,
        ["agent-browser", "--profile", "/tmp/other-chrome",
         "--args", f"--user-data-dir={profile}", "fill"],
    )
    shebang = _FakeProc(
        9808,
        ["node", "/home/x/node_modules/agent-browser/dist/cli.js",
         "--args", f"--user-data-dir={profile}", "fill"],
    )
    other_jar = _FakeProc(
        9809,
        ["agent-browser", "--args", "--user-data-dir=/tmp/other-chrome",
         "fill"],
    )
    argv_overrides = _FakeProc(
        9810,
        ["agent-browser", "--args", "--headless", "fill"],
        {"AGENT_BROWSER_ARGS": f"--user-data-dir={profile}"},
    )
    other_cdp = _FakeProc(
        9811,
        ["agent-browser", "--cdp", "http://127.0.0.1:9222",
         "--args", f"--user-data-dir={profile}", "fill"],
    )
    no_pin = _FakeProc(
        9812,
        ["agent-browser", "fill"],
    )
    bash_parent = _FakeProc(
        9813,
        ["/bin/bash", "-c",
         f"agent-browser --args=--user-data-dir={profile} fill"],
    )
    bdb.remember_dock_cdp_port(9333)
    lease.acquire("human")
    n = interrupt_unregistered_dock_cli(
        processes=[
            leftover, equals, grouped, via_env, via_hermes_env,
            relative, last_wins, profile_overridden, shebang,
            other_jar, argv_overrides, other_cdp, no_pin, bash_parent,
        ],
        chromium_pid=9999,
        owner_daemon_pid=9998,
    )
    assert n == 9
    assert leftover.killed == 1
    assert equals.killed == 1
    assert grouped.killed == 1
    assert via_env.killed == 1
    assert via_hermes_env.killed == 1
    assert relative.killed == 1
    assert last_wins.killed == 1
    assert profile_overridden.killed == 1
    assert shebang.killed == 1
    assert other_jar.killed == 0
    assert argv_overrides.killed == 0
    assert other_cdp.killed == 0
    assert no_pin.killed == 0
    assert bash_parent.killed == 0


def test_unregistered_playwright_mcp_env_and_config_killed_on_takeover():
    """terminal() @playwright/mcp env / --config leftover hid launch-on-jar.

    Official leftover pins ``PLAYWRIGHT_MCP_USER_DATA_DIR`` and
    ``--config`` ``browser.userDataDir`` / ``cdpEndpoint``. Finding 111
    only checked argv ``--user-data-dir``, so Take over left those
    writers typing into the jar. Regular Playwright CLI, another jar,
    ``--isolated``, argv CDP to another Chrome, and the bash ``-c``
    parent stay up.
    """
    from tools.bot_desktop import browser as bdb
    from tools.browser_tool_session import interrupt_unregistered_dock_cli

    profile = bdb.profile_dir()
    cfg = profile.parent / "pw-mcp-dock.json"
    cfg.parent.mkdir(parents=True, exist_ok=True)
    cfg.write_text(json.dumps({"browser": {"userDataDir": str(profile)}}))
    cdp_cfg = profile.parent / "pw-mcp-cdp.json"
    cdp_cfg.write_text(json.dumps({
        "browser": {"cdpEndpoint": "http://127.0.0.1:9333"},
    }))
    other_cfg = profile.parent / "pw-mcp-other.json"
    other_cfg.write_text(json.dumps({
        "browser": {"userDataDir": "/tmp/other-chrome"},
    }))
    bdb.remember_dock_cdp_port(9333)
    leftover = _FakeProc(
        9500,
        ["npx", "@playwright/mcp"],
        {"PLAYWRIGHT_MCP_USER_DATA_DIR": str(profile)},
    )
    relative = _FakeProc(
        9501,
        ["npx", "@playwright/mcp"],
        {"PLAYWRIGHT_MCP_USER_DATA_DIR": "browser-profile"},
        cwd=profile.parent,
    )
    via_cfg = _FakeProc(
        9502,
        ["npx", "@playwright/mcp", "--config", str(cfg)],
    )
    via_cfg_env = _FakeProc(
        9503,
        ["npx", "@playwright/mcp"],
        {"PLAYWRIGHT_MCP_CONFIG": str(cfg)},
    )
    via_rel_cfg = _FakeProc(
        9504,
        ["npx", "@playwright/mcp", "--config", "pw-mcp-dock.json"],
        cwd=profile.parent,
    )
    via_cdp = _FakeProc(
        9505,
        ["npx", "@playwright/mcp", "--config", str(cdp_cfg)],
    )
    shebang = _FakeProc(
        9506,
        ["node", "/home/x/node_modules/@playwright/mcp/cli.js"],
        {"PLAYWRIGHT_MCP_USER_DATA_DIR": str(profile)},
    )
    codegen = _FakeProc(
        9507,
        ["npx", "playwright", "codegen"],
        {"PLAYWRIGHT_MCP_USER_DATA_DIR": str(profile)},
    )
    other = _FakeProc(
        9508,
        ["npx", "@playwright/mcp"],
        {"PLAYWRIGHT_MCP_USER_DATA_DIR": "/tmp/other-chrome"},
    )
    isolated = _FakeProc(
        9509,
        ["npx", "@playwright/mcp", "--isolated"],
    )
    other_cfg_proc = _FakeProc(
        9510,
        ["npx", "@playwright/mcp", "--config", str(other_cfg)],
    )
    cdp_wins = _FakeProc(
        9511,
        ["npx", "@playwright/mcp",
         "--cdp-endpoint", "http://127.0.0.1:9222"],
        {"PLAYWRIGHT_MCP_USER_DATA_DIR": str(profile)},
    )
    bash_parent = _FakeProc(
        9512,
        ["/bin/bash", "-c",
         f"PLAYWRIGHT_MCP_USER_DATA_DIR={profile} npx @playwright/mcp"],
    )
    lease.acquire("human")
    n = interrupt_unregistered_dock_cli(
        processes=[
            leftover, relative, via_cfg, via_cfg_env, via_rel_cfg,
            via_cdp, shebang, codegen, other, isolated, other_cfg_proc,
            cdp_wins, bash_parent,
        ],
        chromium_pid=9999,
        owner_daemon_pid=9998,
    )
    assert n == 7
    assert leftover.killed == 1
    assert relative.killed == 1
    assert via_cfg.killed == 1
    assert via_cfg_env.killed == 1
    assert via_rel_cfg.killed == 1
    assert via_cdp.killed == 1
    assert shebang.killed == 1
    assert codegen.killed == 0
    assert other.killed == 0
    assert isolated.killed == 0
    assert other_cfg_proc.killed == 0
    assert cdp_wins.killed == 0
    assert bash_parent.killed == 0


def test_unregistered_chrome_devtools_config_and_chrome_arg_killed_on_takeover():
    """terminal() chrome-devtools leftover --config / --chromeArg hid the jar.

    Official leftover pins ``--chrome-arg=--user-data-dir`` and
    ``--config`` ``userDataDir`` / ``browserUrl`` / ``chromeArg``.
    Finding 123 only checked argv ``--userDataDir``, so Take over left
    those writers typing into the jar. yargs CLI still overrides the
    file per key. Other jar / ``--autoConnect`` / fill / attach-to-other
    and the bash ``-c`` parent stay up.
    """
    from tools.bot_desktop import browser as bdb
    from tools.browser_tool_session import interrupt_unregistered_dock_cli

    profile = bdb.profile_dir()
    cfg = profile.parent / "cd-mcp-dock.json"
    cfg.parent.mkdir(parents=True, exist_ok=True)
    cfg.write_text(json.dumps({"userDataDir": str(profile)}))
    cdp_cfg = profile.parent / "cd-mcp-cdp.json"
    cdp_cfg.write_text(json.dumps({
        "browserUrl": "http://127.0.0.1:9333",
    }))
    arg_cfg = profile.parent / "cd-mcp-arg.json"
    arg_cfg.write_text(json.dumps({
        "chromeArg": [f"--user-data-dir={profile}"],
    }))
    other_cfg = profile.parent / "cd-mcp-other.json"
    other_cfg.write_text(json.dumps({"userDataDir": "/tmp/other-chrome"}))
    bdb.remember_dock_cdp_port(9333)
    leftover = _FakeProc(
        9600,
        ["npx", "chrome-devtools-mcp",
         f"--chrome-arg=--user-data-dir={profile}"],
    )
    camel = _FakeProc(
        9601,
        ["npx", "chrome-devtools-mcp",
         f"--chromeArg=--user-data-dir={profile}"],
    )
    last_wins = _FakeProc(
        9602,
        ["npx", "chrome-devtools-mcp", "--userDataDir", "/tmp/other-chrome",
         f"--chrome-arg=--user-data-dir={profile}"],
    )
    via_cfg = _FakeProc(
        9603,
        ["npx", "chrome-devtools-mcp", "--config", str(cfg)],
    )
    via_rel = _FakeProc(
        9604,
        ["npx", "chrome-devtools-mcp", "--config", "cd-mcp-dock.json"],
        cwd=profile.parent,
    )
    via_cdp = _FakeProc(
        9605,
        ["npx", "chrome-devtools-mcp", "--config", str(cdp_cfg)],
    )
    via_arg = _FakeProc(
        9606,
        ["npx", "chrome-devtools-mcp", "--config", str(arg_cfg)],
    )
    cli_start = _FakeProc(
        9607,
        ["chrome-devtools", "start", "--config", str(cfg)],
    )
    shebang = _FakeProc(
        9608,
        ["node", "/home/x/node_modules/chrome-devtools-mcp/build/src/index.js",
         f"--chrome-arg=--user-data-dir={profile}"],
    )
    headless_only = _FakeProc(
        9609,
        ["npx", "chrome-devtools-mcp", "--chromeArg", "--headless"],
    )
    other_cfg_proc = _FakeProc(
        9610,
        ["npx", "chrome-devtools-mcp", "--config", str(other_cfg)],
    )
    cli_dir_wins = _FakeProc(
        9611,
        ["npx", "chrome-devtools-mcp", "--userDataDir", "/tmp/other-chrome",
         "--config", str(cfg)],
    )
    cli_arg_wins = _FakeProc(
        9612,
        ["npx", "chrome-devtools-mcp", "--chromeArg", "--headless",
         "--config", str(arg_cfg)],
    )
    url_wins = _FakeProc(
        9613,
        ["npx", "chrome-devtools-mcp",
         "--browserUrl", "http://127.0.0.1:9222",
         "--config", str(cfg)],
    )
    auto = _FakeProc(
        9614,
        ["npx", "chrome-devtools-mcp", "--autoConnect"],
    )
    fill = _FakeProc(
        9615,
        ["chrome-devtools", "fill", "1", "uid", "secret"],
    )
    bash_parent = _FakeProc(
        9616,
        ["/bin/bash", "-c",
         f"npx chrome-devtools-mcp --config {cfg}"],
    )
    lease.acquire("human")
    n = interrupt_unregistered_dock_cli(
        processes=[
            leftover, camel, last_wins, via_cfg, via_rel, via_cdp,
            via_arg, cli_start, shebang, headless_only, other_cfg_proc,
            cli_dir_wins, cli_arg_wins, url_wins, auto, fill, bash_parent,
        ],
        chromium_pid=9999,
        owner_daemon_pid=9998,
    )
    assert n == 9
    assert leftover.killed == 1
    assert camel.killed == 1
    assert last_wins.killed == 1
    assert via_cfg.killed == 1
    assert via_rel.killed == 1
    assert via_cdp.killed == 1
    assert via_arg.killed == 1
    assert cli_start.killed == 1
    assert shebang.killed == 1
    assert headless_only.killed == 0
    assert other_cfg_proc.killed == 0
    assert cli_dir_wins.killed == 0
    assert cli_arg_wins.killed == 0
    assert url_wins.killed == 0
    assert auto.killed == 0
    assert fill.killed == 0
    assert bash_parent.killed == 0


def test_stop_reserved_calls_unregistered_interrupt(monkeypatch):
    from tools.browser_tool_session import interrupt_unregistered_dock_cli as real
    from tools.browser_tool_supervisor_lease import stop_reserved_supervisors

    called = []

    def _spy(home=None, **kwargs):
        called.append(home)
        return real(home=home, processes=[], **kwargs)

    monkeypatch.setattr(
        "tools.browser_tool_session.interrupt_unregistered_dock_cli", _spy)
    lease.acquire("human")
    stop_reserved_supervisors()
    assert called, "Take over did not interrupt unregistered dock CLIs"
