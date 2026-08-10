"""Scripted FakeModel builders shared across test files.

These lived in `tests/test_tree_routing.py` and were imported from there by four other suites — which
made a *test file* a de facto conftest: deleting it would have broken collection in
`test_healthz_and_limits` and `test_customer_memory` (47 tests) at import time, for reasons having
nothing to do with what those files test.

They are plain functions, not fixtures, so they live in a plain module rather than `conftest.py` —
callers still import them explicitly, which is the honest thing when there is no fixture magic to
provide.
"""

from __future__ import annotations

import re

from bean.llm import FakeModel


# ---- a scripted per-hop router ------------------------------------------------------------
# route_user puts the chunk under "UNIT OF WORK:" and the children under "BRANCH OPTIONS"; the
# router reads both and descends a keyword→path map one hop at a time (it can only pick a label
# that is actually offered this hop — exactly how the real walk constrains the model).

def _option_labels(user: str) -> list[str]:
    block = user.split("BRANCH OPTIONS")[1]
    return [m.group(1) for line in block.splitlines() if (m := re.match(r"- (.+?)(?: \[|:)", line))]


def _router(routes: dict[str, list[str]], stall_label: str | None = None):
    def fn(user: str) -> dict:
        chunk = user.split("UNIT OF WORK:")[1].splitlines()[0].strip().lower()
        key = next((k for k in routes if k in chunk), None)
        if key is None:
            return {"choice": "unsure", "confident": "no", "rationale": "no keyword match"}
        offered = _option_labels(user)
        for label in routes[key]:
            if label in offered:
                if label == stall_label:
                    return {"choice": "unsure", "confident": "no", "rationale": "scripted stall"}
                return {"choice": label, "confident": "yes", "rationale": "test"}
        return {"choice": "unsure", "confident": "no", "rationale": "exhausted"}
    return fn


def _one_chunk(text, intent="product question"):
    return FakeModel({"chunk": {"chunks": [{"text": text, "intent": intent}]}})


def _assess(coverage="full", ungrounded=None, stakes="low", citations=("src",), draft="fragment."):
    return {"asks": ["x"], "coverage": coverage, "needed_facts": ["x"],
            "ungrounded_facts": list(ungrounded or []), "stakes": stakes,
            "citations": list(citations), "draft": draft}
