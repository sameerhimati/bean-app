"""Unit tests for the order-notification parser (offline).

The parser turns the store's own confirmation/shipping mail into OrderRecords, and — critically —
returns None on a customer's email so it's safe to run over inbound mail.
"""

from __future__ import annotations

from bean.contract import Email
from bean.fixtures import SW_GOLDEN, SW_NOTIFICATIONS, sw_config
from bean.notification_parser import parse_notification

_BY_ID = {e.id: e for e in SW_NOTIFICATIONS}
_GOLDEN_BY_ID = {c.email.id: c.email for c in SW_GOLDEN}
# The store's Config: it carries the order-number prefixes and the store's own mail domains, both
# of which are per-tenant. Passing it is what production does (bean/server.py hands the loaded
# config to the parser on the inbound path), so it is what these tests exercise.
CFG = sw_config()


def test_order_confirmation_parses_customer_and_items():
    rec = parse_notification(_BY_ID["sw-notif-21050-confirm"], config=CFG)
    assert rec is not None
    assert rec.order_no == "SW-21050"
    assert rec.customer_email == "angela.pruitt@gmail.com"
    assert rec.customer_name == "Angela Pruitt"
    assert rec.status == "confirmed"
    assert rec.items == ["Aspen Extension Dining Table"]
    assert rec.order_date == "2026-06-30"
    assert rec.est_delivery == "2026-07-08"
    assert rec.tracking == ""  # a confirmation has no tracking yet


def test_shipping_confirmation_parses_tracking_and_status():
    rec = parse_notification(_BY_ID["sw-notif-21050-ship"], config=CFG)
    assert rec is not None
    assert rec.order_no == "SW-21050"
    assert rec.status == "shipped"
    assert rec.carrier == "USPS"
    assert rec.tracking == "9400111899223344556677"  # the URL is stripped off the number
    assert rec.tracking_url.startswith("https://tools.usps.com/")
    assert rec.ship_date == "2026-07-01"
    assert rec.customer_email == "angela.pruitt@gmail.com"


def test_delivered_update_parses_status():
    rec = parse_notification(_BY_ID["sw-notif-20988-delivered"], config=CFG)
    assert rec is not None
    assert rec.status == "delivered"
    assert rec.carrier == "UPS"
    assert rec.tracking == "1Z999AA10123456784"
    assert rec.customer_email == "brian.cole@gmail.com"


def test_customer_wismo_is_not_a_notification():
    # A numberless WISMO mentions "shipping confirmation" (a signal word) but carries no order
    # number — must NOT be mistaken for a notification.
    assert parse_notification(_GOLDEN_BY_ID["sw-wismo"], config=CFG) is None


def test_customer_email_with_order_number_but_no_signal_is_none():
    # A cancel request quotes the order number but has no confirmation/shipping signal.
    assert parse_notification(_GOLDEN_BY_ID["sw-cancel"], config=CFG) is None


def test_plain_email_is_none():
    assert parse_notification(
        Email("x", "Sam", "sam@example.com", "hello", "just saying hi, no order here")
    ) is None


# ---- the two per-tenant knobs: order prefixes, and the store's own mail domains ---------------

def test_a_config_supplied_order_prefix_is_honored():
    """The prefix allowlist is CONFIGURATION, not a constant.

    A live store's order prefix used to be hardcoded in the recognizer, which meant preparing this
    repo for release — deleting that store's identity from the source — would have silently stopped
    its WISMO from grounding, with every test still green. So the tenant carries its own prefix and
    this asserts the seam works end to end: `QX` is not in the shipped default and must still match
    a store's hyphen-less Shopify format once its config says so.

    The prefix here is deliberately arbitrary. It used to be the real customer's, which proved
    nothing extra — any prefix outside the default exercises the same seam — and left a real order
    number in public source that every identity grep would flag forever.
    """
    from bean.config import Config

    cfg = Config(categories=(), knowledge_docs={}, settings={"orderPrefixes": "QX"})
    email = Email("n", "A Store", "orders@shopify.com", "Order #QX45336 confirmed",
                  "Customer: Dana <dana@example.com>\nYour order is confirmed.")

    rec = parse_notification(email, config=cfg)
    assert rec is not None, "a configured prefix did not match the store's own order format"
    assert rec.order_no == "QX45336"  # '#' stripped, upper-cased — the same key the lookup builds


def test_the_shipped_default_still_matches_without_any_config():
    # No config at all (fixtures, the demo tenant, a bare call) falls back to the shipped fictional
    # prefixes rather than to an allowlist that matches nothing.
    email = Email("n", "A Store", "orders@shopify.com", "Order #SW45336 confirmed",
                  "Customer: Dana <dana@example.com>\nYour order is confirmed.")
    rec = parse_notification(email)
    assert rec is not None and rec.order_no == "SW45336"


def test_a_prefix_outside_the_allowlist_is_not_an_order():
    # ...and the allowlist still refuses everything else, which is the whole reason it is a list
    # and not `[A-Z]{2,}-?\d+`.
    email = Email("n", "A Store", "orders@shopify.com", "Order #QX45336 confirmed",
                  "Customer: Dana <dana@example.com>\nYour order is confirmed.")
    assert parse_notification(email) is None


def test_the_stores_own_address_is_not_read_as_the_customer():
    """`settings.storeDomains` is the second per-tenant knob, and it is load-bearing.

    Platform mail quotes the merchant's own support address, often ABOVE the customer's. The parser
    keys the customer on the first address in the body, so a store whose domain it does not know
    indexes every order against its own support inbox — and then no customer ever matches on the
    numberless-WISMO path. The store domain used to be a hardcoded constant naming a real business.
    """
    confirm = _BY_ID["sw-notif-21050-confirm"]
    assert "hello@sableandwren.example" in confirm.body, "fixture must exercise the store-address case"

    assert parse_notification(confirm, config=CFG).customer_email == "angela.pruitt@gmail.com"
    # ...and without the setting, the support inbox is what gets indexed. This is not a bug being
    # asserted — it is why the setting exists, and it fails loudly if the wiring is ever dropped.
    assert parse_notification(confirm).customer_email == "hello@sableandwren.example"
