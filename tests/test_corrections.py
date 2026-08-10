"""Unit tests for the edit classifier + correction log (offline)."""

from __future__ import annotations

from bean.corrections import Correction, classify_edit, load, record, substantive_only

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
