"""The correction log + edit classifier — the load-bearing start of feedback-to-context.

Only SUBSTANTIVE edits (changed facts, policy, commitment, reasoning) count against the
calibration metric and feed the loop. COSMETIC edits (whitespace, signature, greeting, a small
wording swap) mean the content was right and must NOT depress the HIGH bucket's trust
— edits are not all corrections. The classifier here is a deterministic
heuristic; an ambiguous diff is the documented seam for a model judgment call later.
"""

from __future__ import annotations

import difflib
import json
import re
from dataclasses import asdict, dataclass, field
from pathlib import Path

from bean.paths import corrections_path

# The per-customer log, on the volume in prod (see bean/paths.py). Callers may still pass an
# explicit log_path (tests do) — this is only the default destination.
DEFAULT_LOG = corrections_path()

# How many past replies ride into one draft prompt. These tokens sit in the per-email USER message
# (uncached) and recur on every call, so the log may grow without bound but the prompt may not.
MAX_FEW_SHOT_EXEMPLARS = 3


class CorrectionsCorruptError(RuntimeError):
    """Raised by `load` when the correction log EXISTS but a line cannot be read.

    The sibling of `config.ConfigCorruptError`, and deliberately not caught by the loop. An absent
    log means a new customer who has taught Bean nothing yet — draft without exemplars, that's fine.
    An UNREADABLE log means the operator's two days of teaching are sitting right there and Bean cannot
    see them; drafting anyway would produce a confident, untaught reply that looks exactly like a
    taught one. Fail the request instead. Silence here is indistinguishable from having learned
    nothing, which is the whole failure this project exists to kill."""

# A standalone sign-off / greeting LINE (short, pure presentation) — dropped wholesale.
#
# Sign-off WORDS only, no operator name. This used to also match the first customer's own first name
# (bare, and as "— <name>"), which made the one general-purpose text normalizer in the codebase
# specific to a single customer. Dropping it is safe because of how this is used: `classify_edit`
# compares an operator's edit against Bean's draft, both signed by the SAME person, so a bare name
# line appears on both sides and cancels in the token-set diff. In the one case it doesn't cancel —
# they add a sign-off that wasn't there — it contributes a single non-`signal` token to an overlap
# ratio, which cannot flip cosmetic to substantive on its own. A generic "dash followed by one word" rule was rejected: it eats bullet
# list items ("- shipping"), which are real content.
_SIGNOFF_LINE = re.compile(
    r"^\s*(thanks|thank you|warmly|best|cheers|regards|sincerely)\b[\s,.!—-]*$",
    re.IGNORECASE,
)
# A leading greeting PREFIX on a content line ("Hi Priya, ...") — stripped, body kept.
_GREETING_PREFIX = re.compile(r"^\s*(hi|hello|hey|dear)\b.*?,\s*", re.IGNORECASE)


def _content_tokens(text: str) -> list[str]:
    """Normalize away presentation: drop standalone sign-off lines, strip the greeting prefix
    off content lines, lowercase, strip punctuation. What remains is the semantic content."""
    kept = []
    for line in text.splitlines():
        if not line.strip() or _SIGNOFF_LINE.match(line):
            continue
        kept.append(_GREETING_PREFIX.sub("", line, count=1))
    norm = re.sub(r"[^a-z0-9\s]", " ", " ".join(kept).lower())
    return norm.split()


def classify_edit(original: str, edited: str) -> str:
    """Return 'cosmetic' or 'substantive'.

    Substantive = the semantic content changed (facts/policy/commitment/reasoning). We compare
    content-token multisets; a high overlap with no new content tokens is cosmetic.
    """
    a, b = _content_tokens(original), _content_tokens(edited)
    if a == b:
        return "cosmetic"

    set_a, set_b = set(a), set(b)
    added = set_b - set_a
    removed = set_a - set_b
    # New numbers, prices, or policy/commitment words are always substantive signals.
    signal = re.compile(r"\d|\$|refund|exchange|return|ship|free|cannot|can't|will|won't|guarantee", re.IGNORECASE)
    if any(signal.search(w) for w in added | removed):
        return "substantive"

    union = set_a | set_b
    jaccard = len(set_a & set_b) / len(union) if union else 1.0
    return "cosmetic" if jaccard >= 0.9 else "substantive"


def edit_ratio(original: str, edited: str) -> float:
    """How much of the draft she changed, in [0,1]: 0 = identical, 1 = nothing survived. A char-level
    dissimilarity (difflib), the MAGNITUDE behind the cosmetic/substantive LABEL — so a one-word tweak
    and a near-total rewrite, both logged as `edit`, stop being indistinguishable. This is the signal
    for whether a green was really green: a green she barely touches is trustworthy; one she rewrites
    every time is a green that shouldn't have been one."""
    if not original and not edited:
        return 0.0
    return round(1.0 - difflib.SequenceMatcher(None, original or "", edited or "").ratio(), 4)


# The draft-outcome taxonomy for the approval-rate instrument. The north star is the
# approved_untouched share — the fraction of drafts the operator sends with one tap, which is the
# only thing that saves them time. Non-draft gestures (snooze, the gate pair mis-file / keep-filed,
# and should-file) map to None: they aren't a verdict on a draft, so they stay out of the
# denominator. should-file is deliberately absent even though it fires on a DRAFTED email — the
# operator is rejecting the gate's decision to route it here at all, not grading the reply Bean
# wrote.
_OUTCOME_BY_ACTION = {
    "approve": "approved_untouched",  # sent Bean's draft as-is — the moat
    "edit": "approved_edited",        # sent it after changes (see edit_ratio for how much)
    "takeover": "rewritten",          # ignored the draft, wrote their own in their client
    "teach": "escalated",             # Bean had no draft; they authored the answer (a correct escalation)
}


def outcome_of(c: Correction) -> str | None:
    """The draft-outcome label for `c`, or None for a non-draft gesture. The one classifier the
    approval-rate read side agrees on, so the taxonomy lives in one place."""
    return _OUTCOME_BY_ACTION.get(c.action)


@dataclass
class Correction:
    email_id: str
    category: str  # the operator's FINAL bucket (their relabel wins over the model's guess)
    confidence: str
    action: str  # approve | edit | teach | takeover | skip | misfile | keep-filed | should-file
    # The gate pair, both directions: misfile = Bean filed it and the operator wanted a reply;
    # should-file = Bean drafted a reply and they wanted it filed. Together they're the gate's
    # labelled errors. teach = the operator authored a reply for an untaught node via the flashcard
    # queue (grounded few-shot exemplar + template source); distinct from takeover (they left Bean
    # to answer in their own client).
    original_draft: str | None = None
    final_text: str | None = None  # the reply actually sent (model draft on approve; edited text on edit)
    edit_kind: str | None = None  # cosmetic | substantive (for action == edit)
    edit_ratio: float | None = None  # 0..1 magnitude of an edit (0 = untouched, 1 = full rewrite)
    note: str = ""  # 💬 the operator's free-text comment on this reply — the strongest, why-bearing signal
    liked: bool = False  # 👍 quick positive reinforcement — "good job Bean", learn from this win
    # Free-dict extension point. Known keys: model_category (the bucket the model picked — a
    # relabel is any row where it differs from `category`), email_subject, email_body (the email
    # context few-shot learns from). 'recategorize' is reserved for a future standalone-relabel
    # gesture; today a relabel always rides an approve/edit/takeover via category + model_category.
    meta: dict = field(default_factory=dict)


def record(correction: Correction, *, log_path: Path = DEFAULT_LOG) -> Correction:
    """Tag edit kind (if an edit) and append one JSON line to the correction log."""
    if correction.action == "edit" and correction.original_draft is not None and correction.final_text is not None:
        correction.edit_kind = classify_edit(correction.original_draft, correction.final_text)
        correction.edit_ratio = edit_ratio(correction.original_draft, correction.final_text)
    log_path.parent.mkdir(parents=True, exist_ok=True)
    with log_path.open("a", encoding="utf-8") as fh:
        fh.write(json.dumps(asdict(correction), sort_keys=True) + "\n")
    return correction


def load(log_path: Path = DEFAULT_LOG) -> list[Correction]:
    """Every correction for one customer, oldest first.

    Absent ⇒ `[]` (a new customer). Present-but-unreadable ⇒ `CorrectionsCorruptError`, naming the
    line, so the caller can refuse the request rather than draft as an untaught Bean.
    """
    if not log_path.exists():
        return []
    try:
        raw = log_path.read_text(encoding="utf-8")
    except OSError as exc:  # unreadable volume — loud, and never mistaken for "she taught nothing"
        raise CorrectionsCorruptError(f"cannot read correction log {log_path}: {exc}") from exc
    out = []
    for lineno, line in enumerate(raw.splitlines(), start=1):
        if not line.strip():
            continue
        try:
            out.append(Correction(**json.loads(line)))
        except (json.JSONDecodeError, TypeError, ValueError) as exc:
            raise CorrectionsCorruptError(f"{log_path}:{lineno} is unreadable: {exc}") from exc
    return out


def substantive_only(corrections: list[Correction]) -> list[Correction]:
    """The subset that feeds the calibration metric — cosmetic edits are excluded."""
    return [c for c in corrections if c.edit_kind != "cosmetic"]


def _is_voice_exemplar(c: Correction) -> bool:
    """Does this row carry a (customer email → the reply the operator stands behind) pair?

    An approve, edit, or teach with `final_text` and the email context in `meta`. Shared by the
    few-shot selector and the leaf-precedent reader so their idea of "usable pair" can't drift.
    """
    if c.action not in ("approve", "edit", "teach") or not c.final_text:
        return False
    return bool(c.meta.get("email_subject") or c.meta.get("email_body"))


def few_shot_examples(
    corrections: list[Correction], category: str, *,
    k: int = MAX_FEW_SHOT_EXEMPLARS, max_chars: int = 400
) -> list[dict]:
    """Past (email → good reply) pairs in `category` — voice exemplars for the DRAFT step.

    This used to accept a `leaf_path` and rank leaf-exact exemplars above the rest of the branch.
    That key was the routing tree's, and it went with the tree: nothing has written `meta.leaf_path`
    or passed the argument since the notebook cutover, so the ranking was a permanent no-op tier.

    A usable pair is an approve, edit, or teach whose `final_text` is the reply the operator stands
    behind — the model draft they approved, the text they edited to, or the reply they authored for
    a flashcard — in their FINAL bucket (`category`), with the email context captured in `meta`. A
    `teach` is the flashcard's grounded exemplar: they wrote the reply from scratch for an untaught
    node, so it is exactly the kind of (email → their words) pair few-shot exists to learn. Truncated:
    these tokens ride in the per-email USER message (uncached) and recur on every call, so keep
    them lean. NOT filtered by `substantive_only` — a cosmetically-edited approval is still a valid
    voice exemplar.

    Weighting: examples the operator 👍 liked or 💬 commented on are the strongest signal, so they
    outrank plain approvals for the K kept slots; recency breaks ties. The sort is stable and
    ascending, so when NO example carries a like or note the result is exactly the most-recent K
    in chronological order (the pre-weighting behavior the selector tests pin). Each dict carries
    `liked`/`note` so the prompt can surface the win or the guidance.
    """
    out: list[dict] = []
    for c in corrections:
        if not _is_voice_exemplar(c):
            continue
        if c.category != category:
            continue
        out.append({
            "subject": c.meta.get("email_subject") or "",
            "body": (c.meta.get("email_body") or "")[:max_chars],
            "reply": c.final_text,
            "liked": bool(getattr(c, "liked", False)), "note": c.note or "",
        })
    # Strongest signals sort to the end (most salient slot); equal-signal rows keep log order, so
    # the no-signal path stays == out[-k:] (most-recent K, chronological).
    out.sort(key=lambda e: (2 if e["liked"] else 0) + (1 if e["note"] else 0))
    return out[-k:]
