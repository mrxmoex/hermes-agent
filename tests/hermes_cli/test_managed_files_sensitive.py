"""Unit tests for the dashboard managed-files / workspace-FS sensitive-path guard.

These call the helpers directly so they run without starlette's TestClient
(httpx). The HTTP 403 routes in ``test_web_server_files`` and
``test_web_server_fs`` cover the same helper through the API when those
deps are present.
"""

from pathlib import Path

import pytest
from fastapi import HTTPException

from hermes_cli.web_routers.files import _is_sensitive_path, _raise_if_sensitive


def test_bot_desktop_tree_is_sensitive():
    """bot-desktop/ is the screen cookie jar + X cookie + lease.

    Any path component named bot-desktop is denied so a managed root at
    $HOME or HERMES_HOME cannot download Cookies / lease.json / Xauthority
    the way export/backup used to ship them.
    """
    assert _is_sensitive_path(Path("/home/u/.hermes/bot-desktop"))
    assert _is_sensitive_path(Path("/home/u/.hermes/bot-desktop/lease.json"))
    assert _is_sensitive_path(
        Path("/home/u/.hermes/bot-desktop/browser-profile/Default/Cookies")
    )
    assert _is_sensitive_path(
        Path("/home/u/.hermes/profiles/coder/bot-desktop/Xauthority")
    )
    assert _is_sensitive_path(Path("/tmp/data/Bot-Desktop/lease.json"))


def test_bot_desktop_does_not_block_neighbouring_files():
    assert not _is_sensitive_path(Path("/home/u/.hermes/config.yaml.example"))
    assert not _is_sensitive_path(Path("/home/u/.hermes/notes.md"))
    assert not _is_sensitive_path(Path("/home/u/.hermes/cache/images/shot.png"))


def test_raise_if_sensitive_refuses_lease_forge_and_cookie_jar():
    """Write / read-text / upload share this 403. Forging lease.json through
    the dashboard would return control without acquire/release; writing
    dock-cdp-port fail-opens leftover CDP.
    """
    for path in (
        Path("/home/u/.hermes/bot-desktop/lease.json"),
        Path("/home/u/.hermes/bot-desktop/dock-cdp-port"),
        Path("/home/u/.hermes/bot-desktop/browser-profile/Default/Cookies"),
        Path("/home/u/.hermes/.env"),
    ):
        with pytest.raises(HTTPException) as exc:
            _raise_if_sensitive(path)
        assert exc.value.status_code == 403
        assert "sensitive" in str(exc.value.detail).lower()


def test_raise_if_sensitive_allows_ordinary_workspace_files():
    _raise_if_sensitive(Path("/home/u/.hermes/notes.md"))
    _raise_if_sensitive(Path("/home/u/src/app.py"))
