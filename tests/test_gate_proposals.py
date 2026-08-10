"""The gate learns — the loop from a logged mis-file to a rule that actually changes a decision.

Until now the gate was the one part of Bean that could not learn. The UI called `misfile` /
`keep-filed` "the gate's training signal" and the log dutifully recorded them, but `needs_reply()`
only ever read `config.gate`, which was only ever hand-typed in Settings. The writer was right and
the consumer did not exist — Bean's recurring bug shape, applied to its own gate.

So the test that matters here is not "the aggregator groups correctly". It is the end-to-end one at
the bottom: a correction becomes a proposal, the proposal becomes a rule, and the rule changes what
the gate does — with no model call. Everything above it guards the ways that loop could go wrong.
"""

from __future__ import annotations

import json
import threading
import urllib.request
from http.server import ThreadingHTTPServer

import pytest

from bean import server as srv
from bean.config import Config
from bean.corrections import Correction
from bean.gate import needs_reply


def _c(action, sender, subject="a subject", **meta):
    """One gate-error correction as bean-root writes it."""
    return Correction(
        email_id=f"e-{sender}-{subject}", category="Filed / FYI", confidence="flag", action=action,
        meta={"sender_email": sender, "email_subject": subject, **meta},
    )


def _cfg(**gate):
    return Config(categories=(), knowledge_docs={}, settings={}, gate=gate or {})


# ---- what becomes a proposal -----------------------------------------------------------------

def test_two_sightings_propose_a_rule_in_the_direction_she_corrected():
    # should-file = Bean drafted, she wanted it filed -> alwaysFile.
    props = srv._gate_proposals(
        [_c("should-file", "rae@sableandwren.example", "orders to print 8-2"),
         _c("should-file", "rae@sableandwren.example", "Re: Mattress Foundation")], _cfg())
    by_pattern = {p["pattern"]: p for p in props}
    assert by_pattern["rae@sableandwren.example"]["direction"] == "alwaysFile"
    assert by_pattern["rae@sableandwren.example"]["count"] == 2
    assert "orders to print 8-2" in by_pattern["rae@sableandwren.example"]["examples"]
    # The domain is proposed alongside the address — a whole internal domain is a real rule.
    assert by_pattern["@sableandwren.example"]["scope"] == "domain"


def test_a_misfile_proposes_the_opposite_direction():
    # misfile = Bean filed it, she wanted a reply -> alwaysReply. The two are mirrors and must not
    # collapse into one direction.
    props = srv._gate_proposals(
        [_c("misfile", "buyer@retailpartner.com"), _c("misfile", "buyer@retailpartner.com")], _cfg())
    assert {p["direction"] for p in props} == {"alwaysReply"}


def test_one_sighting_proposes_nothing():
    # A single odd email from a domain she otherwise wants is an anecdote. Proposing on it teaches
    # her to dismiss the surface, which costs more than the missed rule.
    assert srv._gate_proposals([_c("should-file", "someone@example.com")], _cfg()) == []


def test_non_gate_actions_are_not_evidence():
    # Approving or snoozing says nothing about whether the email should have reached her at all.
    noise = [Correction(email_id=f"n{i}", category="Orders", confidence="high", action=a,
                        meta={"sender_email": "shopper@example.com"})
             for i, a in enumerate(["approve", "edit", "skip", "takeover", "teach", "keep-filed"])]
    assert srv._gate_proposals(noise * 2, _cfg()) == []


def test_corrections_without_a_sender_are_skipped_not_crashed():
    # Every correction predating sender capture looks like this. The surface must start empty
    # rather than explode on the 91 rows already on the live volume.
    old = [Correction(email_id="old", category="Filed / FYI", confidence="flag", action="should-file",
                      meta={"email_subject": "no sender here"})] * 3
    assert srv._gate_proposals(old, _cfg()) == []


def test_a_consumer_domain_is_never_proposed_as_a_domain_rule():
    """The one that would have shipped a disaster.

    Running this surface on demo data offered "always file everything from @yahoo.com" — a domain
    where her actual CUSTOMERS live. Two filed emails from a consumer provider are two customers,
    not a pattern, and accepting that rule would bury real support mail silently. The specific
    address stays proposable; only the domain roll-up is suppressed.
    """
    rows = [_c("should-file", "dale.whitcomb@yahoo.com", "one"),
            _c("should-file", "marcia.reed@yahoo.com", "two"),
            _c("should-file", "someone@gmail.com", "three"),
            _c("should-file", "another@gmail.com", "four")]
    patterns = {p["pattern"] for p in srv._gate_proposals(rows, _cfg())}
    assert "@yahoo.com" not in patterns and "@gmail.com" not in patterns


def test_a_repeat_offender_on_a_consumer_domain_is_still_proposed_by_address():
    # Suppressing the domain must not suppress the sender — one noisy individual is a real rule.
    rows = [_c("should-file", "noreply@gmail.com", "one"), _c("should-file", "noreply@gmail.com", "two")]
    assert {p["pattern"] for p in srv._gate_proposals(rows, _cfg())} == {"noreply@gmail.com"}


def test_a_pattern_she_already_ruled_on_is_not_proposed_again():
    have = _cfg(alwaysFile=["rae@sableandwren.example"])
    props = srv._gate_proposals(
        [_c("should-file", "rae@sableandwren.example"), _c("should-file", "rae@sableandwren.example")],
        have)
    assert "rae@sableandwren.example" not in {p["pattern"] for p in props}


def test_contradictory_evidence_proposes_nothing():
    # She filed two from this domain and rescued two others. A rule either way would be wrong half
    # the time, and filing a real customer is the one mistake the gate may not make.
    rows = [_c("should-file", "a@mixed.com"), _c("should-file", "b@mixed.com"),
            _c("misfile", "c@mixed.com"), _c("misfile", "d@mixed.com")]
    assert "@mixed.com" not in {p["pattern"] for p in srv._gate_proposals(rows, _cfg())}


# ---- the endpoint ----------------------------------------------------------------------------

@pytest.fixture
def url_with_log(tmp_path, monkeypatch):
    cfg_path = tmp_path / "bean-config.json"
    cfg_path.write_text(json.dumps({"categories": [], "knowledge": {}, "settings": {}}), encoding="utf-8")
    monkeypatch.setattr(srv, "CONFIG_PATH", cfg_path)
    monkeypatch.setattr(srv, "CORRECTIONS_PATH", tmp_path / "corrections.jsonl")
    httpd = ThreadingHTTPServer(("127.0.0.1", 0), srv.BeanHandler)
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    try:
        yield f"http://127.0.0.1:{httpd.server_address[1]}", tmp_path / "corrections.jsonl"
    finally:
        httpd.shutdown()
        httpd.server_close()


def _post(url, body):
    req = urllib.request.Request(url, data=json.dumps(body).encode(), method="POST",
                                 headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req) as resp:
        return resp.status, json.loads(resp.read().decode())


def _get(url):
    with urllib.request.urlopen(url) as resp:
        return resp.status, json.loads(resp.read().decode())


def test_endpoint_is_empty_on_a_fresh_install(url_with_log):
    url, _ = url_with_log
    status, body = _get(f"{url}/api/gate-proposals")
    assert status == 200 and body == {"proposals": []}


def test_the_gesture_she_taps_reaches_the_endpoint(url_with_log):
    """The write path and the read path agree — corrections posted by the UI become proposals."""
    url, _ = url_with_log
    for subject in ("orders to print 8-2", "Re: Fw: Awakening Spaces Podcast"):
        status, _b = _post(f"{url}/api/correction", {
            "email_id": f"id-{subject}", "category": "Filed / FYI", "confidence": "flag",
            "action": "should-file",
            "meta": {"sender_email": "rae@sableandwren.example", "email_subject": subject,
                     "model_category": "Needs a human"},
        })
        assert status == 200

    status, body = _get(f"{url}/api/gate-proposals")
    assert status == 200
    top = body["proposals"][0]
    assert top["direction"] == "alwaysFile" and top["count"] == 2
    assert top["pattern"] in ("rae@sableandwren.example", "@sableandwren.example")


# ---- the whole point -------------------------------------------------------------------------

def test_an_accepted_proposal_changes_what_the_gate_does(url_with_log):
    """correction -> proposal -> config.gate -> a different gate decision, with NO model call.

    This is the assertion the feature exists for. Before it, every one of these corrections was
    written and read by nobody; a green suite could pass with the loop still open.
    """
    url, _ = url_with_log
    for subject in ("orders to print 8-2", "Re: Mattress Foundation"):
        _post(f"{url}/api/correction", {
            "email_id": f"x-{subject}", "category": "Filed / FYI", "confidence": "flag",
            "action": "should-file",
            "meta": {"sender_email": "rae@sableandwren.example", "email_subject": subject},
        })
    proposal = _get(f"{url}/api/gate-proposals")[1]["proposals"][0]

    class _MustNotRun:
        def structured(self, *a, **k):
            raise AssertionError("the gate called the model — an accepted rule must short-circuit")

    email = type("E", (), {"sender_name": "Rae Benavides", "sender_email": "rae@sableandwren.example",
                           "subject": "orders to print 8-5", "body": "print these", "thread": []})()

    # Before she accepts, this sender has no rule: the gate would have to ask the model.
    with pytest.raises(AssertionError):
        needs_reply(email, model=_MustNotRun(), rules={})

    # Accepting is what the admin tab does — append the pattern to that direction's list.
    accepted = {proposal["direction"]: [proposal["pattern"]]}
    result = needs_reply(email, model=_MustNotRun(), rules=accepted)
    assert result.disposition == "file" and result.kind == "rule"
    assert proposal["pattern"] in result.reason
