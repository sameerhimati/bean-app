"""Cold start — the first notebook for a store Bean has never seen.

A notebook is normally INDUCED from an operator's real (email → reply) corrections.
A brand-new tenant has none: no precedent, no corrections log, nothing observed. That asymmetry IS
the cold-start problem, and this module is the answer to it — a DETERMINISTIC, model-free projection
of what the store has already stated about itself:

  - config categories  → situation buckets (`always_escalate` ⇒ `high` stakes)
  - config templates   → macros (the standard answers they already wrote)
  - knowledge docs     → facts, every one tagged `stated`
  - corrections        → nothing. There are none yet.

**The notes section comes out EMPTY, and that is the point.** `observed` outranks `stated` policy
precisely because it is precedent — what the operator actually DID. A cold-start notebook has no
precedent, so inventing a judgment note here would forge exactly the signal the engine trusts most.
Day zero, Bean knows what the store says and nothing about what it does; the notes fill in as the
operator corrects it. That is the learning loop's starting state, honestly rendered.

WHAT THIS IS NOT: a shipped default. `bean/paths.py` (notebook_path) forbids a notebook fixture
sitting beside a live customer, because the first save would persist a demo brain over a real one —
that is [[fixtures-leaked-into-prod]]. So nothing here runs at boot or on a missing file; the engine
still 503s rather than fabricate (`server.py:_current_notebook`). This produces a DRAFT that a human
then walks and approves (web/bean-questionnaire.jsx), and only that approval makes it theirs.
"""

from __future__ import annotations

from bean.config import Config
from bean.fixtures import DEMO_CLIFFS, DEMO_STORE, SW_STORE, sw_config
from bean.notebook import Bucket, Fact, Macro, Notebook


def _is_title(line: str) -> bool:
    """True for a doc's shouting header line — `SHIPPING POLICY.`, `SIZE & FIT GUIDE.` — which names
    the section rather than stating a fact. The doc's own key already carries the topic, so keeping
    these would add bullets that assert nothing. Trailing punctuation is ignored; a line with any
    lowercase in it is prose, not a header."""
    return line.rstrip(".:").isupper()


def facts_from_docs(docs: dict[str, str]) -> list[Fact]:
    """Knowledge docs → `stated` facts, one per non-empty, non-header line.

    Line-per-fact rather than sentence-per-fact on purpose: whoever wrote the doc already chose the
    chunking, and a line like "Domestic (US): free over $75, otherwise $6. Delivery is 3-5 business
    days after dispatch." is one coherent policy whose halves are useless apart. Sentence-splitting
    would also mangle the prices and ranges these docs are made of ($75. / 3-5.). Long bullets read
    fine to the model and stay editable by the human, which is the trade we want.
    """
    facts: list[Fact] = []
    for body in docs.values():
        for raw in body.splitlines():
            line = raw.strip()
            if not line or _is_title(line):
                continue
            facts.append(Fact(line, "stated"))
    return facts


def notebook_from_config(config, store: str, cliffs: dict[str, str] | None = None) -> Notebook:
    """Project a Config into a first-draft Notebook. No model call, no corrections, no I/O.

    `cliffs` overrides a bucket's discriminator per category name. It exists because a category
    DESCRIPTION and a bucket CLIFF are different things: the description says what the situation is
    ("customer wants to return or exchange an item"), while the cliff must say what the operator DOES
    at the boundary ("within 30 days and unworn I just do it; worn, I don't — unless it reads as a
    real defect"). The reason is that cosine can't interpolate a policy cliff, so the cliff has to
    be behaviour in prose. Descriptions are the honest fallback for a store that hasn't
    told us its cliffs yet — a starting point the approval walk exists to fix, not a finished brain.
    """
    cliffs = cliffs or {}
    buckets = [
        Bucket(
            c.name,
            (cliffs.get(c.name) or c.description).strip(),
            "high" if c.always_escalate else "normal",
        )
        for c in config.categories
    ]
    # Only categories that already carry a template become macros. A category without one (Wholesale,
    # Escalation) is a situation the operator handles by hand — giving it an invented standard answer
    # would be the yes-man failure baked into onboarding.
    macros = [Macro(c.name, c.template.strip()) for c in config.categories if c.template]
    return Notebook(
        store=store,
        buckets=buckets,
        macros=macros,
        facts=facts_from_docs(config.knowledge_docs),
        notes=[],  # no precedent on day zero — see the module docstring
    )


def demo_notebook() -> Notebook:
    """The Maple & Moss notebook — the demo tenant's brain, built the same way a real new store's
    would be. This is the surface the Loom, the public repo, and any "show me Bean" moment point at,
    which is exactly why it goes through `notebook_from_config` rather than being checked in as
    markdown: if the cold-start path ever breaks, the demo breaks with it and someone notices."""
    return notebook_from_config(Config.from_fixtures(), DEMO_STORE, DEMO_CLIFFS)


def sablewren_notebook() -> Notebook:
    """The Sable & Wren notebook — the PUBLIC DEMO tenant's brain (bean/demo.py).

    Same projection as `demo_notebook()`, run over the frozen Sable & Wren snapshot instead of the
    Maple & Moss default. See `fixtures.SW_STORE` for why the public demo is the second store and
    not the first.

    NO `cliffs` argument is passed, and that is deliberate rather than an omission: Sable & Wren's
    snapshot carries category descriptions but no authored cliff prose, so its buckets fall back to
    descriptions — the honest day-zero state this module's docstring describes. A public demo of a
    product whose whole pitch is "it tells you when it isn't sure" should be showing what a store
    gets on day one, not a hand-tuned brain nobody who clicks the link would ever have. The cliffs
    Sable & Wren *does* state live in its knowledge docs, and arrive as `stated` facts below.
    """
    return notebook_from_config(sw_config(), SW_STORE)
