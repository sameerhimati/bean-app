"""Reads and prices the token/cost log `bean/llm.py` `_log_usage` writes (bean.paths.usage_path).

Nothing has ever read usage.jsonl. It has been accumulating model/tokens/cache/purpose since the
first real call with no price table and no report — so Bean's spend has never once been visible.
This module is that report: per (model, purpose) call counts, cost, average token sizes, and —
the actual question worth asking of a log that carries `cache_read`/`cache_write` at all — the
CACHE-HIT RATE, since a cache miss on a model whose minimum cacheable prefix isn't met (see
llm.py `_log_usage`) silently costs full price with no error.

    python -m bean.usage                 # the default customer's usage.jsonl
    python -m bean.usage --customer NAME # a specific tenant's log
"""

from __future__ import annotations

import argparse
import json
from dataclasses import dataclass
from pathlib import Path

from bean.paths import usage_path

# Verified prices, USD per million tokens (input, output). Anthropic bills cache read/write off the
# model's INPUT price with a multiplier (below), never a separate rate — so this table is the only
# source of truth a cache cost needs.
PRICES: dict[str, tuple[float, float]] = {
    "claude-haiku-4-5": (1.00, 5.00),
    "claude-sonnet-4-6": (3.00, 15.00),
    "claude-opus-4-8": (5.00, 25.00),
}

# Cache write is priced off input price at 1.25x (5-min TTL) or 2.0x (1-hour TTL). The log
# (bean/llm.py _log_usage) does not record which TTL a write used — Bean has only ever requested
# the default (5-min) `cache_control` block, so 1.25x is the correct assumption today, not a guess
# of convenience. If a 1-hour TTL is ever requested, this constant must become per-line data.
CACHE_WRITE_MULTIPLIER = 1.25
CACHE_READ_MULTIPLIER = 0.1
CACHE_READ_MULTIPLIER_OPUS = 0.5  # opus prices a cache read at 5x every other model's rate


def call_cost(model: str, *, input_tokens: int, output_tokens: int, cache_read: int, cache_write: int) -> float | None:
    """Cost of one call in USD, or None if `model` isn't in PRICES.

    None (not 0.0) on an unknown model is load-bearing: a model added to llm.py but not priced here
    must show up as a hole in the report, not a silent $0 that understates spend without a trace.
    """
    price = PRICES.get(model)
    if price is None:
        return None
    input_price, output_price = price
    cache_read_mult = CACHE_READ_MULTIPLIER_OPUS if model == "claude-opus-4-8" else CACHE_READ_MULTIPLIER
    return (
        input_tokens * input_price
        + output_tokens * output_price
        + cache_read * input_price * cache_read_mult
        + cache_write * input_price * CACHE_WRITE_MULTIPLIER
    ) / 1_000_000


@dataclass
class UsageRow:
    """One (model, purpose) bucket's accrued stats. `unpriced_calls` tracks calls whose model has
    no PRICES entry — `total_cost` only ever sums the calls that COULD be priced, so an unpriced
    model can never masquerade as a cheap (or free) one; see `call_cost`."""

    model: str
    purpose: str
    calls: int = 0
    total_cost: float = 0.0
    unpriced_calls: int = 0
    input_tokens: int = 0
    output_tokens: int = 0
    cache_read: int = 0
    cache_write: int = 0
    cache_hit_calls: int = 0  # calls with cache_read > 0

    @property
    def avg_input(self) -> float:
        return self.input_tokens / self.calls if self.calls else 0.0

    @property
    def avg_output(self) -> float:
        return self.output_tokens / self.calls if self.calls else 0.0

    @property
    def cache_hit_rate(self) -> float:
        return self.cache_hit_calls / self.calls if self.calls else 0.0


def _load_lines(path: Path) -> list[dict]:
    if not path.exists():
        return []
    out = []
    for line in path.read_text(encoding="utf-8").splitlines():
        if line.strip():
            out.append(json.loads(line))
    return out


def aggregate(lines: list[dict]) -> dict[tuple[str, str], UsageRow]:
    """Group raw log lines by (model, purpose). `.get(..., 0)` on every numeric field tolerates
    lines written before `cache_write` existed (real prod logs have them) — an old line just
    contributes 0 cache-write tokens, not a KeyError."""
    rows: dict[tuple[str, str], UsageRow] = {}
    for line in lines:
        model = line.get("model", "unknown")
        purpose = line.get("purpose", "unknown")
        key = (model, purpose)
        row = rows.setdefault(key, UsageRow(model=model, purpose=purpose))
        it = line.get("input_tokens", 0)
        ot = line.get("output_tokens", 0)
        cr = line.get("cache_read", 0)
        cw = line.get("cache_write", 0)
        row.calls += 1
        row.input_tokens += it
        row.output_tokens += ot
        row.cache_read += cr
        row.cache_write += cw
        if cr > 0:
            row.cache_hit_calls += 1
        cost = call_cost(model, input_tokens=it, output_tokens=ot, cache_read=cr, cache_write=cw)
        if cost is None:
            row.unpriced_calls += 1
        else:
            row.total_cost += cost
    return rows


def totalize(rows: dict[tuple[str, str], UsageRow]) -> UsageRow:
    """Fold every (model, purpose) bucket into one grand-total row."""
    total = UsageRow(model="TOTAL", purpose="")
    for row in rows.values():
        total.calls += row.calls
        total.total_cost += row.total_cost
        total.unpriced_calls += row.unpriced_calls
        total.input_tokens += row.input_tokens
        total.output_tokens += row.output_tokens
        total.cache_read += row.cache_read
        total.cache_write += row.cache_write
        total.cache_hit_calls += row.cache_hit_calls
    return total


def report(customer: str | None = None, path: Path | None = None) -> dict[tuple[str, str], UsageRow]:
    """The aggregated report for one customer's usage.jsonl (or an explicit `path`, for tests)."""
    return aggregate(_load_lines(path or usage_path(customer)))


# ---- table rendering -------------------------------------------------------------------------

_COLUMNS = ("MODEL", "PURPOSE", "CALLS", "COST", "AVG IN", "AVG OUT", "CACHE R", "CACHE W", "HIT%")


def _cost_str(row: UsageRow) -> str:
    if row.calls and row.unpriced_calls == row.calls:
        return f"UNPRICED ({row.calls})"
    if row.unpriced_calls:
        return f"${row.total_cost:.4f} (+{row.unpriced_calls} unpriced)"
    return f"${row.total_cost:.4f}"


def _row_cells(row: UsageRow) -> tuple[str, ...]:
    return (
        row.model,
        row.purpose or "—",
        str(row.calls),
        _cost_str(row),
        f"{row.avg_input:.0f}",
        f"{row.avg_output:.0f}",
        str(row.cache_read),
        str(row.cache_write),
        f"{row.cache_hit_rate:.0%}",
    )


def render_table(rows: dict[tuple[str, str], UsageRow]) -> str:
    if not rows:
        return "  (no usage logged)"
    body = [_row_cells(row) for _, row in sorted(rows.items())]
    body.append(_row_cells(totalize(rows)))
    widths = [max(len(_COLUMNS[i]), *(len(r[i]) for r in body)) for i in range(len(_COLUMNS))]
    lines = ["  " + "  ".join(c.ljust(w) for c, w in zip(_COLUMNS, widths))]
    lines.append("  " + "  ".join("-" * w for w in widths))
    for cells in body[:-1]:
        lines.append("  " + "  ".join(c.ljust(w) for c, w in zip(cells, widths)))
    lines.append("  " + "  ".join("-" * w for w in widths))
    lines.append("  " + "  ".join(c.ljust(w) for c, w in zip(body[-1], widths)))
    return "\n".join(lines)


def main(argv=None):
    ap = argparse.ArgumentParser(description="Summarize Bean's per-call token/cost usage log.")
    ap.add_argument("--customer", help="which customer's usage.jsonl to read (default: BEAN_CUSTOMER env)")
    args = ap.parse_args(argv)
    print(render_table(report(customer=args.customer)))


if __name__ == "__main__":
    main()
