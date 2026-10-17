"""Unit tests for the custom provider profile's reasoning wiring.

``provider=custom`` covers any OpenAI-compatible endpoint the user points
Hermes at — local Ollama, vLLM, llama.cpp, and hosted reasoning APIs like
GLM-5.2 on Volcengine ARK. Before #57601's salvage, ``CustomProfile`` emitted
nothing when reasoning was *enabled*, so a configured ``reasoning_effort``
was silently dropped for every custom endpoint.

These tests pin the wire-shape contract:
    - disabled on Ollama  → extra_body.think = False + reasoning_effort=none
    - disabled elsewhere  → reasoning_effort=none, no think (strict APIs 422)
    - enabled + effort    → top-level reasoning_effort (native OpenAI-compat
                          format GLM/ARK expect), passed through verbatim
                          including ``max``/``xhigh``
    - enabled + no effort → nothing emitted (endpoint's server default applies)
    - ollama_num_ctx      → extra_body.options.num_ctx, orthogonal to reasoning
"""

from __future__ import annotations

import pytest


@pytest.fixture
def custom_profile():
    """Resolve the registered custom profile via the global registry.

    Importing ``model_tools`` triggers plugin discovery, which registers the
    ``custom`` profile. Going through ``get_provider_profile`` keeps the test
    honest — if the registered class is ever downgraded to a plain
    ``ProviderProfile``, the assertions below collapse.
    """
    import model_tools  # noqa: F401
    import providers

    profile = providers.get_provider_profile("custom")
    assert profile is not None, "custom provider profile must be registered"
    return profile


class TestCustomReasoningWireShape:
    """``build_api_kwargs_extras`` produces the correct wire format."""

    def test_no_reasoning_config_emits_nothing(self, custom_profile):
        """Unset reasoning → omit everything so the endpoint's default applies."""
        eb, tl = custom_profile.build_api_kwargs_extras(
            reasoning_config=None, model="glm-5.2"
        )
        assert eb == {}
        assert tl == {}

    def test_disabled_sends_think_false(self, custom_profile):
        """enabled=False on an Ollama URL → reasoning_effort='none' + think=False.

        Both fields are required on Ollama: /v1/chat/completions silently
        ignores extra_body.think (only /api/chat honours it — ollama#14820)
        but respects top-level reasoning_effort (#25758). think=False stays
        for proxies and the native /api/chat path.
        """
        eb, tl = custom_profile.build_api_kwargs_extras(
            reasoning_config={"enabled": False},
            model="qwen3",
            base_url="http://127.0.0.1:11434/v1",
        )
        assert eb == {"think": False}
        assert tl == {"reasoning_effort": "none"}

    def test_effort_none_sends_think_false(self, custom_profile):
        """effort='none' is the disable alias → same dual emission on Ollama."""
        eb, tl = custom_profile.build_api_kwargs_extras(
            reasoning_config={"enabled": True, "effort": "none"},
            model="qwen3",
            base_url="http://localhost:11434/v1",
        )
        assert eb == {"think": False}
        assert tl == {"reasoning_effort": "none"}

    def test_disabled_omits_think_on_strict_host(self, custom_profile):
        """Strict OpenAI-compat hosts forbid extra ``think`` (HTTP 422).

        Uses a non-Ministral slug on purpose: ``ministral-*`` now suppresses the whole
        reasoning vocabulary (#119249), so it can no longer witness the ``think`` rule.
        """
        eb, tl = custom_profile.build_api_kwargs_extras(
            reasoning_config={"enabled": True, "effort": "none"},
            model="gpt-5.2",
            base_url="https://api.mistral.ai/v1",
        )
        assert "think" not in eb
        assert tl == {"reasoning_effort": "none"}

    def test_disabled_omits_think_without_base_url(self, custom_profile):
        """Unknown custom endpoint — do not send the Ollama-only flag."""
        eb, tl = custom_profile.build_api_kwargs_extras(
            reasoning_config={"enabled": False}, model="glm-5.2"
        )
        assert "think" not in eb
        assert tl == {"reasoning_effort": "none"}

    @pytest.mark.parametrize(
        "base_url",
        [
            "http://127.0.0.1:8080/v1",
            "http://localhost:1234/v1",
            "https://api.groq.com/openai/v1",
        ],
    )
    def test_disabled_omits_think_on_non_ollama_relays(self, custom_profile, base_url):
        eb, tl = custom_profile.build_api_kwargs_extras(
            reasoning_config={"effort": "none"},
            model="llama3",
            base_url=base_url,
        )
        assert "think" not in eb
        assert tl == {"reasoning_effort": "none"}

    def test_disabled_sends_think_false_on_ollama_cloud_host(self, custom_profile):
        eb, tl = custom_profile.build_api_kwargs_extras(
            reasoning_config={"enabled": False},
            model="qwen3",
            base_url="https://ollama.com/v1",
        )
        assert eb == {"think": False}
        assert tl == {"reasoning_effort": "none"}

    @pytest.mark.parametrize(
        "base_url",
        [
            "http://myhost:99999/v1",  # out-of-range port: OpenAI client accepts it
            "http://localhost:80a/v1",  # non-integer port
            "http://localhost:11434./v1",  # trailing-dot port
        ],
    )
    def test_malformed_port_does_not_raise(self, custom_profile, base_url):
        """Malformed ports must not raise — urlparse's ``port`` is ValueError-happy.

        The OpenAI client accepts ``http://myhost:99999/v1`` at construction
        (only httpx fails later), so these URLs reach ``build_api_kwargs_extras``
        in production. The heuristic must treat them as non-Ollama rather than
        killing the kwargs build.
        """
        eb, tl = custom_profile.build_api_kwargs_extras(
            reasoning_config={"enabled": False},
            model="qwen3",
            base_url=base_url,
        )
        assert "think" not in eb
        assert tl == {"reasoning_effort": "none"}

    @pytest.mark.parametrize(
        "effort", ["minimal", "low", "medium", "high", "xhigh", "max"]
    )
    def test_enabled_effort_goes_top_level(self, custom_profile, effort):
        """enabled + effort → TOP-LEVEL reasoning_effort, passed through verbatim.

        GLM-5.2/ARK and OpenAI-compatible reasoning APIs read reasoning_effort
        as a top-level string, not nested in extra_body. ``max`` is GLM's
        native deep-reasoning level and must survive.
        """
        eb, tl = custom_profile.build_api_kwargs_extras(
            reasoning_config={"enabled": True, "effort": effort}, model="glm-5.2"
        )
        assert tl == {"reasoning_effort": effort}
        assert "reasoning_effort" not in eb
        assert "think" not in eb


    def test_does_not_force_think_true_on_enable(self, custom_profile):
        """We must never send think=True on enable — it's Ollama-only and
        would 400 on GLM/vLLM endpoints that don't recognize it."""
        eb, _ = custom_profile.build_api_kwargs_extras(
            reasoning_config={"enabled": True, "effort": "high"}, model="glm-5.2"
        )
        assert eb.get("think") is not True

    @pytest.mark.parametrize(
        "reasoning_config, expected",
        [({"enabled": True, "effort": "high"}, "default"), ({"enabled": False, "effort": "medium"}, "none")],
    )
    def test_groq_host_clamps_effort_to_groq_vocabulary(self, custom_profile, reasoning_config, expected):
        """api.groq.com accepts top-level reasoning_effort only as 'none' / 'default' (#75089).

        Drives the main transport so the clamp is proven where production reads it.
        """
        from agent.transports.chat_completions import ChatCompletionsTransport

        kwargs = ChatCompletionsTransport().build_kwargs(
            model="qwen/qwen3.6-27b", messages=[{"role": "user", "content": "ping"}], tools=None,
            provider_profile=custom_profile, reasoning_config=reasoning_config,
            base_url="https://api.groq.com/openai/v1", provider_name="custom",
        )
        assert kwargs["reasoning_effort"] == expected
        assert "think" not in kwargs.get("extra_body", {}) and "reasoning" not in kwargs.get("extra_body", {})


class TestCustomReasoningWithNumCtx:
    """Ollama num_ctx and reasoning are independent and compose."""

    def test_num_ctx_alone(self, custom_profile):
        eb, tl = custom_profile.build_api_kwargs_extras(
            reasoning_config=None, ollama_num_ctx=8192, model="qwen3"
        )
        assert eb == {"options": {"num_ctx": 8192}}
        assert tl == {}


class TestCustomResponsesEffortVocabulary:
    """The Responses transport honours a custom endpoint's configured ``max`` (#114249).

    A custom relay's models are not on the OpenAI per-model ladder; without the
    profile declaration the Responses transport clamped a configured ``max`` to
    ``xhigh`` while chat-completions on the same provider forwarded ``max``.
    """

    def test_named_relay_keeps_configured_max_where_chat_completions_does(self, custom_profile):
        from agent.transports.codex import _resolve_reasoning

        reasoning = {"enabled": True, "effort": "max"}
        _, top_level = custom_profile.build_api_kwargs_extras(
            reasoning_config=reasoning, base_url="https://relay.example/v1"
        )
        effort, enabled = _resolve_reasoning(
            "deepseek-flash",
            {"provider": "custom:relay", "base_url": "https://relay.example/v1", "reasoning_config": reasoning},
        )
        assert (effort, enabled) == (top_level["reasoning_effort"], True) == ("max", True)

    def test_custom_entry_at_the_official_openai_origin_keeps_the_per_model_ladder(self):
        """api.openai.com's vocabulary is known per model; the custom declaration must not
        put ``max`` on the wire for a slug OpenAI rejects it on."""
        from agent.transports.codex import _resolve_reasoning

        effort, enabled = _resolve_reasoning(
            "gpt-5.2",
            {
                "provider": "custom:oai",
                "base_url": "https://api.openai.com/v1",
                "reasoning_config": {"enabled": True, "effort": "max"},
            },
        )
        assert (effort, enabled) == ("xhigh", True)


#: ``provider=custom`` pointed at api.mistral.ai. Mistral answers every request carrying
#: ``reasoning_effort`` with ``HTTP 400 reasoning_effort is not enabled for this model``,
#: so no vocabulary member — including the ``none`` disable alias — belongs on the wire.
MINISTRAL_BASE_URL = "https://api.mistral.ai/v1"
NON_REASONING_MINISTRAL_MODELS = [
    "ministral-14b-2512",
    "ministral-3b-2512",
    "ministral-small-latest",
    "ministral-large-latest",
]


class TestMistralRejectsReasoningEffort:
    """#119249: the custom profile must send no reasoning controls to non-reasoning
    Ministral models.

    Both emission paths are covered: the graded effort that ``default_reasoning_config``
    injects for an unset ``agent.reasoning_effort`` (the main-loop 400), and the
    ``reasoning_effort="none"`` disable alias — removing only the disable path leaves
    the default path still 400ing.
    """

    @pytest.mark.parametrize("model", NON_REASONING_MINISTRAL_MODELS)
    def test_graded_effort_is_not_sent(self, custom_profile, model):
        """The main-loop case: unset user effort becomes ``medium`` — still never sent."""
        eb, tl = custom_profile.build_api_kwargs_extras(
            reasoning_config={"enabled": True, "effort": "medium"},
            model=model,
            base_url=MINISTRAL_BASE_URL,
        )
        assert "reasoning_effort" not in tl
        assert "reasoning_effort" not in eb
        assert "think" not in eb

    @pytest.mark.parametrize("model", NON_REASONING_MINISTRAL_MODELS)
    @pytest.mark.parametrize(
        "reasoning_config",
        [
            {"enabled": False},
            {"enabled": True, "effort": "none"},
            {"effort": "none"},
            {"enabled": True, "effort": "high"},
        ],
        ids=["enabled-false", "effort-none", "bare-none", "high"],
    )
    def test_no_effort_variant_is_sent(self, custom_profile, model, reasoning_config):
        """No vocabulary member survives — ``none`` is rejected exactly like ``medium``."""
        eb, tl = custom_profile.build_api_kwargs_extras(
            reasoning_config=reasoning_config, model=model, base_url=MINISTRAL_BASE_URL
        )
        assert "reasoning_effort" not in tl
        assert "reasoning_effort" not in eb
        assert "think" not in eb

    @pytest.mark.parametrize("model", NON_REASONING_MINISTRAL_MODELS)
    def test_main_transport_builds_no_reasoning_kwarg(self, custom_profile, model):
        """Drive the real main transport: the 400 the reporter saw came from this path."""
        from agent.transports.chat_completions import ChatCompletionsTransport

        kwargs = ChatCompletionsTransport().build_kwargs(
            model=model,
            messages=[{"role": "user", "content": "ping"}],
            tools=None,
            provider_profile=custom_profile,
            reasoning_config={"enabled": True, "effort": "medium"},
            base_url=MINISTRAL_BASE_URL,
            provider_name="custom",
        )
        assert "reasoning_effort" not in kwargs
        assert "reasoning_effort" not in kwargs.get("extra_body", {})

    def test_title_generation_path_builds_no_reasoning_kwarg(self, custom_profile):
        """Drive the auxiliary projection seam the reporter's title 400 came through.

        Calling the profile directly (as a sibling test does) would only re-prove the
        unit; ``_project_provider_profile`` is the function auxiliary calls that turns a
        ``reasoning_config`` into wire kwargs, so it is what has to stay clean.
        """
        from agent.auxiliary_client import _project_provider_profile

        projection = _project_provider_profile(
            "mistral", "custom", "ministral-3b-2512", MINISTRAL_BASE_URL,
            {"enabled": True, "effort": "medium"},
        )
        assert "reasoning_effort" not in projection.top_level
        assert "reasoning_effort" not in projection.reasoning_extra

    def test_vendor_prefixed_slug_is_still_recognised(self, custom_profile):
        """A ``vendor/model`` slug must be matched on its bare segment."""
        _, tl = custom_profile.build_api_kwargs_extras(
            reasoning_config={"enabled": True, "effort": "medium"},
            model="mistralai/ministral-14b-2512",
            base_url=MINISTRAL_BASE_URL,
        )
        assert "reasoning_effort" not in tl

    def test_orthogonal_extra_body_fields_survive(self, custom_profile):
        """The guard drops reasoning only — ``num_ctx`` is unrelated plumbing."""
        eb, _ = custom_profile.build_api_kwargs_extras(
            reasoning_config={"enabled": True, "effort": "medium"},
            model="ministral-14b-2512",
            base_url=MINISTRAL_BASE_URL,
            ollama_num_ctx=8192,
        )
        assert eb == {"options": {"num_ctx": 8192}}

    @pytest.mark.parametrize("model", ["magistral-medium-latest", "magistral-small-latest"])
    def test_reasoning_capable_mistral_models_keep_their_effort(self, custom_profile, model):
        """Scope is the non-reasoning ``ministral-*`` family only.

        ``magistral-*`` is Mistral's reasoning-tuned line and does accept
        ``reasoning_effort``; a blanket "any Mistral model" rule would break it.
        It shares the ``mistral`` spelling but not the ``ministral-`` prefix.
        """
        _, tl = custom_profile.build_api_kwargs_extras(
            reasoning_config={"enabled": True, "effort": "medium"},
            model=model,
            base_url=MINISTRAL_BASE_URL,
        )
        assert tl == {"reasoning_effort": "medium"}

    def test_non_ministral_model_on_mistral_host_is_untouched(self, custom_profile):
        """A relay serving other vendors through the same host keeps the wire contract."""
        _, tl = custom_profile.build_api_kwargs_extras(
            reasoning_config={"enabled": True, "effort": "high"},
            model="qwen3-235b",
            base_url=MINISTRAL_BASE_URL,
        )
        assert tl == {"reasoning_effort": "high"}

    def test_default_reasoning_config_still_fills_medium(self, custom_profile):
        """The fix lives at the wire edge, so the default-config contract is unchanged.

        ``default_reasoning_config`` stays host-agnostic: it answers what to *ask* for,
        and ``build_api_kwargs_extras`` decides what may actually be *sent*.
        """
        assert custom_profile.default_reasoning_config("ministral-14b-2512") == {
            "enabled": True,
            "effort": "medium",
        }
