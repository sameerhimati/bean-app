"""applyClaim (web/bean-cite.jsx), tested from Python under node.

This is the function that turns an approved chat proposal into a new notebook object. It is the
only place in the chat feature that can change what Bean drafts from, so its invariant is the one
worth pinning: **a row is removed only if it is found.** If the model named a line that is no longer
there, this appends and says so — it never deletes a near-match, and never reports a replacement it
did not make.

The REAL source runs under node (React/window stubbed, so loading the module just defines its
functions), so the test can't drift from what ships. Skips (never lies) when node is absent.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
from pathlib import Path

import pytest

_WEB = Path(__file__).parent.parent / "web"

pytestmark = pytest.mark.skipif(shutil.which("node") is None, reason="node not installed")

_HARNESS = r"""
const fs = require('fs');
const path = require('path');
const WEB = process.env.BEAN_WEB;

// bean-cite.jsx destructures React hooks at module scope and assigns window.* at load; it never
// runs a component body at import, so a bare stub is enough.
global.window = {};
global.React = { createElement: () => null, useState: () => [null, () => {}], Fragment: 'F' };
new Function('window', 'React', 'module', fs.readFileSync(path.join(WEB, 'bean-cite.jsx'), 'utf8'))
  .call(global.window, global.window, global.React, { exports: {} });

const applyClaim = window.applyClaim;

const NB = () => ({
  store: 'A store',
  buckets: [{ name: 'Returns', cliff: 'within 90 days I send a replacement', stakes: 'normal' }],
  macros: [{ name: 'Shipping', text: 'We ship in 2 business days.' }],
  facts: [
    { text: 'Headphone returns within 30 days, receipt required.', provenance: 'stated' },
    { text: 'Free shipping over $75.', provenance: 'stated' },
  ],
  notes: [{ text: 'She waives restocking for repeat customers.', provenance: 'observed' }],
});

const run = (msg, claim, nb) => {
  const before = nb || NB();
  const out = applyClaim(before, msg, claim);
  return { facts: out.notebook.facts, buckets: out.notebook.buckets, macros: out.notebook.macros,
           notes: out.notebook.notes, replaced: out.replaced, mutatedInput: JSON.stringify(before) !== JSON.stringify(NB()) };
};

console.log(JSON.stringify({
  replace_fact: run(
    { section: 'fact', supersedes: 'Headphone returns within 30 days, receipt required.' },
    'Broken headphones get replaced within 60 days.'),
  replace_rewrapped: run(
    { section: 'fact', supersedes: '  headphone returns  within 30 days,\n RECEIPT required. ' },
    'Sixty days now.'),
  add_when_no_supersedes: run({ section: 'fact', supersedes: '' }, 'Gift wrap is $4.'),
  add_when_supersedes_is_gone: run(
    { section: 'fact', supersedes: 'A line she already deleted.' }, 'Gift wrap is $4.'),
  replace_bucket_cliff: run(
    { section: 'bucket', supersedes: 'within 90 days I send a replacement' },
    'within 60 days I send a replacement'),
  unmatched_bucket_lands_as_a_fact: run(
    { section: 'bucket', supersedes: '' }, 'Something about a brand new situation.'),
  unmatched_macro_lands_as_a_fact: run(
    { section: 'macro', supersedes: '' }, 'A brand new standard answer.'),
  replace_macro_text: run(
    { section: 'macro', supersedes: 'We ship in 2 business days.' }, 'We ship in 1 business day.'),
  add_note_keeps_observed: run({ section: 'note', supersedes: '' }, 'She refunds VIPs on sight.'),
  blank_claim_is_a_noop: run({ section: 'fact', supersedes: '' }, '   '),
  unknown_section_falls_back_to_facts: run({ section: 'nowhere', supersedes: '' }, 'Some line.'),
  missing_message_is_survivable: run(undefined, 'Some line.'),
}));
"""


def _run() -> dict:
    proc = subprocess.run(
        ["node", "--input-type=commonjs", "-e", _HARNESS], capture_output=True, text=True,
        env={**os.environ, "BEAN_WEB": str(_WEB)},
    )
    if proc.returncode != 0:
        raise AssertionError(f"node failed:\n{proc.stderr}")
    return json.loads(proc.stdout)


def test_a_named_line_is_replaced_in_place_not_appended():
    r = _run()["replace_fact"]
    texts = [f["text"] for f in r["facts"]]
    assert texts == ["Broken headphones get replaced within 60 days.", "Free shipping over $75."]
    assert r["replaced"] == "Headphone returns within 30 days, receipt required."
    # The whole point: ONE line on the topic afterwards, not two contradicting each other.
    assert not any("30 days" in t for t in texts)


def test_a_rewrapped_line_still_counts_as_the_same_line():
    # A model that re-wraps or re-cases has still identified the row. Refusing that would push a
    # real replacement into an append — the exact failure supersede exists to prevent.
    r = _run()["replace_rewrapped"]
    assert r["replaced"] == "Headphone returns within 30 days, receipt required."
    assert len(r["facts"]) == 2


def test_a_claim_with_nothing_to_supersede_is_appended():
    r = _run()["add_when_no_supersedes"]
    assert [f["text"] for f in r["facts"]][-1] == "Gift wrap is $4."
    assert len(r["facts"]) == 3
    assert r["replaced"] == ""


def test_a_supersedes_that_is_gone_appends_and_reports_that_it_appended():
    """She edited the notebook after Bean proposed. Deleting a near-match would drop the wrong
    policy silently; the honest move is to add, and to say so."""
    r = _run()["add_when_supersedes_is_gone"]
    assert r["replaced"] == ""          # nothing is claimed that isn't true
    assert len(r["facts"]) == 3         # nothing was destroyed
    assert [f["text"] for f in r["facts"]][-1] == "Gift wrap is $4."


def test_a_bucket_cliff_is_replaced_on_its_cliff_field():
    r = _run()["replace_bucket_cliff"]
    assert r["buckets"][0]["cliff"] == "within 60 days I send a replacement"
    assert r["buckets"][0]["name"] == "Returns"          # the name is hers and is never touched
    assert r["buckets"][0]["stakes"] == "normal"         # nor is anything else on the row
    assert r["replaced"] == "within 90 days I send a replacement"


def test_a_macro_is_replaced_on_its_text_field():
    r = _run()["replace_macro_text"]
    assert r["macros"][0]["text"] == "We ship in 1 business day."
    assert r["macros"][0]["name"] == "Shipping"


def test_an_unmatched_bucket_or_macro_becomes_a_fact_rather_than_inventing_a_heading():
    # Buckets and macros are NAMED and a chat proposal carries no name. Bean may propose changing a
    # cliff she already has; it must not conjure a new bucket out of one sentence.
    for key in ("unmatched_bucket_lands_as_a_fact", "unmatched_macro_lands_as_a_fact"):
        r = _run()[key]
        assert len(r["buckets"]) == 1 and len(r["macros"]) == 1  # nothing added to either
        assert len(r["facts"]) == 3
        assert r["replaced"] == ""


def test_an_appended_note_keeps_the_observed_provenance():
    r = _run()["add_note_keeps_observed"]
    assert r["notes"][-1] == {"text": "She refunds VIPs on sight.", "provenance": "observed"}


def test_a_blank_claim_changes_nothing():
    r = _run()["blank_claim_is_a_noop"]
    assert len(r["facts"]) == 2 and r["replaced"] == ""


def test_an_unknown_section_falls_back_to_facts():
    assert len(_run()["unknown_section_falls_back_to_facts"]["facts"]) == 3


def test_a_missing_message_does_not_throw():
    assert len(_run()["missing_message_is_survivable"]["facts"]) == 3


def test_the_chat_component_never_fetches_or_persists():
    """The moat its own header promises, checked rather than trusted.

    BeanChat renders a transcript and calls the seams it is handed. The moment it fetches, the
    notebook ETag has two owners and the panel starts deciding what a confirm means — which is
    exactly the coupling ProposalCard, BeanNotebook and BeanCiteSheet all refuse.
    """
    src = (_WEB / "bean-chat.jsx").read_text(encoding="utf-8")
    code = "\n".join(ln for ln in src.splitlines() if not ln.lstrip().startswith("//"))
    for forbidden in ("fetch(", "beanStore", "localStorage", "XMLHttpRequest"):
        assert forbidden not in code, f"bean-chat.jsx must not reach for {forbidden}"


def test_the_input_notebook_is_never_mutated():
    """bean-root hands its live `notebook` state in. Mutating it would change what is on screen
    before the save has landed — and leave the wrong thing there if the PUT 409s."""
    r = _run()
    for case in r.values():
        assert case["mutatedInput"] is False
