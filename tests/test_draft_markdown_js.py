"""No markdown syntax may reach a customer — the copy path and the preview, tested under node.

This exists because it already happened. The drafting prompt said nothing about output format, so the
model sometimes wrote markdown: **3 of 9** real drafts carried it, nothing rendered it, and a real
customer was sent

    try entering **15106534576** or just **5106534576**

on a reply the operator APPROVED untouched — because `**` is exactly what a quick read slides over.
It is a one-tap-approve product; a defect that survives a quick read is the worst kind it can have.

`bean/engine.py` now forbids markdown outright, which is the real fix. These are the seatbelt, and
they assert the two things that must hold even when the model ignores the instruction:

  1. the CLIPBOARD never carries markdown syntax — html gets real <strong>, plain gets the asterisks
     removed, because a plain-text target can't render bold and would show the customer the syntax;
  2. the PREVIEW agrees with the clipboard. A box captioned "as it'll send" that shows asterisks the
     copy silently strips is a box that lies about the one thing it's for.

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

# The real reply, verbatim from the production correction log.
_REAL = "try entering **15106534576** or just **5106534576** - If the field has a space"

_HARNESS = r"""
const { JSDOM } = require('jsdom');
const fs = require('fs');
const path = require('path');
const WEB = process.env.BEAN_WEB;

const dom = new JSDOM('<!DOCTYPE html><body><div id="root"></div></body>', { url: 'http://localhost' });
const { window } = dom;
global.window = window; global.document = window.document; global.navigator = window.navigator;

function loadVendor(f) {
  new Function('window','self','globalThis','module','exports','require',
    fs.readFileSync(path.join(WEB, 'vendor', f), 'utf8'))
    .call(window, window, window, window, undefined, undefined, undefined);
}
loadVendor('react.production.min.js'); loadVendor('react-dom.production.min.js');
global.React = window.React; global.ReactDOM = window.ReactDOM;

for (const f of ['bean-pixel.jsx','bean-ui.jsx','bean-email.jsx','bean-cite.jsx','bean-draft.jsx']) {
  new Function(fs.readFileSync(path.join(WEB, f), 'utf8')).call(window);
}

const SRC = process.env.BEAN_DRAFT;

// --- what lands on the clipboard -------------------------------------------------------------
// Capture both flavors by stubbing the async clipboard, then clicking the real CopyButton.
let flavors = null;
window.ClipboardItem = function (dict) { Object.assign(this, dict); };
navigator.clipboard = {
  write: (items) => { flavors = items[0]; return Promise.resolve(); },
  writeText: (t) => { flavors = { plainOnly: t }; return Promise.resolve(); },
};
// Blob.text() is async in jsdom; stash the raw strings at construction instead. Must be set on
// BOTH globals — the .jsx files run with `this === window` but reference `Blob` bare, so it
// resolves up the scope chain to node's global rather than to the window stub.
const seen = {};
function BlobStub(parts, opts) { seen[(opts && opts.type) || 'text/plain'] = parts.join(''); }
window.Blob = BlobStub; global.Blob = BlobStub;
global.ClipboardItem = window.ClipboardItem;

const root = document.getElementById('root');
ReactDOM.render(React.createElement(window.CopyButton, { text: SRC }), root);
root.querySelector('button').click();

// --- what the preview shows ------------------------------------------------------------------
// Through DraftView, the real entrypoint — DraftBox is module-local, and a test that reaches past
// the shipped seam proves less than one that goes through it.
ReactDOM.unmountComponentAtNode(root);
ReactDOM.render(React.createElement(window.DraftView, {
  email: { id: 'e1', category: 'Product Recommendation', subject: 'Which one?',
           body: ['Which cord fits?'], from: { name: 'Mel', email: 'mel@example.com' },
           summary: 'A product question.', confidence: 'high', draft: SRC,
           citations: [], concerns: [] },
  config: { categories: [{ name: 'Product Recommendation' }] }, notebook: null,
  onApprove: () => {}, onSkip: () => {}, onHandle: () => {}, onShouldFile: () => {},
  onTeach: () => {}, onKeepFiled: () => {}, onNeedsReply: () => {}, onBack: () => {}, onCite: () => {},
}), root);
const preview = root.querySelector('.draft-text');

// --- what a MANUAL Cmd+C out of the box puts on the clipboard --------------------------------
// The display wears Bean's Courier again, so this handler is the only thing keeping a hand copy
// from carrying a typewriter font into the composer. Fire a real copy event at the rendered node.
const manual = {};
const ev = new window.Event('copy', { bubbles: true, cancelable: true });
ev.clipboardData = { setData: (mime, v) => { manual[mime] = v; } };
preview.dispatchEvent(ev);

console.log(JSON.stringify({
  html: seen['text/html'] || '',
  plain: seen['text/plain'] || '',
  previewText: preview ? preview.textContent : '',
  previewStrong: preview ? [...preview.querySelectorAll('strong')].map(s => s.textContent) : [],
  previewFont: window.getComputedStyle(preview).fontFamily,
  manualHtml: manual['text/html'] || '',
  manualPlain: manual['text/plain'] || '',
  strip: window.stripMarkdown(SRC),
}));
"""


@pytest.fixture(scope="module")
def out(tmp_path_factory):
    script = tmp_path_factory.mktemp("js") / "md.js"
    script.write_text(_HARNESS, encoding="utf-8")
    import os
    proc = subprocess.run(
        ["node", str(script)], capture_output=True, text=True,
        env={"PATH": os.environ["PATH"], "BEAN_WEB": str(_WEB),
             "NODE_PATH": str(_JS / "node_modules"), "BEAN_DRAFT": _REAL},
    )
    assert proc.returncode == 0, f"harness failed:\n{proc.stderr}"
    return json.loads(proc.stdout.strip().splitlines()[-1])


def test_the_plain_clipboard_flavor_carries_no_asterisks(out):
    # THE regression. This exact string reached a real customer.
    assert "**" not in out["plain"], out["plain"]
    assert "15106534576" in out["plain"], "the number itself must survive"


def test_the_html_clipboard_flavor_promotes_bold_instead_of_showing_syntax(out):
    assert "**" not in out["html"], out["html"]
    assert "<strong>15106534576</strong>" in out["html"]


def test_the_preview_shows_what_the_copy_sends(out):
    # A box captioned "as it'll send" must not show syntax the clipboard removes.
    assert "**" not in out["previewText"], out["previewText"]
    assert out["previewStrong"] == ["15106534576", "5106534576"]


def test_strip_leaves_ordinary_text_alone(out):
    assert out["strip"].startswith("try entering 15106534576 or just 5106534576")


# ---- the manual Cmd+C path --------------------------------------------------------------------
# The draft box wears Bean's own Courier on screen. That is only safe because a hand-selected copy
# is intercepted and re-authored; without the handler, the rendered typeface goes into the composer
# and every reply needs reformatting by hand — which is a real tax that was already paid once.

def test_a_manual_copy_carries_the_email_font_not_the_screen_font(out):
    assert "-apple-system" in out["manualHtml"], out["manualHtml"][:200]
    assert "14pt" in out["manualHtml"]
    assert "Courier" not in out["manualHtml"]


def test_a_manual_copy_carries_no_markdown_either(out):
    assert "**" not in out["manualHtml"] and "**" not in out["manualPlain"]
    assert "<strong>15106534576</strong>" in out["manualHtml"]
    assert "15106534576" in out["manualPlain"]


def test_the_preview_itself_is_beans_typeface():
    """The payoff of the handler: display and clipboard are decoupled, so the box can look like Bean.

    Asserted against the stylesheet rather than a computed style — the jsdom harness above loads the
    .jsx files but not Bean.html's CSS, so `getComputedStyle` there reports nothing and would pass
    on any rule at all. `.draft-text` must NOT pin a sans-serif family: it inherits Bean's Courier,
    and what reaches the composer is authored by the copy paths instead.
    """
    css = (_WEB / "Bean.html").read_text(encoding="utf-8")
    rule = next(l for l in css.splitlines() if l.strip().startswith(".draft-text {"))
    assert "font-family" not in rule, f"draft-text pins a font again: {rule.strip()}"
    # ...and the clipboard side must still be carrying the email font, or decoupling them was a loss.
    assert "-apple-system" in (_WEB / "bean-ui.jsx").read_text(encoding="utf-8")
