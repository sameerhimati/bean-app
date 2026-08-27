"""Offline tests for bean/quoting.py — recovering the real messages inside a quoted history.

Pure functions, no I/O, no model. The shapes exercised here are the ones measured on the live
corpus, not invented: Gmail/Proton `On … wrote:` with and without an angle-bracketed address,
Outlook's `-----Original Message-----` header block, deep `>`-nesting, and the 10% of real blobs
with no recognizable delimiter at all.
"""

from __future__ import annotations

import json
import re
from collections import Counter
from pathlib import Path

import pytest

from bean.quoting import (
    CUSTOMER,
    OTHER,
    THREAD_DELIM_RE,
    UNKNOWN,
    render_for_prompt,
    split_quoted,
)

CUST = "paige@example.com"


def test_empty_and_blank_return_nothing():
    assert split_quoted("", CUST) == []
    assert split_quoted("   \n\n  ", CUST) == []
    assert split_quoted("> \n> \n", CUST) == []


def test_no_delimiter_is_one_unattributed_message():
    # 11 of 108 real blobs have no recognizable delimiter. They must come back whole, not dropped.
    msgs = split_quoted("Just some text\n\nwith a paragraph break.", CUST)
    assert len(msgs) == 1
    assert msgs[0].who == "" and msgs[0].side == UNKNOWN
    assert "paragraph break" in msgs[0].text


def test_single_quoted_message_is_attributed_and_dequoted():
    blob = (
        "On Fri, Aug 14, 2026 at 4:45 PM Northwind Support <hello@northwind.example> wrote:\n"
        "> Thanks so much for contacting us.\n"
        ">\n"
        "> We'll get back to you shortly.\n"
    )
    (msg,) = split_quoted(blob, CUST)
    assert msg.who == "Northwind Support"
    assert "Aug 14, 2026" in msg.when
    assert msg.side == OTHER  # address parsed, and it isn't the customer's
    assert ">" not in msg.text
    assert msg.text.startswith("Thanks so much for contacting us.")
    assert "get back to you shortly" in msg.text


def test_nested_chain_splits_oldest_first():
    blob = (
        "On Fri, Aug 14, 2026 at 4:45 PM Support <hello@northwind.example> wrote:\n"
        "> Sorry about that, we're looking into it.\n"
        ">\n"
        "> On Thu, Aug 13, 2026 at 9:00 AM Paige Edmonds <paige@example.com> wrote:\n"
        ">> Any update on my order?\n"
        ">>\n"
        ">> On Wed, Aug 12, 2026 at 8:00 AM Support <hello@northwind.example> wrote:\n"
        ">>> We have received your message.\n"
    )
    msgs = split_quoted(blob, CUST)
    assert len(msgs) == 3
    # OLDEST FIRST — the whole point. A raw blob is newest-first-nested.
    assert msgs[0].text == "We have received your message."
    assert msgs[1].text == "Any update on my order?"
    assert msgs[2].text.startswith("Sorry about that")
    assert [m.side for m in msgs] == [OTHER, CUSTOMER, OTHER]
    assert not any(">" in m.text for m in msgs)


def test_side_is_unknown_when_the_delimiter_carries_no_address():
    # 88 of 222 real delimiter lines have no address. Those must NOT resolve to OTHER, because the
    # UI collapses OTHER — an absence of evidence would hide what the customer actually wrote.
    blob = "On Fri, Aug 14, 2026 at 4:45 PM Paige Edmonds wrote:\n> Where is my order?\n"
    (msg,) = split_quoted(blob, CUST)
    assert msg.side == UNKNOWN
    assert msg.who == "Paige Edmonds"
    assert "Aug 14, 2026" in msg.when
    assert msg.text == "Where is my order?"


def test_name_match_promotes_to_customer_without_an_address():
    blob = "On Fri, Aug 14, 2026 at 4:45 PM Paige Edmonds wrote:\n> Where is my order?\n"
    (msg,) = split_quoted(blob, CUST, customer_name="Paige Edmonds")
    assert msg.side == CUSTOMER


def test_outlook_original_message_header_block():
    blob = (
        "-----Original Message-----\n"
        "From: Paige Edmonds <paige@example.com>\n"
        "Sent: Thursday, August 13, 2026 9:00 AM\n"
        "To: Support\n"
        "Subject: Order NW-1029\n"
        "\n"
        "I never received the package.\n"
    )
    (msg,) = split_quoted(blob, CUST)
    assert msg.who == "Paige Edmonds"
    assert "August 13, 2026" in msg.when
    assert msg.side == CUSTOMER
    assert msg.text == "I never received the package."  # headers consumed, body intact


def test_body_mentioning_from_colon_keeps_every_byte():
    # _take_outlook_headers only fires when the text STARTS with From:. Prose about a "From:" line
    # is still prose.
    text = "The label says\nFrom: the warehouse\nand that is wrong."
    (msg,) = split_quoted(text, CUST)
    assert msg.text == text


def test_text_before_the_first_delimiter_survives():
    blob = "A trailing note from me.\n\nOn Fri, Aug 14, 2026 at 4:45 PM X <x@y.com> wrote:\n> Older.\n"
    msgs = split_quoted(blob, CUST)
    assert [m.text for m in msgs] == ["Older.", "A trailing note from me."]


def test_a_bare_underscore_rule_is_a_signature_not_a_message():
    # The corpus lesson: `_____` separates a signature far more often than it separates a message.
    # Splitting on it unconditionally reported 17 messages in a thread that holds 13, each phantom
    # being somebody's sign-off with no sender.
    blob = "Latest thought.\n\n______________________________\nJane Doe : Creative Director\n555-0100\n"
    (msg,) = split_quoted(blob, CUST)
    assert "Latest thought." in msg.text and "Creative Director" in msg.text


def test_an_underscore_rule_followed_by_headers_is_a_real_boundary():
    blob = (
        "My reply.\n"
        "______________________________\n"
        "From: Paige Edmonds <paige@example.com>\n"
        "Sent: Thursday, August 13, 2026 9:00 AM\n"
        "\n"
        "The original question.\n"
    )
    msgs = split_quoted(blob, CUST, customer_name="Paige Edmonds")
    assert [m.text for m in msgs] == ["The original question.", "My reply."]
    assert msgs[0].who == "Paige Edmonds"


def test_trailing_signature_attaches_to_the_sender_not_to_a_phantom_message():
    # Text ahead of the first delimiter is the tail of the email's own latest message.
    blob = (
        "______________________________\n"
        "Paige Edmonds : Creative Director\n"
        "\n"
        "On Fri, Aug 14, 2026 at 4:45 PM Support <hello@northwind.example> wrote:\n"
        "> How can we help?\n"
    )
    msgs = split_quoted(blob, CUST, customer_name="Paige Edmonds")
    assert len(msgs) == 2
    assert msgs[0].text == "How can we help?"
    assert "Creative Director" in msgs[1].text
    assert msgs[1].who == "Paige Edmonds" and msgs[1].side == CUSTOMER


def test_mailto_residue_never_reaches_the_byline():
    # inbound._strip_html turns <a href="mailto:x">x</a> into TWO address-shaped runs on one line.
    blob = (
        "On Wednesday, June 10th, 2026 at 7:20 AM, "
        "Jane Doe <jane@example.com> <mailto:jane@example.com> wrote:\n"
        "> Following up.\n"
    )
    (msg,) = split_quoted(blob, CUST)
    assert msg.who == "Jane Doe"
    assert "mailto" not in msg.who and "@" not in msg.who


def test_deep_nesting_terminates_and_loses_nothing():
    # The live corpus tops out at 17 recovered messages; _MAX_DEPTH is 25. Past it we stop
    # descending and keep the remainder whole rather than recursing forever.
    depth = 60
    blob = ""
    for i in range(depth):
        q = ">" * i
        blob += f"{q}On Fri, Aug 14, 2026 at 4:45 PM P{i} <p{i}@x.com> wrote:\n{q}>msg{i}\n"
    msgs = split_quoted(blob, CUST)
    assert msgs  # terminated
    assert "msg0" in "\n".join(m.text for m in msgs)
    assert f"msg{depth - 1}" in "\n".join(m.text for m in msgs)


def test_render_for_prompt_keeps_list_of_str_and_adds_a_byline():
    blob = (
        "On Fri, Aug 14, 2026 at 4:45 PM Support <hello@northwind.example> wrote:\n"
        "> Looking into it.\n"
        ">\n"
        "> On Thu, Aug 13, 2026 at 9:00 AM Paige Edmonds <paige@example.com> wrote:\n"
        ">> Any update?\n"
    )
    out = render_for_prompt(split_quoted(blob, CUST))
    assert all(isinstance(s, str) for s in out)  # the shape engine.py + gate.py already take
    assert len(out) == 2
    assert out[0].startswith("[Paige Edmonds · ")  # oldest first, bylined
    assert "Any update?" in out[0]
    assert "Support" in out[1]


def test_unparseable_message_still_renders_without_a_byline():
    assert render_for_prompt(split_quoted("no delimiter here", CUST)) == ["no delimiter here"]


# --- corpus replay ----------------------------------------------------------------------------
# The synthetic cases above pin the logic. This pins the thing that actually matters: run every
# real quoted history through the parser and prove not one word of anybody's message went missing.
# It found the bug it was written to find — an all-quoted blob with no delimiter came back with its
# `>` markers still on. Real mail never enters the source tree, so this reads the operator's volume
# when it is there and skips when it is not; the synthetic tests are the gate that runs everywhere.

def _local_corpora() -> list[Path]:
    """Any tenant inbox log on this machine's volume. Discovered, never named — a tenant directory
    is the operator's identity, and a hardcoded one would put it in a tracked file."""
    root = Path(__file__).resolve().parents[1] / "data"
    return sorted(p for p in root.glob("*/inbox.jsonl") if p.is_file()) if root.is_dir() else []


@pytest.mark.skipif(not _local_corpora(), reason="no local corpus — synthetic cases cover the logic")
def test_replay_over_the_real_corpus_loses_no_words():
    def words(text: str) -> Counter:
        return Counter(re.findall(r"[A-Za-z]{4,}", text))

    rows = [
        json.loads(ln)
        for corpus in _local_corpora()
        for ln in corpus.read_text(encoding="utf-8").splitlines()
        if ln.strip()
    ]
    threaded = [r for r in rows if r.get("thread")]
    assert threaded, "corpus present but carries no threads"

    total_messages = 0
    for row in threaded:
        blob = "\n".join(row["thread"])
        msgs = split_quoted(blob, row.get("sender_email") or "", row.get("sender_name") or "")
        assert msgs, "a non-empty blob must never parse to zero messages"
        total_messages += len(msgs)

        # Everything that is not a delimiter line or a quote marker has to come back out.
        flat = re.sub(r"(?m)^[ \t>]+", "", blob)
        flat = THREAD_DELIM_RE.sub("", flat)
        flat = re.sub(r"(?im)^\s*(From|Sent|Date|To|Cc|Subject|Reply-To):.*$", "", flat)
        assert not (words(flat) - words(" ".join(m.text for m in msgs)))

        assert all(m.side in (CUSTOMER, OTHER, UNKNOWN) for m in msgs)

    # The point of the module, restated as an invariant: unpacking recovers materially more
    # messages than the stored lists ever held. (Real inbound mail stores exactly one element per
    # `thread` — the whole conversation as a single blob — so the gap is large.)
    assert total_messages > sum(len(r["thread"]) for r in threaded)
