"""Arrival order and the operator's clock (web/bean-ui.jsx + web/bean-inbox.jsx), run under node.

Her mail arrives stamped with the SENDER's offset — five consecutive emails in the real corpus carry
-0700, -0400, +0000, -0400 and +0000. The inbox used to render that string verbatim, which read as
GMT and sorted as text (i.e. not at all). These pin the two properties that fixes it:

  * every timestamp renders in the OPERATOR's zone, so the inbox reads as one continuous day
  * rows are newest-first, and mail we cannot date sinks rather than floating to the top

The REAL source is executed under node, so the test can't drift from what ships. Skips (never lies)
when node is absent.
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

global.window = {};
global.React = { createElement: () => null };
for (const f of ['bean-ui.jsx', 'bean-inbox.jsx']) {
  new Function('window', 'React', fs.readFileSync(path.join(WEB, f), 'utf8'))
    .call(global.window, global.window, global.React);
}

const label = window.beanTimeLabel;
const ms = window.beanTimeMs;

// The five real offsets, as they actually arrived on 23 Jul 2026.
const real = [
  { id: 'a', time: 'Thu, 23 Jul 2026 11:13:18 -0700' },  // 13:13 Central
  { id: 'b', time: 'Thu, 23 Jul 2026 13:27:02 -0400' },  // 12:27 Central
  { id: 'c', time: 'Thu, 23 Jul 2026 17:25:47 +0000' },  // 12:25 Central
  { id: 'd', time: 'Thu, 23 Jul 2026 12:24:13 +0000' },  // 07:24 Central
];
const undatable = [
  { id: 'new', time: 'Thu, 23 Jul 2026 11:13:18 -0700' },
  { id: 'junk', time: 'not a date' },
  { id: 'blank', time: '' },
];

// A fixed "now" so the same-day branch is deterministic instead of depending on the clock.
const NOW = Date.parse('Thu, 23 Jul 2026 20:00:00 +0000');       // 15:00 Central, same day
const OTHER_DAY = Date.parse('Fri, 24 Jul 2026 20:00:00 +0000');

console.log(JSON.stringify({
  same_day: label('Thu, 23 Jul 2026 17:25:47 +0000', NOW),
  other_day: label('Thu, 23 Jul 2026 17:25:47 +0000', OTHER_DAY),
  // 23:30 in Los Angeles is 01:30 the NEXT day in Chicago.
  crosses_midnight: label('Mon, 3 Aug 2026 23:30:00 -0700', OTHER_DAY),
  unparseable_passes_through: label('just now', NOW),
  empty_is_empty: label('', NOW),
  ms_of_junk: ms('not a date'),
  newest_first: [...real].sort(window.byNewestFirst).map(e => e.id),
  undatable_sinks: [...undatable].sort(window.byNewestFirst).map(e => e.id),
  nothing_dropped: [...undatable].sort(window.byNewestFirst).length,
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


def test_timestamps_render_in_the_operators_zone():
    """17:25 UTC is 12:25 in Chicago. Rendering the raw header showed her someone else's clock."""
    r = _run()
    assert r["same_day"] == "12:25 PM"


def test_an_older_day_carries_its_date():
    r = _run()
    assert r["other_day"] == "Jul 23, 12:25 PM"


def test_a_late_evening_sender_lands_on_the_right_central_day():
    """23:30 Pacific is 01:30 the next morning Central — the date shown has to be hers, not his."""
    r = _run()
    assert r["crosses_midnight"] == "Aug 4, 1:30 AM"


def test_an_unreadable_date_shows_what_it_had_rather_than_inventing_one():
    """Pasted mail carries the literal string "just now". Never render a fabricated 1970."""
    r = _run()
    assert r["unparseable_passes_through"] == "just now"
    assert r["empty_is_empty"] == ""
    assert r["ms_of_junk"] is None


def test_rows_are_newest_first():
    r = _run()
    assert r["newest_first"] == ["a", "b", "c", "d"]


def test_undatable_mail_sinks_and_is_never_dropped():
    """An unreadable Date header is not evidence something just arrived, so it must not take the
    most important position — and it must still render."""
    r = _run()
    assert r["undatable_sinks"][0] == "new"
    assert r["nothing_dropped"] == 3
