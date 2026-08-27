"""The notebook history through the REAL PUT /api/notebook handler.

The claim being tested is "complete by construction": the log is written at the single writer, so
every edit path — the chat, the notebook editor, the cite sheet, the questionnaire — is recorded
without any of them opting in. That claim is worth checking rather than asserting, because it is
the whole reason the log can be trusted as an audit trail.
"""

from __future__ import annotations

import json
import threading
import urllib.error
import urllib.request
from http.server import ThreadingHTTPServer

import pytest

from bean import server as srv
from bean.notebook import Bucket, Fact, Notebook
from bean.notebook_history import load
from bean.paths import notebook_history_path


def base_notebook() -> Notebook:
    return Notebook(
        "A store",
        buckets=[Bucket("Returns", "within 90 days I send a replacement")],
        facts=[Fact("Headphone returns within 30 days.", "stated")],
    )


@pytest.fixture
def server(tmp_path, monkeypatch):
    monkeypatch.setenv("BEAN_DATA_DIR", str(tmp_path))
    monkeypatch.delenv("BEAN_PASSCODE", raising=False)
    monkeypatch.setattr(srv, "DEMO_READONLY", False)

    nb_path = tmp_path / "notebook.md"
    base_notebook().save(nb_path)
    monkeypatch.setattr(srv, "NOTEBOOK_PATH", nb_path)
    monkeypatch.setattr(srv, "NOTEBOOK_REVIEW_PATH", tmp_path / "notebook_review.json")

    httpd = ThreadingHTTPServer(("127.0.0.1", 0), srv.BeanHandler)
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    try:
        yield f"http://127.0.0.1:{httpd.server_address[1]}"
    finally:
        httpd.shutdown()
        httpd.server_close()


def _get(url, path):
    with urllib.request.urlopen(f"{url}{path}") as r:
        return r.status, json.loads(r.read()), dict(r.headers)


def _put(url, body, etag, source=None):
    headers = {"Content-Type": "application/json", "If-Match": etag}
    if source:
        headers["X-Bean-Source"] = source
    req = urllib.request.Request(
        f"{url}/api/notebook", data=json.dumps(body).encode(), method="PUT", headers=headers)
    try:
        with urllib.request.urlopen(req) as r:
            return r.status, json.loads(r.read()), dict(r.headers)
    except urllib.error.HTTPError as exc:
        return exc.code, {}, dict(exc.headers)


def _save(url, mutate, source=None):
    """GET the notebook, apply `mutate` to the dict, PUT it back with the fresh ETag."""
    _, nb, headers = _get(url, "/api/notebook")
    mutate(nb)
    return _put(url, nb, headers["ETag"], source)


def test_a_chat_confirm_is_logged_with_its_before_and_after(server):
    url = server
    status, _, _ = _save(
        url,
        lambda nb: nb["facts"].__setitem__(0, {"text": "Headphone returns within 60 days.", "provenance": "stated"}),
        source="chat")
    assert status == 200

    (row,) = load(notebook_history_path())
    assert row.source == "chat"
    assert row.action == "changed"
    assert row.section == "Store facts"
    assert row.before == "Headphone returns within 30 days."
    assert row.after == "Headphone returns within 60 days."


def test_a_hand_edit_is_logged_as_editor(server):
    """The by-construction claim. No caller opted in — the notebook editor sends no source header
    at all, and the row still lands."""
    url = server
    _save(url, lambda nb: nb["facts"].append({"text": "Gift wrap is $4.", "provenance": "stated"}))

    (row,) = load(notebook_history_path())
    assert row.source == "editor"          # the default, not a claim that Bean did it
    assert row.action == "added" and row.after == "Gift wrap is $4."


def test_an_unrecognised_source_header_falls_back_to_editor(server):
    # The log must never claim Bean made a change it cannot prove Bean made.
    url = server
    _save(url, lambda nb: nb["facts"].append({"text": "x", "provenance": "stated"}), source="totally-made-up")
    assert load(notebook_history_path())[0].source == "editor"


def test_saving_an_unchanged_notebook_logs_nothing(server):
    url = server
    _, nb, headers = _get(url, "/api/notebook")
    assert _put(url, nb, headers["ETag"])[0] == 200
    assert load(notebook_history_path()) == []
    assert not notebook_history_path().exists()


def test_the_history_endpoint_returns_newest_first(server):
    url = server
    _save(url, lambda nb: nb["facts"].append({"text": "first", "provenance": "stated"}))
    _save(url, lambda nb: nb["facts"].append({"text": "second", "provenance": "stated"}))

    status, body, _ = _get(url, "/api/notebook/history")
    assert status == 200
    assert body["count"] == 2
    # "What changed recently" should not start with last month.
    assert body["changes"][0]["after"] == "second"
    assert body["changes"][1]["after"] == "first"


def test_a_rejected_save_logs_nothing(server):
    """A stale ETag never reaches the notebook, so it must never reach the history either —
    otherwise the log would show changes that did not happen."""
    url = server
    _, nb, _ = _get(url, "/api/notebook")
    nb["facts"].append({"text": "never landed", "provenance": "stated"})
    assert _put(url, nb, "obviously-stale-etag")[0] == 409
    assert load(notebook_history_path()) == []


def test_a_broken_history_log_never_costs_her_the_save(server):
    """The notebook is the artifact; the log is not. If logging fails, the save still happens."""
    url = server
    # A directory where the log file belongs — writing to it raises.
    notebook_history_path().parent.mkdir(parents=True, exist_ok=True)
    notebook_history_path().mkdir()

    status, saved, _ = _save(url, lambda nb: nb["facts"].append({"text": "kept", "provenance": "stated"}))
    assert status == 200
    assert any(f["text"] == "kept" for f in saved["facts"])   # her edit survived
