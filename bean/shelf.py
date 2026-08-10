"""The shelf — retrieval over her own past replies.

~45% of her mail is product questions won by retrieval, not by any tree. The shelf pulls her
closest past replies to *this kind of email*, ranked by product-term overlap, and hands them to the
one drafting call as grounded exemplars. It fixes the concrete bug in the old selector
(`bean/corrections.few_shot_examples`, cap-3 category-join that degenerates to most-recent-3 on a
fat bucket): here ranking is by what the email is ABOUT, and "no neighbor above threshold" is a
first-class signal — it drops the draft to yellow/red rather than dressing up the three most recent
replies as precedent.

Tokenization deliberately MIRRORS `KnowledgeStore.search` (`bean/sources.py:38-45`): lowercase,
maximal alpha runs, drop tokens ≤ 3 chars (a length-based stopword proxy), raw occurrence count.
Same corpus, same notion of "about the same thing" — so the shelf and the knowledge store can't
develop two different ideas of relevance.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

from bean.corrections import Correction, _is_voice_exemplar

# Minimum product-term overlap for a past reply to count as a real neighbor. Below this the shelf
# reports no neighbor — the honest "I have nothing close to lean on" that yellows/reds a draft.
DEFAULT_THRESHOLD = 2


@dataclass
class Exemplar:
    """One past (customer email → the reply she stands behind) pair, with its retrieval score."""

    email_id: str
    subject: str
    body: str
    reply: str
    bucket: str | None  # the situation bucket this reply was filed under, if known
    score: int


def _terms(text: str) -> set[str]:
    """Product/content terms — mirror of KnowledgeStore.search (sources.py:39)."""
    return {t for t in re.findall(r"[a-z]+", text.lower()) if len(t) > 3}


def _overlap(query_terms: set[str], doc: str) -> int:
    low = doc.lower()
    return sum(low.count(t) for t in query_terms)


def _exemplar_of(c: Correction, query_terms: set[str]) -> Exemplar:
    subject = str(c.meta.get("email_subject") or "")
    body = str(c.meta.get("email_body") or "")
    return Exemplar(
        email_id=c.email_id,
        subject=subject,
        body=body,
        reply=c.final_text or "",
        bucket=c.meta.get("bucket"),
        # Score against the customer's words (subject + body), where the product terms live — the
        # symmetric match to "an incoming email that reads like this one."
        score=_overlap(query_terms, f"{subject}\n{body}"),
    )


def top_exemplars(
    corrections: list[Correction],
    email_text: str,
    *,
    k: int = 3,
    bucket: str | None = None,
    threshold: int = DEFAULT_THRESHOLD,
) -> list[Exemplar]:
    """Her `k` closest past replies to `email_text`, most-relevant first.

    When `bucket` is given AND rows carry a `meta.bucket` label, same-bucket exemplars are preferred
    over out-of-bucket ones at equal relevance (situation first, then product) — but out-of-bucket
    neighbors are not discarded, since a product answer often transfers across situations. Rows with
    no bucket label rank purely on overlap. Exemplars scoring below `threshold` are dropped; an empty
    return is the no-neighbor signal (see `has_neighbor`)."""
    query_terms = _terms(email_text)
    if not query_terms:
        return []
    scored = [_exemplar_of(c, query_terms) for c in corrections if _is_voice_exemplar(c)]
    scored = [e for e in scored if e.score >= threshold]

    def rank(e: Exemplar) -> tuple[int, int]:
        in_bucket = 1 if (bucket is not None and e.bucket == bucket) else 0
        return (in_bucket, e.score)

    scored.sort(key=rank, reverse=True)
    return scored[:k]


def has_neighbor(exemplars: list[Exemplar]) -> bool:
    """True when the shelf found at least one real neighbor — the grounding the engine leans on."""
    return bool(exemplars)
