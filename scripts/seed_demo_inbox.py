"""Generate the committed demo inbox by running the Sable & Wren goldens through the REAL engine.

    ANTHROPIC_API_KEY=... python scripts/seed_demo_inbox.py            # regenerate bean/demo_inbox.json
    ANTHROPIC_API_KEY=... python scripts/seed_demo_inbox.py --dry-run  # print the spread, write nothing

This is the one step in the demo path that spends tokens, and it runs ONCE — a human runs it, reads
the spread it prints, and commits the result. Everything downstream (`bean/demo.py: seed_tenant`, the
deployed service) reads the committed file and needs no key at all.

WHY GENERATE RATHER THAN AUTHOR. A demo inbox is trivially fakeable: write nine drafts, label three
green and one red, ship it. Bean's entire pitch is that its confidence labels mean something, so a
demo whose labels were assigned by a human — even a well-intentioned one — would be the product
lying about the exact property it is selling. So every verdict in `bean/demo_inbox.json` is a verdict
the engine actually reached, on the same code path `/api/inbound` uses:

    goldens (SW_GOLDEN)   → skip the gate, draft → the engine's own bucket/confidence/citations
    non-customer mail     → through the REAL gate, and it lands filed only if the gate files it

If the spread that comes out is thin — no green, or nothing red — that is a FINDING about the
notebook, to be fixed in the notebook. Do not hand-edit the fixture into a better-looking demo.

Idempotent: it rewrites the whole fixture from scratch every time, never appends, so running it
twice leaves you with one demo inbox and not two. It runs at temperature=0 (the replay verifier's
setting) to hold the VERDICTS steady — two runs measured back to back produced the identical bucket
and colour for all eleven emails — but not the prose: the drafts and the why-unsure lines are
reworded each time. So expect a real diff on every regeneration, and read the printed spread rather
than the diff to decide whether anything actually changed.

WHAT THE DEMO NOTEBOOK KNOWS. The demo tenant has no corrections log — nobody has taught it — so the
shelf hands the engine ZERO exemplars and the customer-history block is empty. Grounding comes purely
from the notebook's `stated` facts. That is genuinely day zero for a new store, which is what the
demo should be showing; it also means the greens here are the hardest kind to earn (no precedent to
lean on), so a green in this fixture is a real one.
"""

from __future__ import annotations

import argparse
import os
import sys
from dataclasses import asdict
from datetime import datetime, timedelta, timezone
from email.utils import format_datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))  # repo root, so `bean` imports

from bean.adapter import ModelAdapter
from bean.coldstart import sablewren_notebook
from bean.contract import draft_dict
from bean.demo import save_demo_inbox
from bean.engine import draft_email
from bean.fixtures import SW_DEMO_FILED, SW_GOLDEN
from bean.gate import needs_reply
from bean.inbox import InboxItem
from bean.llm import DRAFT_MODEL

# When the demo's mail "arrived". Baked into the fixture (not computed at seed time) so the file is
# deterministic and the tests can assert on it. The dates sit just after the order notifications in
# SW_NOTIFICATIONS (2026-06-30 / 07-01) so the little store's timeline hangs together, and they are
# RFC-2822 strings because that is exactly what `/api/inbound` stores — Postmark's `Date` header,
# verbatim. The UI renders `received_at` raw, so a prettier format here would make the demo look
# nicer than the real thing, which is the wrong direction to be dishonest in.
_FIRST_RECEIVED = datetime(2026, 7, 2, 9, 12, tzinfo=timezone(timedelta(hours=-7)))
# A plausible trickle rather than eleven emails at once, and tight enough that the whole demo inbox
# lands inside one working day — mail timestamped 01:37 reads as fake in a way nobody can name.
_SPACING = timedelta(minutes=55)


def _received_at(index: int) -> str:
    return format_datetime(_FIRST_RECEIVED + index * _SPACING)


def _item(email, received_at: str, result: dict) -> dict:
    """One InboxItem dict, built exactly as `server._handle_inbound` builds it — same fields, same
    `reply_to` (the customer, since none of these arrive via a relay), same stored `result`."""
    return asdict(InboxItem(
        id=email.id,
        sender_name=email.sender_name,
        sender_email=email.sender_email,
        reply_to=email.sender_email,
        subject=email.subject,
        body=email.body,
        received_at=received_at,
        thread=list(email.thread),
        result=result,
    ))


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--dry-run", action="store_true", help="print the spread, write nothing")
    args = ap.parse_args()

    # Checked HERE, before a single email is processed. The failure without this is the worst kind:
    # the first gate call raises deep inside the SDK, several seconds and one confusing traceback in,
    # and the operator is left guessing whether it was the key, the network, or the fixture.
    if not os.environ.get("ANTHROPIC_API_KEY", "").strip():
        print(
            "REFUSING: ANTHROPIC_API_KEY is not set.\n"
            "  This script calls the real model — it is the one step in the demo path that does.\n"
            "  Put the key in .env (it is gitignored) or export it, then re-run.\n"
            "  Everything downstream (scripts/seed_demo_tenant.py, the deployed demo) reads the\n"
            "  committed fixture and needs no key.",
            file=sys.stderr,
        )
        return 2

    notebook = sablewren_notebook()
    # temperature=0: the replay verifier's setting. It does NOT make the file byte-stable (the prose
    # rewords on every run), but it holds the thing that matters — "did the notebook change the
    # verdict?" is unanswerable if the sampler is changing it too.
    adapter = ModelAdapter(DRAFT_MODEL, temperature=0)

    items: list[dict] = []
    spread: dict[str, int] = {}
    index = 0

    print(f"drafting {len(SW_GOLDEN)} customer emails against the {notebook.store} notebook "
          f"({len(notebook.buckets)} buckets · {len(notebook.facts)} facts · no corrections yet)\n")
    for case in SW_GOLDEN:
        email = case.email
        # No gate: the goldens are the calibration customers, definitionally needs-reply. Same
        # decision the retired cli.run_inbox made, and the reason SW_DEMO_FILED exists separately.
        # Exemplars/history are empty because the demo tenant has no corrections — see the module note.
        result = draft_email(notebook, [], list(email.thread), None, email, adapter)
        items.append(_item(email, _received_at(index), draft_dict(result)))
        spread[result.confidence.value] = spread.get(result.confidence.value, 0) + 1
        index += 1
        print(f"  {result.confidence.value.upper():6} {email.id:<18} {result.bucket}")
        if result.why_unsure:
            print(f"         └ {result.why_unsure[0]}")

    print(f"\ngating {len(SW_DEMO_FILED)} non-customer emails (the Filed/FYI lane)\n")
    for email in SW_DEMO_FILED:
        g = needs_reply(email)
        if g.disposition == "file":
            result = {"disposition": "filed", "kind": g.kind, "reason": g.reason}
            spread["filed"] = spread.get("filed", 0) + 1
            print(f"  {'FILED':6} {email.id:<18} {g.kind} — {g.reason}")
        else:
            # The gate let it through. NOT overridden: the gate's verdict is the demo's verdict, and
            # a newsletter that reaches the drafting path is a real thing that happens on real mail.
            result = draft_dict(draft_email(notebook, [], list(email.thread), None, email, adapter))
            conf = result["confidence"]
            spread[conf] = spread.get(conf, 0) + 1
            print(f"  {conf.upper():6} {email.id:<18} gate said REPLY, so it was drafted")
        items.append(_item(email, _received_at(index), result))
        index += 1

    order = ("green", "yellow", "red", "filed")
    print("\nspread: " + " · ".join(f"{k}={spread.get(k, 0)}" for k in order))
    # The thesis needs all four lanes visible, but a MISSING lane is a finding, not a failure — the
    # honest response is to say so and let a human decide whether the notebook or the goldens move.
    # Faking one here would be the yes-man bug, wearing a fixture as a disguise.
    missing = [k for k in order if not spread.get(k)]
    if missing:
        print(f"NOTE: the engine produced nothing in these lanes: {', '.join(missing)}. "
              f"That is what it did — fix the notebook, not the fixture.")

    u = adapter.total
    print(f"tokens: in={u.input_tokens} out={u.output_tokens} "
          f"cache_write={u.cache_creation_input_tokens} cache_read={u.cache_read_input_tokens}")

    if args.dry_run:
        print("\n--dry-run: nothing written")
        return 0
    save_demo_inbox(items)
    print(f"\nwrote {len(items)} items to bean/demo_inbox.json — commit it")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
