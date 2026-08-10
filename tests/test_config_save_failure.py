"""A failed `PUT /api/config` must never look like a save (web/bean-inbox.jsx).

`upsertStatus` and `recordCorrection` were both taught to surface a dropped write. `saveConfig` —
the endpoint that persists the operator's categories, templates, gate rules and routing tree, which
is to say the moat itself — was left with `catch (e) { return c; }`. A redeploy mid-PUT, a laptop
waking from sleep, and the tree node they just taught Bean is gone from the server while the UI says
"Saved — Bean will reuse this next time."

The hard part is that a rejected fetch means two different things: on the static demo there is no
backend (silence is correct), and in the deployed app the network dropped (silence is data loss).
So the tests below pin BOTH: silent before /api/config has ever answered, loud after.

Runs against the REAL source extracted from the shipped .jsx, like tests/test_correction_outbox.py.
"""

from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path

import pytest

_INBOX_JSX = Path(__file__).parent.parent / "web" / "bean-inbox.jsx"
_START = "function _signalWriteFailed"
_END = "// ---- the correction outbox"

pytestmark = pytest.mark.skipif(shutil.which("node") is None, reason="node not installed")


def _extract_source() -> str:
    src = _INBOX_JSX.read_text(encoding="utf-8")
    return src[src.index(_START):src.index(_END)]


def _run(script: str, *, responses: list) -> dict:
    """Run `script` against the real loadConfig/saveConfig.

    `responses` is a queue of fetch outcomes consumed in order: an int is an HTTP status, and the
    string "reject" is a network error — a dead server or a dropped connection.
    """
    harness = f"""
    const {{ JSDOM }} = require('jsdom');
    const dom = new JSDOM('', {{ url: 'https://bean.test/' }});
    global.window = dom.window;
    global.CustomEvent = dom.window.CustomEvent;  // node's own CustomEvent is not jsdom's Event
    window.CONFIG = {{ categories: [], knowledgeDocs: [], settings: {{}} }};

    const responses = {json.dumps(responses)};
    const signals = [];
    window.addEventListener('bean:write-failed', e => signals.push(e.detail));
    console.error = () => {{}};  // _signalWriteFailed logs; keep the harness output clean

    global.fetch = (url, opts) => {{
      const r = responses.shift();
      if (r === 'reject') return Promise.reject(new TypeError('Failed to fetch'));
      return Promise.resolve({{
        ok: r >= 200 && r < 300,
        status: r,
        headers: {{ get: () => '"etag-1"' }},
        json: async () => ({{ categories: [], knowledgeDocs: [], settings: {{}} }}),
      }});
    }};

    {_extract_source()}

    (async () => {{
      {script}
      console.log(JSON.stringify({{ signals }}));
    }})();
    """
    proc = subprocess.run(
        ["node", "--input-type=commonjs", "-e", harness],
        capture_output=True, text=True, cwd=_INBOX_JSX.parent.parent / "tests" / "js",
    )
    if proc.returncode != 0:
        # `node` existing is not `jsdom` existing. The module-level skipif only proves the former,
        # and tests/js/node_modules is gitignored — so on any fresh clone this file was the one
        # jsdom test that HARD-FAILED where its eight siblings skipped politely. Four red tests on
        # a stranger's first `pytest` read as "this repo is broken", not "install a dev dep".
        if "Cannot find module 'jsdom'" in proc.stderr:
            pytest.skip("jsdom not installed — run `npm install` in tests/js to cover config saves")
        pytest.fail(f"node failed:\n{proc.stdout}\n{proc.stderr}")
    return json.loads(proc.stdout.strip().splitlines()[-1])


def test_rejected_put_after_the_backend_answered_is_reported_as_a_lost_save():
    """The bug: a redeploy drops the PUT and the operator's taught node vanishes without a word."""
    out = _run("await loadConfig(); await saveConfig({categories: []});",
               responses=[200, "reject"])
    assert out["signals"] == [{"what": "config", "status": 0, "queued": False}], (
        "a dropped PUT /api/config must surface — it is the operator's teaching, and it is gone"
    )


def test_rejected_put_with_no_backend_stays_silent():
    """The static demo has no /api/*. Nothing was lost, so nothing is claimed."""
    out = _run("await loadConfig(); await saveConfig({categories: []});",
               responses=["reject", "reject"])
    assert out["signals"] == [], "the offline demo must not cry about a server it never had"


def test_a_404_put_stays_silent_but_a_500_does_not():
    """404 is the static demo answering for /api/*; a 500 is a server actively refusing."""
    assert _run("await loadConfig(); await saveConfig({});", responses=[200, 404])["signals"] == []
    assert _run("await loadConfig(); await saveConfig({});", responses=[200, 500])["signals"] == [
        {"what": "config", "status": 500, "queued": False}
    ]


def test_a_successful_put_signals_nothing():
    out = _run("await loadConfig(); await saveConfig({});", responses=[200, 200])
    assert out["signals"] == []
