"""Render smoke for web/bean-proposal.jsx — the one idiom every notebook change arrives through.

What must hold: the claim renders in HER language with provenance translated
(the internal words never reach her), a confirm reports the edited text when she fixed it first, a
decline writes nothing and says so, and both leave a payoff row rather than silently vanishing.
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

new Function(fs.readFileSync(path.join(WEB, 'bean-proposal.jsx'), 'utf8')).call(window);

const calls = { confirmed: [], declined: 0, fixed: 0 };
const root = document.getElementById('root');
const CLAIM = 'me think: when something arrives broken, you get a photo and send a replacement — no return needed.';
const mount = (extra) => { ReactDOM.unmountComponentAtNode(root);
  ReactDOM.render(React.createElement(window.ProposalCard, {
    claim: CLAIM, provenance: 'observed', receipts: '4 emails like this one',
    consequence: 'Me would have drafted the replacement offer on Marie’s cracked-tube email.',
    onConfirm: t => calls.confirmed.push(t),
    onFix: () => { calls.fixed++; },
    onDecline: () => { calls.declined++; },
    ...(extra || {}),
  }), root); };
const buttonWith = (t) => [...root.querySelectorAll('button')].find(b => b.textContent === t);
const setValue = (el, v) => {
  Object.getOwnPropertyDescriptor(window.HTMLTextAreaElement.prototype, 'value').set.call(el, v);
  el.dispatchEvent(new window.Event('input', { bubbles: true }));
};

// ---- 1. plain confirm ---------------------------------------------------------------------
mount();
const mounted = root.textContent;
buttonWith('✓ that’s right').click();
const afterConfirm = root.textContent;

// ---- 2. fix, then confirm — the confirm must carry HER wording, not Bean's ------------------
mount();
buttonWith('✎ fix it').click();
const editing = { areas: [...root.querySelectorAll('textarea')].map(t => t.value),
                  text: root.textContent };
setValue(root.querySelector('textarea'), 'photo first, then a replacement — and me always apologises.');
buttonWith('✓ Save my version').click();
const afterFix = root.textContent;

// ---- 3. decline — writes nothing, and says so ----------------------------------------------
mount();
buttonWith('not now').click();
const afterDecline = root.textContent;

// ---- 4. a proposal with no consequence and no fix affordance (Bean has no honest example) ----
mount({ consequence: null, onFix: false, confirmLabel: '✓ yes, that’s me', declineLabel: 'skip' });
const bare = { text: root.textContent, hasFix: !!buttonWith('✎ fix it') };

process.stdout.write(JSON.stringify({ mounted, afterConfirm, editing, afterFix, afterDecline, bare, calls }));
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


def test_the_card_speaks_her_language_and_shows_its_receipts():
    out = _run()
    m = out["mounted"]
    assert "when something arrives broken" in m
    assert "me saw it in your replies" in m, "provenance must render translated, never as 'observed'"
    assert "observed" not in m, "the internal vocabulary must never reach her"
    assert "4 emails like this one" in m
    assert "Marie’s cracked-tube email" in m  # the consequence preview: what a confirm changes


def test_a_confirm_reports_the_claim_and_leaves_a_payoff():
    out = _run()
    assert out["calls"]["confirmed"][0].startswith("me think: when something arrives broken")
    assert "Got it — me follows this now." in out["afterConfirm"]


def test_fixing_first_confirms_her_wording_not_beans():
    """'✎ fix it' is not a separate save path — it edits the claim in place and the SAME confirm
    reports it. If it reported Bean's original, her correction would be silently discarded."""
    out = _run()
    assert out["calls"]["fixed"] == 1
    assert out["editing"]["areas"] == ["me think: when something arrives broken, you get a photo and send a replacement — no return needed."]
    confirmed = out["calls"]["confirmed"]
    assert confirmed[-1] == "photo first, then a replacement — and me always apologises."
    assert "Got it — me follows this now." in out["afterFix"]


def test_a_decline_writes_nothing_and_says_so():
    """'Bean may propose nothing' cuts both ways — declining must be a real, visible outcome, not a
    card that just disappears leaving her unsure whether it got saved anyway."""
    out = _run()
    assert out["calls"]["declined"] == 1
    assert out["calls"]["confirmed"] == ["me think: when something arrives broken, you get a photo and send a replacement — no return needed.",
                                         "photo first, then a replacement — and me always apologises."]
    assert "Left it out. Me wrote nothing down." in out["afterDecline"]


def test_consequence_and_fix_are_both_optional():
    """Bean often has no honest example of what a confirm changes; inventing one is the yes-man move.
    A card without a consequence (or without an inline fix) must still render and confirm."""
    out = _run()
    assert "Marie" not in out["bare"]["text"]
    assert out["bare"]["hasFix"] is False
    assert "✓ yes, that’s me" in out["bare"]["text"] and "skip" in out["bare"]["text"]
