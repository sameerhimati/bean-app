"""The stats page's numbers — the value view an invoice gets read from.

Two failures are what these pin, and both are ways a chart lies quietly rather than loudly:

  * A DAY IS NOT A STRING SLICE. `received_at` is the RFC-2822 Date header kept verbatim, so
    "Mon, 03 Aug" and "Mon, 3 Aug" are the same day and differ at index 9. A `[:10]` truncation
    splits one day into two bars and nothing errors.
  * FILED MAIL OUTLIVES THE INBOX. `clear_filed` deletes filed lines, and "Bean filed 430 emails
    you never opened" is the strongest number on the page. Before this it silently reset to zero
    the first time she tidied her inbox.
"""

from __future__ import annotations

import json

from bean.corrections import Correction
from bean.inbox import clear_filed, load_filed_history
from bean.outcomes import daily_counts
from bean.server import _stats_summary


def _drafted(email_id: str, received_at: str, confidence: str = "green"):
    return {"id": email_id, "sender_email": "cust@x.com", "subject": "help", "body": "?",
            "received_at": received_at,
            "result": {"bucket": "Product questions", "confidence": confidence,
                       "draft": "hi", "citations": ["notebook:Product questions"], "why_unsure": []}}


def _filed(email_id: str, received_at: str):
    return {"id": email_id, "sender_email": "news@x.com", "subject": "sale", "body": "",
            "received_at": received_at,
            "result": {"disposition": "filed", "kind": "promo", "reason": "marketing blast"}}


def _write(path, records):
    path.write_text("".join(json.dumps(r, sort_keys=True) + "\n" for r in records), encoding="utf-8")


# ---- daily_counts --------------------------------------------------------------------------

def test_one_day_stays_one_bar_across_rfc2822_padding():
    """The bug a `[:10]` slice would ship: single- and double-digit day-of-month render the same
    date differently, and both forms appear in real Postmark payloads."""
    rows, undated = daily_counts([
        _drafted("a", "Mon, 3 Aug 2026 09:00:00 -0700"),
        _drafted("b", "Mon, 03 Aug 2026 14:30:00 -0700"),
        _filed("c", "Mon, 3 Aug 2026 18:00:00 -0700"),
    ])
    assert undated == 0
    assert rows == [{"day": "2026-08-03", "filed": 1, "drafted": 2}]


def test_days_sort_chronologically_not_lexically():
    rows, _ = daily_counts([
        _drafted("a", "Tue, 1 Sep 2026 09:00:00 -0700"),
        _drafted("b", "Mon, 31 Aug 2026 09:00:00 -0700"),
        _drafted("c", "Sat, 1 Aug 2026 09:00:00 -0700"),
    ])
    assert [r["day"] for r in rows] == ["2026-08-01", "2026-08-31", "2026-09-01"]


def test_undated_mail_is_counted_and_reported_never_guessed_a_day():
    """An unreadable date must not be dropped (the chart would understate) and must not be bucketed
    under a guessed day (the chart would invent traffic). It is returned separately so the page can
    say so."""
    rows, undated = daily_counts([
        _drafted("a", "Mon, 3 Aug 2026 09:00:00 -0700"),
        _drafted("b", ""),
        _drafted("c", "not a date at all"),
    ])
    assert undated == 2
    assert rows == [{"day": "2026-08-03", "filed": 0, "drafted": 1}]


# ---- filed history -------------------------------------------------------------------------

def test_clearing_filed_mail_preserves_the_count(tmp_path):
    """The whole point: after a clear, the inbox has lost the mail and the page has not lost the
    number."""
    inbox = tmp_path / "inbox.jsonl"
    history = tmp_path / "filed_history.jsonl"
    _write(inbox, [
        _drafted("d1", "Mon, 3 Aug 2026 09:00:00 -0700"),
        _filed("f1", "Mon, 3 Aug 2026 10:00:00 -0700"),
        _filed("f2", "Mon, 3 Aug 2026 11:00:00 -0700"),
        _filed("f3", "Tue, 4 Aug 2026 11:00:00 -0700"),
    ])
    removed, kept = clear_filed(inbox, backup_path=tmp_path / "bak", history_path=history)
    assert (removed, kept) == (3, 1)
    assert load_filed_history(history) == {"2026-08-03": 2, "2026-08-04": 1}


def test_repeated_clears_accumulate_rather_than_overwrite(tmp_path):
    inbox = tmp_path / "inbox.jsonl"
    history = tmp_path / "filed_history.jsonl"
    _write(inbox, [_filed("f1", "Mon, 3 Aug 2026 10:00:00 -0700")])
    clear_filed(inbox, backup_path=tmp_path / "bak1", history_path=history)
    _write(inbox, [_filed("f2", "Mon, 3 Aug 2026 12:00:00 -0700"),
                   _filed("f3", "Tue, 4 Aug 2026 12:00:00 -0700")])
    clear_filed(inbox, backup_path=tmp_path / "bak2", history_path=history)
    assert load_filed_history(history) == {"2026-08-03": 2, "2026-08-04": 1}


def test_a_corrupt_history_line_never_takes_down_the_page(tmp_path):
    """This log is decoration on a chart. It must degrade to "fewer bars", never to a 503."""
    history = tmp_path / "filed_history.jsonl"
    history.write_text('{"day": "2026-08-03", "filed": 2}\nnot json\n{"filed": 9}\n', encoding="utf-8")
    assert load_filed_history(history) == {"2026-08-03": 2}


# ---- the assembled payload -----------------------------------------------------------------

def test_summary_folds_archived_filed_into_the_totals_and_the_chart():
    inbox = [_drafted("d1", "Mon, 3 Aug 2026 09:00:00 -0700"),
             _filed("f1", "Mon, 3 Aug 2026 10:00:00 -0700")]
    out = _stats_summary(inbox, [], archived={"2026-08-03": 5, "2026-08-02": 2})
    assert out["totals"] == {"handled": 9, "drafted": 1, "filed": 8,
                             "archived_filed": 7, "archived_drafted": 0}
    assert out["daily"] == [
        {"day": "2026-08-02", "filed": 2, "drafted": 0},
        {"day": "2026-08-03", "filed": 6, "drafted": 1},
    ]


def test_summary_folds_archived_drafted_mail_back_into_the_chart():
    """The Clear sweep's twin of the above, and the bug it was written for.

    On 2026-08-21 the operator cleared three weeks of actioned mail. `clear_ids` deleted it from
    inbox.jsonl without rolling anything up, so the daily "drafted" bars — built from that file —
    dropped to ~0 for every cleared day and the page reported that Bean had drafted almost nothing.
    Clearing mail is allowed to cost the evidence; it must never cost the count."""
    inbox = [_drafted("d1", "Tue, 4 Aug 2026 09:00:00 -0700")]
    out = _stats_summary(inbox, [], archived={}, drafted_archive={"2026-08-03": 9, "2026-08-04": 2})
    assert out["totals"]["drafted"] == 12          # 1 still here + 11 swept
    assert out["totals"]["archived_drafted"] == 11
    assert out["daily"] == [
        {"day": "2026-08-03", "filed": 0, "drafted": 9},   # a day with nothing left in the inbox
        {"day": "2026-08-04", "filed": 0, "drafted": 3},   # swept + survivor, summed
    ]


def test_loop_is_windowed_by_date_not_by_what_is_still_in_the_inbox():
    """The roadmap's "window the approval rate to the engine currently running" — as a DATE CUT.

    It used to id-join corrections against inbox.jsonl, which meant clearing the inbox erased the
    measurement: on 2026-08-21 the operator cleared her actioned mail and `loop.graded` went to 0
    while she had graded 205 drafts. A date cut cannot be erased by tidying up. The pre-cutover row
    still shows in `loop_lifetime` rather than being averaged silently into the headline."""
    corrections = [
        Correction("cleared", "Product questions", "green", "approve", final_text="hi",
                   ts="2026-08-20T10:00:00+00:00"),      # this email is NOT in the inbox any more
        Correction("old", "Product questions", "green", "takeover", final_text="mine",
                   ts="2026-07-15T10:00:00+00:00"),      # the deleted tree engine graded this
    ]
    out = _stats_summary([], corrections, archived={})
    assert out["loop"]["graded"] == 1 and out["loop"]["approved_untouched"] == 1
    assert out["loop"]["rate"] == 1.0                     # survived the inbox being empty
    assert out["loop_since"] == "2026-08-03"
    assert out["loop_lifetime"]["graded"] == 2 and out["loop_lifetime"]["rewritten"] == 1


def test_undated_corrections_are_reported_not_counted_either_way():
    """Rows written before Correction grew a `ts`. Counting them would misstate the window and
    dropping them silently would hide that the window is incomplete, so they are surfaced."""
    corrections = [
        Correction("new", "Product questions", "green", "approve", final_text="hi",
                   ts="2026-08-20T10:00:00+00:00"),
        Correction("undated", "Product questions", "green", "approve", final_text="hi"),  # ts=""
    ]
    out = _stats_summary([], corrections, archived={})
    assert out["loop"]["graded"] == 1
    assert out["loop"]["undated"] == 1
    assert out["loop_lifetime"]["graded"] == 2


def test_summary_carries_no_cost_field_anywhere():
    """The page that justifies an invoice must never render what the invoice cost to produce. A
    money field arriving here is the failure — spend belongs on the owner-only /api/usage."""
    out = _stats_summary([_drafted("d1", "Mon, 3 Aug 2026 09:00:00 -0700")], [], archived={})
    blob = json.dumps(out).lower()
    for forbidden in ("cost", "token", "usd", "price", "dollar", "margin", "haiku", "sonnet", "opus"):
        assert forbidden not in blob, f"{forbidden!r} leaked into the operator's value view"


def test_empty_tenant_renders_rather_than_divides_by_zero():
    """A brand-new operator: no mail, no corrections. Every rate is 0.0, never 1.0 and never a
    ZeroDivisionError — the same honesty as Outcomes.drafted_fraction."""
    out = _stats_summary([], [], archived={})
    assert out["totals"]["handled"] == 0
    assert out["loop"]["graded"] == 0 and out["loop"]["rate"] == 0.0
    assert out["daily"] == [] and out["undated"] == 0


def test_days_are_bucketed_in_the_operators_zone_not_the_senders():
    """Mail arrives stamped with the SENDER's offset. 23:30 in Los Angeles is 01:30 the NEXT day in
    Chicago, so bucketing on the message's own offset would put it on the wrong bar of her chart —
    and bucketing on UTC would move everything after 7pm Central into tomorrow."""
    rows, _ = daily_counts([
        _drafted("late", "Mon, 3 Aug 2026 23:30:00 -0700"),   # → 2026-08-04 01:30 Central
        _drafted("early", "Tue, 4 Aug 2026 08:00:00 -0500"),  # → 2026-08-04 08:00 Central
    ])
    assert rows == [{"day": "2026-08-04", "filed": 0, "drafted": 2}]


def test_two_senders_in_different_zones_share_one_bar():
    """The same instant, stamped by a customer in London and one in Dallas. One moment, one day."""
    rows, _ = daily_counts([
        _drafted("london", "Mon, 3 Aug 2026 18:00:00 +0100"),  # 12:00 Central
        _drafted("dallas", "Mon, 3 Aug 2026 12:00:00 -0500"),  # 12:00 Central
    ])
    assert len(rows) == 1 and rows[0]["drafted"] == 2


def test_a_date_header_with_no_offset_does_not_crash():
    """Some senders omit the offset entirely. Treat it as already-local rather than raising —
    a malformed header must cost one bar's accuracy, never the page."""
    rows, undated = daily_counts([_drafted("naive", "3 Aug 2026 09:00:00")])
    assert undated == 0 and rows == [{"day": "2026-08-03", "filed": 0, "drafted": 1}]
