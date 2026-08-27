"""Offline tests for bean/conversation.py — grouping a customer who writes more than once.

Pure functions over already-loaded logs. No I/O, no model.
"""

from __future__ import annotations

from bean.conversation import (
    Conversation,
    build,
    conversation_key,
    group,
    open_siblings,
    render_for_prompt,
    store_spoke_last,
)


def item(id_, subject, *, sender="paige@example.com", body="", thread=None, result=None, received=""):
    return {
        "id": id_,
        "sender_email": sender,
        "sender_name": "Paige Edmonds",
        "subject": subject,
        "body": body,
        "received_at": received,
        "thread": thread or [],
        "result": result if result is not None else {"confidence": "yellow"},
    }


FILED = {"disposition": "filed", "kind": "newsletter"}


# --- keying -------------------------------------------------------------------------------------

def test_reply_prefixes_do_not_start_a_new_conversation():
    a = conversation_key("paige@example.com", "Order NW-1029")
    for subject in ("Re: Order NW-1029", "RE: [EXTERNAL] Re: Fwd: Order NW-1029", "  fwd: Order NW-1029 "):
        assert conversation_key("paige@example.com", subject) == a


def test_the_same_subject_from_a_different_person_is_a_different_conversation():
    # thread_key alone over-groups identical subjects across senders; the address is what closes it.
    assert conversation_key("paige@example.com", "Where is my order?") != conversation_key(
        "sam@example.com", "Where is my order?"
    )


def test_the_address_is_matched_case_insensitively():
    assert conversation_key("Paige@Example.COM", "Hi") == conversation_key("paige@example.com", "Hi")


# --- shapes -------------------------------------------------------------------------------------

def test_a_pile_up_is_everything_unanswered():
    conv = build([item("a", "Where is my order?"), item("b", "Re: Where is my order?")], {})
    assert conv.pile_up
    assert [i["id"] for i in conv.unanswered] == ["a", "b"]
    assert not conv.answered_before


def test_one_message_is_not_a_pile_up():
    conv = build([item("a", "Where is my order?")], {})
    assert not conv.pile_up
    assert len(conv.unanswered) == 1


def test_a_reply_she_sent_ends_the_pile_up():
    conv = build(
        [item("a", "Where is my order?"), item("b", "Re: Where is my order?")],
        {"a": "approved"},
    )
    assert conv.answered_before and not conv.pile_up
    assert [i["id"] for i in conv.unanswered] == ["b"]  # only what came after the reply


def test_handled_counts_as_answered_but_skipped_does_not():
    assert build([item("a", "s"), item("b", "Re: s")], {"a": "handled"}).answered_before
    # Snoozed is not answered — the customer is still waiting, which is the whole point.
    assert not build([item("a", "s"), item("b", "Re: s")], {"a": "skipped"}).answered_before


def test_a_store_reply_in_the_quoted_history_marks_a_back_and_forth():
    quoted = ["On Fri, Aug 14, 2026 at 4:45 PM Support <hello@northwind.example> wrote:\n> On it.\n"]
    latest = item("b", "Re: Where is my order?", thread=quoted)
    assert store_spoke_last(latest)
    conv = build([item("a", "Where is my order?"), latest], {})
    assert conv.answered_before and not conv.pile_up


def test_unknown_attribution_does_not_count_as_a_store_reply():
    # split_quoted leaves `side` UNKNOWN when the delimiter carries no address. Reading that as
    # "she replied" would silently downgrade a real pile-up to a routine follow-up.
    quoted = ["On Fri, Aug 14, 2026 at 4:45 PM Somebody wrote:\n> Any update?\n"]
    assert not store_spoke_last(item("b", "Re: s", thread=quoted))
    assert build([item("a", "s"), item("b", "Re: s", thread=quoted)], {}).pile_up


def test_filed_mail_joins_the_conversation_but_is_never_unanswered():
    conv = build([item("a", "Order shipped", result=FILED), item("b", "Re: Order shipped")], {})
    assert len(conv.items) == 2
    assert [i["id"] for i in conv.unanswered] == ["b"]
    assert not conv.pile_up


def test_a_conversation_of_only_filed_mail_owes_nothing():
    conv = build([item(str(n), "Your order shipped", result=FILED) for n in range(19)], {})
    assert conv.unanswered == [] and not conv.pile_up


# --- grouping -----------------------------------------------------------------------------------

def test_group_splits_by_sender_and_subject():
    inbox = [
        item("a", "Where is my order?"),
        item("b", "Re: Where is my order?"),
        item("c", "Different question"),
        item("d", "Where is my order?", sender="sam@example.com"),
    ]
    by_key = {c.key: c for c in group(inbox, {})}
    assert len(by_key) == 3
    sizes = sorted(len(c.items) for c in by_key.values())
    assert sizes == [1, 1, 2]


def test_group_keeps_arrival_order_within_a_conversation():
    conv = group([item("a", "s"), item("c", "Re: s"), item("b", "RE: s")], {})[0]
    assert [i["id"] for i in conv.items] == ["a", "c", "b"]


# --- open_siblings ------------------------------------------------------------------------------

def test_open_siblings_finds_the_unanswered_earlier_mail():
    inbox = [item("a", "Where is my order?"), item("z", "Unrelated")]
    assert [i["id"] for i in open_siblings(item("b", "Re: Where is my order?"), inbox, {})] == ["a"]


def test_open_siblings_skips_resolved_filed_and_the_email_itself():
    me = item("b", "Re: s")
    inbox = [
        item("a", "s"),
        item("done", "Re: s"),
        item("filed", "Re: s", result=FILED),
        me,  # a Postmark retry replays a delivered webhook; never its own predecessor
    ]
    got = [i["id"] for i in open_siblings(me, inbox, {"done": "approved"})]
    assert got == ["a"]


# --- prompt block -------------------------------------------------------------------------------

def test_a_lone_email_adds_nothing_to_the_prompt():
    # Byte-identical to the pre-grouping prompt: the common case stays a cache-warm no-op.
    assert render_for_prompt(build([item("a", "s", body="hi")], {})) == ""


def test_a_pile_up_tells_the_model_to_answer_all_of_it():
    conv = build(
        [item("a", "s", body="Where is my order?"), item("b", "Re: s", body="Still nothing?")], {}
    )
    block = render_for_prompt(conv)
    assert "Where is my order?" in block  # the earlier BODY, not just its subject
    assert "NOBODY HAS REPLIED" in block
    assert "ONE reply that answers all of them" in block


def test_a_back_and_forth_tells_the_model_to_answer_only_what_is_new():
    conv = build(
        [item("a", "s", body="Where is my order?"), item("b", "Re: s", body="Thanks — one more thing")],
        {"a": "approved"},
    )
    # Nothing outstanding before the newest message, so there is nothing extra to answer.
    assert render_for_prompt(conv) == ""


def test_a_back_and_forth_with_a_pile_up_after_it_answers_only_the_tail():
    conv = build(
        [
            item("a", "s", body="First question"),
            item("b", "Re: s", body="Second question"),
            item("c", "Re: s", body="Third question"),
        ],
        {"a": "approved"},
    )
    block = render_for_prompt(conv)
    assert "Second question" in block
    assert "First question" not in block  # she already answered that one
    assert "Answer ONLY what is new" in block


def test_the_prompt_block_is_bounded():
    many = [item(str(n), "s", body="x" * 5000) for n in range(12)]
    block = render_for_prompt(build(many, {}))
    assert block.count("--- ") == 5  # _MAX_IN_PROMPT
    assert "…" in block  # each body truncated
    assert len(block) < 5000


def test_conversation_is_a_value_with_sane_empties():
    empty = Conversation(key="k")
    assert empty.items == [] and empty.unanswered == [] and empty.latest is None
    assert not empty.pile_up
