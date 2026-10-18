"""Regression tests for #122010 — a provider switch must re-derive fast-mode
``request_overrides`` (``speed`` / ``service_tier``) for the NEW route.

Before the fix, ``_apply_switched_provider_request_overrides`` cleared the previous
provider's ``extra_body`` but kept the previous route's fast-mode keys. An Anthropic
fast-capable primary pins ``{"speed": "fast"}`` into ``agent.request_overrides``; on
failover to a chat-completions route the transport copies every non-``extra_body``
override to a top-level SDK kwarg, so ``openai`` raises::

    TypeError: Completions.create() got an unexpected keyword argument 'speed'

The single fast-mode gate (``hermes_cli.models.resolve_fast_mode_overrides``) decides
per route whether the params are valid at all; the switch never re-ran it.
"""

import agent.agent_runtime_helpers as arh
from hermes_cli.models import resolve_fast_mode_overrides


class _Agent:
    pass


def _agent(*, model, base_url, provider, request_overrides, service_tier="priority",
           custom_providers=None, api_mode="chat_completions"):
    a = _Agent()
    # switch_model() sets these on the live agent before calling the helper.
    a.model = model
    a.base_url = base_url
    a.provider = provider
    a.api_mode = api_mode
    a.service_tier = service_tier
    a.request_overrides = request_overrides
    a._custom_providers = custom_providers or []
    return a


def test_switch_from_anthropic_fast_to_local_route_drops_speed():
    """The reported crash: an Anthropic fast route's ``speed`` must not survive the
    switch onto a local OpenAI-compatible route the gate rejects."""
    a = _agent(
        model="Hermes-3-Llama-3.1-8B-4bit",
        base_url="http://127.0.0.1:8080/v1",
        provider="custom",
        request_overrides={"speed": "fast"},
    )
    # the gate says this route gets no fast params at all
    assert resolve_fast_mode_overrides(
        a.model, provider="custom", base_url="http://127.0.0.1:8080/v1") is None

    arh._apply_switched_provider_request_overrides(a, "custom")

    assert "speed" not in a.request_overrides
    assert "service_tier" not in a.request_overrides


def test_switch_from_anthropic_fast_to_openai_route_rederives_service_tier():
    """Switching onto an OpenAI priority-capable route re-derives through the gate:
    ``service_tier`` for OpenAI, and never Anthropic's ``speed``."""
    a = _agent(
        model="gpt-5.4",
        base_url="https://api.openai.com/v1",
        provider="openai",
        request_overrides={"speed": "fast"},
    )
    arh._apply_switched_provider_request_overrides(a, "openai")

    assert a.request_overrides == {"service_tier": "priority"}
    assert "speed" not in a.request_overrides


def test_switch_to_anthropic_fast_route_keeps_speed():
    """Re-deriving must not lose fast mode: a switch that lands back on an Anthropic
    fast-capable route re-pins ``speed``."""
    a = _agent(
        model="claude-opus-5",
        base_url="https://api.anthropic.com",
        provider="anthropic",
        request_overrides={"service_tier": "priority"},
        api_mode="anthropic_messages",
    )
    arh._apply_switched_provider_request_overrides(a, "anthropic")

    assert a.request_overrides["speed"] == "fast"
    assert "service_tier" not in a.request_overrides


def test_switch_with_fast_mode_off_does_not_invent_overrides():
    """With fast mode off, a switch adds no fast params even if a stale one was carried."""
    a = _agent(
        model="gpt-5.4",
        base_url="https://api.openai.com/v1",
        provider="openai",
        request_overrides={"speed": "fast"},
        service_tier=None,
    )
    arh._apply_switched_provider_request_overrides(a, "openai")

    assert "speed" not in a.request_overrides
    assert "service_tier" not in a.request_overrides


def test_switch_preserves_unrelated_overrides_and_new_extra_body():
    """The gate re-derivation is scoped to the fast-mode keys only — user-configured
    overrides and the new provider's extra_body survive the switch."""
    custom_providers = [
        {
            "name": "local",
            "base_url": "http://10.0.0.1:8000/v1",
            "model": "think-model",
            "extra_body": {"chat_template_kwargs": {"enable_thinking": True}},
        },
    ]
    a = _agent(
        model="think-model",
        base_url="http://10.0.0.1:8000/v1",
        provider="custom",
        request_overrides={"speed": "fast", "seed": 7},
        custom_providers=custom_providers,
    )
    arh._apply_switched_provider_request_overrides(a, "custom:local")

    assert a.request_overrides["extra_body"] == {"chat_template_kwargs": {"enable_thinking": True}}
    assert a.request_overrides["seed"] == 7  # unrelated override untouched
    assert "speed" not in a.request_overrides


def test_switched_overrides_reach_the_sdk_without_speed():
    """End of the chain: the switched agent's effective overrides are valid kwargs for
    the chat-completions transport — no Anthropic-only ``speed`` reaches the SDK."""
    import inspect

    a = _agent(
        model="Hermes-3-Llama-3.1-8B-4bit",
        base_url="http://127.0.0.1:8080/v1",
        provider="custom",
        request_overrides={"speed": "fast"},
    )
    arh._apply_switched_provider_request_overrides(a, "custom")

    import openai

    accepted = set(inspect.signature(openai.OpenAI(api_key="x").chat.completions.create).parameters)
    assert "speed" not in a.request_overrides
    assert all(k in accepted for k in a.request_overrides)


def test_ultrafast_tier_is_preserved_not_downgraded():
    """``ultrafast`` is a static tier too: re-deriving must pass the tier through the gate,
    never silently downgrade it to ``priority`` (the gate documents "never a silent
    downgrade to a different paid tier")."""
    from hermes_cli.models import resolve_fast_mode_overrides

    assert resolve_fast_mode_overrides(
        "gpt-6-astra", provider="openai", base_url="https://api.openai.com/v1", tier="ultrafast"
    ) == {"service_tier": "ultrafast"}

    a = _agent(
        model="gpt-6-astra",
        base_url="https://api.openai.com/v1",
        provider="openai",
        request_overrides={"service_tier": "ultrafast"},
        service_tier="ultrafast",
    )
    arh._apply_switched_provider_request_overrides(a, "openai")
    assert a.request_overrides == {"service_tier": "ultrafast"}

    # ...but a non-ultrafast model gets nothing (never downgraded to "priority").
    plain = _agent(
        model="gpt-5.4",
        base_url="https://api.openai.com/v1",
        provider="openai",
        request_overrides={"service_tier": "ultrafast"},
        service_tier="ultrafast",
    )
    arh._apply_switched_provider_request_overrides(plain, "openai")
    assert plain.request_overrides == {}


# --- the fallback path (the crash path in the issue's log) --------------------------
# try_activate_fallback() swaps model/provider/base_url in place and re-derives the
# custom_providers extra_body via _rescope_fallback_extra_body, but never touched the
# fast-mode keys — so an Anthropic ``speed`` survived every rung of the chain.
def test_fallback_to_local_route_drops_speed():
    """The reported live crash: falling back from Anthropic fast to an OpenAI-compatible
    local server must not send Anthropic's ``speed`` to the chat-completions SDK."""
    import agent.chat_completion_helpers as cch

    a = _agent(
        model="Hermes-3-Llama-3.1-8B-4bit",  # already swapped to the fallback route
        base_url="http://127.0.0.1:8080/v1",
        provider="custom",
        request_overrides={"speed": "fast"},
    )
    cch._rescope_fallback_extra_body(
        a, old_model="claude-opus-5", old_provider="anthropic",
        old_base_url="https://api.anthropic.com")

    assert "speed" not in a.request_overrides
    assert "service_tier" not in a.request_overrides


def test_fallback_to_openai_route_rederives_service_tier():
    """Falling back onto an OpenAI priority route re-derives ``service_tier`` for it."""
    import agent.chat_completion_helpers as cch

    a = _agent(
        model="gpt-5.4",
        base_url="https://api.openai.com/v1",
        provider="openai",
        request_overrides={"speed": "fast"},
    )
    cch._rescope_fallback_extra_body(
        a, old_model="claude-opus-5", old_provider="anthropic",
        old_base_url="https://api.anthropic.com")

    assert a.request_overrides == {"service_tier": "priority"}
    assert "speed" not in a.request_overrides


def test_fallback_with_fast_mode_off_does_not_invent_overrides():
    """Fast mode off: a fallback adds no fast params even if a stale one was carried."""
    import agent.chat_completion_helpers as cch

    a = _agent(
        model="gpt-5.4",
        base_url="https://api.openai.com/v1",
        provider="openai",
        request_overrides={"speed": "fast"},
        service_tier=None,
    )
    cch._rescope_fallback_extra_body(
        a, old_model="claude-opus-5", old_provider="anthropic",
        old_base_url="https://api.anthropic.com")

    assert "speed" not in a.request_overrides
    assert "service_tier" not in a.request_overrides


def test_fallback_preserves_unrelated_overrides_and_caller_extra_body():
    """Re-derivation is scoped to the fast-mode keys: a caller-supplied extra_body and
    unrelated overrides survive the fallback."""
    import agent.chat_completion_helpers as cch

    a = _agent(
        model="Hermes-3-Llama-3.1-8B-4bit",
        base_url="http://127.0.0.1:8080/v1",
        provider="custom",
        request_overrides={"speed": "fast", "seed": 7, "extra_body": {"caller": True}},
    )
    cch._rescope_fallback_extra_body(
        a, old_model="claude-opus-5", old_provider="anthropic",
        old_base_url="https://api.anthropic.com")

    assert a.request_overrides["seed"] == 7
    assert a.request_overrides["extra_body"] == {"caller": True}
    assert "speed" not in a.request_overrides