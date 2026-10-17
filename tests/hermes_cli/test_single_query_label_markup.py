"""``chat -q`` echoes the query verbatim — arbitrary text must never be parsed as Rich markup.

The non-quiet one-shot path printed the query as part of a markup-enabled
``Console.print`` f-string, so a query file containing a pytest-style parameter
token (``test_case[/fb-images/a/../b.webp?w=336]``) made Rich raise
``MarkupError`` and the CLI exited 1 *before the agent turn started* — a valid
autonomous handoff failed on incidental prose (#98789).
"""

from __future__ import annotations

import io
from types import SimpleNamespace

import pytest
from rich.console import Console

import cli


@pytest.fixture(autouse=True)
def _no_inherited_kanban_env(monkeypatch):
    monkeypatch.delenv("HERMES_KANBAN_TASK", raising=False)
    monkeypatch.delenv("HERMES_KANBAN_GOAL_MODE", raising=False)


def _run_non_quiet(monkeypatch, query, images=None):
    """Drive ``_run_single_query_mode`` down the non-quiet tail with a REAL Rich console.

    A stubbed ``print`` would swallow the markup parser and hide the regression, so
    the console is genuine and its output is captured.

    Returns ``(exit_code, rendered_output)``.
    """
    monkeypatch.setattr(cli, "_should_seed_interactive", lambda *a, **k: False)
    monkeypatch.setattr(cli, "_collect_query_images", lambda q, i: (q, images or []))
    monkeypatch.setattr(cli, "_collect_kanban_task_images", lambda imgs: [])
    monkeypatch.setattr(cli, "_finalize_single_query", lambda c: None)

    buffer = io.StringIO()
    stub = SimpleNamespace(
        _single_query_mode=False,
        _claim_active_session=lambda *a, **k: True,
        console=Console(file=buffer, force_terminal=False, width=10_000, no_color=True),
        _show_security_advisories=lambda: None,
        chat=lambda *a, **k: "response",
        _print_exit_summary=lambda **k: None,
        # A real completed turn. ``turn_exit_code`` maps a non-dict result to 1 ("never ran"),
        # so a None here would assert the wrong contract.
        _last_turn_result={"failed": False, "partial": False, "completed": True},
    )
    try:
        cli._run_single_query_mode(stub, query, None, False, True)
    except SystemExit as exc:
        return exc.code, buffer.getvalue()
    return None, buffer.getvalue()


# The reported shape, plus the other bracket/prose shapes that parse as Rich tags.
@pytest.mark.parametrize(
    "query",
    [
        "test_case[/fb-images/a/../b.webp?w=336]",
        "check [/red]this[/]",
        "match re.compile(r'^\\[a-z]+$')",
        "array[0] and dict{'k': 'v'}",
        "check this[/nope] closes nothing",
    ],
)
def test_query_text_that_looks_like_rich_markup_still_reaches_the_turn(monkeypatch, query):
    """#98789: the query must reach the agent turn instead of crashing the CLI at the echo."""
    code, output = _run_non_quiet(monkeypatch, query)

    assert code == 0, "the turn completed and the CLI exited on the answer, not on a MarkupError"
    assert query in output, "the query is echoed literally — not consumed as markup"


def test_query_echo_renders_the_label_without_swallowing_the_query(monkeypatch):
    """The styled ``Query:`` label survives; only the user's text is de-markuped."""
    _, output = _run_non_quiet(monkeypatch, "plain prompt text")

    assert "Query:" in output
    assert "plain prompt text" in output


def test_image_only_query_still_renders_its_placeholder(monkeypatch):
    """An image-only run has no query text; the placeholder must survive as literal text.

    It is itself bracket-shaped, so it is only visible when the label is not parsed
    as markup.
    """
    code, output = _run_non_quiet(monkeypatch, None, images=["/tmp/x.png"])

    assert code == 0
    assert "[image attached]" in output