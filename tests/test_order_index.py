"""Unit tests for the email-derived order index + its OrderLookup backing (offline)."""

from __future__ import annotations

from bean.contract import Email
from bean.fixtures import SW_NOTIFICATIONS, sw_config
from bean.notification_parser import parse_notification
from bean.order_index import OrderIndex, OrderRecord, load_orders, record_order
from bean.sources import OrderLookup


# The store's Config — it carries the order prefixes and the store's own mail domains, both
# per-tenant. Production hands it to both halves of the recognizer; so does this file.
CFG = sw_config()


def _wismo(sender_email: str, body: str = "Where is my order?") -> Email:
    return Email("q", "Cust", sender_email, "Order status?", body)


def test_jsonl_round_trip(tmp_path):
    log = tmp_path / "orders.jsonl"
    rec = OrderRecord("SW-1", customer_email="a@x.com", items=["Widget"], status="shipped")
    record_order(rec, log_path=log)
    loaded = load_orders(log)
    assert loaded == [rec]


def test_load_missing_file_is_empty(tmp_path):
    assert load_orders(tmp_path / "nope.jsonl") == []


def test_confirmation_and_shipping_merge_into_one_record():
    recs = [parse_notification(e, config=CFG) for e in SW_NOTIFICATIONS]
    index = OrderIndex([r for r in recs if r])
    merged = index.for_order_no("SW-21050")
    assert merged is not None
    # The confirmation supplied order_date + est_delivery; the shipping email supplied tracking +
    # ship_date + status. The merge holds both halves.
    assert merged.order_date == "2026-06-30"
    assert merged.est_delivery == "2026-07-08"
    assert merged.status == "shipped"
    assert merged.tracking == "9400111899223344556677"
    assert merged.ship_date == "2026-07-01"


def test_customer_lookup_returns_most_recent_order():
    index = OrderIndex([
        OrderRecord("SW-100", customer_email="jo@x.com", order_date="2026-05-01", status="delivered"),
        OrderRecord("SW-200", customer_email="jo@x.com", ship_date="2026-06-20", status="shipped"),
    ])
    latest = index.for_customer("jo@x.com")
    assert latest is not None
    assert latest.order_no == "SW-200"


def test_lookup_grounds_by_order_number():
    index = OrderIndex([r for e in SW_NOTIFICATIONS if (r := parse_notification(e, config=CFG))])
    lookup = OrderLookup(orders={}, index=index, config=CFG)
    facts = lookup.facts_for(_wismo("someone@x.com", "Update on #SW-21050 please?"))
    assert "SW-21050" in facts
    assert "9400111899223344556677" in facts  # real tracking, grounded


def test_lookup_grounds_numberless_wismo_by_customer_email():
    # The WISMO win: no order number in the email, grounded via the sender's address.
    index = OrderIndex([r for e in SW_NOTIFICATIONS if (r := parse_notification(e, config=CFG))])
    lookup = OrderLookup(orders={}, index=index, config=CFG)
    facts = lookup.facts_for(_wismo("angela.pruitt@gmail.com"))
    assert "SW-21050" in facts
    assert "shipped" in facts


def test_empty_index_grounds_nothing():
    lookup = OrderLookup(orders={}, index=OrderIndex([]), config=CFG)
    assert lookup.facts_for(_wismo("angela.pruitt@gmail.com", "Update on #SW-21050?")) == ""
