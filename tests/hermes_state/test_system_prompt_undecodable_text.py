"""Invariant: one ``system_prompts`` row holding invalid UTF-8 cannot take down
every session list / load query (issue #109450).

Every session-read query materialises ``COALESCE(sp.prompt, s.system_prompt) AS
_system_prompt_resolved``. pysqlite decodes TEXT cells with the connection's
``text_factory``, which defaults to strict UTF-8, so a single truncated
multi-byte sequence (e.g. a 3650-byte prompt ending in the first two bytes of a
3-byte ``✓`` -- ``e2 9c``) aborted the whole ``fetchall`` with
``sqlite3.OperationalError: Could not decode to UTF-8 column
'_system_prompt_resolved'``, leaving the desktop session panel unable to list or
open *any* session.

State.db connections now decode lossily (U+FFFD), so one bad row degrades to a
replacement character instead of failing the query.
"""

from __future__ import annotations

import hashlib
import sqlite3
from pathlib import Path

import pytest

from hermes_state import SessionDB

# A well-formed prompt followed by the first two bytes of a 3-byte U+2713: a
# truncated multi-byte sequence, exactly what a process killed mid-write leaves
# behind in a TEXT cell.
_TRUNCATED_PROMPT_BYTES = b"You are Hermes Agent, be precise.\xe2\x9c"


_CORRUPT_SESSION_PROMPT = "Corrupt prompt here."
_GOOD_SESSION_PROMPT = "You are Hermes Agent."


def _seed_sessions(db_path: Path) -> None:
    """Create two sessions whose prompts are stored in ``system_prompts``.

    ``system_prompts`` is a content-addressed side table (one row per distinct
    prompt, joined by ``sessions.system_prompt_hash``), so the two sessions use
    distinct prompts and only one of the two rows is poisoned below.
    """
    db = SessionDB(db_path=db_path)
    try:
        db.create_session("good-session", source="cli", system_prompt=_GOOD_SESSION_PROMPT)
        db.create_session("corrupt-session", source="cli", system_prompt=_CORRUPT_SESSION_PROMPT)
    finally:
        db.close()


def _corrupt_prompt_row(db_path: Path, prompt: str) -> None:
    """Rewrite the stored ``prompt`` of the row holding *prompt* with invalid UTF-8 bytes.

    pysqlite re-encodes Python ``str`` params, so the raw bytes are forced
    through with ``CAST(x'..' AS TEXT)`` -- the same technique the kanban
    undecodable-text regression test uses. Only this session's row is touched,
    so the healthy sibling proves the blast radius stayed at one row.
    """
    prompt_hash = hashlib.sha256(prompt.encode("utf-8")).hexdigest()
    conn = sqlite3.connect(str(db_path))
    try:
        conn.execute("PRAGMA foreign_keys = OFF")
        conn.execute(
            "UPDATE system_prompts SET prompt = CAST(? AS TEXT) WHERE hash = ?",
            (sqlite3.Binary(_TRUNCATED_PROMPT_BYTES), prompt_hash),
        )
        conn.commit()
    finally:
        conn.close()


def _precondition_confirmed(db_path: Path) -> None:
    """Assert the corruption really makes a raw strict-decode read raise.

    Without this the regression test could silently stop exercising the bug if a
    future SQLite/pysqlite build decodes TEXT cells more leniently.
    """
    raw = sqlite3.connect(str(db_path))
    try:
        with pytest.raises(sqlite3.OperationalError, match="Could not decode to UTF-8"):
            raw.execute(
                "SELECT COALESCE(sp.prompt, s.system_prompt) AS _system_prompt_resolved "
                "FROM sessions s LEFT JOIN system_prompts sp ON sp.hash = s.system_prompt_hash"
            ).fetchall()
    finally:
        raw.close()


@pytest.fixture
def corrupt_prompt_db(tmp_path) -> Path:
    db_path = tmp_path / "state.db"
    _seed_sessions(db_path)
    _corrupt_prompt_row(db_path, _CORRUPT_SESSION_PROMPT)
    _precondition_confirmed(db_path)
    return db_path


def test_list_sessions_survives_undecodable_system_prompt(corrupt_prompt_db):
    """Listing sessions must return every session, with the corrupt prompt
    degraded to a replacement character rather than aborting the query."""
    db = SessionDB(db_path=corrupt_prompt_db)
    try:
        sessions = db.list_sessions_rich(include_hidden=True)
    finally:
        db.close()

    by_id = {s["id"]: s for s in sessions}
    assert {"good-session", "corrupt-session"} <= set(by_id), (
        "one corrupt system_prompts row must not hide the other sessions"
    )
    corrupt = by_id["corrupt-session"]["system_prompt"]
    assert isinstance(corrupt, str)
    assert "\ufffd" in corrupt, "undecodable bytes must degrade to U+FFFD"
    # Healthy sibling prompt stays intact -- only the bad row is affected.
    assert by_id["good-session"]["system_prompt"] == _GOOD_SESSION_PROMPT


def test_load_session_survives_undecodable_system_prompt(corrupt_prompt_db):
    """Loading a single session resolves its prompt lossily instead of raising."""
    db = SessionDB(db_path=corrupt_prompt_db)
    try:
        session = db.get_session("corrupt-session")
        assert session is not None
        assert isinstance(session["system_prompt"], str)
    finally:
        db.close()