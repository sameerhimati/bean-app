"""Offline tests for bean/engine.py + the DraftResult contract — one call, coerced DOWN only.

The engine's safety property is the whole project in miniature: a draft it can't ground must never
wear a confident (green) face. These pin that coercion is monotonic (green→yellow when uncited,
never the reverse), that an off-list bucket falls to escalation, and that the DraftResult contract
refuses to construct a green with no citation. All offline via FakeModel — no network, no key.
"""

from __future__ import annotations

import pytest

from bean.contract import DraftResult, Email, Grounding
from bean.engine import DRAFT_TOOL, ESCALATE_BUCKET, draft_email
from bean.llm import FakeModel
from bean.notebook import Bucket, Notebook
from bean.shelf import Exemplar


def _nb() -> Notebook:
    return Notebook("S", buckets=[Bucket("Returns & refunds", "30 days, original condition")])


def _email() -> Email:
    return Email(id="e1", sender_name="A", sender_email="a@x.com", subject="return", body="can I return this?")


def _shelf() -> list[Exemplar]:
    """One neighbour, so these tests exercise the CITATION rule in isolation. An empty shelf is its
    own downgrade now (see test_green_on_an_empty_shelf_is_never_one_tap), and passing [] here would
    make every green-path test pass for the wrong reason."""
    return [Exemplar(email_id="past1", subject="return", body="can I send this back?",
                     reply="Of course — within 30 days.", bucket=None, score=0.42)]


def _run(model_output: dict, exemplars: list[Exemplar] | None = None) -> DraftResult:
    model = FakeModel({DRAFT_TOOL["name"]: model_output})
    shelf = _shelf() if exemplars is None else exemplars
    return draft_email(_nb(), shelf, [], None, _email(), model)


def test_green_with_citation_stays_green():
    r = _run({"bucket": "Returns & refunds", "draft": "Yes, within 30 days.",
              "citations": ["notebook:Returns & refunds"], "confidence": "green", "why_unsure": []})
    assert r.confidence == Grounding.GREEN
    assert r.email_id == "e1"


def test_green_without_citation_is_coerced_to_yellow():
    # The core safety coercion: a green it can't cite is not grounded, so it drops — never raises.
    r = _run({"bucket": "Returns & refunds", "draft": "Yes, within 30 days.",
              "citations": [], "confidence": "green", "why_unsure": []})
    assert r.confidence == Grounding.YELLOW
    assert any("grounded" in w for w in r.why_unsure)


def test_high_stakes_bucket_caps_grounded_green_at_yellow():
    # The calibration guardrail: a money/commitment bucket never one-taps, even fully grounded —
    # it's held at yellow for her to eyeball. This is the whole trust model in one coercion.
    nb = Notebook("S", buckets=[Bucket("Returns & refunds", "30 days", stakes="high")])
    model = FakeModel({DRAFT_TOOL["name"]: {"bucket": "Returns & refunds", "draft": "Yes, within 30 days.",
                                            "citations": ["notebook:Returns & refunds"],
                                            "confidence": "green", "why_unsure": []}})
    r = draft_email(nb, [], [], None, _email(), model)
    assert r.confidence == Grounding.YELLOW
    assert any("high-stakes" in w for w in r.why_unsure)


def test_normal_bucket_keeps_grounded_green():
    # Same grounded draft, a normal bucket — one-tap green survives. The gate is stakes, nothing else.
    r = _run({"bucket": "Returns & refunds", "draft": "Yes, within 30 days.",
              "citations": ["notebook:Returns & refunds"], "confidence": "green", "why_unsure": []})
    assert r.confidence == Grounding.GREEN


def test_offlist_bucket_falls_to_escalation():
    r = _run({"bucket": "Some bucket the notebook never defined", "draft": "",
              "citations": [], "confidence": "red", "why_unsure": ["not mine"]})
    assert r.bucket == ESCALATE_BUCKET


def test_red_without_reason_does_not_crash():
    # A model that returns red but forgets why_unsure must still yield a valid DraftResult.
    r = _run({"bucket": "Needs a human", "draft": "", "citations": [], "confidence": "red", "why_unsure": []})
    assert r.confidence == Grounding.RED
    assert r.why_unsure, "engine must supply a reason so the contract holds"


def test_exemplars_and_notebook_reach_the_model():
    model = FakeModel({DRAFT_TOOL["name"]: {"bucket": "Returns & refunds", "draft": "ok",
                                            "citations": ["notebook:Returns & refunds"],
                                            "confidence": "green", "why_unsure": []}})
    ex = Exemplar(email_id="past1", subject="s", body="b", reply="her past reply", bucket=None, score=5)
    draft_email(_nb(), [ex], [], None, _email(), model)
    call = model.calls[0]
    assert "past1" in call["user"], "the exemplar id must be in the user message for citation"
    assert "her past reply" in call["user"]
    joined = " ".join(b["text"] for b in call["system"])
    assert "30 days, original condition" in joined, "the notebook must be in the system prefix"


def test_contract_rejects_uncited_green():
    with pytest.raises(ValueError):
        DraftResult("e", "B", Grounding.GREEN, draft="hi", citations=[])


def test_contract_rejects_yellow_without_reason():
    with pytest.raises(ValueError):
        DraftResult("e", "B", Grounding.YELLOW, draft="hi", citations=[], why_unsure=[])


# ---- the empty-shelf rule --------------------------------------------------------------------

def test_green_on_an_empty_shelf_is_never_one_tap():
    """The asymmetry this closes: the CITATION rule was enforced in code, but "you have nothing of
    hers to lean on" was only ever a sentence in the prompt — one honesty guarantee a rule, the
    other a polite request, in a product whose whole thesis is not trusting the model's self-report.

    An empty shelf means she has never answered anything like this. The notebook may still cover it,
    so this is not an automatic red — but green means "approve blind", and that is a promise she has
    made this exact call before."""
    r = _run({"bucket": "Returns & refunds", "draft": "Yes, within 30 days.",
              "citations": ["notebook:Returns & refunds"], "confidence": "green", "why_unsure": []},
             exemplars=[])
    assert r.confidence == Grounding.YELLOW
    assert any("no close past reply" in w for w in r.why_unsure), \
        "the downgrade must say WHY — an unexplained yellow is not calibration"


def test_an_empty_shelf_does_not_force_red():
    """Coercion stays monotonic and minimal: the rule removes the one-tap claim, it does not throw
    away a usable draft. Over-refusing is its own failure — a Bean that flags everything saves no
    time (see bean/outcomes.py)."""
    r = _run({"bucket": "Returns & refunds", "draft": "Yes, within 30 days.",
              "citations": ["notebook:Returns & refunds"], "confidence": "yellow",
              "why_unsure": ["not sure on timing"]}, exemplars=[])
    assert r.confidence == Grounding.YELLOW
    assert r.draft == "Yes, within 30 days."


def test_a_shelf_does_not_rescue_an_uncited_green():
    """Both rules apply independently — having a neighbour is not a substitute for citing one."""
    r = _run({"bucket": "Returns & refunds", "draft": "Yes.", "citations": [],
              "confidence": "green", "why_unsure": []})
    assert r.confidence == Grounding.YELLOW
