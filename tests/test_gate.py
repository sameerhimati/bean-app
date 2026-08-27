"""The pre-generation triage gate (bean/gate.py) — all offline via FakeModel.

Proves the four behaviors that make the gate trustworthy:
  - the operator's rules short-circuit the model (and alwaysReply beats alwaysFile — a customer
    must never be filed by a sloppy rule),
  - the classifier files no-reply mail with a kind + reason and passes customer mail through,
  - the default is asymmetric: anything not clearly "no" goes to reply (fail open),
  - the server runs the gate BEFORE the tree ($0 drafting for filed mail) and `skipGate`
    is the recovery path back into the tree.
"""

from __future__ import annotations

import json
import threading
import urllib.request
from http.server import ThreadingHTTPServer

import pytest

from bean import server as srv
from tests.test_server import _install_engine
from bean.contract import Email
from bean.gate import GateResult, needs_reply
from bean.llm import FakeModel

CUSTOMER = Email("g-1", "Dana", "dana@gmail.com", "sofa arm creaking",
                 "My Harlow sofa arm started creaking after a week — can you help?")
NEWSLETTER = Email("g-2", "Supplier Weekly", "news@supplierweekly.com", "July deals inside!",
                   "Top 10 packaging trends this summer. Unsubscribe below.")


def _boom_model():
    """A FakeModel with no scripted responses — any call raises, proving no model was used."""
    return FakeModel({})


# ---- rules short-circuit the model ----------------------------------------------------------

def test_always_file_rule_files_without_model():
    r = needs_reply(NEWSLETTER, model=_boom_model(),
                    rules={"alwaysFile": ["supplierweekly.com"]})
    assert r.disposition == "file" and r.kind == "rule"
    assert "supplierweekly.com" in r.reason


def test_always_reply_beats_always_file():
    r = needs_reply(CUSTOMER, model=_boom_model(),
                    rules={"alwaysReply": ["gmail.com"], "alwaysFile": ["gmail.com"]})
    assert r.disposition == "reply" and r.kind == "rule"


def test_rules_match_subject_case_insensitively():
    r = needs_reply(NEWSLETTER, model=_boom_model(), rules={"alwaysFile": ["JULY DEALS"]})
    assert r.disposition == "file"


# ---- the classifier ---------------------------------------------------------------------------

def test_classifier_files_a_newsletter_with_kind_and_reason():
    m = FakeModel({"gate": {"needs_reply": "no", "kind": "newsletter",
                            "reason": "A marketing blast with no question in it."}})
    r = needs_reply(NEWSLETTER, model=m)
    assert r.disposition == "file" and r.kind == "newsletter" and r.reason
    # the gate rides its own lean system prompt, never the big drafting prefix
    assert "front gate" in m.calls[0]["system"][0]["text"]


def test_classifier_passes_a_customer_through():
    m = FakeModel({"gate": {"needs_reply": "yes", "kind": "customer",
                            "reason": "A customer with a product problem."}})
    assert needs_reply(CUSTOMER, model=m).disposition == "reply"


def test_anything_not_clearly_no_goes_to_reply():
    # Fail open: a malformed/absent verdict must never file a customer.
    m = FakeModel({"gate": {"kind": "other", "reason": "??"}})
    assert needs_reply(CUSTOMER, model=m).disposition == "reply"


# ---- the server seam: gate before the engine, skipGate as recovery ---------------------------

@pytest.fixture
def gated_url(tmp_path, monkeypatch):
    monkeypatch.setattr(srv, "CONFIG_PATH", tmp_path / "bean-config.json")
    # Bound at import from BEAN_CUSTOMER, so it must be patched, not left to BEAN_DATA_DIR — see
    # the note on the same line in test_inbound.py's inbound_server.
    monkeypatch.setattr(srv, "CORRECTIONS_PATH", tmp_path / "corrections.jsonl")
    _install_engine(monkeypatch, tmp_path)
    httpd = ThreadingHTTPServer(("127.0.0.1", 0), srv.BeanHandler)
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    try:
        yield f"http://127.0.0.1:{httpd.server_address[1]}"
    finally:
        httpd.shutdown()
        httpd.server_close()


def _post(url, body):
    req = urllib.request.Request(url, data=json.dumps(body).encode(), method="POST",
                                 headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req) as resp:
        return resp.status, json.loads(resp.read().decode())


_EMAIL_PAYLOAD = {"email": {"id": "g-2", "sender_name": "Supplier Weekly",
                            "sender_email": "news@supplierweekly.com",
                            "subject": "July deals inside!", "body": "Trends. Unsubscribe below."}}


def test_preview_files_before_the_engine(gated_url, monkeypatch, tmp_path):
    class _MustNotRun:
        def structured(self, *a, **k):
            raise AssertionError("the engine ran for filed mail — the gate must short-circuit")

    _install_engine(monkeypatch, tmp_path, model=_MustNotRun())
    monkeypatch.setattr(srv, "gate",
                        lambda email, rules=None, customer=None: GateResult("file", "newsletter", "No question."))

    status, body = _post(f"{gated_url}/api/preview", _EMAIL_PAYLOAD)
    assert status == 200
    assert body == {"disposition": "filed", "email_id": "g-2",
                    "kind": "newsletter", "reason": "No question."}


def test_skip_gate_recovers_into_the_engine(gated_url, monkeypatch):
    """The mis-file recovery path: skipGate bypasses the gate entirely and drafts. Without it a
    wrongly-filed customer would have no way back into the queue."""
    def gate_must_not_run(email, rules=None, customer=None):
        raise AssertionError("gate ran despite skipGate — recovery must bypass it")
    monkeypatch.setattr(srv, "gate", gate_must_not_run)

    status, body = _post(f"{gated_url}/api/preview", {**_EMAIL_PAYLOAD, "skipGate": True})
    assert status == 200
    # yellow, not green: no correction log for this tenant ⇒ empty shelf ⇒ never a one-tap send.
    # What this test is about is the RECOVERY reaching the engine at all, which it still does.
    assert body["disposition"] == "reply" and body["confidence"] == "yellow"
