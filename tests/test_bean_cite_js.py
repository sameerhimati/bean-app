"""Render smoke for web/bean-cite.jsx under a real DOM (jsdom + the vendored React).

The citation chip is only worth tapping if the sheet behind it does three things: resolves a
`notebook:<label>` to the actual line and hands the EDITED WHOLE notebook back through onSaveNotebook
(one writer, one ETag), shows a `corpus:<id>` past reply read-only via the onLoadReply prop, and says
so honestly when neither resolves. `node --check` can't see any of that — this mounts the shipped
component and drives it. Skips (never lies) when node or jsdom is absent.
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

window.EmailBody = ({ text }) => React.createElement('div', { className: 'email-body' }, text || '');

new Function(fs.readFileSync(path.join(WEB, 'bean-cite.jsx'), 'utf8')).call(window);

const notebook = {
  store: 'Sable & Wren',
  buckets: [{ name: 'Cracked tube', cliff: 'Within 90 days me just replaces it.', stakes: 'normal' }],
  macros: [{ name: 'Shipping times', text: 'Orders ship in 1-2 business days.' }],
  facts: [{ text: 'Free U.S. shipping on orders $120+.', provenance: 'stated' }],
  notes: [{ text: 'Never blame the customer for a cracked tube.', provenance: 'observed' }],
};
const calls = { saved: [], loaded: [], closed: 0 };
const base = {
  notebook,
  onSaveNotebook: nb => { calls.saved.push(nb); return Promise.resolve(nb); },
  onLoadReply: id => { calls.loaded.push(id); return id === 'e7'
    ? Promise.resolve({ email_id: 'e7', subject: 'cracked tube', body: 'it split in the box',
                        reply: 'New tube going out today, no charge.' })
    : Promise.resolve(null); },
  onClose: () => { calls.closed++; },
};

const root = document.getElementById('root');
const mount = (cite) => { ReactDOM.unmountComponentAtNode(root);
  ReactDOM.render(React.createElement(window.BeanCiteSheet, { ...base, cite }), root); };
const buttonWith = (t) => [...root.querySelectorAll('button')].find(b => b.textContent === t);

(async () => {
  // ---- 1. a notebook bucket: resolves, opens editable, saves the WHOLE notebook -----------------
  mount('notebook:Cracked tube');
  const nbMounted = root.textContent;
  const nbFields = { inputs: [...root.querySelectorAll('input')].map(i => i.value),
                     areas: [...root.querySelectorAll('textarea')].map(t => t.value) };
  const area = root.querySelector('textarea');
  const setter = Object.getOwnPropertyDescriptor(window.HTMLTextAreaElement.prototype, 'value').set;
  setter.call(area, 'Within 120 days me just replaces it.');
  area.dispatchEvent(new window.Event('input', { bubbles: true }));
  buttonWith('Save & approve').click();
  await new Promise(r => setTimeout(r, 20));

  // ---- 2. a fact cited by a partial label (the model writes prose, not ids) ---------------------
  mount('notebook:Free U.S. shipping');
  const factMounted = root.textContent;
  const factAreas = [...root.querySelectorAll('textarea')].map(t => t.value);

  // ---- 3. a citation that no longer resolves — named, not faked --------------------------------
  mount('notebook:Something she deleted');
  const missMounted = root.textContent;
  const missEditable = root.querySelectorAll('textarea').length;

  // ---- 4. a past reply, read-only --------------------------------------------------------------
  mount('corpus:e7');
  const corpusLoading = root.textContent;
  await new Promise(r => setTimeout(r, 20));
  const corpusMounted = root.textContent;
  const corpusEditable = root.querySelectorAll('textarea').length + root.querySelectorAll('input').length;

  // ---- 5. a past reply that isn't on file ------------------------------------------------------
  mount('corpus:gone');
  await new Promise(r => setTimeout(r, 20));
  const corpusMissing = root.textContent;

  // ---- 6. the store stub -----------------------------------------------------------------------
  mount('store:prod_42');
  const storeMounted = root.textContent;

  // ---- 7. the chip labels the draft card renders from --------------------------------------
  const chips = ['notebook:Cracked tube', 'corpus:e7', 'store:prod_42', 'a bare line']
    .map(c => { const k = window.beanCite.chipFor(c); return { kind: k.kind, icon: k.icon, text: k.text }; });

  process.stdout.write(JSON.stringify({ nbMounted, nbFields, factMounted, factAreas, missMounted,
    missEditable, corpusLoading, corpusMounted, corpusEditable, corpusMissing, storeMounted, chips, calls }));
})();
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


def test_a_notebook_chip_opens_its_line_editable_and_saves_the_whole_notebook():
    out = _run()
    assert "From your notebook" in out["nbMounted"]
    assert "How I route" in out["nbMounted"]              # it names WHICH section the line lives in
    assert "Cracked tube" in out["nbFields"]["inputs"]
    assert "Within 90 days me just replaces it." in out["nbFields"]["areas"]

    # The save hands back the ENTIRE notebook with just that row patched — same shape BeanNotebook
    # commits, so bean-root's saveNotebook stays the single writer.
    saved = out["calls"]["saved"]
    assert len(saved) == 1, "Save & approve must commit exactly once"
    nb = saved[0]
    assert nb["buckets"][0]["cliff"] == "Within 120 days me just replaces it."
    assert nb["buckets"][0]["name"] == "Cracked tube"     # untouched fields survive
    assert nb["store"] == "Sable & Wren"
    assert nb["macros"][0]["text"] == "Orders ship in 1-2 business days."
    assert nb["facts"] and nb["notes"]
    assert out["calls"]["closed"] == 1                    # a committed save closes the sheet


def test_a_partial_label_still_finds_the_line():
    """The label is model-written prose ('the bucket or fact it came from'), not an id — a chip that
    names only the front of a fact must still open that fact, not the not-found state."""
    out = _run()
    assert "Store facts" in out["factMounted"]
    assert "Free U.S. shipping on orders $120+." in out["factAreas"]


def test_an_unresolvable_citation_is_named_not_faked():
    """An uncited-able citation is itself a grounding smell. Say it plainly and offer no edit box —
    inventing a blank line for her to fill would write a fact Bean never actually leaned on."""
    out = _run()
    assert "can’t find that exact line" in out["missMounted"]
    assert "Something she deleted" in out["missMounted"]
    assert out["missEditable"] == 0


def test_a_corpus_chip_shows_the_past_reply_read_only():
    out = _run()
    assert out["calls"]["loaded"] == ["e7", "gone"]       # resolved through the prop, never a fetch here
    assert "Me’s finding that one…" in out["corpusLoading"]
    assert "cracked tube" in out["corpusMounted"] and "it split in the box" in out["corpusMounted"]
    assert "New tube going out today, no charge." in out["corpusMounted"]
    assert out["corpusEditable"] == 0, "a reply that already went out is a record, not an edit surface"


def test_a_missing_past_reply_says_so_instead_of_showing_an_empty_box():
    out = _run()
    assert "can’t pull that one up" in out["corpusMissing"]


def test_the_chip_label_matches_the_sheet_it_opens():
    """Caught in the browser pass: a `store:` citation rendered as "📎 a past reply" because the chip
    only knew notebook-vs-everything-else. The chip is the operator's at-a-glance read on the
    grounding — a chip that names the wrong source is a small lie about the one thing they're meant
    to trust."""
    out = _run()
    assert out["chips"] == [
        {"kind": "notebook", "icon": "📓", "text": "Cracked tube"},   # names the line it cited
        {"kind": "corpus", "icon": "📎", "text": "a past reply"},
        {"kind": "store", "icon": "⬚", "text": "a store record"},
        # a colon-less citation is all label — read as a notebook line, not dropped
        {"kind": "notebook", "icon": "📓", "text": "a bare line"},
    ]


def test_the_store_chip_is_an_honest_stub():
    """`store:<product_id>` is reserved in the citation contract but has no producer yet (M2). Say
    that rather than render a 404 or an empty record."""
    out = _run()
    assert "prod_42" in out["storeMounted"] and "can’t show store records yet" in out["storeMounted"]
