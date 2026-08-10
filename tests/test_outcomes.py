"""`bean/outcomes.py` — the read surface that answers "is Bean USEFUL?".

Bean flagged 100% of the operator's real mail for weeks and every check stayed green, because every check
measured configuration and honesty and none measured outcomes. These tests pin the number that would
have caught it, and the three ways it could have lied: filed noise inflating the denominator, an
empty denominator scoring 1.0, and an alarm too twitchy to be believed.
"""

from __future__ import annotations

import json

from bean.corrections import Correction, edit_ratio, outcome_of, record
from bean.outcomes import (
    MIN_STALL_SAMPLE,
    RECENT_WINDOW,
    aggregate,
    approval_rates,
    drafting_stalled,
    recent_outcomes,
    render_table,
    report,
)


def _walked(email_id: str, confidence: str, *, path=("Sofas & Upholstery", "Which fabric grade"),
            missing_template=False, node_type=None):
    """One inbox line for an email that reached the engine — the stored-result shape
    bean/server.py writes (bean/inbox.py). Only the fields outcomes reads are filled in."""
    return {
        "id": email_id, "subject": f"subject {email_id}", "sender_name": "Sam",
        "sender_email": "sam@x.com", "body": "a real question", "received_at": "",
        "result": {
            "confidence": confidence,
            "chunks": [{"path": list(path), "node_type": node_type or ("escalate" if confidence == "flag" else "answer"),
                        "missing_template": missing_template}],
        },
    }


def _filed(email_id: str, kind: str = "newsletter"):
    """A gate-filed line: no `chunks`, because it never reached the tree."""
    return {"id": email_id, "subject": "50% OFF", "sender_name": "Spam", "sender_email": "s@x.com",
            "body": "", "received_at": "",
            "result": {"disposition": "filed", "kind": kind, "reason": "promo"}}


def _draft(email_id: str, grounding: str):
    """One inbox line for an email the NOTEBOOK engine drafted — the `draft_dict(DraftResult)` shape:
    a bucket + groundedness (green/yellow/red), NO `chunks`. This is what the M1 cutover stores."""
    return {"id": email_id, "subject": f"subject {email_id}", "sender_name": "Sam",
            "sender_email": "sam@x.com", "body": "a real question", "received_at": "",
            "result": {"bucket": "Product questions", "confidence": grounding,
                       "draft": "hi" if grounding != "red" else None,
                       "citations": ["notebook:Product questions"] if grounding == "green" else [],
                       "why_unsure": [] if grounding == "green" else ["unsure"]}}


def test_notebook_drafts_count_as_walked_with_grounding_mapped():
    # The M1 discriminator fix: a DraftResult has no `chunks` but IS a sample of Bean's judgment.
    # green→one-tap, yellow→check, red→her job — the same three affordances as the tree's H/L/F.
    o = aggregate([_draft("a", "green"), _draft("b", "yellow"), _draft("c", "red"), _filed("n1")])
    assert o.walked == 3 and o.filed == 1
    assert o.high == 1 and o.low == 1 and o.flag == 1
    assert o.drafted == 2  # green + yellow are approvable; red is her job


def test_approval_rates_is_untouched_share_of_drafts_acted_on():
    # The north-star instrument. Non-draft gestures (skip) are excluded from the denominator.
    cs = [Correction("a", "Q", "green", "approve"),
          Correction("b", "Q", "green", "approve"),
          Correction("c", "Q", "yellow", "edit", edit_ratio=0.2),
          Correction("d", "Q", "red", "takeover"),
          Correction("e", "Q", "green", "skip")]  # snooze: not a draft verdict
    ar = approval_rates(cs)
    assert ar.total == 4  # the skip is excluded
    assert ar.counts == {"approved_untouched": 2, "approved_edited": 1, "rewritten": 1}
    assert ar.rate == 0.5  # 2 untouched / 4 acted-on
    assert ar.mean_edit_ratio == 0.2


def test_approval_rate_empty_is_zero_not_one():
    # An agent nobody has approved is UNMEASURED, not perfect — the same honesty as drafted_fraction.
    assert approval_rates([]).rate == 0.0


def test_outcome_of_maps_actions_and_ignores_non_draft_gestures():
    assert outcome_of(Correction("e", "Q", "green", "approve")) == "approved_untouched"
    assert outcome_of(Correction("e", "Q", "green", "edit")) == "approved_edited"
    assert outcome_of(Correction("e", "Q", "green", "takeover")) == "rewritten"
    assert outcome_of(Correction("e", "Q", "green", "teach")) == "escalated"
    assert outcome_of(Correction("e", "Q", "green", "skip")) is None


def test_record_stamps_edit_ratio_on_edits(tmp_path):
    # record() must persist the numeric magnitude, not only the cosmetic/substantive label.
    log = tmp_path / "corrections.jsonl"
    c = record(Correction("e", "Q", "green", "edit", original_draft="Yes, within 30 days.",
                          final_text="Yes, you can return it within 30 days of delivery."), log_path=log)
    assert c.edit_ratio is not None and 0.0 < c.edit_ratio < 1.0
    assert edit_ratio("same text", "same text") == 0.0


# ---- the denominator ---------------------------------------------------------------------------

def test_gate_filed_mail_is_never_counted_as_an_outcome():
    """46% of everything Bean receives is machine noise (USPS/Amazon/DHL). Counting it would make a
    store whose inbox is half carrier notices look half 'handled' while drafting nothing — the exact
    flattering-denominator trick this module exists to prevent."""
    inbox = [_filed("n1"), _filed("n2"), _filed("n3"), _walked("e1", "flag")]
    o = aggregate(inbox)
    assert o.filed == 3
    assert o.walked == 1 and o.flag == 1
    assert o.drafted_fraction == 0.0


def test_an_empty_denominator_scores_zero_not_one():
    """The bug that hid all of this, in its general form. An agent that has drafted nothing because
    it has seen nothing is not a 100% success — it is unmeasured, and the honest score is 0."""
    o = aggregate([])
    assert o.walked == 0
    assert o.drafted_fraction == 0.0
    assert drafting_stalled(o) is False  # …but silence is NOT a stall: nothing arrived to flag


def test_an_unrecognized_confidence_is_never_counted_as_drafted():
    """A corrupt/legacy result must not be able to inflate the usefulness number. It lands in the
    denominator and in `unknown`, never in `drafted`."""
    o = aggregate([_walked("e1", "high"), _walked("e2", "???")])
    assert o.walked == 2 and o.unknown == 1 and o.drafted == 1
    assert o.drafted_fraction == 0.5


def test_verdicts_split_high_low_flag_and_drafted_is_high_plus_low():
    o = aggregate([_walked("a", "high"), _walked("b", "low"), _walked("c", "flag"),
                   _walked("d", "flag")])
    assert (o.high, o.low, o.flag) == (1, 1, 2)
    assert o.drafted == 2 and o.drafted_fraction == 0.5


# ---- the alarm ---------------------------------------------------------------------------------

def test_drafting_stalled_fires_when_mail_arrives_and_bean_drafts_none():
    """Prod, reproduced: real customer mail walking the tree, zero drafts. THE alarm."""
    o = aggregate([_walked(f"e{i}", "flag") for i in range(10)])
    assert o.walked == 10 and o.drafted == 0
    assert drafting_stalled(o) is True


def test_one_honest_flag_does_not_trip_the_alarm():
    """The operator's own goldens expect a FLAG on ~40% of mail, so a short all-flag run is ordinary. An
    alarm that fires on n=1 is an alarm that gets ignored — which is how the original blindness
    survived. Below MIN_STALL_SAMPLE it stays quiet; the fraction is still reported."""
    o = aggregate([_walked(f"e{i}", "flag") for i in range(MIN_STALL_SAMPLE - 1)])
    assert o.drafted_fraction == 0.0  # the number is honest…
    assert drafting_stalled(o) is False  # …the alarm is not twitchy


def test_a_single_draft_in_the_window_clears_the_alarm():
    """`drafting_stalled` is deliberately not 'the fraction is low' — a low fraction is a judgment
    call, and judgment calls get argued with. ZERO drafts is not a judgment call."""
    o = aggregate([_walked(f"e{i}", "flag") for i in range(19)] + [_walked("ok", "high")])
    assert drafting_stalled(o) is False
    assert o.drafted_fraction == 0.05


def test_the_window_counts_walked_mail_not_inbox_lines():
    """A burst of filed carrier noise must not push every real customer email out of the window and
    leave /healthz judging Bean on a sample of zero. 100 USPS notices + 6 flagged emails = a stall,
    not a clean bill of health."""
    inbox = [_walked(f"old{i}", "high") for i in range(3)]
    inbox += [_walked(f"e{i}", "flag") for i in range(6)]
    inbox += [_filed(f"n{i}") for i in range(100)]

    o = recent_outcomes(inbox, window=6)
    assert o.walked == 6 and o.filed == 0  # the six most recent WALKED emails, noise ignored
    assert drafting_stalled(o) is True

    # the older HIGHs are outside the window — the alarm is about now, not about a lifetime average
    assert recent_outcomes(inbox, window=RECENT_WINDOW).drafted == 3


# ---- the CLI surface ---------------------------------------------------------------------------

def test_report_reads_the_real_logs_and_renders_the_stall(tmp_path):
    inbox_path = tmp_path / "inbox.jsonl"
    inbox_path.write_text("\n".join(
        json.dumps(_walked(f"e{i}", "flag")) for i in range(6)
    ) + "\n", encoding="utf-8")
    o = report(inbox=inbox_path)
    assert o.walked == 6 and o.drafted == 0
    table = render_table(o)
    assert "DRAFTING STALLED" in table and "6 customer emails" in table


def test_report_on_absent_logs_is_empty_not_a_crash(tmp_path):
    o = report(inbox=tmp_path / "nope.jsonl")
    assert o.walked == 0
    assert "no customer mail has reached the engine" in render_table(o)
