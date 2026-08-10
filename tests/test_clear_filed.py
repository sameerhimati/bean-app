"""The FYI-clear delete: it must remove filed mail and NEVER a customer email.

`clear_filed` rewrites the append-only inbox log, so the invariant that matters is one-directional:
a walked customer email is untouchable, a parse quirk can only ever KEEP. These tests pin exactly
that, plus the backup + no-op behaviour.
"""

from __future__ import annotations

import json

from bean.inbox import clear_filed


def _walked(i):
    return {"id": f"c{i}", "sender_email": "cust@x.com", "subject": "help",
            "result": {"confidence": "flag", "chunks": [{"node_type": "escalate", "path": ["Sofas & Upholstery"]}]}}


def _filed(i, kind="promo"):
    return {"id": f"f{i}", "sender_email": "news@x.com", "subject": "sale",
            "result": {"disposition": "filed", "kind": kind, "reason": "marketing blast"}}


def _write(path, records):
    path.write_text("".join(json.dumps(r, sort_keys=True) + "\n" for r in records), encoding="utf-8")


def test_removes_filed_keeps_walked(tmp_path):
    p = tmp_path / "inbox.jsonl"
    _write(p, [_walked(1), _filed(1), _walked(2), _filed(2, "cold-outreach"), _filed(3, "notification")])
    removed, kept = clear_filed(p, backup_path=tmp_path / "bak")
    assert (removed, kept) == (3, 2)
    ids = [json.loads(l)["id"] for l in p.read_text().splitlines() if l.strip()]
    assert ids == ["c1", "c2"]  # both customers survive, in order; every filed line gone


def test_a_walked_email_is_never_removed_even_if_also_marked_filed(tmp_path):
    """The double guard: a line with BOTH a filed disposition and chunks (a shape that should never
    exist) is KEPT — the code fails toward preserving a customer email, never toward deleting one."""
    p = tmp_path / "inbox.jsonl"
    weird = {"id": "c9", "result": {"disposition": "filed", "chunks": [{"node_type": "answer"}]}}
    _write(p, [weird, _filed(1)])
    removed, kept = clear_filed(p, backup_path=tmp_path / "bak")
    assert removed == 1 and kept == 1
    assert [json.loads(l)["id"] for l in p.read_text().splitlines() if l.strip()] == ["c9"]


def test_unparseable_line_is_kept(tmp_path):
    p = tmp_path / "inbox.jsonl"
    p.write_text("not json at all\n" + json.dumps(_filed(1), sort_keys=True) + "\n", encoding="utf-8")
    removed, kept = clear_filed(p, backup_path=tmp_path / "bak")
    assert removed == 1 and kept == 1
    assert p.read_text().splitlines()[0] == "not json at all"


def test_backup_holds_the_full_original(tmp_path):
    p = tmp_path / "inbox.jsonl"
    bak = tmp_path / "bak"
    _write(p, [_walked(1), _filed(1)])
    original = p.read_text()
    clear_filed(p, backup_path=bak)
    assert bak.read_text() == original  # the backup is the pre-delete file, byte-for-byte


def test_no_filed_mail_is_a_noop_no_backup(tmp_path):
    p = tmp_path / "inbox.jsonl"
    bak = tmp_path / "bak"
    _write(p, [_walked(1), _walked(2)])
    before = p.read_text()
    removed, kept = clear_filed(p, backup_path=bak)
    assert (removed, kept) == (0, 2)
    assert p.read_text() == before  # untouched
    assert not bak.exists()          # nothing removed ⇒ nothing backed up


def test_missing_file_is_safe(tmp_path):
    assert clear_filed(tmp_path / "nope.jsonl", backup_path=tmp_path / "bak") == (0, 0)
