"""The "Should've been filed" gesture, rendered from the real web/bean-draft.jsx under jsdom.

This exists because of one live incident: Bean drafted a full reply to a follow-up on a thread the
operator had already answered, they typed *"This should be in FYI/Filed as this was sent as a follow
up to an original email"* into the comment box — and there was no button to submit it. The comment
only ever rides an action, and on an email they'll neither send nor snooze there was no action to
ride.

So the assertion that matters is not "a button exists". It is **the typed comment reaches the
correction payload**, on every drafted state (green / yellow / red-with-draft / red-without-draft).
A button that files the email but drops that note would leave the original bug in place while
looking fixed. Skips (never lies) when node or jsdom is absent.
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

_LABEL = "↩ Should’ve been filed"

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

// Bean.html's load order for the slice DraftView needs: pixel (BeanMark), ui (Btn/CONF/CopyButton/
// order meter), email (linkifyText), cite (beanCite.chipFor), then the module under test. Same
// shared-global idiom as the app — these files call each other bare, not through window.*.
for (const f of ['bean-pixel.jsx', 'bean-ui.jsx', 'bean-email.jsx', 'bean-cite.jsx', 'bean-draft.jsx']) {
  new Function(fs.readFileSync(path.join(WEB, f), 'utf8')).call(window);
}

const root = document.getElementById('root');
const buttonWith = (t) => [...root.querySelectorAll('button')].find(b => b.textContent === t);
const setValue = (el, v) => {
  Object.getOwnPropertyDescriptor(window.HTMLTextAreaElement.prototype, 'value').set.call(el, v);
  el.dispatchEvent(new window.Event('input', { bubbles: true }));
};

const NOTE = 'This should be in FYI/Filed as this was sent as a follow up to an original email';
const CONFIG = { categories: [{ name: 'Needs a human' }, { name: 'Orders' }] };

// The four drafted states. `filed: true` is deliberately absent — that path is FiledState, which
// already has its own mirror ("↩ This needs a reply") and must NOT grow this one.
const STATES = {
  high:      { confidence: 'high', draft: 'Hi Amanda, it shipped Tuesday.', citations: ['notebook:Orders'] },
  low:       { confidence: 'low',  draft: 'Hi Amanda, it shipped Tuesday.', concerns: ['no tracking on file'] },
  redDraft:  { confidence: 'flag', draft: 'Hi Amanda, sorry about that!',   concerns: ['question never stated'] },
  redNoDraft:{ confidence: 'flag', draft: '',                              concerns: ['nothing on file to lean on'] },
};

const out = {};
for (const [name, extra] of Object.entries(STATES)) {
  const seen = [];
  const email = {
    id: 'e-' + name, category: 'Needs a human', subject: 'Re: Auto: Inventory Report',
    body: ['Hi my question is in regards to my recent purchase. Order number: SW45535'],
    from: { name: 'Amanda Johnson', email: 'amanda@example.com' },
    summary: 'A follow-up with no question in it.', citations: [], concerns: [], ...extra,
  };
  ReactDOM.unmountComponentAtNode(root);
  ReactDOM.render(React.createElement(window.DraftView, {
    email, config: CONFIG, notebook: null,
    onApprove: x => seen.push(['approve', x]),
    onSkip:    x => seen.push(['skip', x]),
    onHandle:  x => seen.push(['handle', x]),
    onShouldFile: x => seen.push(['should-file', x]),
    onTeach: () => {}, onKeepFiled: () => {}, onNeedsReply: () => {},
    onBack: () => {}, onCite: () => {},
  }), root);

  // The gesture lives in the ⋯ menu (rare actions stay out of the send's way), so open it first —
  // exactly as she has to.
  const more = root.querySelector('button[aria-label="More actions"]');
  if (more) more.click();
  const btn = buttonWith(BUTTON_LABEL);
  const present = !!btn;
  // Type into the "Tell Bean why" note exactly as she did, then file.
  const note = root.querySelector('#bean-note');
  if (note) setValue(note, NOTE);
  if (btn) buttonWith(BUTTON_LABEL).click();
  out[name] = { present, calls: seen };
}

// The filed card must keep exactly its own two gestures and never gain this one.
{
  ReactDOM.unmountComponentAtNode(root);
  ReactDOM.render(React.createElement(window.DraftView, {
    email: { id: 'f1', filed: true, gateKind: 'newsletter', gateReason: 'A newsletter.',
             subject: 'Weekly digest', body: ['unsubscribe'], from: { name: 'News', email: 'n@x.com' } },
    config: CONFIG, notebook: null,
    onApprove: () => {}, onSkip: () => {}, onHandle: () => {}, onShouldFile: () => {},
    onTeach: () => {}, onKeepFiled: () => {}, onNeedsReply: () => {}, onBack: () => {}, onCite: () => {},
  }), root);
  out.filed = { present: !!buttonWith(BUTTON_LABEL), hasMirror: !!buttonWith('↩ This needs a reply') };
}

console.log(JSON.stringify(out));
"""


@pytest.fixture(scope="module")
def rendered(tmp_path_factory):
    label = json.dumps(_LABEL)
    script = tmp_path_factory.mktemp("js") / "should_file.js"
    script.write_text(f"const BUTTON_LABEL = {label};\n" + _HARNESS, encoding="utf-8")
    proc = subprocess.run(
        ["node", str(script)],
        capture_output=True, text=True,
        env={"PATH": __import__("os").environ["PATH"], "BEAN_WEB": str(_WEB),
             "NODE_PATH": str(_JS / "node_modules")},
    )
    assert proc.returncode == 0, f"harness failed:\n{proc.stderr}"
    return json.loads(proc.stdout.strip().splitlines()[-1])


DRAFTED = ["high", "low", "redDraft", "redNoDraft"]


@pytest.mark.parametrize("state", DRAFTED)
def test_every_drafted_state_offers_the_gesture(rendered, state):
    # Including redNoDraft: "Bean had nothing to say" and "this needed no reply" are different
    # verdicts, and only she can tell them apart.
    assert rendered[state]["present"], f"{state} card has no “{_LABEL}” button"


@pytest.mark.parametrize("state", DRAFTED)
def test_the_gesture_carries_her_typed_comment(rendered, state):
    # THE regression this feature exists for. The button is the FeedbackBar's submit; if the note
    # doesn't ride the action, her feedback still has nowhere to go and the bug is unfixed.
    calls = rendered[state]["calls"]
    assert [c[0] for c in calls] == ["should-file"], f"{state}: expected one should-file, got {calls}"
    payload = calls[0][1]
    assert payload["note"].startswith("This should be in FYI/Filed")
    assert payload["liked"] is False
    # The category rides too, so bean-root can pair Bean's bucket against her verdict.
    assert payload["category"] == "Needs a human"


def test_filed_mail_keeps_its_own_mirror_and_does_not_gain_this_one(rendered):
    # The two gestures are opposites on opposite cards. A filed email offers "↩ This needs a reply";
    # offering "should've been filed" there would be a no-op that implies Bean did something wrong.
    assert rendered["filed"]["hasMirror"] is True
    assert rendered["filed"]["present"] is False
