"""Unit tests for the edit classifier + correction log (offline)."""

from __future__ import annotations

from datetime import datetime

import pytest

from bean.corrections import (
    Correction,
    CorrectionsCorruptError,
    classify_edit,
    load,
    record,
    substantive_only,
)

DRAFT = "Hi Priya, happy to help! Your size 28 exchange is free and in stock.\n\nWarmly,\nNora"


def test_whitespace_and_signature_only_is_cosmetic():
    edited = "Hi Priya, happy to help! Your size 28 exchange is free and in stock.\n\nBest,\nNora\n\n"
    assert classify_edit(DRAFT, edited) == "cosmetic"


def test_greeting_swap_is_cosmetic():
    edited = "Hello Priya, happy to help! Your size 28 exchange is free and in stock.\n\nWarmly,\nNora"
    assert classify_edit(DRAFT, edited) == "cosmetic"


def test_changed_fact_is_substantive():
    edited = "Hi Priya, happy to help! Your size 28 exchange is $12 and out of stock.\n\nWarmly,\nNora"
    assert classify_edit(DRAFT, edited) == "substantive"


def test_changed_commitment_is_substantive():
    edited = "Hi Priya, unfortunately we cannot exchange that item.\n\nWarmly,\nNora"
    assert classify_edit(DRAFT, edited) == "substantive"


def test_record_tags_edit_and_filters_cosmetic(tmp_path):
    log = tmp_path / "corrections.jsonl"
    record(Correction("mm-1021", "Returns & Exchanges", "high", "edit",
                       original_draft=DRAFT, final_text=DRAFT.replace("Warmly", "Best")), log_path=log)
    record(Correction("mm-0998", "Returns & Exchanges", "low", "edit",
                       original_draft=DRAFT, final_text="Hi Greg, we cannot take worn boots back."), log_path=log)

    rows = load(log)
    assert [r.edit_kind for r in rows] == ["cosmetic", "substantive"]
    # Only the substantive edit feeds the calibration metric.
    assert [r.email_id for r in substantive_only(rows)] == ["mm-0998"]


# ---- timestamps + forward compatibility ------------------------------------------------------

def test_record_stamps_a_timestamp(tmp_path):
    """Every other log on the volume carried one and this one did not, so there was no learning
    RATE — no way to see the operator teaching Bean less this week than last, which is the earliest
    signal the loop has died."""
    p = tmp_path / "corrections.jsonl"
    record(Correction("e1", "Order Status", "green", "approve", final_text="ok"), log_path=p)
    row = load(p)[0]
    assert row.ts
    assert datetime.fromisoformat(row.ts).tzinfo is not None  # UTC-aware, sortable


def test_record_keeps_a_caller_supplied_timestamp(tmp_path):
    """A backfill or a replay must be able to write a row with its REAL time, not the time it was
    re-imported — otherwise re-importing history rewrites when it happened."""
    p = tmp_path / "corrections.jsonl"
    record(Correction("e1", "Order Status", "green", "approve", final_text="ok",
                      ts="2026-07-09T12:00:00+00:00"), log_path=p)
    assert load(p)[0].ts == "2026-07-09T12:00:00+00:00"


def test_rows_written_before_the_field_existed_still_load(tmp_path):
    """The 128 rows already on the volume have no `ts`. They must read as unknown ("") and never
    as the epoch — a chart that plants them all on 1970-01-01 is worse than one that says where its
    history starts."""
    p = tmp_path / "corrections.jsonl"
    p.write_text('{"email_id": "old", "category": "Order Status", "confidence": "green",'
                 ' "action": "approve", "final_text": "ok"}\n', encoding="utf-8")
    row = load(p)[0]
    assert row.ts == "" and row.email_id == "old"


def test_a_field_this_build_has_never_heard_of_is_dropped_not_fatal(tmp_path):
    """The deploy-rollback hazard, closed. `load` used to splat the whole row into the dataclass, so
    a row carrying a newer build's field raised → 503 on /api/learning, /api/corrections, /api/stats
    and every draft. That made adding ANY field silently un-rollbackable: deploy, take one
    correction, roll back, and Bean refuses to read the operator's brain."""
    p = tmp_path / "corrections.jsonl"
    p.write_text('{"email_id": "e1", "category": "Order Status", "confidence": "green",'
                 ' "action": "approve", "final_text": "ok", "some_future_field": {"a": 1}}\n',
                 encoding="utf-8")
    rows = load(p)
    assert len(rows) == 1 and rows[0].email_id == "e1"  # lossy, not fatal


def test_a_genuinely_corrupt_line_still_fails_loudly(tmp_path):
    """The tolerance above must not soften the failure that matters. Silence on an unreadable log is
    indistinguishable from having learned nothing, which is the whole failure this project exists
    to kill."""
    p = tmp_path / "corrections.jsonl"
    p.write_text('{"email_id": "e1", "category": "C", "confidence": "green", "action": "approve"}\n'
                 'not json at all\n', encoding="utf-8")
    with pytest.raises(CorrectionsCorruptError):
        load(p)
