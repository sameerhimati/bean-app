"""POST /api/redraft — one tap re-drafts a pile-up so ONE reply answers all of it.

Driven through the real handler with only the network faked. The mail this exists for was triaged
before conversations existed: each message got its own draft, and each of those drafts had only ever
read the last message. Her live queue holds 13 such pile-ups.

The invariants worth pinning are the destructive ones — this is the only operator-triggered path
that rewrites inbox.jsonl.
"""

from __future__ import annotations

import json
import threading
import urllib.error
import urllib.request
from http.server import ThreadingHTTPServer

import pytest

from bean import server as srv
from bean.fixtures import sw_config
from bean.inbox import InboxItem, append_inbox, load_inbox
from bean.llm import FakeModel
from bean.notebook import Bucket, Notebook
from bean.paths import inbox_path, status_path

CFG = sw_config()
SENDER = "dana@example.com"

DRAFT = {
    "bucket": "Orders, Returns & Refunds", "draft": "Here is your return label.",
    "citations": ["notebook:Orders, Returns & Refunds"], "confidence": "green", "why_unsure": [],
}


def _item(email_id, subject, body, *, sender=SENDER, received="", result=None):
    return InboxItem(
        id=email_id, sender_name="Dana Whitlock", sender_email=sender, reply_to=sender,
        subject=subject, body=body, received_at=received,
        result=result if result is not None else {"bucket": "x", "confidence": "red",
                                                  "draft": "old draft", "citations": [], "why_unsure": []},
    )


@pytest.fixture
def server(tmp_path, monkeypatch):
    monkeypatch.setenv("BEAN_DATA_DIR", str(tmp_path))
    monkeypatch.delenv("BEAN_PASSCODE", raising=False)
    monkeypatch.setattr(srv, "DEMO_READONLY", False)

    cfg_path = tmp_path / "bean-config.json"
    cfg_path.write_text(json.dumps(CFG.to_dict()), encoding="utf-8")
    monkeypatch.setattr(srv, "CONFIG_PATH", cfg_path)

    nb_path = tmp_path / "notebook.md"
    Notebook("A store", buckets=[Bucket("Orders, Returns & Refunds", "cite the return policy")]).save(nb_path)
    monkeypatch.setattr(srv, "NOTEBOOK_PATH", nb_path)

    model = FakeModel({"draft": dict(DRAFT)})
    monkeypatch.setattr(srv, "ModelAdapter", lambda *a, **k: model)
    srv._preview_hits[:] = []

    httpd = ThreadingHTTPServer(("127.0.0.1", 0), srv.BeanHandler)
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    try:
        yield f"http://127.0.0.1:{httpd.server_address[1]}", model
    finally:
        httpd.shutdown()
        httpd.server_close()


def _post(url, body):
    req = urllib.request.Request(
        f"{url}/api/redraft", data=json.dumps(body).encode(),
        method="POST", headers={"Content-Type": "application/json"},
    )
    try:
        with urllib.request.urlopen(req) as resp:
            return resp.status, json.loads(resp.read())
    except urllib.error.HTTPError as exc:
        raw = exc.read()
        try:
            return exc.code, json.loads(raw)
        except json.JSONDecodeError:
            return exc.code, {}


def _seed(*items):
    for it in items:
        append_inbox(it, path=inbox_path())


def _rows():
    return {r["id"]: r for r in load_inbox(inbox_path())}


def test_a_pile_up_redrafts_once_and_folds_the_rest_in(server):
    url, model = server
    _seed(_item("m1", "Return Label", "I need to return the router.", received="Thu, 9 Jul 2026 10:00:00 -0400"),
          _item("m2", "Re: Return Label", "Still nothing?", received="Sun, 12 Jul 2026 08:00:00 -0400"))

    status, body = _post(url, {"email_id": "m2"})
    assert status == 200
    assert body["covered"] == 2
    assert body["draft"] == "Here is your return label."

    # The earlier message's BODY reached the model, which is the whole point of re-drafting.
    user = model.calls[-1]["user"]
    assert "I need to return the router." in user
    assert "NOBODY HAS REPLIED TO ANY OF THESE" in user

    rows = _rows()
    assert rows["m2"]["result"]["draft"] == "Here is your return label."  # the new verdict is stored
    assert rows["m1"]["result"]["rolled_into"] == "m2"
    assert not rows["m2"]["result"].get("rolled_into")
    assert len(rows) == 2  # nothing dropped, nothing duplicated


def test_the_earlier_rows_keep_their_own_verdict(server):
    url, _ = server
    _seed(_item("m1", "Return Label", "First.", received="Thu, 9 Jul 2026 10:00:00 -0400"),
          _item("m2", "Re: Return Label", "Second.", received="Sun, 12 Jul 2026 08:00:00 -0400"))
    _post(url, {"email_id": "m2"})
    # Only `rolled_into` is added — the row still says what Bean originally made of it.
    assert _rows()["m1"]["result"]["draft"] == "old draft"
    assert _rows()["m1"]["result"]["confidence"] == "red"


def test_a_lone_email_is_refused_rather_than_charged_for(server):
    """Nothing is waiting behind it, so a re-draft would return what is already on screen. Refusing
    is cheaper and more honest than billing her for an identical draft."""
    url, model = server
    _seed(_item("m1", "Return Label", "Only message.", received="Thu, 9 Jul 2026 10:00:00 -0400"))
    status, body = _post(url, {"email_id": "m1"})
    assert status == 409
    assert "nothing else is waiting" in body["error"]
    assert not model.calls


def test_an_already_answered_conversation_is_refused(server):
    url, model = server
    _seed(_item("m1", "Return Label", "First.", received="Thu, 9 Jul 2026 10:00:00 -0400"),
          _item("m2", "Re: Return Label", "Second.", received="Sun, 12 Jul 2026 08:00:00 -0400"))
    status_path().parent.mkdir(parents=True, exist_ok=True)
    status_path().write_text(json.dumps({"m1": "approved"}), encoding="utf-8")

    status, _ = _post(url, {"email_id": "m2"})
    assert status == 409       # she already answered m1; only m2 is outstanding
    assert not model.calls


def test_filed_mail_is_refused_and_pointed_at_the_right_recovery(server):
    url, model = server
    _seed(_item("m1", "Return Label", "First.", received="Thu, 9 Jul 2026 10:00:00 -0400"),
          _item("f1", "Re: Return Label", "A newsletter.", received="Sun, 12 Jul 2026 08:00:00 -0400",
                result={"disposition": "filed", "kind": "newsletter", "reason": "no question in it"}))
    status, body = _post(url, {"email_id": "f1"})
    assert status == 409
    assert "needs a reply" in body["error"]
    assert not model.calls


def test_an_unknown_id_is_a_404_not_a_silent_no_op(server):
    url, model = server
    status, _ = _post(url, {"email_id": "nope"})
    assert status == 404
    assert not model.calls


def test_a_missing_id_is_refused_before_any_read(server):
    url, model = server
    assert _post(url, {})[0] == 400
    assert not model.calls


def test_a_model_failure_leaves_the_log_untouched(server, monkeypatch):
    """The rewrite happens only after a successful draft. A 502 that had already folded the siblings
    in would leave a conversation pointing at a draft that does not exist."""
    url, _ = server
    _seed(_item("m1", "Return Label", "First.", received="Thu, 9 Jul 2026 10:00:00 -0400"),
          _item("m2", "Re: Return Label", "Second.", received="Sun, 12 Jul 2026 08:00:00 -0400"))
    before = inbox_path().read_text(encoding="utf-8")

    def boom(*a, **k):
        raise RuntimeError("no brain")

    monkeypatch.setattr(srv, "_run_engine", boom)
    status, _ = _post(url, {"email_id": "m2"})
    assert status == 502
    assert inbox_path().read_text(encoding="utf-8") == before


def test_a_read_only_demo_has_no_redraft_route(server, monkeypatch):
    url, model = server
    _seed(_item("m1", "Return Label", "First.", received="Thu, 9 Jul 2026 10:00:00 -0400"),
          _item("m2", "Re: Return Label", "Second.", received="Sun, 12 Jul 2026 08:00:00 -0400"))
    monkeypatch.setattr(srv, "DEMO_READONLY", True)
    assert _post(url, {"email_id": "m2"})[0] == 404
    assert not model.calls


def test_redrafting_twice_is_allowed_and_stays_consistent(server):
    """A second tap re-drafts again rather than refusing.

    A row that is already `rolled_into` this one is still UNANSWERED, so it stays an open sibling —
    which is what lets a third message fold in both of its predecessors. The visible consequence is
    that the button keeps its promise: tap it again (say, after teaching Bean something) and you get
    a fresh draft. What must not happen is the conversation eating itself.
    """
    url, _ = server
    _seed(_item("m1", "Return Label", "First.", received="Thu, 9 Jul 2026 10:00:00 -0400"),
          _item("m2", "Re: Return Label", "Second.", received="Sun, 12 Jul 2026 08:00:00 -0400"))
    assert _post(url, {"email_id": "m2"})[0] == 200
    status, body = _post(url, {"email_id": "m2"})

    assert status == 200 and body["covered"] == 2
    rows = _rows()
    assert rows["m1"]["result"]["rolled_into"] == "m2"
    assert not rows["m2"]["result"].get("rolled_into")   # never folded into itself
    assert len(rows) == 2                                 # and nothing was lost or duplicated


def test_a_third_message_folds_in_both_predecessors(server):
    """The reason a rolled-in row stays an open sibling. m1 was already folded into m2; when m3
    arrives and she re-drafts it, ONE reply has to answer all three."""
    url, model = server
    _seed(_item("m1", "Return Label", "First.", received="Thu, 9 Jul 2026 10:00:00 -0400"),
          _item("m2", "Re: Return Label", "Second.", received="Fri, 10 Jul 2026 08:00:00 -0400"),
          _item("m3", "RE: Return Label", "Third.", received="Sun, 12 Jul 2026 08:00:00 -0400"))
    assert _post(url, {"email_id": "m2"})[0] == 200      # m1 → m2
    status, body = _post(url, {"email_id": "m3"})        # now everything → m3

    assert status == 200 and body["covered"] == 3
    user = model.calls[-1]["user"]
    assert "First." in user and "Second." in user
    rows = _rows()
    assert rows["m1"]["result"]["rolled_into"] == "m3"
    assert rows["m2"]["result"]["rolled_into"] == "m3"
    assert not rows["m3"]["result"].get("rolled_into")
