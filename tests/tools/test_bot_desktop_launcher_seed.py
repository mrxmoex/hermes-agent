"""Bot Desktop launcher seeds: the dock only points at programs that exist, the look is applied."""

from __future__ import annotations

import os
import shutil
import subprocess
import xml.etree.ElementTree as ET
from pathlib import Path

import pytest

LAUNCHER = Path(__file__).resolve().parents[2] / "tools" / "bot_desktop" / "launcher.sh"
pytestmark = pytest.mark.linux_only


def _seed(tmp_path: Path, fake_bins: list[str], browser_exec: str = "") -> Path:
    bindir = tmp_path / "bin"
    bindir.mkdir()
    for name in fake_bins:
        exe = bindir / name
        exe.write_text("#!/bin/sh\n", encoding="utf-8")
        exe.chmod(0o755)
    # The script's own tooling (mkdir, sed, cat, awk...) symlinked in, so PATH need not contain the
    # host's /usr/bin where a real chrome/thunar would leak into the dock under test.
    for tool in ("mkdir", "sed", "cat", "printf", "dirname", "bash", "sh", "rm", "ln", "touch", "chmod", "xauth", "od", "tr", "awk"):
        real = shutil.which(tool)
        if real and not (bindir / tool).exists():
            (bindir / tool).symlink_to(real)
    cfg = tmp_path / "xdg"
    env = {
        "PATH": str(bindir),
        "HOME": str(tmp_path),
        "HERMES_BD_PROFILE": "t", "HERMES_BD_DISPLAY_NUM": "99",
        "HERMES_BD_SOCKET": str(tmp_path / "rfb.sock"), "HERMES_BD_XAUTH": str(tmp_path / "Xauthority"),
        "HERMES_BD_ENV_FILE": str(tmp_path / "env"), "HERMES_BD_CONFIG_HOME": str(cfg),
        "HERMES_BD_SEED_ONLY": "1",
        **({"HERMES_BD_BROWSER_EXEC": browser_exec} if browser_exec else {}),
    }
    subprocess.run(["bash", str(LAUNCHER)], env=env, check=True, stdin=subprocess.DEVNULL, capture_output=True, timeout=30)
    return cfg


def test_dock_lists_only_programs_present_on_path(tmp_path):
    chrome = tmp_path / "bin" / "chrome"  # the browser is the one runtime.py resolved, never a PATH scan
    cfg = _seed(tmp_path, ["xfce4-terminal", "chrome", "firefox"], browser_exec=f"{chrome} --user-data-dir={tmp_path}/bp")
    panel = ET.parse(cfg / "xfce4/xfconf/xfce-perchannel-xml/xfce4-panel.xml")  # well-formed or this raises
    launcher_ids = [str(p.get("name")) for p in panel.iter("property") if p.get("value") == "launcher"]
    execs = sorted(
        line.split("=", 1)[1]
        for pid in launcher_ids
        for line in (cfg / "xfce4/panel" / pid.replace("plugin-", "launcher-") / "hermes.desktop").read_text(encoding="utf-8").splitlines()
        if line.startswith("Exec=")
    )
    assert execs == [f"{chrome} --user-data-dir={tmp_path}/bp", "xfce4-terminal"]


def test_xauth_cookie_is_passed_on_stdin_not_argv(tmp_path):
    """``xauth add … $(od …)`` puts the MIT-MAGIC-COOKIE on argv for the life of that
    line; any local UID can read it from ps. The launcher must feed the cookie via stdin."""
    bindir = tmp_path / "bin"
    bindir.mkdir()
    argv_log = tmp_path / "xauth.argv"
    (bindir / "xauth").write_text(
        "#!/bin/sh\nprintf '%s\\n' \"$0 $*\" >> \"$HERMES_BD_XAUTH_ARGV\"\n"
        "if [ \"$1\" = source ] || [ \"$3\" = source ]; then cat >/dev/null; fi\n",
        encoding="utf-8",
    )
    (bindir / "xauth").chmod(0o755)
    env_extra = {"HERMES_BD_XAUTH_ARGV": str(argv_log)}
    # _seed overwrites PATH but we need the argv log path in the child env.
    bindir_names = ["xfce4-terminal"]
    for name in bindir_names:
        exe = bindir / name
        if not exe.exists():
            exe.write_text("#!/bin/sh\n", encoding="utf-8")
            exe.chmod(0o755)
    for tool in ("mkdir", "sed", "cat", "printf", "dirname", "bash", "sh", "rm", "ln", "touch", "chmod", "od", "tr", "awk"):
        real = shutil.which(tool)
        if real and not (bindir / tool).exists():
            (bindir / tool).symlink_to(real)
    cfg = tmp_path / "xdg"
    env = {
        "PATH": str(bindir),
        "HOME": str(tmp_path),
        "HERMES_BD_PROFILE": "t", "HERMES_BD_DISPLAY_NUM": "99",
        "HERMES_BD_SOCKET": str(tmp_path / "rfb.sock"), "HERMES_BD_XAUTH": str(tmp_path / "Xauthority"),
        "HERMES_BD_ENV_FILE": str(tmp_path / "env"), "HERMES_BD_CONFIG_HOME": str(cfg),
        "HERMES_BD_SEED_ONLY": "1",
        **env_extra,
    }
    subprocess.run(["bash", str(LAUNCHER)], env=env, check=True, stdin=subprocess.DEVNULL,
                   capture_output=True, timeout=30)
    logged = argv_log.read_text(encoding="utf-8")
    assert "source" in logged
    assert "MIT-MAGIC-COOKIE" not in logged
    hex_runs = [tok for tok in logged.replace("\n", " ").split() if len(tok) == 32 and all(c in "0123456789abcdef" for c in tok)]
    assert hex_runs == [], f"cookie leaked onto xauth argv: {logged!r}"


def test_look_is_seeded_with_wallpaper_and_theme(tmp_path):
    cfg = _seed(tmp_path, ["xfce4-terminal"])
    desktop = (cfg / "xfce4/xfconf/xfce-perchannel-xml/xfce4-desktop.xml").read_text(encoding="utf-8")
    xsettings = (cfg / "xfce4/xfconf/xfce-perchannel-xml/xsettings.xml").read_text(encoding="utf-8")
    assert str(LAUNCHER.with_name("wallpaper.png")) in desktop
    assert "PLACEHOLDER" not in desktop + xsettings
    assert os.path.isfile(LAUNCHER.with_name("wallpaper.png"))
