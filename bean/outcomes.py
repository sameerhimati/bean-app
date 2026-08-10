"""Reads the verdict distribution Bean actually produced on REAL customer mail.

Nothing had ever read this. Every verification surface Bean has — `/healthz`, `make eval`, the
preflight — measures whether Bean is correctly CONFIGURED and honestly CAUTIOUS. None of them
measures whether it is USEFUL. So Bean flagged 100% of the operator's mail for weeks with every
light green, and the one surface that did read real outcomes rendered the failure as a
healthy-looking to-do list: a to-do list has no denominator, and the denominator is the entire
story.

This module is that denominator. Of the customer emails that actually REACHED THE ENGINE, how many
did Bean draft (HIGH + LOW) and how many did it flag?

Two exclusions, both load-bearing:

  * Gate-FILED mail (newsletters, receipts, carrier noise) is NOT counted. It never reaches the
    engine, so it is not a sample of Bean's judgment — and a store whose inbox is half USPS notices
    would otherwise look half "handled" while drafting nothing. It is reported separately so the
    pipe stays visible.
  * An empty denominator scores 0.0, never 1.0. An agent that has drafted nothing because it has
    seen nothing is not a 100% success; it is UNMEASURED. The eval's `precision = ... if high else
    1.0` was exactly this mistake, and it is what let an all-flag Bean score perfectly.

    python -m bean.outcomes                 # the default customer's inbox
    python -m bean.outcomes --customer NAME # a specific tenant's
"""

from __future__ import annotations

import argparse
from dataclasses import dataclass, field
from pathlib import Path

from bean.corrections import Correction, outcome_of
from bean.inbox import load_inbox
from bean.paths import inbox_path

# How many of the most recent walked emails `/healthz` judges on. Long enough that a run of
# legitimately-escalated mail doesn't read as a stall, short enough that a tree taught yesterday
# shows up today (a lifetime average would be dragged down by every email from before the fix).
RECENT_WINDOW = 20

# Below this many walked emails, `drafting_stalled` stays False no matter what — not because a
# small all-flag sample is fine, but because an alarm that fires on n=1 is an alarm that gets
# ignored, which is precisely how the original blindness survived. The operator's own goldens expect a
# FLAG on ~40% of mail, so five consecutive honest flags happen roughly 1% of the time on a HEALTHY
# Bean; zero drafts in five is already a real signal, and in twenty it is unambiguous.
MIN_STALL_SAMPLE = 5


@dataclass
class Outcomes:
    """One window's verdict counts. `unknown` tracks walked emails whose stored result carries a
    confidence this code doesn't recognize — a data bug, surfaced rather than folded into `flag`
    (which would flatter Bean's caution) or `drafted` (which would flatter its usefulness). It sits
    in the `walked` denominator either way, so a corrupt result can never inflate the fraction."""

    walked: int = 0  # customer emails that ran the chunk→route→resolve loop
    high: int = 0
    low: int = 0
    flag: int = 0
    unknown: int = 0
    filed: int = 0  # gate-filed: never reached the engine, never a sample of Bean's judgment

    @property
    def drafted(self) -> int:
        """HIGH + LOW. Both are a reply the operator can approve with one tap; only FLAG is Bean
        asking them to do the work themselves, which is the thing that saves them no time."""
        return self.high + self.low

    @property
    def drafted_fraction(self) -> float:
        return self.drafted / self.walked if self.walked else 0.0

    def share(self, n: int) -> float:
        return n / self.walked if self.walked else 0.0


def _is_walked(result: dict) -> bool:
    """A result that is a sample of Bean's judgment on a customer email — a tree walk (per-chunk
    breakdown, HIGH/LOW/FLAG) or a notebook DraftResult (bucket + groundedness green/yellow/red) — as
    opposed to a gate-FILED record, which stores `{"disposition": "filed", ...}` and never reached an
    engine. Discriminate on the filed marker, not `chunks`: a DraftResult has no chunks yet IS walked,
    so a `chunks` test would wrongly bucket every notebook-engine draft as filed and zero the fraction."""
    return bool(result) and result.get("disposition") != "filed"


def _walked_only(inbox: list[dict]) -> list[dict]:
    """The customer mail that reached an engine (tree or notebook). Gate-filed mail is excluded."""
    return [i for i in inbox if _is_walked(i.get("result") or {})]


def aggregate(inbox: list[dict]) -> Outcomes:
    """Count verdicts over `inbox` (raw lines, filed mail included)."""
    out = Outcomes()
    for item in inbox:
        result = item.get("result") or {}
        if not _is_walked(result):
            out.filed += 1
            continue
        out.walked += 1
        # Both engines' vocabularies collapse to the same three affordances: one-tap (tree HIGH /
        # notebook GREEN), check-it (LOW / YELLOW), her job (FLAG / RED).
        confidence = result.get("confidence")
        if confidence in ("high", "green"):
            out.high += 1
        elif confidence in ("low", "yellow"):
            out.low += 1
        elif confidence in ("flag", "red"):
            out.flag += 1
        else:
            out.unknown += 1
    return out


def recent_outcomes(inbox: list[dict], *, window: int = RECENT_WINDOW) -> Outcomes:
    """The last `window` emails that WALKED THE TREE — not the last `window` inbox lines.

    The distinction matters: a burst of filed carrier noise would otherwise push every real customer
    email out of the window and leave `/healthz` judging Bean on a sample of zero.
    """
    return aggregate(_walked_only(inbox)[-window:])


@dataclass
class ApprovalRates:
    """What the operator DID with the drafts Bean showed them — the north-star instrument. `counts`
    is one tally per outcome (approved_untouched / approved_edited / rewritten / escalated); `rate`
    is the approved-untouched share, the fraction that saved them time. `mean_edit_ratio` is how
    much they changed the edited ones (a low mean = the yellows were nearly green; a high mean =
    they weren't)."""

    counts: dict[str, int] = field(default_factory=dict)
    total: int = 0  # drafts the operator acted on (non-draft gestures excluded)
    mean_edit_ratio: float | None = None

    @property
    def rate(self) -> float:
        """Approved-untouched share. Empty scores 0.0, never 1.0 — the same honesty as drafted_fraction:
        an agent nobody has approved is UNMEASURED, not perfect."""
        return self.counts.get("approved_untouched", 0) / self.total if self.total else 0.0


def approval_rates(corrections: list[Correction]) -> ApprovalRates:
    """Tally the draft outcomes over `corrections`. Non-draft gestures (snooze, mis-file) are skipped
    by `outcome_of`, so the denominator is only drafts the operator actually acted on."""
    counts: dict[str, int] = {}
    ratios: list[float] = []
    for c in corrections:
        label = outcome_of(c)
        if label is None:
            continue
        counts[label] = counts.get(label, 0) + 1
        if label == "approved_edited" and c.edit_ratio is not None:
            ratios.append(c.edit_ratio)
    total = sum(counts.values())
    mean = round(sum(ratios) / len(ratios), 4) if ratios else None
    return ApprovalRates(counts=counts, total=total, mean_edit_ratio=mean)


def drafting_stalled(outcomes: Outcomes) -> bool:
    """Is mail arriving and Bean producing NOTHING? The alarm that would have caught all of this.

    Deliberately not "drafted_fraction is low" — a low fraction is a judgment call and judgment
    calls get argued with. Zero drafts across a window of real customer mail is not a judgment call.
    """
    return outcomes.walked >= MIN_STALL_SAMPLE and outcomes.drafted == 0


def report(customer: str | None = None, *, inbox: Path | None = None) -> Outcomes:
    """The lifetime report for one customer (or an explicit path, for tests)."""
    return aggregate(load_inbox(inbox or inbox_path(customer)))


# ---- table rendering -------------------------------------------------------------------------

_VERDICT_COLUMNS = ("VERDICT", "EMAILS", "SHARE")


def _table(columns: tuple[str, ...], body: list[tuple[str, ...]], total: tuple[str, ...] | None = None) -> list[str]:
    rows = body + ([total] if total else [])
    widths = [max(len(columns[i]), *(len(r[i]) for r in rows)) for i in range(len(columns))]
    lines = ["  " + "  ".join(c.ljust(w) for c, w in zip(columns, widths)),
             "  " + "  ".join("-" * w for w in widths)]
    for cells in body:
        lines.append("  " + "  ".join(c.ljust(w) for c, w in zip(cells, widths)))
    if total:
        lines.append("  " + "  ".join("-" * w for w in widths))
        lines.append("  " + "  ".join(c.ljust(w) for c, w in zip(total, widths)))
    return lines


def render_table(o: Outcomes) -> str:
    if not o.walked:
        return f"  (no customer mail has reached the engine — {o.filed} filed)"
    verdicts = [("high (one-tap)", str(o.high), f"{o.share(o.high):.0%}"),
                ("low (check it)", str(o.low), f"{o.share(o.low):.0%}"),
                ("flag (her job)", str(o.flag), f"{o.share(o.flag):.0%}")]
    if o.unknown:
        verdicts.append(("unknown (!)", str(o.unknown), f"{o.share(o.unknown):.0%}"))
    lines = _table(_VERDICT_COLUMNS, verdicts,
                   total=("DRAFTED", str(o.drafted), f"{o.drafted_fraction:.0%}"))
    lines.append("")
    lines.append(f"  {o.filed} filed by the gate (never reached the engine, not counted above)")
    if drafting_stalled(o):
        lines.append(f"  DRAFTING STALLED: {o.walked} customer emails reached the engine and Bean drafted NONE.")
    return "\n".join(lines)


def main(argv=None):
    ap = argparse.ArgumentParser(description="What Bean actually did with real customer mail.")
    ap.add_argument("--customer", help="which customer's inbox.jsonl to read (default: BEAN_CUSTOMER env)")
    args = ap.parse_args(argv)
    print(render_table(report(customer=args.customer)))


if __name__ == "__main__":
    main()
