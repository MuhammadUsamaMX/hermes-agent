"""Lint guard: no model-visible renderer may compose the imitable truncation idiom.

The bare ``...[truncated]`` idiom was imitated from replayed context into new
durable writes (#83435/#83714/#121548). Every model-visible elision must route
through ``agent.compression_marker.elide`` / ``elide_middle``, which mint the
non-imitable ``⟪HERMES-CONTEXT-COMPRESSION: N of M chars omitted…⟫`` marker.

This is the half of #121548 that was still missing after the five reported sites
were routed: without a source-level scan, an open-coded marker in a new renderer
is invisible until it has poisoned real board records — which is exactly how five
sites were missed the first time, and how four more hid afterwards.

The scan parses each module and inspects **string literals**, so prose that
merely discusses the idiom (comments and docstrings) never trips it while every
rendered marker does — including markers assembled from implicitly concatenated
multi-line f-strings, which a line-oriented regex cannot see.
"""

from __future__ import annotations

import ast
import re
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]

# Directories that make up the shipped model-visible source tree.
SCAN_DIRS = ("agent",)

# Any rendering of the idiom: "...[truncated]", "...[truncated 12 chars]",
# "[... truncated ...]", "…(truncated)", "[...truncated AGENTS.md: ...]".
IMITABLE_IDIOM_RE = re.compile(
    r"(?:…|\.\.\.)\s*[\[(][^\])]{0,200}?truncat"   # "...[truncated…", "…(truncated)"
    r"|\[[^\]\n]{0,200}?truncat"                     # "[...truncated…" / "[... truncated…"
)

# Legitimate hits, each with the reason it is safe. A renderer must not be added
# here: route it through elide()/elide_middle() instead.
ALLOWED_IDIOM_SITES: dict[str, str] = {
    # Classifies Hermes' own synthetic continuation notices so they are never
    # compressed away. These are fixed prose the model is TOLD about a cut-off
    # response — not an elision marker appended to content the model wrote.
    "agent/conversation_compression.py": "_SYNTHETIC_USER_PREFIXES detector, not a rendered elision",
    # Same synthetic-notice wording as above.
    "agent/conversation_loop.py": "synthetic continuation notice, not a rendered elision",
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
    """Return [(relpath, lineno)] where a rendered string literal holds the idiom."""
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
                # JoinedStr (f-string) literals surface as their Constant parts,
                # which is what carries the marker text.
                if not (isinstance(node, ast.Constant) and isinstance(node.value, str)):
                    continue
                if node.lineno in docs:
                    continue
                if IMITABLE_IDIOM_RE.search(node.value):
                    sites.append((rel, node.lineno))
    return sites


def test_no_open_coded_imitable_truncation_idiom_in_model_visible_source():
    offenders = [f"{rel}:{lineno}" for rel, lineno in _idiom_sites() if rel not in ALLOWED_IDIOM_SITES]
    assert not offenders, (
        "Open-coded imitable truncation idiom at:\n  "
        + "\n  ".join(offenders)
        + "\nRoute model-visible elision through agent.compression_marker.elide() / "
        "elide_middle() so the marker is non-imitable and carries per-instance "
        "counts, or consciously extend ALLOWED_IDIOM_SITES in this test with a "
        "justification."
    )


def test_allowlist_entries_still_exist():
    """Prune the allowlist when files are removed/renamed."""
    for rel in ALLOWED_IDIOM_SITES:
        assert (REPO_ROOT / rel).is_file(), (
            f"Allowlist entry {rel} no longer exists — remove it from ALLOWED_IDIOM_SITES."
        )
