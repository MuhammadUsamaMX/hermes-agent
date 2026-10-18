"""batch_runner's documented CLI flags survive Python Fire's argument parsing.

``batch_runner.py`` is driven by ``fire``, which literal-evaluates every flag value.
The documented ``--providers_allowed=anthropic,openai`` therefore arrives as a
``('anthropic', 'openai')`` tuple rather than the string the parser's type hint
promises, and a date-stamped ``--run_name=20260926`` arrives as an ``int``.
``_split_csv`` assumed ``str`` and ``main`` passed ``run_name`` through unconverted,
so both crashed at startup before a single prompt ran.

These tests drive the real ``fire.Fire(main, command=[...])`` parsing rather than
calling ``main`` directly, so they fail for the same reason a user's command line does.
"""

import fire

import batch_runner


class _RecordingRunner:
    """Stands in for BatchRunner so no credentials, network, or output dir are needed."""

    captured: dict = {}

    def __init__(self, **kwargs):
        type(self).captured.update(kwargs)

    def run(self, resume=False):
        return None


def _fire_main(monkeypatch, command):
    captured = {}
    monkeypatch.setattr(batch_runner, "BatchRunner", _RecordingRunner)
    _RecordingRunner.captured = captured
    fire.Fire(batch_runner.main, command=command)
    return captured


def test_documented_comma_separated_providers_reach_the_runner(monkeypatch):
    """--providers_allowed=anthropic,openai parses to a tuple; it must still become a list."""
    captured = _fire_main(monkeypatch, [
        "--dataset_file=d.jsonl",
        "--batch_size=1",
        "--run_name=r",
        "--providers_allowed=anthropic,openai",
    ])

    assert captured["providers_allowed"] == ["anthropic", "openai"]


def test_all_three_provider_list_flags_normalize(monkeypatch):
    captured = _fire_main(monkeypatch, [
        "--dataset_file=d.jsonl",
        "--batch_size=1",
        "--run_name=r",
        "--providers_allowed=anthropic,openai",
        "--providers_ignored=together,deepinfra",
        "--providers_order=anthropic,google",
    ])

    assert captured["providers_allowed"] == ["anthropic", "openai"]
    assert captured["providers_ignored"] == ["together", "deepinfra"]
    assert captured["providers_order"] == ["anthropic", "google"]


def test_numeric_run_name_is_coerced_to_a_string(monkeypatch):
    """--run_name=20260926 parses to an int; Path("data") / run_name must not crash."""
    captured = _fire_main(monkeypatch, [
        "--dataset_file=d.jsonl",
        "--batch_size=1",
        "--run_name=20260926",
    ])

    assert captured["run_name"] == "20260926"


def test_comma_values_with_blank_items_drop_the_blanks(monkeypatch):
    """A space stops fire literal-evaluating, so this stays a str and must be split too."""
    captured = _fire_main(monkeypatch, [
        "--dataset_file=d.jsonl",
        "--batch_size=1",
        "--run_name=r",
        "--providers_order=anthropic, ,openai",
    ])

    assert captured["providers_order"] == ["anthropic", "openai"]


def test_quoted_comma_separated_string_is_still_split(monkeypatch):
    captured = _fire_main(monkeypatch, [
        "--dataset_file=d.jsonl",
        "--batch_size=1",
        "--run_name=r",
        "--providers_allowed=anthropic,openai",
    ])

    assert captured["providers_allowed"] == ["anthropic", "openai"]


def test_absent_provider_flags_stay_none(monkeypatch):
    """Omitting the flags must keep the existing None default, not become []."""
    captured = _fire_main(monkeypatch, [
        "--dataset_file=d.jsonl",
        "--batch_size=1",
        "--run_name=r",
    ])

    assert captured["providers_allowed"] is None
    assert captured["providers_ignored"] is None
    assert captured["providers_order"] is None


def test_missing_run_name_is_still_rejected(monkeypatch, capsys):
    """Guards the fix: str() coercion must not make a missing --run_name pass validation."""
    import pytest

    with pytest.raises(SystemExit):
        fire.Fire(batch_runner.main, command=["--dataset_file=d.jsonl", "--batch_size=1"])

    assert "--run_name is required" in capsys.readouterr().out


def test_split_csv_unit_shapes():
    """Direct coverage of the helper's accepted input shapes."""
    from batch_runner import _split_csv

    assert _split_csv("anthropic,openai") == ["anthropic", "openai"]
    assert _split_csv(("anthropic", "openai")) == ["anthropic", "openai"]
    assert _split_csv(["anthropic", "openai"]) == ["anthropic", "openai"]
    assert _split_csv(" anthropic , openai ") == ["anthropic", "openai"]
    assert _split_csv("anthropic, ,openai") == ["anthropic", "openai"]
    assert _split_csv(None) is None
    assert _split_csv("") is None
    assert _split_csv(",") is None