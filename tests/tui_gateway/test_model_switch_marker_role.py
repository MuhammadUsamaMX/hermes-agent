"""Tests for _append_model_switch_marker role fix (issue #48338).

The model switch marker must NOT use role="system" because strict providers
(vLLM, Qwen) reject system messages that appear mid-conversation. Using
role="user" is safe — the system prompt is prepended to the API message list,
so a user-role marker can appear at any later position, and the gateway's
sanitize/merge pass already coalesces consecutive user messages.
"""

from __future__ import annotations


from tui_gateway.server import _append_model_switch_marker


class TestAppendModelSwitchMarkerRole:
    """Verify the marker uses role='user', not role='system'."""

    def test_marker_uses_user_role(self) -> None:
        """The history entry must be role='user', not role='system'."""
        session: dict = {"session_key": "test-session", "history": []}
        _append_model_switch_marker(session, model="gpt-4o", provider="openai")
        assert len(session["history"]) == 1
        entry = session["history"][0]
        assert entry["role"] == "user", (
            f"Expected role='user' but got role='{entry['role']}'. "
            "Strict providers (vLLM, Qwen) reject mid-conversation system messages."
        )




class TestModelSwitchMarkerDedup:
    """#65891: only the newest marker is meaningful; older ones must not
    accumulate in the live history and burn context tokens every turn."""

    @staticmethod
    def _markers(session: dict) -> list:
        from tui_gateway.server import _is_model_switch_marker

        return [h for h in session["history"] if _is_model_switch_marker(h)]

    def test_second_switch_replaces_first_marker(self) -> None:
        session: dict = {"session_key": "s", "history": []}
        _append_model_switch_marker(session, model="model-a", provider="p")
        _append_model_switch_marker(session, model="model-b", provider="p")
        markers = self._markers(session)
        assert len(markers) == 1, "a second switch must replace, not stack, the marker"
        assert "model-b" in markers[0]["content"]
        assert "model-a" not in markers[0]["content"]
        # The surviving marker is the last history entry.
        assert session["history"][-1] is markers[0]


    def test_dedup_preserves_real_conversation_turns(self) -> None:
        session: dict = {
            "session_key": "s",
            "history": [
                {"role": "user", "content": "hello"},
                {"role": "assistant", "content": "hi"},
            ],
        }
        _append_model_switch_marker(session, model="model-a", provider="p")
        _append_model_switch_marker(session, model="model-b", provider="p")
        # Real turns untouched; exactly one marker, appended at the end.
        assert session["history"][0] == {"role": "user", "content": "hello"}
        assert session["history"][1] == {"role": "assistant", "content": "hi"}
        assert len(self._markers(session)) == 1
        assert len(session["history"]) == 3

    def test_prior_marker_between_turns_is_removed(self) -> None:
        # A stale marker not at the tail (a later turn followed it) is still
        # stripped on the next switch.
        session: dict = {
            "session_key": "s",
            "history": [
                {"role": "user", "content": "q1"},
                _make_marker_entry("model-a"),
                {"role": "assistant", "content": "a1"},
            ],
        }
        _append_model_switch_marker(session, model="model-b", provider="p")
        markers = self._markers(session)
        assert len(markers) == 1
        assert "model-b" in markers[0]["content"]
        # The real turns are preserved in order.
        assert [h["content"] for h in session["history"] if not _is_marker(h)] == ["q1", "a1"]

    def test_history_version_increments_once_on_replace(self) -> None:
        session: dict = {"session_key": "s", "history": [], "history_version": 0}
        _append_model_switch_marker(session, model="model-a", provider="p")
        _append_model_switch_marker(session, model="model-b", provider="p")
        assert session["history_version"] == 2  # one increment per switch


def _make_marker_entry(model: str) -> dict:
    from tui_gateway.server import _MODEL_SWITCH_MARKER_PREFIX

    return {"role": "user", "content": f"{_MODEL_SWITCH_MARKER_PREFIX}{model}.]"}


def _is_marker(entry: dict) -> bool:
    from tui_gateway.server import _is_model_switch_marker

    return _is_model_switch_marker(entry)


class TestModelSwitchMarkerIsStampedLikeAFlushedRow:
    """#121734: the marker is durable, so it must be stamped like any flushed row.

    The in-place compaction commit archives the rows the compressor held rather than every row
    up to the newest one, which it can only do when each held dict names its durable row. A
    marker that persisted a row but left the live entry unstamped reads as an unpersisted turn:
    the commit cannot name it and falls back to the watermark, archiving turns the compressor
    never saw. This is the invariant #121780 added the stamping for, and main had no direct
    test binding it.
    """

    @staticmethod
    def _session_with_db(db, session_key: str = "sess-marker") -> dict:
        class _Agent:
            def __init__(self, session_db):
                self._session_db = session_db
                self.session_id = session_key

        return {"session_key": session_key, "history": [], "agent": _Agent(db)}

    def test_entry_names_its_durable_row(self, tmp_path) -> None:
        """The entry carries both halves of the flushed-row stamp: row id and persisted marker."""
        from agent.context_compressor import _DB_PERSISTED_MARKER
        from hermes_state import SessionDB

        db = SessionDB(db_path=tmp_path / "state.db")
        try:
            db.create_session("sess-marker", source="cli")
            session = self._session_with_db(db)

            _append_model_switch_marker(session, model="gpt-4o", provider="openai")

            entry = _markers_of(session)[0]
            assert isinstance(entry.get("_row_id"), int) and entry["_row_id"] > 0, (
                f"the marker must name the durable row it wrote: {entry.get('_row_id')!r}"
            )
            assert entry.get(_DB_PERSISTED_MARKER) is True, (
                "without the persisted marker the commit reads the durable marker as an "
                "unpersisted turn and falls back to the watermark"
            )
            rows = db.get_messages_as_conversation("sess-marker", include_row_ids=True)
            assert [row.get("_row_id") for row in rows] == [entry["_row_id"]], (
                "the stamped id must be the row the marker actually persisted"
            )
            assert rows[0].get("display_kind") == "model_switch"
        finally:
            db.close()

    def test_stamped_marker_counts_as_exact_held_coverage(self, tmp_path) -> None:
        """The stamped id is what the commit uses to hold the marker exactly, not positionally."""
        from agent.context_compressor import _DB_PERSISTED_MARKER
        from agent.conversation_compression_archive import newest_exact_held_id
        from hermes_state import SessionDB

        db = SessionDB(db_path=tmp_path / "state.db")
        try:
            db.create_session("sess-marker", source="cli")
            session = self._session_with_db(db)
            _append_model_switch_marker(session, model="gpt-4o", provider="openai")
            entry = _markers_of(session)[0]
            # A real flushed row carries the marker too; the model-switch entry is no different.
            entry[_DB_PERSISTED_MARKER] = entry.get(_DB_PERSISTED_MARKER)

            assert newest_exact_held_id([entry]) == entry["_row_id"], (
                "the commit caps coverage by this id, so it must resolve to the marker's own row"
            )
        finally:
            db.close()

    def test_an_unstamped_marker_is_not_exact_coverage(self, tmp_path) -> None:
        """The stamp is load-bearing: without it the id is ignored, not merely advisory."""
        from agent.conversation_compression_archive import newest_exact_held_id

        entry = {"role": "user", "content": "marker", "_row_id": 7}
        assert newest_exact_held_id([entry]) is None, (
            "an unstamped row id must not be treated as held coverage"
        )


def _markers_of(session: dict) -> list:
    return [h for h in session["history"] if _is_marker(h)]
