"""The email-derived order index — order facts Bean learns from the store's own notification mail.

Every store emails an order-confirmation and a shipping-confirmation for each order, and those
carry exactly the WISMO facts (order number, items, customer email, ship date, tracking). Bean
parses that mail (bean/notification_parser.py) into OrderRecords and indexes them here — so an
order-status reply can be GROUNDED without a Shopify Admin API token or any admin action from the
merchant. Zero credentials, provider-agnostic by construction: any store that emails its customers
works, not just Shopify. The Admin API / OAuth becomes a scale-phase optimization, not the floor.

Persistence mirrors the correction log exactly (bean/corrections.py): one JSON line per record on
the BEAN_DATA_DIR volume. The JSONL→SQLite migration trigger documented in bean/paths.py covers
this file too — it's a format upgrade when the log gets large, not a re-architecture.
"""

from __future__ import annotations

import json
import re
from dataclasses import asdict, dataclass, field
from functools import lru_cache
from pathlib import Path

from bean.paths import order_index_path

# The per-customer index, on the volume in prod. Callers may pass an explicit log_path (tests do);
# this is only the default destination.
DEFAULT_LOG = order_index_path()


# ---- recognizing an order number -------------------------------------------------------------
#
# ONE definition, shared by the two halves that have to agree: the INDEX half
# (bean/notification_parser.py, reading the store's own notification mail) and the LOOKUP half
# (bean/sources.py, reading what a customer typed). Each used to hold its own copy of this regex
# under a comment promising to keep them in sync — a promise, not a mechanism. If they ever drifted,
# a customer asking about "#SW45336" would key a different order than the store's notification mail
# indexed, WISMO would ground on nothing, and Bean would ask for a number it had already been given.
# Both modules import from here now, so the sync is structural.
#
# An ALLOWLIST of store prefixes, never a broad `[A-Z]{2,}-\d+`, because the loose form reads
# "net-30" and "Gen-4" as order numbers.
#
# The HYPHEN IS OPTIONAL and a leading "#" is allowed, because real Shopify order numbers arrive as
# `#SW45336`, not `SW-45336` — a hyphen-REQUIRED pattern matched not one of a live store's 73 real
# emails, so every WISMO ran ungrounded while the parser looked like it worked. The "#" is stripped
# from the captured number and the result upper-cased, so `#sw45336` and `SW45336` are one key.
#
# The prefix list is PER-STORE, therefore CONFIGURATION and not a constant. The shipped default
# covers only the fictional fixture stores (MM = Maple & Moss, SW = Sable & Wren); a real tenant
# carries its own in `settings.orderPrefixes`. Hardcoding a live store's prefix here is how one
# tenant's grounding silently breaks the day somebody edits the constant for a different tenant.
DEFAULT_ORDER_PREFIXES = ("MM", "SW")

_PREFIX_SPLIT = re.compile(r"[,\s]+")


@lru_cache(maxsize=16)
def order_pattern(prefixes: tuple[str, ...] = DEFAULT_ORDER_PREFIXES) -> re.Pattern:
    """The order-number regex for one store's prefix allowlist.

    Cached because both callers rebuild it per email and the input is a tiny closed set. An empty
    allowlist compiles to a pattern that matches nothing rather than to `(?:)`, which would match
    a bare number anywhere and turn every "3-5 business days" into an order.
    """
    alternation = "|".join(re.escape(p) for p in prefixes) if prefixes else "(?!)"
    return re.compile(rf"#?\b(?:{alternation})-?\d+\b", re.IGNORECASE)


def order_prefixes(config=None) -> tuple[str, ...]:
    """A store's order prefixes from its Config, falling back to the shipped default.

    Read off `settings.orderPrefixes`. The on-disk settings map is string-valued everywhere else
    (and the admin UI edits it as such), so the documented form is comma- or space-separated —
    `"orderPrefixes": "TW"`. A JSON list is accepted too, because a hand-edited config plausibly
    holds one and silently ignoring it would break grounding with no error anywhere.

    Anything empty or unset means "use the default": a tenant who has never touched the setting
    must not end up with an allowlist that matches nothing.
    """
    raw = getattr(config, "settings", {}).get("orderPrefixes") if config is not None else None
    if isinstance(raw, str):
        raw = _PREFIX_SPLIT.split(raw.strip())
    if not raw:
        return DEFAULT_ORDER_PREFIXES
    found = tuple(p.strip().upper() for p in raw if isinstance(p, str) and p.strip())
    return found or DEFAULT_ORDER_PREFIXES


@dataclass
class OrderRecord:
    """One order's facts, accreted from its notification emails. Fields are optional because a
    confirmation and a later shipping email each supply a different slice — `OrderIndex` merges
    the two records for the same `order_no` into one full picture."""

    order_no: str
    customer_email: str = ""
    customer_name: str = ""
    status: str = ""  # confirmed | shipped | delivered | ...
    items: list[str] = field(default_factory=list)
    carrier: str = ""
    tracking: str = ""
    tracking_url: str = ""
    order_date: str = ""  # ISO date from the order confirmation
    ship_date: str = ""  # ISO date from the shipping confirmation
    est_delivery: str = ""
    total: str = ""
    source_email_id: str = ""  # the notification email this came from (provenance)

    @property
    def recency_key(self) -> str:
        """What to sort a customer's orders by — the shipping date if known, else the order date.
        ISO strings sort chronologically as plain strings."""
        return self.ship_date or self.order_date or ""

    def facts_block(self) -> str:
        """The human-readable order facts injected into the draft prompt. This is the string half
        of the OrderLookup contract — same shape the loose-dict fixture path renders."""
        lines = [f"Order {self.order_no}:"]
        for label, value in (
            ("status", self.status),
            ("items", ", ".join(self.items)),
            ("carrier", self.carrier),
            ("tracking", self.tracking),
            ("ordered", self.order_date),
            ("shipped", self.ship_date),
            ("est. delivery", self.est_delivery),
        ):
            if value:
                lines.append(f"  - {label}: {value}")
        return "\n".join(lines)


def record_order(rec: OrderRecord, *, log_path: Path = DEFAULT_LOG) -> OrderRecord:
    """Append one OrderRecord as a JSON line (mirrors corrections.record)."""
    log_path.parent.mkdir(parents=True, exist_ok=True)
    with log_path.open("a", encoding="utf-8") as fh:
        fh.write(json.dumps(asdict(rec), sort_keys=True) + "\n")
    return rec


def load_orders(log_path: Path = DEFAULT_LOG) -> list[OrderRecord]:
    if not log_path.exists():
        return []
    out = []
    for line in log_path.read_text(encoding="utf-8").splitlines():
        if line.strip():
            out.append(OrderRecord(**json.loads(line)))
    return out


def _merge(base: OrderRecord, update: OrderRecord) -> OrderRecord:
    """Fold a later record for the same order into an earlier one: any non-empty field on the
    update wins. This is how a shipping confirmation adds tracking + ship_date onto the record the
    order confirmation created, without either one clobbering the other's fields."""
    merged = asdict(base)
    for key, value in asdict(update).items():
        if value:  # non-empty scalar or non-empty list
            merged[key] = value
    return OrderRecord(**merged)


class OrderIndex:
    """Two lookups over the accrued records: by order number, and by customer email (most-recent
    order first). Records sharing an `order_no` are merged, so a confirmation + its shipping email
    collapse into one full record. Later log lines win on conflicting fields."""

    def __init__(self, records: list[OrderRecord] | None = None):
        by_order: dict[str, OrderRecord] = {}
        for rec in records or []:
            if not rec.order_no:
                continue
            key = rec.order_no.upper()
            by_order[key] = _merge(by_order[key], rec) if key in by_order else rec
        self.by_order_no = by_order

        by_email: dict[str, list[OrderRecord]] = {}
        for rec in by_order.values():
            if rec.customer_email:
                by_email.setdefault(rec.customer_email.lower(), []).append(rec)
        for recs in by_email.values():
            recs.sort(key=lambda r: r.recency_key, reverse=True)  # most recent first
        self.by_customer_email = by_email

    @classmethod
    def from_log(cls, log_path: Path = DEFAULT_LOG) -> "OrderIndex":
        return cls(load_orders(log_path))

    def for_order_no(self, order_no: str) -> OrderRecord | None:
        return self.by_order_no.get(order_no.upper())

    def for_customer(self, email: str) -> OrderRecord | None:
        """The customer's most recent order — the fallback when their email carries no order
        number (the common WISMO case: 'I ordered last week, where is it?')."""
        recs = self.by_customer_email.get(email.lower())
        return recs[0] if recs else None
