"""Bot Desktop teardown on ``hermes uninstall``.

The launcher is not a gateway. A full wipe that only stops the gateway
leaves Xvnc + Xfce + the dock Chromium running against a deleted
HERMES_HOME — the same hole as deleting a profile without
``_stop_bot_desktop``. ``--yes --full`` never calls ``_uninstall_profile``
for named profiles, but their homes still vanish under the default
``rmtree``.
"""

from __future__ import annotations

import subprocess
from pathlib import Path
from types import SimpleNamespace

import pytest

from hermes_cli import uninstall


def _plant_launcher(home: Path) -> subprocess.Popen:
    from tools.bot_desktop import runtime

    desktop = home / "bot-desktop"
    desktop.mkdir(parents=True, exist_ok=True)
    proc = subprocess.Popen(["sleep", "60"], start_new_session=True)
    born = runtime._create_time(proc.pid)
    (desktop / "launcher.pid").write_text(f"{proc.pid} {born}", encoding="utf-8")
    (desktop / "env").write_text("DISPLAY=:42\n", encoding="utf-8")
    return proc


def _stub_uninstall_side_effects(monkeypatch) -> None:
    """Keep a full-wipe test off the real user PATH, wrappers, and gateway."""
    monkeypatch.setattr(uninstall, "uninstall_gateway_service", lambda: True)
    monkeypatch.setattr(uninstall, "remove_path_from_shell_configs", lambda: [])
    monkeypatch.setattr(uninstall, "remove_wrapper_script", lambda: [])
    monkeypatch.setattr(uninstall, "remove_node_symlinks", lambda home: [])
    monkeypatch.setattr(uninstall, "_is_windows", lambda: False)


def _reap(proc: subprocess.Popen) -> None:
    if proc.poll() is None:
        proc.kill()
        proc.wait(timeout=5)


def test_full_wipe_homes_include_nested_named_not_a_stranger(tmp_path):
    """A named profile outside the wiped root is not this rmtree's problem."""
    default = tmp_path / ".hermes"
    nested = default / "profiles" / "coder"
    stranger = tmp_path / "other-home"
    nested.mkdir(parents=True)
    stranger.mkdir()
    homes = uninstall._homes_wiped_by_full_uninstall(
        default,
        [
            SimpleNamespace(path=nested),
            SimpleNamespace(path=stranger),
            SimpleNamespace(path=None),
        ],
    )
    assert homes[0] == default
    assert nested in homes
    assert stranger not in homes


def test_keep_data_dry_run_does_not_claim_to_stop_screens(tmp_path, monkeypatch, capsys):
    project_root = tmp_path / "hermes-agent"
    hermes_home = tmp_path / ".hermes"
    project_root.mkdir()
    hermes_home.mkdir()
    monkeypatch.setattr(uninstall, "get_project_root", lambda: project_root)
    monkeypatch.setattr(uninstall, "get_hermes_home", lambda: hermes_home)
    monkeypatch.setattr(uninstall, "_is_default_hermes_home", lambda home: False)
    uninstall._print_uninstall_dry_run(
        project_root=project_root, hermes_home=hermes_home, full_uninstall=False,
    )
    out = capsys.readouterr().out
    assert "Bot Desktop" not in out
    assert "Keep Hermes config/data" in out


def test_full_wipe_dry_run_mentions_bot_desktop(tmp_path, capsys):
    project_root = tmp_path / "hermes-agent"
    hermes_home = tmp_path / ".hermes"
    uninstall._print_uninstall_dry_run(
        project_root=project_root, hermes_home=hermes_home, full_uninstall=True,
    )
    assert "Bot Desktop screens whose homes this wipe deletes" in capsys.readouterr().out


@pytest.mark.linux_only
def test_uninstall_profile_stops_a_live_bot_desktop(tmp_path, monkeypatch):
    """Named-profile uninstall rmtree's the home; the launcher must die first."""
    monkeypatch.setattr(
        uninstall.subprocess, "run",
        lambda *a, **k: subprocess.CompletedProcess([], 0),
    )
    profile_dir = tmp_path / "profiles" / "screenbot"
    proc = _plant_launcher(profile_dir)
    try:
        uninstall._uninstall_profile(
            SimpleNamespace(name="screenbot", path=profile_dir, alias_path=None),
        )
        assert proc.wait(timeout=10) != 0
        assert not profile_dir.exists()
    finally:
        _reap(proc)


def test_bot_desktop_stop_failure_does_not_abort_uninstall_profile(tmp_path, monkeypatch):
    """A wedged launcher must not block wiping the profile home."""
    monkeypatch.setattr(
        uninstall.subprocess, "run",
        lambda *a, **k: subprocess.CompletedProcess([], 0),
    )
    monkeypatch.setattr("tools.bot_desktop.runtime.is_supported_host", lambda: True)

    def _boom():
        raise RuntimeError("launcher wedged")

    monkeypatch.setattr("tools.bot_desktop.runtime.stop", _boom)
    profile_dir = tmp_path / "profiles" / "screenbot"
    profile_dir.mkdir(parents=True)
    uninstall._uninstall_profile(
        SimpleNamespace(name="screenbot", path=profile_dir, alias_path=None),
    )
    assert not profile_dir.exists()


@pytest.mark.linux_only
def test_full_uninstall_stops_default_and_nested_named_desktops(tmp_path, monkeypatch):
    """``--yes --full`` never calls ``_uninstall_profile`` (remove_profiles
    is false) but still ``rmtree``s ``<default>/profiles/<name>``. Both
    the default screen and the nested named one must go down.
    """
    _stub_uninstall_side_effects(monkeypatch)
    project_root = tmp_path / "hermes-agent"
    hermes_home = tmp_path / ".hermes"
    project_root.mkdir()
    hermes_home.mkdir()
    named = hermes_home / "profiles" / "coder"
    default_proc = _plant_launcher(hermes_home)
    named_proc = _plant_launcher(named)
    try:
        uninstall._perform_uninstall(
            project_root=project_root,
            hermes_home=hermes_home,
            full_uninstall=True,
            remove_profiles=False,
            named_profiles=[SimpleNamespace(name="coder", path=named, alias_path=None)],
        )
        assert default_proc.wait(timeout=10) != 0
        assert named_proc.wait(timeout=10) != 0
        assert not hermes_home.exists()
        assert not project_root.exists()
    finally:
        _reap(default_proc)
        _reap(named_proc)


@pytest.mark.linux_only
def test_keep_data_uninstall_leaves_the_desktop_running(tmp_path, monkeypatch):
    """Keep-data removes the checkout and leaves ``HERMES_HOME``. The
    screen is still that home's computer; do not tear it down.
    """
    _stub_uninstall_side_effects(monkeypatch)
    project_root = tmp_path / "hermes-agent"
    hermes_home = tmp_path / ".hermes"
    project_root.mkdir()
    hermes_home.mkdir()
    proc = _plant_launcher(hermes_home)
    try:
        uninstall._perform_uninstall(
            project_root=project_root,
            hermes_home=hermes_home,
            full_uninstall=False,
            remove_profiles=False,
            named_profiles=[],
        )
        assert proc.poll() is None
        assert hermes_home.is_dir()
        assert not project_root.exists()
    finally:
        _reap(proc)


def test_full_uninstall_stop_failure_does_not_abort_wipe(tmp_path, monkeypatch):
    """A wedged screen must not block ``--yes --full``."""
    _stub_uninstall_side_effects(monkeypatch)
    monkeypatch.setattr("tools.bot_desktop.runtime.is_supported_host", lambda: True)
    monkeypatch.setattr(
        "tools.bot_desktop.runtime.stop",
        lambda: (_ for _ in ()).throw(RuntimeError("launcher wedged")),
    )
    project_root = tmp_path / "hermes-agent"
    hermes_home = tmp_path / ".hermes"
    project_root.mkdir()
    hermes_home.mkdir()
    (hermes_home / "config.yaml").write_text("model: {}\n")
    uninstall._perform_uninstall(
        project_root=project_root,
        hermes_home=hermes_home,
        full_uninstall=True,
        remove_profiles=False,
        named_profiles=[],
    )
    assert not hermes_home.exists()
