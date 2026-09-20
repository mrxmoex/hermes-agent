"""The display bridge admits only a display ticket minted for THIS profile's socket: a gateway ticket,
an expired ticket, or one for another provider is refused before any socket is dialled.

A refused gateway ticket must stay in the store — popping it here logged the
user out of a pending ``/api/ws`` upgrade. Malformed bot-desktop tickets are
still consumed (they are not a login)."""

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
    # A foreign ticket must not be popped: /api/ws still has to redeem it.
    assert ws_tickets.consume_ticket(gateway_ticket)["user_id"] == "u"

    unpinned = ws_tickets.mint_ticket(user_id="display:v", provider="bot-desktop")
    assert display._consume_display_ticket(_Ws(display_ticket=unpinned)) is None
    with pytest.raises(ws_tickets.TicketInvalid):
        ws_tickets.consume_ticket(unpinned)

    no_viewer = ws_tickets.mint_ticket(user_id="display:v", provider="bot-desktop",
                                       extra={"hermes_home": "/srv/hermes/bot-a"})
    assert display._consume_display_ticket(_Ws(display_ticket=no_viewer)) is None
    with pytest.raises(ws_tickets.TicketInvalid):
        ws_tickets.consume_ticket(no_viewer)

    blank_viewer = ws_tickets.mint_ticket(user_id="display:v", provider="bot-desktop",
                                          extra={"hermes_home": "/srv/hermes/bot-a", "viewer_id": "  "})
    assert display._consume_display_ticket(_Ws(display_ticket=blank_viewer)) is None
    with pytest.raises(ws_tickets.TicketInvalid):
        ws_tickets.consume_ticket(blank_viewer)

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
