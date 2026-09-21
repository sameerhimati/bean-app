"""The open-email view, driven through the whole app under jsdom — down to the POST body.

The redesign moved grading into one primary button, "Copy & mark sent", because "Copy" used to
record nothing server-side and grading had fallen from 57 a week to 12. So the assertion that
matters is not that a button exists but what reaches `POST /api/correction` when she presses it:

  * Bean's draft untouched            → action `approve`, final_text = the draft;
  * the draft edited in place first   → action `edit`, original_draft AND final_text (the server
                                         computes edit_ratio from that pair).

It boots every script Bean.html ships (the same shared-global harness as test_bean_app_boot_js),
serves a demo inbox from a stubbed /api/inbox, opens `#draft/<id>` and clicks — so the path under
test is DraftView → bean-root approve() → record() → beanStore.recordCorrection → fetch, the one
the server actually sees. Patching DraftView's callback would prove only that DraftView calls it.

Also pinned here: a red card says each reason ONCE (the summary card used to repeat why[0] above a
list that repeated it again, beside a meter repeating the pill's colour), and the rare actions live
in the ⋯ menu without losing what they post.

Demo data only (Sable & Wren, the public demo store). Skips (never lies) when node or jsdom is absent.
"""

from __future__ import annotations

import json
import os
import re
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

_HIGH_DRAFT = "Hi Tom,\n\nThe Harlow frame carries a 5-year warranty, so you're well within it."
_FLAG_WHY = [
    "Chargeback threat on a third contact — you need to act, not just reply.",
    "The refund has to be issued in Shopify, and mattresses carry a restocking fee.",
    "No past reply like this one; the draft is a holding message.",
]

_INBOX = {"emails": [
    {"id": "e-high", "sender_name": "Tom Ferris", "sender_email": "tom@example.com",
     "subject": "Sofa frame creaking", "received_at": "", "body": "The right arm creaks.",
     "result": {"bucket": "Sofas & Upholstery", "confidence": "green", "draft": _HIGH_DRAFT,
                "citations": ["notebook:Frame warranty", "notebook:Voice rules", "corpus:e7"],
                "why_unsure": []}},
    {"id": "e-low", "sender_name": "Dana Reyes", "sender_email": "dana@example.com",
     "subject": "Will it fit my doorway?", "received_at": "", "body": "Is it 30 inches?",
     "result": {"bucket": "Delivery", "confidence": "yellow", "draft": "Hi Dana, it will fit.",
                "citations": ["notebook:Delivery"], "why_unsure": ["No doorway measurement on file."]}},
    {"id": "e-flag", "sender_name": "Marcus Bell", "sender_email": "marcus@example.com",
     "subject": "Still no refund", "received_at": "", "body": "Third email about my refund.",
     "result": {"bucket": "Escalation", "confidence": "red", "draft": "Hi Marcus, escalating now.",
                "citations": ["notebook:Escalation"], "why_unsure": _FLAG_WHY}},
]}

_HARNESS = r"""
const { JSDOM } = require('jsdom');
const fs = require('fs');
const path = require('path');
const WEB = process.env.BEAN_WEB;
const SCRIPTS = JSON.parse(process.env.BEAN_SCRIPTS);
const INBOX = JSON.parse(process.env.BEAN_INBOX);
const SCENARIO = process.env.BEAN_SCENARIO;
const OPEN = process.env.BEAN_OPEN;

const dom = new JSDOM('<!DOCTYPE html><body><div id="root"></div></body>',
  { url: 'http://localhost/#draft/' + OPEN, runScripts: 'dangerously' });
const { window } = dom;
global.window = window; global.document = window.document; global.navigator = window.navigator;
window.matchMedia = window.matchMedia || (() => ({ matches: false, addListener(){}, removeListener(){},
  addEventListener(){}, removeEventListener(){} }));
window.scrollTo = () => {};
// Onboarding and the demo tour are overlays with their own tests; mark them done so the draft view
// is the only thing on screen.
try { window.localStorage.setItem('bean_onboarded', '1'); } catch (e) {}

// The clipboard: plain-text path only (no ClipboardItem), recording what landed.
const copied = [];
Object.defineProperty(window.navigator, 'clipboard', { configurable: true, value: {
  writeText: (t) => { copied.push(t); return Promise.resolve(); },
} });

const READS = {
  '/api/status': {}, '/api/config': {}, '/api/notebook': null, '/api/notebook/review': {},
  '/api/inbox': INBOX, '/api/learning': {}, '/api/corrections': [], '/api/meta': {},
  '/whats-new.json': { entries: [] },
};
const posts = [];
window.fetch = (url, opts) => {
  const p = String(url).split('?')[0];
  if (opts && opts.method && opts.method !== 'GET') posts.push({ url: p, body: JSON.parse(opts.body || 'null') });
  const body = p in READS ? READS[p] : {};
  return Promise.resolve({ ok: true, status: 200, headers: { get: () => null },
    json: () => Promise.resolve(body), text: () => Promise.resolve(JSON.stringify(body)) });
};
const errors = [];
window.console.error = (...a) => errors.push(a.map(String).join(' '));
dom.virtualConsole.on('jsdomError', e => errors.push('uncaught: ' + (e && (e.detail && e.detail.stack || e.message) || e)));

const runScript = (code) => { const el = document.createElement('script'); el.textContent = code; document.body.appendChild(el); };
runScript(fs.readFileSync(path.join(WEB, 'vendor', 'react.production.min.js'), 'utf8'));
runScript(fs.readFileSync(path.join(WEB, 'vendor', 'react-dom.production.min.js'), 'utf8'));
for (const src of SCRIPTS) {
  const p = path.join(WEB, src);
  runScript(src === 'bean-data.jsx' && !fs.existsSync(p)
    ? 'window.EMAILS = []; window.CONFIG = {}; window.SETTINGS = {};' : fs.readFileSync(p, 'utf8'));
}

const root = document.getElementById('root');
const tick = (ms) => new Promise(r => setTimeout(r, ms || 30));
const buttonWith = (t) => [...root.querySelectorAll('button')].find(b => b.textContent === t);
const setValue = (el, v) => {
  Object.getOwnPropertyDescriptor(window.HTMLTextAreaElement.prototype, 'value').set.call(el, v);
  el.dispatchEvent(new window.Event('input', { bubbles: true }));
};
const corrections = () => posts.filter(p => p.url === '/api/correction').map(p => p.body);

(async () => {
  await tick(300);  // mount, load the inbox, open the draft
  const out = { errors, scenario: SCENARIO };
  out.opened = !!root.querySelector('.draft-view .dv-bar');
  out.subject = (root.querySelector('.dv-title h2') || {}).textContent || '';

  if (SCENARIO === 'untouched') {
    buttonWith('⧉ Copy & mark sent').click();
    await tick(60);
  } else if (SCENARIO === 'refused') {
    // Both clipboard paths refuse, the way a browser does outside a user gesture: the async write
    // rejects, and the legacy execCommand fallback returns false instead of throwing.
    window.navigator.clipboard.writeText = () => Promise.reject(new Error('NotAllowedError'));
    document.execCommand = () => false;
    buttonWith('⧉ Copy & mark sent').click();
    await tick(60);
    out.notice = (root.querySelector('.dv-bar') || {}).textContent || '';
  } else if (SCENARIO === 'edited') {
    buttonWith('✎ Edit').click();
    await tick();
    out.editorInDraftBox = !!root.querySelector('.draft-box .draft-edit');
    setValue(root.querySelector('.draft-edit'), 'Hi Tom — replacing the arm under warranty today.');
    setValue(root.querySelector('#bean-note'), 'offer the replacement, not just the warranty');
    await tick();
    buttonWith('⧉ Copy & mark sent').click();
    await tick(60);
  } else if (SCENARIO === 'flag') {
    out.text = root.textContent;
    out.whyBlocks = root.querySelectorAll('.why-block').length;
    out.hasMeter = typeof window.ConfidenceMeter !== 'undefined' || /\d\/5\b/.test(root.textContent);
    out.pills = [...root.querySelectorAll('.conf-badge')].map(b => b.textContent);
  } else if (SCENARIO === 'menu') {
    out.beforeOpen = !!buttonWith('↩ Should’ve been filed');
    root.querySelector('button[aria-label="More actions"]').click();
    await tick();
    out.afterOpen = !!buttonWith('↩ Should’ve been filed');
    out.menuHasCategory = !!root.querySelector('.dv-menu select.category-select');
    buttonWith('↩ Should’ve been filed').click();
    await tick(60);
  }
  out.copied = copied;
  out.corrections = corrections();
  process.stdout.write(JSON.stringify(out));
  process.exit(0);
})();
"""


def _script_tags() -> list[str]:
    html = (_WEB / "Bean.html").read_text(encoding="utf-8")
    return re.findall(r'<script src="(?!/vendor/)([^"]+\.jsx)"></script>', html)


def _run(scenario: str, open_id: str) -> dict:
    proc = subprocess.run(
        ["node", "-e", _HARNESS], cwd=str(_JS), capture_output=True, text=True, timeout=120,
        env={**os.environ, "BEAN_WEB": str(_WEB), "BEAN_SCRIPTS": json.dumps(_script_tags()),
             "BEAN_INBOX": json.dumps(_INBOX), "BEAN_SCENARIO": scenario, "BEAN_OPEN": open_id},
    )
    assert proc.returncode == 0, f"harness failed:\nSTDOUT:{proc.stdout}\nSTDERR:{proc.stderr}"
    out = json.loads(proc.stdout)
    assert out["opened"], f"#draft/{open_id} did not open the draft view: {out}"
    assert not out["errors"], "\n".join(out["errors"])
    return out


def test_the_primary_on_an_untouched_draft_copies_it_and_posts_approve():
    out = _run("untouched", "e-high")
    assert out["copied"] == [_HIGH_DRAFT], "the primary must put the draft on the clipboard"
    [row] = out["corrections"]
    assert row["action"] == "approve"
    assert row["email_id"] == "e-high"
    assert row["final_text"] == _HIGH_DRAFT
    assert row.get("original_draft") is None


def test_the_primary_after_an_edit_posts_edit_with_both_texts():
    out = _run("edited", "e-high")
    # Edit swaps a textarea into the SAME box — no separate pane, no second approve step.
    assert out["editorInDraftBox"]
    edited = "Hi Tom — replacing the arm under warranty today."
    assert out["copied"] == [edited], "what gets copied is what she edited, not Bean's draft"
    [row] = out["corrections"]
    assert row["action"] == "edit"
    assert row["original_draft"] == _HIGH_DRAFT
    assert row["final_text"] == edited
    # The note sits by the draft and rides the same row.
    assert row["note"] == "offer the replacement, not just the warranty"


def test_a_red_card_says_each_reason_once_and_has_no_meter():
    out = _run("flag", "e-flag")
    for reason in _FLAG_WHY:
        assert out["text"].count(reason) == 1, f"said {out['text'].count(reason)}× — {reason!r}"
    assert out["whyBlocks"] == 1
    assert out["hasMeter"] is False, "the 5-block meter repeated what the pill already says"
    assert out["pills"] == ["Needs you"], "confidence is shown once, as the pill"
    assert out["corrections"] == [], "opening an email must not grade it"


def test_should_have_been_filed_lives_in_the_menu_and_still_posts():
    out = _run("menu", "e-low")
    assert out["beforeOpen"] is False, "rare actions stay in ⋯, out of the send's way"
    assert out["afterOpen"] is True
    assert out["menuHasCategory"], "Change category moved into ⋯ too"
    [row] = out["corrections"]
    assert row["action"] == "should-file"
    assert row["category"] == "Filed / FYI"
    assert row["meta"]["model_category"] == "Delivery"
    assert out["copied"] == [], "filing it must not touch the clipboard"


def test_a_refused_copy_grades_nothing_and_says_so():
    # The regression: the legacy fallback returned true whenever nothing threw, so a refused copy
    # (execCommand → false) still logged "approve" and moved on with an empty clipboard.
    out = _run("refused", "e-high")
    assert out["corrections"] == [], "a copy that never landed must not be graded as sent"
    assert "me marked nothing sent" in out["notice"]
