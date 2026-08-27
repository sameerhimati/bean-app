"""The correction outbox (web/bean-inbox.jsx), tested from Python under jsdom.

A correction is the learning signal — the only record of what the operator would have said instead
of Bean.
Losing one leaves a permanent, invisible hole in the few-shot pool. Before the outbox, a correction
POSTed against a restarting server was dropped while the UI flashed "Sent. On to the next one."

The outbox parks an undeliverable correction in localStorage and re-sends it on the next load. That
makes the safety net itself load-bearing, and a safety net with a TTL, a size cap, and a
distinction between "retry this" and "this will never succeed" is exactly the kind of code that rots
quietly. So it runs here, in CI, against the REAL source extracted from the shipped .jsx.

Requires `npm install` (jsdom is a dev dependency). Skips rather than lying about coverage.
"""

from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path

import pytest

_INBOX_JSX = Path(__file__).parent.parent / "web" / "bean-inbox.jsx"
# Where node resolves `require('jsdom')` from. The repo's documented setup installs it under
# tests/js (see tests/js/package.json), and every other jsdom harness here runs node with that as its
# cwd. These two ran it with NO cwd, so they resolved against whatever happened to be beside the
# process — which on this machine is a stray gitignored node_modules at the repo root. Follow the
# documented install on a clean clone and they skipped forever, saying "jsdom not installed" while
# four sibling harnesses ran fine. Skips do not lie here, but a skip nobody can clear is a test that
# does not exist.
_JS = Path(__file__).parent / "js"

_START = "// ---- the correction outbox"
_END = "// ---------- Header ----------"

pytestmark = pytest.mark.skipif(shutil.which("node") is None, reason="node not installed")


def _extract_source() -> str:
    src = _INBOX_JSX.read_text(encoding="utf-8")
    return src[src.index(_START):src.index(_END)]


def _run(script: str, *, responses: list, now_ms: int | None = None, seed: list | None = None) -> dict:
    """Run `script` against the real outbox code.

    `responses` is the queue of fetch outcomes, consumed in order: an int is an HTTP status, the
    string "reject" is a network error (what an offline click or a dead server looks like).
    `seed` pre-loads localStorage. Returns {outbox, sent, signals} as observed afterwards.
    """
    harness = f"""
    const {{ JSDOM }} = require('jsdom');
    const dom = new JSDOM('', {{ url: 'https://bean.test/' }});
    global.window = dom.window;
    // Freeze the clock when a test needs one. Capture the real Date FIRST — a subclass whose
    // static now() called Date.now() would resolve Date to itself and recurse forever.
    const _RealDate = Date;
    const _FROZEN = {now_ms if now_ms is not None else "null"};
    global.Date = class extends _RealDate {{
      static now() {{ return _FROZEN === null ? _RealDate.now() : _FROZEN; }}
    }};

    const responses = {json.dumps(responses)};
    const sent = [];
    const signals = [];
    global.fetch = (url, opts) => {{
      sent.push(JSON.parse(opts.body));
      const r = responses.shift();
      if (r === 'reject') return Promise.reject(new Error('network'));
      return Promise.resolve({{ ok: r >= 200 && r < 300, status: r }});
    }};
    global.console = {{ error: () => {{}}, log: console.log }};
    function _signalWriteFailed(what, endpoint, status, queued) {{ signals.push({{what, status, queued: !!queued}}); }}

    const seed = {json.dumps(seed)};
    if (seed) window.localStorage.setItem('bean.correction.outbox', JSON.stringify(seed));

    {_extract_source()}

    (async () => {{
      {script}
      const raw = window.localStorage.getItem('bean.correction.outbox');
      let outbox = [];
      try {{ outbox = raw ? JSON.parse(raw) : []; }} catch (e) {{ outbox = 'UNPARSEABLE'; }}
      console.log(JSON.stringify({{ outbox: outbox, sent: sent, signals: signals }}));
    }})();
    """
    proc = subprocess.run(["node", "--input-type=commonjs", "-e", harness],
                          cwd=str(_JS), capture_output=True, text=True)
    if proc.returncode != 0:
        if "Cannot find module 'jsdom'" in proc.stderr:
            pytest.skip("jsdom not installed — run `npm install` to cover the correction outbox")
        raise AssertionError(f"node failed:\n{proc.stderr}")
    return json.loads(proc.stdout.strip().splitlines()[-1])


# ---- parking: a correction that can't be delivered must survive ------------------------------

def test_a_network_failure_parks_the_correction_instead_of_dropping_it():
    """The exact incident: she clicks approve, the server is mid-redeploy, the fetch dies."""
    out = _run("await new Promise(r => { recordCorrection({email_id: 'e1', action: 'approve'}); setTimeout(r, 50); });",
               responses=["reject"])
    assert len(out["outbox"]) == 1
    assert out["outbox"][0]["payload"]["email_id"] == "e1"


def test_a_5xx_is_retried_once_then_parked():
    """A restart answers 500 twice within the retry window; the correction is kept, not lost."""
    out = _run("await new Promise(r => { recordCorrection({email_id: 'e1', action: 'approve'}); setTimeout(r, 2200); });",
               responses=[500, 500])
    assert len(out["sent"]) == 2, "should retry exactly once"
    assert len(out["outbox"]) == 1
    assert out["signals"] and out["signals"][-1]["queued"] is True, "must tell her it was KEPT"


def test_a_4xx_is_not_queued_because_it_would_never_succeed():
    """A malformed payload will fail identically forever. Queuing it is a poison pill; say it's lost."""
    out = _run("await new Promise(r => { recordCorrection({bad: true}); setTimeout(r, 50); });",
               responses=[400])
    assert out["outbox"] == []
    assert out["signals"][-1]["queued"] is False, "must NOT claim it was kept"


def test_a_successful_send_drains_a_backlog_parked_earlier():
    """The self-repair: one working POST flushes whatever a previous session couldn't deliver."""
    seed = [{"at": 10_000, "payload": {"email_id": "old", "action": "edit"}}]
    out = _run("await new Promise(r => { recordCorrection({email_id: 'new', action: 'approve'}); setTimeout(r, 50); });",
               responses=[200, 200], now_ms=20_000, seed=seed)
    assert out["outbox"] == [], "backlog should be gone"
    assert [s["email_id"] for s in out["sent"]] == ["new", "old"]


# ---- the outbox's own bounds -----------------------------------------------------------------

def test_flush_keeps_a_5xx_entry_and_discards_a_4xx_entry():
    seed = [{"at": 10_000, "payload": {"email_id": "keep"}}, {"at": 10_000, "payload": {"email_id": "drop"}}]
    out = _run("await flushCorrectionOutbox();", responses=[500, 400], now_ms=20_000, seed=seed)
    assert [e["payload"]["email_id"] for e in out["outbox"]] == ["keep"]


def test_entries_older_than_the_ttl_are_dropped_not_silently_replayed():
    """A week-old correction is not evidence of what she'd write today — replaying it would teach
    Bean from stale context. It must be dropped, and never sent."""
    week = 7 * 24 * 60 * 60 * 1000
    seed = [{"at": 0, "payload": {"email_id": "stale"}}, {"at": week, "payload": {"email_id": "fresh"}}]
    out = _run("await flushCorrectionOutbox();", responses=[200], now_ms=week + 1000, seed=seed)
    assert [s["email_id"] for s in out["sent"]] == ["fresh"], "the stale one must never be sent"
    assert out["outbox"] == []


def test_the_outbox_is_capped_so_a_dead_backend_cannot_fill_her_storage():
    """60 failures, cap of 50: the OLDEST are dropped, because the newest corrections are the ones
    worth keeping."""
    script = """
    for (let i = 0; i < 60; i++) _outboxPark({email_id: 'e' + i});
    """
    out = _run(script, responses=[])
    assert len(out["outbox"]) == 50
    ids = [e["payload"]["email_id"] for e in out["outbox"]]
    assert ids[0] == "e10" and ids[-1] == "e59", "kept the newest 50"


def test_a_corrupt_localStorage_value_behaves_as_an_empty_outbox():
    """Never let a bad stored value throw inside the click handler that also advances the UI."""
    out = _run("window.localStorage.setItem('bean.correction.outbox', 'not json'); await flushCorrectionOutbox();",
               responses=[])
    assert out["sent"] == []
