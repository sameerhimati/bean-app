"""Recover the drafted-mail day counts that the first Clear sweep deleted without rolling up.

    BEAN_DATA_DIR=/data BEAN_CUSTOMER=<tenant> python3 scripts/backfill_drafted_history.py --dry-run
    BEAN_DATA_DIR=/data BEAN_CUSTOMER=<tenant> python3 scripts/backfill_drafted_history.py --apply

`clear_ids` shipped on 2026-08-19 without a rollup (bean/inbox.py explains why that reasoning was
wrong). The operator swept three weeks of actioned mail on 2026-08-21, and the stats page's daily
"drafted" bars — which are built from inbox.jsonl — went flat for every cleared day.

The mail itself is not gone. Every sweep writes `inbox.jsonl.bak-cleared-*` holding the FULL inbox
as it stood immediately before the rewrite, so the deleted lines are still on the volume. This
reconstructs the per-day counts from those snapshots.

HOW THE DELETED SET IS RECOVERED. A backup is a whole-inbox snapshot, not a list of what was
removed, so a line is "deleted at step i" iff its id appears in snapshot i and not in the state that
follows it. Chronologically: the ordered `.bak-*` files, then the live inbox last. New mail arriving
between two clears only ever ADDS ids to the later state, so it cannot make a surviving line look
deleted. Only WALKED lines are counted — filed ones were already rolled up correctly by
`clear_filed`, and counting them here would double them.

IDEMPOTENT BY CONSTRUCTION. It compares the reconstructed totals against what the archive already
holds and appends only the shortfall, so a second run writes nothing. That matters because
filed_history.jsonl is append-only and summed on read: a naive re-run would silently double a bar.

One-shot and hand-run. It is deliberately NOT wired into the start command — a migration that runs
itself on every boot is how a volume gets quietly rewritten by a redeploy.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from bean.inbox import load_drafted_history  # noqa: E402
from bean.outcomes import daily_counts  # noqa: E402
from bean.paths import filed_history_path, inbox_path  # noqa: E402


def _records(path: Path) -> list[dict]:
    out = []
    for line in path.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        try:
            out.append(json.loads(line))
        except (json.JSONDecodeError, ValueError):
            continue  # a corrupt line has no id and cannot be attributed either way
    return out


def _is_walked(rec: dict) -> bool:
    """Mirrors bean.inbox._is_filed, inverted: a filed line is `disposition: filed` with no chunks."""
    res = rec.get("result") or {}
    if not isinstance(res, dict):
        return False
    return not (res.get("disposition") == "filed" and not res.get("chunks"))


def reconstruct(inbox: Path) -> tuple[dict[str, int], list[str]]:
    """Per-day counts of WALKED mail deleted across every sweep, plus the snapshots it read."""
    snaps = sorted(inbox.parent.glob(f"{inbox.name}.bak-cleared-*"))
    if not snaps:
        return {}, []
    states = [_records(p) for p in snaps]
    states.append(_records(inbox) if inbox.exists() else [])

    deleted: list[dict] = []
    for earlier, later in zip(states, states[1:]):
        survived = {r.get("id") for r in later}
        deleted.extend(r for r in earlier if r.get("id") not in survived and _is_walked(r))

    rows, undated = daily_counts(deleted)
    if undated:
        print(f"  note: {undated} recovered lines carried no usable date and are not counted")
    return {r["day"]: r["drafted"] for r in rows if r["drafted"]}, [p.name for p in snaps]


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    mode = ap.add_mutually_exclusive_group(required=True)
    mode.add_argument("--dry-run", action="store_true", help="report what would be written")
    mode.add_argument("--apply", action="store_true", help="append the shortfall to the archive")
    args = ap.parse_args(argv)

    inbox, history = inbox_path(), filed_history_path()
    print(f"inbox:   {inbox}")
    print(f"archive: {history}")

    recovered, snaps = reconstruct(inbox)
    if not snaps:
        print("\nNo `.bak-cleared-*` snapshots on this volume — nothing was ever swept. Nothing to do.")
        return 0
    print(f"\nread {len(snaps)} snapshot(s): {', '.join(snaps)}")

    have = load_drafted_history(history)
    shortfall = {d: n - have.get(d, 0) for d, n in recovered.items() if n > have.get(d, 0)}

    print(f"\n{'day':<14}{'recovered':>10}{'archived':>10}{'to write':>10}")
    for day in sorted(recovered):
        print(f"{day:<14}{recovered[day]:>10}{have.get(day, 0):>10}{shortfall.get(day, 0):>10}")
    total = sum(shortfall.values())
    print(f"\n{total} drafted email(s) to restore across {len(shortfall)} day(s)")

    if not total:
        print("Archive already agrees with the snapshots — nothing to write. (Safe to re-run.)")
        return 0
    if args.dry_run:
        print("\nDry run. Re-run with --apply to write.")
        return 0

    history.parent.mkdir(parents=True, exist_ok=True)
    with history.open("a", encoding="utf-8") as fh:
        for day in sorted(shortfall):
            fh.write(json.dumps({"day": day, "drafted": shortfall[day]}, sort_keys=True) + "\n")
    print(f"\nwrote {len(shortfall)} row(s) to {history}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
