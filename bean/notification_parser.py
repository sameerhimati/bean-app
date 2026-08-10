"""Parse a store's order-notification email into an OrderRecord.

The source is the mail the store's own platform sends on every order: the order confirmation and
the shipping confirmation. Those land in the merchant's inbox (Shopify sends them to the store's
own notification address) and flow into Bean's inbound stream, so Bean can build its order index
from them with zero credentials and no admin action. See bean/order_index.py for why this is the
right architecture, not a workaround.

The parser is deliberately tolerant: it pulls whatever fields are present and returns None when the
email is not a recognizable order notification — so it is safe to run over inbound mail and only
bite on real notifications. Provider-agnostic by signature; Shopify's text format is the first (and
today only) one modeled. Calibrating against a live store's real notification mail — especially
confirming the customer email is present and parseable, and handling HTML bodies — is the documented
fast-follow.
"""

from __future__ import annotations

import re

from bean.contract import Email
from bean.order_index import OrderRecord, order_pattern, order_prefixes

_EMAIL_RE = re.compile(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}")

# Enough of a notification signal to distinguish store mail from a customer's own email. Only
# reached on the notification-ingestion path (gate-classified receipt/notification), never on a
# customer reply, so a light recognizer is safe.
_NOTIFICATION_SIGNAL = re.compile(
    r"confirm(?:ed|ation)|on the way|has shipped|shipment|out for delivery|"
    r"delivered|tracking|new order|order receipt|your order is",
    re.IGNORECASE,
)
_SHIPPED_SIGNAL = re.compile(r"on the way|has shipped|shipment|out for delivery|tracking", re.IGNORECASE)
_DELIVERED_SIGNAL = re.compile(r"been delivered|was delivered|delivered on", re.IGNORECASE)

# Domains that belong to the PLATFORM — never the customer we want to key on. The store's OWN
# domain has to be excluded too (platform mail quotes the merchant's support address, often ABOVE
# the customer's, and the first address wins — so a store whose domain we don't know ends up with
# every order indexed against its own support inbox and no customer ever matching), but a live
# store's domain is tenant data, not a constant: it comes from `settings.storeDomains`. Same
# reasoning as the order prefixes — see bean/order_index.order_pattern.
_PLATFORM_DOMAINS = ("shopify.com", "shopifyemail.com")
_DOMAIN_SPLIT = re.compile(r"[,\s]+")

_URL_RE = re.compile(r"https?://\S+")
# A line item: "1 × Aspen Extension Dining Table" / "2 x Wexford Bookshelf".
_ITEM_RE = re.compile(r"^\s*(\d+)\s*[×xX]\s*(.+?)\s*$")


def _labelled(label: str) -> re.Pattern:
    """A 'Label: value' extractor, anchored on a word boundary so 'total' doesn't match 'Subtotal'."""
    return re.compile(rf"\b{label}\s*[:\-]\s*(.+)", re.IGNORECASE)


_CUSTOMER_RE = _labelled("customer")
_CARRIER_RE = _labelled("carrier")
_TRACKING_RE = _labelled(r"tracking(?: number| #| no\.?)?")
_ORDER_DATE_RE = _labelled("order date")
_SHIP_DATE_RE = _labelled(r"(?:shipped on|ship date)")
_EST_RE = _labelled(r"(?:estimated delivery|expected delivery)")


def _store_domains(config=None) -> tuple[str, ...]:
    """Platform domains plus the tenant's own, from `settings.storeDomains` (comma-separated)."""
    raw = getattr(config, "settings", {}).get("storeDomains", "") if config is not None else ""
    own = tuple(d.strip().lower() for d in _DOMAIN_SPLIT.split(raw) if d.strip()) if raw else ()
    return _PLATFORM_DOMAINS + own


def parse_notification(email: Email, *, config=None) -> OrderRecord | None:
    """An OrderRecord if `email` is a recognizable order/shipping notification, else None.

    `config` is the tenant's Config; it supplies the order-number prefixes and the store's own mail
    domains. Omitted (tests, fixtures) the shipped fictional defaults apply.
    """
    store_domains = _store_domains(config)
    text = f"{email.subject}\n{email.body}"
    order_match = order_pattern(order_prefixes(config)).search(text)
    if not order_match or not _NOTIFICATION_SIGNAL.search(text):
        return None
    order_no = order_match.group(0).lstrip("#").upper()

    status = (
        "delivered" if _DELIVERED_SIGNAL.search(text)
        else "shipped" if _SHIPPED_SIGNAL.search(text)
        else "confirmed"
    )

    customer_email = ""
    for m in _EMAIL_RE.finditer(email.body):
        addr = m.group(0)
        if not any(addr.lower().endswith(dom) for dom in store_domains):
            customer_email = addr
            break

    items = []
    for line in email.body.splitlines():
        m = _ITEM_RE.match(line)
        if m:
            qty, name = m.group(1), m.group(2).strip()
            items.append(name if qty == "1" else f"{qty} × {name}")

    def grab(pattern: re.Pattern) -> str:
        m = pattern.search(email.body)
        return m.group(1).strip() if m else ""

    # The customer name is whatever precedes the address on a "Customer:" line.
    customer_name = _EMAIL_RE.sub("", grab(_CUSTOMER_RE)).strip(" <>()")

    tracking = _URL_RE.sub("", grab(_TRACKING_RE)).strip()
    tracking_url = ""
    if tracking:
        url_match = _URL_RE.search(email.body)
        if url_match:
            tracking_url = url_match.group(0)

    return OrderRecord(
        order_no=order_no,
        customer_email=customer_email,
        customer_name=customer_name,
        status=status,
        items=items,
        carrier=grab(_CARRIER_RE),
        tracking=tracking,
        tracking_url=tracking_url,
        order_date=grab(_ORDER_DATE_RE),
        ship_date=grab(_SHIP_DATE_RE),
        est_delivery=grab(_EST_RE),
        source_email_id=email.id,
    )
