"""The shelf — retrieval over her own past replies.

~45% of her mail is product questions won by retrieval, not by any tree. The shelf pulls her
closest past replies to *this kind of email*, ranked by product-term overlap, and hands them to the
one drafting call as grounded exemplars. It fixes the concrete bug in the old selector
(`bean/corrections.few_shot_examples`, cap-3 category-join that degenerates to most-recent-3 on a
fat bucket): here ranking is by what the email is ABOUT, and "no neighbor above threshold" is a
first-class signal — it drops the draft to yellow/red rather than dressing up the three most recent
replies as precedent.

TOKENIZATION still mirrors `KnowledgeStore.search` (`bean/sources.py`): lowercase, maximal alpha
runs, drop tokens ≤ 3 chars. SCORING deliberately does not, and the divergence is the point — see
`_similarity`. The knowledge store answers "which doc mentions this", where a raw count is fine; the
shelf answers "do I have anything CLOSE ENOUGH to lean on", which is a question about degree and
needs a bounded, comparable number.

⚠️ MEASURED 2026-08-14, and the reason this file was rewritten: the previous scorer summed raw
occurrence counts of every 4+ char token against a threshold of 2, and it NEVER ONCE reported a
missing neighbour. Not on the 147 production emails, not on the 203 replayed here — the full k=3
came back every single time, median top score 13 against a bar of 2. `_terms` has no stopword list,
so `have`, `that`, `this`, `with`, `your` and `order` all scored, and any two real emails clear a
bar of 2 on function words alone. The honesty signal this module's docstring promised was
unreachable, and nothing failed — the only symptom was Bean quietly always having "precedent".

That is the failure this project exists to prevent, aimed at itself: a confident-looking answer with
nothing behind it. Bean's whole claim is that it says when it does not know.
"""

from __future__ import annotations

import math
import re
from dataclasses import dataclass

from bean.corrections import Correction, _is_voice_exemplar

# Minimum similarity for a past reply to count as a real neighbour, on `_similarity`'s 0..1 scale.
# Below this the shelf reports no neighbour — the honest "I have nothing close to lean on".
#
# CHOSEN BY READING THE MAIL AT THE BOUNDARY, not by picking a round number. On the replay corpus
# the best-match score has no natural cliff (median 0.19, p10 0.13), so no threshold falls out of
# the data — this is a policy about how often Bean should admit it has nothing, and it has to be
# made deliberately. At 0.15 the shelf comes back empty for ~1 in 5 emails, and the mail below the
# line is genuinely unfamiliar: an enquiry in Portuguese, a cold sales pitch, the first shipping
# notice of its kind. At 0.10 it is ~4% and still waves through mail she has never answered; at
# 0.20 it is >50% and starts refusing ordinary repeat questions.
#
# Re-read the boundary against a real corpus before moving this. The number is a judgment about her
# mail, not a constant of nature.
#
# CONFIRMED 2026-08-14 against the LIVE volume (138 corrections, 131 drafted emails), the corpus the
# paragraph above could not reach — it was measured on a Jul 23 snapshot. 0.15 holds: no-neighbour
# 10.7% (vs 14.3% on the snapshot), own-thread and own-id exclusion 0% at EVERY threshold. The
# refusal rate fell because the corpus grew — 81 usable voice exemplars now vs 40 — so a bigger
# shelf finds more neighbours, and the threshold did not need to move to track it. Still no cliff on
# prod either (p10 0.150, median 0.201).
#
# The refusals are honest, and that was checked rather than assumed: exactly ONE exemplar in her
# whole corpus answers a broken-item email, so Bean refusing "my wife's headphones broke" is a true
# "I have nothing close", not a retrieval failure. Read the mail before calling a refusal a miss.
#
# What the boundary DOES expose is not a threshold problem: B2B proposals, an agency thread and a
# duty-drawback pitch sit on both sides of the line. That is mail the shelf should never have been
# asked about — the lever is filing it upstream, not tuning this number.
DEFAULT_THRESHOLD = 0.15


@dataclass
class Exemplar:
    """One past (customer email → the reply she stands behind) pair, with its retrieval score."""

    email_id: str
    subject: str
    body: str
    reply: str
    bucket: str | None  # the situation bucket this reply was filed under, if known
    score: float  # 0..1 similarity (see _similarity); was an unbounded raw count before 2026-08-14


def _terms(text: str) -> set[str]:
    """Product/content terms — mirror of KnowledgeStore.search (sources.py:39)."""
    return {t for t in re.findall(r"[a-z]+", text.lower()) if len(t) > 3}


def _idf(docs: list[set[str]]) -> dict[str, float]:
    """Inverse document frequency over the exemplar corpus — how much each word is worth.

    This is what makes a threshold mean anything. Words that appear in most of her mail (`order`,
    `your`, `have`, `shipping`) carry almost no evidence that two emails are about the same thing,
    and they were the entire reason the old raw count always cleared its bar. A word she has used
    once (`chargeback`, `netting`, `Portuguese`) is strong evidence. IDF is that weighting, computed
    from her own corpus rather than a hand-written stopword list — so it adapts to what her store
    actually talks about instead of encoding a guess about English.
    """
    n = len(docs)
    df: dict[str, int] = {}
    for terms in docs:
        for t in terms:
            df[t] = df.get(t, 0) + 1
    # +1 smoothing so a term in every document scores low rather than exactly zero, and the floor of
    # 1.0 keeps every term contributing something — a common word is weak evidence, not none.
    return {t: math.log(n / (1 + d)) + 1.0 for t, d in df.items()}


def _similarity(query: set[str], doc: set[str], idf: dict[str, float]) -> float:
    """IDF-weighted cosine over DISTINCT terms, in 0..1. 1.0 = the same words, 0.0 = nothing shared.

    Two properties the old raw count lacked, both required for a threshold to exist at all:

      * BOUNDED. A count of 13 answers "13 what?" — you cannot say whether it is close without
        knowing the corpus. 0.19 is comparable across emails of any length.
      * LENGTH-NORMALISED. Raw counts reward long documents: a rambling past reply outscored a
        precise one simply by containing more words, so the shelf drifted toward whichever
        exemplars were wordiest rather than whichever were most alike.
    """
    shared = query & doc
    if not shared:
        return 0.0
    num = sum(idf.get(t, 1.0) ** 2 for t in shared)
    nq = math.sqrt(sum(idf.get(t, 1.0) ** 2 for t in query))
    nd = math.sqrt(sum(idf.get(t, 1.0) ** 2 for t in doc))
    if not nq or not nd:
        return 0.0
    # Clamped: identical term sets come out as 1.0000000000000002 in float, and a "similarity"
    # documented as 0..1 that can exceed 1 is a leak every caller then has to remember.
    return min(1.0, num / (nq * nd))


# Reply/forward prefixes, including the mail-gateway tags that ride along ("[EXTERNAL]"). Stripped
# repeatedly, because a long chain accretes them: "RE: [EXTERNAL] Re: Fwd: Headphones issue".
_THREAD_PREFIX = re.compile(r"^\s*((re|fwd|fw|aw|sv)\s*:|\[[^\]]{1,20}\]\s*)+", re.IGNORECASE)


def thread_key(subject: str | None) -> str:
    """The conversation a subject belongs to — the subject with every reply prefix peeled off.

    Crude on purpose: no Message-ID or References header survives the forward into Postmark, so the
    subject line is the only conversation handle Bean has. It over-groups identical subjects from
    different customers, which is why `top_exemplars` only ever uses it to EXCLUDE — the cost of
    dropping one real precedent is a slightly thinner shelf, and the cost of keeping a false one is
    Bean citing a conversation back to itself as though it were precedent.
    """
    s = (subject or "").strip()
    prev = None
    while prev != s:
        prev = s
        s = _THREAD_PREFIX.sub("", s).strip()
    return s.lower()


def _exemplar_of(c: Correction, score: float) -> Exemplar:
    return Exemplar(
        email_id=c.email_id,
        subject=str(c.meta.get("email_subject") or ""),
        body=str(c.meta.get("email_body") or ""),
        reply=c.final_text or "",
        bucket=c.meta.get("bucket"),
        score=score,
    )


def top_exemplars(
    corrections: list[Correction],
    email_text: str,
    *,
    k: int = 3,
    bucket: str | None = None,
    threshold: float = DEFAULT_THRESHOLD,
    exclude_id: str | None = None,
    exclude_subject: str | None = None,
) -> list[Exemplar]:
    """Her `k` closest past replies to `email_text`, most-relevant first.

    When `bucket` is given AND rows carry a `meta.bucket` label, same-bucket exemplars are preferred
    over out-of-bucket ones at equal relevance (situation first, then product) — but out-of-bucket
    neighbors are not discarded, since a product answer often transfers across situations. Rows with
    no bucket label rank purely on similarity. Exemplars scoring below `threshold` are dropped; an
    empty return is the no-neighbor signal (see `has_neighbor`).

    `exclude_id` / `exclude_subject` drop THIS EMAIL'S OWN CONVERSATION from its own retrieval.
    Measured before it existed: 18.8% of shelves contained the email's own past reply and 23.3%
    contained some message from the same thread — Bean reading a conversation back to itself and
    presenting it as precedent for how she handles this kind of mail. It is not precedent, it is a
    mirror, and the engine already puts the thread in the prompt separately (`EARLIER IN THIS
    THREAD`), so those slots were spent showing the model something it had already been shown.
    Excluding by SUBJECT as well as id is what closes it: a follow-up in a thread is a different
    email_id, so id alone caught 38 of the 47 real cases. Same intent as
    `customer_history.history_block(..., exclude_id=...)`, which has always done this.
    """
    query = _terms(email_text)
    if not query:
        return []

    skip_thread = thread_key(exclude_subject) if exclude_subject else None
    rows = []
    for c in corrections:
        if not _is_voice_exemplar(c):
            continue
        if exclude_id is not None and c.email_id == exclude_id:
            continue
        subject = str(c.meta.get("email_subject") or "")
        if skip_thread and thread_key(subject) == skip_thread:
            continue
        rows.append((c, _terms(f"{subject}\n{c.meta.get('email_body') or ''}")))
    if not rows:
        return []

    # IDF over the CANDIDATE corpus (post-exclusion), so a word's weight reflects the pool actually
    # being ranked. Computed per call: her whole corpus is ~10^2 rows, and a cache keyed on a
    # mutable list is the kind of staleness that would silently rank against last week's mail.
    idf = _idf([terms for _, terms in rows])
    scored = [_exemplar_of(c, _similarity(query, terms, idf)) for c, terms in rows]
    scored = [e for e in scored if e.score >= threshold]

    def rank(e: Exemplar) -> tuple[int, float]:
        in_bucket = 1 if (bucket is not None and e.bucket == bucket) else 0
        return (in_bucket, e.score)

    scored.sort(key=rank, reverse=True)
    return scored[:k]


def has_neighbor(exemplars: list[Exemplar]) -> bool:
    """True when the shelf found at least one real neighbor — the grounding the engine leans on."""
    return bool(exemplars)
