"""THE calibration eval for the shipped path — the notebook engine.

This replaces `test_calibration_tree.py`, which graded the routing tree until the tree was deleted.
It grades the same thing on the same terms, and reports the number that IS the product:

    GREEN-bucket precision — of the emails Bean marks green, what fraction could the operator send
    unedited? A green draft she must fix costs her more than a red, because a red is honest.

We approximate "she'd send it unedited" with the two things the goldens actually pin: the case was
*supposed* to land green, and the draft cites a source. An overconfident green and an uncited green
are both drafts she has to re-read, which is the failure the whole product exists to avoid.

Time saved is a function of how much she can trust the green bucket without checking it, so the
UPPER gate is one-sided on purpose: a red that should have been green costs a little time, a green
that should have been red costs trust. That asymmetry is right and it stays.

BOTH SIDES ARE GATED, and the reason is the most expensive bug this repo has had. The tree eval
once read `precision = len(trustworthy) / len(high) if high else 1.0` — so an EMPTY green bucket
scored 100%. The headline metric was maximized by drafting nothing, every assertion passed
vacuously on an all-flag run, and prod flagged 100% of real mail for weeks with the suite green.
`_usefulness_gate` is the lower bound that catches that, and
`test_an_all_red_run_scores_zero_not_one` proves it still bites, offline.

Deliberately graded against the DEMO store (`GOLDEN` + `demo_notebook()`), not a real customer's
seed: this eval must survive the synthetic-seed swap untouched, and a fictional store is the only
fixture that can. It also means the eval measures a COLD notebook — projected from config, zero
corrections, no judgment notes — which is the honest floor. A warm Bean should only score higher.

STATUS: this is the ported floor, not the finished article. A real notebook eval — one that grades
the buckets/cliffs the engine actually reasons over, and grades voice, not just calibration — is a
committed follow-up.
"""

from __future__ import annotations

import json
import os
from pathlib import Path

import pytest

from bean.adapter import ModelAdapter
from bean.coldstart import demo_notebook
from bean.contract import Confidence, Grounding
from bean.engine import DRAFT_TOOL, draft_email
from bean.fixtures import GOLDEN
from bean.llm import DRAFT_MODEL, FakeModel
from bean.shelf import Exemplar

# The tree spoke HIGH/LOW/FLAG; the notebook engine speaks GREEN/YELLOW/RED. The goldens are still
# written in the first vocabulary because that is what a golden case ASSERTS — the expectation, not
# the engine's verdict (see bean/contract.py). One axis maps onto the other exactly.
_AS_GROUNDING = {
    Confidence.HIGH: Grounding.GREEN,
    Confidence.LOW: Grounding.YELLOW,
    Confidence.FLAG: Grounding.RED,
}

# ---- the usefulness floor ---------------------------------------------------------------------
# Floors, not targets: each is what the current code MEASURABLY clears, with room for model drift
# across releases. Measured 2026-07-30 on a cold demo notebook, temp-0:
#
#     label_accuracy = 1.00 (8/8)    green_bucket_precision = 1.00 (3/3)    drafted = 0.75 (6/8)
#
# A two-case slip is tolerated; a collapse is not. An all-red Bean scores 0.50 / 0.00 / 0.00 and
# fails all three — verified by the offline acceptance tests at the bottom of this file.

MIN_LABEL_ACCURACY = 0.75  # two of eight goldens may land on an unaccepted label
MIN_GREEN_PRECISION = 0.8  # an EMPTY green bucket scores 0.0, so it fails here


def _accepted(case) -> tuple[Grounding, ...]:
    return tuple(_AS_GROUNDING[c] for c in (case.expected_confidence, *case.also_accepts))


def min_drafted_fraction() -> float:
    """The drafted floor, DERIVED from the goldens rather than picked.

    A golden whose accepted labels do not include RED is a case the fixtures say Bean must answer.
    Deriving it means the floor RISES the moment someone adds a golden Bean is supposed to handle:
    the bar tracks what we claim Bean does, and cannot be quietly left behind.
    """
    must_draft = [c for c in GOLDEN if Grounding.RED not in _accepted(c)]
    return len(must_draft) / len(GOLDEN)


@pytest.fixture(scope="module", autouse=True)
def _pinned_data_root(tmp_path_factory):
    """Point BEAN_DATA_DIR at an empty tmp dir for the whole eval, so the number cannot drift with
    whatever happens to sit in ./data on this machine. Empty root ⇒ this measures the AGENT."""
    root = tmp_path_factory.mktemp("eval_data")
    old = os.environ.get("BEAN_DATA_DIR")
    os.environ["BEAN_DATA_DIR"] = str(root)
    yield root
    if old is None:
        os.environ.pop("BEAN_DATA_DIR", None)
    else:
        os.environ["BEAN_DATA_DIR"] = old


@pytest.fixture(scope="module")
def results():
    """Every golden through the real engine on a cold notebook, temperature 0.

    The same `ModelAdapter` production drafts with (bean/server.py::_run_engine), pinned to temp-0
    to cut sampling noise.

    ⚠️ temp-0 is NOT determinism, and this suite has the receipts: two consecutive runs of identical
    code scored mm-1043 yellow, then red. So a one-case delta between runs is NOISE, not a signal —
    read the aggregate, and re-run before believing a regression. That is also why the goldens with
    a genuine judgement call accept more than one label: a gate that flaps is a gate that gets
    ignored, and this eval has already been ignored once (see the module docstring).
    """
    notebook = demo_notebook()
    adapter = ModelAdapter(DRAFT_MODEL, temperature=0)
    return [(c, draft_email(notebook, [], [], None, c.email, adapter)) for c in GOLDEN]


def score(results) -> dict:
    """The scoreboard. `green_bucket_precision` is 0.0 — NOT 1.0 — when the green bucket is empty.

    That `1.0` was the deepest bug in this repo: it made the headline metric of the whole eval
    maximal for a Bean that drafts NOTHING. An empty green bucket is not perfect precision, it is an
    absent product; a rate with no denominator is not a score, it is a missing measurement, and the
    honest value of a missing measurement is zero.
    """
    green = [(c, r) for c, r in results if r.confidence == Grounding.GREEN]
    trustworthy = [(c, r) for c, r in green if Grounding.GREEN in _accepted(c) and r.citations]
    correct = [(c, r) for c, r in results if r.confidence in _accepted(c)]
    drafted = [(c, r) for c, r in results if r.confidence != Grounding.RED]
    return {
        "path": "notebook (shipped)",
        "cases": len(results),
        "label_accuracy": round(len(correct) / len(results), 3) if results else 0.0,
        "drafted": len(drafted),
        "drafted_fraction": round(len(drafted) / len(results), 3) if results else 0.0,
        "green_bucket_size": len(green),
        "green_bucket_trustworthy": len(trustworthy),
        "green_bucket_precision": round(len(trustworthy) / len(green), 3) if green else 0.0,
        "per_case": {c.email.id: r.confidence.value for c, r in results},
    }


def _usefulness_gate(summary: dict) -> None:
    """The LOWER bound: is Bean any USE?

    Not a replacement for the anti-yes-man gate — an ADDITION. Bean must be honest (never green on
    a case that isn't) AND useful (actually draft the mail it is supposed to draft). With only the
    first half checked, a Bean that flagged 100% of real mail scored a perfect 1.0 while saving
    nobody anything.
    """
    assert summary["green_bucket_precision"] >= MIN_GREEN_PRECISION, (
        f"green-bucket precision {summary['green_bucket_precision']} < {MIN_GREEN_PRECISION} "
        f"(bucket size {summary['green_bucket_size']}) — an EMPTY green bucket scores 0.0, not 1.0: "
        f"a Bean that drafts nothing has not passed, it has stopped working."
    )
    assert summary["label_accuracy"] >= MIN_LABEL_ACCURACY, (
        f"label accuracy {summary['label_accuracy']} < {MIN_LABEL_ACCURACY} — Bean is landing on "
        f"labels the goldens do not accept: {summary['per_case']}"
    )
    floor = min_drafted_fraction()
    assert summary["drafted_fraction"] >= floor, (
        f"drafted fraction {summary['drafted_fraction']} < {floor} — Bean flagged mail the goldens "
        f"say it must answer. Flagging everything is not caution, it is the operator doing her own "
        f"job with extra steps: {summary['per_case']}"
    )


def _overconfidence_gate(results) -> None:
    """The UPPER bound: every green must be one the goldens accept as green.

    Extracted so the offline acceptance test can prove it still bites a Bean that games the
    usefulness floor by drafting confidently on everything.
    """
    green = [(c, r) for c, r in results if r.confidence == Grounding.GREEN]
    overconfident = [c.email.id for c, r in green if Grounding.GREEN not in _accepted(c)]
    assert not overconfident, f"green on emails that should not be green: {overconfident}"


@pytest.mark.live
def test_notebook_calibration_on_the_goldens(results):
    summary = score(results)
    scoreboard = "\n".join(
        f"  {'OK ' if r.confidence in _accepted(c) else 'XX '}{c.email.id:16} "
        f"expected={'|'.join(x.value for x in _accepted(c)):14} got={r.confidence.value:6} "
        f"cited={len(r.citations or [])}"
        for c, r in results
    )
    print(f"\n{scoreboard}\n\nEVAL SUMMARY {json.dumps(summary, sort_keys=True)}")
    if out := os.environ.get("BEAN_EVAL_OUT"):
        Path(out).write_text(json.dumps(summary, indent=2, sort_keys=True), encoding="utf-8")

    # UPPER bound first — the trust invariant is the doctrine's priority, and it names the yes-man
    # by email id. (A yes-man also dilutes the precision floor below, so if the lower gate ran first
    # an overconfidence regression would report as a confusing "precision too low".)
    _overconfidence_gate(results)
    # LOWER bound — is Bean useful?
    _usefulness_gate(summary)


@pytest.mark.live
def test_green_drafts_are_sourced(results):
    """No green draft without a cited source — the anti-fabrication gate, on the shipped path."""
    uncited = [c.email.id for c, r in results if r.confidence == Grounding.GREEN and not r.citations]
    assert not uncited, f"green drafts with no source cited: {uncited}"


# ---- THE ACCEPTANCE TEST: an all-red run must FAIL this eval ----------------------------------
# Offline (FakeModel, no tokens), because a gate you only exercise when you have an API key is a
# gate you do not have. It runs the REAL gates over the REAL engine and asserts the eval goes RED.
# If this ever passes by the gate NOT raising, the blindness is back.

def _all_red_results():
    """Every golden, answered by a model that escalates everything — the 0/49 Bean that shipped."""
    notebook = demo_notebook()
    model = FakeModel({DRAFT_TOOL["name"]: {
        "bucket": "Escalation", "draft": "", "citations": [],
        "confidence": "red", "why_unsure": ["me know nothin' about this one"],
    }})
    return [(c, draft_email(notebook, [], [], None, c.email, model)) for c in GOLDEN]


def test_an_all_red_run_scores_zero_not_one():
    """The bug, pinned. `precision = ... if green else 1.0` scored this run a PERFECT 1.0."""
    summary = score(_all_red_results())
    assert summary["green_bucket_size"] == 0
    assert summary["green_bucket_precision"] == 0.0, "an empty green bucket must not score 1.0"
    assert summary["drafted_fraction"] == 0.0


def test_an_all_red_run_fails_the_usefulness_gate():
    """The gate itself must RAISE on the all-red run — not merely score it badly."""
    with pytest.raises(AssertionError, match="has stopped working"):
        _usefulness_gate(score(_all_red_results()))


def test_the_floor_does_not_licence_a_yes_man():
    """A Bean that games the usefulness floor by drafting confident greens on everything must still
    fail the UPPER gate. Usefulness never buys permission to be overconfident."""
    notebook = demo_notebook()
    model = FakeModel({DRAFT_TOOL["name"]: {
        "bucket": "Order Status", "draft": "Absolutely, all set!",
        "citations": ["notebook:Order Status"], "confidence": "green", "why_unsure": [],
    }})
    # A NEIGHBOUR is supplied deliberately. Without one the engine's empty-shelf rule now caps every
    # draft at yellow, so this yes-man could never produce a green and the gate under test would
    # never fire — the test would pass while measuring nothing. Giving it grounding is what keeps
    # this an honest test OF THE GATE. (That the rule defuses a yes-man on its own is the point of
    # the rule; it is pinned in tests/test_engine.py, not here.)
    shelf = [Exemplar(email_id="past", subject="s", body="b", reply="her past reply",
                      bucket=None, score=0.9)]
    results = [(c, draft_email(notebook, shelf, [], None, c.email, model)) for c in GOLDEN]
    with pytest.raises(AssertionError, match="should not be green"):
        _overconfidence_gate(results)
