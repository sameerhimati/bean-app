"""The app actually boots — every script Bean.html ships, loaded in Bean.html's order, mounted.

Bean's web UI has NO build step: the .jsx files are plain React.createElement loaded as ordinary
<script> tags. That buys a lot (no toolchain, edit-and-reload), but it costs the one guarantee a
bundler gives for free — a reference to a global that no longer exists is not a compile error, it is
a **white screen in production**. Deleting a file and missing one `window.beanFoo` caller looks
exactly like a successful change until someone opens the page.

So this test IS the missing compiler. It reads the script list out of Bean.html (never a hand-copy —
a tag added there is covered here automatically), loads each one into a jsdom window in that order,
mounts the app, and fails on any thrown error, any console.error, or a root that came out empty. It
does not assert what the UI looks like; the per-component tests do that. It asserts the page renders
at all, which is the failure the no-build-step trade makes cheap to ship and expensive to notice.

Every /api/* read is stubbed to its empty-but-valid shape, so this is also the cold-start path: a
brand-new customer with no notebook, no config and no mail must still get a page.

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
    """The app's own script list, in load order, straight from the shipped page."""
    html = (_WEB / "Bean.html").read_text(encoding="utf-8")
    return re.findall(r'<script src="(?!/vendor/)([^"]+\.jsx)"></script>', html)


_HARNESS = r"""
const { JSDOM } = require('jsdom');
const fs = require('fs');
const path = require('path');
const WEB = process.env.BEAN_WEB;
const SCRIPTS = JSON.parse(process.env.BEAN_SCRIPTS);
const HASH = process.env.BEAN_HASH || '';

// runScripts:'dangerously' so the files execute as real <script> tags do: ONE shared global scope.
// This is load-bearing fidelity, not convenience — the .jsx files call each other's top-level
// functions bare (bean-root's BeanReply calls bean-admin's `pasteWithLinks` directly, not through a
// window.* export), which only resolves because script tags share a global. Wrapping each file in
// its own function scope would make those calls fail here and pass in the browser — a harness that
// lies, and one that would have to be re-fixed the first time it flagged a false positive.
const dom = new JSDOM('<!DOCTYPE html><body><div id="root"></div></body>',
  { url: 'http://localhost/#' + HASH, runScripts: 'dangerously' });
const { window } = dom;
global.window = window; global.document = window.document; global.navigator = window.navigator;
window.matchMedia = window.matchMedia || (() => ({ matches: false, addListener(){}, removeListener(){},
  addEventListener(){}, removeEventListener(){} }));
window.scrollTo = () => {};

// Run a file the way the page runs it: as a script element in the document.
const runScript = (code) => {
  const el = window.document.createElement('script');
  el.textContent = code;
  window.document.body.appendChild(el);
};

// Every read the app makes on mount, in its empty-but-valid shape: the cold start of a brand-new
// customer. A 404 would be a different (also valid) test; empty is the one that must render.
const EMPTY = {
  // config is an object even for a brand-new customer (the server always builds one); notebook is
  // genuinely null until something has been distilled, which is the state worth booting against.
  '/api/status': {}, '/api/config': {}, '/api/notebook': null, '/api/notebook/review': {},
  '/api/inbox': [], '/api/learning': {}, '/api/corrections': [], '/api/meta': {},
  // Not an API — a static file in web/, fetched on mount to decide whether the ⚙ Settings button
  // shows its "something new" dot. `{entries: []}` is the honest empty shape: a deployment with no
  // release notes yet has nothing to announce, and must not show a dot promising otherwise.
  '/whats-new.json': { entries: [] },
};
const unstubbed = [];
window.fetch = (url, opts) => {
  const p = String(url).split('?')[0];
  if (!(p in EMPTY) && !(opts && opts.method && opts.method !== 'GET')) unstubbed.push(p);
  const body = p in EMPTY ? EMPTY[p] : {};
  return Promise.resolve({
    ok: true, status: 200, headers: { get: () => null },
    json: () => Promise.resolve(body), text: () => Promise.resolve(JSON.stringify(body)),
  });
};

// Anything the page would have complained about. React reports render failures through console.error
// rather than by throwing, so a swallowed error still fails this test. An uncaught error inside a
// script element surfaces as a window 'error' event (jsdom's VirtualConsole would otherwise just
// print it and let the run look clean).
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

// Load exactly what the page loads, in the page's order. The last one (bean-root.jsx) reads the
// hash and mounts, which is why the hash is set on the window URL above rather than after the fact.
const loaded = [];
for (const src of SCRIPTS) {
  const before = errors.length;
  runScript(readOrSynthesize(src));
  if (errors.length > before) { errors.push('...while loading ' + src); break; }
  loaded.push(src);
}

// React 18 renders off a microtask; let the mount and its on-mount effects settle.
setTimeout(() => {
  const root = document.getElementById('root');
  process.stdout.write(JSON.stringify({
    errors, loaded, scripts: SCRIPTS, unstubbed: [...new Set(unstubbed)],
    html: root.innerHTML.length, text: root.textContent.slice(0, 3000),
  }));
  // The mounted app keeps timers alive (status polling), so node would never exit on its own.
  process.exit(0);
}, 250);
"""


# Every view a URL can land on, as bean-root's initialView() reads them. Listed here rather than
# scraped, so removing a route stays a deliberate edit. bean-root reads the hash once at evaluation
# and its top-level `const`s can't be re-declared, so each view gets its own process/window.
# "draft/nope" is deliberate: a shareable link is opened cold and opened stale, so the
# email-not-here path is a real destination, not an error case.
_VIEWS = ["", "notebook", "questionnaire", "admin", "draft/nope"]


def _run(hash_: str = "") -> dict:
    proc = subprocess.run(
        ["node", "-e", _HARNESS],
        cwd=str(_JS), capture_output=True, text=True, timeout=120,
        env={**os.environ, "BEAN_WEB": str(_WEB), "BEAN_SCRIPTS": json.dumps(_script_tags()),
             "BEAN_HASH": hash_},
    )
    assert proc.returncode == 0, (
        f"harness failed for #{hash_ or 'inbox'}:\nSTDOUT:{proc.stdout}\nSTDERR:{proc.stderr}")
    return json.loads(proc.stdout)


def test_every_script_the_page_ships_loads():
    """A deleted file or a syntax error lands here, not on the operator."""
    out = _run()
    assert out["loaded"] == out["scripts"], (
        "a script Bean.html loads did not evaluate — first failure:\n" + "\n".join(out["errors"]))


@pytest.mark.parametrize("hash_", _VIEWS, ids=lambda h: h or "inbox")
def test_every_view_a_url_can_land_on_renders(hash_):
    """The white-screen guard, per view. A stale window.* caller inside one view is invisible from
    the inbox — it only fires when she opens that tab, which is exactly when it must not happen."""
    out = _run(hash_)
    assert not out["errors"], (
        f"#{hash_ or 'inbox'} rendered with errors:\n" + "\n".join(out["errors"]))
    assert out["html"] > 0, f"#{hash_ or 'inbox'} mounted an empty root — that is the white screen"


def test_a_customer_with_nothing_yet_still_gets_a_page():
    """Cold start: no config, no notebook, no mail. The chrome must still render — this is the first
    thing a new customer sees, and it's the state no fixture covers."""
    out = _run()
    assert "Bean." in out["text"], "the wordmark did not render"
    assert "Teach Bean" in out["text"], "the teach entry point vanished from the top bar"
    assert "Notebook" in out["text"]


def test_the_page_asks_for_nothing_the_test_does_not_know_about():
    """A new on-mount fetch should be a deliberate act: add it to EMPTY (deciding what its empty shape
    IS) rather than letting it silently resolve to {} and hide a boot-time dependency."""
    out = _run()
    assert not out["unstubbed"], f"unstubbed GETs on mount: {out['unstubbed']}"
