"""The shared email renderer's tokenizer (web/bean-email.jsx tokenizeEmailText), tested from Python.

EmailBody shows a sample email or a drafted reply the way the customer sees it — bare URLs become
links, `#SW-1234` order refs become Shopify admin deep-links. The riskiest part is the tokenizer
that splits text into those pieces (a greedy URL match or a mis-anchored order pattern would either
swallow trailing punctuation into a link or link the wrong thing). So the REAL shipped function is
required under node and exercised directly — the test cannot drift from the source it covers. node
is dev-only; if it is missing the test SKIPS rather than lie about coverage.
"""

from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path

import pytest

_EMAIL_JSX = Path(__file__).parent.parent / "web" / "bean-email.jsx"

pytestmark = pytest.mark.skipif(shutil.which("node") is None, reason="node not installed")


def _tokenize(cases: list[tuple[str, str]]) -> list[list[dict]]:
    """Run the shipped tokenizeEmailText over each (text, store) pair under node."""
    harness = f"""
    const {{ tokenizeEmailText }} = require({json.dumps(str(_EMAIL_JSX))});
    const cases = {json.dumps(cases)};
    console.log(JSON.stringify(cases.map(([t, s]) => tokenizeEmailText(t, s))));
    """
    proc = subprocess.run(
        ["node", "--input-type=commonjs", "-e", harness], capture_output=True, text=True,
    )
    if proc.returncode != 0:
        raise AssertionError(f"node failed:\n{proc.stderr}")
    return json.loads(proc.stdout)


def test_bare_url_becomes_a_link_and_keeps_surrounding_text():
    (toks,) = _tokenize([("Verify here: https://x/verify?c=9", "sable-and-wren")])
    assert toks == [
        {"type": "text", "value": "Verify here: "},
        {"type": "url", "value": "https://x/verify?c=9", "href": "https://x/verify?c=9"},
    ]


def test_trailing_sentence_punctuation_is_not_part_of_the_link():
    (toks,) = _tokenize([("see https://foo.com. Thanks", "sable-and-wren")])
    kinds = [(t["type"], t["value"]) for t in toks]
    assert ("url", "https://foo.com") in kinds
    assert ("text", ".") in kinds  # the period stayed out of the href


def test_order_ref_becomes_a_store_scoped_deep_link():
    (toks,) = _tokenize([("about order #SW-10482 please", "sable-and-wren")])
    order = next(t for t in toks if t["type"] == "order")
    assert order["value"] == "#SW-10482"
    assert order["href"] == (
        "https://admin.shopify.com/store/sable-and-wren/orders?query=SW-10482"
    )


def test_order_ref_without_a_store_is_plain_text_not_a_dead_link():
    (toks,) = _tokenize([("order #SW-10482", "")])
    order = next(t for t in toks if t["type"] == "order")
    assert order["href"] is None  # no store configured → the UI renders it as plain text


def test_plain_text_stays_a_single_text_token():
    (toks,) = _tokenize([("no links here at all", "sable-and-wren")])
    assert toks == [{"type": "text", "value": "no links here at all"}]
