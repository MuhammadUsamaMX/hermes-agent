"""Lint guard: no model-visible renderer outside ``agent/`` may compose the imitable idiom.

Sibling of ``tests/agent/test_imitable_truncation_idiom_guard.py``, which covers the ``agent/``
tree. That guard was added because an open-coded marker in a *new* renderer is invisible until it
has poisoned real durable records — which is exactly how the sites in #83714/#83435/#110094 were
missed, then the ten in #121548, then five more afterwards. The miss kept happening because the
scan's blast radius was one directory: ``tools/``, ``hermes_cli/``, ``plugins/`` and ``cron/`` all
render elisions straight into model context and none of them were under any guard.

Every model-visible elision must route through ``agent.compression_marker.elide`` /
``elide_middle``, which mint the non-imitable
``⟪HERMES-CONTEXT-COMPRESSION: N of M chars omitted here by Hermes's context compressor.⟫``
marker: non-prose delimiters, an explicit "not original content" disclaimer, and per-instance
counts that make a verbatim copy visibly stale (#83435 copy-forward).

The scan parses each module and inspects **string literals**, so prose that merely *discusses* the
idiom (comments and docstrings) never trips it while every rendered marker does — including
markers assembled from implicitly concatenated multi-line f-strings, which a line-oriented regex
cannot see.
"""

from __future__ import annotations

import ast
import re
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]

# Shipped source trees outside ``agent/`` whose rendered strings reach a model: tool results,
# tool error bodies, the kanban worker context a spawned worker reads, MCP handler envelopes,
# the goal-judge prompt, agent-to-agent transcripts, and the monitor block injected into a cron
# prompt. ``gateway/`` is deliberately absent — it renders delivery footers for a chat client,
# not model context.
SCAN_DIRS = ("cron", "hermes_cli", "plugins", "tools")

# Any rendering of the idiom: "…[truncated]", "...[truncated 12 chars]", "[…truncated …]".
IMITABLE_IDIOM_RE = re.compile(
    r"(?:…|\.\.\.)\s*[\[(][^\])]{0,200}?truncat"   # "…[truncated…", "...(truncated)"
    r"|\[[^\]\n]{0,200}?truncat"                     # "[…truncated…"
)

# Legitimate hits, each with the reason it is safe. A *renderer* must not be added here: route it
# through elide()/elide_middle() instead. Only surfaces the model never reads belong here.
ALLOWED_IDIOM_SITES: dict[str, str] = {
    # Caps a payload bound for agent.log so a chatty MCP server cannot flood the log file.
    # logger.log only — this string never reaches model context.
    "tools/mcp_tool_health.py": "agent.log payload cap, logger-only, never model-visible",
    # Marks credentials REDACTED in the on-disk dashboard-auth audit log. It is a redaction
    # sentinel (deliberately terse and stable so an operator can grep it), not an elision marker
    # appended to content the model wrote.
    "hermes_cli/dashboard_auth/audit.py": (
        "credential-redaction sentinel in an audit log, not a rendered elision"
    ),
    # Bounds a third-party MiniMax OAuth HTTP error body. It is raised as an AuthError and
    # surfaced by format_auth_error() to CLI output and logs; it is never replayed into a
    # conversation, so it carries no copy-forward hazard.
    "hermes_cli/auth_minimax.py": "OAuth HTTP error body -> AuthError -> CLI/logs, not model context",
    # Header the `hermes debug` support bundle puts above a log file the operator attaches to a
    # bug report. Human-facing diagnostics, never model context.
    "hermes_cli/debug.py": "support-bundle log header for a human bug report, not model context",
    # Renders a pasted Slack table for the chat client. The model never reads this string; it is
    # a message sent to a human in Slack.
    "plugins/platforms/slack/adapter.py": "Slack chat message rendering, not model context",
    # Share-card preview line in the Yuanbao chat adapter — a message sent to a human.
    "plugins/platforms/yuanbao/adapter.py": "chat share-card preview, not model context",
    # Marker written into an on-disk browser snapshot cache. It already carries per-instance
    # counts ("truncated at N chars of M"), so a verbatim copy is visibly wrong, and the file
    # is a stored artifact rather than elided replay context.
    "tools/browser_tool_snapshot.py": "on-disk stored snapshot with per-instance counts",
    # Same shape as browser_tool_snapshot: an on-disk web-extraction cache copy whose marker
    # already carries per-instance counts and names the remedy.
    "tools/web_tools_truncate.py": "on-disk stored web cache with per-instance counts",
}


def _docstring_linenos(tree: ast.AST) -> set[int]:
    """Line numbers of every module/class/function docstring expression."""
    out: set[int] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Module | ast.ClassDef | ast.FunctionDef | ast.AsyncFunctionDef):
            body = getattr(node, "body", None)
            if body and isinstance(body[0], ast.Expr) and isinstance(body[0].value, ast.Constant):
                out.add(body[0].value.lineno)
    return out


def _idiom_sites() -> list[tuple[str, int]]:
    """Return ``[(relpath, lineno)]`` where a rendered string literal holds the idiom."""
    sites: list[tuple[str, int]] = []
    for d in SCAN_DIRS:
        for path in sorted((REPO_ROOT / d).rglob("*.py")):
            rel = path.relative_to(REPO_ROOT).as_posix()
            try:
                tree = ast.parse(path.read_text(encoding="utf-8", errors="replace"))
            except (OSError, SyntaxError):
                continue
            docs = _docstring_linenos(tree)
            for node in ast.walk(tree):
                # JoinedStr (f-string) literals surface as their Constant parts, which is
                # exactly what carries the marker text.
                if not (isinstance(node, ast.Constant) and isinstance(node.value, str)):
                    continue
                if node.lineno in docs:
                    continue
                if IMITABLE_IDIOM_RE.search(node.value):
                    sites.append((rel, node.lineno))
    return sites


def test_no_open_coded_imitable_truncation_idiom_outside_agent() -> None:
    offenders = [f"{rel}:{lineno}" for rel, lineno in _idiom_sites() if rel not in ALLOWED_IDIOM_SITES]
    assert not offenders, (
        "Open-coded imitable truncation idiom at:\n  "
        + "\n  ".join(offenders)
        + "\nRoute model-visible elision through agent.compression_marker.elide() / "
        "elide_middle() so the marker is non-imitable and carries per-instance counts, or "
        "consciously extend ALLOWED_IDIOM_SITES in this test with a justification."
    )


def test_allowlist_entries_still_exist() -> None:
    """Prune the allowlist when files are removed/renamed."""
    for rel in ALLOWED_IDIOM_SITES:
        assert (REPO_ROOT / rel).is_file(), (
            f"Allowlist entry {rel} no longer exists — remove it from ALLOWED_IDIOM_SITES."
        )


def test_allowlisted_sites_still_hold_the_idiom() -> None:
    """An allowlist entry that stops being an offender is stale and hides a future regression."""
    present = {rel for rel, _ in _idiom_sites()}
    stale = sorted(set(ALLOWED_IDIOM_SITES) - present)
    assert not stale, (
        "Allowlist entries no longer contain the idiom — delete them so a future "
        f"reintroduction is caught: {stale}"
    )
