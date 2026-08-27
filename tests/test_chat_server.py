"""POST /api/chat driven through the REAL handler.

test_chat.py pins the pure coercion. This pins the thing that ships: the route, its guards, and the
bytes actually put in front of the model. Only the network is faked — a test that patched the seam
it means to exercise would prove nothing (project memory: "verify the path the server calls").

The invariant worth stating out loud: this endpoint reads the notebook and never writes it. A turn
of conversation must not be able to change the operator's brain; only her confirming a proposal,
through PUT /api/notebook, can.
"""

from __future__ import annotations

import json
import threading
import urllib.error
import urllib.request
from http.server import ThreadingHTTPServer

import pytest

from bean import server as srv
from bean.llm import FakeModel
from bean.notebook import Bucket, Fact, Notebook

PROPOSAL = {
    "kind": "proposal", "text": "Me hears a rule in that.",
    "claim": "Broken headphones get replaced within 60 days — no receipt needed.",
    "section": "fact", "supersedes": "Headphone returns within 30 days, receipt required.",
    "consequence": "Me will lean on this for warranty mail.",
}
ANSWER = {"kind": "answer", "text": "Me asks them to wait a day.", "cites": ["notebook:Returns"]}


@pytest.fixture
def server(tmp_path, monkeypatch):
    monkeypatch.setenv("BEAN_DATA_DIR", str(tmp_path))
    monkeypatch.delenv("BEAN_PASSCODE", raising=False)
    monkeypatch.setattr(srv, "DEMO_READONLY", False)

    nb_path = tmp_path / "notebook.md"
    Notebook(
        "A store",
        buckets=[Bucket("Returns", "within 90 days I send a replacement")],
        facts=[Fact("Headphone returns within 30 days, receipt required.", "stated")],
    ).save(nb_path)
    monkeypatch.setattr(srv, "NOTEBOOK_PATH", nb_path)

    model = FakeModel({"answer_or_propose": dict(PROPOSAL)})
    monkeypatch.setattr(srv, "ModelAdapter", lambda *a, **k: model)

    # A fresh window per test — the limiter is module-global by design (one operator, one instance).
    srv._chat_hits[:] = []

    httpd = ThreadingHTTPServer(("127.0.0.1", 0), srv.BeanHandler)
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    try:
        yield f"http://127.0.0.1:{httpd.server_address[1]}", model, nb_path
    finally:
        httpd.shutdown()
        httpd.server_close()


def _post(url, body):
    req = urllib.request.Request(
        f"{url}/api/chat", data=json.dumps(body).encode(),
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


def test_a_stated_rule_comes_back_as_a_proposal_to_judge(server):
    url, model, _ = server
    status, body = _post(url, {"message": "we replace broken headphones within 60 days now"})

    assert status == 200
    assert body["kind"] == "proposal"
    assert body["claim"].startswith("Broken headphones")
    assert body["provenance"] == "stated"
    assert body["section"] == "fact"
    # It replaces a real line, and the card says so in the words she'll read.
    assert body["supersedes"] == "Headphone returns within 30 days, receipt required."
    assert 'Replaces: "Headphone returns within 30 days' in body["consequence"]
    assert body["receipts"]


def test_the_notebook_rides_in_the_cached_prefix_and_her_words_do_not(server):
    url, model, _ = server
    said = "walnut frames went up 8% last monday"
    _post(url, {"message": said})

    call = model.calls[-1]
    system = "\n".join(b["text"] for b in call["system"])
    assert "Headphone returns within 30 days" in system   # her notebook IS the prefix
    assert said in call["user"]
    assert said not in system                            # the per-turn half stays out of the cache


def test_the_transcript_reaches_the_model_oldest_first(server):
    url, model, _ = server
    _post(url, {
        "message": "make it 60 then",
        "transcript": [
            {"from": "you", "text": "what is the headphone window"},
            {"from": "bean", "text": "thirty days, me thinks"},
        ],
    })
    user = model.calls[-1]["user"]
    assert user.index("what is the headphone window") < user.index("thirty days")
    assert user.index("thirty days") < user.index("make it 60 then")


def test_a_question_comes_back_as_an_answer_with_receipts(server):
    url, model, _ = server
    model._responses["answer_or_propose"] = dict(ANSWER)
    status, body = _post(url, {"message": "what do i say about lost packages?"})
    assert status == 200
    assert body["kind"] == "answer"
    assert body["cites"] == ["notebook:Returns"]
    assert "claim" not in body  # nothing to confirm, so no card is offered


def test_the_endpoint_never_writes_the_notebook(server):
    """The separation that makes a chat safe: talking cannot change her brain. Only her confirming
    a proposal, through the one ETag-guarded writer, can."""
    url, _, nb_path = server
    before = nb_path.read_text(encoding="utf-8")
    _post(url, {"message": "we replace broken headphones within 60 days now"})
    _post(url, {"message": "and stop offering the discount"})
    assert nb_path.read_text(encoding="utf-8") == before


def test_a_blank_message_is_refused_before_any_model_call(server):
    url, model, _ = server
    status, _ = _post(url, {"message": "   "})
    assert status == 400
    assert not model.calls  # a 400 that has already paid for tokens is not a guard


def test_an_enormous_message_is_refused_before_any_model_call(server):
    url, model, _ = server
    status, _ = _post(url, {"message": "x" * (srv._CHAT_MAX_CHARS + 1)})
    assert status == 413
    assert not model.calls


def test_a_junk_transcript_is_ignored_rather_than_fatal(server):
    url, _, _ = server
    status, body = _post(url, {"message": "hello", "transcript": "not a list"})
    assert status == 200 and body["kind"] == "proposal"


def test_the_rate_limit_refuses_before_reaching_a_model(server):
    url, model, _ = server
    for _ in range(srv._CHAT_MAX_PER_MIN):
        assert _post(url, {"message": "hi"})[0] == 200
    calls_at_cap = len(model.calls)
    status, _ = _post(url, {"message": "hi"})
    assert status == 429
    assert len(model.calls) == calls_at_cap  # the 429 cost nothing


def test_a_model_failure_degrades_instead_of_500ing(server, monkeypatch):
    url, _, _ = server

    def boom(*a, **k):
        raise RuntimeError("no brain")

    monkeypatch.setattr(srv.chat, "reply", boom)
    status, body = _post(url, {"message": "hello"})
    assert status == 502
    assert "couldn't reach its brain" in body["error"]


def test_a_read_only_demo_has_no_chat_route_at_all(server, monkeypatch):
    # It spends a token per turn and exists to change somebody's notebook — both halves of the
    # demo-lock rule. 404, not 403: the route does not exist for a visitor.
    url, model, _ = server
    monkeypatch.setattr(srv, "DEMO_READONLY", True)
    status, _ = _post(url, {"message": "hello"})
    assert status == 404
    assert not model.calls


def test_no_notebook_yet_is_a_503_not_an_invented_one(server, monkeypatch):
    url, model, nb_path = server
    nb_path.unlink()
    status, _ = _post(url, {"message": "hello"})
    assert status == 503
    assert not model.calls  # there is nothing to talk ABOUT, so nothing is spent
