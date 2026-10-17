"""#121734 residual: a re-inserted carrier must not keep naming the run it was merged from.

The alternation repair welds consecutive user rows into one live dict and records the
merged-away row ids in ``_absorbed_row_ids`` so the compaction commit can archive exactly
them. That record describes rows which existed *before* the weld — but once the weld itself is
flushed, the dict is its own durable row and the ids it names are archived by that very commit.

A reader that counts a carrier's run instead of consulting the active set then counts the same
user turn more than once. ``tui_gateway.methods_prompt._archived_user_turns`` is exactly such a
reader: for every id in ``{own, *absorbed}`` it fails closed and counts a turn when the id is not
in the durable rows, so a stale run makes one turn bill as several and trips the deep-truncate
confirmation gate on a cut that drops a single turn.
"""

from agent.conversation_compression_archive import ABSORBED_ROW_IDS
from hermes_state import SessionDB


def _repaired_carrier(absorbed: list[int]) -> dict:
    """The live dict ``repair_message_sequence`` leaves after welding a run of user rows."""
    return {"role": "user", "content": "u1\n\nu2", ABSORBED_ROW_IDS: list(absorbed)}


def test_reinserted_carrier_drops_the_run_it_was_merged_from(tmp_path):
    """The insert is the moment the run is spent; keeping the ids makes a reader double-count."""
    db = SessionDB(tmp_path / "state.db")
    try:
        db.create_session("sid", source="test")
        welded = db.append_message("sid", "user", "u1")
        merged_away = db.append_message("sid", "user", "u2")
        carrier = _repaired_carrier([merged_away])
        assert carrier[ABSORBED_ROW_IDS] == [merged_away]

        db._insert_message_rows(db._conn, "sid", [carrier])

        assert isinstance(carrier.get("_row_id"), int) and carrier["_row_id"] != welded, (
            "the weld must land as its own durable row, distinct from the rows it absorbed"
        )
        assert ABSORBED_ROW_IDS not in carrier, (
            f"the merged-away ids {carrier.get(ABSORBED_ROW_IDS)!r} name rows this commit "
            "archives; a reader counting the carrier's run counts them a second time"
        )
    finally:
        db.close()


def test_a_committed_carrier_counts_one_turn_not_the_whole_run(tmp_path):
    """The real path: the commit clones the carrier, so the stale run reaches a live reader.

    ``archive_and_compact`` archives the rows the compressor held and then re-inserts the
    compacted set, which is where a repaired carrier becomes a durable row of its own. That
    leaves ``_archived_user_turns`` counting one live user turn as three.
    """
    from tui_gateway import methods_prompt

    db = SessionDB(tmp_path / "state.db")
    try:
        db.create_session("sid", source="test")
        welded = db.append_message("sid", "user", "u1")
        merged_away = db.append_message("sid", "user", "u2")
        tail = db.append_message("sid", "assistant", "a1")
        carrier = _repaired_carrier([merged_away])
        carrier["_row_id"] = welded

        db.archive_and_compact(
            "sid", [{"role": "user", "content": "SUMMARY"}, carrier],
            watermark=tail, covered_ids=[welded, merged_away, tail], unresolved_held=[])

        active = db.get_messages_as_conversation("sid", include_row_ids=True)
        assert [row["content"] for row in active] == ["SUMMARY", "u1\n\nu2"], (
            "the commit must leave the summary and exactly one live copy of the welded turn"
        )
        # The live surface holds the dict the commit was handed, not a re-read of the row: that
        # dict is what a later prompt.submit counts, and it is the one carrying the stale run.
        live = [{"role": "user", "content": "SUMMARY"}, carrier]

        original = methods_prompt._load_durable_truncation_history
        methods_prompt._load_durable_truncation_history = lambda *a, **k: active
        try:
            # Cut at the carrier: the single live user turn it stands for is what gets dropped.
            assert methods_prompt._archived_user_turns({}, "sid", live, 1, set()) == 1, (
                "the carrier is one live user turn; the ids it was welded from are archived by "
                "this very commit and must not be counted again"
            )
        finally:
            methods_prompt._load_durable_truncation_history = original
    finally:
        db.close()


def test_an_unflushed_carrier_keeps_its_run(tmp_path):
    """Only the insert retires the run — a carrier still in flight must keep naming it."""
    db = SessionDB(tmp_path / "state.db")
    try:
        db.create_session("sid", source="test")
        merged_away = db.append_message("sid", "user", "u2")
        carrier = _repaired_carrier([merged_away])

        assert carrier[ABSORBED_ROW_IDS] == [merged_away], (
            "before the weld is persisted the merged-away row is still live and must stay named, "
            "or the commit cannot archive it and the summary loses it"
        )
    finally:
        db.close()