"""The moat, guarded structurally at the source.

Bean's learning loop must only learn from GROUNDED real mail — a reply the operator actually sent to
an actual customer. The failure this guards against is the yes-man trap: a made-up or one-off answer
becoming a reusable template that greets every future customer.

Under the notebook engine the enforcement is a shape, not a rule: the surfaces that COLLECT the
operator's judgment (the proposal card, the questionnaire walk, the citation sheet, the reply box)
are dumb — they own no fetch, no correction log, no ETag. They call props, and bean-root decides what
a confirm means. A write that leaks into any of them is a second, untested writer on their notebook
or their learning log, so each one is pinned here by its own absence of writes.

The tree-era half of this file (the teach/edit sheet's synthetic-vs-real save callbacks, paste-teach)
went with the routing tree — those surfaces no longer exist. See bean/corrections.py:few_shot_examples
for the backstop gate (already pinned by tests/test_fewshot.py).
"""

from __future__ import annotations

from pathlib import Path

_WEB = Path(__file__).parent.parent / "web"


def _strip_comments(src: str) -> str:
    """Drop // line-comment tails so the warning comments (which name `record`/`fetch`) don't count
    as call sites. The sheet has no `//` inside string literals, so this is safe here."""
    return "\n".join(line.split("//")[0] for line in src.splitlines())


def test_the_cite_sheet_never_persists_or_fetches_directly():
    """The citation-chip sheet reads a past reply and writes a notebook fix — both through props.
    If it grew its own fetch there would be two writers on the notebook (two ETags, one silent
    clobber) and a second, untested read path onto her mail. Keep the one seam."""
    code = _strip_comments((_WEB / "bean-cite.jsx").read_text(encoding="utf-8"))
    for forbidden in ("record(", "recordCorrection", "beanStore", "fetch(", "api/"):
        assert forbidden not in code, (
            f"the cite sheet must only call its onSaveNotebook/onLoadReply props — found {forbidden!r}")


def test_the_proposal_card_never_persists_or_fetches_directly():
    """The proposal card is the idiom every notebook change arrives through, so it is the highest-
    leverage place for a write to leak in. It must only call onConfirm/onFix/onDecline — the CALLER
    decides what a confirm means (a questionnaire keeps it in a working copy; the diary PUTs it)."""
    code = _strip_comments((_WEB / "bean-proposal.jsx").read_text(encoding="utf-8"))
    for forbidden in ("record(", "recordCorrection", "beanStore", "fetch(", "api/"):
        assert forbidden not in code, (
            f"the proposal card must only call its props — found {forbidden!r}")


def test_the_questionnaire_never_persists_and_commits_exactly_once():
    """The walk holds a WORKING COPY and hands it back whole at the end — one ETag'd PUT, which is
    her approval. If it grew a fetch (or called onComplete per card) the ETag would rotate a dozen
    times mid-walk and Bean would start drafting from a half-approved notebook."""
    src = (_WEB / "bean-questionnaire.jsx").read_text(encoding="utf-8")
    code = _strip_comments(src)
    for forbidden in ("record(", "beanStore", "fetch(", "api/", "If-Match"):
        assert forbidden not in code, (
            f"the questionnaire must only call its onComplete/onClose props — found {forbidden!r}")
    # exactly one call site, and it is the one inside finish() — the last card's commit
    assert code.count("onComplete(") == 1, "onComplete must be called from ONE place (finish), never per card"
    # The resume store is separate and rides a PROP too (onSaveProgress), never a fetch from here —
    # so the NOTEBOOK still commits once while her answers-so-far save every tap. The two must not
    # merge: saving progress is not approving the notebook.
    assert "onSaveProgress" in code, "per-card resume saves must ride a prop, not a fetch"

    # and bean-root wires the single commit to the same saveNotebook every other surface uses, and
    # the per-tap resume saves to beanStore.saveReviewProgress (the fetch lives there, not the view)
    root = _strip_comments((_WEB / "bean-root.jsx").read_text(encoding="utf-8"))
    assert "onComplete: nb => saveNotebook(nb)" in root
    assert "onSaveProgress: window.beanStore.saveReviewProgress" in root


def test_the_reply_box_never_persists_and_never_forges_a_template():
    """BeanReply is the ONE surface left that turns a single real email into training signal, so it
    inherits the whole moat. It must stay dumb (no record/fetch of its own — it calls onSave), and
    the callback behind it must log her reply as ONE grounded exemplar and write nothing reusable.
    The tree era's version of this mistake minted a node per taught email; the notebook era's would
    be appending to the notebook's macros from one customer's reply. Neither may return."""
    root_src = (_WEB / "bean-root.jsx").read_text(encoding="utf-8")

    # (1) the component is a view: it takes a reply and hands it up
    start = root_src.index("function BeanReply({")
    body = _strip_comments(root_src[start:root_src.index("window.BeanReply = BeanReply;")])
    for forbidden in ("record(", "recordCorrection", "beanStore", "fetch(", "api/"):
        assert forbidden not in body, (
            f"BeanReply must only call its onSave/onClose props — found {forbidden!r}")

    # (2) the callback logs exactly one exemplar, and touches nothing reusable
    start = root_src.index("function onReplyFromEmail(email, reply) {")
    cb = _strip_comments(root_src[start:root_src.index("\n  }", start)])
    assert cb.count("record(") == 1, "one real reply is one exemplar — no more, no fewer"
    assert "'teach'" in cb, "it must log as a teach correction (what the retrieval shelf reads back)"
    for forbidden in ("saveNotebook", "setNotebook", "macros", "setConfig"):
        assert forbidden not in cb, (
            f"teaching from ONE email must not write anything reusable — found {forbidden!r}")
