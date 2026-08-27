"""bean/chat.py — the two shapes a turn can take, and the guards on the one that writes.

Offline and pure: no I/O, no live model. The load-bearing tests are the coercions, because this is
the only path where a model's self-report can end up written into the operator's brain.
"""

from __future__ import annotations

from bean.chat import (
    ABOUT_MAX_CHARS,
    CHAT_TOOL,
    MAX_TURNS,
    SECTIONS,
    ChatReply,
    _coerce,
    _resolve_supersedes,
    _user_message,
    load_about,
    reply,
)
from bean.llm import FakeModel
from bean.notebook import Bucket, Fact, Macro, Note, Notebook

NB = Notebook(
    store="A store",
    buckets=[Bucket("Returns", "within 90 days I send a replacement; older I frame lifespan")],
    macros=[Macro("Shipping", "We ship in 2 business days.")],
    facts=[Fact("Headphone returns within 30 days, receipt required.", "stated"),
           Fact("Free shipping over $75.", "stated")],
    notes=[Note("She waives restocking for repeat customers.", "observed")],
)


# --- answers ------------------------------------------------------------------------------------

def test_an_answer_keeps_its_citations():
    r = _coerce({"kind": "answer", "text": "Me asks them to wait a day.",
                 "cites": ["notebook:Lost in transit", " "]}, NB)
    assert r.kind == "answer"
    assert r.cites == ["notebook:Lost in transit"]  # blank citations dropped
    assert r.to_dict() == {"kind": "answer", "text": "Me asks them to wait a day.",
                           "cites": ["notebook:Lost in transit"]}


def test_an_answer_with_no_citations_still_renders():
    # "the notebook does not cover it" is a real answer. Absence renders as absence, not as a
    # fabricated label — the same stance CategoryTag takes.
    r = _coerce({"kind": "answer", "text": "Me not know that one — you never told me."}, NB)
    assert r.kind == "answer" and r.cites == []
    assert "cites" in r.to_dict() and r.to_dict()["cites"] == []


def test_an_empty_reply_never_renders_blank():
    assert _coerce({"kind": "answer", "text": "  "}, NB).text


# --- proposals ----------------------------------------------------------------------------------

def test_a_proposal_carries_the_claim_and_lands_in_a_section():
    r = _coerce({"kind": "proposal", "text": "Me hears a rule.",
                 "claim": "Broken headphones get replaced within 60 days.",
                 "section": "fact", "consequence": "Me will lean on this for warranty mail."}, NB)
    assert r.kind == "proposal"
    assert r.claim == "Broken headphones get replaced within 60 days."
    d = r.to_dict()
    assert d["provenance"] == "stated"       # she said it out loud
    assert d["section"] == "fact"
    assert "warranty mail" in d["consequence"]


def test_a_proposal_with_no_claim_degrades_to_an_answer():
    # A card whose confirm button would write an empty line into her notebook must not exist.
    r = _coerce({"kind": "proposal", "text": "Got it!", "claim": "   "}, NB)
    assert r.kind == "answer"
    assert r.to_dict()["kind"] == "answer"


def test_an_unknown_section_falls_back_to_fact():
    r = _coerce({"kind": "proposal", "text": "x", "claim": "y", "section": "elsewhere"}, NB)
    assert r.section == "fact" and r.section in SECTIONS


# --- supersede ----------------------------------------------------------------------------------

def test_supersedes_resolves_an_exact_line():
    r = _coerce({"kind": "proposal", "text": "x",
                 "claim": "Headphone returns within 60 days, no receipt.",
                 "section": "fact",
                 "supersedes": "Headphone returns within 30 days, receipt required."}, NB)
    assert r.supersedes == "Headphone returns within 30 days, receipt required."
    assert 'Replaces: "Headphone returns within 30 days' in r.consequence
    assert r.to_dict()["receipts"]  # the card says it is a replacement


def test_supersedes_tolerates_rewrapped_whitespace_and_case():
    # A model that re-wraps a line has still identified it; refusing that would push a real
    # replacement into an append, which is the exact failure supersede exists to prevent.
    r = _coerce({"kind": "proposal", "text": "x", "claim": "y", "section": "fact",
                 "supersedes": "  headphone returns  within 30 days,\n receipt REQUIRED. "}, NB)
    assert r.supersedes == "Headphone returns within 30 days, receipt required."


def test_a_supersedes_that_matches_nothing_becomes_a_disclosed_add():
    r = _coerce({"kind": "proposal", "text": "x", "claim": "y", "section": "fact",
                 "supersedes": "We never take headphones back at all.",
                 "consequence": "Me will lean on this."}, NB)
    assert r.supersedes == ""                       # nothing is claimed that isn't true
    assert "can't find that line" in r.consequence  # and she is told, rather than it going quiet
    assert not r.to_dict()["receipts"]


def test_supersedes_only_matches_within_its_own_section():
    # The fact text is real, but it is not a bucket cliff. Matching across sections would replace
    # the wrong row.
    assert _resolve_supersedes("Headphone returns within 30 days, receipt required.", "bucket", NB) == ""
    assert _resolve_supersedes("Headphone returns within 30 days, receipt required.", "fact", NB)


def test_supersedes_matches_a_bucket_by_its_cliff_and_a_macro_by_its_text():
    assert _resolve_supersedes(NB.buckets[0].cliff, "bucket", NB) == NB.buckets[0].cliff
    assert _resolve_supersedes("We ship in 2 business days.", "macro", NB) == "We ship in 2 business days."
    assert _resolve_supersedes("She waives restocking for repeat customers.", "note", NB)


def test_an_empty_supersedes_is_simply_an_addition():
    r = _coerce({"kind": "proposal", "text": "x", "claim": "y", "section": "fact",
                 "supersedes": "", "consequence": "c"}, NB)
    assert r.supersedes == "" and r.consequence == "c"  # no apology text on a plain add


# --- the prompt ---------------------------------------------------------------------------------

def test_the_transcript_is_bounded_and_labelled():
    turns = [{"from": "you" if i % 2 == 0 else "bean", "text": f"turn {i} " + "x" * 5000}
             for i in range(30)]
    user = _user_message("the newest thing", turns)
    assert user.count("She said:") + user.count("You said:") == MAX_TURNS
    assert "turn 29" in user and "turn 0" not in user   # the RECENT ones survive
    assert "the newest thing" in user
    assert len(user) < MAX_TURNS * 700 + 500            # each turn truncated


def test_a_first_message_carries_no_transcript_block():
    user = _user_message("hello", [])
    assert "EARLIER IN THIS CONVERSATION" not in user
    assert user.endswith("hello")


def test_blank_turns_are_dropped():
    assert "EARLIER" not in _user_message("hi", [{"from": "you", "text": "   "}])


# --- the call ------------------------------------------------------------------------------------

def test_reply_sends_the_notebook_as_the_cached_prefix_and_forces_the_tool():
    model = FakeModel({"answer_or_propose": {
        "kind": "proposal", "text": "Me hears a rule.",
        "claim": "Broken headphones get replaced within 60 days.",
        "section": "fact", "supersedes": "Headphone returns within 30 days, receipt required.",
        "consequence": "Me will lean on this for warranty mail.",
    }})
    # A message with no overlap with the instructions, so "not in system" means what it says.
    said = "walnut frames went up 8% last monday"
    out = reply(NB, said, [], model)

    assert out.kind == "proposal"
    assert out.supersedes == "Headphone returns within 30 days, receipt required."

    call = model.calls[-1]
    assert call["tool"] == CHAT_TOOL["name"]                # the boundary is the schema, not the prompt
    system = "\n".join(b["text"] for b in call["system"])
    assert "Headphone returns within 30 days" in system     # the notebook IS the prefix
    assert said in call["user"]                             # her words go in the user half
    assert said not in system                               # ...and never in the cached half


def test_the_tool_schema_offers_nowhere_to_put_a_chatty_reply():
    """The product boundary, enforced structurally. Bean cannot answer 'sure thing!' and stop —
    every turn is an answer or a proposal because the schema has no third option."""
    assert CHAT_TOOL["input_schema"]["properties"]["kind"]["enum"] == ["answer", "proposal"]
    assert "kind" in CHAT_TOOL["input_schema"]["required"]


# --- about me: visible to the chat, invisible to the engine ------------------------------------

ABOUT = "Bean was made by a person to help the operator with her mail. Bean is a silly bean."


def test_the_about_block_reaches_the_chat_prompt():
    model = FakeModel({"answer_or_propose": {"kind": "answer", "text": "me just here to help"}})
    reply(NB, "who made you?", [], model, about=ABOUT)
    system = "\n".join(b["text"] for b in model.calls[-1]["system"])
    assert "silly bean" in system
    assert "ABOUT ME" in system


def test_no_about_file_leaves_the_prompt_byte_identical():
    """Absent ⇒ no block. The common case must not pay for a feature it does not use, and the
    cached prefix must not change shape just because the feature exists."""
    a = FakeModel({"answer_or_propose": {"kind": "answer", "text": "x"}})
    b = FakeModel({"answer_or_propose": {"kind": "answer", "text": "x"}})
    reply(NB, "hello", [], a)                 # no about= at all
    reply(NB, "hello", [], b, about="   ")    # present but blank
    assert a.calls[-1]["system"] == b.calls[-1]["system"]
    assert len(a.calls[-1]["system"]) == 2    # instructions + notebook, nothing else




def test_load_about_degrades_to_nothing_when_the_file_is_missing(tmp_path):
    # Flavour must never cost her a turn of conversation.
    assert load_about(tmp_path / "nope.md") == ""


def test_load_about_reads_and_caps_a_real_file(tmp_path):
    p = tmp_path / "about.md"
    p.write_text("  " + ("y" * 5000) + "  ", encoding="utf-8")
    out = load_about(p)
    assert len(out) == ABOUT_MAX_CHARS and out.startswith("y")


def test_the_engine_never_sees_the_about_text():
    """The load-bearing separation. The notebook is citable INTO A CUSTOMER REPLY; this is not
    notebook material, so it must be structurally impossible for it to reach the drafting prompt."""
    from bean.contract import Email
    from bean.engine import _user_message

    email = Email(id="e1", sender_name="A", sender_email="a@b.com", subject="s", body="b")
    user = _user_message(email, [], [], None, None)
    assert "silly bean" not in user
    # And the engine module has no route to the file at all.
    import inspect

    import bean.engine as engine
    assert "about_path" not in inspect.getsource(engine)
    assert "about" not in [p for p in inspect.signature(engine.draft_email).parameters]


def test_chatreply_defaults_are_a_safe_answer():
    assert ChatReply(kind="answer", text="hi").to_dict() == {"kind": "answer", "text": "hi", "cites": []}
