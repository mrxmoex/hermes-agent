"""The documented CLI stop is also recovery for a disconnected viewer's lease."""

import argparse
import json

from hermes_cli.subcommands.computer_use_screen import build_screen_parser
import pytest


@pytest.mark.linux_only
def test_screen_stop_hands_back_even_when_the_desktop_has_already_exited():
    from tools.bot_desktop import lease

    parser = argparse.ArgumentParser()
    build_screen_parser(parser.add_subparsers(), lambda sub, help_text: sub.add_argument('--json', action='store_true'))
    lease.acquire('disconnected-viewer')
    args = parser.parse_args(['screen', 'stop'])
    assert args.screen_func(args) == 0
    assert lease.get().holder == lease.AGENT
    assert lease.wait_for_release(timeout=0)


def test_screen_status_json_never_discloses_viewer_id(capsys):
    """CLI ``status --json`` is an outbound snapshot, same contract as display.status."""
    from tools.bot_desktop import lease

    parser = argparse.ArgumentParser()
    build_screen_parser(parser.add_subparsers(), lambda sub, help_text: sub.add_argument('--json', action='store_true'))
    lease._reset_for_tests()
    try:
        lease.acquire("SECRET-VIEWER-ID")
        args = parser.parse_args(["screen", "status", "--json"])
        args.screen_func(args)
        out = capsys.readouterr().out
        assert "SECRET-VIEWER-ID" not in out
        payload = json.loads(out)
        assert payload["lease"]["viewer_id"] is None
        assert payload["lease"]["viewer_hash"]
        assert payload["lease"]["holder"] == "human"
    finally:
        lease._reset_for_tests()
