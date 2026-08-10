"""Few-shot learning: the correction log feeds past (email -> approved reply) pairs into the
DRAFT step, so Bean drafts in the operator's voice over time. All offline.

This file used to also pin WHERE the examples rode in the prompt (user message only, never the
cached system prefix). Those tests drove the routing tree's assess leg and went with it; the
notebook engine builds its own prompt (bean/engine.py) and is covered by tests/test_engine.py.
What remains is the selector itself — which pairs are usable, how they rank, and that they
survive a round-trip through the log on disk.
"""

from __future__ import annotations

from bean.corrections import Correction, few_shot_examples


def _approval(cat, reply, *, subject="prior subject", body="prior body", action="approve"):
    return Correction(
        email_id="x", category=cat, confidence="high", action=action,
        final_text=reply, meta={"email_subject": subject, "email_body": body},
    )


# ---- 1. the selector: same-category, most-recent K, only usable pairs -----------------------

def test_selector_returns_same_category_most_recent_k():
    log = [
        _approval("Order Status", "reply 1"),
        _approval("Order Status", "reply 2"),
        _approval("Order Status", "reply 3"),
        _approval("Order Status", "reply 4"),
    ]
    out = few_shot_examples(log, "Order Status", k=3)
    assert [e["reply"] for e in out] == ["reply 2", "reply 3", "reply 4"]  # last K, in order


def test_selector_excludes_other_categories():
    log = [_approval("Returns & Exchanges", "wrong"), _approval("Order Status", "right")]
    assert [e["reply"] for e in few_shot_examples(log, "Order Status")] == ["right"]


def test_selector_includes_teach_but_still_excludes_takeover():
    """The flashcard un-starve: a 'teach' row (a reply the operator authored for an untaught node)
    is a usable exemplar; a 'takeover' (they left Bean to answer elsewhere) is NOT — even if it
    somehow carries text — because a takeover is not a reply they vetted as Bean's."""
    log = [
        _approval("Order Status", "taught this", action="teach"),
        _approval("Order Status", "left the app", action="takeover"),
    ]
    assert [e["reply"] for e in few_shot_examples(log, "Order Status")] == ["taught this"]


def test_selector_needs_final_text_even_for_teach():
    log = [Correction(email_id="x", category="Order Status", confidence="flag", action="teach",
                      final_text=None, meta={"email_subject": "s", "email_body": "b"})]
    assert few_shot_examples(log, "Order Status") == []  # no reply text → nothing to learn


def test_selector_excludes_rows_without_a_reply_or_context():
    log = [
        _approval("Order Status", None, action="takeover"),       # no reply (FLAG takeover)
        _approval("Order Status", None, action="skip"),           # no reply (deferral)
        Correction(email_id="x", category="Order Status", confidence="high", action="approve",
                   final_text="reply but no context", meta={}),   # no email context in meta
        _approval("Order Status", "usable"),
    ]
    assert [e["reply"] for e in few_shot_examples(log, "Order Status")] == ["usable"]


def test_selector_includes_edits_and_truncates_body():
    log = [_approval("Order Status", "edited reply", body="x" * 1000, action="edit")]
    out = few_shot_examples(log, "Order Status", max_chars=400)
    assert out[0]["reply"] == "edited reply"
    assert len(out[0]["body"]) == 400


# ---- 1b. feedback weighting: liked / commented examples outrank plain approvals -------------

def test_liked_example_survives_as_a_kept_slot():
    # Oldest row, but the operator liked it — it must outrank newer plain approvals for the K slots.
    log = [
        _approval("Order Status", "liked one"),
        _approval("Order Status", "plain 1"),
        _approval("Order Status", "plain 2"),
        _approval("Order Status", "plain 3"),
    ]
    log[0].liked = True
    replies = [e["reply"] for e in few_shot_examples(log, "Order Status", k=2)]
    assert "liked one" in replies          # the win is kept despite being the oldest
    assert replies[-1] == "liked one"      # and sits in the most-salient (last) slot


def test_no_signal_path_is_unchanged_by_weighting():
    # With no like/comment, weighting is a no-op: still most-recent-K in chronological order.
    log = [_approval("Order Status", f"reply {i}") for i in range(1, 5)]
    assert [e["reply"] for e in few_shot_examples(log, "Order Status", k=3)] == ["reply 2", "reply 3", "reply 4"]


# ---- 6. disk round-trip: load() -> few-shot works end to end -------------------------------

def test_corrections_loaded_from_disk_feed_few_shot(tmp_path):
    from bean.corrections import load, record

    log_path = tmp_path / "corrections.jsonl"
    record(_approval("Order Status", "Disk-sourced reply.", subject="hello", body="world"),
           log_path=log_path)

    out = few_shot_examples(load(log_path), "Order Status")
    assert out and out[0]["reply"] == "Disk-sourced reply."
