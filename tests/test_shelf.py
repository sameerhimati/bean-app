"""Offline tests for bean/shelf.py — retrieval over her past replies.

The shelf's whole reason to exist is that it ranks by what the email is ABOUT and reports honestly
when it has nothing close — the fix for the old cap-3 category-join that dressed up the three most
recent replies as precedent. So the tests pin: product-term ranking, the no-neighbor signal, and the
tokenization mirror (a ≤3-char term must not count, matching KnowledgeStore.search).
"""

from __future__ import annotations

from bean.corrections import Correction
from bean.shelf import has_neighbor, thread_key, top_exemplars


def _row(email_id: str, body: str, reply: str, bucket: str | None = None) -> Correction:
    meta = {"email_subject": "", "email_body": body}
    if bucket:
        meta["bucket"] = bucket
    return Correction(email_id=email_id, category="X", confidence="high", action="teach",
                      final_text=reply, meta=meta)


def test_ranks_by_product_overlap():
    corpus = [
        _row("a", "my mattress has a dip and a ridge", "rotate the mattress head to foot"),
        _row("b", "which sofa fabric suits a dog", "the performance weave suits it"),
    ]
    got = top_exemplars(corpus, "the mattress keeps dipping into a ridge", k=2)
    assert got, "should find a neighbor"
    assert got[0].email_id == "a", "the mattress reply must outrank the sofa reply"


def test_no_neighbor_below_threshold_is_empty():
    # An email about something she has never answered has no grounding — the shelf must SAY so
    # (empty), not return the least-irrelevant row.
    corpus = [_row("a", "mattress dip", "rotate it")]
    got = top_exemplars(corpus, "do you offer corporate net-30 invoicing terms", k=3)
    assert got == []
    assert has_neighbor(got) is False


def test_short_terms_do_not_count():
    # "fit"/"the" are ≤3 chars; only "upholstery" (>3) should drive the match — the tokenizer mirror.
    corpus = [_row("a", "upholstery question about the fit", "here is the sizing")]
    # a query sharing only short words must not clear threshold (scores are 0..1 since 2026-08-14)
    assert top_exemplars(corpus, "can the box fit", k=3) == []
    # a query sharing the long terms does
    assert top_exemplars(corpus, "a upholstery sizing question", k=3)


def test_same_bucket_preferred_at_equal_relevance():
    corpus = [
        _row("out", "sofa one arm loose", "troubleshoot it", bucket="Not working / broken"),
        _row("inb", "sofa one arm loose", "troubleshoot it", bucket="Returns & refunds"),
    ]
    got = top_exemplars(corpus, "sofa one arm loose", k=2, bucket="Returns & refunds")
    assert got[0].bucket == "Returns & refunds", "same-bucket exemplar should rank first on a tie"


# ---- the 2026-08-14 rewrite: a threshold that can actually fire -------------------------------

def test_the_no_neighbour_signal_actually_fires():
    """The regression that mattered. The old scorer summed raw occurrence counts of every 4+ char
    token against a bar of 2, so any two real emails cleared it on function words alone — across 147
    production emails and 203 replayed ones it returned the full k=3 EVERY time. The honesty signal
    this module promises was unreachable, and nothing failed; Bean just quietly always had
    "precedent"."""
    corpus = [_row("a", "my mattress has a dip and a ridge after six weeks of use",
                   "rotate the mattress head to foot every month")]
    # Shares ordinary words ("about", "your", "have") and nothing that matters.
    got = top_exemplars(corpus, "do you have information about your wholesale invoicing terms")
    assert got == [] and has_neighbor(got) is False


def test_scores_are_bounded_similarities_not_raw_counts():
    """0..1 is what makes a threshold mean anything: a count of 13 answers "13 what?" — you cannot
    say whether it is close without knowing the corpus."""
    corpus = [_row("a", "mattress dip ridge", "rotate it")]
    got = top_exemplars(corpus, "mattress dip ridge", k=1)
    assert got and 0.0 < got[0].score <= 1.0


def test_a_long_rambling_exemplar_does_not_win_on_length_alone():
    """Raw counts rewarded wordiness: a rambling past reply outscored a precise one simply by
    containing more words, so the shelf drifted toward whichever exemplars were longest."""
    corpus = [
        _row("tight", "mattress ridge", "rotate it"),
        _row("rambling", "mattress " * 40 + "sofa rugs curtains lighting delivery wholesale", "..."),
    ]
    got = top_exemplars(corpus, "mattress ridge", k=2)
    assert got[0].email_id == "tight"


def test_an_email_never_cites_its_own_past_reply():
    corpus = [_row("e1", "mattress dip ridge problem", "rotate it")]
    assert top_exemplars(corpus, "mattress dip ridge problem", exclude_id="e1") == []


def test_an_email_never_cites_its_own_conversation():
    """Excluding by id alone caught 38 of 47 real cases — a follow-up in a thread is a different
    email_id, so the same conversation walked straight back in as its own precedent. The engine
    already puts the thread in the prompt separately, so those slots were spent showing the model
    something it had already been shown."""
    corpus = [Correction(email_id="earlier", category="X", confidence="high", action="teach",
                         final_text="here is the answer",
                         meta={"email_subject": "Headphones issue", "email_body": "mattress dip ridge"})]
    got = top_exemplars(corpus, "mattress dip ridge", exclude_id="later",
                        exclude_subject="RE: [EXTERNAL] Re: Headphones issue")
    assert got == [], "the same conversation is a mirror, not precedent"


def test_a_different_conversation_is_still_retrieved():
    """The exclusion must not eat real precedent — it is scoped to THIS conversation only."""
    corpus = [Correction(email_id="other", category="X", confidence="high", action="teach",
                         final_text="rotate it",
                         meta={"email_subject": "Mattress ridge", "email_body": "mattress dip ridge"})]
    got = top_exemplars(corpus, "mattress dip ridge", exclude_id="later",
                        exclude_subject="Re: Headphones issue")
    assert [e.email_id for e in got] == ["other"]


def test_thread_key_peels_a_whole_accreted_chain():
    assert thread_key("RE: [EXTERNAL] Re: Fwd: Headphones issue") == "headphones issue"
    assert thread_key("Headphones issue") == thread_key("Re: Headphones issue")
    assert thread_key(None) == ""
