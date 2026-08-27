"""The "what's new" release notes and their unread dot (web/bean-inbox.jsx), under node.

The dot is the whole discoverability plan: a changelog buried in Settings, with nothing pointing at
it, tells her about new features only after she has already found them. So the load-bearing tests
are the ones about WHEN it shows.

The file itself is checked too — it ships in the repo and is hand-written, so a typo'd date would
silently break the ordering and the dot at the same time.
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

let store = {};
global.localStorage = {
  getItem: k => (k in store ? store[k] : null),
  setItem: (k, v) => { store[k] = String(v); },
};
global.window = { friendlyKind: k => k, localStorage: global.localStorage };
global.React = { createElement: () => null };
new Function('window', 'React', fs.readFileSync(path.join(WEB, 'bean-inbox.jsx'), 'utf8'))
  .call(global.window, global.window, global.React);

const { whatsNewUnread, markWhatsNewSeen } = window.beanStore;
const entries = [
  { date: '2026-08-16', title: 'Newest' },
  { date: '2026-08-15', title: 'Older' },
];

const out = {};
store = {};
out.unread_when_never_opened = (whatsNewUnread(entries) || {}).title;

store = { bean_whatsnew_seen: '2026-08-15' };
out.unread_when_one_is_new = (whatsNewUnread(entries) || {}).title;

store = { bean_whatsnew_seen: '2026-08-16' };
out.unread_when_caught_up = whatsNewUnread(entries);

store = { bean_whatsnew_seen: '2026-09-01' };
out.unread_when_seen_is_ahead = whatsNewUnread(entries);

out.unread_when_no_entries = whatsNewUnread([]);
out.unread_when_null = whatsNewUnread(null);

// Reading the page stamps the NEWEST date, so nothing older ever re-announces itself.
store = {};
markWhatsNewSeen(entries);
out.stamped = store.bean_whatsnew_seen;
out.unread_after_marking = whatsNewUnread(entries);

// Marking with nothing must not write a value that would suppress a later first entry.
store = {};
markWhatsNewSeen([]);
out.stamped_when_empty = store.bean_whatsnew_seen === undefined ? 'nothing' : store.bean_whatsnew_seen;

console.log(JSON.stringify(out));
"""


def _run() -> dict:
    proc = subprocess.run(
        ["node", "--input-type=commonjs", "-e", _HARNESS], capture_output=True, text=True,
        env={**os.environ, "BEAN_WEB": str(_WEB)},
    )
    if proc.returncode != 0:
        raise AssertionError(f"node failed:\n{proc.stderr}")
    return json.loads(proc.stdout)


def test_everything_is_new_to_someone_who_has_never_opened_it():
    assert _run()["unread_when_never_opened"] == "Newest"


def test_only_entries_newer_than_what_she_read_count():
    assert _run()["unread_when_one_is_new"] == "Newest"


def test_no_dot_once_she_is_caught_up():
    r = _run()
    assert r["unread_when_caught_up"] is None
    # A stamp ahead of every entry (clock skew, a rolled-back note) must not resurrect the dot.
    assert r["unread_when_seen_is_ahead"] is None


def test_no_dot_when_there_is_nothing_to_announce():
    """A deployment with no release notes must not promise something new."""
    r = _run()
    assert r["unread_when_no_entries"] is None
    assert r["unread_when_null"] is None


def test_reading_the_page_stamps_the_newest_date_and_clears_the_dot():
    r = _run()
    assert r["stamped"] == "2026-08-16"
    assert r["unread_after_marking"] is None


def test_marking_an_empty_list_writes_nothing():
    """Otherwise the first note ever added would arrive already-read."""
    assert _run()["stamped_when_empty"] == "nothing"


# --- the shipped file ---------------------------------------------------------------------------

def test_the_release_notes_file_is_valid_and_ordered():
    data = json.loads((_WEB / "whats-new.json").read_text(encoding="utf-8"))
    entries = data["entries"]
    assert entries, "the file ships with entries or the feature announces nothing"
    for e in entries:
        assert e["date"] and e["title"] and e["body"]
        # ISO dates, because the unread check is a STRING comparison — "Aug 16" would break it
        # silently, showing a dot forever or never.
        assert len(e["date"]) == 10 and e["date"][4] == "-" and e["date"][7] == "-", e["date"]
    assert [e["date"] for e in entries] == sorted((e["date"] for e in entries), reverse=True)


def test_the_notes_are_written_for_the_operator_not_for_a_reviewer():
    """Hand-written, never generated from commits. A conventional-commit prefix here would mean
    someone piped `git log` into it, which is the failure this file exists to avoid."""
    data = json.loads((_WEB / "whats-new.json").read_text(encoding="utf-8"))
    for e in data["entries"]:
        blob = (e["title"] + " " + e["body"]).lower()
        for jargon in ("feat(", "fix(", "refactor", "commit", "endpoint", "api/", "jsonl", ".jsx"):
            assert jargon not in blob, f"{jargon!r} is reviewer vocabulary: {e['title']!r}"
