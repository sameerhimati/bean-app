"""The "clear the handled pile" delete: what it removes, what it refuses to, and what it never logs.

Two invariants carry this feature, and both are one-directional:

  1. The ids come from status.json on the SERVER. A request body naming its own ids would be a
     delete-any-email primitive pointed at real customer mail, so the body is ignored entirely.
  2. SNOOZED mail is never swept. 'skipped' means "come back to this"; a sweep that ate her
     later-pile is the one surprise that would stop her using the button at all.

The third property — that clearing writes NOTHING to corrections.jsonl — lives in the UI, and is
pinned at the bottom of this file. The rendering half (the control is on the row, it does not open
the email, the sweep counts honestly) is tests/test_bean_clear_js.py.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from bean.inbox import clear_ids

from tests.test_server import _req, base_url  # noqa: F401  (fixture import)
import bean.server as srv


# ---- the file rewrite ---------------------------------------------------------------------------

def _rec(i):
    return {"id": f"c{i}", "sender_email": "cust@x.com", "subject": "help",
            "result": {"confidence": "high", "chunks": [{"node_type": "answer"}]}}


def _write(path, records):
    path.write_text("".join(json.dumps(r, sort_keys=True) + "\n" for r in records), encoding="utf-8")


def test_removes_only_the_named_ids(tmp_path):
    p = tmp_path / "inbox.jsonl"
    _write(p, [_rec(1), _rec(2), _rec(3)])
    removed, kept = clear_ids(["c1", "c3"], p, backup_path=tmp_path / "bak")
    assert (removed, kept) == (2, 1)
    assert [json.loads(l)["id"] for l in p.read_text().splitlines() if l.strip()] == ["c2"]


def test_unknown_ids_are_ignored_not_an_error(tmp_path):
    p = tmp_path / "inbox.jsonl"
    _write(p, [_rec(1)])
    removed, kept = clear_ids(["nope", "c1"], p, backup_path=tmp_path / "bak")
    assert (removed, kept) == (1, 0)


def test_empty_id_set_is_a_no_op_with_no_backup(tmp_path):
    """A no-op stays a no-op — no rewrite, no backup file. Same stance as clear_filed."""
    p = tmp_path / "inbox.jsonl"
    bak = tmp_path / "bak"
    _write(p, [_rec(1)])
    before = p.read_text()
    assert clear_ids([], p, backup_path=bak) == (0, 0)
    assert p.read_text() == before
    assert not bak.exists()


def test_unparseable_line_is_kept(tmp_path):
    """A corrupt line has no readable id, so it can never match — and the fail-safe direction on
    customer mail is always toward keeping it."""
    p = tmp_path / "inbox.jsonl"
    p.write_text("not json at all\n" + json.dumps(_rec(1), sort_keys=True) + "\n", encoding="utf-8")
    removed, kept = clear_ids(["c1"], p, backup_path=tmp_path / "bak")
    assert (removed, kept) == (1, 1)
    assert p.read_text().splitlines()[0] == "not json at all"


def test_backup_holds_the_full_original(tmp_path):
    p = tmp_path / "inbox.jsonl"
    bak = tmp_path / "bak"
    _write(p, [_rec(1), _rec(2)])
    original = p.read_text()
    clear_ids(["c1"], p, backup_path=bak)
    assert bak.read_text() == original  # every line, including the one just deleted


def test_missing_inbox_is_a_no_op(tmp_path):
    assert clear_ids(["c1"], tmp_path / "nothing.jsonl") == (0, 0)


# ---- through the real route ---------------------------------------------------------------------

@pytest.fixture
def inbox_at(tmp_path, monkeypatch):
    """Point BEAN_DATA_DIR at tmp so inbox_path() (resolved live) writes nowhere near real data."""
    monkeypatch.setenv("BEAN_DATA_DIR", str(tmp_path))
    from bean.paths import inbox_path
    p = inbox_path(srv.CUSTOMER)
    p.parent.mkdir(parents=True, exist_ok=True)
    return p


def test_route_sweeps_approved_and_handled_and_leaves_snoozed(base_url, inbox_at):  # noqa: F811
    """The button's real path. Approved + handled go; snoozed and untouched mail stays."""
    url, _ = base_url
    _write(inbox_at, [_rec(1), _rec(2), _rec(3), _rec(4)])
    for eid, state in (("c1", "approved"), ("c2", "handled"), ("c3", "skipped")):
        assert _req(f"{url}/api/status", method="POST", body={"id": eid, "state": state})[0] == 200

    status, body = _req(f"{url}/api/clear-handled", method="POST", body={})
    assert status == 200
    assert body == {"ok": True, "removed": 2, "kept": 2}
    survivors = [json.loads(l)["id"] for l in inbox_at.read_text().splitlines() if l.strip()]
    assert survivors == ["c3", "c4"]  # the snoozed one and the one she never touched


def test_route_ignores_ids_in_the_request_body(base_url, inbox_at):  # noqa: F811
    """The delete list is READ FROM status.json, never accepted from the caller. A body naming an
    email she never actioned must not delete it — that is the whole security stance of this route."""
    url, _ = base_url
    _write(inbox_at, [_rec(1), _rec(2)])
    _req(f"{url}/api/status", method="POST", body={"id": "c1", "state": "handled"})

    status, body = _req(f"{url}/api/clear-handled", method="POST", body={"ids": ["c2"]})
    assert status == 200
    assert body["removed"] == 1
    assert [json.loads(l)["id"] for l in inbox_at.read_text().splitlines() if l.strip()] == ["c2"]


def test_route_prunes_the_status_map_it_swept(base_url, inbox_at):  # noqa: F811
    """The email is gone, so its state is a key nothing can ever read again. Snoozed keys stay."""
    url, _ = base_url
    _write(inbox_at, [_rec(1), _rec(2)])
    _req(f"{url}/api/status", method="POST", body={"id": "c1", "state": "approved"})
    _req(f"{url}/api/status", method="POST", body={"id": "c2", "state": "skipped"})

    _req(f"{url}/api/clear-handled", method="POST", body={})
    assert _req(f"{url}/api/status")[1] == {"c2": "skipped"}


def test_route_with_nothing_handled_deletes_nothing(base_url, inbox_at):  # noqa: F811
    url, _ = base_url
    _write(inbox_at, [_rec(1)])
    before = inbox_at.read_text()
    status, body = _req(f"{url}/api/clear-handled", method="POST", body={})
    assert status == 200 and body["removed"] == 0
    assert inbox_at.read_text() == before


# ---- the property that makes Clear safe to use thirty times a morning ---------------------------

def test_clear_writes_nothing_to_the_correction_log():
    """`clearOne` must never call record().

    corrections.jsonl is the brain. Every other way off the queue writes to it — approve, edit,
    skip, takeover, should-file — and each of those is her telling Bean something. Clear is not: it
    is housekeeping, and a one-tap gesture used thirty times a morning would bury the corrections
    that carry real judgment under a drift of "she pressed the X button". Measured precedent: 18 of
    21 takeovers already paired with a teach on the same email, so the takeover half was recording
    nothing on its own.

    A source assertion rather than a rendered one, because the thing being asserted is an ABSENCE —
    there is no seam to observe when nothing is written, and a test that mounted the app and checked
    an empty log would pass just as well if the button had stopped working.
    """
    src = (Path(__file__).parent.parent / "web" / "bean-root.jsx").read_text(encoding="utf-8")
    start = src.index("function clearOne(")
    body = src[start:src.index("\n  function ", start + 1)]
    assert "record(" not in body, "clearOne must not write a correction — see the docstring"
    assert "setOne(id, 'handled')" in body  # it still has to take the email off the queue


# ---- the sweep must not cost the RECORD that Bean drafted ---------------------------------------

def test_the_sweep_rolls_up_what_it_deletes(tmp_path):
    """The 2026-08-21 regression, pinned.

    `clear_ids` shipped without a rollup, on the reasoning that a walked email's record lives in
    corrections.jsonl. Two consumers say otherwise: the stats page's daily "drafted" bars and its
    windowed loop both read inbox.jsonl. So the operator's first real use of Clear flattened three
    weeks of bars to zero and her page reported that Bean had drafted almost nothing.
    """
    from bean.inbox import load_drafted_history, load_filed_history

    p = tmp_path / "inbox.jsonl"
    hist = tmp_path / "filed_history.jsonl"
    walked = lambda i, day: {"id": f"c{i}", "received_at": f"Mon, {day} Aug 2026 09:00:00 -0500",
                             "result": {"confidence": "high", "chunks": [{"node_type": "answer"}]}}
    _write(p, [walked(1, 3), walked(2, 3), walked(3, 4)])

    removed, kept = clear_ids(["c1", "c2", "c3"], p, backup_path=tmp_path / "bak", history_path=hist)
    assert (removed, kept) == (3, 0)
    assert load_drafted_history(hist) == {"2026-08-03": 2, "2026-08-04": 1}
    # ...and it wrote nothing the FILED loader would pick up: the two tallies share one log and must
    # not contaminate each other.
    assert load_filed_history(hist) == {}


def test_the_two_rollups_share_one_log_without_colliding(tmp_path):
    from bean.inbox import clear_filed, load_drafted_history, load_filed_history

    p = tmp_path / "inbox.jsonl"
    hist = tmp_path / "filed_history.jsonl"
    day = "Mon, 3 Aug 2026 09:00:00 -0500"
    _write(p, [
        {"id": "c1", "received_at": day,
         "result": {"confidence": "high", "chunks": [{"node_type": "answer"}]}},
        {"id": "f1", "received_at": day, "result": {"disposition": "filed", "kind": "promo"}},
    ])
    clear_filed(p, backup_path=tmp_path / "bak1", history_path=hist)
    clear_ids(["c1"], p, backup_path=tmp_path / "bak2", history_path=hist)

    assert load_filed_history(hist) == {"2026-08-03": 1}
    assert load_drafted_history(hist) == {"2026-08-03": 1}


def test_a_no_op_sweep_writes_no_history(tmp_path):
    from bean.inbox import load_drafted_history

    p = tmp_path / "inbox.jsonl"
    hist = tmp_path / "filed_history.jsonl"
    _write(p, [_rec(1)])
    assert clear_ids(["nothing-here"], p, backup_path=tmp_path / "bak", history_path=hist) == (0, 1)
    assert load_drafted_history(hist) == {} and not hist.exists()
