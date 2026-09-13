"""computer_use schema ships Bot Screen handoff only on hosts that can run a per-profile desktop."""

from __future__ import annotations

from tools.computer_use.schema import COMPUTER_USE_SCHEMA, schema_for_host


def test_schema_for_host_strips_handoff_on_unsupported_hosts_and_leaves_the_catalog_intact():
    full = schema_for_host(bot_desktop_supported=True)
    assert full is COMPUTER_USE_SCHEMA
    assert {"request_handoff", "wait_for_human"} <= set(full["parameters"]["properties"]["action"]["enum"])
    assert "take over this screen" in full["parameters"]["properties"]["action"]["description"]

    live = schema_for_host(bot_desktop_supported=False)
    actions = live["parameters"]["properties"]["action"]["enum"]
    assert "request_handoff" not in actions and "wait_for_human" not in actions
    assert "capture" in actions and "click" in actions
    assert "take over this screen" not in live["parameters"]["properties"]["action"]["description"]
    # The catalog itself is not mutated — Linux conversations keep the full schema.
    assert {"request_handoff", "wait_for_human"} <= set(
        COMPUTER_USE_SCHEMA["parameters"]["properties"]["action"]["enum"]
    )


def test_dynamic_rewriter_follows_the_host_gate(monkeypatch):
    import model_tools
    from tools.bot_desktop import runtime

    td = model_tools._fn_def(dict(COMPUTER_USE_SCHEMA))
    monkeypatch.setattr(runtime, "is_supported_host", lambda: False)
    rewritten = model_tools._apply_dynamic_schemas([td])
    assert len(rewritten) == 1
    actions = rewritten[0]["function"]["parameters"]["properties"]["action"]["enum"]
    assert "request_handoff" not in actions
    monkeypatch.setattr(runtime, "is_supported_host", lambda: True)
    kept = model_tools._apply_dynamic_schemas([td])
    assert "request_handoff" in kept[0]["function"]["parameters"]["properties"]["action"]["enum"]
