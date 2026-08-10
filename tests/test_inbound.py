"""Offline tests for the inbound mail receiver (bean/inbound.py + POST /api/inbound + /api/inbox).

Everything here is offline: the Postmark→Email mapping is pure, and the webhook pipeline runs the
gate + triage through *injected* seams (same FakeModel-free stubbing test_server.py uses for
/api/preview) so no live model is ever called. BEAN_DATA_DIR points at tmp_path, so the order index
and inbox JSONL never touch real data.
"""

from __future__ import annotations

import json
import threading
import urllib.error
import urllib.request
from http.server import ThreadingHTTPServer
from pathlib import Path

import pytest

from bean import server as srv
from bean.contract import Email
from bean.gate import GateResult
from bean.inbound import email_from_postmark, reply_target
from bean.inbox import load_inbox
from bean.order_index import load_orders
from bean.paths import inbox_path, order_index_path

_FIXTURES = Path(__file__).parent / "fixtures"


def _load_fixture(name: str) -> dict:
    return json.loads((_FIXTURES / name).read_text())


# ---- bean/inbound.email_from_postmark (pure mapping) -----------------------------------------

def test_email_from_postmark_maps_customer_payload():
    payload = _load_fixture("postmark_inbound_customer.json")
    email = email_from_postmark(payload)
    assert email.sender_email == "jordan.ramirez@example.com"  # FromFull.Email
    assert email.sender_name == "Jordan Ramirez"
    assert email.subject == "Where is my order??"
    assert email.id == "9f5a1c30-2b3e-4e1a-9c77-1a2b3c4d5e6f"  # MessageID
    assert "Aspen extension dining table" in email.body  # TextBody, not the HTML
    # ReplyTo empty → reply target falls back to From (the customer, not the forwarder).
    assert reply_target(payload) == "jordan.ramirez@example.com"


def test_email_from_postmark_preserves_thread_and_latest_body():
    # A reply inside a thread: body is the latest message only, and the
    # quoted history is preserved on `thread` so the draft + confidence call have context.
    payload = _load_fixture("postmark_inbound_threaded.json")
    email = email_from_postmark(payload)
    assert "drawer runner" in email.body and "repair the one I have" in email.body
    assert "On Sun" not in email.body and "wrote:" not in email.body  # quoted history not in body
    assert email.thread  # the earlier message is carried as context
    assert "wrote:" in email.thread[0]
    assert "checking in on your recent" in email.thread[0]


def test_body_falls_back_to_stripped_html_when_textbody_empty():
    payload = {
        "MessageID": "html-only", "From": "a@b.com", "FromFull": {"Email": "a@b.com", "Name": "A"},
        "Subject": "s", "ReplyTo": "", "TextBody": "", "StrippedTextReply": "",
        "HtmlBody": "<html><body><p>Hello&nbsp;there</p><p>Second&amp;line</p></body></html>",
    }
    email = email_from_postmark(payload)
    assert "Hello there" in email.body
    assert "Second&line" in email.body  # entity unescaped
    assert "<" not in email.body  # tags stripped


def test_reply_target_prefers_replyto_when_set():
    payload = {"From": "forwarder@sableandwren.example", "ReplyTo": "customer@gmail.com"}
    assert reply_target(payload) == "customer@gmail.com"


# ---- a live server on an ephemeral port, pointed at tmp data ---------------------------------

def _fake_gate(email, rules=None):
    """Deterministic offline gate: store notification mail (from the platform, or a shipment
    subject) files as 'notification'; everything else needs a reply."""
    hay = f"{email.sender_email} {email.subject}".lower()
    if "shopify.com" in email.sender_email.lower() or "shipment" in hay or "on the way" in hay:
        return GateResult("file", "notification", "A shipping notification.")
    return GateResult("reply", "customer", "A customer asking about their order.")


def _resolving_gate(email, rules=None):
    """A gate that resolves a website contact-form relay to the real customer in the body — the
    dynamic-envelope path. (The default _fake_gate would FILE mailer@shopify.com as a notification;
    this test overrides it to exercise the reply+resolution wiring.)"""
    return GateResult(
        "reply", "customer", "A customer sent a message through the website contact form.",
        customer_name="Dana", customer_email="dana.whitlock@example.com",
        reply_target="dana.whitlock@example.com",
        customer_message="Hi, please call me. I have a question about the dining chairs I "
                         "purchased. Thank you",
    )


def _filed_gate(email, rules=None):
    """A gate that FILES everything as a non-receipt newsletter — exercises the filed-persist path
    (a live gate false-negative: a real customer this gate wrongly sets aside must still be stored)."""
    return GateResult("file", "newsletter", "A marketing newsletter — no question in it.")


def _notification_gate(email, rules=None):
    """A gate that calls everything a notification — the live verdict on mail that carries no order
    (a forwarding/domain-auth confirmation). Pairs with a payload the notification parser rejects."""
    return GateResult("file", "notification", "An automated notification — nothing to reply to.")


_CANNED_DRAFT = {"bucket": "Order Status", "draft": "Hi Jordan, it shipped via USPS.",
                 "citations": ["notebook:Order Status"], "confidence": "green", "why_unsure": []}


@pytest.fixture
def inbound_server(tmp_path, monkeypatch):
    """Threaded server with the gate + the drafting model stubbed, persistence under tmp_path.

    The notebook itself is real (parsed off disk) — only the network call is faked, so the
    assemble → coerce → store path these tests exercise is the one production runs."""
    from bean.llm import FakeModel
    from bean.notebook import Bucket, Notebook

    monkeypatch.setenv("BEAN_DATA_DIR", str(tmp_path))
    monkeypatch.delenv("BEAN_PASSCODE", raising=False)  # passcode gate off (the operator is 'authed')
    monkeypatch.setattr(srv, "CONFIG_PATH", tmp_path / "bean-config.json")  # absent → fixtures gate
    monkeypatch.setattr(srv, "gate", _fake_gate)

    nb_path = tmp_path / "notebook.md"
    Notebook("A store", buckets=[Bucket("Order Status", "cite the order and stop")]).save(nb_path)
    monkeypatch.setattr(srv, "NOTEBOOK_PATH", nb_path)
    monkeypatch.setattr(srv, "ModelAdapter", lambda *a, **k: FakeModel({"draft": _CANNED_DRAFT}))

    httpd = ThreadingHTTPServer(("127.0.0.1", 0), srv.BeanHandler)
    t = threading.Thread(target=httpd.serve_forever, daemon=True)
    t.start()
    try:
        yield f"http://127.0.0.1:{httpd.server_address[1]}"
    finally:
        httpd.shutdown()
        httpd.server_close()


def _req(url, method="GET", body=None):
    data = json.dumps(body).encode() if body is not None else None
    headers = {"Content-Type": "application/json"} if data is not None else {}
    req = urllib.request.Request(url, data=data, method=method, headers=headers)
    try:
        with urllib.request.urlopen(req) as resp:
            return resp.status, json.loads(resp.read().decode() or "null")
    except urllib.error.HTTPError as e:
        return e.code, json.loads(e.read().decode() or "null")


# ---- POST /api/inbound: a customer email is triaged and lands in the inbox --------------------

def test_inbound_customer_triages_and_persists_to_inbox(inbound_server, monkeypatch):
    monkeypatch.setenv("BEAN_INBOUND_TOKEN", "sekret")
    url = inbound_server
    payload = _load_fixture("postmark_inbound_customer.json")

    status, body = _req(f"{url}/api/inbound?token=sekret", method="POST", body=payload)
    assert status == 200
    assert body["ok"] is True and body["disposition"] == "reply"
    assert body["email_id"] == payload["MessageID"]

    items = load_inbox(inbox_path())
    assert len(items) == 1
    item = items[0]
    assert item["sender_email"] == "jordan.ramirez@example.com"
    assert item["reply_to"] == "jordan.ramirez@example.com"
    assert item["received_at"] == payload["Date"]  # from the payload, deterministic
    # the stored result is the engine's output, so the UI renders without re-running the model
    assert item["result"]["bucket"] == "Order Status"
    assert item["result"]["confidence"] == "green"
    assert item["result"]["draft"] == "Hi Jordan, it shipped via USPS."


# ---- POST /api/inbound: the stored result is a DraftResult ---------------------------------------

def test_inbound_stores_a_draft_result(inbound_server, monkeypatch):
    """The webhook persists a DraftResult — bucket + groundedness + citations, and no `chunks`.
    That last assertion is the M1 cutover's tombstone: `chunks` was the routing tree's per-chunk
    breakdown, and nothing may reintroduce it into the stored shape the UI reads."""
    monkeypatch.setenv("BEAN_INBOUND_TOKEN", "sekret")
    url = inbound_server
    payload = _load_fixture("postmark_inbound_customer.json")
    status, body = _req(f"{url}/api/inbound?token=sekret", method="POST", body=payload)
    assert status == 200 and body["disposition"] == "reply"

    item = load_inbox(inbox_path())[0]
    result = item["result"]
    assert result["bucket"] == "Order Status"
    assert result["confidence"] == "green"  # the groundedness axis
    assert result["citations"] == ["notebook:Order Status"]
    assert "chunks" not in result


# ---- POST /api/inbound: a Shopify contact-form resolves to the real customer, not the relay ----

def test_inbound_contact_form_resolves_real_customer(inbound_server, monkeypatch):
    monkeypatch.setattr(srv, "gate", _resolving_gate)
    monkeypatch.delenv("BEAN_INBOUND_TOKEN", raising=False)
    url = inbound_server
    payload = _load_fixture("postmark_inbound_contact_form.json")  # From: mailer@shopify.com

    status, body = _req(f"{url}/api/inbound", method="POST", body=payload)
    assert status == 200 and body["disposition"] == "reply"

    items = load_inbox(inbox_path())
    assert len(items) == 1
    item = items[0]
    # the real customer from the body — NOT the Shopify relay in the From header
    assert item["sender_email"] == "dana.whitlock@example.com"
    assert item["sender_name"] == "Dana"
    assert item["reply_to"] == "dana.whitlock@example.com"  # would have been mailer@shopify.com
    # triage saw the customer's words, not the contact-form template boilerplate
    assert item["body"].startswith("Hi, please call me.")
    assert "contact form" not in item["body"]


# ---- POST /api/inbound: a store notification feeds the order index, not the inbox -------------

def test_inbound_notification_feeds_order_index_not_inbox(inbound_server, monkeypatch):
    monkeypatch.setenv("BEAN_INBOUND_TOKEN", "sekret")
    url = inbound_server
    # Read whole from the fixture, order number included. This used to restate the order-bearing
    # fields inline, because the fixture was a gitignored CAPTURE — "a file that does not exist for
    # anyone else," and one that might carry a real store's mail. Both halves of that were true and
    # the workaround was the wrong fix: it made the assertions portable while leaving the real mail
    # sitting in the repo, unreadable by an identity gate that only scans tracked files. The
    # fixtures are authored and tracked now, so the indirection has nothing left to protect.
    payload = _load_fixture("postmark_inbound_notification.json")

    status, body = _req(f"{url}/api/inbound?token=sekret", method="POST", body=payload)
    assert status == 200
    assert body["disposition"] == "filed" and body["kind"] == "notification"

    # the order index went live from the store's own shipping mail...
    orders = load_orders(order_index_path())
    assert len(orders) == 1
    assert orders[0].order_no == "SW-10482"
    assert orders[0].status == "shipped"
    assert orders[0].customer_email == "jordan.ramirez@example.com"
    # ...and it is NOT in the triage inbox (the operator shouldn't see notification mail)
    assert load_inbox(inbox_path()) == []


# ---- POST /api/inbound: a notification the parser can't read is filed, never dropped ----------

def test_inbound_unparseable_notification_persists_to_inbox(inbound_server, monkeypatch):
    # Regression: the operator's own mailbox-forwarding verification email was gated "notification",
    # found no order number, and was dropped on the floor with a 200 — the confirmation link they
    # needed to finish onboarding vanished. A notification the parser rejects must land in the Filed
    # lane.
    monkeypatch.setattr(srv, "gate", _notification_gate)
    monkeypatch.setenv("BEAN_INBOUND_TOKEN", "sekret")
    url = inbound_server
    payload = _load_fixture("postmark_inbound_customer.json")  # a real body, but no order number
    payload["Subject"] = "Confirm your forwarding address"
    payload["TextBody"] = "Click to verify forwarding to hello@sableandwren.example: https://x/verify?c=9"

    status, body = _req(f"{url}/api/inbound?token=sekret", method="POST", body=payload)
    assert status == 200
    assert body["disposition"] == "filed" and body["kind"] == "notification"

    # it is recoverable: persisted, body intact (the verification link survives), verdict attached
    items = load_inbox(inbox_path())
    assert len(items) == 1
    assert items[0]["subject"] == "Confirm your forwarding address"
    assert "https://x/verify?c=9" in items[0]["body"]
    assert items[0]["result"]["disposition"] == "filed"
    assert items[0]["result"]["kind"] == "notification"

    # and it did NOT contaminate the order index — there was no order in it to record
    assert load_orders(order_index_path()) == []


# ---- POST /api/inbound: a filed non-receipt is persisted, never silently dropped --------------

def test_inbound_filed_nonreceipt_persists_to_inbox(inbound_server, monkeypatch):
    # The gate files this as a newsletter. It must NOT vanish: a live false-negative (a real
    # customer the gate wrongly filed) would be invisible and unrecoverable if dropped.
    monkeypatch.setattr(srv, "gate", _filed_gate)
    monkeypatch.setenv("BEAN_INBOUND_TOKEN", "sekret")
    url = inbound_server
    payload = _load_fixture("postmark_inbound_customer.json")

    status, body = _req(f"{url}/api/inbound?token=sekret", method="POST", body=payload)
    assert status == 200
    assert body["disposition"] == "filed" and body["kind"] == "newsletter"

    # (i) it was persisted, and (iv) the customer's content is intact — not silently gone
    items = load_inbox(inbox_path())
    assert len(items) == 1
    item = items[0]
    assert item["id"] == payload["MessageID"]
    assert item["sender_email"] == "jordan.ramirez@example.com"
    assert item["subject"] == "Where is my order??"
    assert "Aspen extension dining table" in item["body"]
    # the gate's verdict rides in `result` — the exact {disposition,kind,reason} the UI reads
    assert item["result"]["disposition"] == "filed"
    assert item["result"]["kind"] == "newsletter"
    assert item["result"]["reason"]  # a non-empty operator-facing sentence

    # notification path untouched: nothing leaked into the order index from a filed newsletter
    assert load_orders(order_index_path()) == []


def test_get_inbox_exposes_filed_verdict_for_recovery(inbound_server, monkeypatch):
    # (ii) GET /api/inbox surfaces the filed item with the verdict the client's applyResult turns
    # into filed:true/gateKind/gateReason, and (iii) that kind+reason is what the "needs a reply"
    # recovery uses to re-route and log a mis-file — so recovery CAN act on live mail, no drop.
    monkeypatch.setattr(srv, "gate", _filed_gate)
    monkeypatch.delenv("BEAN_INBOUND_TOKEN", raising=False)
    url = inbound_server
    payload = _load_fixture("postmark_inbound_customer.json")
    _req(f"{url}/api/inbound", method="POST", body=payload)  # gate files it

    status, body = _req(f"{url}/api/inbox")
    assert status == 200
    assert len(body["emails"]) == 1
    result = body["emails"][0]["result"]
    assert result["disposition"] == "filed"  # → applyResult sets filed:true
    assert result["kind"] == "newsletter"  # → gateKind, drives the mis-file note
    assert result["reason"]  # → gateReason

    # recovery is reachable: re-previewing the same email past the gate yields a real draft, so a
    # mis-filed customer is fully recoverable (the JS "needs a reply" path drives this same endpoint).
    email = body["emails"][0]
    status, recovered = _req(f"{url}/api/preview", method="POST", body={
        "email": {"id": email["id"], "sender_name": email["sender_name"],
                  "sender_email": email["sender_email"], "subject": email["subject"],
                  "body": email["body"]},
        "skipGate": True,
    })
    assert status == 200 and recovered["disposition"] == "reply"
    assert recovered["bucket"] == "Order Status"  # the engine drafted it — no longer filed away


# ---- token enforcement -----------------------------------------------------------------------

def test_inbound_rejects_missing_or_wrong_token(inbound_server, monkeypatch):
    monkeypatch.setenv("BEAN_INBOUND_TOKEN", "sekret")
    url = inbound_server
    payload = _load_fixture("postmark_inbound_customer.json")

    status, _ = _req(f"{url}/api/inbound", method="POST", body=payload)  # no token
    assert status == 401
    status, _ = _req(f"{url}/api/inbound?token=wrong", method="POST", body=payload)
    assert status == 401
    assert load_inbox(inbox_path()) == []  # nothing persisted on a rejected webhook


def test_inbound_allows_when_token_unset(inbound_server, monkeypatch):
    monkeypatch.delenv("BEAN_INBOUND_TOKEN", raising=False)  # disabled → open (local/offline)
    url = inbound_server
    payload = _load_fixture("postmark_inbound_customer.json")
    status, body = _req(f"{url}/api/inbound", method="POST", body=payload)
    assert status == 200 and body["disposition"] == "reply"


def test_inbound_malformed_json_is_400(inbound_server, monkeypatch):
    monkeypatch.delenv("BEAN_INBOUND_TOKEN", raising=False)
    url = inbound_server
    req = urllib.request.Request(
        f"{url}/api/inbound", data=b"not json", method="POST",
        headers={"Content-Type": "application/json"},
    )
    try:
        with urllib.request.urlopen(req) as resp:
            status = resp.status
    except urllib.error.HTTPError as e:
        status = e.code
    assert status == 400


# ---- GET /api/inbox: the UI reads the persisted, triaged inbox --------------------------------

def test_get_inbox_returns_persisted_items(inbound_server, monkeypatch):
    monkeypatch.delenv("BEAN_INBOUND_TOKEN", raising=False)
    url = inbound_server
    payload = _load_fixture("postmark_inbound_customer.json")
    _req(f"{url}/api/inbound", method="POST", body=payload)  # deliver one

    status, body = _req(f"{url}/api/inbox")
    assert status == 200
    assert len(body["emails"]) == 1
    assert body["emails"][0]["id"] == payload["MessageID"]
    assert body["emails"][0]["result"]["bucket"] == "Order Status"


# ---- the access log must never write the inbound token into the log store --------------------

def test_redact_query_strips_the_inbound_token():
    line = 'POST /api/inbound?token=DjAd90ZQVaQoxCNHanjyjOg7c HTTP/1.1'
    out = srv._redact_query(line)
    assert "DjAd90ZQVaQoxCNHanjyjOg7c" not in out
    assert out == 'POST /api/inbound?<redacted> HTTP/1.1'
    # a path with no query is untouched, and the method/proto stay readable for debugging
    assert srv._redact_query('GET /api/inbox HTTP/1.1') == 'GET /api/inbox HTTP/1.1'
