"""Render smoke for web/bean-questionnaire.jsx — the approval conversation for her notebook.

The two things that must hold, because getting either wrong is a consent bug rather than a UI bug:

1. **One commit, at the end.** The walk edits a working copy; nothing leaves the component until the
   last card. A per-card save would rotate the ETag a dozen times and leave a half-approved notebook
   Bean would start drafting from mid-walk.
2. **What she said is what gets saved** — a fixed claim keeps her wording, a declined card is really
   gone, an untouched card survives byte-for-byte, and her sign-off answer lands as a judgment note.

Skips (never lies) when node or jsdom is absent.
"""

from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path

import pytest

_ROOT = Path(__file__).parent.parent
_WEB = _ROOT / "web"
_JS = Path(__file__).parent / "js"

pytestmark = pytest.mark.skipif(
    shutil.which("node") is None or not (_JS / "node_modules" / "jsdom").exists(),
    reason="node or tests/js jsdom not installed",
)

_HARNESS = r"""
const { JSDOM } = require('jsdom');
const fs = require('fs');
const path = require('path');
const WEB = process.env.BEAN_WEB;

const dom = new JSDOM('<!DOCTYPE html><body><div id="root"></div></body>', { url: 'http://localhost' });
const { window } = dom;
global.window = window; global.document = window.document; global.navigator = window.navigator;

function loadVendor(file) {
  const code = fs.readFileSync(path.join(WEB, 'vendor', file), 'utf8');
  new Function('window','self','globalThis','module','exports','require', code)
    .call(window, window, window, window, undefined, undefined, undefined);
}
loadVendor('react.production.min.js');
loadVendor('react-dom.production.min.js');
global.React = window.React; global.ReactDOM = window.ReactDOM;
window.BeanMark = () => null;

new Function(fs.readFileSync(path.join(WEB, 'bean-proposal.jsx'), 'utf8')).call(window);
new Function(fs.readFileSync(path.join(WEB, 'bean-questionnaire.jsx'), 'utf8')).call(window);

const notebook = {
  store: 'Sable & Wren',
  buckets: [
    { name: 'Cracked tube', cliff: 'Within 90 days me just replaces it.', stakes: 'normal' },
    { name: 'Refund ask', cliff: 'Me reads the order first, then decides.', stakes: 'high' },
  ],
  macros: [{ name: 'Shipping times', text: 'Orders ship in 1-2 business days.' }],
  facts: [{ text: 'Free U.S. shipping on orders $120+.', provenance: 'stated' }],
  notes: [{ text: 'Never blame the customer for a cracked tube.', provenance: 'observed' }],
};
const calls = { completed: [], closed: 0, progress: [] };
const root = document.getElementById('root');
const mount = (nb, initialProgress) => { ReactDOM.unmountComponentAtNode(root);
  ReactDOM.render(React.createElement(window.BeanQuestionnaire, {
    notebook: nb === undefined ? notebook : nb,
    initialProgress: initialProgress || {},
    onSaveProgress: p => { calls.progress.push(p); },
    onComplete: n => { calls.completed.push(n); return Promise.resolve(n); },
    onClose: () => { calls.closed++; },
  }), root); };
const buttonWith = (t) => [...root.querySelectorAll('button')].find(b => b.textContent === t);
const setValue = (el, v) => {
  Object.getOwnPropertyDescriptor(window.HTMLTextAreaElement.prototype, 'value').set.call(el, v);
  el.dispatchEvent(new window.Event('input', { bubbles: true }));
};

// ---- the empty state: nothing distilled yet -------------------------------------------------
mount(null);
const emptyState = root.textContent;

// ---- the walk --------------------------------------------------------------------------------
mount();
const card1 = root.textContent;

// 1 · bucket "Cracked tube" — she FIXES it before confirming
buttonWith('✎ fix it').click();
setValue(root.querySelector('textarea'), 'Within 120 days me just replaces it, no questions.');
buttonWith('✓ Save my version').click();
const card2 = root.textContent;
const midwalkCommits = calls.completed.length;   // must still be 0 — nothing saves mid-walk

// 2 · bucket "Refund ask" (high stakes) — plain confirm
const card2HasStakesPromise = card2.includes('always check with you before sending');
buttonWith('✓ that’s right').click();
const card3 = root.textContent;

// 3 · macro — she leaves it out
buttonWith('leave it out').click();
const card4 = root.textContent;

// 4 · fact — plain confirm
buttonWith('✓ that’s right').click();
const card5 = root.textContent;

// 5 · note — plain confirm
buttonWith('✓ that’s right').click();
const card6 = root.textContent;

// 6 · sign-off — the answer becomes a judgment note, and finishing IS the approval
setValue(root.querySelector('textarea'), 'hand it to me with what you found');
buttonWith('✓ That’s my notebook — save it').click();

// what each tap saved as she went — the resume snapshots
const progressAfterFirstWalk = calls.progress.slice();

// ---- a second run where she skips the sign-off ------------------------------------------------
mount();
for (let n = 0; n < 5; n++) buttonWith('✓ that’s right').click();
const signoffCard = root.textContent;
buttonWith('skip this bit').click();

// ---- RESUME: reopen a half-finished walk from saved progress ---------------------------------
// She answered the two buckets last session (fixed #0, kept #1), then closed the tab. Reopening
// should land on card 3 (the macro), not card 1, and carry her earlier answers into the final save.
calls.progress = [];
const savedProgress = { patched: { 'buckets:0': 'Within 120 days, replaced.' }, dropped: { 'buckets:1': true }, signoff: '' };
mount(notebook, savedProgress);
const resumeCard = root.textContent;   // should be Card 3 of 6 (the macro)
// finish the rest: macro confirm, fact confirm, note confirm, then sign-off
buttonWith('✓ that’s right').click();  // macro
buttonWith('✓ that’s right').click();  // fact
buttonWith('✓ that’s right').click();  // note
setValue(root.querySelector('textarea'), 'ask me');
buttonWith('✓ That’s my notebook — save it').click();
const resumedNotebook = calls.completed[calls.completed.length - 1];

// ---- the load race: mounted before the notebook landed, then it lands --------------------------
// Two fetches start on mount (the notebook and her resume state) and either can win. If the walk is
// mounted on the loser, `cards` freezes empty — and stays empty when the notebook arrives, because
// it is built ONCE on purpose. It must degrade to the empty state, never index off the end of it.
let raceCrash = null;
try {
  ReactDOM.unmountComponentAtNode(root);
  ReactDOM.render(React.createElement(window.BeanQuestionnaire, {
    notebook: null, initialProgress: {}, onSaveProgress: () => {},
    onComplete: n => Promise.resolve(n), onClose: () => {},
  }), root);
  // the notebook arrives a beat later, same mount — no remount, no new key
  ReactDOM.render(React.createElement(window.BeanQuestionnaire, {
    notebook, initialProgress: {}, onSaveProgress: () => {},
    onComplete: n => Promise.resolve(n), onClose: () => {},
  }), root);
} catch (e) { raceCrash = String(e && e.message || e); }
const afterRace = root.textContent;

// ---- walkRemaining: the "N left" hint on the 🫘 Teach Bean button -----------------------------
// Pure, so it's read directly rather than through a render. The states that matter are the three
// the button has to tell apart: never started, mid-walk, and approved (progress cleared).
const remaining = {
  fresh: window.walkRemaining(notebook, {}),
  noNotebook: window.walkRemaining(null, { patched: { 'buckets:0': 'x' } }),
  midwalk: window.walkRemaining(notebook, savedProgress),   // buckets 0 + 1 decided → 3 content cards left
  everythingDecided: window.walkRemaining(notebook, {
    patched: { 'buckets:0': 'a', 'buckets:1': 'b', 'macros:0': 'c', 'facts:0': 'd' },
    dropped: { 'notes:0': true },
  }),
};

process.stdout.write(JSON.stringify({ emptyState, card1, card2, card3, card4, card5, card6,
  midwalkCommits, card2HasStakesPromise, signoffCard, calls,
  progressAfterFirstWalk, resumeCard, resumedNotebook, remaining, raceCrash, afterRace }));
"""


def _run() -> dict:
    import os
    proc = subprocess.run(
        ["node", "-e", _HARNESS],
        cwd=str(_JS), capture_output=True, text=True, timeout=60,
        env={**os.environ, "BEAN_WEB": str(_WEB)},
    )
    assert proc.returncode == 0, f"harness failed:\nSTDOUT:{proc.stdout}\nSTDERR:{proc.stderr}"
    return json.loads(proc.stdout)


def test_the_walk_is_one_card_at_a_time_with_honest_progress():
    out = _run()
    assert "Card 1 of 6" in out["card1"]       # 2 buckets + 1 macro + 1 fact + 1 note + sign-off
    assert "Card 2 of 6" in out["card2"]
    assert "Card 6 of 6" in out["card6"]
    # The lead-in names what it's asking about; the claim itself is HER sentence, unprefixed.
    assert "how you handle “Cracked tube”" in out["card1"]
    assert "Within 90 days me just replaces it." in out["card1"]
    assert "Your standard answers" in out["card3"] and "Orders ship in 1-2 business days." in out["card3"]
    assert "One last thing" in out["card6"]


def test_nothing_persists_until_the_last_card():
    """The consent invariant. Five decisions were made before the sign-off; none of them committed."""
    out = _run()
    assert out["midwalkCommits"] == 0
    # the harness completes 3 walks total (full, skip-signoff, resume); the invariant is one commit
    # PER completed walk, never a mid-walk one — proven by midwalkCommits==0 above.
    assert len(out["calls"]["completed"]) == 3


def test_the_approved_notebook_carries_exactly_what_she_said():
    out = _run()
    nb = out["calls"]["completed"][0]

    assert nb["store"] == "Sable & Wren"
    # fixed → her wording, other fields untouched
    assert nb["buckets"][0]["cliff"] == "Within 120 days me just replaces it, no questions."
    assert nb["buckets"][0]["name"] == "Cracked tube" and nb["buckets"][0]["stakes"] == "normal"
    # confirmed unchanged → byte-for-byte
    assert nb["buckets"][1] == {"name": "Refund ask", "cliff": "Me reads the order first, then decides.", "stakes": "high"}
    # declined → really gone, not blanked
    assert nb["macros"] == []
    assert nb["facts"] == [{"text": "Free U.S. shipping on orders $120+.", "provenance": "stated"}]
    # her sign-off answer lands as a judgment note she told us directly
    assert nb["notes"][0] == {"text": "Never blame the customer for a cracked tube.", "provenance": "observed"}
    assert nb["notes"][1] == {"text": "When unsure: hand it to me with what you found", "provenance": "stated"}


def test_a_high_stakes_bucket_previews_its_promise_while_she_decides():
    """'🔒 always my confirm' is a promise about her money. Show it at the moment she's agreeing to
    the bucket, not as a pill she has to decode in the document later."""
    out = _run()
    assert out["card2HasStakesPromise"] is True


def test_the_signoff_can_be_skipped_without_inventing_a_rule():
    """Skipping still completes the approval — but it must not write a blank 'When unsure:' note,
    which would read to the engine as a rule she never gave."""
    out = _run()
    assert "When me’s not sure about an email" in out["signoffCard"]
    nb = out["calls"]["completed"][1]
    assert [n["text"] for n in nb["notes"]] == ["Never blame the customer for a cracked tube."]


def test_an_undistilled_notebook_says_so_rather_than_walking_her_through_nothing():
    out = _run()
    assert "Nothing to go over yet" in out["emptyState"]


def test_each_card_saves_her_answer_as_she_goes():
    """The 45-card walk is done across sittings, so every tap must persist — not just the final
    submit. Each save carries the WHOLE decisions object (a resume snapshot), so the last one has all
    five decisions even though the notebook itself hasn't been written yet."""
    out = _run()
    saves = out["progressAfterFirstWalk"]
    assert len(saves) >= 5, "one save per card tap, not just at the end"
    # still zero notebook commits at that point — progress saving is NOT the approval
    assert out["midwalkCommits"] == 0
    last = saves[-1]
    assert last["patched"]["buckets:0"] == "Within 120 days me just replaces it, no questions."
    assert last["dropped"]["macros:0"] is True   # the macro she left out


def test_reopening_resumes_on_the_first_unanswered_card_with_earlier_answers_kept():
    """She answered the two buckets last session and closed the tab. Reopening from saved progress
    lands on card 3 (the macro) — not card 1 — and the final approved notebook still carries the
    fixed bucket and the dropped one, proving the resume state actually fed the walk."""
    out = _run()
    assert "Card 3 of 6" in out["resumeCard"], "resume must skip the two answered buckets"
    assert "Your standard answers" in out["resumeCard"]
    nb = out["resumedNotebook"]
    # the bucket she fixed last session survived into this session's final save
    assert nb["buckets"][0]["cliff"] == "Within 120 days, replaced."
    # the bucket she dropped last session is still gone
    assert len(nb["buckets"]) == 1 and nb["buckets"][0]["name"] == "Cracked tube"


def test_the_teach_bean_hint_only_counts_a_walk_she_is_genuinely_mid_way_through():
    """The number on 🫘 Teach Bean is a resume hint, so it may only appear when resuming is the thing
    to do. A count in the other states would be a lie: '45 left' before she has ever tapped reads as
    a chore list, and — because approval CLEARS her progress server-side — the same empty overlay
    would otherwise make a freshly approved notebook advertise a full walk again."""
    out = _run()
    r = out["remaining"]
    assert r["midwalk"] == 3, "2 of the 5 content cards decided → 3 left (the sign-off never counts)"
    assert r["fresh"] is None, "never started → the plain button, no count"
    assert r["everythingDecided"] is None, "nothing left → no '(0 left)'"
    assert r["noNotebook"] is None, "nothing distilled yet → nothing to count"


def test_a_notebook_that_arrives_after_mount_never_leaves_a_blank_page():
    """The walk is built once at mount, so a notebook that lands a moment later cannot refill it. That
    is the correct trade for a stable 45-card walk — but it means mounting one beat early used to
    leave `cards` empty while `notebook` read truthy, and the render indexed straight off the end of
    it. A white screen, on the surface the 🫘 Teach Bean button now opens. bean-root holds the real
    fix (it waits for both fetches); this pins the component's own floor."""
    out = _run()
    assert out["raceCrash"] is None, f"the walk threw when the notebook arrived late: {out['raceCrash']}"
    assert "Nothing to go over yet" in out["afterRace"], (
        "an empty walk must say so — a blank page is the failure this guards")
