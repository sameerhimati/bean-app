"""The public beta waitlist (GET /join + POST /api/waitlist).

This endpoint is the one place Bean takes a write from someone WITHOUT a passcode — which is the
exact combination the plan refused to build for the auth-lockout table: an unauthenticated write is
a way to fill the disk the operator's corrections live on. So the tests that matter here are not "does a
signup save" (it does) but the guardrails: a flood is refused before it writes, a bad email never
lands, and a signup is never told it succeeded when the write failed.
"""

from __future__ import annotations

import json
import threading
import urllib.error
import urllib.request
from http.server import ThreadingHTTPServer

import pytest

from bean import server as srv
from bean.paths import waitlist_path


@pytest.fixture(autouse=True)
def _reset_windows():
    srv._waitlist_hits.clear()
    yield
    srv._waitlist_hits.clear()


@pytest.fixture
def base(tmp_path, monkeypatch):
    monkeypatch.setenv("BEAN_DATA_DIR", str(tmp_path))
    monkeypatch.delenv("BEAN_PASSCODE", raising=False)  # prove it works with NO passcode set
    httpd = ThreadingHTTPServer(("127.0.0.1", 0), srv.BeanHandler)
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    try:
        yield f"http://127.0.0.1:{httpd.server_address[1]}", tmp_path
    finally:
        httpd.shutdown()
        httpd.server_close()


def _post(base_url, body):
    req = urllib.request.Request(
        f"{base_url}/api/waitlist", data=json.dumps(body).encode(),
        method="POST", headers={"Content-Type": "application/json"},
    )
    try:
        with urllib.request.urlopen(req) as r:
            return r.status, json.loads(r.read().decode() or "null")
    except urllib.error.HTTPError as e:
        return e.code, json.loads(e.read().decode() or "null")


def _get(url):
    try:
        with urllib.request.urlopen(url) as r:
            return r.status, r.read().decode()
    except urllib.error.HTTPError as e:
        return e.code, e.read().decode()


def _rows(tmp):
    p = waitlist_path()
    return [json.loads(l) for l in p.read_text().splitlines() if l.strip()] if p.exists() else []


# ---- the landing page is public --------------------------------------------------------------

def test_join_page_is_public_even_with_a_passcode_set(base, monkeypatch):
    base_url, _ = base
    monkeypatch.setenv("BEAN_PASSCODE", "secret")  # the app is gated...
    status, html = _get(f"{base_url}/join")
    assert status == 200                            # ...but the beta invite is not
    assert "Get on the list" in html
    assert "og:title" in html  # the shared link previews as Bean


# ---- a real signup lands ---------------------------------------------------------------------

def test_a_signup_is_saved_to_the_volume(base):
    base_url, tmp = base
    status, body = _post(base_url, {"email": "sam@acme.com"})
    assert status == 200 and body == {"ok": True}
    rows = _rows(tmp)
    assert len(rows) == 1
    assert rows[0]["email"] == "sam@acme.com"
    assert "ts" in rows[0]


def test_an_optional_note_is_captured(base):
    base_url, tmp = base
    _post(base_url, {"email": "sam@acme.com", "note": "i run a 3-person support desk"})
    assert _rows(tmp)[0]["note"] == "i run a 3-person support desk"


# ---- the guardrails (the reason this file exists) --------------------------------------------

def test_a_bad_email_never_lands(base):
    base_url, tmp = base
    for bad in ["", "nope", "a@b", "a b@c.com", "x@y."]:
        status, body = _post(base_url, {"email": bad})
        assert status == 400, f"{bad!r} was accepted"
        assert body["ok"] is False
    assert _rows(tmp) == []  # nothing bad reached the volume


def test_a_duplicate_does_not_double_write(base):
    base_url, tmp = base
    assert _post(base_url, {"email": "sam@acme.com"})[0] == 200
    assert _post(base_url, {"email": "SAM@acme.com"})[0] == 200  # case-insensitive
    assert len(_rows(tmp)) == 1


def test_a_flood_is_refused_before_it_writes(base, monkeypatch):
    base_url, tmp = base
    monkeypatch.setattr(srv, "_WAITLIST_MAX_PER_MIN", 3)
    codes = [_post(base_url, {"email": f"user{i}@acme.com"})[0] for i in range(5)]
    assert codes == [200, 200, 200, 429, 429]
    assert len(_rows(tmp)) == 3, "a rate-limited signup still hit the disk"


def test_a_failed_write_is_never_reported_as_success(base, monkeypatch):
    """The whole session's rule, applied here: a signup that did not save must not return ok."""
    base_url, _ = base

    def boom(*a, **k):
        raise OSError("disk full")
    monkeypatch.setattr(srv.Path, "open", boom)

    status, body = _post(base_url, {"email": "sam@acme.com"})
    assert status == 503 and body["ok"] is False


def test_the_email_is_length_capped(base):
    base_url, tmp = base
    huge = "a" * 300 + "@acme.com"
    assert _post(base_url, {"email": huge})[0] == 400
    assert _rows(tmp) == []
