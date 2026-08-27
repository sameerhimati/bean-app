"""The engine — one model call from (notebook + shelf + email) to a DraftResult.

This is the whole response pipeline the rebuild keeps, replacing
chunk→route→resolve→assemble→reduce. The notebook is the cached system prefix; the shelf's exemplars
and the email are the per-email user message; the model, in ONE call, assigns the situation bucket,
drafts a reply in her voice grounded in those exemplars, and self-reports groundedness. Bucket
assignment is not a routing hop — it exists to scope retrieval on the next email and to keep metrics.

The chicken/egg (retrieve-by-bucket vs assign-bucket) is resolved the simple way the plan allows:
the shelf retrieves on the RAW email text (product-term overlap needs no bucket), and the model
picks the bucket while it drafts. Confidence is only ever coerced DOWN here — a green the model can't
back with a citation becomes yellow, never the reverse. Faithfulness of the citations themselves is
audited out-of-band by the replay verifier; the engine's job is to never emit a confident-looking
reply it wasn't handed the grounding for.
"""

from __future__ import annotations

from bean import quoting
from bean.contract import DraftResult, Email, Grounding
from bean.llm import DRAFT_MODEL, Model
from bean.notebook import Notebook
from bean.shelf import Exemplar

# The bucket every ungroundable / hand-requiring email lands in — always an option even if the
# notebook doesn't name it, so the model has a real "not mine to answer" exit instead of forcing a fit.
ESCALATE_BUCKET = "Needs a human"

_INSTRUCTIONS = """You are the support operator's drafting assistant. Below is her NOTEBOOK: how she \
handles her mail, in her own words — the situation buckets (each with the cliff that decides it), her \
standard answers, store facts, and judgment notes distilled from what she has actually done. Precedent \
tagged `observed` outranks stated policy when they conflict.

For the ONE customer email in the user message, do all of this in a single step:

1. BUCKET — put it in exactly one situation bucket from the notebook (or "Needs a human"), chosen by \
what she would DO about it, not by the product it names. When two fit, the cliff text decides.

2. DRAFT — write the reply she would send, in her voice, matching the tone of her past replies shown \
in the user message. Ground every factual claim (policy, price, timing, what you'll do) in either one \
of those past replies or a notebook line. Do not invent facts.

3. CITE — list the sources each claim leans on. Cite a past reply as `corpus:<its id>` (the id is \
shown in brackets) and a notebook line as `notebook:<the bucket or fact it came from>`. Only cite \
sources actually shown to you.

4. CONFIDENCE — report how grounded the draft is:
   - green  = every claim in it is backed by a cited past reply or notebook line, and it needs no \
physical action from her. This is the one-tap-approve bucket — do not mark green unless it is true.
   - yellow = the shape is right but grounding is partial, or you're unsure of a detail. Draft anyway \
and say what you're unsure of.
   - red    = you have nothing close to lean on, OR a physical action is required (issue a refund, \
send a label, edit an order), OR it is legal / an angry customer / a VIP, OR it is not customer \
support at all (a partnership / B2B / marketing pitch). Bucket those as "Needs a human". You may still \
attempt a draft, but it is never confident — and if you truly cannot answer, say "me not know this one" \
rather than faking one.

Never emit a green you can't cite. When unsure, go down a level, not up.

FORMATTING — the draft is pasted straight into an email composer and sent to a customer, so write \
PLAIN PROSE ONLY. No markdown of any kind: no **bold**, no *italics*, no `backticks`, no [links](url), \
no # headings, no bulleted or numbered lists built from - or * characters. Emphasis that survives the \
paste does not exist here, so carry it with word choice instead. Separate ideas with blank lines. \
Write a URL out in full rather than hiding it behind link syntax. Anything you type literally reaches \
the customer literally."""

DRAFT_TOOL = {
    "name": "draft",
    "description": "Emit the bucket, drafted reply, citations, groundedness, and any uncertainty.",
    "input_schema": {
        "type": "object",
        "properties": {
            "bucket": {"type": "string", "description": "The situation bucket, from the notebook or 'Needs a human'."},
            "draft": {"type": "string", "description": "The reply in her voice, or '' if genuinely none."},
            "citations": {
                "type": "array",
                "items": {"type": "string"},
                "description": "Sources leaned on: corpus:<id> for a past reply, notebook:<label> for a notebook line.",
            },
            "confidence": {"type": "string", "enum": ["green", "yellow", "red"]},
            "why_unsure": {
                "type": "array",
                "items": {"type": "string"},
                "description": "For yellow/red: each thing you can't stand behind. Empty for green.",
            },
        },
        "required": ["bucket", "draft", "citations", "confidence", "why_unsure"],
    },
}


def _exemplar_block(exemplars: list[Exemplar]) -> str:
    if not exemplars:
        return "(no close past reply on file — you have nothing of hers to lean on here)"
    out = []
    for e in exemplars:
        tag = f"[{e.email_id}]" + (f" (bucket: {e.bucket})" if e.bucket else "")
        who = e.subject or e.body[:80]
        out.append(f"{tag}\ncustomer wrote: {who}\nshe replied: {e.reply}")
    return "\n\n".join(out)


def _user_message(
    email: Email,
    exemplars: list[Exemplar],
    thread: list[str],
    customer_history: str | None,
    conversation: str | None = None,
) -> str:
    parts = ["CUSTOMER EMAIL", f"From: {email.sender_name} <{email.sender_email}>", f"Subject: {email.subject}", "", email.body]
    # "(oldest first)" was a lie for as long as this line existed. `thread` is stored as ONE raw
    # quoted blob, and a quoted blob is newest-first-nested — outermost is the most recent message,
    # and every `>` level goes further back. So the model was told oldest-first and handed the exact
    # reverse, inside a wall of quote markers that hid up to thirteen real messages in what the UI
    # counted as one. bean/quoting.py unpacks it into the order this header has always claimed.
    prior = quoting.thread_for_prompt(
        list(thread) or list(email.thread), email.sender_email, email.sender_name
    )
    if prior:
        parts += ["", "EARLIER IN THIS THREAD (oldest first):", *prior]
    # Placed ABOVE the customer history on purpose. The history is about this person across every
    # subject; this is the one conversation they are waiting on, and it decides what the reply must
    # cover — all of it, or only what is new. Empty for a single email, so the ordinary case is
    # byte-identical to the prompt before grouping existed.
    if conversation:
        parts += ["", conversation]
    if customer_history:
        parts += ["", "THIS CUSTOMER'S HISTORY:", customer_history]
    parts += ["", "HER PAST REPLIES YOU MAY LEAN ON (cite as corpus:<id>):", _exemplar_block(exemplars)]
    return "\n".join(parts)


def _coerce(data: dict, notebook: Notebook, email_id: str, *, has_shelf: bool = True) -> DraftResult:
    """Turn the raw tool output into a validated DraftResult, coercing DOWN only. A green with no
    citation is not grounded, so it becomes yellow; a bucket the notebook doesn't know becomes the
    escalate bucket. Never coerces up — honesty is monotonic here.

    `has_shelf` is False when retrieval found nothing close (`shelf.has_neighbor`). It closes an
    asymmetry that stood here for months: the citation rule was enforced in CODE, but the
    empty-shelf case was only ever a sentence in the prompt (`_exemplar_block`'s "you have nothing
    of hers to lean on"). So one honesty guarantee was a rule and the other was a polite request —
    in a product whose entire thesis is not trusting the model's self-report. Both are rules now."""
    valid = set(notebook.bucket_names()) | {ESCALATE_BUCKET}
    bucket = data.get("bucket") or ESCALATE_BUCKET
    if bucket not in valid:
        bucket = ESCALATE_BUCKET

    conf = str(data.get("confidence", "red")).lower()
    confidence = {"green": Grounding.GREEN, "yellow": Grounding.YELLOW}.get(conf, Grounding.RED)
    draft = (data.get("draft") or "").strip() or None
    citations = [c for c in (data.get("citations") or []) if str(c).strip()]
    why_unsure = [w for w in (data.get("why_unsure") or []) if str(w).strip()]

    # The calibration guardrail (the moat, made inspectable): a HIGH-stakes bucket — money out the
    # door or a commitment she'd want eyes on — never one-taps, even perfectly grounded. Hold it at
    # yellow so she eyeballs it. Which buckets are high-stakes is HERS to set, in the notebook.
    stakes = next((b.stakes for b in notebook.buckets if b.name == bucket), "normal")
    if confidence == Grounding.GREEN and stakes == "high":
        confidence, why_unsure = Grounding.YELLOW, why_unsure + ["high-stakes bucket — worth a look before sending"]

    # An empty shelf means she has never answered anything like this. The notebook may still cover
    # it in general terms, so this is not an automatic red — but it is never a one-tap send, because
    # "approve blind" is a promise that she has already made this exact call before.
    if confidence == Grounding.GREEN and not has_shelf:
        confidence, why_unsure = Grounding.YELLOW, why_unsure + ["no close past reply of yours to lean on"]

    if confidence == Grounding.GREEN and not citations:
        confidence, why_unsure = Grounding.YELLOW, why_unsure + ["draft is not grounded in a cited source"]
    if confidence == Grounding.GREEN and not draft:
        confidence, why_unsure = Grounding.YELLOW, why_unsure + ["no draft produced"]
    if confidence == Grounding.YELLOW and not draft:
        confidence, why_unsure = Grounding.RED, why_unsure + ["no draft produced"]
    if confidence == Grounding.YELLOW and not why_unsure:
        why_unsure = ["grounding is only partial"]
    if confidence == Grounding.RED and not why_unsure:
        why_unsure = ["nothing close on file to stand behind a reply"]

    return DraftResult(
        email_id=email_id,
        bucket=bucket,
        confidence=confidence,
        draft=draft,
        citations=citations,
        why_unsure=why_unsure,
    )


def draft_email(
    notebook: Notebook,
    exemplars: list[Exemplar],
    thread: list[str],
    customer_history: str | None,
    email: Email,
    adapter: Model,
    *,
    conversation: str | None = None,
    max_tokens: int = 1500,
) -> DraftResult:
    """One model call: (notebook + shelf exemplars + email) → a validated DraftResult.

    `adapter` is any `Model` — the live `ModelAdapter` (cached notebook prefix, per-tenant key) in
    production, or `FakeModel` in tests. The notebook rides in the cached system prefix; everything
    that changes per email is in the user message."""
    system = [
        {"type": "text", "text": _INSTRUCTIONS},
        {"type": "text", "text": notebook.render()},
    ]
    user = _user_message(email, exemplars, thread, customer_history, conversation)
    result = adapter.structured(
        system=system, user=user, tool=DRAFT_TOOL, images=email.image_paths or None, max_tokens=max_tokens
    )
    # The tool has no email_id field (it's context, not a model decision) — stamp it from the email.
    return _coerce(result.data, notebook, email.id, has_shelf=bool(exemplars))


DEFAULT_MODEL = DRAFT_MODEL
