"""Offline tests for the shared-passcode gate (bean/server.py).

The gate is a single shared secret (BEAN_PASSCODE) checked per request — no auth service, no
session DB. It's read live from the env inside the handler, so these tests toggle it with
monkeypatch and drive the real server on an ephemeral port (same pattern as tests/test_server.py).

Two invariants under test: (1) when the secret is UNSET the gate is fully disabled — the app
serves exactly as before; (2) when it's SET, pages return the passcode entry page and /api/* is
401 until a cookie from /api/auth is presented.
"""

from __future__ import annotations

import threading
import urllib.error
import urllib.request
from http.server import ThreadingHTTPServer

import pytest

from bean import server as srv


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


def _req(url, method="GET", body=None, cookie=None):
    """Return (status, headers, text). Headers kept so we can read Set-Cookie."""
    data = body.encode() if isinstance(body, str) else body
    headers = {}
    if data is not None:
        headers["Content-Type"] = "application/json"
    if cookie is not None:
        headers["Cookie"] = cookie
    req = urllib.request.Request(url, data=data, method=method, headers=headers)
    try:
        with urllib.request.urlopen(req) as resp:
            return resp.status, resp.headers, resp.read().decode("utf-8", "replace")
    except urllib.error.HTTPError as e:
        return e.code, e.headers, e.read().decode("utf-8", "replace")


# ---- gate ENABLED (BEAN_PASSCODE set) --------------------------------------------------------

def test_unauthed_page_get_returns_passcode_page(base_url, monkeypatch):
    monkeypatch.setenv("BEAN_PASSCODE", "letmein")
    status, _, text = _req(f"{base_url}/Bean.html")
    assert status == 200  # the app's front door: a login page is a real page (see _ENTRY_PATHS)
    assert 'id="bean-passcode"' in text  # the gate page, not the real app
    assert "me let you in" in text  # the Bean-voice gate copy (reskinned page, no emoji wordmark)


def test_unauthed_api_post_is_401(base_url, monkeypatch):
    monkeypatch.setenv("BEAN_PASSCODE", "letmein")
    status, _, _ = _req(f"{base_url}/api/preview", method="POST", body='{"email": {}}')
    assert status == 401


def test_auth_with_correct_passcode_sets_cookie_and_unlocks(base_url, monkeypatch):
    monkeypatch.setenv("BEAN_PASSCODE", "letmein")
    status, headers, _ = _req(f"{base_url}/api/auth", method="POST", body='{"passcode": "letmein"}')
    assert status == 200
    set_cookie = headers.get("Set-Cookie", "")
    # The cookie is derived from the passcode, never the passcode itself (see _session_token).
    assert f"bean_auth={srv._session_token('letmein')}" in set_cookie
    assert "letmein" not in set_cookie
    assert "HttpOnly" in set_cookie and "Secure" in set_cookie

    # a request carrying that cookie is no longer gated: the page GET is the real app, and an
    # /api/* call is no longer 401 (config always exists via fixtures).
    status, _, text = _req(f"{base_url}/Bean.html", cookie="bean_auth=letmein")
    assert status == 200 and 'id="bean-passcode"' not in text
    status, _, _ = _req(f"{base_url}/api/config", cookie="bean_auth=letmein")
    assert status == 200


def test_auth_with_wrong_passcode_is_401(base_url, monkeypatch):
    monkeypatch.setenv("BEAN_PASSCODE", "letmein")
    status, _, _ = _req(f"{base_url}/api/auth", method="POST", body='{"passcode": "nope"}')
    assert status == 401


# ---- gate DISABLED (BEAN_PASSCODE unset) -----------------------------------------------------

def test_gate_disabled_serves_real_app(base_url, monkeypatch):
    monkeypatch.delenv("BEAN_PASSCODE", raising=False)
    status, _, text = _req(f"{base_url}/Bean.html")
    assert status == 200
    assert 'id="bean-passcode"' not in text  # gate off → real static app, no passcode page


# ---- the gate must tell the truth about its status code --------------------------------------

def test_the_front_door_is_a_real_page_and_previews_as_one(base_url, monkeypatch):
    """`/` is the login page today, and a login page is a real page: 200 + og: tags, so a link to
    the instance still previews in iMessage/Slack. Shrink _ENTRY_PATHS when a landing page ships
    and this becomes a 401 like everything else."""
    monkeypatch.setenv("BEAN_PASSCODE", "let-me-in")
    status, headers, text = _req(f"{base_url}/")
    assert status == 200
    assert 'og:title' in text  # the crawler has something to render
    assert headers.get("Cache-Control") == "no-store"
    # A WWW-Authenticate header would make the browser pop its native basic-auth dialog.
    assert headers.get("WWW-Authenticate") is None


def test_the_join_page_and_everything_it_embeds_are_reachable_unauthed(base_url, monkeypatch):
    """`/join` is how someone without a passcode ASKS for one, so every asset it embeds has to
    clear the gate too.

    It shipped without that: the page rendered 200 and its `<video src="/demo.mp4">` 401'd. Nothing
    raised, no log line, and the only person who could see the page working was the one already
    holding a session cookie. A landing page whose proof is a dead frame is worse than one with no
    video, so the embed and the asset get asserted together — checking the page alone is what
    missed it.

    The video was removed 2026-08-10 (it was the previous demo tenant's footage, and the product
    category in it was identifying) and restored the same day, re-recorded against the current
    tenant by scripts/record_demo.mjs. Through both, the rule never changed: whatever /join sends an
    unauthenticated visitor to has to actually work for them.
    """
    monkeypatch.setenv("BEAN_PASSCODE", "let-me-in")

    status, _, text = _req(f"{base_url}/join")
    assert status == 200
    assert "bean-demo-production" in text, "the join page should point at the live demo"
    assert "/demo.mp4" in text, "the join page should embed the demo recording"

    status, _, _ = _req(f"{base_url}/demo.mp4")
    assert status == 200, "the recording the join page embeds must clear the passcode gate"


def test_a_path_that_does_not_exist_is_401_when_unauthed(base_url, monkeypatch):
    """This is the one that mattered: every unauthenticated path used to answer 200 with the login
    page, so any monitor checking a status code read "healthy". That is how `curl /healthz` returned
    200 against production before /healthz existed."""
    monkeypatch.setenv("BEAN_PASSCODE", "let-me-in")
    status, headers, _ = _req(f"{base_url}/no-such-page")
    assert status == 401
    assert headers.get("Cache-Control") == "no-store"


def test_json_responses_are_never_cached(base_url, monkeypatch):
    monkeypatch.setenv("BEAN_PASSCODE", "let-me-in")
    _, headers, _ = _req(f"{base_url}/api/inbox")
    assert headers.get("Cache-Control") == "no-store"
    _, health_headers, _ = _req(f"{base_url}/healthz")
    assert health_headers.get("Cache-Control") == "no-store", "a cached /healthz would lie to deploy.sh"


# ---- the cookie must not be the passcode ------------------------------------------------------

def test_the_session_cookie_is_not_the_passcode(base_url, monkeypatch):
    secret = "correct-horse-battery-staple"
    monkeypatch.setenv("BEAN_PASSCODE", secret)
    status, headers, _ = _req(f"{base_url}/api/auth", method="POST", body=f'{{"passcode": "{secret}"}}')
    assert status == 200
    set_cookie = headers.get("Set-Cookie")
    assert secret not in set_cookie, "the passcode was written verbatim into a Set-Cookie header"
    assert srv._session_token(secret) in set_cookie
    assert "HttpOnly" in set_cookie and "Secure" in set_cookie


def test_the_derived_cookie_authenticates(base_url, monkeypatch):
    secret = "correct-horse-battery-staple"
    monkeypatch.setenv("BEAN_PASSCODE", secret)
    token = srv._session_token(secret)
    assert _req(f"{base_url}/api/inbox", cookie=f"bean_auth={token}")[0] == 200


def test_the_legacy_passcode_cookie_still_authenticates(base_url, monkeypatch):
    """The operator's browser holds a cookie whose value IS the passcode. This change must not sign
    them out mid-inbox. Delete this test and the legacy arm once that cookie has rotated."""
    secret = "correct-horse-battery-staple"
    monkeypatch.setenv("BEAN_PASSCODE", secret)
    assert _req(f"{base_url}/api/inbox", cookie=f"bean_auth={secret}")[0] == 200


def test_a_wrong_cookie_is_still_refused(base_url, monkeypatch):
    monkeypatch.setenv("BEAN_PASSCODE", "correct-horse-battery-staple")
    assert _req(f"{base_url}/api/inbox", cookie="bean_auth=nope")[0] == 401


def test_rotating_the_passcode_invalidates_the_derived_token(base_url, monkeypatch):
    old = srv._session_token("old-passcode")
    monkeypatch.setenv("BEAN_PASSCODE", "new-passcode")
    assert _req(f"{base_url}/api/inbox", cookie=f"bean_auth={old}")[0] == 401
