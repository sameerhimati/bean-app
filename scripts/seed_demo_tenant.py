"""Seed the whole public demo tenant — config + notebook + inbox — from committed fixtures.

    python scripts/seed_demo_tenant.py                    # seed data/sablewren/ (no-op if seeded)
    python scripts/seed_demo_tenant.py --force            # rewrite all three from the fixtures
    BEAN_DATA_DIR=/data python scripts/seed_demo_tenant.py   # …onto the mounted volume

NO API KEY. No model call, no network — every byte comes off disk. That is the requirement the demo
service is built on: it is deployed WITHOUT `ANTHROPIC_API_KEY` (with `BEAN_DEMO_READONLY=1`) so a
public URL cannot spend a cent, and a seeder that needed a key would put the key back on the box.
The one step that DOES call the model is `scripts/seed_demo_inbox.py`, run once by a human, whose
output is committed as `bean/demo_inbox.json`.

This is what runs on deploy, ahead of the server:

    startCommand = "python scripts/seed_demo_tenant.py && python -m bean.server"

which is why it is idempotent rather than fussy: an already-seeded volume is a quiet no-op and exit
0, not a nonzero exit that crash-loops the container. Deliberately NOT wired into railway.toml — the
production service shares that file, and running a demo seeder there is precisely the direction of
the fixtures-leaked-into-prod bug. The demo service overrides the start command itself.

Refuses point-blank to write into a folder that holds a corrections log, `--force` or not: a tenant
with correction history is a REAL operator and their brain is not ours to regenerate.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))  # repo root, so `bean` imports

from bean.demo import DEMO_CUSTOMER, DemoSeedRefused, seed_tenant
from bean.paths import customer_dir


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--customer", default=DEMO_CUSTOMER,
                    help=f"tenant folder to seed (default: {DEMO_CUSTOMER!r})")
    ap.add_argument("--force", action="store_true",
                    help="rewrite files that already exist (the wall below still holds)")
    args = ap.parse_args()

    try:
        did = seed_tenant(args.customer, force=args.force)
    except DemoSeedRefused as exc:
        print(f"REFUSING: {exc}", file=sys.stderr)
        return 2
    # A missing fixture is loud rather than an empty demo — see bean/demo.py: load_demo_inbox.
    except FileNotFoundError as exc:
        print(f"REFUSING: the demo inbox fixture is missing ({exc}).\n"
              f"  Generate it once with: ANTHROPIC_API_KEY=... python scripts/seed_demo_inbox.py",
              file=sys.stderr)
        return 2

    where = customer_dir(args.customer)
    print(f"demo tenant {args.customer!r} at {where}")
    for name in ("config", "notebook", "inbox"):
        print(f"  {did[name]:>5}  {name}")
    if all(v == "kept" for v in did.values()):
        print("  (already seeded — nothing to do. Pass --force to rewrite.)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
