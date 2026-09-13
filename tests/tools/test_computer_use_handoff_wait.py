"""wait_for_human answers an unanswered handoff early instead of blocking the whole timeout, and still
waits the full timeout once a human actually holds the screen."""

from __future__ import annotations

import json
import threading
import time

import pytest

from tools.bot_desktop import lease
from tools.computer_use.handoff import handle_handoff


@pytest.fixture(autouse=True)
def _fresh_lease(monkeypatch):
    from tools.bot_desktop import runtime
    monkeypatch.setattr(runtime, "is_supported_host", lambda: True)
    lease._reset_for_tests()
    yield
    lease._reset_for_tests()


def test_handoff_refuses_immediately_on_unsupported_hosts(monkeypatch):
    from tools.bot_desktop import runtime
    monkeypatch.setattr(runtime, "is_supported_host", lambda: False)
    t0 = time.monotonic()
    res = json.loads(handle_handoff("wait_for_human", {"seconds": 30, "grace": 20}))
    assert res["code"] == "unsupported_host" and res["ok"] is False
    assert time.monotonic() - t0 < 2, "an unsupported host must not block on the grace window"


def test_wait_for_human_returns_no_takeover_when_nobody_answers_but_waits_out_a_real_takeover():
    handle_handoff("request_handoff", {"reason": "log in"})
    t0 = time.monotonic()
    res = json.loads(handle_handoff("wait_for_human", {"seconds": 30, "grace": 0.2}))
    assert res["code"] == "no_takeover" and res["state"]["pending_handoff"] == "log in"
    assert time.monotonic() - t0 < 10, "an unanswered request must not run the full timeout"

    # A human takes over inside the grace window and hands back later: the wait outlives the grace.
    def _take_then_release():
        time.sleep(0.1)
        lease.acquire("viewer-1")
        time.sleep(0.6)
        lease.release("viewer-1")
    threading.Thread(target=_take_then_release, daemon=True).start()
    res = json.loads(handle_handoff("wait_for_human", {"seconds": 30, "grace": 0.3}))
    assert res["ok"] and res["state"]["holder"] == lease.AGENT
