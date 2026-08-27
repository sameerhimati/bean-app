"""bean/notebook_history.py — the audit trail for the operator's brain.

Pure diff plus an append-only log. The property that matters is that a REPLACEMENT reads as one
`changed` row carrying both texts, not as a remove and an add that lose the connection between
them — because "what did that replace?" is the question this log exists to answer.
"""

from __future__ import annotations

import json

from bean.notebook import Bucket, Fact, Macro, Note, Notebook
from bean.notebook_history import ACTIONS, Change, diff, load, record


def nb(**kw) -> Notebook:
    base = dict(
        store="A store",
        buckets=[Bucket("Returns", "within 90 days I send a replacement")],
        macros=[Macro("Shipping", "We ship in 2 business days.")],
        facts=[Fact("Headphone returns within 30 days.", "stated"),
               Fact("Free shipping over $75.", "stated")],
        notes=[Note("She waives restocking for repeat customers.", "observed")],
    )
    base.update(kw)
    return Notebook(**base)


def only(changes, action=None, section=None):
    return [c for c in changes
            if (action is None or c.action == action) and (section is None or c.section == section)]


# --- the shape of a change ----------------------------------------------------------------------

def test_an_identical_notebook_produces_no_rows():
    """A save that changes nothing must leave no trace — otherwise the log fills with noise from
    opening the editor and pressing save."""
    assert diff(nb(), nb()) == []


def test_a_replaced_fact_is_ONE_changed_row_carrying_both_texts():
    after = nb(facts=[Fact("Headphone returns within 60 days.", "stated"),
                      Fact("Free shipping over $75.", "stated")])
    changes = diff(nb(), after)
    assert len(changes) == 1
    c = changes[0]
    assert c.action == "changed"
    assert c.section == "Store facts"
    assert c.before == "Headphone returns within 30 days."
    assert c.after == "Headphone returns within 60 days."


def test_an_added_fact_is_an_addition_with_no_before():
    after = nb(facts=nb().facts + [Fact("Gift wrap is $4.", "stated")])
    (c,) = diff(nb(), after)
    assert (c.action, c.before, c.after) == ("added", "", "Gift wrap is $4.")


def test_a_removed_fact_is_a_removal_with_no_after():
    (c,) = diff(nb(), nb(facts=[Fact("Free shipping over $75.", "stated")]))
    assert (c.action, c.before, c.after) == ("removed", "Headphone returns within 30 days.", "")


def test_untouched_rows_never_appear():
    after = nb(facts=[Fact("Headphone returns within 60 days.", "stated"),
                      Fact("Free shipping over $75.", "stated")])
    assert not [c for c in diff(nb(), after) if "Free shipping" in (c.before + c.after)]


# --- named vs unnamed sections ------------------------------------------------------------------

def test_a_reworded_bucket_cliff_is_a_change_keyed_by_its_name():
    # Buckets are NAMED, so the name holds the two texts together across an edit.
    after = nb(buckets=[Bucket("Returns", "within 60 days I send a replacement")])
    (c,) = diff(nb(), after)
    assert c.action == "changed" and c.label == "Returns"
    assert c.before.endswith("90 days I send a replacement")
    assert c.after.endswith("60 days I send a replacement")


def test_a_renamed_bucket_reads_as_a_removal_plus_an_addition():
    # There is no honest way to call this "the same row" — the only handle was the name.
    changes = diff(nb(), nb(buckets=[Bucket("Refunds", "within 90 days I send a replacement")]))
    assert {c.action for c in changes} == {"added", "removed"}
    assert {c.label for c in changes} == {"Returns", "Refunds"}


def test_a_macro_is_matched_on_its_name_and_diffed_on_its_text():
    after = nb(macros=[Macro("Shipping", "We ship in 1 business day.")])
    (c,) = diff(nb(), after)
    assert c.section == "My standard answers" and c.label == "Shipping"
    assert c.action == "changed"


def test_notes_are_diffed_like_facts():
    (c,) = diff(nb(), nb(notes=[Note("She never waives restocking.", "observed")]))
    assert c.section == "Judgment notes" and c.action == "changed"


# --- provenance ----------------------------------------------------------------------------------

def test_the_source_is_carried_onto_every_row():
    after = nb(facts=nb().facts + [Fact("Gift wrap is $4.", "stated")])
    assert all(c.source == "chat" for c in diff(nb(), after, source="chat"))
    assert all(c.source == "editor" for c in diff(nb(), after))


def test_every_row_is_stamped_and_uses_a_known_action():
    after = nb(facts=nb().facts + [Fact("Gift wrap is $4.", "stated")])
    for c in diff(nb(), after, ts="2026-08-15T00:00:00Z"):
        assert c.ts == "2026-08-15T00:00:00Z"
        assert c.action in ACTIONS


# --- several changes at once ----------------------------------------------------------------------

def test_a_multi_section_save_logs_each_line_once():
    after = nb(
        buckets=[Bucket("Returns", "within 60 days I send a replacement")],
        facts=[Fact("Headphone returns within 30 days.", "stated")],           # dropped one
        notes=nb().notes + [Note("She refunds VIPs on sight.", "observed")],   # added one
    )
    changes = diff(nb(), after)
    assert len(changes) == 3
    assert len(only(changes, "changed", "How I route")) == 1
    assert len(only(changes, "removed", "Store facts")) == 1
    assert len(only(changes, "added", "Judgment notes")) == 1


def test_duplicate_lines_do_not_churn():
    dup = [Fact("Same line.", "stated"), Fact("Same line.", "stated")]
    assert diff(nb(facts=dup), nb(facts=dup)) == []
    # Dropping one of a duplicated pair is exactly one removal.
    (c,) = diff(nb(facts=dup), nb(facts=[Fact("Same line.", "stated")]))
    assert c.action == "removed"


# --- the log --------------------------------------------------------------------------------------

def test_record_appends_and_load_reads_back_in_order(tmp_path):
    p = tmp_path / "notebook_history.jsonl"
    assert record(diff(nb(), nb(facts=[Fact("A.", "stated"), Fact("Free shipping over $75.", "stated")])), log_path=p) == 1
    assert record(diff(nb(), nb(facts=nb().facts + [Fact("B.", "stated")]), source="chat"), log_path=p) == 1

    rows = load(p)
    assert [r.action for r in rows] == ["changed", "added"]
    assert [r.source for r in rows] == ["editor", "chat"]


def test_an_empty_diff_writes_no_file_at_all(tmp_path):
    p = tmp_path / "notebook_history.jsonl"
    assert record([], log_path=p) == 0
    assert not p.exists()


def test_a_missing_log_reads_as_empty(tmp_path):
    assert load(tmp_path / "nope.jsonl") == []


def test_rows_are_written_with_sorted_keys(tmp_path):
    p = tmp_path / "h.jsonl"
    record([Change("t", "Store facts", "added", "", "", "x", "chat")], log_path=p)
    row = json.loads(p.read_text(encoding="utf-8").splitlines()[0])
    assert list(row) == sorted(row)


def test_unknown_fields_are_dropped_rather_than_fatal(tmp_path):
    """A field added later must not make an older image unreadable — the lesson corrections.py
    records after hand-listed field names made every previous image un-rollbackable."""
    p = tmp_path / "h.jsonl"
    p.write_text(json.dumps({"ts": "t", "action": "added", "after": "x", "from_the_future": 1}) + "\n",
                 encoding="utf-8")
    (row,) = load(p)
    assert row.action == "added" and row.after == "x"


def test_one_corrupt_line_does_not_hide_the_rest(tmp_path):
    p = tmp_path / "h.jsonl"
    p.write_text('{"ts":"a","action":"added"}\nnot json at all\n{"ts":"b","action":"removed"}\n',
                 encoding="utf-8")
    assert [r.action for r in load(p)] == ["added", "removed"]
