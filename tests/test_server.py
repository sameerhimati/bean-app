"""Offline tests for the local backend (bean/server.py).

The Phase-2 server replaces the web admin's localStorage with real persistence + live preview.
Everything here is offline: config round-trips through a tmp file (never the repo's
bean-config.json), and /api/preview runs through an *injected* FakeModel-backed triage seam
(a notebook on disk plus a FakeModel-backed adapter) so no live model is ever called.
"""

from __future__ import annotations

import json
import os
import threading
import urllib.error
import urllib.request
from http.server import ThreadingHTTPServer
from pathlib import Path

import pytest

from bean import server as srv
from bean.config import Config, load_config
from bean.contract import Email
from bean.gate import GateResult
from bean.llm import FakeModel, OutOfCreditsError
from bean.notebook import Bucket, Notebook


# ---- the engine seam: a notebook on disk + a canned model, no network ------------------------
# The tree's preview seam was a single injected `triage_tree` callable. The notebook engine has no
# such single entry point — it reads a notebook off the volume and builds its own adapter — so the
# seam is these two constants instead. `_install_engine` is the default; pass `answer=` to script a
# different verdict, or `model=` to hand in a model that raises.

_CANNED_DRAFT = {"bucket": "Order Status", "draft": "Hi, it shipped via UPS.",
                 "citations": ["notebook:Order Status"], "confidence": "green", "why_unsure": []}


def _install_engine(monkeypatch, tmp_path, *, answer=None, model=None):
    nb_path = tmp_path / "notebook.md"
    Notebook("A store", buckets=[Bucket("Order Status", "cite the order and stop")]).save(nb_path)
    monkeypatch.setattr(srv, "NOTEBOOK_PATH", nb_path)
    fake = model if model is not None else FakeModel({"draft": answer or _CANNED_DRAFT})
    monkeypatch.setattr(srv, "ModelAdapter", lambda *a, **k: fake)
    return fake


# ---- a tiny live server in a background thread, pointed at a tmp config -----------------------

@pytest.fixture(autouse=True)
def _reset_rate_limits():
    """The rate-limit counters are module-level (per-process), so clear them around every test —
    otherwise a lockout from one test bleeds into the next (and into other files' auth tests)."""
    for structure in (srv._auth_failures, srv._auth_lockouts, srv._inbound_hits, srv._preview_hits):
        structure.clear()
    yield
    for structure in (srv._auth_failures, srv._auth_lockouts, srv._inbound_hits, srv._preview_hits):
        structure.clear()


@pytest.fixture
def base_url(tmp_path, monkeypatch):
    cfg_path = tmp_path / "bean-config.json"
    monkeypatch.setattr(srv, "CONFIG_PATH", cfg_path)
    monkeypatch.setattr(srv, "STATUS_PATH", tmp_path / "status.json")
    monkeypatch.setattr(srv, "CORRECTIONS_PATH", tmp_path / "corrections.jsonl")
    monkeypatch.setattr(srv, "NOTEBOOK_PATH", tmp_path / "notebook.md")
    monkeypatch.setattr(srv, "NOTEBOOK_REVIEW_PATH", tmp_path / "notebook_review.json")
    # Default the gate seam to pass-through so preview tests exercise the engine offline;
    # gate-specific tests override this with their own fake (see test_gate.py).
    monkeypatch.setattr(srv, "gate", lambda email, rules=None: GateResult("reply"))
    # ...and default the engine to a notebook on disk plus a canned model, so every endpoint that
    # drafts works without a network. Tests that care about the model override it via _engine().
    _install_engine(monkeypatch, tmp_path)

    httpd = ThreadingHTTPServer(("127.0.0.1", 0), srv.BeanHandler)
    t = threading.Thread(target=httpd.serve_forever, daemon=True)
    t.start()
    try:
        yield f"http://127.0.0.1:{httpd.server_address[1]}", cfg_path
    finally:
        httpd.shutdown()
        httpd.server_close()


def _req(url, method="GET", body=None, headers=None):
    data = json.dumps(body).encode() if body is not None else None
    hdrs = {"Content-Type": "application/json"} if data is not None else {}
    hdrs.update(headers or {})
    req = urllib.request.Request(url, data=data, method=method, headers=hdrs)
    try:
        with urllib.request.urlopen(req) as resp:
            return resp.status, json.loads(resp.read().decode() or "null")
    except urllib.error.HTTPError as e:
        return e.code, json.loads(e.read().decode() or "null")


def _get_etag(url):
    """The config version this 'client' loaded — echoed back as If-Match on its next save. The
    header is a quoted string (RFC 7232); an unquoted one is malformed and Cloudflare drops it."""
    with urllib.request.urlopen(f"{url}/api/config") as resp:
        raw = resp.headers.get("ETag")
    assert raw.startswith('"') and raw.endswith('"'), f"ETag must be quoted, got {raw!r}"
    return raw


def _put_config(url, body, etag):
    return _req(f"{url}/api/config", method="PUT", body=body, headers={"If-Match": etag or ""})


# ---- GET /api/config -------------------------------------------------------------------------

def test_get_config_falls_back_to_fixtures_when_absent(base_url):
    url, cfg_path = base_url
    assert not cfg_path.exists()
    status, body = _req(f"{url}/api/config")
    assert status == 200
    assert body == Config.from_fixtures().to_dict()


def test_get_config_returns_persisted_file(base_url):
    url, cfg_path = base_url
    custom = Config.from_fixtures().to_dict()
    custom["settings"]["shopifyStore"] = "round-trip-store"
    cfg_path.write_text(json.dumps(custom))
    status, body = _req(f"{url}/api/config")
    assert status == 200
    assert body == load_config(cfg_path).to_dict()
    assert body["settings"]["shopifyStore"] == "round-trip-store"


# ---- GET/PUT /api/notebook (the editable brain) ----------------------------------------------

def _seed_notebook(path):
    from bean.notebook import Bucket, Notebook
    Notebook("Sable & Wren", buckets=[
        Bucket("Not working / broken", "within ~90 days I replace", stakes="high"),
        Bucket("Product questions", "answer the question and stop"),
    ]).save(path)


def _notebook_etag(url):
    with urllib.request.urlopen(f"{url}/api/notebook") as resp:
        raw = resp.headers.get("ETag")
    assert raw and raw.startswith('"') and raw.endswith('"'), f"ETag must be quoted, got {raw!r}"
    return raw


def test_get_notebook_503_when_absent(base_url, monkeypatch, tmp_path):
    # No notebook on disk = not distilled/approved yet. A loud 503, never a fabricated default.
    # Points past the fixture's default notebook on purpose: absence is the thing under test.
    url, _ = base_url
    monkeypatch.setattr(srv, "NOTEBOOK_PATH", tmp_path / "nope" / "notebook.md")
    status, _ = _req(f"{url}/api/notebook")
    assert status == 503


def test_put_notebook_persists_stakes_edit_and_round_trips(base_url):
    # The approval/edit gate through the REAL entrypoint: flip a bucket's stakes and it sticks.
    url, cfg_path = base_url
    _seed_notebook(cfg_path.parent / "notebook.md")
    status, body = _req(f"{url}/api/notebook")
    assert status == 200
    assert body["buckets"][0]["stakes"] == "high"
    body["buckets"][1]["stakes"] = "high"  # the operator marks Product questions high-stakes too
    status, saved = _req(f"{url}/api/notebook", method="PUT", body=body,
                         headers={"If-Match": _notebook_etag(url)})
    assert status == 200
    _, got = _req(f"{url}/api/notebook")
    assert got["buckets"][1]["stakes"] == "high"


def test_put_notebook_requires_if_match(base_url):
    url, cfg_path = base_url
    _seed_notebook(cfg_path.parent / "notebook.md")
    status, _ = _req(f"{url}/api/notebook", method="PUT", body={"store": "S", "buckets": []})
    assert status == 428


def test_put_notebook_stale_etag_conflicts(base_url):
    url, cfg_path = base_url
    _seed_notebook(cfg_path.parent / "notebook.md")
    status, body = _req(f"{url}/api/notebook", method="PUT", body={"store": "S", "buckets": []},
                        headers={"If-Match": '"deadbeef"'})
    assert status == 409
    assert "notebook" in body  # hands back the current so the client can reload, not clobber


# ---- GET/PUT /api/notebook/review (the questionnaire resume state) ---------------------------

def test_notebook_review_starts_empty_then_round_trips(base_url):
    """The 45-card walk is done across sittings, so her answers-so-far must survive a reload. Absent
    file → {} (nothing saved yet, not an error); a PUT of her decisions comes back on the next GET."""
    url, _ = base_url
    assert _req(f"{url}/api/notebook/review") == (200, {})
    decisions = {"patched": {"bucket:0": "within 120 days I replace, no return"},
                 "dropped": {"macro:2": True}, "signoff": "hand it to me"}
    status, _ = _req(f"{url}/api/notebook/review", method="PUT", body=decisions)
    assert status == 200
    assert _req(f"{url}/api/notebook/review") == (200, decisions)


def test_notebook_review_put_replaces_not_merges(base_url):
    """It's a resume snapshot, not an event log — the client sends the whole decisions object each
    save, and the server stores exactly that (a card she un-drops must not linger from an old PUT)."""
    url, _ = base_url
    _req(f"{url}/api/notebook/review", method="PUT", body={"dropped": {"macro:2": True}})
    _req(f"{url}/api/notebook/review", method="PUT", body={"dropped": {}, "signoff": "x"})
    assert _req(f"{url}/api/notebook/review")[1] == {"dropped": {}, "signoff": "x"}


def test_approving_the_notebook_clears_the_review_progress(base_url, tmp_path):
    """Finishing the walk = one PUT /api/notebook (the approval). That's the moment the resume file
    has done its job, so it's deleted — reopening the walk later starts clean, not mid-old-session."""
    url, cfg_path = base_url
    _seed_notebook(cfg_path.parent / "notebook.md")
    _req(f"{url}/api/notebook/review", method="PUT", body={"signoff": "partial"})
    assert (tmp_path / "notebook_review.json").exists()

    _, body = _req(f"{url}/api/notebook")
    status, _ = _req(f"{url}/api/notebook", method="PUT", body=body,
                     headers={"If-Match": _notebook_etag(url)})
    assert status == 200
    assert not (tmp_path / "notebook_review.json").exists()
    assert _req(f"{url}/api/notebook/review") == (200, {})


def test_notebook_review_rejects_a_non_object(base_url):
    url, _ = base_url
    status, _ = _req(f"{url}/api/notebook/review", method="PUT", body=["not", "a", "dict"])
    assert status == 400


# ---- static routing (regressions from the first deploy) --------------------------------------

def _get_text(url):
    with urllib.request.urlopen(url) as resp:
        return resp.status, resp.read().decode()


def test_root_serves_the_app_not_a_directory_listing(base_url):
    # Bare "/" must serve Bean.html — not SimpleHTTPRequestHandler's directory listing of web/.
    url, _ = base_url
    status, body = _get_text(f"{url}/")
    assert status == 200
    assert "bean-root.jsx" in body
    assert "Directory listing" not in body


def test_bean_data_jsx_is_synthesized_when_absent(base_url, tmp_path, monkeypatch):
    # In a deploy the generated web/bean-data.jsx is gitignored/absent; the server must synthesize
    # window.CONFIG (from the live config) + an empty inbox so the app doesn't crash on mount.
    url, _ = base_url
    empty_web = tmp_path / "web-empty"
    empty_web.mkdir()
    monkeypatch.setattr(srv, "WEB_DIR", empty_web)  # read per-request at handler init
    status, body = _get_text(f"{url}/bean-data.jsx")
    assert status == 200
    assert "window.CONFIG" in body and "window.EMAILS = []" in body


# ---- GET /api/meta ---------------------------------------------------------------------------

def test_get_meta_returns_inbound_address_from_env(base_url, monkeypatch):
    url, _ = base_url
    monkeypatch.setenv("BEAN_INBOUND_ADDRESS", "support@in.bean.test")
    status, body = _req(f"{url}/api/meta")
    assert status == 200
    assert body == {"inbound_address": "support@in.bean.test"}


def test_get_meta_defaults_to_empty_address(base_url, monkeypatch):
    url, _ = base_url
    monkeypatch.delenv("BEAN_INBOUND_ADDRESS", raising=False)
    status, body = _req(f"{url}/api/meta")
    assert status == 200
    assert body == {"inbound_address": ""}


# ---- /api/status (per-email action state, server-side so it's one Bean across devices) --------

def test_get_status_is_empty_when_absent(base_url):
    url, _ = base_url
    status, body = _req(f"{url}/api/status")
    assert status == 200
    assert body == {}




# ---- PUT /api/config -------------------------------------------------------------------------

def test_put_config_persists_and_round_trips(base_url):
    url, cfg_path = base_url
    new = Config.from_fixtures().to_dict()
    new["settings"]["shopifyStore"] = "edited-store"
    new["categories"][0]["description"] = "edited by the operator"

    status, _ = _put_config(url, new, _get_etag(url))
    assert status == 200
    # persisted to disk and parseable as a Config
    assert cfg_path.exists()
    Config.from_dict(json.loads(cfg_path.read_text()))
    # a following GET returns it
    status, got = _req(f"{url}/api/config")
    assert status == 200
    assert got == Config.from_dict(new).to_dict()
    assert got["settings"]["shopifyStore"] == "edited-store"


def test_put_config_rejects_malformed_json(base_url):
    url, cfg_path = base_url
    req = urllib.request.Request(
        f"{url}/api/config", data=b"not json", method="PUT",
        headers={"Content-Type": "application/json", "If-Match": _get_etag(url)},
    )
    try:
        with urllib.request.urlopen(req) as resp:
            status = resp.status
    except urllib.error.HTTPError as e:
        status = e.code
    assert status == 400
    assert not cfg_path.exists()  # nothing written on bad input


def test_healthz_reports_when_mail_stopped_arriving(base_url, tmp_path, monkeypatch):
    """Silence is this product's real failure mode, and it used to look exactly like health.

    Inbound died at Postmark's test-mode cap on 2026-07-11 and healthz stayed green for two days —
    Bean was perfectly healthy and perfectly deaf. This is the counter that would have said so.
    """
    url, _ = base_url
    from datetime import datetime, timedelta, timezone

    from bean import paths

    inbox = tmp_path / "inbox.jsonl"
    marker = tmp_path / "last_inbound.txt"
    monkeypatch.setattr(paths, "inbox_path", lambda customer=None: inbox)
    monkeypatch.setattr(srv, "inbox_path", lambda customer=None: inbox)
    monkeypatch.setattr(srv, "last_inbound_path", lambda customer=None: marker)

    # An inbox that HAS received mail before, with no heartbeat: unexplained silence, so stale.
    # (A tenant with neither is a new store whose mail never started — covered in
    # test_healthz_and_limits, since "never began" and "stopped" are different alarms.)
    inbox.write_text('{"id": "e0"}\n', encoding="utf-8")
    _, body = _req(f"{url}/healthz")
    assert body["last_inbound_at"] is None
    assert body["inbound_stale"] is True

    # Mail lands — stamped by /api/inbound, which is the marker's only writer.
    marker.write_text(datetime.now(timezone.utc).isoformat(timespec="seconds"), encoding="utf-8")
    _, body = _req(f"{url}/healthz")
    assert body["inbound_stale"] is False
    assert body["hours_since_inbound"] < 1
    assert body["last_inbound_at"]

    # ...and then stops for two days, exactly as it did in prod. The heartbeat is the marker's
    # CONTENT, so this is a timestamp two days old — not a touched mtime, which any writer could fake.
    stale = datetime.now(timezone.utc) - timedelta(hours=49)
    marker.write_text(stale.isoformat(timespec="seconds"), encoding="utf-8")
    _, body = _req(f"{url}/healthz")
    assert body["inbound_stale"] is True
    assert 48 < body["hours_since_inbound"] < 50


def test_healthz_surfaces_approval_rate(base_url):
    """The north star, inspectable: of the drafts she acted on, the untouched share. Empty = 0.0."""
    from bean.corrections import Correction, record

    url, _ = base_url
    _, empty = _req(f"{url}/healthz")
    assert empty["approval_rate"] == 0.0  # nothing acted on yet — unmeasured, not perfect

    for i in range(3):
        record(Correction(f"e{i}", "Q", "green", "approve"), log_path=srv.CORRECTIONS_PATH)
    record(Correction("e3", "Q", "yellow", "edit", original_draft="a", final_text="a b c"), log_path=srv.CORRECTIONS_PATH)
    _, body = _req(f"{url}/healthz")
    assert body["approval_rate"] == 0.75  # 3 untouched of 4 acted-on
    assert body["approval_outcomes"] == {"approved_untouched": 3, "approved_edited": 1}


def test_stale_inbound_reports_but_does_not_fail_the_healthcheck(base_url, tmp_path, monkeypatch):
    """It must NOT 503. `ok` drives the status code and scripts/deploy.sh gates on it, so failing
    here would mean a quiet mailbox blocks every deploy and rolls back good code. The deploy gate
    asks "is the code I shipped serving?"; this asks "is the pipe open?" — different questions."""
    url, _ = base_url
    # An inbox that has received mail, but no heartbeat — the silent-mailbox condition.
    inbox = tmp_path / "went-quiet.jsonl"
    inbox.write_text('{"id": "e0"}\n', encoding="utf-8")
    monkeypatch.setattr(srv, "inbox_path", lambda customer=None: inbox)
    monkeypatch.setattr(srv, "last_inbound_path", lambda customer=None: tmp_path / "no-heartbeat.txt")

    # Give it a real tenant config first, so the ONLY unhealthy-looking thing is the silent mailbox.
    # (Without one it serves the demo default, which fails the check for its own good reasons.)
    cfg = Config.from_fixtures().to_dict()
    cfg["categories"][0]["name"] = "Not The Demo Store"
    assert _put_config(url, cfg, _get_etag(url))[0] == 200

    status, body = _req(f"{url}/healthz")
    assert body["config_is_demo_default"] is False
    assert body["inbound_stale"] is True
    assert status == 200, "a silent mailbox must not 503 — that would roll back deploys"


# ---- POST /api/correction: the (question, answer) corpus is stored in FULL ---------------------

def test_teaching_a_real_email_stores_the_complete_question_and_answer(base_url, tmp_path):
    """A teach exemplar is the durable (email -> reply) pair — the corpus for future fine-tuning/RAG,
    independent of few-shot's prompt use. The FULL email (subject + body) is the question and the FULL
    reply is the answer; nothing here may be truncated (few_shot_examples truncates for the PROMPT
    only — never the stored row). This asserts it through the REAL POST the browser makes."""
    from bean.corrections import load

    url, _ = base_url
    # Longer than few-shot's 400-char prompt cap, so any accidental truncation would show.
    question = "I ordered the Aspen dining table last week. " + ("Details follow. " * 60)
    answer = "Hi Priya, thanks for reaching out. " + ("Here is the full explanation. " * 60)
    payload = {
        "email_id": "tw-teach-1", "category": "Meter", "confidence": "flag", "action": "teach",
        "final_text": answer,
        "meta": {"model_category": "General", "email_subject": "Aspen dining table question",
                 "email_body": question},
    }
    status, body = _req(f"{url}/api/correction", method="POST", body=payload)
    assert status == 200 and body["ok"] is True

    rows = load(tmp_path / "corrections.jsonl")
    assert len(rows) == 1
    row = rows[0]
    assert row.action == "teach"
    # FULL answer, verbatim and untruncated
    assert row.final_text == answer and len(row.final_text) > 400
    # FULL question — both subject and body — verbatim and untruncated
    assert row.meta["email_subject"] == "Aspen dining table question"
    assert row.meta["email_body"] == question and len(row.meta["email_body"]) > 400
    # meta is a free dict and rides through intact — the relabel signal is what depends on it
    assert row.meta["model_category"] == "General"


# ---- PUT /api/config: optimistic concurrency (the lost-update bug) ----------------------------

def test_put_config_without_if_match_is_refused(base_url):
    """A save that doesn't name the version it edited can't be safe — it would blindly replace."""
    url, cfg_path = base_url
    status, body = _req(f"{url}/api/config", method="PUT", body=Config.from_fixtures().to_dict())
    assert status == 428
    assert not cfg_path.exists()  # nothing written
    assert body["etag"]  # the server tells the client which version it should have matched


def test_put_config_with_stale_etag_is_rejected_and_disk_untouched(base_url):
    """The real incident: an import saved, then a tab that loaded BEFORE it saved on top and the
    server answered 200, silently destroying the import. Now the stale writer loses, not the work."""
    url, cfg_path = base_url

    # both "clients" load the same version
    tab_etag = _get_etag(url)
    script_etag = _get_etag(url)
    assert tab_etag == script_etag

    # the script saves first, adding a branch
    from_script = Config.from_fixtures().to_dict()
    from_script["settings"]["shopifyStore"] = "written-by-the-script"
    assert _put_config(url, from_script, script_etag)[0] == 200

    # the stale tab now saves its own copy — this is the write that used to win
    from_tab = Config.from_fixtures().to_dict()
    from_tab["settings"]["shopifyStore"] = "written-by-the-stale-tab"
    status, body = _put_config(url, from_tab, tab_etag)
    assert status == 409
    assert "stale" in body["error"]

    # the script's work survived, on disk and over the wire
    on_disk = json.loads(cfg_path.read_text())
    assert on_disk["settings"]["shopifyStore"] == "written-by-the-script"
    _, got = _req(f"{url}/api/config")
    assert got["settings"]["shopifyStore"] == "written-by-the-script"
    # and the 409 hands back the current config + etag so the client can recover without a reload
    assert body["config"]["settings"]["shopifyStore"] == "written-by-the-script"
    assert body["etag"] == _get_etag(url).strip('"')  # body carries the bare hash


def test_put_config_etag_advances_so_the_same_client_can_save_twice(base_url):
    """The saver adopts the ETag it gets back (to_dict normalizes what is actually persisted), so a
    second save from the same tab succeeds rather than 409-ing against its own write."""
    url, _ = base_url
    cfg = Config.from_fixtures().to_dict()
    with urllib.request.urlopen(f"{url}/api/config") as resp:
        etag1 = resp.headers.get("ETag")

    req = urllib.request.Request(
        f"{url}/api/config", data=json.dumps(cfg).encode(), method="PUT",
        headers={"Content-Type": "application/json", "If-Match": etag1},
    )
    with urllib.request.urlopen(req) as resp:
        assert resp.status == 200
        etag2 = resp.headers.get("ETag")

    assert etag2 and etag2 == _get_etag(url)  # quoted, and matches a fresh GET
    # saving again with the returned etag works; with the old one it does not
    cfg["settings"]["shopifyStore"] = "second-save"
    assert _put_config(url, cfg, etag2)[0] == 200
    assert _put_config(url, cfg, etag1)[0] == 409


# ---- POST /api/preview (the notebook engine, offline) ----------------------------------------

def test_preview_runs_the_engine_and_returns_a_draft_result(base_url):
    url, _ = base_url
    payload = {
        "email": {
            "id": "mm-1043", "sender_name": "Devin", "sender_email": "d@example.com",
            "subject": "Where is my order?", "body": "Order MM-1043, where is it?",
        }
    }
    status, body = _req(f"{url}/api/preview", method="POST", body=payload)
    assert status == 200
    assert body["bucket"] == "Order Status"
    assert body["confidence"] in ("green", "yellow", "red")
    assert "draft" in body


def test_preview_rejects_missing_email(base_url):
    url, _ = base_url
    status, _ = _req(f"{url}/api/preview", method="POST", body={"nope": 1})
    assert status == 400


def test_preview_402s_when_out_of_credits(base_url, monkeypatch, tmp_path):
    # Preview spends tokens (gate + draft); a dry balance previously dropped the connection with no
    # status (read as "is the server running?"). Now it's a clean 402 the UI can branch on.
    url, _ = base_url

    class _Dry:
        def structured(self, *a, **k):
            raise OutOfCreditsError("credit balance is too low")

    _install_engine(monkeypatch, tmp_path, model=_Dry())
    payload = {"email": {
        "id": "ooc-1", "sender_name": "Devin", "sender_email": "d@example.com",
        "subject": "Where is my order?", "body": "Order MM-1043, where is it?",
    }}
    status, body = _req(f"{url}/api/preview", method="POST", body=payload)
    assert status == 402
    assert body["error_code"] == "out_of_credits"


# ---- POST /api/correction (the learning-signal write path) -----------------------------------

@pytest.fixture
def base_url_with_log(tmp_path, monkeypatch):
    """Like base_url, but also redirects the correction log to a tmp file."""
    cfg_path = tmp_path / "bean-config.json"
    log_path = tmp_path / "corrections.jsonl"
    monkeypatch.setattr(srv, "CONFIG_PATH", cfg_path)
    monkeypatch.setattr(srv, "CORRECTIONS_PATH", log_path)
    httpd = ThreadingHTTPServer(("127.0.0.1", 0), srv.BeanHandler)
    t = threading.Thread(target=httpd.serve_forever, daemon=True)
    t.start()
    try:
        yield f"http://127.0.0.1:{httpd.server_address[1]}", log_path
    finally:
        httpd.shutdown()
        httpd.server_close()


def test_correction_records_to_log(base_url_with_log):
    url, log_path = base_url_with_log
    status, body = _req(f"{url}/api/correction", method="POST", body={
        "email_id": "mm-sage", "category": "Product Question", "confidence": "flag",
        "action": "approve",
    })
    assert status == 200 and body["ok"] is True
    rows = [json.loads(line) for line in log_path.read_text().splitlines() if line.strip()]
    assert len(rows) == 1 and rows[0]["email_id"] == "mm-sage" and rows[0]["action"] == "approve"


def test_correction_tags_substantive_edit(base_url_with_log):
    # A recategorize-or-rewrite that changes a fact must be tagged substantive (feeds calibration).
    url, log_path = base_url_with_log
    status, body = _req(f"{url}/api/correction", method="POST", body={
        "email_id": "mm-0998", "category": "Returns & Exchanges", "confidence": "low",
        "action": "edit",
        "original_draft": "Hi Greg, happy to help — your return is free and in stock.",
        "final_text": "Hi Greg, we cannot take worn boots back.",
    })
    assert status == 200 and body["edit_kind"] == "substantive"


def test_approve_persists_reply_text_and_email_context(base_url_with_log):
    # An approve now captures the (email -> good reply) pair few-shot learns from: the accepted
    # draft as final_text, and the email context + the model's original bucket in meta.
    url, log_path = base_url_with_log
    status, body = _req(f"{url}/api/correction", method="POST", body={
        "email_id": "mm-1043", "category": "Order Status", "confidence": "high",
        "action": "approve", "final_text": "Hi Devin, it shipped via UPS yesterday.",
        "meta": {"model_category": "Order Status", "email_subject": "where is my order",
                 "email_body": "ordered last week"},
    })
    assert status == 200 and body["ok"] is True
    row = json.loads(log_path.read_text().splitlines()[0])
    assert row["final_text"] == "Hi Devin, it shipped via UPS yesterday."
    assert row["meta"]["email_subject"] == "where is my order"


def test_should_file_records_the_gate_false_positive_with_her_note(base_url_with_log):
    # The mirror of `misfile`: Bean DREW a reply, she says it should have been filed. The action is
    # new but the endpoint is not — `action` is an unvalidated str, so the write path needs no
    # change. What must survive the round-trip is the labelled pair (her verdict as `category`, Bean's
    # bucket as meta.model_category) plus the note that had nowhere to go before this gesture existed.
    url, log_path = base_url_with_log
    status, body = _req(f"{url}/api/correction", method="POST", body={
        "email_id": "3851152d", "category": "Filed / FYI", "confidence": "flag",
        "action": "should-file",
        "note": "This should be in FYI/Filed as this was sent as a follow up to an original email",
        "meta": {"model_category": "Needs a human", "email_subject": "Re: Auto: Inventory Report"},
    })
    assert status == 200 and body["ok"] is True
    row = json.loads(log_path.read_text().splitlines()[0])
    assert row["action"] == "should-file"
    assert row["note"].startswith("This should be in FYI/Filed")
    # category != model_category is what makes it machine-readable as a gate error.
    assert row["category"] == "Filed / FYI" and row["meta"]["model_category"] == "Needs a human"


def test_should_file_stays_out_of_the_approval_rate_denominator(base_url_with_log):
    # She's rejecting the gate's decision to route it to her at all — not grading the draft. Counting
    # it as a draft outcome would make the gate's errors look like Bean writing bad replies.
    from bean.corrections import Correction, outcome_of
    assert outcome_of(Correction(email_id="x", category="Filed / FYI", confidence="flag",
                                 action="should-file")) is None


def test_recategorize_is_captured_orthogonally_without_a_new_action(base_url_with_log):
    # The operator relabels the bucket while approving. The relabel is detectable as category !=
    # meta.model_category — no dedicated 'recategorize' action, no schema change.
    url, log_path = base_url_with_log
    status, _ = _req(f"{url}/api/correction", method="POST", body={
        "email_id": "mm-sage", "category": "Returns & Exchanges", "confidence": "low",
        "action": "approve", "final_text": "...",
        "meta": {"model_category": "Product Question"},
    })
    assert status == 200
    row = json.loads(log_path.read_text().splitlines()[0])
    assert row["category"] == "Returns & Exchanges"
    assert row["meta"]["model_category"] == "Product Question"
    assert row["category"] != row["meta"]["model_category"]  # this difference IS the relabel signal


def test_gate_corrections_record_the_filing_signal(base_url_with_log):
    # The gate's ONLY learning signal: a mis-file ("this needs a reply") and a confirmed keep.
    # Both ride the existing correction log (new action values, no schema change) — pin their shape
    # so a UI refactor can't silently drop the gate's calibration data.
    url, log_path = base_url_with_log
    status, _ = _req(f"{url}/api/correction", method="POST", body={
        "email_id": "paste-123", "action": "misfile",
        "note": "the operator says this needs a reply; gate filed it as newsletter.",
    })
    assert status == 200
    status, _ = _req(f"{url}/api/correction", method="POST", body={
        "email_id": "paste-456", "action": "keep-filed", "note": "A supplier blast.",
    })
    assert status == 200
    rows = [json.loads(line) for line in log_path.read_text().splitlines() if line.strip()]
    by_id = {r["email_id"]: r for r in rows}
    assert by_id["paste-123"]["action"] == "misfile" and "needs a reply" in by_id["paste-123"]["note"]
    assert by_id["paste-456"]["action"] == "keep-filed"


def test_correction_rejects_missing_fields(base_url_with_log):
    url, log_path = base_url_with_log
    status, _ = _req(f"{url}/api/correction", method="POST", body={"action": "approve"})
    assert status == 400
    assert not log_path.exists()  # nothing written on bad input


# ---- GET /api/learning (the read surface over the correction log) ----------------------------

def test_learning_aggregates_the_correction_log_per_category(base_url_with_log):
    from bean.corrections import Correction, record

    url, log_path = base_url_with_log
    # Order Status: an approve (liked, few-shot-usable via meta), a substantive edit, a cosmetic edit.
    record(Correction(
        email_id="os-1", category="Order Status", confidence="high", action="approve",
        final_text="Hi Devin, it shipped via UPS.", liked=True,
        meta={"email_subject": "where is my order", "email_body": "ordered last week"},
    ), log_path=log_path)
    record(Correction(
        email_id="os-2", category="Order Status", confidence="low", action="edit",
        original_draft="Hi Greg, your refund is on the way.",
        final_text="Hi Greg, we cannot refund a used item.", note="policy",
    ), log_path=log_path)
    record(Correction(
        email_id="os-3", category="Order Status", confidence="high", action="edit",
        original_draft="Hi, it shipped yesterday.", final_text="Hi there, it shipped yesterday.",
    ), log_path=log_path)
    # Returns & Exchanges: a takeover + a skip (no edits, no exemplars).
    record(Correction(email_id="re-1", category="Returns & Exchanges", confidence="flag", action="takeover"),
           log_path=log_path)
    record(Correction(email_id="re-2", category="Returns & Exchanges", confidence="low", action="skip"),
           log_path=log_path)

    status, body = _req(f"{url}/api/learning")
    assert status == 200

    os_ = body["Order Status"]
    assert os_["actions"] == {"approve": 1, "edit": 2}
    assert os_["liked"] == 1 and os_["noted"] == 1
    assert os_["substantive_edits"] == 1  # only os-2 changed content; os-3 was cosmetic
    assert os_["few_shot_exemplars"] == 1  # only os-1 carries email context
    sent = os_["drafted_vs_sent"]
    assert len(sent) == 2  # both edit rows carry original + final
    assert {"original_draft": "Hi Greg, your refund is on the way.",
            "final_text": "Hi Greg, we cannot refund a used item."} in sent

    re_ = body["Returns & Exchanges"]
    assert re_["actions"] == {"takeover": 1, "skip": 1}
    assert re_["substantive_edits"] == 0 and re_["few_shot_exemplars"] == 0
    assert re_["drafted_vs_sent"] == []


def test_learning_is_empty_when_no_corrections(base_url_with_log):
    url, _ = base_url_with_log
    status, body = _req(f"{url}/api/learning")
    assert status == 200 and body == {}


# ---- the gate seam is actually wired to config.gate (not just the pass-through stub) ----------

def test_config_gate_rules_file_a_matching_email_through_the_real_gate(base_url, monkeypatch):
    # base_url stubs srv.gate to pass-through; restore the REAL gate here. An alwaysFile rule that
    # matches the sender short-circuits before any model call (rule, not classifier), so this is
    # offline — and it proves config.gate → gate → filed disposition is really wired.
    from bean.gate import needs_reply as real_gate
    monkeypatch.setattr(srv, "gate", real_gate)
    url, _ = base_url
    override = Config.from_fixtures().to_dict()
    override["gate"] = {"alwaysFile": ["supplierweekly.com"]}
    payload = {
        "email": {"id": "n-1", "sender_name": "Supplier Weekly",
                  "sender_email": "news@supplierweekly.com", "subject": "deals", "body": "trends"},
        "config": override,
    }
    status, body = _req(f"{url}/api/preview", method="POST", body=payload)
    assert status == 200
    assert body["disposition"] == "filed" and body["kind"] == "rule"


# ---- static file serving ---------------------------------------------------------------------

def test_serves_static_web_file(base_url):
    url, _ = base_url
    req = urllib.request.Request(f"{url}/Bean.html")
    with urllib.request.urlopen(req) as resp:
        assert resp.status == 200
        head = resp.read(200).decode("utf-8", "replace").lower()
    assert "<" in head  # served some HTML, not JSON


def test_put_config_accepts_the_weak_etag_a_proxy_may_return(base_url):
    """Cloudflare re-encodes the body and can hand the browser W/"abc" instead of "abc". Both name
    the same version, so both must save — otherwise every save through the CDN 409s forever."""
    url, _ = base_url
    etag = _get_etag(url).strip('"')
    cfg = Config.from_fixtures().to_dict()
    status, _ = _req(f"{url}/api/config", method="PUT", body=cfg,
                     headers={"If-Match": f'W/"{etag}"'})
    assert status == 200


# ---- security: the passcode gate fails CLOSED in production (issue #1) ------------------------
# Today a missing BEAN_PASSCODE fails OPEN — fine for dev, a silent breach in prod (every customer
# email body served to the open internet). BEAN_ENV=production flips it to fail-closed.

def test_gate_fails_closed_in_prod_but_open_in_dev(base_url, tmp_path, monkeypatch):
    url, _ = base_url
    monkeypatch.setenv("BEAN_DATA_DIR", str(tmp_path))  # never read real mail from /api/inbox
    monkeypatch.delenv("BEAN_PASSCODE", raising=False)
    # dev (BEAN_ENV unset): the gate stays OFF and the inbox serves — today's behavior preserved.
    monkeypatch.delenv("BEAN_ENV", raising=False)
    assert _req(f"{url}/api/inbox")[0] == 200
    # production with no passcode: fail CLOSED (503), never serve customer mail unauthenticated.
    monkeypatch.setenv("BEAN_ENV", "production")
    status, body = _req(f"{url}/api/inbox")
    assert status == 503
    assert "error" in body


def test_inbound_fails_closed_in_prod_without_token(base_url, tmp_path, monkeypatch):
    url, _ = base_url
    monkeypatch.setenv("BEAN_DATA_DIR", str(tmp_path))
    monkeypatch.delenv("BEAN_INBOUND_TOKEN", raising=False)  # the fixture's engine never runs
    monkeypatch.setenv("BEAN_ENV", "production")
    status, _ = _req(f"{url}/api/inbound", method="POST", body={"From": "a@b.c"})
    assert status == 503  # an unset token in prod must refuse, not run Anthropic calls for free


# ---- security: client image paths are confined to the data root (issue #2) -------------------

def test_preview_rejects_image_path_outside_data_root(base_url, tmp_path, monkeypatch):
    url, _ = base_url
    monkeypatch.setenv("BEAN_DATA_DIR", str(tmp_path))
    # A hand-crafted path outside the data root would base64 a local secret into a model call.
    payload = {"email": {"id": "evil", "subject": "s", "body": "b",
                         "image_paths": ["/etc/passwd"]}}
    status, _ = _req(f"{url}/api/preview", method="POST", body=payload)
    assert status == 400


def test_preview_missing_in_root_image_is_400_not_500(base_url, tmp_path, monkeypatch):
    # A path INSIDE the root passes confinement but, if it doesn't exist, read_bytes() raises
    # FileNotFoundError deep in the model call — that's a bad request (400), not a server error (500).
    url, _ = base_url
    monkeypatch.setenv("BEAN_DATA_DIR", str(tmp_path))

    class _Reading:
        def structured(self, *a, images=None, **k):
            for path in images or []:
                Path(path).read_bytes()  # exactly what bean/llm.py:_image_block does
            return dict(_CANNED_DRAFT)

    _install_engine(monkeypatch, tmp_path, model=_Reading())
    inside = tmp_path / "tenant" / "missing.png"
    payload = {"email": {"id": "m", "subject": "s", "body": "b", "image_paths": [str(inside)]}}
    status, _ = _req(f"{url}/api/preview", method="POST", body=payload)
    assert status == 400


# ---- security: request bodies are capped (issue #3) ------------------------------------------

def test_oversized_request_body_is_413(base_url, monkeypatch):
    # Cap tiny so a small over-cap body is enough to prove the header-based rejection (the real 1 MB
    # cap would need a >1 MB upload, whose reset-before-drain confuses the client, not the point here).
    url, _ = base_url
    monkeypatch.setattr(srv, "_MAX_BODY", 50)  # the fixture's engine guards a revert; never reached
    payload = {"email": {"id": "big", "subject": "s", "body": "x" * 200}}
    status, _ = _req(f"{url}/api/preview", method="POST", body=payload)
    assert status == 413


def test_inbound_accepts_a_large_body_under_the_webhook_cap(base_url, tmp_path, monkeypatch):
    # A Postmark payload with base64 attachments is legitimately large; the webhook cap is generous
    # (35 MB) precisely so a real customer email over the 1 MB generic cap is never dropped. The
    # generic cap is set tiny here to PROVE inbound uses its own larger ceiling, not the generic one.
    url, _ = base_url
    monkeypatch.setenv("BEAN_DATA_DIR", str(tmp_path))
    monkeypatch.setattr(srv, "_MAX_BODY", 50)  # would 413 this payload on any other endpoint
    payload = {"FromFull": {"Email": "c@example.com", "Name": "C"}, "From": "c@example.com",
               "Subject": "photos", "TextBody": "x" * 5000, "MessageID": "big-1"}
    status, _ = _req(f"{url}/api/inbound", method="POST", body=payload)
    assert status == 200  # accepted under the webhook cap, NOT 413


# ---- resilience: a model failure on a live path refuses cleanly, never crashes the thread --------
# (Anthropic 529/overload, a connection error, or a forced-tool-decline ValueError.) preview → 502
# so the card shows a retry; inbound → 503 so Postmark retries and the mail is delayed, never dropped.

class _RaisingModel:
    """A model whose call always fails — an Anthropic 529, a connection drop, a tool decline."""
    def structured(self, *a, **k):
        raise RuntimeError("the model is having a moment")


def _raise_model_error(*a, **k):
    raise RuntimeError("Anthropic 529 overloaded")


def test_preview_502s_when_the_gate_model_fails(base_url, monkeypatch):
    url, _ = base_url
    monkeypatch.setattr(srv, "gate", _raise_model_error)
    status, _ = _req(f"{url}/api/preview", method="POST",
                     body={"email": {"id": "e", "subject": "s", "body": "b"}})
    assert status == 502  # not a 500 / dropped socket


def test_preview_502s_when_the_draft_model_fails(base_url, monkeypatch, tmp_path):
    url, _ = base_url
    _install_engine(monkeypatch, tmp_path, model=_RaisingModel())
    status, _ = _req(f"{url}/api/preview", method="POST",
                     body={"email": {"id": "e", "subject": "s", "body": "b"}})
    assert status == 502


def test_preview_400s_on_a_non_dict_email(base_url):
    # {"email": "oops"} makes .get() raise AttributeError inside _email_from_payload — a malformed
    # body (400), not a server fault (500). Guards the widened payload catch.
    url, _ = base_url
    status, _ = _req(f"{url}/api/preview", method="POST", body={"email": "oops"})
    assert status == 400


def test_inbound_503s_when_the_gate_model_fails(base_url, tmp_path, monkeypatch):
    url, _ = base_url
    monkeypatch.setenv("BEAN_DATA_DIR", str(tmp_path))
    monkeypatch.setattr(srv, "gate", _raise_model_error)
    payload = {"FromFull": {"Email": "c@example.com", "Name": "C"}, "From": "c@example.com",
               "Subject": "help", "TextBody": "my order", "MessageID": "m-1"}
    status, _ = _req(f"{url}/api/inbound", method="POST", body=payload)
    assert status == 503  # delayed (Postmark retries), never a crashed webhook thread


def test_inbound_503s_when_the_draft_model_fails(base_url, tmp_path, monkeypatch):
    url, _ = base_url
    monkeypatch.setenv("BEAN_DATA_DIR", str(tmp_path))
    _install_engine(monkeypatch, tmp_path, model=_RaisingModel())
    payload = {"FromFull": {"Email": "c@example.com", "Name": "C"}, "From": "c@example.com",
               "Subject": "help", "TextBody": "my order", "MessageID": "m-2"}
    status, _ = _req(f"{url}/api/inbound", method="POST", body=payload)
    assert status == 503


# ---- integrity: POST /api/status upserts one id (issue #4, the lost-update fix) ---------------

def test_post_status_upsert_merges_without_clobbering_other_ids(base_url):
    """The phone-approves-A / laptop-approves-B race: a per-id upsert reads the current map and sets
    one key, so B's write can never revert A the way a whole-map PUT would."""
    url, _ = base_url
    assert _req(f"{url}/api/status", method="POST", body={"id": "em-A", "state": "approved"})[0] == 200
    status, body = _req(f"{url}/api/status", method="POST", body={"id": "em-B", "state": "approved"})
    assert status == 200
    assert body == {"em-A": "approved", "em-B": "approved"}  # A survived a second device's write
    # a fresh GET (the other device) agrees
    assert _req(f"{url}/api/status")[1] == {"em-A": "approved", "em-B": "approved"}


def test_post_status_empty_state_deletes_the_key(base_url):
    url, _ = base_url
    _req(f"{url}/api/status", method="POST", body={"id": "em-A", "state": "approved"})
    status, body = _req(f"{url}/api/status", method="POST", body={"id": "em-A", "state": ""})
    assert status == 200 and body == {}  # empty state = undo/clear


# ---- POST /api/clear-filed deletes FYI mail through the real route, never a customer email --------

def test_clear_filed_removes_fyi_and_keeps_customer_mail(base_url, tmp_path, monkeypatch):
    """The Clear-FYI button hits this route. Through the real handler: filed lines go, walked customer
    lines stay, and the response reports honest counts. inbox_path resolves BEAN_DATA_DIR live, so
    pointing it at tmp isolates the write from real data."""
    url, _ = base_url
    monkeypatch.setenv("BEAN_DATA_DIR", str(tmp_path))
    from bean.paths import inbox_path
    p = inbox_path(srv.CUSTOMER)
    p.parent.mkdir(parents=True, exist_ok=True)
    walked = {"id": "c1", "result": {"confidence": "flag", "chunks": [{"node_type": "escalate"}]}}
    filed = {"id": "f1", "result": {"disposition": "filed", "kind": "promo"}}
    p.write_text(json.dumps(walked) + "\n" + json.dumps(filed) + "\n", encoding="utf-8")

    status, body = _req(f"{url}/api/clear-filed", method="POST", body={})
    assert status == 200
    assert body == {"ok": True, "removed": 1, "kept": 1}
    remaining = [json.loads(l)["id"] for l in p.read_text().splitlines() if l.strip()]
    assert remaining == ["c1"]  # the customer email is untouched; only the filed promo is gone


def test_post_status_rejects_a_missing_id(base_url):
    url, _ = base_url
    assert _req(f"{url}/api/status", method="POST", body={"state": "approved"})[0] == 400


# ---- integrity: the config PUT serializes concurrent saves (issue #5, the TOCTOU) -------------

def test_config_put_lock_serializes_concurrent_saves(base_url, monkeypatch):
    """Two threads reading the same ETag must not both pass If-Match and both write (last-writer-wins
    with both getting 200). A widened read window makes the race deterministic: with the lock exactly
    one save wins and the rest 409; without it, all of them would."""
    url, _ = base_url
    real_load = srv.load_config

    def slow_load(path):
        import time as _t
        _t.sleep(0.05)  # widen the read→compare→write window so all threads overlap
        return real_load(path)

    monkeypatch.setattr(srv, "load_config", slow_load)
    etag = _get_etag(url)
    results: list[int] = []

    def save(i):
        cfg = Config.from_fixtures().to_dict()
        cfg["settings"]["shopifyStore"] = f"writer-{i}"
        results.append(_put_config(url, cfg, etag)[0])

    threads = [threading.Thread(target=save, args=(i,)) for i in range(5)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert results.count(200) == 1  # exactly one winner
    assert results.count(409) == 4  # the rest lose against the winner's new version


# ---- security: brute-force + spend guards (issue #6) -----------------------------------------

def test_auth_locks_out_after_repeated_failures(base_url, monkeypatch):
    url, _ = base_url
    monkeypatch.setenv("BEAN_PASSCODE", "letmein")
    # The threshold of wrong tries are each an ordinary 401 ...
    for _ in range(srv._AUTH_MAX_FAILURES):
        assert _req(f"{url}/api/auth", method="POST", body={"passcode": "nope"})[0] == 401
    # ... then the IP is locked out (429), and even the CORRECT passcode is refused during lockout.
    assert _req(f"{url}/api/auth", method="POST", body={"passcode": "nope"})[0] == 429
    assert _req(f"{url}/api/auth", method="POST", body={"passcode": "letmein"})[0] == 429


def test_inbound_rate_limit_returns_429(base_url, tmp_path, monkeypatch):
    url, _ = base_url
    monkeypatch.setenv("BEAN_DATA_DIR", str(tmp_path))
    monkeypatch.setattr(srv, "_INBOUND_MAX_PER_MIN", 0)  # any inbound trips the ceiling
    status, _ = _req(f"{url}/api/inbound", method="POST", body={"From": "a@b.c"})
    assert status == 429  # Postmark retries on non-2xx, so this delays the mail, never drops it


# ---- a corrupt config must 503, never impersonate her with fixtures --------------------------

def test_corrupt_config_503s_instead_of_serving_fixtures(base_url):
    """The worst failure in the repo: `write_text` truncated the config before writing, so a
    redeploy mid-write left an empty file — and load_config answered that with demo fixtures, which
    the next save would write back over her real templates. Every read path must now refuse loudly.
    """
    url, cfg_path = base_url
    cfg_path.write_text("{ this is not json")           # a torn write, no .bak beside it
    assert not cfg_path.with_suffix(".json.bak").exists()

    status, _ = _req(f"{url}/api/config")
    assert status == 503, "GET /api/config served something instead of refusing"

    # preview must not triage a pasted email against a stranger's templates either
    status, _ = _req(f"{url}/api/preview", method="POST",
                     body={"email": {"id": "e1", "sender_name": "A", "sender_email": "a@b.com",
                                     "subject": "s", "body": "b"}})
    assert status == 503

    # a PUT must not "win" against a config it could not read
    status, _ = _req(f"{url}/api/config", method="PUT", body=Config.from_fixtures().to_dict(),
                     headers={"If-Match": '"whatever"'})
    assert status == 503
    assert cfg_path.read_text() == "{ this is not json"  # untouched — nothing overwrote her file


def test_corrupt_config_recovers_from_the_rolling_backup(base_url):
    """write_json_atomic leaves a .bak on every save. A torn live file must be answered from it —
    loudly — rather than by refusing or by inventing fixtures."""
    url, cfg_path = base_url
    good = Config.from_fixtures().to_dict()
    good["settings"]["shopifyStore"] = "recovered-from-backup"
    cfg_path.with_suffix(".json.bak").write_text(json.dumps(good))
    cfg_path.write_text("")                              # zero-byte: the exact truncation failure

    status, body = _req(f"{url}/api/config")
    assert status == 200
    assert body["settings"]["shopifyStore"] == "recovered-from-backup"


def test_synthesized_bean_data_jsx_503s_on_a_corrupt_config(base_url, tmp_path, monkeypatch):
    """The synthesized bean-data.jsx bakes window.CONFIG into the page. On a corrupt config it must
    refuse, not hand the browser a fixtures config that the next save would persist as hers."""
    url, cfg_path = base_url
    empty_web = tmp_path / "web-empty"
    empty_web.mkdir()
    monkeypatch.setattr(srv, "WEB_DIR", empty_web)   # force the synth path, as the sibling test does
    cfg_path.write_text("")                           # zero-byte torn write, no backup

    req = urllib.request.Request(f"{url}/bean-data.jsx")
    try:
        with urllib.request.urlopen(req) as resp:
            status = resp.status
    except urllib.error.HTTPError as e:
        status = e.code
    assert status == 503


def test_put_status_is_gone_so_the_whole_map_replace_cannot_come_back(base_url):
    """The per-id upsert removed the lost-update surface. Leaving the whole-map PUT alive would let
    any caller reintroduce it (phone's map overwrites laptop's). It must simply not exist."""
    url, _ = base_url
    status, _ = _req(f"{url}/api/status", method="PUT", body={"em-1": "approved"})
    assert status == 404


def test_corrupt_status_file_warns_instead_of_resetting_in_silence(base_url, caplog):
    """A torn status.json silently wiped every 'handled' mark. Degrading to {} is acceptable — it's
    derivable state with no backup — but doing it without a log line is not."""
    url, cfg_path = base_url
    (cfg_path.parent / "status.json").write_text("{ not json")
    with caplog.at_level("WARNING", logger="bean.server"):
        status, body = _req(f"{url}/api/status")
    assert status == 200 and body == {}
    assert any("unreadable" in r.message for r in caplog.records), "reset the action map in silence"


def test_corrections_endpoint_lists_rows_and_flags_exemplars(base_url, tmp_path, monkeypatch):
    """The operator's window on the learning log: newest-first rows, each marked exemplar (fed the
    loop) vs takeover (teaches nothing) — the exact distinction between 'wired' and 'fed'."""
    from bean import corrections as corr
    log = tmp_path / "corrections.jsonl"
    monkeypatch.setattr(srv, "CORRECTIONS_PATH", log)
    # an edit carrying her reply + the email it answers → a usable few-shot exemplar
    corr.record(corr.Correction(
        email_id="e1", category="Returns", confidence="flag", action="edit",
        original_draft="draft", final_text="Here's how returns work.",
        meta={"email_subject": "return?", "email_body": "how do I return"}), log_path=log)
    # a takeover with no final_text → Bean never saw what she sent, teaches nothing
    corr.record(corr.Correction(
        email_id="e2", category="General", confidence="flag", action="takeover"), log_path=log)

    url, _ = base_url
    status, body = _req(f"{url}/api/corrections")
    assert status == 200
    assert body["count"] == 2 and body["exemplars"] == 1
    rows = body["corrections"]
    assert rows[0]["email_id"] == "e2"                       # newest first
    e1 = next(r for r in rows if r["email_id"] == "e1")
    e2 = next(r for r in rows if r["email_id"] == "e2")
    assert e1["is_exemplar"] is True and e1["final_text"] == "Here's how returns work."
    assert e2["is_exemplar"] is False and e2["action"] == "takeover"


def test_reply_endpoint_returns_the_newest_reply_for_an_email(base_url, tmp_path, monkeypatch):
    """A `corpus:<email_id>` citation chip is only tappable if the reply behind it is retrievable.
    The shelf has no by-id lookup, so this endpoint IS the resolver: newest row for that id that
    actually carries a reply, with the email it answered."""
    from bean import corrections as corr
    log = tmp_path / "corrections.jsonl"
    monkeypatch.setattr(srv, "CORRECTIONS_PATH", log)
    # an older reply for the same email — superseded by the one below
    corr.record(corr.Correction(
        email_id="e7", category="Returns", confidence="flag", action="approve",
        final_text="First pass.", meta={"email_subject": "cracked tube", "email_body": "it split"}), log_path=log)
    # a later row with NO final_text must not win (it carries no reply to show)
    corr.record(corr.Correction(
        email_id="e7", category="Returns", confidence="flag", action="edit",
        original_draft="d", final_text="We'll send a new tube today, no charge.",
        meta={"email_subject": "cracked tube", "email_body": "it split"}), log_path=log)
    corr.record(corr.Correction(email_id="e7", category="Returns", confidence="flag", action="skip"), log_path=log)

    url, _ = base_url
    status, body = _req(f"{url}/api/reply?id=e7")
    assert status == 200
    assert body["email_id"] == "e7"
    assert body["reply"] == "We'll send a new tube today, no charge."
    assert body["subject"] == "cracked tube" and body["body"] == "it split"


def test_reply_endpoint_404s_when_nothing_was_ever_sent(base_url, tmp_path, monkeypatch):
    """No row, or only rows without a reply → 404, so the sheet can say 'me can't find it' instead
    of rendering an empty box that reads like a lost reply."""
    from bean import corrections as corr
    log = tmp_path / "corrections.jsonl"
    monkeypatch.setattr(srv, "CORRECTIONS_PATH", log)
    corr.record(corr.Correction(email_id="e8", category="General", confidence="flag", action="takeover"), log_path=log)

    url, _ = base_url
    assert _req(f"{url}/api/reply?id=e8")[0] == 404
    assert _req(f"{url}/api/reply?id=nope")[0] == 404
    assert _req(f"{url}/api/reply")[0] == 400  # no id at all is a client error, not a miss
