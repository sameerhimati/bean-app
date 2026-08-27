"""The Clear affordances, rendered from the real web/bean-inbox.jsx under jsdom.

She answers plenty of this mail in her own client, and plenty more she simply never wanted a draft
for. Before this the only ways off the queue were "Take it over" and "Snooze" — both behind a click
into the draft view, and both writing to corrections.jsonl. So the assertions that matter are:

  1. Clear is reachable FROM THE ROW. A dismiss that costs a click into the email is the thing this
     replaces, not a smaller version of it.
  2. Clearing does not open the email. The row is a <button> and the control lives inside it, so a
     missing stopPropagation would silently do both.
  3. The sweep counts what the SERVER will delete — approved + handled, never the snoozed rows that
     share the same lane. A count that promised to delete her later-pile would be a lie the server
     then correctly refuses to tell.

That clearing writes NOTHING to the correction log is a property of bean-root.jsx's `clearOne` —
there is no record() on the path — and is asserted there by grep, not here: this file renders the
inbox, which has no correction seam to observe. Skips (never lies) when node or jsdom is absent.
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

// Bean.html's load order for the slice Inbox needs.
for (const f of ['bean-pixel.jsx', 'bean-ui.jsx', 'bean-email.jsx', 'bean-roast.jsx', 'bean-inbox.jsx']) {
  new Function(fs.readFileSync(path.join(WEB, f), 'utf8')).call(window);
}

const root = document.getElementById('root');
const el = (sel) => root.querySelector(sel);
const all = (sel) => [...root.querySelectorAll(sel)];
const clickable = (t) => all('button, [role="button"]').find(b => b.textContent.trim() === t);

function mk(id, extra) {
  return {
    id, category: 'Orders', confidence: 'high', subject: 'Where is it',
    from: { name: 'Amanda Johnson', email: 'amanda@example.com' },
    summary: 'asking about an order', time: '2026-08-19T10:00:00Z', body: [''],
    citations: [], concerns: [], thread: [], ...extra,
  };
}

// c1 pending · c2 approved · c3 snoozed · f1 filed
window.EMAILS = [
  mk('c1'), mk('c2'), mk('c3'),
  mk('f1', { filed: true, category: 'Filed / FYI', gateKind: 'newsletter' }),
];
const status = { c2: 'approved', c3: 'skipped' };

const cleared = [], opened = [], swept = [], dismissed = [], home = [];
ReactDOM.render(React.createElement(window.Inbox, {
  status, pasted: [], filter: {}, onFilterChange: () => {},
  onOpen: id => opened.push(id),
  onClear: id => cleared.push(id),
  onClearHandled: () => swept.push(1),
  onApproveAllHigh: () => {}, onClearFiled: () => {}, onRedraft: () => {}, redraftBusy: null,
  whatsNew: { date: '2026-08-19', title: 'Clear an email', body: 'press Clear on the row', where: 'Any inbox row' },
  onWhatsNewDismiss: () => dismissed.push(1),
  onWhatsNewMore: () => {},
}), root);

const out = {};

// --- 1. the row control ------------------------------------------------------------------------
const clears = all('.row-clear');
out.clear_count = clears.length;                       // only the one pending, unfiled row
out.clear_labels = clears.map(c => c.textContent.trim());

// --- 2. clicking it clears and does NOT open ----------------------------------------------------
clears[0].dispatchEvent(new window.MouseEvent('click', { bubbles: true }));
out.cleared = cleared.slice();
out.opened_on_clear = opened.slice();                  // must stay empty — stopPropagation

// --- 3. the row itself still opens --------------------------------------------------------------
el('.inbox-row').dispatchEvent(new window.MouseEvent('click', { bubbles: true }));
out.opened_on_row = opened.slice();

// --- 4. the sweep counts approved+handled, never snoozed ----------------------------------------
const sweep = all('button').find(b => /Clear \d+ handled/.test(b.textContent));
out.sweep_label = sweep ? sweep.textContent.trim() : null;
if (sweep) sweep.dispatchEvent(new window.MouseEvent('click', { bubbles: true }));
out.swept = swept.length;

// --- 5. the what's-new card is on the inbox, above the greeting ---------------------------------
const card = el('.whatsnew-card');
out.card_present = !!card;
out.card_title = card ? card.querySelector('h3').textContent : null;
out.card_before_greeting = !!(card && (card.compareDocumentPosition(el('.greet-card')) &
  window.Node.DOCUMENT_POSITION_FOLLOWING));
const x = card && card.querySelector('.whatsnew-x');
if (x) x.dispatchEvent(new window.MouseEvent('click', { bubbles: true }));
out.dismissed = dismissed.length;

// --- 6. the topbar: the wordmark is the way home, the bean is not ------------------------------
ReactDOM.render(React.createElement(window.TopBar, {
  onOpenInbox: () => home.push(1),
  onOpenAdmin: () => {}, onOpenStats: () => {}, onOpenNotebook: () => {},
  onTryEmail: () => {}, onOpenTeach: () => {}, onReopenOnboarding: () => {},
  teachLeft: 0, connected: true, whatsNew: null,
}), root);
const nav = [...root.querySelectorAll('.teach-btn')].map(b => b.textContent.trim());
out.nav_labels = nav;
out.nav_has_emoji = nav.some(t => /\p{Extended_Pictographic}|[\u2000-\u2BFF]/u.test(t));
root.querySelector('.brand-home').dispatchEvent(new window.MouseEvent('click', { bubbles: true }));
out.home_from_wordmark = home.length;
root.querySelector('.brand .beanary-btn').dispatchEvent(new window.MouseEvent('click', { bubbles: true }));
out.home_after_bean_press = home.length;   // unchanged — the bean brews, it does not navigate

console.log(JSON.stringify(out));
"""


@pytest.fixture(scope="module")
def rendered():
    proc = subprocess.run(
        ["node", "-e", _HARNESS],
        capture_output=True, text=True, cwd=_JS, env={"BEAN_WEB": str(_WEB), "PATH": "/usr/bin:/bin:/usr/local/bin:/opt/homebrew/bin"},
    )
    assert proc.returncode == 0, proc.stderr
    return json.loads(proc.stdout.strip().splitlines()[-1])


def test_only_open_customer_mail_offers_a_clear(rendered):
    """One control, on the one row that is both pending and not filed. A done row has nothing left to
    clear, and filed mail has its own one-tap purge — two clears on one row is two decisions."""
    assert rendered["clear_count"] == 1
    assert rendered["clear_labels"] == ["Clear"]


def test_clearing_a_row_does_not_open_the_email(rendered):
    """The control lives INSIDE the row's <button>. Without stopPropagation this would clear the
    email and drop her into the draft view for it in the same tap."""
    assert rendered["cleared"] == ["c1"]
    assert rendered["opened_on_clear"] == []


def test_the_row_still_opens_when_tapped_anywhere_else(rendered):
    assert rendered["opened_on_row"] == ["c1"]


def test_the_sweep_counts_only_what_the_server_will_delete(rendered):
    """c2 is approved, c3 is snoozed. Both sit in 'Handled today'; only c2 is sweepable, and the
    label has to say 1 — the server refuses to delete a snoozed email, so a 2 would be a promise
    it would break."""
    assert rendered["sweep_label"] == "Clear 1 handled"
    assert rendered["swept"] == 1


def test_whats_new_lands_on_the_inbox_above_the_greeting(rendered):
    """It used to live three taps deep in Settings behind a dot. The point of the change is that she
    is TOLD, on the screen she opens every morning, without it blocking the queue."""
    assert rendered["card_present"]
    assert rendered["card_title"] == "Clear an email"
    assert rendered["card_before_greeting"]
    assert rendered["dismissed"] == 1


def test_the_wordmark_goes_home_and_the_bean_does_not(rendered):
    """Two jobs down the middle of the brand lockup. Pressing the words is the way back to the
    inbox from anywhere; pressing the bean still only brews. Rolling them together would cost one
    or the other."""
    assert rendered["home_from_wordmark"] == 1
    assert rendered["home_after_bean_press"] == 1  # the bean press navigated nowhere


def test_the_nav_carries_no_emoji(rendered):
    """✎ 🫘 📓 📊 ⚙ rendered as two text glyphs and three full-colour emoji in one row of otherwise
    identical buttons — two weights, and a palette that appears nowhere else in Bean. The colour
    ones also change shape with the OS emoji font, which is not a thing to hand to Apple."""
    assert rendered["nav_labels"] == ["Paste email", "Teach Bean", "Notebook", "Report", "Settings"]
    assert not rendered["nav_has_emoji"]
