"""Write the demo tenant's notebook — the one command that boots a store Bean has never seen.

This is the cold-start path made runnable (`bean/coldstart.py`). It projects the Maple & Moss
fixture config into a notebook and writes it to that tenant's folder on the data volume, which is
everything the notebook engine needs to start drafting:

    BEAN_CUSTOMER=maplemoss python scripts/seed_demo_notebook.py
    BEAN_CUSTOMER=maplemoss python -m bean.server

It is a SCRIPT and not a boot-time fallback on purpose. `bean/paths.py` (notebook_path) forbids a
shipped default notebook and `server.py:_current_notebook` 503s rather than fabricate one, because a
demo brain that materializes itself is one save away from overwriting a real operator's — that is
[[fixtures-leaked-into-prod]]. Materializing has to be somebody's explicit act, so it lives here.

Refuses to overwrite an existing notebook without `--force`, and refuses point-blank to write into a
customer folder that holds a corrections log — a tenant with correction history is a REAL operator,
and their brain is not ours to regenerate.

Usage:
    BEAN_CUSTOMER=maplemoss python scripts/seed_demo_notebook.py
    BEAN_CUSTOMER=maplemoss python scripts/seed_demo_notebook.py --force   # rewrite it
    python scripts/seed_demo_notebook.py --print                           # stdout, write nothing
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))  # repo root, so `bean` imports

from bean.coldstart import demo_notebook
from bean.demo import tenant_is_taught
from bean.paths import corrections_path, default_customer, notebook_path


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--customer", help="tenant folder to seed (default: BEAN_CUSTOMER env)")
    ap.add_argument("--force", action="store_true", help="overwrite an existing notebook")
    ap.add_argument("--print", dest="show", action="store_true", help="print to stdout, write nothing")
    args = ap.parse_args()

    notebook = demo_notebook()
    if args.show:
        print(notebook.render())
        return 0

    customer = args.customer or default_customer()
    path = notebook_path(customer)

    # A corrections log means a human has been teaching this tenant. Regenerating their notebook from
    # fixtures would replace a learned brain with a demo one — the exact direction of the
    # fixtures-leaked-into-prod bug. --force does not override this; it is not a guardrail, it's a
    # wall. The predicate now lives in bean/demo.py so this script and the demo-tenant seeder cannot
    # drift into two different ideas of "is somebody real behind this folder".
    if tenant_is_taught(customer):
        print(
            f"REFUSING: {customer!r} has a corrections log — that's a real operator, not a demo "
            f"tenant.\n  {corrections_path(customer)}",
            file=sys.stderr,
        )
        return 2

    if path.exists() and not args.force:
        print(f"REFUSING: {path} already exists (pass --force to overwrite)", file=sys.stderr)
        return 2

    notebook.save(path)
    print(
        f"wrote {path}\n"
        f"  {len(notebook.buckets)} buckets · {len(notebook.macros)} macros · "
        f"{len(notebook.facts)} facts · {len(notebook.notes)} notes (day zero — no precedent yet)"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
