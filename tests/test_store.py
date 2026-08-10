"""Durability tests for the atomic write path + load_config's absent-vs-corrupt contract.

These guard the worst bug in the repo: a config truncated mid-write on the volume, then served
back as demo fixtures. The behavior under test is crash-safety and loud failure, so the tests
simulate a crash (serialization raising partway) and assert the ORIGINAL file survives — never
that a warning was logged. All offline, stdlib only.
"""

from __future__ import annotations

import json
import logging

import pytest

from bean.config import Config, ConfigCorruptError, load_config
from bean.store import write_json_atomic


# ---- write_json_atomic: no temp turds, target fully readable -------------------------------

def test_write_is_atomic_and_leaves_no_temp(tmp_path):
    target = tmp_path / "config.json"
    write_json_atomic(target, {"a": 1, "b": [2, 3]})

    assert json.loads(target.read_text()) == {"a": 1, "b": [2, 3]}
    # No *.tmp left in the directory — a failure-free write must clean up after itself.
    assert [p.name for p in tmp_path.iterdir() if p.suffix == ".tmp"] == []


def test_write_creates_parent_dir(tmp_path):
    # A brand-new customer has no folder yet; the write must mkdir it (mirrors corrections.record).
    target = tmp_path / "new-customer" / "config.json"
    write_json_atomic(target, {"ok": True})
    assert json.loads(target.read_text()) == {"ok": True}


# ---- the core guarantee: a crash mid-write cannot truncate the target ----------------------

class _Unserializable:
    """json.dumps raises TypeError on this — stands in for a process dying partway through a write."""


def test_crash_mid_write_leaves_original_intact(tmp_path):
    target = tmp_path / "config.json"
    write_json_atomic(target, {"real": "config", "moat": True})  # a good file exists first
    before = target.read_text()

    # Serialization blows up partway through the "write". With write_text this would already have
    # truncated the file to zero bytes; with the atomic path the target must be byte-for-byte intact.
    with pytest.raises(TypeError):
        write_json_atomic(target, {"broken": _Unserializable()})

    assert target.read_text() == before
    assert json.loads(target.read_text()) == {"real": "config", "moat": True}
    assert [p.name for p in tmp_path.iterdir() if p.suffix == ".tmp"] == []


def test_crash_via_monkeypatched_dumps_leaves_original_intact(tmp_path, monkeypatch):
    # Same guarantee, but the failure is injected mid-serialization (a torn json.dumps) rather than
    # via an unserializable value — proves it's the write path that's safe, not the input shape.
    target = tmp_path / "config.json"
    write_json_atomic(target, {"real": "config"})
    before = target.read_text()

    def boom(*_a, **_k):
        raise RuntimeError("serialization died mid-flight (simulated crash)")

    monkeypatch.setattr("bean.store.json.dumps", boom)
    with pytest.raises(RuntimeError):
        write_json_atomic(target, {"whatever": 1})

    assert target.read_text() == before
    assert [p.name for p in tmp_path.iterdir() if p.suffix == ".tmp"] == []


# ---- the rolling backup holds the PREVIOUS contents ----------------------------------------

def test_backup_captures_previous_contents(tmp_path):
    target = tmp_path / "config.json"
    bak = tmp_path / "config.json.bak"

    write_json_atomic(target, {"version": 1})
    assert not bak.exists()  # first write of a fresh file: nothing to back up yet

    write_json_atomic(target, {"version": 2})
    # The live file is the new version; the backup holds exactly the version it replaced.
    assert json.loads(target.read_text()) == {"version": 2}
    assert json.loads(bak.read_text()) == {"version": 1}


def test_backup_can_be_disabled(tmp_path):
    target = tmp_path / "config.json"
    write_json_atomic(target, {"version": 1})
    write_json_atomic(target, {"version": 2}, backup=False)
    assert not (tmp_path / "config.json.bak").exists()


# ---- load_config: absent vs corrupt --------------------------------------------------------

def test_absent_file_falls_back_to_fixtures(tmp_path):
    # A brand-new customer (no file) is the ONE case fixtures are correct — behavior preserved.
    assert load_config(tmp_path / "does-not-exist.json") == Config.from_fixtures()


def test_corrupt_config_recovers_from_backup_and_logs_error(tmp_path, caplog):
    # A good save leaves a .bak; if the live file is later corrupted, load_config must serve the
    # BACKUP (the operator's last real config), never fixtures, and shout about it at ERROR. Use a
    # DISTINCT setting value so recovery is provably the backup and not the fixtures fallback.
    target = tmp_path / "config.json"
    base = Config.from_fixtures()
    taught = Config(categories=base.categories, knowledge_docs=base.knowledge_docs,
                    settings={"shopifyStore": "her-real-store"})
    write_json_atomic(target, taught.to_dict())  # good save → establishes state
    write_json_atomic(target, taught.to_dict())  # second save → .bak now holds the taught config

    target.write_text("{ corrupt ]]]")  # volume shredded the live file

    with caplog.at_level(logging.ERROR, logger="bean.config"):
        recovered = load_config(target)

    assert recovered.settings["shopifyStore"] == "her-real-store"  # from the backup, not fixtures
    assert recovered != Config.from_fixtures()
    assert any(r.levelno >= logging.ERROR and "corrupt" in r.message.lower()
               for r in caplog.records)


def test_corrupt_config_with_corrupt_backup_still_raises(tmp_path):
    # A .bak that is ALSO unparseable is not "usable" — it must not rescue a corrupt live file.
    target = tmp_path / "config.json"
    target.write_text("{ corrupt ]]]")
    (tmp_path / "config.json.bak").write_text("<<< also garbage >>>")

    with pytest.raises(ConfigCorruptError):
        load_config(target)


def test_corrupt_config_with_no_backup_raises(tmp_path):
    # The headline fix: present-but-unparseable + no backup ⇒ raise, do NOT serve fixtures.
    target = tmp_path / "config.json"
    target.write_text("{ not json at all ]]]")
    with pytest.raises(ConfigCorruptError):
        load_config(target)


def test_recovered_backup_config_is_a_real_config(tmp_path):
    # End-to-end: authored config saved, live file corrupted, recovery returns her data intact —
    # the moat survives a mid-write crash. (This used to assert on a taught tree leaf; the tree is
    # gone, so it asserts on the authored content that IS still config: templates + knowledge docs.)
    from bean.config import Category

    base = Config.from_fixtures()
    authored = Config(
        categories=(Category("Widgets", "broken widgets", "Hi {name}, here's the fix."),),
        knowledge_docs={"Warranty": "Two years, no receipt needed."},
        settings=base.settings,
    )
    target = tmp_path / "config.json"
    write_json_atomic(target, authored.to_dict())
    write_json_atomic(target, authored.to_dict())  # ensure .bak holds the authored config

    target.write_text("")  # empty file — the classic mid-write truncation

    recovered = load_config(target)
    assert recovered == authored
    assert recovered.templates()["Widgets"] == "Hi {name}, here's the fix."
    assert recovered.knowledge_docs["Warranty"] == "Two years, no receipt needed."
