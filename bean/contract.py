"""The output contract — the interface between the engine and the web UI. The engine's job is to
*produce* a validated DraftResult for each email; the UI's job is to render it. The struct is
derived from the design-studio prototype's mock dataset (see memory `bean-output-contract`).

`Confidence` is the GRADING vocabulary — what a golden case says the right answer looks like
(bean/fixtures.py). `Grounding` below is what the engine actually emits. They are deliberately
separate axes: one is the expectation, the other the verdict.

    HIGH  ── all 3 checks pass ──────────────▶ one-tap "Approve & send"
    LOW   ── category clear, coverage/grounding shaky ▶ "looks right?" + why-unsure
    FLAG  ── unsourced / no template / high-stakes ──▶ no draft, no send button, ask the operator
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from enum import Enum


class Confidence(str, Enum):
    HIGH = "high"
    LOW = "low"
    FLAG = "flag"


@dataclass(frozen=True)
class Email:
    """One inbound email. `image_paths` carries customer photo attachments (multimodal).
    `thread` holds prior messages in the same conversation (oldest→newest) for context."""

    id: str
    sender_name: str
    sender_email: str
    subject: str
    body: str
    image_paths: list[str] = field(default_factory=list)
    thread: list[str] = field(default_factory=list)


# ── The rebuild contract (Option C) ──────────────────────────────────
# The notebook engine emits ONE DraftResult per email from ONE model call — bucket, draft, the
# sources it cites, and a groundedness verdict. Confidence here is groundedness, not a route: a
# claim is trusted exactly as far as it cites a shelf exemplar or a notebook line.


class Grounding(str, Enum):
    """How grounded the draft is — the rebuild's confidence axis (rebuild-plan.md §2 item 4).

        GREEN  — every claim cites a shelf exemplar or notebook line ─▶ one-tap approve
        YELLOW — partial grounding; drafts anyway and says why unsure ─▶ "looks right?"
        RED    — no grounding, OR a physical action is required (refund/label/order edit), OR
                 legal / anger / VIP ─▶ shows context, never a confident reply
    """

    GREEN = "green"
    YELLOW = "yellow"
    RED = "red"


@dataclass
class DraftResult:
    """What the notebook engine emits per email (one model call). Validated on construction so an
    ungrounded GREEN or a citation-less GREEN fails loudly rather than reaching the operator as a
    confident-looking untaught reply — the exact failure this project exists to kill.

    `bucket` is the induced SITUATION bucket, assigned inside the
    drafting call — it scopes retrieval and metrics, it is not a routing hop. `citations` are the
    sources each claim leans on: `corpus:<email_id>` for a past reply, `notebook:<label>` for a
    notebook line (the faithfulness verifier resolves them against the real corpus/notebook)."""

    email_id: str
    bucket: str  # a proposal-tree situation bucket, or "Needs a human"
    confidence: Grounding
    draft: str | None = None  # the reply attempt; may be present even on RED (a non-confident try)
    citations: list[str] = field(default_factory=list)  # corpus:<id> / notebook:<label>
    why_unsure: list[str] = field(default_factory=list)  # YELLOW/RED: what Bean can't stand behind

    def __post_init__(self) -> None:
        if self.confidence == Grounding.GREEN:
            if not self.draft:
                raise ValueError("GREEN results require a draft.")
            if not self.citations:
                raise ValueError(
                    "GREEN results require citations — green MEANS grounded; an uncited claim is not green."
                )
        elif self.confidence == Grounding.YELLOW:
            if not self.draft:
                raise ValueError("YELLOW results require a draft (it drafts anyway and says why unsure).")
            if not self.why_unsure:
                raise ValueError("YELLOW results require why_unsure (what Bean can't stand behind).")
        else:  # RED
            if not self.why_unsure:
                raise ValueError("RED results require why_unsure (why Bean won't stand behind a reply).")


def draft_dict(r: DraftResult) -> dict:
    """The stored/wire JSON shape of a DraftResult — asdict with the enum flattened to its value."""
    d = asdict(r)
    d["confidence"] = r.confidence.value
    return d
