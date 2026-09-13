"""A Bot Desktop display ticket admits one RFB bridge on ``/api/display/ws`` — it must never pass as a
full login on ``/api/ws`` (the reverse of ``test_display_ws_ticket``, which refuses gateway tickets there)."""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from hermes_cli import web_server
import hermes_cli.web_server_chat as _web_server_chat
from hermes_cli.dashboard_auth.ws_tickets import (
    TicketInvalid,
    _reset_for_tests,
    consume_ticket,
    mint_ticket,
)


@pytest.fixture
def gated_state():
    _reset_for_tests()
    prev = getattr(web_server.app.state, "auth_required", None)
    web_server.app.state.auth_required = True
    yield
    web_server.app.state.auth_required = prev
    _reset_for_tests()


def _ws(ticket: str):
    return SimpleNamespace(
        query_params={"ticket": ticket}, headers={},
        client=SimpleNamespace(host="203.0.113.9"), url=SimpleNamespace(path="/api/ws"))


def test_display_ticket_is_refused_as_a_gateway_login(gated_state):
    ticket = mint_ticket(user_id="display:v", provider="bot-desktop",
                         extra={"hermes_home": "/srv/hermes/bot-a", "viewer_id": "v"})
    ws = _ws(ticket)
    reason, _credential = _web_server_chat._ws_auth_reason(ws)
    assert reason == "ticket_invalid"
    assert not hasattr(ws, "_hermes_auth_identity")
    # Login-door policy: a leaked display ticket that was offered as a login
    # is burned so it cannot still open the RFB bridge. The display door is
    # the inverse — it must not burn a gateway ticket (see test_display_ws_ticket).
    with pytest.raises(TicketInvalid):
        consume_ticket(ticket)
