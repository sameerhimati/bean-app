"""The public demo: a tenant seeded from committed fixtures, and the write lock that makes it safe
to hand a stranger a URL for.

Two properties are load-bearing here and neither is obvious from reading the code:

  1. NO API KEY. The demo service is deployed without `ANTHROPIC_API_KEY` on purpose — a public URL
     must not be able to spend money — so seeding and serving have to work with the key deleted from
     the environment. These tests delete it rather than assume it's absent, because the developer
     running them has one in `.env` and would never otherwise notice a new model call sneaking into
     the boot path.

  2. 404, NOT 403. Every locked write must answer 404 specifically. The client treats a 404 on a
     write as "there is no backend here" and stays silent; anything else fires a visible
     write-failed toast at a visitor who did nothing wrong. `test_bean_inbox_js.py` and
     web/bean-inbox.jsx are the other half of that contract — this file pins the server's half.
"""

from __future__ import annotations

import json
import os
import threading
import urllib.error
import urllib.request
from http.server import ThreadingHTTPServer

import pytest

from bean import server as srv
from bean.contract import Grounding
from bean.demo import DEMO_CUSTOMER, DemoSeedRefused, load_demo_inbox, seed_tenant, tenant_is_taught
from bean.fixtures import sw_config
from bean.gate import GateResult
from bean.llm import FakeModel
from bean.notebook import load as load_notebook
from bean.paths import config_path, corrections_path, inbox_path, notebook_path, status_path


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


def _seed_into(tmp_path, monkeypatch):
    """Seed a real demo tenant under `tmp_path` and point the server module's paths at it.

    The server binds CUSTOMER and its path constants at IMPORT time, so a test that only sets
    BEAN_DATA_DIR would seed one folder and serve another — which is exactly the class of bug
    [[verify-the-shipped-path]] is about. Both halves are set here."""
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)  # property (1): the demo has no key
    monkeypatch.setenv("BEAN_DATA_DIR", str(tmp_path))
    seed_tenant(DEMO_CUSTOMER)
    monkeypatch.setattr(srv, "CUSTOMER", DEMO_CUSTOMER)
    monkeypatch.setattr(srv, "CONFIG_PATH", config_path(DEMO_CUSTOMER))
    monkeypatch.setattr(srv, "NOTEBOOK_PATH", notebook_path(DEMO_CUSTOMER))
    monkeypatch.setattr(srv, "CORRECTIONS_PATH", corrections_path(DEMO_CUSTOMER))
    monkeypatch.setattr(srv, "STATUS_PATH", status_path(DEMO_CUSTOMER))
    monkeypatch.setattr(srv, "NOTEBOOK_REVIEW_PATH", tmp_path / DEMO_CUSTOMER / "notebook_review.json")


def _serve():
    httpd = ThreadingHTTPServer(("127.0.0.1", 0), srv.BeanHandler)
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    return httpd, f"http://127.0.0.1:{httpd.server_address[1]}"


@pytest.fixture(autouse=True)
def _reset_rate_limits():
    for structure in (srv._auth_failures, srv._auth_lockouts, srv._inbound_hits, srv._preview_hits):
        structure.clear()
    yield
    for structure in (srv._auth_failures, srv._auth_lockouts, srv._inbound_hits, srv._preview_hits):
        structure.clear()


@pytest.fixture
def demo_url(tmp_path, monkeypatch):
    """The deployed shape: a seeded demo tenant, BEAN_DEMO_READONLY on, no key, no seams patched.

    Nothing is monkeypatched into the gate or the engine on purpose — if any GET or any locked route
    reached a model, this fixture would blow up rather than quietly pass."""
    _seed_into(tmp_path, monkeypatch)
    monkeypatch.setattr(srv, "DEMO_READONLY", True)
    httpd, url = _serve()
    try:
        yield url
    finally:
        httpd.shutdown()
        httpd.server_close()


@pytest.fixture
def open_url(tmp_path, monkeypatch):
    """The same tenant with the lock OFF — the control. Offline seams patched in, because proving
    "this route still exists without the flag" must not depend on a live model."""
    _seed_into(tmp_path, monkeypatch)
    monkeypatch.setattr(srv, "DEMO_READONLY", False)
    monkeypatch.setattr(srv, "gate", lambda email, rules=None: GateResult("reply"))
    monkeypatch.setattr(srv, "ModelAdapter", lambda *a, **k: FakeModel({"draft": {
        "bucket": "Sofas & Upholstery", "draft": "hi", "citations": ["notebook:Sofas & Upholstery"],
        "confidence": "green", "why_unsure": [],
    }}))
    httpd, url = _serve()
    try:
        yield url
    finally:
        httpd.shutdown()
        httpd.server_close()


# ---- 1. seeding a whole tenant from committed fixtures, with no key ---------------------------

def test_seed_writes_config_notebook_and_inbox_with_no_api_key(tmp_path, monkeypatch):
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    monkeypatch.setenv("BEAN_DATA_DIR", str(tmp_path))

    did = seed_tenant(DEMO_CUSTOMER)
    assert did == {"config": "wrote", "notebook": "wrote", "inbox": "wrote"}

    # The config is the frozen Sable & Wren snapshot, not the Maple & Moss default a config-less tenant
    # would be served — so the demo can never be mistaken for (or overwrite into) that default.
    assert json.loads(config_path(DEMO_CUSTOMER).read_text()) == sw_config().to_dict()
    assert load_notebook(notebook_path(DEMO_CUSTOMER)).store == "Sable & Wren"
    lines = [ln for ln in inbox_path(DEMO_CUSTOMER).read_text().splitlines() if ln.strip()]
    assert [json.loads(ln) for ln in lines] == load_demo_inbox()


def test_seeding_twice_is_a_noop(tmp_path, monkeypatch):
    """It runs on every container boot, so a second run must be a quiet no-op — not a nonzero exit
    that crash-loops the service, and not a rewrite that clobbers whatever is there."""
    monkeypatch.setenv("BEAN_DATA_DIR", str(tmp_path))
    seed_tenant(DEMO_CUSTOMER)
    notebook_path(DEMO_CUSTOMER).write_text("# Edited — support notebook\n", encoding="utf-8")

    assert seed_tenant(DEMO_CUSTOMER) == {"config": "kept", "notebook": "kept", "inbox": "kept"}
    assert notebook_path(DEMO_CUSTOMER).read_text().startswith("# Edited")
    # ...and --force is how you actually pick up a regenerated fixture.
    assert seed_tenant(DEMO_CUSTOMER, force=True)["notebook"] == "wrote"
    assert load_notebook(notebook_path(DEMO_CUSTOMER)).store == "Sable & Wren"


def test_seeding_refuses_a_tenant_that_has_been_taught(tmp_path, monkeypatch):
    """The wall. A corrections log means a human has been teaching this folder — regenerating their
    brain from fixtures is [[fixtures-leaked-into-prod]] pointed the other way. `force` does not
    override it, which is the whole difference between a guardrail and a wall."""
    monkeypatch.setenv("BEAN_DATA_DIR", str(tmp_path))
    corrections_path(DEMO_CUSTOMER).parent.mkdir(parents=True, exist_ok=True)
    corrections_path(DEMO_CUSTOMER).write_text('{"email_id": "real"}\n', encoding="utf-8")
    assert tenant_is_taught(DEMO_CUSTOMER)

    for force in (False, True):
        with pytest.raises(DemoSeedRefused):
            seed_tenant(DEMO_CUSTOMER, force=force)
    assert not config_path(DEMO_CUSTOMER).exists()  # nothing was written on the way to refusing


def test_the_committed_fixture_shows_all_four_lanes():
    """The demo's whole job is showing the thesis — one-tap green, hedged yellow, refused red, and
    filed. If a regeneration collapses the spread (everything yellow, say), the demo silently stops
    making the argument, and nothing else in the suite would notice."""
    items = load_demo_inbox()
    seen = {i["result"].get("confidence") or i["result"].get("disposition") for i in items}
    assert {"green", "yellow", "red", "filed"} <= seen, f"the demo inbox only shows {sorted(seen)}"


def test_every_fixture_result_obeys_the_output_contract():
    """The fixture is generated, but it is committed — so it is now a file a human can hand-edit,
    and a hand-edited "green" with no citation would be the yes-man bug shipped as a demo. Re-assert
    the DraftResult invariants over what is actually on disk."""
    for item in load_demo_inbox():
        r = item["result"]
        assert item["id"] and item["subject"] and item["body"]
        if r.get("disposition") == "filed":
            assert r.get("kind") and r.get("reason")
            continue
        assert r["email_id"] == item["id"]
        if r["confidence"] == Grounding.GREEN.value:
            assert r["draft"] and r["citations"], "green MEANS grounded — an uncited green is a lie"
        elif r["confidence"] == Grounding.YELLOW.value:
            assert r["draft"] and r["why_unsure"]
        else:
            assert r["why_unsure"], "red must say why it won't stand behind a reply"


# ---- 2. the seeded tenant actually serves, with the key deleted -------------------------------

def test_the_demo_serves_its_inbox_config_and_notebook_with_no_key(demo_url):
    assert "ANTHROPIC_API_KEY" not in os.environ

    status, body = _req(f"{demo_url}/api/inbox")
    assert status == 200
    assert body["emails"] == load_demo_inbox()
    assert any(e["result"].get("confidence") == "green" for e in body["emails"])

    status, body = _req(f"{demo_url}/api/config")
    assert status == 200 and body == sw_config().to_dict()

    status, body = _req(f"{demo_url}/api/notebook")
    assert status == 200 and body["store"] == "Sable & Wren"


def test_the_demo_boots_past_the_forwarding_overlay(demo_url, tmp_path, monkeypatch):
    """`/bean-data.jsx` must announce the demo SYNCHRONOUSLY, and only for the demo.

    web/bean-root.jsx seeds `onboarded` from `window.BEAN_DEMO_TENANT` before it looks at
    localStorage, so a visitor lands on the inbox instead of a Proton-Mail forwarding walk they have
    no mailbox for. The flag can only ride THIS script: Bean.html loads it ahead of bean-root, and
    anything fetched (/api/meta, /api/config) resolves a frame too late — which renders the overlay
    and then hides it, the one outcome worse than leaving it up.

    Asserted in both directions, because a flag that is never false proves nothing. WEB_DIR is
    repointed at an empty tmp dir so the answer comes from the flag rather than from whether the
    machine running the tests happens to have a generated bean-data.jsx lying around.
    """
    def _text(url):
        with urllib.request.urlopen(url) as resp:
            return resp.status, resp.read().decode()

    monkeypatch.setattr(srv, "WEB_DIR", tmp_path)

    monkeypatch.setattr(srv, "DEMO_TENANT", True)
    status, body = _text(f"{demo_url}/bean-data.jsx")
    assert status == 200 and "window.BEAN_DEMO_TENANT = true;" in body

    monkeypatch.setattr(srv, "DEMO_TENANT", False)
    status, body = _text(f"{demo_url}/bean-data.jsx")
    assert status == 200 and "window.BEAN_DEMO_TENANT = false;" in body


def test_the_demo_never_serves_a_laptop_generated_bean_data_file(demo_url, tmp_path, monkeypatch):
    """The demo synthesizes `/bean-data.jsx` even when one exists on disk.

    That file is gitignored and generated from whatever tenant the generating laptop was pointed at
    — the original leak in this codebase — so the public demo serving it is [[fixtures-leaked-into-
    prod]] pointing outward. It is also what would make the boot flag above present in the deploy
    and absent on a local demo run, which is the worst way for a flag to behave. A real tenant still
    gets the file: this is the only deployment that overrides it."""
    def _text(url):
        with urllib.request.urlopen(url) as resp:
            return resp.status, resp.read().decode()

    monkeypatch.setattr(srv, "WEB_DIR", tmp_path)
    (tmp_path / "bean-data.jsx").write_text("window.CONFIG = 'SOMEBODY ELSE\\'S STORE';\n")

    monkeypatch.setattr(srv, "DEMO_TENANT", True)
    _, body = _text(f"{demo_url}/bean-data.jsx")
    assert "SOMEBODY ELSE" not in body and "window.BEAN_DEMO_TENANT = true;" in body

    monkeypatch.setattr(srv, "DEMO_TENANT", False)
    _, body = _text(f"{demo_url}/bean-data.jsx")
    assert "SOMEBODY ELSE" in body, "a real tenant's own generated file must still be served"


# ---- 3. the write lock ------------------------------------------------------------------------
# Every route BEAN_DEMO_READONLY hides, with the payload it would normally take. The payloads are
# real ones (not junk) so the "without the flag" half proves the route genuinely works rather than
# merely answering something-that-is-not-404.

_LOCKED = [
    ("POST", "/api/correction", {"email_id": "sw-fabric", "category": "Sofas & Upholstery", "action": "approve"}),
    ("POST", "/api/status", {"id": "sw-fabric", "state": "approved"}),
    ("POST", "/api/clear-filed", {}),
    ("POST", "/api/preview", {"email": {"id": "p1", "sender_name": "A", "sender_email": "a@b.com",
                                        "subject": "s", "body": "which fabric grade for a home with dogs?"}}),
    ("POST", "/api/inbound", {"MessageID": "m1", "From": "a@b.com", "Subject": "s", "TextBody": "hi"}),
    ("PUT", "/api/config", None),      # body filled in per-test: it needs a live ETag
    ("PUT", "/api/notebook", None),
    ("PUT", "/api/notebook/review", {"decisions": {}}),
]


@pytest.mark.parametrize("method,path,body", _LOCKED, ids=[f"{m} {p}" for m, p, _ in _LOCKED])
def test_every_write_route_is_404_under_the_flag(demo_url, method, path, body):
    """404 and not 403, deliberately — see this module's docstring. The assertion is on the exact
    status because that IS the contract with the client; 'some 4xx' would pass while the UI toasts."""
    status, _ = _req(f"{demo_url}{path}", method=method, body=body if body is not None else {},
                     headers={"If-Match": "whatever"})
    assert status == 404, f"{method} {path} answered {status} — the client only tolerates 404"


@pytest.mark.parametrize("method,path,body", _LOCKED, ids=[f"{m} {p}" for m, p, _ in _LOCKED])
def test_every_write_route_still_exists_without_the_flag(open_url, method, path, body):
    """The control. Without the flag these must NOT 404 — otherwise the test above would pass on a
    typo'd route name and the lock would be proving nothing."""
    etag = None
    if method == "PUT":
        with urllib.request.urlopen(f"{open_url}{path}") as resp:
            etag, body = resp.headers.get("ETag"), json.loads(resp.read().decode())
    status, _ = _req(f"{open_url}{path}", method=method, body=body,
                     headers={"If-Match": etag} if etag else None)
    assert status != 404, f"{method} {path} 404s even with the lock off"


def test_the_writes_that_matter_actually_land_without_the_flag(open_url):
    """Stronger than 'not 404' for the two the demo hides most visibly: prove they really persist,
    so the lock is switching off working routes rather than broken ones."""
    assert _req(f"{open_url}/api/status", method="POST", body={"id": "sw-fabric", "state": "approved"}) \
        == (200, {"sw-fabric": "approved"})
    status, body = _req(f"{open_url}/api/correction", method="POST",
                        body={"email_id": "sw-fabric", "category": "Sofas & Upholstery", "action": "approve"})
    assert status == 200 and body["ok"] is True


def test_reads_are_untouched_by_the_flag(demo_url):
    """GET is the whole demo. The lock is checked only in the POST/PUT routers, so a read must be
    byte-identical to an unlocked deployment — including the reads whose WRITE twin is hidden."""
    for path in ("/api/config", "/api/notebook", "/api/inbox", "/api/status", "/api/meta",
                 "/api/learning", "/api/corrections", "/api/notebook/review", "/api/gate-proposals"):
        status, _ = _req(f"{demo_url}{path}")
        assert status == 200, f"GET {path} broke under the read-only flag ({status})"
    assert _req(f"{demo_url}/healthz")[0] == 200


def test_the_public_signup_is_not_locked(demo_url):
    """/api/waitlist is a write and it is deliberately NOT hidden: it is the one write a demo visitor
    legitimately wants to make ("let me in"), and it is already rate-limited, deduped and hard-capped
    for exactly this exposure. Pinned so that stays a decision rather than an oversight."""
    assert _req(f"{demo_url}/api/waitlist", method="POST", body={"email": "someone@example.com"}) \
        == (200, {"ok": True})
