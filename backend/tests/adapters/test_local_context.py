"""Fitting the local model's conversation into its context."""

from app.adapters.local_context import KEEP_RECENT, estimate_tokens, fit, prompt_budget
from app.adapters.local_tools import TOOL_DEFINITIONS

BIG = "x" * 9000  # about 3,000 tokens


def turn(task: str, outputs: int = 2, answer: str = "") -> list[dict]:
    """A user message, then tool calls with big outputs, then an answer."""
    messages = [{"role": "user", "content": task}]
    for n in range(outputs):
        messages.append({"role": "assistant", "content": "",
                         "tool_calls": [{"function": {"name": "write_file", "arguments": {"path": f"f{n}.txt", "content": BIG}}}]})
        messages.append({"role": "tool", "content": BIG, "tool_name": "write_file"})
    messages.append({"role": "assistant", "content": f"Done with {task}.{answer}"})
    return messages


SYSTEM = {"role": "system", "content": "You are a test model."}


def size(messages: list[dict]) -> int:
    return sum(estimate_tokens(m) for m in messages)


def test_a_small_conversation_is_sent_as_is() -> None:
    messages = [SYSTEM, *turn("task", 1)]
    fitted = fit(messages, 100_000)
    assert fitted.messages == messages and not fitted.changed


def test_old_tool_outputs_are_shortened_first() -> None:
    messages = [SYSTEM, *turn("task one"), *turn("task two"), {"role": "user", "content": "next"}]
    budget = size(messages) - 5000
    fitted = fit(messages, budget)
    assert fitted.tokens <= budget
    assert fitted.shortened > 0 and fitted.dropped == 0
    # The oldest output went first; the latest messages are untouched.
    assert "left out to save space" in fitted.messages[3]["content"]
    assert fitted.messages[-KEEP_RECENT:] == messages[-KEEP_RECENT:]
    # Old file contents in calls are shortened too, and the input isn't changed.
    assert any("characters, left out" in str(m.get("tool_calls")) for m in fitted.messages)
    assert messages[3]["content"] == BIG


def test_earlier_turns_are_dropped_keeping_the_task_and_the_current_turn() -> None:
    current = [{"role": "user", "content": "latest request"}, {"role": "assistant", "content": "working"}]
    long = " " + "z" * 6000  # answers too long to keep
    messages = [SYSTEM, *turn("the original task", 4), *turn("second", answer=long), *turn("third", answer=long), *current]
    fitted = fit(messages, 3000)
    assert fitted.tokens <= 3000
    assert fitted.dropped > 0
    contents = [m.get("content") for m in fitted.messages]
    assert "the original task" in contents
    assert contents[-2:] == ["latest request", "working"]
    assert not any(str(c).startswith("Done with second.") for c in contents)
    assert f"{fitted.dropped} earlier messages" in fitted.messages[0]["content"]
    # Every tool result that is left still follows its call: whole turns go, never half of one.
    for i, m in enumerate(fitted.messages):
        if m["role"] == "tool":
            assert fitted.messages[i - 1]["role"] in ("assistant", "tool")


def test_earlier_tool_steps_go_before_their_answers() -> None:
    current = [{"role": "user", "content": "latest request"}]
    messages = [SYSTEM, *turn("the original task", 3), *turn("second", 3), *current]
    budget = size([SYSTEM, *turn("the original task", 3)]) // 2
    fitted = fit(messages, budget)
    contents = [m.get("content") for m in fitted.messages]
    # The requests and final answers stay as a record of what was done; the tool steps went.
    assert contents[1:] == ["the original task", "Done with the original task.", "second", "Done with second.",
                            "latest request"]
    assert fitted.dropped == 12


def test_a_huge_current_message_keeps_its_start_and_end() -> None:
    brief = "START " + "y" * 60_000 + " END"
    fitted = fit([SYSTEM, {"role": "user", "content": brief}], 5000)
    assert fitted.cut_prompt
    content = fitted.messages[1]["content"]
    assert content.startswith("START") and content.endswith("END")
    assert "left out here to fit" in content
    assert fitted.tokens <= 5000


def test_prompt_budget_leaves_room_for_the_reply_and_tools() -> None:
    budget = prompt_budget(32768, TOOL_DEFINITIONS)
    assert 32768 - 6554 - 1500 < budget < 32768 - 6554
    assert prompt_budget(4096, None) == 1024  # never below a usable floor
