"""``delegate_task`` runs a nested orchestrator's whole batch inside one tool call by design, so it must not
sit under the generic sequential-call deadline: with it, every batch longer than the deadline "timed out"
while its children kept running as orphans and the orchestrator polled transcripts for hours."""
from agent import tool_executor as te


def test_delegate_task_is_exempt_from_the_sequential_deadline():
    assert "delegate_task" in te._SEQUENTIAL_DEADLINE_EXEMPT_TOOLS


def test_exemption_is_narrow():
    assert "terminal" not in te._SEQUENTIAL_DEADLINE_EXEMPT_TOOLS
    assert "execute_code" not in te._SEQUENTIAL_DEADLINE_EXEMPT_TOOLS
    assert "computer_use" not in te._SEQUENTIAL_DEADLINE_EXEMPT_TOOLS


def test_wait_for_human_is_exempt_other_computer_use_actions_are_not():
    """Handoff owns a 600 s lease wait; a stuck click must still hit the generic deadline."""
    assert te._is_sequential_deadline_exempt("computer_use", {"action": "wait_for_human"})
    assert not te._is_sequential_deadline_exempt("computer_use", {"action": "click"})
    assert not te._is_sequential_deadline_exempt("computer_use", {"action": "capture"})
    assert not te._is_sequential_deadline_exempt("computer_use", {})
