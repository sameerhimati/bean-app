"""The link-preserving paste handler (web/bean-admin.jsx htmlToTextWithLinks), tested from Python.

A real operator's product-recommendation template arrived with every product hyperlink stripped: a
rich paste puts both text/plain (anchors already flattened, hrefs gone) and text/html (hrefs intact)
on the clipboard, and a <textarea> silently takes the plain one. The handler reads the html flavor and folds each link
back in as `label (url)` — a bare URL, which every mail client auto-links on send and which survives
CopyButton's text/plain clipboard.

This is the most delicate code in the editor, so it is tested rather than trusted. The REAL function
is extracted from the shipped .jsx and executed under node, so the test cannot drift from the source
it claims to cover. jsdom is a dev-only dependency (see package.json) — it is the only way to run a
browser API like DOMParser outside a browser. If node or jsdom is missing the tests SKIP rather than
lie about coverage; run `npm install` to turn them on.
"""

from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path

import pytest

_ADMIN_JSX = Path(__file__).parent.parent / "web" / "bean-admin.jsx"
_START = "// Editors disagree"
_END = "// onPaste for a reply box"

pytestmark = pytest.mark.skipif(shutil.which("node") is None, reason="node not installed")


def _extract_source() -> str:
    src = _ADMIN_JSX.read_text(encoding="utf-8")
    start, end = src.index(_START), src.index(_END)
    return src[start:end]


def _run(html_cases: list[str]) -> list[str]:
    """Execute the shipped htmlToTextWithLinks over each html string, under jsdom's DOM.

    `DOMParser` is the only browser API the function touches, and node has no DOM of its own, so the
    harness installs jsdom's as a global before evaluating the extracted source verbatim.
    """
    harness = f"""
    const {{ JSDOM }} = require('jsdom');
    global.DOMParser = new JSDOM('').window.DOMParser;
    {_extract_source()}
    const cases = {json.dumps(html_cases)};
    console.log(JSON.stringify(cases.map(htmlToTextWithLinks)));
    """
    proc = subprocess.run(
        ["node", "--input-type=commonjs", "-e", harness],
        capture_output=True, text=True,
    )
    if proc.returncode != 0:
        if "Cannot find module 'jsdom'" in proc.stderr:
            pytest.skip("jsdom not installed — run `npm install` to cover the paste handler")
        raise AssertionError(f"node failed:\n{proc.stderr}")
    return json.loads(proc.stdout)


def test_anchor_becomes_label_and_bare_url():
    """The shape the real template actually had: product names hyperlinked to their store pages."""
    (out,) = _run(['<p><a href="https://sableandwren.example/products/harlow">The Harlow</a></p>'
                   '<p>This is affordable!</p>'])
    assert out == ("The Harlow (https://sableandwren.example/products/harlow)\n\n"
                   "This is affordable!")


def test_a_url_that_is_its_own_label_is_not_doubled():
    (out,) = _run(['<a href="https://sableandwren.example/pages/return-policy">'
                   'https://sableandwren.example/pages/return-policy</a>'])
    assert out == "https://sableandwren.example/pages/return-policy"


def test_non_http_targets_keep_the_words_and_drop_the_target():
    """A broken link in a customer's reply is worse than plain text."""
    mailto, js, rel = _run(['<a href="mailto:h@t.com">email us</a>',
                            '<a href="javascript:alert(1)">click</a>',
                            '<a href="/products/aspen">The Aspen</a>'])
    assert (mailto, js, rel) == ("email us", "click", "The Aspen")


def test_paragraphs_and_lines_differ_because_editors_do():
    """Docs wraps paragraphs in <p>; Gmail/Proton wrap each LINE in <div>. Treating them alike
    double-spaces a Gmail paste or crushes a Docs one."""
    divs, paras, runs = _run(['<div>line one</div><div>line two</div>',
                              '<p>para one</p><p>para two</p>',
                              '<div>Hi</div><br><br><br><br><div>Bye</div>'])
    assert divs == "line one\nline two"
    assert paras == "para one\n\npara two"
    assert runs == "Hi\n\nBye"  # runaway breaks collapse


def test_rich_paste_artifacts_are_cleaned():
    """The \\xa0 in her stored Meter template is the fingerprint of the rich paste that ate the links."""
    (nbsp,) = _run(["<p>meters&nbsp;we offer</p>"])
    assert nbsp == "meters we offer"
    assert "\xa0" not in nbsp


def test_script_and_style_never_reach_a_customer_reply():
    (out,) = _run(["<p>Hi</p><script>evil()</script><style>p{color:red}</style>"])
    assert out == "Hi"


def test_empty_anchor_falls_back_to_the_url():
    (out,) = _run(['<a href="https://x.com/a"></a>'])
    assert out == "https://x.com/a"


def test_meter_shaped_block_round_trips_to_her_original_spacing():
    (out,) = _run([
        '<p>Hi {Name}!</p>'
        '<p><a href="https://sableandwren.example/products/harlow">The Harlow</a></p>'
        '<p>Most affordable.</p>'
        '<p><a href="https://sableandwren.example/products/aspen">The Aspen</a></p>'
        '<p>A level up.</p>'
    ])
    assert out == (
        "Hi {Name}!\n\n"
        "The Harlow (https://sableandwren.example/products/harlow)\n\n"
        "Most affordable.\n\n"
        "The Aspen (https://sableandwren.example/products/aspen)\n\n"
        "A level up."
    )
