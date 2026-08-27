"""`/healthz` and the preview spend ceiling — the two surfaces the deploy script trusts.

`/healthz` is the only production surface a deploy may touch: it must answer without a passcode,
without a model call, and without leaking a single line of the operator's mail. `deploy.sh` believes it
and nothing else, because "Deploy complete" from Railway once meant an image with no Python in it.

The preview limit is a spend guard on the most expensive endpoint in the app. The test that matters
is not that it returns 429 — it is that the model was never reached. A 429 that already paid for
the tokens is not a limit.
"""

from __future__ import annotations

import json
import threading
import urllib.error
import urllib.request
from datetime import datetime, timedelta, timezone
from http.server import ThreadingHTTPServer

import pytest

from bean import server as srv
from bean.fixtures import sw_config
from bean.gate import GateResult
from bean.llm import FakeModel
from bean.notebook import Bucket, Notebook
from bean.paths import corrections_path, inbox_path, last_inbound_path

_EMAIL = {
    "id": "tw-c", "sender_name": "Sam", "sender_email": "sam@x.com",
    "subject": "fabric?", "body": "which fabric grade if we have a dog?",
}
_APPROVAL = json.dumps({
    "email_id": "prior-1", "category": "Sofas & Upholstery", "confidence": "high", "action": "approve",
    "original_draft": None, "final_text": "Performance Weave for a home with dogs.", "edit_kind": None,
    "note": "", "liked": False, "meta": {"email_subject": "s", "email_body": "b"},
})


@pytest.fixture(autouse=True)
def _reset_windows():
    for structure in (srv._auth_failures, srv._auth_lockouts, srv._inbound_hits, srv._preview_hits):
        structure.clear()
    yield
    for structure in (srv._auth_failures, srv._auth_lockouts, srv._inbound_hits, srv._preview_hits):
        structure.clear()


@pytest.fixture
def server(tmp_path, monkeypatch):
    """A live server with only the network faked. Yields (base_url, draft_model, tmp_path)."""
    monkeypatch.setenv("BEAN_DATA_DIR", str(tmp_path))
    monkeypatch.delenv("BEAN_CUSTOMER", raising=False)
    monkeypatch.delenv("BEAN_PASSCODE", raising=False)

    cfg_path = tmp_path / "config.json"
    cfg_path.write_text(json.dumps(sw_config().to_dict()), encoding="utf-8")
    monkeypatch.setattr(srv, "CONFIG_PATH", cfg_path)
    monkeypatch.setattr(srv, "CORRECTIONS_PATH", corrections_path())
    monkeypatch.setattr(srv, "STATUS_PATH", tmp_path / "status.json")
    monkeypatch.setattr(srv, "gate", lambda email, rules=None, customer=None: GateResult("reply"))

    nb_path = tmp_path / "notebook.md"
    Notebook("A store", buckets=[Bucket("Sofas & Upholstery", "cite the product doc")]).save(nb_path)
    monkeypatch.setattr(srv, "NOTEBOOK_PATH", nb_path)
    draft = FakeModel({"draft": {
        "bucket": "Sofas & Upholstery", "draft": "Performance Weave for a home with dogs.",
        "citations": ["notebook:Sofas & Upholstery"], "confidence": "green", "why_unsure": [],
    }})
    monkeypatch.setattr(srv, "ModelAdapter", lambda *a, **k: draft)

    httpd = ThreadingHTTPServer(("127.0.0.1", 0), srv.BeanHandler)
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    try:
        yield f"http://127.0.0.1:{httpd.server_address[1]}", draft, tmp_path
    finally:
        httpd.shutdown()
        httpd.server_close()


def _get(url):
    try:
        with urllib.request.urlopen(url) as r:
            return r.status, json.loads(r.read().decode() or "null")
    except urllib.error.HTTPError as e:
        return e.code, json.loads(e.read().decode() or "null")


def _preview(base):
    req = urllib.request.Request(
        f"{base}/api/preview", data=json.dumps({"email": _EMAIL}).encode(),
        method="POST", headers={"Content-Type": "application/json"},
    )
    try:
        with urllib.request.urlopen(req) as r:
            return r.status
    except urllib.error.HTTPError as e:
        return e.code


# ---- /healthz -------------------------------------------------------------------------------

def test_healthz_reports_sha_config_and_corrections(server):
    base, draft, tmp = server
    corrections_path().parent.mkdir(parents=True, exist_ok=True)
    corrections_path().write_text(_APPROVAL + "\n", encoding="utf-8")

    status, body = _get(f"{base}/healthz")
    assert status == 200
    assert body["config_ok"] is True
    # `config_nodes` used to be asserted here (the loaded tree's top-level node count). It went with
    # Config.tree — an inert count of a structure nothing walks reads as a health signal it isn't.
    assert body["volume_writable"] is True
    assert body["corrections_ok"] is True
    assert body["corrections_loaded"] == 1
    assert "sha" in body
    assert not draft.calls, "/healthz called a model"


def test_a_maintenance_write_cannot_forge_the_inbound_heartbeat(server):
    """THE regression. The alarm used to read inbox.jsonl's mtime, so re-drafting her inbox and
    copying it back reported `hours_since_inbound: 0.0` for 24h while inbound had been dead four days.
    The one monitor whose entire job is detecting silence was forged by a routine write."""
    base, _, _ = server
    inbox = inbox_path(srv.CUSTOMER)
    inbox.parent.mkdir(parents=True, exist_ok=True)

    # Mail genuinely landed an hour ago...
    stamp = datetime.now(timezone.utc) - timedelta(hours=1)
    last_inbound_path(srv.CUSTOMER).write_text(stamp.isoformat(timespec="seconds"), encoding="utf-8")
    before = _get(f"{base}/healthz")[1]["hours_since_inbound"]
    assert before == pytest.approx(1.0, abs=0.2)

    # ...and now a maintenance write touches the inbox, exactly as the re-draft push did.
    inbox.write_text('{"id": "rewritten-by-maintenance"}\n', encoding="utf-8")

    after = _get(f"{base}/healthz")[1]["hours_since_inbound"]
    assert after == pytest.approx(before, abs=0.2), "a maintenance write moved the inbound heartbeat"


def test_a_successful_inbound_stamps_the_heartbeat(server):
    """The other half: the marker is worthless if the real path doesn't write it. Goes through the
    actual webhook rather than calling the helper — the shipped path is the one that has to stamp."""
    base, _, _ = server
    marker = last_inbound_path(srv.CUSTOMER)
    assert not marker.exists()

    req = urllib.request.Request(
        f"{base}/api/inbound", method="POST", headers={"Content-Type": "application/json"},
        data=json.dumps({
            "MessageID": "hb-1", "From": "dana@example.com", "FromName": "Dana",
            "Subject": "Which fabric grade for a home with dogs?", "TextBody": "Which one do I need?",
        }).encode(),
    )
    with urllib.request.urlopen(req) as resp:
        assert resp.status == 200

    assert marker.exists(), "a successful inbound did not stamp the heartbeat"
    body = _get(f"{base}/healthz")[1]
    assert body["inbound_stale"] is False
    assert body["hours_since_inbound"] == pytest.approx(0.0, abs=0.2)


def test_a_tenant_that_has_never_received_mail_is_not_reported_stale(server):
    """`stale` means mail STOPPED. A brand-new tenant's mail never started, and an alarm that cries on
    a new store's first day is an alarm nobody reads by day ten."""
    base, _, _ = server
    assert not last_inbound_path(srv.CUSTOMER).exists()
    assert not inbox_path(srv.CUSTOMER).exists()
    body = _get(f"{base}/healthz")[1]
    assert body["last_inbound_at"] is None
    assert body["inbound_stale"] is False


def test_an_inbox_with_no_heartbeat_is_still_reported_stale(server):
    """The inverse, and the one that must not regress into optimism: mail HAS arrived here before
    (there is an inbox), yet there is no heartbeat. That is unexplained silence — report it."""
    base, _, _ = server
    inbox = inbox_path(srv.CUSTOMER)
    inbox.parent.mkdir(parents=True, exist_ok=True)
    inbox.write_text('{"id": "arrived-before-markers-existed"}\n', encoding="utf-8")
    body = _get(f"{base}/healthz")[1]
    assert body["last_inbound_at"] is None
    assert body["inbound_stale"] is True


def test_serving_demo_fields_fails_healthz_for_a_real_tenant(server, monkeypatch):
    """The alarm that already fired once in production: the live tenant's categories were silently
    replaced by the demo store's, byte-for-byte, and every other counter stayed green. A real tenant
    serving demo fields is NOT healthy."""
    base, _, _ = server
    monkeypatch.setattr(srv, "CONFIG_PATH", srv.CONFIG_PATH.parent / "no-such-config.json")
    monkeypatch.setattr(srv, "DEMO_TENANT", False)
    status, body = _get(f"{base}/healthz")
    assert body["config_is_demo_default"] is True
    assert status == 503


def test_the_demo_tenant_serving_demo_fields_is_healthy(server, monkeypatch):
    """...and the same condition is CORRECT for the demo instance, which exists to serve exactly that
    config. Without this the demo can never deploy — scripts/deploy.sh gates on this endpoint. It is
    still reported, so the flag changes the verdict, never the facts."""
    base, _, _ = server
    monkeypatch.setattr(srv, "CONFIG_PATH", srv.CONFIG_PATH.parent / "no-such-config.json")
    monkeypatch.setattr(srv, "DEMO_TENANT", True)
    status, body = _get(f"{base}/healthz")
    assert body["config_is_demo_default"] is True
    assert status == 200


def test_production_refuses_to_start_without_an_explicit_tenant(monkeypatch):
    """The default tenant is the DEMO store, so an unset BEAN_CUSTOMER in production would serve demo
    fields out of an empty folder and persist them on the first save. Prod ran without this variable
    set, back when the default was a REAL tenant's folder name — so the day that default changed to
    the demo store, a silent tenant switch was one
    deploy away. Crash instead: deploy.sh catches a dead boot and rolls back; it cannot catch a server
    that came up happily as the wrong customer."""
    monkeypatch.setenv("BEAN_ENV", "production")
    monkeypatch.delenv("BEAN_CUSTOMER", raising=False)
    with pytest.raises(SystemExit) as exc:
        srv.serve(port=0)
    assert "BEAN_CUSTOMER" in str(exc.value)


def test_healthz_needs_no_passcode_even_in_production(server, monkeypatch):
    """The deploy script has no secret and must still be able to verify the deploy."""
    base, _, _ = server
    monkeypatch.setenv("BEAN_ENV", "production")
    monkeypatch.setenv("BEAN_PASSCODE", "a-secret-we-never-send")
    assert _get(f"{base}/healthz")[0] == 200
    assert _get(f"{base}/api/inbox")[0] in (401, 503)  # the gated surface stays shut


def test_healthz_leaks_no_customer_content(server):
    base, _, _ = server
    corrections_path().parent.mkdir(parents=True, exist_ok=True)
    corrections_path().write_text(_APPROVAL + "\n", encoding="utf-8")
    raw = json.dumps(_get(f"{base}/healthz")[1])
    for secret in ("Performance Weave for a home with dogs.", "prior-1", "Sofas & Upholstery"):
        assert secret not in raw, f"/healthz leaked {secret!r}"


def test_healthz_is_503_when_the_config_cannot_be_read(server):
    base, _, tmp = server
    (tmp / "config.json").write_text("{ shredded", encoding="utf-8")
    status, body = _get(f"{base}/healthz")
    assert status == 503
    assert body["config_ok"] is False


def test_healthz_is_503_when_the_correction_log_is_unreadable(server):
    base, _, _ = server
    corrections_path().parent.mkdir(parents=True, exist_ok=True)
    corrections_path().write_text("{ not json\n", encoding="utf-8")
    status, body = _get(f"{base}/healthz")
    assert status == 503
    assert body["corrections_ok"] is False


# ---- is Bean actually DRAFTING? --------------------------------------------------------------
# The field none of the above measures. Every check in this file passes for a Bean that flags 100%
# of the operator's mail — and one did, for weeks.

def _write_inbox(confidences):
    """Write one inbox line per confidence — the `_result_dict` shape the server stores. `None`
    means gate-FILED mail (no `chunks`): it never walked the tree, so it is not an outcome.

    `inbox_path(srv.CUSTOMER)`, never a bare `inbox_path()`. `srv.CUSTOMER` is bound ONCE at import
    (bean/server.py:71), so the `server` fixture's `delenv("BEAN_CUSTOMER")` cannot reach it — it
    patches the four derived *_PATH constants but not the root one. A bare call resolves live off
    the env instead, so with BEAN_CUSTOMER set the test wrote `<tmp>/maplemoss/inbox.jsonl` while
    `_health` read `<tmp>/<that tenant>/inbox.jsonl`: two different files, an inbox the server never
    saw, and `drafting_stalled: False`.

    That made these four tests pass by hand and fail under `scripts/deploy.sh`, which REQUIRES
    BEAN_CUSTOMER — i.e. the deploy gate was red exactly when it ran the documented way, and green
    when run manually. A gate with that asymmetry teaches you to run preflight by hand and then
    reach for `railway up` yourself, which is precisely how production died once already."""
    path = inbox_path(srv.CUSTOMER)
    path.parent.mkdir(parents=True, exist_ok=True)
    lines = []
    for i, conf in enumerate(confidences):
        result = ({"disposition": "filed", "kind": "newsletter"} if conf is None else
                  {"confidence": conf, "chunks": [{"path": ["Sofas & Upholstery"], "node_type": "escalate"}]})
        lines.append(json.dumps({"id": f"e{i}", "sender_name": "Sam", "sender_email": "s@x.com",
                                 "reply_to": "s@x.com", "subject": "s", "body": "b",
                                 "received_at": "", "result": result}))
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def test_healthz_reports_drafting_stalled_but_stays_200(server):
    """The alarm that was missing — and the 503 it deliberately does NOT raise.

    A 503 makes scripts/deploy.sh roll back, and rolling back the code does not teach the tree: the
    failure lives in the operator's config, so failing the deploy gate on it would revert good code and
    still flag every email. Report loudly, never fail. (Same call the codebase already made for
    `inbound_stale`.)
    """
    base, _, _ = server
    _write_inbox(["flag"] * 6)

    status, body = _get(f"{base}/healthz")
    assert status == 200, "an unteachable Bean must not roll back a good deploy"
    assert body["drafting_stalled"] is True
    assert body["drafted_fraction"] == 0.0
    assert body["walked_emails"] == 6
    assert body["config_ok"] is True  # every OTHER light is green — that is the whole problem


def test_healthz_does_not_cry_stall_when_bean_is_drafting(server):
    base, _, _ = server
    _write_inbox(["high", "low", "flag", "high"])

    _, body = _get(f"{base}/healthz")
    assert body["drafting_stalled"] is False
    assert body["drafted_fraction"] == 0.75


def test_healthz_ignores_filed_noise_when_judging_whether_bean_drafts(server):
    """46% of Bean's inbound is carrier/receipt noise the gate files. It never reaches the tree, so
    it must neither dilute the fraction nor push real customer mail out of the window."""
    base, _, _ = server
    _write_inbox([None] * 30 + ["flag"] * 6)

    _, body = _get(f"{base}/healthz")
    assert body["walked_emails"] == 6, "filed mail is not a sample of Bean's judgment"
    assert body["drafting_stalled"] is True


def test_healthz_survives_an_unreadable_inbox(server):
    """A corrupt inbox line is not a 503 — /healthz reports, it does not raise."""
    base, _, _ = server
    inbox_path(srv.CUSTOMER).parent.mkdir(parents=True, exist_ok=True)
    inbox_path(srv.CUSTOMER).write_text("{ not json\n", encoding="utf-8")

    status, body = _get(f"{base}/healthz")
    assert status == 200
    assert body["drafting_stalled"] is None  # unknown, and honest about it


# ---- the preview spend ceiling ---------------------------------------------------------------

def test_preview_over_the_limit_returns_429_and_never_reaches_the_model(server, monkeypatch):
    base, draft, _ = server
    monkeypatch.setattr(srv, "_PREVIEW_MAX_PER_MIN", 3)

    assert [_preview(base) for _ in range(3)] == [200, 200, 200]
    calls_before = len(draft.calls)
    assert calls_before > 0  # the allowed previews really did run the model

    assert _preview(base) == 429
    assert _preview(base) == 429
    assert len(draft.calls) == calls_before, "a rate-limited preview still called the model"


def test_preview_limit_does_not_gate_healthz(server, monkeypatch):
    """A rate-limited app must still be able to say what it is running, or rollback flies blind."""
    base, _, _ = server
    monkeypatch.setattr(srv, "_PREVIEW_MAX_PER_MIN", 0)
    assert _preview(base) == 429
    assert _get(f"{base}/healthz")[0] == 200
