"""Phase 1 config-as-data tests: the byte-identity cache guard, malformed-JSON fallback,
and the injection proof. All offline (FakeModel).

The byte-identity test is the cache invariant: a Config loaded from the fixtures-derived
default must rebuild the EXACT same system prefix the old fixtures-fed path produced. If
anyone perturbs the prefix (whitespace, key order, an extra field), this fails — which is
the point. Never weaken it to pass.
"""

from __future__ import annotations

import json
import re
import subprocess
from pathlib import Path

import pytest

from bean.config import Config, load_config
from bean.fixtures import (
    CATEGORIES,
    CATEGORY_DESCRIPTIONS,
    GOLDEN,
    KNOWLEDGE_DOCS,
    TEMPLATES,
)


def _by_id(email_id):
    return next(c.email for c in GOLDEN if c.email.id == email_id)


# ---- 1. the committed example config still loads -------------------------------------------

def test_example_config_file_carries_the_fixture_store():
    """The committed example file must stay in sync with the fixtures it documents.

    Compared field-by-field rather than whole: the example ships no `tree` (it never needed one —
    the tree was engine state, not store facts), and this used to be asserted indirectly by
    comparing the prompt prefix, which excluded the tree for the same reason."""
    from pathlib import Path as _Path

    example = _Path(__file__).resolve().parent.parent / "bean-config.example.json"
    cfg, fixtures = load_config(example), Config.from_fixtures()
    assert cfg.categories == fixtures.categories
    assert cfg.knowledge_docs == fixtures.knowledge_docs
    assert cfg.settings == fixtures.settings


# ---- 2. the deleted routing tree still loads off disk ---------------------------------------

def test_a_config_carrying_the_deleted_tree_still_loads_and_ignores_it(tmp_path):
    """The compatibility guarantee the tree deletion rests on.

    Every live config.json on the volume still carries the routing tree's
    `tree` key (and per-doc `image` paths) from the engine that was deleted in the notebook
    cutover. `from_dict` is `.get()`-based, so it reads past keys it no longer knows: an old file
    must keep loading, keep its categories/docs/settings intact, and simply stop round-tripping the
    dead keys back to disk. If this ever starts raising, every deployed tenant fails to boot.
    """
    p = tmp_path / "config.json"
    p.write_text(json.dumps({
        "categories": [{"name": "Orders", "description": "d", "template": "t"}],
        "knowledgeDocs": [{"title": "Returns", "body": "30 days", "image": "/data/shot.png"}],
        "settings": {"shopifyStore": "sable-and-wren"},
        "tree": [{"nodeType": "category", "label": "Orders", "children": [
            {"nodeType": "answer", "label": "WISMO", "trigger": "t", "answer": "a", "stakes": "low"}]}],
    }))

    cfg = load_config(p)
    assert cfg.category_names() == ["Orders"]
    assert cfg.knowledge_docs == {"Returns": "30 days"}
    assert cfg.settings == {"shopifyStore": "sable-and-wren"}
    # the dead keys are dropped, not preserved — the next save writes a config without them
    assert "tree" not in cfg.to_dict()
    assert cfg.to_dict()["knowledgeDocs"] == [{"title": "Returns", "body": "30 days"}]
    assert not hasattr(cfg, "tree")


def _seed(name: str) -> dict:
    from pathlib import Path

    return json.loads((Path(__file__).resolve().parent.parent / "bean" / name).read_text())


def test_sablewren_seed_is_normal_form_and_is_a_frozen_test_fixture():
    # bean/sablewren_seed.json must be in Config normal form (sorted docs) so sw_config() reproduces
    # the file exactly — that equality is what makes it a trustworthy fixture for the SW_GOLDEN
    # labels. The seed on disk still carries the deleted routing tree's `tree` key; Config reads
    # past it (see the compat test above), so normal form is asserted over everything else. That
    # dead key is deliberate: this seed is the live regression guard for the compatibility every
    # deployed tenant's config.json depends on.
    #
    # It is NOT a backup of prod and NOT a default. It is deliberately frozen: the SW_GOLDEN
    # confidence labels are graded against this exact snapshot, so re-seeding it from any live
    # config would move the goldens under us.
    from bean.fixtures import sw_config

    seed = {k: v for k, v in _seed("sablewren_seed.json").items() if k != "tree"}
    assert sw_config().to_dict() == seed


# The identity-separation invariant.
#
# The literals live OFF the tree, in a gitignored `tests/identity_denylist.json`, and this file
# only knows how to load them. That is not squeamishness — it is the same invariant applied to
# itself. This list is the real store, its handles, the operator and the founder; publishing it
# hands a reader the tidy answer key to a scrub that took weeks, in the one file whose whole job
# is keeping those names out. The project constraint says "not in fixtures, not in tests, not in
# screenshots", and a test is a test.
#
# So: the private checkout has the file and runs the real gate. The public tree does not have it,
# these assertions skip loudly, and nothing is disclosed. What still runs everywhere is
# `test_captured_mail_fixtures_name_only_invented_people`, which needs no denylist — it is
# provenance-based, and it is the one guarding people who never agreed to any of this.
#
# NOTHING in the denylist may be softened, and it only ever grows. Grep is not the gate; a grep
# nobody runs proves nothing.
_DENYLIST_PATH = Path(__file__).resolve().parent / "identity_denylist.json"


def _denylist():
    """(identifiers, order_re) from the private denylist, or None when it isn't present.

    Absent means "this is the public tree" (gitignored) — or a fresh private clone, where it must
    be restored before the identity gate means anything. See CLAUDE.md.
    """
    try:
        raw = json.loads(_DENYLIST_PATH.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return tuple(raw["identifiers"]), re.compile(raw["order_pattern"])


def _assert_no_real_identity(blob: str, label: str):
    loaded = _denylist()
    if loaded is None:
        pytest.skip(f"tests/identity_denylist.json absent — the real-identity gate cannot run here")
    identifiers, order_re = loaded
    for identifier in identifiers:
        assert identifier not in blob, f"{label} leaks {identifier!r}"
    assert not order_re.search(blob), f"{label} leaks a real order number"


# Words the second FICTIONAL store (Sable & Wren, furniture) legitimately uses. Wholly invented, so
# they stay here in the open. They are asserted only against the Maple & Moss DEMO DEFAULT, which is
# an apparel store — seeing "Harlow" in an apparel config means the furniture fixture has bled into
# the default a config-less tenant is served, which is exactly the leak this file exists to catch.
_LEAKED_FIXTURE_VOCABULARY = ("Harlow", "Sable & Wren", "sablewren", "sable-and-wren")


def test_the_demo_default_contains_nothing_of_a_real_customers():
    """The invariant production actually violated.

    `from_fixtures()` is what a tenant with no config.json is served, and their first save persists
    it. So the demo default must be wholly fictional. It used to carry a REAL customer's routing
    tree (`PRODUCT_TREES = _LIVE_SEED["tree"]`, seeded from that customer's own config), which meant
    a brand-new tenant booted into their tree and their authored answers — and the demo half of that
    same conflation was found persisted OVER the live config in prod on 2026-07-13.

    The tree it leaked through is deleted; the rule it taught is not. So this now scans the WHOLE
    shipped default rather than only the half that happened to carry the leak — a shipped default
    must contain nothing real, wherever the next real thing gets added.
    """
    default = Config.from_fixtures()

    blob = json.dumps(default.to_dict())
    # The fictional-vocabulary half needs no denylist, so it runs everywhere — do it first, before
    # the identity half is allowed to skip.
    for identifier in _LEAKED_FIXTURE_VOCABULARY:
        assert identifier not in blob, f"demo default leaks {identifier!r}"

    # ...and it is not a copy of the other fixture store by any other route either.
    assert default.to_dict() != {k: v for k, v in _seed("sablewren_seed.json").items() if k != "tree"}

    _assert_no_real_identity(blob, "demo default")


def test_the_shipped_fixture_store_carries_no_real_customer_identity():
    """The other half of the same invariant, and the one the release actually turned on.

    `sablewren_seed.json` + the SW_* fixture block replaced a real customer's config snapshot and
    her real support mail. The store, the operator, the founder and the order prefix are all
    invented now — this fails if any real identifier ever comes back through a "let's just re-seed
    from prod to make the goldens realistic" shortcut, which is precisely how the original data got
    in. The fictional vocabulary is NOT checked here: this store is meant to sell furniture.
    """
    from bean import fixtures
    from bean.fixtures import sw_config

    blob = json.dumps([
        sw_config().to_dict(),
        _seed("sablewren_seed.json"),
        [(c.email.id, c.email.subject, c.email.body, c.email.thread, c.note) for c in fixtures.SW_GOLDEN],
        [(e.id, e.sender_name, e.sender_email, e.subject, e.body) for e in fixtures.SW_NOTIFICATIONS],
        [(e.id, e.sender_name, e.sender_email, e.subject, e.body) for e in fixtures.SW_DEMO_FILED],
    ])
    _assert_no_real_identity(blob, "the shipped fixture store")


def test_the_committed_demo_inbox_carries_no_real_customer_identity():
    """The same invariant over the newest, and riskiest, shipped artifact.

    `bean/demo_inbox.json` is the public demo's inbox: eleven emails and eleven engine verdicts,
    committed, and served to anyone who opens the demo URL. It is the first thing in this repo that
    is BOTH generated by running the real engine AND published — which is exactly the shape that put
    a real customer's data on disk the first time (`web/bean-data.jsx`, generated on a laptop pointed
    at the live tenant, and the only demo inbox that existed until now).

    Generated FROM the fictional fixtures, so it should be clean by construction. This is the test
    that makes "should be" into "is", and that catches a regeneration run against the wrong tenant —
    the one mistake that would look completely normal in the diff.

    The fictional vocabulary is deliberately NOT checked: the demo store IS the furniture store,
    and the whole inbox is about Harlow sofas. Only the REAL identifiers are forbidden.
    """
    from bean.demo import load_demo_inbox

    blob = json.dumps(load_demo_inbox())
    _assert_no_real_identity(blob, "the committed demo inbox")


# ---- the same invariant, over the whole repo rather than three chosen blobs -------------------
#
# Everything above scans an artifact someone REMEMBERED to scan. That is the flaw those three tests
# share, and on 2026-08-05 it cost: `tests/fixtures/postmark_inbound_*.json` were captures of live
# support mail, carrying the store name, its Shopify handle, its Postmark inbound address, the
# founder's address, two real customers' email addresses and a real phone number. Every literal in
# `_REAL_IDENTIFIERS` was already sitting in that list. No test looked at that file, so none of it
# fired.
#
# Its predecessor was `git grep` for the operator's name, which missed 2,037 untracked hit lines.
# The lesson taken then was "grep the working tree, not the index". That was the wrong half: the
# gate does not need a better SEARCH, it needs a fixed SURFACE. So this reads the surface that
# actually ships — every tracked file — and it is a test, not a command someone has to remember.

_REPO_ROOT = Path(__file__).resolve().parent.parent

# Two files legitimately contain forbidden strings:
#   - this one, which stores the literals in order to forbid them.
#   - session-handoff.md, private working notes deleted at the public cut. If it ever stops being
#     deleted there, this exclusion is the thing that has to be revisited.
# Nothing is exempt any more, and that is the point of keeping the set.
#
# It held two entries. `tests/test_config_snapshot.py` was exempt because it stored the real
# identifiers in order to forbid them — they live in a gitignored denylist now, so this file has
# nothing left to hide from itself. `session-handoff.md` was exempt because it is private working
# notes deleted at the public cut — it is untracked now, so it never reaches this scan at all.
#
# Both exemptions were load-bearing holes: a file the gate skips is a file the gate does not
# protect, and the cut depended on a human remembering to delete one of them. If you find yourself
# adding an entry here, that is the moment to ask whether the file should be tracked instead.
_SCAN_EXEMPT: set[str] = set()


def _tracked_text_files():
    """Every tracked file, as (repo-relative path, text). Binaries are skipped, not read.

    Skips (rather than fails) outside a git checkout: the release process builds the public tree by
    copying `git ls-files` output into a plain directory, and the suite has to stay runnable there.
    """
    try:
        out = subprocess.run(["git", "ls-files", "-z"], cwd=_REPO_ROOT,
                             capture_output=True, check=True).stdout
    except (OSError, subprocess.CalledProcessError):
        pytest.skip("not a git checkout — nothing to enumerate")

    for name in out.decode().split("\0"):
        if not name or name in _SCAN_EXEMPT:
            continue
        path = _REPO_ROOT / name
        if not path.is_file():
            continue
        try:
            yield name, path.read_text(encoding="utf-8")
        except (UnicodeDecodeError, OSError):
            continue  # images and other binaries: opaque here, excluded by path in .gitignore


def test_no_tracked_file_anywhere_carries_the_real_operators_identity():
    """The scope fix: the whole shipped surface, not three remembered blobs."""
    loaded = _denylist()
    if loaded is None:
        pytest.skip("tests/identity_denylist.json absent — the real-identity gate cannot run here")
    identifiers, order_re = loaded

    leaks = []
    for name, text in _tracked_text_files():
        for identifier in identifiers:
            if identifier in text:
                leaks.append(f"{name}: {identifier!r}")
        if order_re.search(text):
            leaks.append(f"{name}: a real order number")

    assert not leaks, "real identity in tracked files:\n  " + "\n  ".join(leaks)


# ---- captured mail: the surface where OTHER people's identity enters --------------------------
#
# The check above would have caught three of the four leaked fixtures, because they named the
# store. It would NOT have caught the fourth: its store fields were already fictional and what
# leaked was two of that store's CUSTOMERS — people with no connection to this project, who never
# agreed to anything, and whose names no operator-identity list will ever contain.
#
# They cannot be forbidden as literals either, because ~23 consumer-domain addresses in this repo
# are deliberately invented golden customers, and a real customer's address is indistinguishable
# from `angela.pruitt@gmail.com` by inspection. (Writing the real one down here to illustrate that
# is the same mistake one level up — which is why _REAL_ORDER_RE is a pattern and not a literal.)
#
# What distinguishes them is PROVENANCE. Invented customers are authored in bean/fixtures.py; real
# ones arrive by pasting a captured Postmark payload into tests/fixtures/. So the strict rule binds
# to that surface — four small, slow-changing files where any address at all is suspicious — rather
# than to the whole corpus, where it would be unenforceable.

_CAPTURE_GLOB = "tests/fixtures/postmark_inbound_*.json"

# example.com/.net/.org and .example are RFC 2606: reserved forever, so they cannot become a real
# person's address later. The rest are the fictional store and the platforms it transacts with.
_INVENTED_DOMAINS = {"example.com", "example.net", "example.org",
                     "sableandwren.example", "shopify.com", "inbound.postmarkapp.com"}

_ANY_EMAIL_RE = re.compile(r"[A-Za-z0-9._%+-]+@([A-Za-z0-9.-]+\.[A-Za-z]{2,})")
# 10 digits with optional separators. Deliberately does NOT match a 7-digit local number, so it is
# a floor rather than a proof — it catches the shape that actually leaked (`305469-3864`) and the
# fictional 555-01xx range passes it by being too short to match.
_PHONE_RE = re.compile(r"\b\d{3}[-.\s]?\d{3}[-.\s]?\d{4}\b")


def test_captured_mail_fixtures_name_only_invented_people():
    """Inbound fixtures must be AUTHORED, never captured from a live mailbox.

    These four files are read by tests/test_inbound.py and are tracked, so whatever is in them
    ships. They were captures until 2026-08-05, hidden from every gate by a blanket `fixtures/`
    ignore rule that also broke `pytest` on fresh clones — one rule doing two jobs, badly.
    """
    paths = sorted(_REPO_ROOT.glob(_CAPTURE_GLOB))
    assert paths, f"no fixtures matched {_CAPTURE_GLOB} — did they get ignored again?"

    for path in paths:
        text = path.read_text(encoding="utf-8")
        name = path.relative_to(_REPO_ROOT)

        for domain in set(_ANY_EMAIL_RE.findall(text)):
            assert domain in _INVENTED_DOMAINS, (
                f"{name} carries an address at {domain!r}, which is not a known-invented domain. "
                f"If this came out of a real mailbox it is someone's actual mail: rewrite it as "
                f"invented Sable & Wren mail. If the store genuinely gained a new fictional domain, "
                f"add it to _INVENTED_DOMAINS deliberately."
            )

        assert not _PHONE_RE.search(text), (
            f"{name} carries a phone-shaped number. A real customer's phone leaked this way once; "
            f"use the fictional 555-01xx range."
        )


def test_a_real_tenant_config_is_never_the_demo_default():
    """What `/healthz` reports as `config_is_demo_default`, asserted here as an invariant.

    Identity on the whole category set — NOT name overlap. A real tenant may legitimately share a
    category name with the demo store; the Sable & Wren config really does contain "Wholesale" and
    "Escalation". Only an exact match on names+descriptions+templates means the demo store has been
    served as somebody's config, which is precisely what happened in prod.
    """
    from bean.fixtures import sw_config

    demo = Config.from_fixtures()
    assert sw_config().categories != demo.categories

    # ...and the too-strict version is genuinely wrong, so nobody "fixes" the test into a lie:
    shared = {c.name for c in sw_config().categories} & {c.name for c in demo.categories}
    assert shared, "name overlap is legitimate — the invariant must not be disjointness"


def test_proposals_roundtrip_and_default_off_disk():
    # proposals: Bean's pending suggestions, omitted when empty. (This test also covered TreeNode's
    # `provenance` field — who authored a node — which went with the tree itself.)
    base = Config.from_fixtures()
    assert "proposals" not in base.to_dict()
    prop = {"id": "prop-x", "kind": "template", "path": ["Meter"], "answer": "…"}
    with_props = Config(categories=base.categories, knowledge_docs=base.knowledge_docs,
                        settings=base.settings, proposals=(prop,))
    assert with_props.to_dict()["proposals"] == [prop]
    assert Config.from_dict(with_props.to_dict()) == with_props


def test_gate_rules_roundtrip_and_default_off_disk():
    # Config.gate feeds the triage gate (bean/gate.py). Empty ⇒ omitted on disk (byte-identity
    # guard intact); a set of rules must survive the PUT/GET camelCase round-trip untouched, or a
    # wiring regression would silently drop the operator's overrides.
    base = Config.from_fixtures()
    assert "gate" not in base.to_dict()
    rules = {"alwaysReply": ["wholesale", "@retailpartner.com"], "alwaysFile": ["newsletter@"]}
    gated = Config(categories=base.categories, knowledge_docs=base.knowledge_docs,
                   settings=base.settings, gate=rules)
    assert gated.to_dict()["gate"] == rules
    assert Config.from_dict(gated.to_dict()) == gated


# ---- 2. malformed-JSON fallback ------------------------------------------------------------

def test_corrupt_config_with_no_backup_raises_not_fixtures(tmp_path):
    # CONTRACT CHANGE (was test_malformed_json_falls_back_and_logs): a config file that EXISTS but
    # is unparseable must NOT degrade to fixtures — that silent swap served demo templates as the
    # customer's config and let the next save overwrite her real data. With no .bak to recover from,
    # loud failure is the only safe outcome.
    from bean.config import ConfigCorruptError

    bad = tmp_path / "config.json"
    bad.write_text("{ this is not valid json ]]]")

    with pytest.raises(ConfigCorruptError):
        load_config(bad)


def test_missing_file_falls_back(tmp_path):
    cfg = load_config(tmp_path / "does-not-exist.json")
    assert cfg == Config.from_fixtures()


def test_none_path_falls_back():
    assert load_config(None) == Config.from_fixtures()
