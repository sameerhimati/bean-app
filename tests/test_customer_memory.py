"""Bean's memory: prior contact from this sender, the quoted thread, and the customer's photos.

The bug this pins down is measurable and was live: 32% of the operator's real mail is a repeat
contact, one customer wrote five times in six days about one unresolved return, and Bean greeted
each of those as a stranger — while their routing tree carried an escalate trigger reading "angry or
repeat-contact customers" that the model had no way on earth to evaluate. Separately, the quoted
thread was extracted by bean/inbound.py, shown only to the GATE, and thrown away before the tree
walked — so the docstring promising it as "context for the draft and the confidence call" was false.

These tests drive the REAL entrypoints: the inbound webhook → bean/loop.triage_tree (which does the
disk read) → the tree walk, with only the MODEL faked. A test that patched the seam it means to
exercise would prove nothing (see project memory: "verify the path the server calls"). The load-
bearing assertion is therefore on FakeModel.calls[...]["user"] — the bytes actually put in front of
the model — and its mirror image, that none of this ever reaches the CACHED SYSTEM PREFIX.

All senders/emails here are invented. Nothing in this file is a real customer.
"""

from __future__ import annotations

import base64
import json
import threading
import urllib.request
from dataclasses import asdict
from http.server import ThreadingHTTPServer
from pathlib import Path

import pytest

from bean import server as srv
from bean.contract import Email
from bean.customer_history import history_block, prior_contact, render_prior_contact
from bean.paths import default_customer
from bean.fixtures import sw_config
from bean.inbox import InboxItem, append_inbox, load_inbox
from bean.llm import FakeModel
from bean.notebook import Bucket, Notebook
from bean.notification_parser import parse_notification
from bean.paths import inbox_path, status_path
from bean.sources import OrderLookup

CFG = sw_config()


def _item(email_id, sender, subject, *, received_at, result=None, thread=None):
    return InboxItem(
        id=email_id, sender_name="Dana Whitlock", sender_email=sender, reply_to=sender,
        subject=subject, body="(body)", received_at=received_at,
        result=result if result is not None else {"category": "Orders, Returns & Refunds", "confidence": "flag"},
        thread=thread or [],
    )


def _row(*args, **kwargs) -> dict:
    """An inbox LINE — the raw dict `load_inbox` hands back, which is what the reader consumes."""
    return asdict(_item(*args, **kwargs))


# ---- 1. the reader: "did anyone ever answer them" is the signal, not "have we met" -----------

def test_unanswered_prior_contact_is_rendered_as_unanswered():
    inbox = [
        _row("e1", "dana@example.com", "Return Label", received_at="Thu, 9 Jul 2026 10:00:00 -0400"),
        _row("e2", "dana@example.com", "Re: Return Label", received_at="Fri, 10 Jul 2026 09:00:00 -0400"),
    ]
    entries = prior_contact("dana@example.com", inbox, status={})  # nothing actioned
    assert [e["subject"] for e in entries] == ["Return Label", "Re: Return Label"]
    assert all(not e["resolved"] for e in entries)

    block = render_prior_contact(entries)
    assert "written in 2 times before" in block
    assert "2 of those got no reply" in block
    assert block.count("NOBODY HAS REPLIED") == 2
    assert "losing patience" in block  # the routing/tone instruction that makes the trigger fulfillable


def test_resolved_prior_contact_reads_differently_from_an_ignored_one():
    """'We handled this last week' and 'she has asked three times and nobody replied' are different
    emails. If the block can't tell them apart it is decoration, not memory."""
    inbox = [_row("e1", "dana@example.com", "Return Label", received_at="Thu, 9 Jul 2026 10:00:00 -0400")]
    resolved = render_prior_contact(prior_contact("dana@example.com", inbox, {"e1": "approved"}))
    ignored = render_prior_contact(prior_contact("dana@example.com", inbox, {"e1": "pending"}))

    assert "all resolved" in resolved and "you approved + sent it" in resolved
    assert "NOBODY HAS REPLIED" not in resolved
    assert "got no reply" in ignored and "NOBODY HAS REPLIED" in ignored


def test_a_first_time_customer_gets_no_block_at_all():
    """A stranger must cost nothing: "" keeps their prompt byte-identical to the pre-memory one."""
    assert prior_contact("nobody@example.com", [], {}) == []
    assert render_prior_contact([]) == ""
    assert prior_contact("", [_row("e1", "dana@example.com", "s", received_at="")], {}) == []


def test_history_is_bounded_to_the_last_five():
    """Unbounded history would tax every leg of every walk. Newest five, oldest→newest."""
    inbox = [
        _row(f"e{i}", "dana@example.com", f"Email {i}", received_at=f"Thu, {i} Jul 2026 10:00:00 -0400")
        for i in range(1, 9)
    ]
    entries = prior_contact("dana@example.com", inbox, {})
    assert [e["subject"] for e in entries] == ["Email 4", "Email 5", "Email 6", "Email 7", "Email 8"]


def test_an_old_inbox_line_without_a_thread_key_still_reads():
    """Real prod lines predate `thread` (and `result` keys drift). An old line is not a broken one."""
    legacy = {"id": "old-1", "sender_email": "dana@example.com", "subject": "Old one"}
    entries = prior_contact("dana@example.com", [legacy], {})
    assert len(entries) == 1 and entries[0]["subject"] == "Old one"
    assert render_prior_contact(entries)  # renders without a category/result/received_at


def test_the_email_being_triaged_is_never_its_own_prior_contact():
    """A Postmark retry replays a webhook whose email is already persisted. Bean must not cite it."""
    inbox = [_row("e1", "dana@example.com", "Return Label", received_at="Thu, 9 Jul 2026 10:00:00 -0400")]
    assert prior_contact("dana@example.com", inbox, {}, exclude_id="e1") == []


def test_history_block_reads_the_live_volume(tmp_path, monkeypatch):
    monkeypatch.setenv("BEAN_DATA_DIR", str(tmp_path))
    append_inbox(_item("e1", "dana@example.com", "Return Label", received_at="Thu, 9 Jul 2026 10:00:00 -0400"),
                 path=inbox_path())
    status_path().parent.mkdir(parents=True, exist_ok=True)
    status_path().write_text(json.dumps({"e1": "approved"}), encoding="utf-8")

    block = history_block("dana@example.com")
    assert "Return Label" in block and "you approved + sent it" in block
    assert history_block("stranger@example.com") == ""  # a different sender is still a stranger


# ---- 3. the REAL inbound path: two emails from one sender, and Bean remembers the first -------

def _payload(msg_id, sender, subject, body, *, date, attachments=None, text_body=None):
    """A synthetic Postmark inbound-parse payload (invented sender)."""
    return {
        "FromName": "Dana Whitlock", "From": sender,
        "FromFull": {"Email": sender, "Name": "Dana Whitlock"},
        "Subject": subject, "MessageID": msg_id, "ReplyTo": "", "Date": date,
        "TextBody": text_body if text_body is not None else body,
        "HtmlBody": "", "StrippedTextReply": "", "Attachments": attachments or [],
    }


@pytest.fixture
def memory_server(tmp_path, monkeypatch):
    """The real webhook → the real server path (real disk read of inbox.jsonl + status.json) → the
    real notebook engine. ONLY the network is faked, and the one model is shared so a test can read
    the exact user messages each POST put in front of it."""
    monkeypatch.setenv("BEAN_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("BEAN_INBOUND_TOKEN", "sekret")
    monkeypatch.delenv("BEAN_PASSCODE", raising=False)

    # A real config on disk, so the server serves it (not the demo default).
    cfg_path = tmp_path / "bean-config.json"
    cfg_path.write_text(json.dumps(CFG.to_dict()), encoding="utf-8")
    monkeypatch.setattr(srv, "CONFIG_PATH", cfg_path)

    from bean.gate import GateResult
    monkeypatch.setattr(srv, "gate", lambda email, rules=None, customer=None: GateResult("reply", "customer", "A real customer."))

    # NOT a stub of the engine: the real _run_engine runs, so the history read in bean/server.py —
    # the seam under test — actually executes. Only the network is swapped for a fake.
    nb_path = tmp_path / "notebook.md"
    Notebook("A store", buckets=[Bucket("Orders, Returns & Refunds", "cite the return policy")]).save(nb_path)
    monkeypatch.setattr(srv, "NOTEBOOK_PATH", nb_path)
    model = FakeModel({"draft": {
        "bucket": "Orders, Returns & Refunds", "draft": "Here is your return label.",
        "citations": ["notebook:Orders, Returns & Refunds"], "confidence": "green", "why_unsure": [],
    }})
    monkeypatch.setattr(srv, "ModelAdapter", lambda *a, **k: model)

    httpd = ThreadingHTTPServer(("127.0.0.1", 0), srv.BeanHandler)
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    try:
        yield f"http://127.0.0.1:{httpd.server_address[1]}", model, tmp_path
    finally:
        httpd.shutdown()
        httpd.server_close()


def _post_inbound(url, payload):
    req = urllib.request.Request(
        f"{url}/api/inbound?token=sekret", data=json.dumps(payload).encode(),
        method="POST", headers={"Content-Type": "application/json"},
    )
    with urllib.request.urlopen(req) as resp:
        return resp.status, json.loads(resp.read().decode() or "null")


def test_a_repeat_contact_is_remembered_through_the_real_inbound_path(memory_server):
    """The five-emails-in-six-days bug, end to end. Bean must meet the SECOND email knowing about
    the first — and knowing nobody ever answered it."""
    url, model, _ = memory_server

    status, _ = _post_inbound(url, _payload(
        "m1", "dana@example.com", "Return Label", "I need to return the router.",
        date="Thu, 9 Jul 2026 10:00:00 -0400"))
    assert status == 200

    # The first email is a stranger's: no memory to have.
    assert "PRIOR CONTACT" not in model.calls[0]["user"]

    seen_before = len(model.calls)

    status, _ = _post_inbound(url, _payload(
        "m2", "dana@example.com", "Re: Return Label", "Still waiting on that label — third time asking.",
        date="Sun, 12 Jul 2026 08:00:00 -0400"))
    assert status == 200

    # The SECOND email's drafting call knows her. This is where "escalate a repeat-contact
    # customer" becomes a decidable rule instead of a wish.
    user = model.calls[seen_before]["user"]
    assert "PRIOR CONTACT" in user, "the engine met her as a stranger on her second email"
    assert "Return Label" in user          # what she wrote about last time
    assert "NOBODY HAS REPLIED" in user    # and that it was never answered

    # ...and none of it leaked into the cached prefix.
    assert "PRIOR CONTACT" not in json.dumps(model.calls[-1]["system"])


def test_the_prior_email_reads_as_resolved_once_the_operator_actions_it(memory_server):
    """Same two emails, but the operator handled the first — the second must NOT be told the
    customer was ignored."""
    url, model, tmp_path = memory_server
    _post_inbound(url, _payload("m1", "dana@example.com", "Return Label", "I need to return the router.",
                                date="Thu, 9 Jul 2026 10:00:00 -0400"))

    status_path().parent.mkdir(parents=True, exist_ok=True)
    status_path().write_text(json.dumps({"m1": "approved"}), encoding="utf-8")

    before = len(model.calls)
    _post_inbound(url, _payload("m2", "dana@example.com", "Re: Return Label", "Thanks — one more thing.",
                                date="Sun, 12 Jul 2026 08:00:00 -0400"))

    user = model.calls[before]["user"]
    assert "PRIOR CONTACT" in user and "all resolved" in user
    assert "NOBODY HAS REPLIED" not in user


def test_the_thread_survives_to_disk(memory_server):
    """`thread` was extracted and then dropped before persistence — so a triaged email lost the
    conversation it belonged to the moment it was written down."""
    url, _, _ = memory_server
    quoted = "I need to return the router.\n\nOn Thu, Jul 9, 2026 at 10:00 AM Dana wrote:\n> original ask"
    _post_inbound(url, _payload("m3", "dana@example.com", "Re: Return Label", "", date="", text_body=quoted))

    item = load_inbox(inbox_path())[-1]
    assert item["thread"], "the quoted chain never reached disk"
    assert "original ask" in item["thread"][0]
    assert "original ask" not in item["body"]  # body stays the latest message only


# ---- 4. attachments: the photo IS the evidence on a defect claim ------------------------------

_PIXEL = (Path(__file__).parent / "fixtures_pixel.png").read_bytes()
assert _PIXEL[:8] == b"\x89PNG\r\n\x1a\n"  # the fixture is a real PNG, so the sniff admits it
_JPEG = b"\xff\xd8\xff" + b"\x00" * 64     # a minimal JPEG-magic blob (enough for the byte sniff)
_NOT_AN_IMAGE = b"%PDF-1.7\nthis is not an image at all"


def _attachment(name, ctype, raw=_PIXEL):
    return {"Name": name, "ContentType": ctype, "ContentLength": len(raw),
            "Content": base64.standard_b64encode(raw).decode()}


def test_an_attached_photo_reaches_the_model(memory_server):
    url, model, tmp_path = memory_server
    _post_inbound(url, _payload(
        "m4", "dana@example.com", "Broken on arrival", "See attached photo.",
        date="Thu, 9 Jul 2026 10:00:00 -0400", attachments=[_attachment("damage.png", "image/png")]))

    saved = list(tmp_path.rglob("damage.png"))
    assert saved, "the customer's photo was dropped on the floor"
    assert saved[0].read_bytes() == _PIXEL
    # It didn't just land on disk — it went INTO the model call (the whole point).
    assert str(saved[0]) in model.images[-1]


def test_a_hostile_attachment_name_cannot_escape_the_data_root(memory_server, tmp_path):
    """`Name` is attacker-controlled. A traversal here writes attacker bytes outside the volume AND
    feeds them to a model read — so it is confined at the boundary, twice."""
    url, model, data_root = memory_server
    _post_inbound(url, _payload(
        "m5", "dana@example.com", "Broken", "See attached.",
        date="", attachments=[_attachment("../../../../../../tmp/evil.png", "image/png")]))

    assert not Path("/tmp/evil.png").exists()
    for path in model.images[-1]:
        assert Path(path).is_relative_to(data_root), f"{path} escaped the data root"


def test_a_dotted_message_id_cannot_choose_the_write_directory(memory_server):
    """MessageID is header-controlled and the folder-name allowlist KEEPS dots, so a bare ".." would
    survive sanitizing as a real parent-dir hop and drop the file in the customer dir instead of the
    per-email folder. Contained by the data root either way, but a webhook must not pick its own
    write directory."""
    url, model, data_root = memory_server
    _post_inbound(url, _payload(
        "..", "dana@example.com", "Broken", "See attached.",
        date="", attachments=[_attachment("shot.png", "image/png")]))

    written = model.images[-1]
    assert written, "the photo should still be saved — this is containment, not rejection"
    for path in written:
        assert Path(path).parent.parent == data_root / default_customer() / "attachments"
        assert Path(path).parent.name == "unknown"  # ".." collapsed, never a parent-dir hop


def test_a_jpeg_is_admitted_by_its_bytes_even_with_an_odd_name():
    """The stored extension follows the BYTES, not the sender's claim — a JPEG named '.jpeg' with a
    text/plain content-type is still a JPEG and still reaches the model, stored as .jpg."""
    from bean.server import _save_inbound_images
    payload = {"Attachments": [{"Name": "photo.jpeg", "ContentType": "text/plain",
                                "Content": base64.b64encode(_JPEG).decode()}]}
    saved = _save_inbound_images(payload, "m-jpeg", customer="acme")
    assert len(saved) == 1 and saved[0].endswith(".jpg")


def test_non_image_bytes_wearing_an_image_name_are_refused(memory_server):
    """The core of the lost-email bug: valid base64 of NON-image bytes, named '.png' with an
    image/png content-type, passed the old name+type filter and shipped garbage to the API as a
    PNG — a 400 nothing on the inbound path caught, so the email was lost and Postmark retried it
    forever. The byte sniff refuses it; the email still triages, with no image."""
    url, model, tmp_path = memory_server
    status, _ = _post_inbound(url, _payload(
        "m6", "dana@example.com", "Invoice question", "See attached.", date="",
        attachments=[
            _attachment("statement.png", "image/png", raw=_NOT_AN_IMAGE),  # the smuggling attempt
            _attachment("invoice.pdf", "application/pdf", raw=_NOT_AN_IMAGE),
            _attachment("photo.heic", "image/heic", raw=_NOT_AN_IMAGE),      # a real iPhone photo bean/llm.py can't encode
            {"Name": "broken.png", "ContentType": "image/png", "Content": "!!not base64!!"},
        ]))
    assert status == 200                       # the email still got triaged
    assert model.images[-1] == []    # ...with no images ever reaching the model
    assert not list(tmp_path.rglob("*.pdf")) and not list(tmp_path.rglob("*.png"))


def test_an_oversize_image_is_skipped_not_shipped(memory_server):
    """A real 26 MB phone photo would blow the API's per-image ceiling → the same uncaught 400. It
    is capped here, and its email still triages."""
    url, model, tmp_path = memory_server
    big = _PIXEL + b"\x00" * (5 * 1024 * 1024)  # valid PNG magic, over the 4 MB cap
    status, _ = _post_inbound(url, _payload(
        "m7", "dana@example.com", "Huge photo", "See attached.", date="",
        attachments=[_attachment("huge.png", "image/png", raw=big)]))
    assert status == 200
    assert model.images[-1] == []


def test_the_attachment_count_is_capped(memory_server):
    """A 35 MB body of tiny PNGs is >100k files; the per-email cap bounds the writes and the images
    that reach the model."""
    url, model, tmp_path = memory_server
    status, _ = _post_inbound(url, _payload(
        "m8", "dana@example.com", "Many", "See attached.", date="",
        attachments=[_attachment(f"p{i}.png", "image/png") for i in range(25)]))
    assert status == 200
    assert len(model.images[-1]) == 10  # _MAX_IMAGES_PER_EMAIL


# ---- 5. order recognition: the real Shopify format has no hyphen, so it had never matched ----

def _notification(subject, body=""):
    return Email("n1", "Sable & Wren", "orders@shopify.com", subject, body or "Your order is confirmed.")


@pytest.mark.parametrize("subject, expected", [
    ("Order #SW45336 confirmed", "SW45336"),                          # the real Shopify format
    ("Re: Order #SW45189 confirmed", "SW45189"),
    ("A shipment from order #SW45225 is on the way", "SW45225"),
    ("Order SW45336 confirmed", "SW45336"),                           # no '#'
    ("A shipment from order SW-10482 is on the way", "SW-10482"),     # the hyphenated fixture form
])
def test_order_numbers_parse_in_the_stores_real_formats(subject, expected):
    rec = parse_notification(_notification(subject), config=CFG)
    assert rec is not None, f"{subject!r} parsed as no order at all"
    assert rec.order_no == expected


def test_a_store_that_has_not_configured_a_prefix_falls_back_to_the_shipped_default():
    # `orderPrefixes` is per-tenant, so a config that never set it must not end up with an
    # allowlist that matches nothing — it gets the shipped fictional prefixes instead. MM is one of
    # them and is NOT in the Sable & Wren config above, which is what makes this a real assertion.
    rec = parse_notification(_notification("A shipment from order MM-10482 is on the way"))
    assert rec is not None and rec.order_no == "MM-10482"


@pytest.mark.parametrize("subject", [
    "Your net-30 invoice is confirmed",   # the false positives the prefix allowlist exists to stop
    "Your Harlow II sofa has shipped",
    "Your order is confirmed",            # a notification with no order number at all
])
def test_the_allowlist_still_refuses_things_that_are_not_orders(subject):
    assert parse_notification(_notification(subject), config=CFG) is None


def test_a_customer_asking_about_a_hashed_order_grounds_on_the_indexed_one():
    """The two halves of the same bug: the INDEX is keyed off the store's notification mail and the
    LOOKUP off the customer's email. If they normalize differently, WISMO grounds on nothing."""
    from bean.order_index import OrderIndex, OrderRecord

    rec = parse_notification(_notification("Order #SW45336 confirmed", "Customer: Dana <dana@example.com>"), config=CFG)
    lookup = OrderLookup(orders={}, index=OrderIndex(records=[rec]), config=CFG)
    asking = Email("c1", "Dana", "dana@example.com", "Where is my order?", "Any news on #SW45336?")

    assert lookup.order_numbers(asking) == ["SW45336"]
    assert "SW45336" in lookup.facts_for(asking)
