"""Offline tests for bean/shelf.py — retrieval over her past replies.

The shelf's whole reason to exist is that it ranks by what the email is ABOUT and reports honestly
when it has nothing close — the fix for the old cap-3 category-join that dressed up the three most
recent replies as precedent. So the tests pin: product-term ranking, the no-neighbor signal, and the
tokenization mirror (a ≤3-char term must not count, matching KnowledgeStore.search).
"""

from __future__ import annotations

from bean.corrections import Correction
from bean.shelf import has_neighbor, top_exemplars


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
    # a query sharing only short words must not clear threshold
    assert top_exemplars(corpus, "can the box fit", k=3, threshold=2) == []
    # a query sharing the long term does
    assert top_exemplars(corpus, "a upholstery upholstery sizing question", k=3, threshold=1)


def test_same_bucket_preferred_at_equal_relevance():
    corpus = [
        _row("out", "sofa one arm loose", "troubleshoot it", bucket="Not working / broken"),
        _row("inb", "sofa one arm loose", "troubleshoot it", bucket="Returns & refunds"),
    ]
    got = top_exemplars(corpus, "sofa one arm loose", k=2, bucket="Returns & refunds")
    assert got[0].bucket == "Returns & refunds", "same-bucket exemplar should rank first on a tie"
