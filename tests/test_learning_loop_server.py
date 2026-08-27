"""Does a correction on disk reach the model on the path the SERVER actually calls?

Every other few-shot test answers a weaker question. `tests/test_fewshot_tree.py` hands
`corrections=[...]` straight to `evaluate_tree`, so it proves the plumbing *below* the server.
`tests/test_server.py` monkeypatches `srv.triage_tree` with a fake whose signature cannot even
accept corrections, so it proves the plumbing *above* the loop. Between them sat the only question
that matters — does `POST /api/preview` load the operator's log and put it in the prompt? — and 182 green
tests never asked it.

So this file fakes exactly ONE thing: the network (`bean.loop.live_model`). The real handler, the
real `triage_tree`, and the real on-disk correction log all run. If few-shot is not wired into the
request path, these tests fail.
"""

from __future__ import annotations

import json
import threading
import urllib.error
import urllib.request
from http.server import ThreadingHTTPServer

import pytest

from bean import server as srv
from bean.corrections import CorrectionsCorruptError
from bean.fixtures import sw_config
from bean.gate import GateResult
from bean.llm import FakeModel
from bean.notebook import Bucket, Notebook
from bean.paths import corrections_path

# The fabric chunk routes Sofas & Upholstery › Which fabric grade — so its top branch, and
# therefore the few-shot join key, is "Sofas & Upholstery".
_CHUNK_TEXT = "fabric grade for a home with dogs"
_EXEMPLAR_REPLY = "With a dog in the house you want Performance Weave — it wipes clean."
_EMAIL = {
    "id": "sw-c", "sender_name": "Sam", "sender_email": "sam@x.com",
    "subject": "fabric?", "body": "which fabric grade if we have a dog?",
}


def _correction_line(**over) -> str:
    row = {
        "email_id": "prior-1", "category": "Sofas & Upholstery", "confidence": "high",
        "action": "approve", "original_draft": None, "final_text": _EXEMPLAR_REPLY,
        "edit_kind": None, "note": "", "liked": False,
        "meta": {"email_subject": "which fabric", "email_body": "We have a dog, which grade?"},
    }
    row.update(over)
    return json.dumps(row)


@pytest.fixture
def preview(tmp_path, monkeypatch):
    """A live server whose only fake is the model client. Yields (post, draft_model).

    `BEAN_DATA_DIR` points at tmp_path, so `corrections_path()` — which `loop.triage_tree` calls at
    request time — resolves under it. That is the seam the production bug would live in.
    """
    monkeypatch.setenv("BEAN_DATA_DIR", str(tmp_path))
    monkeypatch.delenv("BEAN_CUSTOMER", raising=False)

    cfg_path = tmp_path / "config.json"
    cfg_path.write_text(json.dumps(sw_config().to_dict()), encoding="utf-8")
    monkeypatch.setattr(srv, "CONFIG_PATH", cfg_path)
    monkeypatch.setattr(srv, "STATUS_PATH", tmp_path / "status.json")
    monkeypatch.setattr(srv, "gate", lambda email, rules=None, customer=None: GateResult("reply"))
    # The server resolves its paths once at import (BEAN_DATA_DIR predates the process in prod), so
    # patch the constant, not just the env — exactly as the CONFIG_PATH seam does. `corrections_path()`
    # below reads the patched env, so the file the test writes and the file the server reads are one.
    monkeypatch.setattr(srv, "CORRECTIONS_PATH", corrections_path())

    nb_path = tmp_path / "notebook.md"
    Notebook("A store", buckets=[Bucket("Sofas & Upholstery", "cite the product doc")]).save(nb_path)
    monkeypatch.setattr(srv, "NOTEBOOK_PATH", nb_path)
    draft = FakeModel({"draft": {
        "bucket": "Sofas & Upholstery", "draft": "Performance Weave for a home with dogs.",
        "citations": ["notebook:Sofas & Upholstery"], "confidence": "green", "why_unsure": [],
    }})
    # The ONLY fake: the network. The server still loads corrections off disk itself.
    monkeypatch.setattr(srv, "ModelAdapter", lambda *a, **k: draft)

    httpd = ThreadingHTTPServer(("127.0.0.1", 0), srv.BeanHandler)
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    url = f"http://127.0.0.1:{httpd.server_address[1]}/api/preview"

    def post(body=None):
        req = urllib.request.Request(
            url, data=json.dumps(body or {"email": _EMAIL}).encode(),
            method="POST", headers={"Content-Type": "application/json"},
        )
        try:
            with urllib.request.urlopen(req) as resp:
                return resp.status, json.loads(resp.read().decode() or "null")
        except urllib.error.HTTPError as e:
            return e.code, json.loads(e.read().decode() or "null")

    try:
        yield post, draft
    finally:
        httpd.shutdown()
        httpd.server_close()


def _draft_user(draft: FakeModel) -> str:
    calls = [c for c in draft.calls if c["tool"] == "draft"]
    assert calls, f"the drafting call never ran; tools called: {[c['tool'] for c in draft.calls]}"
    return calls[0]["user"]


# ---- 1. THE test: a correction on disk reaches the drafting prompt via POST /api/preview -------

def test_correction_on_disk_reaches_the_draft_prompt_through_the_preview_endpoint(preview):
    post, draft = preview
    corrections_path().parent.mkdir(parents=True, exist_ok=True)
    corrections_path().write_text(_correction_line() + "\n", encoding="utf-8")

    status, body = post()
    assert status == 200 and body["disposition"] == "reply"

    user = _draft_user(draft)
    assert "HER PAST REPLIES YOU MAY LEAN ON" in user
    assert _EXEMPLAR_REPLY in user, "the operator's approved reply never reached the model"
    assert "which fabric" in user  # the exemplar's email context rode along


# ---- 1b. the flashcard un-starve: a 'teach' exemplar reaches the draft prompt too -------------

def test_flashcard_teach_exemplar_reaches_the_draft_prompt(preview):
    """The whole point of the flashcard. Before 'teach' joined the few-shot filter, a reply the
    operator authored for an untaught node never reached the model — the loop was wired and starved. A teach
    row with `final_text` must now ride into the DRAFT step exactly like an approve does."""
    post, draft = preview
    corrections_path().parent.mkdir(parents=True, exist_ok=True)
    corrections_path().write_text(_correction_line(action="teach") + "\n", encoding="utf-8")

    status, body = post()
    assert status == 200 and body["disposition"] == "reply"
    assert _EXEMPLAR_REPLY in _draft_user(draft), "a taught reply never reached the model — still starved"


# ---- 2. empty log → a new customer drafts fine, with no exemplar block ------------------------

def test_absent_correction_log_drafts_without_exemplars(preview):
    """A brand-new customer still drafts — and the prompt says so out loud. Unlike the tree, which
    omitted the block entirely, the notebook engine states that it has nothing to lean on; naming
    the absence is what stops the model inferring a voice it has never seen."""
    post, draft = preview
    assert not corrections_path().exists()

    status, body = post()
    assert status == 200 and body["disposition"] == "reply"
    assert "no close past reply on file" in _draft_user(draft)
    assert _EXEMPLAR_REPLY not in _draft_user(draft)


# ---- 3. unreadable log → say so loudly; never draft as if she taught Bean nothing -------------

def test_corrupt_correction_log_fails_loudly_instead_of_drafting_untaught(preview):
    post, draft = preview
    corrections_path().parent.mkdir(parents=True, exist_ok=True)
    corrections_path().write_text(_correction_line() + "\n{ this is not json\n", encoding="utf-8")

    status, body = post()
    assert status == 503, "a shredded correction log must not silently draft as an untaught Bean"
    assert "correction" in body["error"].lower()
    assert not draft.calls, "the model was called despite an unreadable correction log"


# ---- 4. the exemplar count is capped — the log grows forever, the prompt must not -------------

def test_exemplar_count_is_capped(preview):
    post, draft = preview
    corrections_path().parent.mkdir(parents=True, exist_ok=True)
    corrections_path().write_text(
        "".join(_correction_line(email_id=f"p{i}", final_text=f"exemplar reply number {i:02d}") + "\n"
                for i in range(12)),
        encoding="utf-8",
    )

    status, _ = post()
    assert status == 200
    user = _draft_user(draft)
    kept = [i for i in range(12) if f"exemplar reply number {i:02d}" in user]
    # bean/server.py::_run_engine asks the shelf for k=3. The log grows forever; the prompt must not.
    assert len(kept) == 3, f"expected a cap, {len(kept)} exemplars rode along"


# ---- 5. the corrupt-log error is the type the server maps to 503, not a bare ValueError -------

def test_load_raises_corrections_corrupt_error(tmp_path):
    from bean.corrections import load

    log = tmp_path / "corrections.jsonl"
    log.write_text("{ nope\n", encoding="utf-8")
    with pytest.raises(CorrectionsCorruptError) as exc:
        load(log)
    assert "corrections.jsonl" in str(exc.value)
    assert str(log) in str(exc.value)


def test_load_of_absent_log_is_empty_not_an_error(tmp_path):
    from bean.corrections import load

    assert load(tmp_path / "nothing.jsonl") == []
