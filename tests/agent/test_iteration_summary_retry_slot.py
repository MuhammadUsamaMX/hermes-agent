"""The iteration-limit summary retry slot (#123436).

``handle_max_iterations`` deliberately spends one retry pass on a bad summary response:
commit 3dc2403c45 ("single per-wire summary attempt with one retry pass") rebuilt the
per-transport attempts around a ``for retry_count in (0, 1)`` loop, and an unusable
response reaches that loop as *empty text* (the router-timeout shim, #68396).

A provider call that **raises** never became empty text: it escaped the loop on the first
attempt and landed in the outer ``except Exception``, so the retry pass was consumed by
nothing and the turn fell back to failure copy even when a perfectly good summary was one
attempt away. These tests pin the retry slot for the raising transport.
"""
from types import SimpleNamespace
from unittest.mock import patch

import pytest

SUMMARY = "Recovered summary."


def _completion(content, completion_tokens=3):
    return SimpleNamespace(
        choices=[SimpleNamespace(message=SimpleNamespace(content=content, tool_calls=None),
                                 finish_reason="stop")],
        usage=SimpleNamespace(completion_tokens=completion_tokens), model="m")


def _make_agent():
    from run_agent import AIAgent

    agent = AIAgent(api_key="test-key", base_url="https://openrouter.ai/api/v1", model="test/model",
                    quiet_mode=True, skip_context_files=True, skip_memory=True, enabled_toolsets=[])
    agent.api_mode = "chat_completions"
    agent._cached_system_prompt = "You are helpful."
    return agent


def _summarize(agent):
    return agent._handle_max_iterations([{"role": "user", "content": "work"}], 60)


@patch("agent.process_bootstrap.OpenAI")
def test_raised_summary_attempt_spends_the_retry_slot(_mock_openai):
    """First summary call raises, second returns a real summary: the summary is used.

    This is the regression — the raise used to leave the loop on attempt 0, so the turn
    returned failure copy with the good summary from attempt 1 never requested.
    """
    agent = _make_agent()
    agent.client.chat.completions.create.side_effect = [
        RuntimeError("provider 500"),
        _completion(SUMMARY),
    ]

    assert _summarize(agent) == SUMMARY
    assert agent.client.chat.completions.create.call_count == 2


@patch("agent.process_bootstrap.OpenAI")
def test_raised_summary_attempts_keep_the_failure_copy(_mock_openai):
    """Both attempts raise: the turn still gets user-facing copy, never the raw exception."""
    from agent.turn_failure_copy import site_copy

    agent = _make_agent()
    agent.client.chat.completions.create.side_effect = [
        RuntimeError("provider 500"),
        RuntimeError("provider 500 again"),
    ]

    result = _summarize(agent)

    assert result == site_copy("max_iterations_no_summary", limit=agent.max_iterations)
    assert "provider 500" not in result
    assert agent.client.chat.completions.create.call_count == 2


@patch("agent.process_bootstrap.OpenAI")
def test_raised_then_empty_summary_keeps_the_failure_copy(_mock_openai):
    """A raise followed by an empty summary stays on the pre-existing fallback path.

    Only the raise-on-first-attempt case changes behaviour; a raise that still leaves the
    turn with no summary keeps the copy it always had.
    """
    from agent.turn_failure_copy import site_copy

    agent = _make_agent()
    agent.client.chat.completions.create.side_effect = [
        RuntimeError("provider 500"),
        _completion("", 0),
    ]

    assert _summarize(agent) == site_copy("max_iterations_no_summary", limit=agent.max_iterations)
    assert agent.client.chat.completions.create.call_count == 2


@patch("agent.process_bootstrap.OpenAI")
def test_interrupted_summary_still_propagates(_mock_openai, monkeypatch):
    """Cancellation is not a summary failure and must not be retried or swallowed.

    ``handle_max_iterations`` requeues the turn as interrupted by dropping the nudge and
    re-raising; catching the raise as "unusable summary" would break that contract.
    """
    agent = _make_agent()
    attempts = []

    def _interrupted(*args, **kwargs):
        attempts.append(1)
        raise InterruptedError("user interrupt")

    monkeypatch.setattr(agent, "_interruptible_api_call", _interrupted)

    with pytest.raises(InterruptedError):
        _summarize(agent)

    assert len(attempts) == 1, "an interrupt must not consume the summary retry slot"


@patch("agent.process_bootstrap.OpenAI")
def test_empty_summary_still_retries_without_a_raise(_mock_openai):
    """The pre-existing empty-summary retry path is unchanged by the raise handling."""
    agent = _make_agent()
    agent.client.chat.completions.create.side_effect = [_completion("", 0), _completion(SUMMARY)]

    assert _summarize(agent) == SUMMARY
    assert agent.client.chat.completions.create.call_count == 2


def _choiceless():
    """The shape the issue reports: the provider answered 200 with ``choices`` unset."""
    return SimpleNamespace(choices=None, usage=None, model="m")


@patch("agent.process_bootstrap.OpenAI")
def test_choiceless_response_retry_recovers_the_summary(_mock_openai):
    """The literal #123436 repro: ``choices=None`` must reach the retry, not end the turn.

    ``choices=None`` surfaces as ``TypeError: 'NoneType' object is not subscriptable``
    from inside ``normalize_response`` — i.e. it *is* the raising-transport case, raised
    by the normalizer rather than the wire. Before the fix this escaped on attempt 0, so
    the turn returned failure copy and never asked for the perfectly good summary sitting
    one attempt away.
    """
    agent = _make_agent()
    agent.client.chat.completions.create.side_effect = [_choiceless(), _completion(SUMMARY)]

    assert _summarize(agent) == SUMMARY
    assert agent.client.chat.completions.create.call_count == 2


@patch("agent.process_bootstrap.OpenAI")
def test_choiceless_response_never_leaks_the_internal_typeerror(_mock_openai):
    """Every attempt choiceless: graceful copy, and no ``subscriptable`` text for the user."""
    from agent.turn_failure_copy import site_copy

    agent = _make_agent()
    agent.client.chat.completions.create.side_effect = [_choiceless(), _choiceless()]

    result = _summarize(agent)

    assert result == site_copy("max_iterations_no_summary", limit=agent.max_iterations)
    assert "subscriptable" not in result
    assert agent.client.chat.completions.create.call_count == 2