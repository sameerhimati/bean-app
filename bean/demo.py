"""The public demo tenant — a whole Bean, seeded from committed fixtures, that spends nothing.

Bean had no demo data path. The only inbox anyone could click through came from `web/bean-data.jsx`,
a gitignored file generated on somebody's laptop from whatever tenant that laptop was pointed at —
which meant the local "demo" was the REAL customer's mail, and a deploy (where the file is absent)
got a synthesized one with an EMPTY inbox. Both answers are wrong for a link you hand to a stranger:
one leaks, the other shows nothing. This module is the third answer — a fictional store, a real
notebook, and real engine output, all committed.

Three pieces, and the split between them is the point:

  1. `bean/demo_inbox.json` — the INBOX FIXTURE. Produced ONCE by running the Sable & Wren goldens
     through the real engine (`scripts/seed_demo_inbox.py`, which needs an API key) and committed.
     Every verdict in it is a verdict Bean actually reached: nothing in it is hand-written. A demo
     with hand-written drafts would be a product that lies about its own calibration, which is the
     exact failure this product exists to prevent — so the fixture is generated, never authored, and
     whatever spread the engine produced is the spread the demo shows.
  2. `seed_tenant()` — materializes a tenant folder from that fixture plus the frozen config and
     the cold-start notebook. NO API KEY, no model call, no network. This is what runs on deploy.
  3. `BEAN_DEMO_READONLY` in `bean/server.py` — the write lock that makes the result safe to expose.

WHY A SCRIPT AND NOT A BOOT-TIME FALLBACK: exactly the reason `bean/paths.py` (notebook_path)
forbids a shipped default notebook. A demo brain that materializes itself is one save away from
overwriting a real operator's — that is [[fixtures-leaked-into-prod]], which has already happened
here once. Materializing is somebody's explicit act. `tenant_is_taught` is the wall under that:
a tenant with a corrections log is a REAL operator, and their brain is not ours to regenerate.
"""

from __future__ import annotations

import json
import os
from pathlib import Path

from bean.coldstart import sablewren_notebook
from bean.fixtures import sw_config
from bean.paths import config_path, corrections_path, inbox_path, notebook_path
from bean.store import write_json_atomic

# The demo's tenant folder. Named after the store it runs (the same convention as
# `paths.DEFAULT_CUSTOMER = "maplemoss"`), so `data/sablewren/` reads as what it is at a glance and
# can never be confused with a real operator's directory.
#
# ⚠️ This string is also the demo SERVICE's `BEAN_CUSTOMER` env var. The seeder writes the folder
# this names and the server reads the folder `BEAN_CUSTOMER` names; if they disagree the container
# boots onto an empty tenant and 503s its own healthcheck. Change both or neither.
DEMO_CUSTOMER = "sablewren"

# The committed inbox fixture. It ships in the IMAGE (repo file), not on the volume, because it is
# shared baseline rather than accrued per-tenant state — see the bean/paths.py layout note.
DEMO_INBOX_FIXTURE = Path(__file__).resolve().parent / "demo_inbox.json"


class DemoSeedRefused(RuntimeError):
    """Raised by `seed_tenant` when the target folder belongs to a real operator. Deliberately an
    exception and not a return value: a caller that forgets to check a boolean regenerates somebody's
    brain from fixtures, and the whole point of the wall is that it cannot be walked past by accident.
    """


def tenant_is_taught(customer: str) -> bool:
    """True when this tenant has a corrections log — i.e. a human has been teaching it.

    The ONE question every demo seeder must ask before it writes. A corrections log is the moat: it
    is the record of what a real operator would have said instead of Bean, and it is the only thing
    in the system that cannot be regenerated. Its presence means "there is a person behind this
    folder", so every seeder refuses, and `--force` does not override it. It is not a guardrail,
    it is a wall.
    """
    return corrections_path(customer).exists()


def load_demo_inbox() -> list[dict]:
    """The committed inbox fixture as a list of `InboxItem`-shaped dicts — exactly the records
    `GET /api/inbox` serves, each carrying the `draft_dict(DraftResult)` the engine emitted.

    Raises FileNotFoundError when the fixture has never been generated. LOUD on purpose: a demo that
    silently degrades to an empty inbox is the state this module exists to end, and it is the state
    that hid for months behind `_serve_data_jsx`'s synthesized empty file.
    """
    return json.loads(DEMO_INBOX_FIXTURE.read_text(encoding="utf-8"))


def save_demo_inbox(items: list[dict]) -> None:
    """Rewrite the committed inbox fixture (`scripts/seed_demo_inbox.py` is the only caller).

    A repo file, not volume state — but written through the same atomic path anyway: the seeder
    makes ~11 live model calls to produce this, and losing the lot to a truncating write that got
    interrupted would cost real tokens to redo. Pretty-printed (write_json_atomic uses indent=2)
    because this is a file humans read in diffs.
    """
    write_json_atomic(DEMO_INBOX_FIXTURE, items, backup=False)


def _write_inbox_jsonl(path: Path, items: list[dict]) -> None:
    """Materialize the fixture as the tenant's `inbox.jsonl` (one JSON line per item).

    Whole-file, not append: seeding is "make this folder be exactly the demo", and appending would
    double every email on a re-run. Written tmp-then-`os.replace` for the same reason
    `inbox.clear_filed` does — the live file is either wholly the old one or wholly the new one, so
    a crash mid-seed can never leave the demo serving half an inbox.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    # Not write_json_atomic: this is JSONL (a stream of lines), not one JSON document. The atomicity
    # discipline is identical though — the only non-atomic write here is to `.tmp`, a scratch path no
    # reader ever opens, and the live file changes solely through the atomic replace below.
    tmp.write_text("".join(json.dumps(i, sort_keys=True) + "\n" for i in items), encoding="utf-8")
    os.replace(tmp, path)  # atomic


def seed_tenant(customer: str | None = None, *, force: bool = False) -> dict:
    """Write a complete demo tenant — config + notebook + inbox — from committed fixtures.

    No API key, no model call, no network: every byte comes off disk. That is the requirement, not a
    nicety. The demo service is deployed WITHOUT `ANTHROPIC_API_KEY` precisely so that a public URL
    cannot spend money, and a seeder that needed one would put the key back on the box.

    IDEMPOTENT BY DEFAULT, and that is a deploy requirement rather than a preference: this runs on
    every container boot, so an already-seeded folder must be a quiet no-op instead of a nonzero exit
    that crash-loops the service. `force=True` rewrites the three files (the notebook and config keep
    their own rolling `.bak`), which is how you pick up a regenerated fixture.

    Raises `DemoSeedRefused` when the folder holds a corrections log — see `tenant_is_taught`.
    Returns what it did, per file: {"config": "wrote"|"kept", ...}.
    """
    customer = customer or DEMO_CUSTOMER
    if tenant_is_taught(customer):
        raise DemoSeedRefused(
            f"{customer!r} has a corrections log — that is a real operator, not a demo tenant.\n"
            f"  {corrections_path(customer)}"
        )

    inbox = load_demo_inbox()  # read BEFORE any write: a missing fixture must not leave a half-seed
    did: dict[str, str] = {}

    cfg = config_path(customer)
    if force or not cfg.exists():
        write_json_atomic(cfg, sw_config().to_dict())
        did["config"] = "wrote"
    else:
        did["config"] = "kept"

    nb = notebook_path(customer)
    if force or not nb.exists():
        sablewren_notebook().save(nb)
        did["notebook"] = "wrote"
    else:
        did["notebook"] = "kept"

    ib = inbox_path(customer)
    if force or not ib.exists():
        _write_inbox_jsonl(ib, inbox)
        did["inbox"] = "wrote"
    else:
        did["inbox"] = "kept"

    return did
