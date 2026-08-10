"""Offline tests for bean/coldstart.py — the first notebook for a store Bean has never seen.

Cold start is what onboarding tenant #2 rests on, and its failure modes are quiet ones: a notebook
that renders but asserts nothing, or worse, one that invents precedent. So the properties under test
are mostly about what must NOT be there.
"""

from __future__ import annotations

from bean.coldstart import demo_notebook, facts_from_docs, notebook_from_config
from bean.config import Config
from bean.fixtures import DEMO_CLIFFS, DEMO_STORE
from bean.notebook import loads


def test_cold_start_has_no_observed_notes():
    """The one that matters. `observed` outranks `stated` policy in the drafting prompt because it is
    precedent — what the operator actually did. A brand-new store has no precedent, so any note here
    would be a forged signal in the highest-trust slot Bean has."""
    nb = demo_notebook()
    assert nb.notes == []


def test_buckets_carry_authored_cliffs_not_descriptions():
    """A cliff states behaviour; a category description states the situation. Shipping the
    description as a cliff is the tree-axis mistake — cheap to regress,
    invisible until drafts go vague."""
    nb = demo_notebook()
    cliffs = {b.name: b.cliff for b in nb.buckets}
    assert cliffs["Shipping"] == DEMO_CLIFFS["Shipping"]
    # The description is a *fallback*, so assert the authored text actually displaced it.
    descriptions = Config.from_fixtures().descriptions()
    assert cliffs["Shipping"] != descriptions["Shipping"]


def test_always_escalate_categories_are_high_stakes_and_macroless():
    """Wholesale and Escalation have no template on purpose — no standard answer exists. They must
    arrive as high-stakes buckets (never green-eligible) and must NOT acquire an invented macro."""
    nb = demo_notebook()
    stakes = {b.name: b.stakes for b in nb.buckets}
    assert stakes["Wholesale"] == "high"
    assert stakes["Escalation"] == "high"
    macro_names = {m.name for m in nb.macros}
    assert "Wholesale" not in macro_names
    assert "Escalation" not in macro_names
    # ...while the ordinary buckets stay green-eligible.
    assert stakes["Order Status"] == "normal"


def test_facts_are_all_stated_and_drop_shouting_headers():
    """Everything a store says about itself is `stated` — nothing here was observed. Header lines
    ("SHIPPING POLICY.") name a section rather than assert anything, so they must not become facts."""
    nb = demo_notebook()
    assert nb.facts, "cold start produced no facts at all"
    assert {f.provenance for f in nb.facts} == {"stated"}
    assert not any(f.text.rstrip(".:").isupper() for f in nb.facts)


def test_facts_preserve_the_docs_own_line_chunking():
    """One fact per line, verbatim — no sentence-splitting, which would sever "$75" from "otherwise
    $6" and turn a policy into two half-truths."""
    facts = facts_from_docs({"shipping": "SHIPPING POLICY.\nFree over $75, otherwise $6.\n\nShips in 3-5 days."})
    assert [f.text for f in facts] == ["Free over $75, otherwise $6.", "Ships in 3-5 days."]


def test_cold_start_notebook_round_trips_through_markdown():
    """The file IS the render (bean/notebook.py), so a cold-start notebook must survive save→load
    intact — including the high-stakes markers, which gate the one-tap approve."""
    nb = demo_notebook()
    back = loads(nb.render())
    assert back.store == DEMO_STORE
    assert [(b.name, b.cliff, b.stakes) for b in back.buckets] == [
        (b.name, b.cliff, b.stakes) for b in nb.buckets
    ]
    assert [(m.name, m.text) for m in back.macros] == [(m.name, m.text) for m in nb.macros]
    assert [(f.text, f.provenance) for f in back.facts] == [(f.text, f.provenance) for f in nb.facts]


def test_cliffs_fall_back_to_descriptions_when_unauthored():
    """A store that hasn't told us its cliffs still gets a usable draft — the approval walk is what
    turns descriptions into behaviour. Silent empty cliffs would be the worse failure."""
    config = Config.from_fixtures()
    nb = notebook_from_config(config, "Somewhere New")  # no cliffs passed
    descriptions = config.descriptions()
    assert all(b.cliff for b in nb.buckets)
    assert {b.name: b.cliff for b in nb.buckets}["Shipping"] == descriptions["Shipping"]
