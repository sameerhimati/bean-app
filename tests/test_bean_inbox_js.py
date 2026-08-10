"""applyInboxFilters + inboxFacets (web/bean-inbox.jsx), tested from Python under node.

These are the pure functions behind the inbox facet filter. The one that MUST hold is the additive
invariant: with nothing selected the filter returns the exact input array, so the default inbox is
byte-identical to before the feature — a lens must never hide a real customer email.

The REAL source is executed under node (with the mount/globals stubbed, so loading the module just
defines its functions) — the test can't drift from what ships. Skips (never lies) when node is absent.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
from pathlib import Path

import pytest

_WEB = Path(__file__).parent.parent / "web"

pytestmark = pytest.mark.skipif(shutil.which("node") is None, reason="node not installed")

_HARNESS = r"""
const fs = require('fs');
const path = require('path');
const WEB = process.env.BEAN_WEB;

// bean-inbox.jsx defines functions and assigns window.* at load; it never runs a component body or
// touches window.CONF/fetch/localStorage at import, so a bare React stub is enough to load it.
// inboxCategory calls window.friendlyKind for filed mail (defined in bean-ui.jsx) — stub the two
// kinds this fixture uses so the module runs standalone.
global.window = { friendlyKind: k => ({ newsletter: 'Newsletter', promo: 'Promotion' }[k] || k) };
global.React = { createElement: () => null };
new Function('window', 'React', fs.readFileSync(path.join(WEB, 'bean-inbox.jsx'), 'utf8'))
  .call(global.window, global.window, global.React);

const apply = window.applyInboxFilters;
const facets = window.inboxFacets;

const emails = [
  { id: 'e1', category: 'Sofas & Upholstery',  confidence: 'high' },
  { id: 'e2', category: 'Sofas & Upholstery',  confidence: 'low' },
  { id: 'e3', category: 'Mattresses', confidence: 'flag' },
  { id: 'e4', category: 'Filed / FYI', filed: true, gateKind: 'newsletter' },
  { id: 'e5', category: 'Rugs',        confidence: 'high' },
];
// e1,e4 unhandled · e2,e5 handled · e3 snoozed
const status = { e2: 'handled', e3: 'skipped', e5: 'approved' };

const ids = list => list.map(e => e.id);
const empty = apply(emails, {}, status);

console.log(JSON.stringify({
  // the additive invariant: same ARRAY back (identity), not just equal contents
  empty_is_identity: empty === emails,
  empty_ids: ids(empty),
  by_category: ids(apply(emails, { categories: ['Sofas & Upholstery'] }, status)),
  by_fyi_kind: ids(apply(emails, { categories: ['Newsletter'] }, status)),   // filed mail filters by its FYI kind
  by_filed: ids(apply(emails, { confidences: ['filed'] }, status)),
  by_high: ids(apply(emails, { confidences: ['high'] }, status)),
  by_unhandled: ids(apply(emails, { statuses: ['unhandled'] }, status)),
  by_snoozed: ids(apply(emails, { statuses: ['snoozed'] }, status)),
  by_handled: ids(apply(emails, { statuses: ['handled'] }, status)),
  and_combo: ids(apply(emails, { categories: ['Sofas & Upholstery'], statuses: ['unhandled'] }, status)),
  or_within_facet: ids(apply(emails, { confidences: ['high', 'flag'] }, status)),
  empty_arrays_are_no_filter: apply(emails, { categories: [], confidences: [], statuses: [] }, status) === emails,
  facets: facets(emails, status),
}));
"""


def _run() -> dict:
    proc = subprocess.run(
        ["node", "--input-type=commonjs", "-e", _HARNESS], capture_output=True, text=True,
        env={**os.environ, "BEAN_WEB": str(_WEB)},
    )
    if proc.returncode != 0:
        raise AssertionError(f"node failed:\n{proc.stderr}")
    return json.loads(proc.stdout)


def test_empty_filter_returns_the_input_unchanged():
    """The invariant that protects real mail: no facet selected ⇒ the same array back, so the
    default inbox is identical to before the filter existed. Both {} and all-empty-arrays count."""
    r = _run()
    assert r["empty_is_identity"] is True
    assert r["empty_ids"] == ["e1", "e2", "e3", "e4", "e5"]
    assert r["empty_arrays_are_no_filter"] is True


def test_category_filter():
    assert _run()["by_category"] == ["e1", "e2"]


def test_filed_mail_filters_by_its_fyi_kind():
    # e4 is a filed newsletter; selecting the 'Newsletter' label pulls it up (not the flat 'Filed / FYI')
    assert _run()["by_fyi_kind"] == ["e4"]


def test_confidence_filter_including_filed():
    r = _run()
    assert r["by_filed"] == ["e4"]           # gate-filed mail is its own 'filed' bucket
    assert r["by_high"] == ["e1", "e5"]


def test_status_buckets():
    r = _run()
    assert r["by_unhandled"] == ["e1", "e4"]  # no status + 'pending' collapse into 'unhandled'
    assert r["by_snoozed"] == ["e3"]          # 'skipped'
    assert r["by_handled"] == ["e2", "e5"]    # 'handled' + 'approved'


def test_facets_and_or_combine():
    r = _run()
    assert r["and_combo"] == ["e1"]                     # Sofas & Upholstery AND unhandled
    assert r["or_within_facet"] == ["e1", "e3", "e5"]   # high OR flag


def test_options_are_derived_from_the_mail():
    """Category options come from the loaded emails (sorted), not a hard-coded tree; confidence and
    status appear in canonical order but only when present."""
    f = _run()["facets"]
    # topics (routed tree categories) and fyiKinds (gate-filed lanes) are split for the optgroups
    assert f["categories"] == ["Mattresses", "Rugs", "Sofas & Upholstery"]
    assert f["fyiKinds"] == ["Newsletter"]
    assert f["confidences"] == ["high", "low", "flag", "filed"]
    assert f["statuses"] == ["unhandled", "handled", "snoozed"]
