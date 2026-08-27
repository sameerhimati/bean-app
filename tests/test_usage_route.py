"""`GET /api/usage` — the owner-only read path over usage.jsonl, and the attribution under it.

`bean/usage.py` has priced and aggregated this log since the first real model call and is tested.
Nothing in production had ever READ it, so Bean's spend accrued on a volume that needed an ssh to
see. Two things are pinned here, and the second is the one that makes the first worth having:

  * The route is behind a SECOND secret. The passcode is the operator's — she types it daily — so
    "behind the passcode" is not a control over her, and this route reports what Bean costs to run.
    Unset owner secret ⇒ 404, never fall-open.
  * Usage lines land on the RIGHT tenant. A cost view that lies about whose spend it is, is worse
    than no cost view — and the singleton client cache is where that lie would come from.
"""

from __future__ import annotations

import json
import threading
import urllib.error
import urllib.request
from http.server import ThreadingHTTPServer

import pytest

from bean import llm, server as srv
from bean.llm import Usage, _log_usage, live_model
from bean.paths import usage_path


@pytest.fixture
def base_url():
    httpd = ThreadingHTTPServer(("127.0.0.1", 0), srv.BeanHandler)
    t = threading.Thread(target=httpd.serve_forever, daemon=True)
    t.start()
    try:
        yield f"http://127.0.0.1:{httpd.server_address[1]}"
    finally:
        httpd.shutdown()
        httpd.server_close()


def _req(url, token=None):
    headers = {"Authorization": f"Bearer {token}"} if token else {}
    req = urllib.request.Request(url, method="GET", headers=headers)
    try:
        with urllib.request.urlopen(req) as resp:
            return resp.status, resp.read().decode("utf-8", "replace")
    except urllib.error.HTTPError as e:
        return e.code, e.read().decode("utf-8", "replace")


def _seed_usage(tmp_path, customer, rows):
    path = tmp_path / customer / "usage.jsonl"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("".join(json.dumps(r, sort_keys=True) + "\n" for r in rows), encoding="utf-8")


# ---- the owner gate ---------------------------------------------------------------------------

def test_unset_owner_secret_is_404_not_an_open_door(base_url, monkeypatch):
    """Fail CLOSED. `_authed` falls OPEN on an empty BEAN_PASSCODE (a dev convenience); this route
    must not inherit that, because the failure mode is a customer reading her own margin."""
    monkeypatch.delenv("BEAN_OWNER_PASSCODE", raising=False)
    monkeypatch.delenv("BEAN_PASSCODE", raising=False)
    status, _ = _req(f"{base_url}/api/usage")
    assert status == 404


def test_wrong_or_missing_token_is_401(base_url, monkeypatch):
    monkeypatch.setenv("BEAN_OWNER_PASSCODE", "owner-secret")
    assert _req(f"{base_url}/api/usage")[0] == 401
    assert _req(f"{base_url}/api/usage", token="not-it")[0] == 401


def test_the_operators_passcode_does_not_open_the_cost_view(base_url, monkeypatch):
    """The whole point of a second secret: she holds the passcode, and it must not be a key to this."""
    monkeypatch.setenv("BEAN_PASSCODE", "hers")
    monkeypatch.setenv("BEAN_OWNER_PASSCODE", "mine")
    assert _req(f"{base_url}/api/usage", token="hers")[0] == 401


def test_owner_token_returns_priced_rows_and_a_total(base_url, tmp_path, monkeypatch):
    monkeypatch.setenv("BEAN_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("BEAN_OWNER_PASSCODE", "owner-secret")
    _seed_usage(tmp_path, srv.CUSTOMER, [
        {"model": "claude-haiku-4-5", "purpose": "classify", "input_tokens": 2000,
         "output_tokens": 100, "cache_read": 0, "cache_write": 0},
        {"model": "claude-sonnet-4-6", "purpose": "draft", "input_tokens": 3000,
         "output_tokens": 400, "cache_read": 1000, "cache_write": 0},
    ])
    status, text = _req(f"{base_url}/api/usage", token="owner-secret")
    assert status == 200
    body = json.loads(text)
    assert body["customer"] == srv.CUSTOMER
    assert body["total"]["calls"] == 2
    # Priced off bean/usage.py's table, which is the single source of truth — asserted as a real
    # number so a silently-unpriced model (cost 0.0) can't pass as a cheap one.
    assert body["total"]["cost"] > 0
    assert body["total"]["unpriced_calls"] == 0
    assert {r["purpose"] for r in body["rows"]} == {"classify", "draft"}


def test_text_format_returns_the_same_table_the_cli_prints(base_url, tmp_path, monkeypatch):
    from bean.usage import render_table, report

    monkeypatch.setenv("BEAN_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("BEAN_OWNER_PASSCODE", "owner-secret")
    _seed_usage(tmp_path, srv.CUSTOMER, [
        {"model": "claude-haiku-4-5", "purpose": "classify", "input_tokens": 2000,
         "output_tokens": 100, "cache_read": 0, "cache_write": 0},
    ])
    status, text = _req(f"{base_url}/api/usage?format=text", token="owner-secret")
    assert status == 200
    # The route must not grow a second renderer — `python -m bean.usage` and this have to agree.
    assert text == render_table(report(customer=srv.CUSTOMER))


def test_customer_query_reports_a_different_tenant(base_url, tmp_path, monkeypatch):
    monkeypatch.setenv("BEAN_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("BEAN_OWNER_PASSCODE", "owner-secret")
    _seed_usage(tmp_path, "otherstore", [
        {"model": "claude-sonnet-4-6", "purpose": "draft", "input_tokens": 10,
         "output_tokens": 10, "cache_read": 0, "cache_write": 0},
    ])
    status, text = _req(f"{base_url}/api/usage?customer=otherstore", token="owner-secret")
    body = json.loads(text)
    assert status == 200 and body["customer"] == "otherstore" and body["total"]["calls"] == 1


# ---- attribution: the thing that makes the numbers above true ---------------------------------

def test_usage_lands_on_the_named_tenants_volume(tmp_path, monkeypatch):
    monkeypatch.setenv("BEAN_DATA_DIR", str(tmp_path))
    _log_usage("claude-sonnet-4-6", Usage(input_tokens=10, output_tokens=5), "draft", "store-a")
    _log_usage("claude-sonnet-4-6", Usage(input_tokens=20, output_tokens=5), "draft", "store-b")
    assert len(usage_path("store-a").read_text().splitlines()) == 1
    assert len(usage_path("store-b").read_text().splitlines()) == 1
    assert json.loads(usage_path("store-b").read_text())["input_tokens"] == 20


def test_the_client_cache_does_not_hand_one_tenant_anothers_client(monkeypatch):
    """The bug this fix exists to close. `_LIVE` used to be keyed on model id alone, so the second
    tenant in a process got the first tenant's client — and every token it spent was filed under
    the first tenant's name, silently and with no error anywhere."""
    monkeypatch.setattr(llm, "_LIVE", {})
    # Constructing a real AnthropicModel needs the SDK but no key/network, so stub the client out.
    monkeypatch.setattr(llm.AnthropicModel, "__init__",
                        lambda self, model, temperature=None, *, customer=None: (
                            setattr(self, "name", model), setattr(self, "customer", customer),
                            setattr(self, "temperature", temperature), None)[-1])
    a = live_model("claude-haiku-4-5", "store-a")
    b = live_model("claude-haiku-4-5", "store-b")
    assert a is not b
    assert a.customer == "store-a" and b.customer == "store-b"
    assert live_model("claude-haiku-4-5", "store-a") is a  # still cached per tenant


def test_no_customer_keeps_the_old_single_tenant_default(tmp_path, monkeypatch):
    """The single-tenant path and every test must keep working with no customer named."""
    monkeypatch.setenv("BEAN_DATA_DIR", str(tmp_path))
    _log_usage("claude-sonnet-4-6", Usage(input_tokens=1, output_tokens=1), "draft")
    assert usage_path().exists()
