"""Fixture-backed sources: the knowledge space, order lookup, and the email source seam.

These are the integration points that go real in Slice 2 (vector DB, Shopify Admin API,
Proton Bridge). For Slice 1 they're backed by the fixtures dicts, behind the same interfaces
so the swap is local.
"""

from __future__ import annotations

import re
from typing import Protocol

from bean.contract import Email
from bean.fixtures import GOLDEN, KNOWLEDGE_DOCS, ORDERS
from bean.order_index import OrderIndex, order_pattern, order_prefixes


class KnowledgeStore:
    """A simple doc store. `all()` feeds the cached system prefix (the whole store is small
    in Slice 1); `search()` is the seam that becomes embedding retrieval in Slice 2."""

    def __init__(self, docs: dict[str, str] | None = None):
        self._docs = dict(docs if docs is not None else KNOWLEDGE_DOCS)

    def all(self) -> dict[str, str]:
        return dict(sorted(self._docs.items()))

    def search(self, query: str, k: int = 4) -> list[tuple[str, str]]:
        terms = {t for t in re.findall(r"[a-z]+", query.lower()) if len(t) > 3}
        scored = [
            (sum(doc.lower().count(t) for t in terms), name, doc)
            for name, doc in self._docs.items()
        ]
        scored.sort(key=lambda s: s[0], reverse=True)
        return [(name, doc) for score, name, doc in scored[:k] if score > 0]


class OrderLookup:
    """Read-only order facts. Two backings, chosen by the caller:

    - `orders` (loose dict) — the Maple & Moss calibration fixture, keyed by order number with
      free-form fact dicts. The flat path's default; unchanged.
    - `index` (OrderIndex) — the email-derived index built from the store's own notification mail
      (bean/order_index.py). The live-mail path passes this; it also enables a customer-email
      fallback for the common WISMO case where the email carries no order number.

    `config` supplies the store's order-number prefixes (`settings.orderPrefixes`); omitted, the
    shipped fictional default applies. This is the LOOKUP half of the recognizer — see
    bean/order_index.order_pattern for why the two halves share one definition.

    `facts_for` prefers the index, then the loose dict; a matched order with no record on either is
    simply skipped (so an empty index renders '' exactly like having no order data at all)."""

    def __init__(self, orders: dict[str, dict] | None = None, *, index: OrderIndex | None = None,
                 config=None):
        self._orders = dict(orders if orders is not None else ORDERS)
        self._index = index
        self._order_re = order_pattern(order_prefixes(config))

    def order_numbers(self, email: Email) -> list[str]:
        found = self._order_re.findall(f"{email.subject} {email.body}")
        # Normalize case and dedupe, preserving order.
        seen, out = set(), []
        for raw in found:
            key = raw.lstrip("#").upper()  # "#SW45336" and "SW45336" are the same order
            if key not in seen:
                seen.add(key)
                out.append(key)
        return out

    def facts_for(self, email: Email) -> str:
        """Human-readable order facts for the matched order(s), or '' if none on file."""
        blocks = []
        for num in self.order_numbers(email):
            if self._index and (rec := self._index.for_order_no(num)):
                blocks.append(rec.facts_block())
                continue
            legacy = self._orders.get(num)
            if legacy:
                fields = "\n".join(f"  - {k}: {v}" for k, v in legacy.items())
                blocks.append(f"Order {num}:\n{fields}")
            # A matched-but-unknown order contributes nothing — absence reads as '(no order data)'.
        # Fallback: no order number resolved, so ground on the customer's most recent order. This
        # is the WISMO win — "I ordered last week, where is it?" carries no number to match.
        if not blocks and self._index and email.sender_email:
            if rec := self._index.for_customer(email.sender_email):
                blocks.append(rec.facts_block())
        return "\n".join(blocks)


class EmailSource(Protocol):
    def unread(self) -> list[Email]: ...


class FixtureEmailSource:
    """Stands in for ProtonBridge: serves the golden emails. Same interface."""

    def unread(self) -> list[Email]:
        return [case.email for case in GOLDEN]
