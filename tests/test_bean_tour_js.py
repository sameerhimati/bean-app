"""The public demo's guided tour (web/bean-tour.jsx), booted through the real page.

Two things are worth a test here and neither is "does the copy look right".

**It must never appear for the operator.** The tour is a coach-mark walk over an inbox, addressed to
somebody who has never seen Bean. Firing it on the real tenant would interrupt her morning triage to
explain her own mail to her, which is the same mistake `onboarded` already sidesteps on the demo
(bean-root.jsx). One env var stands between those two worlds, so the off-case gets a test of its own
rather than a comment saying it is fine.

**Its targets resolve after the commit, not during render.** BeanTour mounts in the SAME React commit
as the inbox rows it points at. Resolving `document.querySelector` in a `useMemo` — the obvious way
to write it — runs during render, before React has put a single row in the document: every targeted
step would be filtered out, the tour would silently degrade to its two untargeted steps, and nothing
would throw. That is a bug you only ever catch by counting the steps, so this counts them.

The harness is the one from test_bean_app_boot_js.py: every script Bean.html ships, in Bean.html's
order, in one shared global scope, because that is how the browser runs them and the .jsx files call
each other's top-level functions bare.

Skips (never lies) when node or jsdom is absent.
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


def _script_tags() -> list[str]:
    html = (_WEB / "Bean.html").read_text(encoding="utf-8")
    return re.findall(r'<script src="(?!/vendor/)([^"]+\.jsx)"></script>', html)


def _email(eid: str, name: str, confidence: str) -> dict:
    """One /api/inbox record, in the shape bean/server.py serves and `applyResult` maps."""
    return {
        "id": eid, "sender_name": name, "sender_email": f"{eid}@example.com",
        "subject": f"Subject for {eid}", "body": "Body.", "received_at": "Thu, 02 Jul 2026 09:12:00 -0700",
        "result": {
            "bucket": "Orders", "confidence": confidence, "draft": "A draft.",
            "citations": [], "why_unsure": [] if confidence == "green" else ["me not sure"],
        },
    }


_HARNESS = r"""
const { JSDOM } = require('jsdom');
const fs = require('fs');
const path = require('path');
const WEB = process.env.BEAN_WEB;
const SCRIPTS = JSON.parse(process.env.BEAN_SCRIPTS);
const INBOX = JSON.parse(process.env.BEAN_INBOX);
const DEMO = process.env.BEAN_DEMO === '1';

const dom = new JSDOM('<!DOCTYPE html><body><div id="root"></div></body>',
  { url: 'http://localhost/', runScripts: 'dangerously' });
const { window } = dom;
global.window = window; global.document = window.document; global.navigator = window.navigator;
window.matchMedia = window.matchMedia || (() => ({ matches: false, addListener(){}, removeListener(){},
  addEventListener(){}, removeEventListener(){} }));
window.scrollTo = () => {};
// jsdom has no layout, so Element.prototype.scrollIntoView does not exist. The tour calls it on every
// targeted step; without this the step throws and the failure reads as a tour bug rather than a
// harness gap.
window.Element.prototype.scrollIntoView = function () {};

// The one switch this whole file is about. Set before any script runs, exactly as the server does:
// it writes the flag into the synthesized bean-data.jsx, which loads before bean-root.
window.BEAN_DEMO_TENANT = DEMO;

const runScript = (code) => {
  const el = window.document.createElement('script');
  el.textContent = code;
  window.document.body.appendChild(el);
};

// Non-GET calls are recorded, not just stubbed: a tour that quietly wrote something would defeat the
// point of running it on a read-only deployment.
const writes = [];
const EMPTY = {
  '/api/status': {}, '/api/config': {}, '/api/notebook': null, '/api/notebook/review': {},
  '/api/inbox': { emails: INBOX }, '/api/learning': {}, '/api/corrections': [], '/api/meta': {},
  '/whats-new.json': { entries: [{ date: '2026-08-19', title: 'A release note', body: 'Body.' }] },
};
window.fetch = (url, opts) => {
  const p = String(url).split('?')[0];
  if (opts && opts.method && opts.method !== 'GET') writes.push(opts.method + ' ' + p);
  const body = p in EMPTY ? EMPTY[p] : {};
  return Promise.resolve({
    ok: true, status: 200, headers: { get: () => null },
    json: () => Promise.resolve(body), text: () => Promise.resolve(JSON.stringify(body)),
  });
};

const errors = [];
window.console.error = (...a) => errors.push(a.map(String).join(' '));
dom.virtualConsole.on('jsdomError', e => errors.push('uncaught: ' + (e && (e.detail && e.detail.stack || e.message) || e)));
window.addEventListener('error', e => errors.push('window.onerror: ' + (e.message || e)));
window.addEventListener('unhandledrejection', e => errors.push('unhandled rejection: ' + (e.reason && e.reason.message || e.reason)));

// ⚠️ web/bean-data.jsx IS GITIGNORED, so it does not exist in a clone — and Bean.html loads it by
// name. Reading it blind is an ENOENT that kills the whole harness, which is why every jsdom test
// in this repo failed for anyone who cloned it, silently, from the day the public tree was cut.
//
// Absent is not an error state, it is the DEPLOYED state: the server synthesizes this file when no
// baked fixture is present (bean/server.py:_serve_data_jsx), and a real deployment therefore always
// runs the synthesized one. So the missing-file path is the shipped path, and standing in for it
// here makes the harness MORE faithful rather than more forgiving — the baked file on a developer's
// laptop is the special case.
const dataStandIn = 'window.EMAILS = []; window.CONFIG = {}; window.SETTINGS = {};';
const readOrSynthesize = (src) => {
  const p = path.join(WEB, src);
  if (src === 'bean-data.jsx' && !fs.existsSync(p)) return dataStandIn;
  return fs.readFileSync(p, 'utf8');
};

runScript(fs.readFileSync(path.join(WEB, 'vendor', 'react.production.min.js'), 'utf8'));
runScript(fs.readFileSync(path.join(WEB, 'vendor', 'react-dom.production.min.js'), 'utf8'));
global.React = window.React; global.ReactDOM = window.ReactDOM;
for (const src of SCRIPTS) {
  runScript(readOrSynthesize(src));
  // ⚠️ THE COMMITTED web/bean-data.jsx IS NOT WHAT A DEMO DEPLOYMENT SERVES, and skipping this line
  // makes every assertion below meaningless. That file carries a full baked `window.EMAILS`, so the
  // inbox is already populated at first render and the tour resolves its targets against the FIXTURE
  // — the stubbed /api/inbox never touches it, and a one-email inbox still reports "1 of 5". A real
  // demo gets a SYNTHESIZED bean-data.jsx with `window.EMAILS = []` (bean/server.py:_serve_data_jsx),
  // precisely so the live inbox is the only inbox. Reproduce that, or test the wrong page.
  if (DEMO && src === 'bean-data.jsx') runScript('window.EMAILS = [];');
}

const q = (s) => window.document.querySelector(s);
const snap = () => {
  const tour = q('.bean-tour');
  const spot = q('.bean-tour-spot');
  return {
    present: !!tour,
    count: tour ? (q('.bean-tour-count') || {}).textContent : null,
    title: tour ? (q('.bean-tour h3') || {}).textContent : null,
    next: tour ? (q('.bean-tour-next') || {}).textContent : null,
    spotConf: spot ? spot.getAttribute('data-conf') : null,
  };
};
const click = (el) => el.dispatchEvent(new window.MouseEvent('click', { bubbles: true }));
const tick = (ms) => new Promise(r => setTimeout(r, ms));

// The inbox arrives over fetch and the tour mounts on the render after it lands, so this needs more
// than one microtask turn to settle. Then walk the whole tour the way a visitor does — Next until it
// closes — because "the tour writes nothing" is a claim about what it does, not about how it looks.
(async () => {
  await tick(300);
  const first = snap();
  const walk = [first];
  const writesOnArrival = writes.slice();
  // Captured HERE and not at the end: the question is whether the nub and the tour bar are on screen
  // at the same time, and by the end the tour is gone so the answer would always be "no".
  const hintOnArrival = !!q('.beanary-hint');
  for (let n = 0; n < 12 && q('.bean-tour-next'); n++) {
    click(q('.bean-tour-next'));
    await tick(40);
    if (q('.bean-tour')) walk.push(snap());
  }
  await tick(60);
  process.stdout.write(JSON.stringify({
    errors, writes, writesOnArrival,
    present: first.present, count: first.count, title: first.title, spotConf: first.spotConf,
    walk,
    rows: window.document.querySelectorAll('.inbox-row').length,
    whatsNewCard: !!q('.whatsnew-card'),
    beanaryHint: hintOnArrival,
    // After the walk: the bar is gone, the ring went with it, the visitor is marked as told, and the
    // Beanary nub is allowed back.
    endTour: !q('.bean-tour'), endSpot: !q('.bean-tour-spot'),
    endSeen: window.localStorage.getItem('bean_tour_seen'),
    endHint: !!q('.beanary-hint'),
  }));
  process.exit(0);
})();
"""

# A full spread: one of every bucket, so all five steps have somewhere to point.
_FULL = [
    _email("e-green", "Green Sender", "green"),
    _email("e-yellow", "Yellow Sender", "yellow"),
    _email("e-red", "Red Sender", "red"),
]


def _run(inbox: list[dict], demo: bool = True) -> dict:
    proc = subprocess.run(
        ["node", "-e", _HARNESS],
        cwd=str(_JS), capture_output=True, text=True, timeout=120,
        env={**os.environ, "BEAN_WEB": str(_WEB), "BEAN_SCRIPTS": json.dumps(_script_tags()),
             "BEAN_INBOX": json.dumps(inbox), "BEAN_DEMO": "1" if demo else "0"},
    )
    assert proc.returncode == 0, f"harness failed:\nSTDOUT:{proc.stdout}\nSTDERR:{proc.stderr}"
    out = json.loads(proc.stdout)
    assert not out["errors"], "the page rendered with errors:\n" + "\n".join(out["errors"])
    return out


def test_the_tour_never_shows_up_for_a_real_tenant():
    """The guard that matters. BEAN_DEMO_TENANT off ⇒ no tour, no matter what is in the inbox."""
    out = _run(_FULL, demo=False)
    assert out["rows"] > 0, "the inbox itself failed to render — this test would pass vacuously"
    assert not out["present"], "the tour mounted on a NON-demo tenant"


def test_the_tour_finds_the_rows_it_points_at():
    """Every step keeps its target — the useMemo-vs-useEffect bug, caught by counting.

    Five steps only survive if all three targeted ones resolved against a document that already had
    rows in it. A render-time resolution yields "1 of 2" here and throws nothing.
    """
    out = _run(_FULL)
    assert out["present"], "the tour did not mount on a demo tenant"
    assert out["count"] == "1 of 5", (
        f"expected all five steps, got {out['count']!r} — targeted steps were dropped, which means "
        "they were resolved before the inbox rows were in the document")


def test_a_bucket_the_engine_did_not_produce_is_skipped_not_pointed_at():
    """The demo inbox is regenerated by running goldens through the real engine, so a bucket can come
    back empty. That step must disappear, not ring nothing."""
    out = _run([_email("e-yellow", "Yellow Sender", "yellow")])
    assert out["count"] == "1 of 3", (
        f"expected intro + yellow + outro, got {out['count']!r}")


def test_the_steps_ring_the_buckets_in_the_order_the_pitch_needs():
    """Intro rings nothing; then yellow, then red, then green; then the outro lets go again.

    The order is the argument, not a preference. Yellow first because a draft that states its own
    doubt is the product; red second because refusing is the other half of it; green last, once
    "ready to send" means something. Opening on green would demo a template matcher.
    """
    out = _run(_FULL)
    assert [s["spotConf"] for s in out["walk"]] == [None, "low", "flag", "high", None], (
        f"the tour rang the wrong rows: {[s['spotConf'] for s in out['walk']]}")


def test_the_tour_writes_nothing_from_end_to_end():
    """It runs on a deployment where every write 404s. Anything it POSTed would be swallowed and
    lost, which is the failure mode this repo keeps re-learning — so it must not try.

    Measured as a DELTA across the walk, not as an absolute. The page itself PUTs /api/config once on
    mount (the debounced save behind the initial GET), which long predates this and is swallowed by
    design on a read-only deployment. Asserting zero writes would just re-fail on that every run and
    say nothing about the tour.
    """
    out = _run(_FULL)
    added = out["writes"][len(out["writesOnArrival"]):]
    assert added == [], f"walking the tour wrote: {added}"


def test_finishing_the_tour_puts_everything_back():
    """It closes, the ring goes with it, and it does not greet the same visitor twice."""
    out = _run(_FULL)
    assert out["endTour"], "the tour bar survived its own last step"
    assert out["endSpot"], "the highlight outlived the thing explaining it — reads as stuck selection"
    assert out["endSeen"] == "1", "a refresh would replay the whole tour"
    assert out["endHint"], "the Beanary hint never came back after the tour finished"


def test_the_demo_withholds_the_operator_facing_release_note():
    """The what's-new card is written to her about her own habits and lands above the greeting. A
    visitor's first line of Bean should not be a changelog entry. The Settings tab still has it."""
    out = _run(_FULL)
    assert not out["whatsNewCard"], "the release-note card rendered on the public demo"


def test_the_beanary_hint_waits_for_the_tour_to_finish():
    """Both are fixed to the bottom of the screen. Two nubs at once is the kind of thing that only
    shows up in a screenshot, so it gets an assertion instead."""
    out = _run(_FULL)
    assert out["present"], "precondition: the tour should be up"
    assert not out["beanaryHint"], "the Beanary hint shared the screen with the tour bar"
