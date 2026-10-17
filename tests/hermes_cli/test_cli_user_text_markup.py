"""User-authored text reaches Rich consoles as literal text, never as markup.

Companion to #98789 (the ``-q`` query echo). The same defect class reached the
other console output paths that interpolate content the user wrote — a skill
bundle's ``description``/``instruction``, the skill names a bundle references,
and the Tier 1 advisory text built from a scanned skill file's findings. Any of
them containing a bracket shape Rich reads as an unmatched tag raised
``MarkupError`` and took the command down.

A stubbed ``print`` would hide this, so every console here is a real Rich
``Console`` writing to a buffer.
"""

from __future__ import annotations

import io
from types import SimpleNamespace

import pytest
from rich.console import Console

import cli
from hermes_cli import bundles, skills_hub
from tools.skillevaluator_scan import Tier1Finding, Tier1Report


def _bundle_info(**overrides):
    info = {
        "slug": "ops",
        "name": "release [/weird] bundle",
        "description": "fix [/x] before shipping",
        "instruction": "then do [/y] this",
        "path": "/tmp/bundles/ops.yaml",
        "skills": ["deploy [/z] helper"],
    }
    info.update(overrides)
    return info


def test_bundle_show_renders_bracket_shaped_metadata_without_crashing(monkeypatch):
    """The bundle's own words must reach the terminal verbatim.

    A ``description`` like ``fix [/x] before shipping`` is a Rich closing tag with
    nothing open; it used to abort ``hermes bundles show`` with ``MarkupError``.
    """
    buffer = io.StringIO()
    console = Console(file=buffer, force_terminal=False, width=10_000, no_color=True)
    monkeypatch.setattr(bundles, "_console", lambda: console)
    monkeypatch.setattr(bundles, "get_bundle", lambda name: _bundle_info())

    bundles._cmd_show(SimpleNamespace(name="ops"))

    out = buffer.getvalue()
    assert "fix [/x] before shipping" in out
    assert "then do [/y] this" in out
    assert "deploy [/z] helper" in out
    assert "release [/weird] bundle" in out


# --- `cli._queue_loaded_skills`: names of bundle members that did not load --


@pytest.mark.parametrize(
    "missing",
    [
        ["deploy [/weird] helper"],
        ["a[/]", "b[/]"],
        ["plain-missing-skill"],
    ],
)
def test_missing_bundle_members_are_named_literally(monkeypatch, missing):
    """A bundle referencing an uninstalled skill must name it, not crash on its brackets.

    The ``[yellow]…[/]`` frame is ours; only the joined names are user content.
    """
    buffer = io.StringIO()
    console = Console(file=buffer, force_terminal=False, width=10_000, no_color=True)
    monkeypatch.setattr(cli, "ChatConsole", lambda: console)
    stub = SimpleNamespace(_queue_skill_message=lambda msg: None)

    cli.HermesCLI._queue_loaded_skills(stub, "msg", "loading", missing)

    assert ", ".join(missing) in buffer.getvalue()


# --- Tier 1 skill advisory: text built from a scanned skill's findings -------


def test_tier1_advisory_text_is_shown_even_when_it_looks_like_markup(monkeypatch, tmp_path):
    """A finding message that reads as markup must still reach the user.

    ``_print_tier1_advisory`` is wrapped in a blanket ``except Exception``, so the
    ``MarkupError`` was swallowed and the security advisory silently vanished --
    the worst possible outcome for a credential warning.
    """
    from tools import skillevaluator_scan as scan

    buffer = io.StringIO()
    console = Console(file=buffer, force_terminal=False, width=10_000, no_color=True)
    report = Tier1Report(
        available=True,
        passed=False,
        findings=[
            Tier1Finding(
                check="emails", validator="PII Scan", severity="high",
                message="possible address [/weird] found", file="notes.md", line=7,
            )
        ],
    )
    monkeypatch.setattr(scan, "tier1_advisory_enabled", lambda: True)
    monkeypatch.setattr(scan, "run_tier1_scan", lambda path: report)

    skills_hub._print_tier1_advisory(tmp_path, console)

    out = buffer.getvalue()
    assert "possible address [/weird] found" in out
    assert "notes.md:7" in out