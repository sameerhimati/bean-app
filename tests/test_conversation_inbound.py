"""One draft per conversation, driven through the REAL inbound webhook.

The unit tests in test_conversation.py pin the pure grouping. These pin the thing that ships: POST
a second email into a conversation nobody has answered and assert (a) the bytes actually put in
front of the model tell it to answer both, and (b) the earlier row is marked as rolled into the new
draft, so the queue stops showing two drafts for one reply.

Only the network is faked — the real handler, the real disk reads, the real engine. A test that
patched the seam it means to exercise would prove nothing (project memory: "verify the path the
server calls").

All senders and addresses here are invented.
"""

from __future__ import annotations

import json
import threading
import urllib.request
from http.server import ThreadingHTTPServer

import pytest

from bean import server as srv
from bean.fixtures import sw_config
from bean.inbox import load_inbox
from bean.llm import FakeModel
from bean.notebook import Bucket, Notebook
from bean.paths import inbox_path, status_path

CFG = sw_config()
SENDER = "dana@example.com"


def _payload(msg_id, subject, body, *, date, sender=SENDER, text_body=None):
    return {
        "FromName": "Dana Whitlock", "From": sender,
        "FromFull": {"Email": sender, "Name": "Dana Whitlock"},
        "Subject": subject, "MessageID": msg_id, "ReplyTo": "", "Date": date,
        "TextBody": text_body if text_body is not None else body,
        "HtmlBody": "", "StrippedTextReply": "", "Attachments": [],
    }


@pytest.fixture
def server(tmp_path, monkeypatch):
    monkeypatch.setenv("BEAN_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("BEAN_INBOUND_TOKEN", "sekret")
    monkeypatch.delenv("BEAN_PASSCODE", raising=False)

    cfg_path = tmp_path / "bean-config.json"
    cfg_path.write_text(json.dumps(CFG.to_dict()), encoding="utf-8")
    monkeypatch.setattr(srv, "CONFIG_PATH", cfg_path)

    from bean.gate import GateResult
    monkeypatch.setattr(
        srv, "gate", lambda email, rules=None, customer=None: GateResult("reply", "customer", "A real customer.")
    )

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
        yield f"http://127.0.0.1:{httpd.server_address[1]}", model
    finally:
        httpd.shutdown()
        httpd.server_close()


def _post(url, payload):
    req = urllib.request.Request(
        f"{url}/api/inbound?token=sekret", data=json.dumps(payload).encode(),
        method="POST", headers={"Content-Type": "application/json"},
    )
    with urllib.request.urlopen(req) as resp:
        return resp.status


def _rows():
    return {r["id"]: r for r in load_inbox(inbox_path())}


def test_a_second_unanswered_email_drafts_once_for_the_whole_conversation(server):
    url, model = server
    _post(url, _payload("m1", "Return Label", "I need to return the router.",
                        date="Thu, 9 Jul 2026 10:00:00 -0400"))

    before = len(model.calls)
    _post(url, _payload("m2", "Re: Return Label", "Still nothing — second time asking.",
                        date="Sun, 12 Jul 2026 08:00:00 -0400"))

    user = model.calls[before]["user"]
    assert "ONGOING CONVERSATION" in user
    assert "I need to return the router." in user     # the earlier BODY, so one reply can answer it
    assert "NOBODY HAS REPLIED TO ANY OF THESE" in user
    assert "ONE reply that answers all of them" in user

    # ...and none of it leaked into the cached prefix, which must stay byte-stable to keep caching.
    assert "ONGOING CONVERSATION" not in json.dumps(model.calls[before]["system"])


def test_the_earlier_message_is_rolled_into_the_new_draft(server):
    url, _ = server
    _post(url, _payload("m1", "Return Label", "I need to return the router.",
                        date="Thu, 9 Jul 2026 10:00:00 -0400"))
    _post(url, _payload("m2", "Re: Return Label", "Still nothing?",
                        date="Sun, 12 Jul 2026 08:00:00 -0400"))

    rows = _rows()
    assert rows["m1"]["result"]["rolled_into"] == "m2"
    assert not rows["m2"]["result"].get("rolled_into")  # the newest one IS the group's draft
    # Nothing destroyed: m1 keeps the verdict Bean originally gave it.
    assert rows["m1"]["result"]["bucket"] == "Orders, Returns & Refunds"
    assert len(rows) == 2  # no line dropped, no line duplicated


def test_a_third_message_rolls_up_everything_still_open(server):
    url, _ = server
    for n, (mid, subj) in enumerate([("m1", "Return Label"), ("m2", "Re: Return Label"), ("m3", "RE: Return Label")]):
        _post(url, _payload(mid, subj, f"message {n}", date=f"Thu, {9 + n} Jul 2026 10:00:00 -0400"))

    rows = _rows()
    assert rows["m1"]["result"]["rolled_into"] == "m3"
    assert rows["m2"]["result"]["rolled_into"] == "m3"
    assert not rows["m3"]["result"].get("rolled_into")


def test_an_answered_conversation_asks_only_about_what_is_new(server):
    url, model = server
    _post(url, _payload("m1", "Return Label", "I need to return the router.",
                        date="Thu, 9 Jul 2026 10:00:00 -0400"))
    status_path().parent.mkdir(parents=True, exist_ok=True)
    status_path().write_text(json.dumps({"m1": "approved"}), encoding="utf-8")

    before = len(model.calls)
    _post(url, _payload("m2", "Re: Return Label", "Thanks — one more thing.",
                        date="Sun, 12 Jul 2026 08:00:00 -0400"))

    user = model.calls[before]["user"]
    assert "NOBODY HAS REPLIED TO ANY OF THESE" not in user
    # She already dealt with m1, so it is not rolled into anything — it is done.
    assert not _rows()["m1"]["result"].get("rolled_into")


def test_a_different_subject_from_the_same_person_stays_its_own_draft(server):
    url, _ = server
    _post(url, _payload("m1", "Return Label", "About a return.", date="Thu, 9 Jul 2026 10:00:00 -0400"))
    _post(url, _payload("m2", "Do you ship to Canada?", "Unrelated question.",
                        date="Sun, 12 Jul 2026 08:00:00 -0400"))

    rows = _rows()
    assert not rows["m1"]["result"].get("rolled_into")
    assert not rows["m2"]["result"].get("rolled_into")


def test_a_lone_email_is_unchanged_by_grouping(server):
    """The majority path. One email from a new sender must put the same bytes in front of the model
    as it did before conversations existed — otherwise every draft pays for a feature most mail
    never uses, and the cached prefix stops being stable."""
    url, model = server
    _post(url, _payload("m1", "Return Label", "I need to return the router.",
                        date="Thu, 9 Jul 2026 10:00:00 -0400"))

    user = model.calls[-1]["user"]
    assert "ONGOING CONVERSATION" not in user
    assert "PRIOR CONTACT" not in user
    assert not _rows()["m1"]["result"].get("rolled_into")


def test_a_webhook_retry_never_makes_an_email_its_own_predecessor(server):
    """Postmark retries a delivered webhook. The replay must not roll the original into itself."""
    url, _ = server
    payload = _payload("m1", "Return Label", "I need to return the router.",
                       date="Thu, 9 Jul 2026 10:00:00 -0400")
    _post(url, payload)
    _post(url, payload)  # same MessageID, replayed

    for row in load_inbox(inbox_path()):
        assert row["result"].get("rolled_into") != row["id"]


def test_grouping_failure_never_blocks_the_mail(server, monkeypatch):
    """Grouping is an enhancement. If the conversation read raises, the email still gets triaged —
    the same degrade stance customer_history takes for the same reason."""
    url, _ = server
    _post(url, _payload("m1", "Return Label", "First.", date="Thu, 9 Jul 2026 10:00:00 -0400"))

    def boom(*a, **k):
        raise RuntimeError("volume unreadable")

    monkeypatch.setattr(srv.conversation, "open_siblings", boom)
    _post(url, _payload("m2", "Re: Return Label", "Second.", date="Sun, 12 Jul 2026 08:00:00 -0400"))

    assert "m2" in _rows()  # the customer's email is on disk and triaged, which is what matters
