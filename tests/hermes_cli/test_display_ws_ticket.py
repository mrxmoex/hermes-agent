"""The display bridge admits only a display ticket minted for THIS profile's socket: a gateway ticket,
an expired ticket, or one for another provider is refused before any socket is dialled."""

from __future__ import annotations

import pytest

from hermes_cli.dashboard_auth import ws_tickets
from hermes_cli.web_routers import display


class _Ws:
    def __init__(self, **params):
        self.query_params = params


def test_display_ticket_must_be_a_bot_desktop_ticket_pinned_to_a_profile_home(monkeypatch):
    ws_tickets._reset_for_tests()
    gateway_ticket = ws_tickets.mint_ticket(user_id="u", provider="google")
    assert display._consume_display_ticket(_Ws(display_ticket=gateway_ticket)) is None

    unpinned = ws_tickets.mint_ticket(user_id="display:v", provider="bot-desktop")
    assert display._consume_display_ticket(_Ws(display_ticket=unpinned)) is None

    good = ws_tickets.mint_ticket(user_id="display:v", provider="bot-desktop",
                                  extra={"hermes_home": "/srv/hermes/bot-a", "viewer_id": "v"})
    info = display._consume_display_ticket(_Ws(display_ticket=good))
    assert info and info["hermes_home"] == "/srv/hermes/bot-a" and info["viewer_id"] == "v"
    assert display._consume_display_ticket(_Ws(display_ticket=good)) is None, "single use"


def test_revoke_unused_tickets_drops_only_that_viewer_on_that_home():
    ws_tickets._reset_for_tests()
    keep_other_viewer = ws_tickets.mint_ticket(
        user_id="display:other", provider="bot-desktop",
        extra={"hermes_home": "/srv/hermes/bot-a", "viewer_id": "other"})
    keep_other_home = ws_tickets.mint_ticket(
        user_id="display:v", provider="bot-desktop",
        extra={"hermes_home": "/srv/hermes/bot-b", "viewer_id": "v"})
    stale = ws_tickets.mint_ticket(
        user_id="display:v", provider="bot-desktop",
        extra={"hermes_home": "/srv/hermes/bot-a", "viewer_id": "v"})
    assert ws_tickets.revoke_unused_tickets(viewer_id="v", hermes_home="/srv/hermes/bot-a") == 1
    with pytest.raises(ws_tickets.TicketInvalid):
        ws_tickets.consume_ticket(stale)
    assert ws_tickets.consume_ticket(keep_other_viewer)["viewer_id"] == "other"
    assert ws_tickets.consume_ticket(keep_other_home)["hermes_home"] == "/srv/hermes/bot-b"
