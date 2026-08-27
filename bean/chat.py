"""Talking to Bean about the notebook — the operator's own feature request.

She says "we replace broken headphones within 60 days now" and Bean works out what in her notebook
that lands on. This is the EXPLICIT channel into her brain; the implicit one (corrections feeding
the shelf) is untouched.

The shape of a turn is the whole design. Bean must never reply "Got it!" and quietly write — that is
the autosend failure wearing a friendlier hat. So a turn resolves into one of exactly two things:

  - a PROPOSAL: the rule restated in her words, what it will change, and what it replaces. She
    confirms, fixes, or declines, and declining writes nothing.
  - an ANSWER: a read of her own notebook, carrying the lines it leaned on as citations.

Both are enforced by the TOOL, not by the prompt. Every model call in Bean is already tool-forced
(`bean/adapter.py`), and keeping that here is what makes the product boundary structural rather than
a rule someone has to remember: Bean cannot emit a chatty non-answer, because the schema gives it
nowhere to put one. The roadmap refuses to become a chief-of-staff stack (roadmap.md); a chat box is
the easiest way to violate that, and the schema is the thing that stops it.

Single-turn, like every other call here. `Model.structured` has no message-list parameter and
`FakeModel` would have to grow one — not needed, because rendering the recent transcript into the
user message does the same job with no new seam.

Pure: prompt construction plus one `structured()` call plus a coercion function. No I/O. The write
path is deliberately somewhere else — the operator approves a proposal in the UI, and the existing
single notebook writer (PUT /api/notebook, ETag-guarded) performs it. Nothing here touches disk.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from bean.llm import DRAFT_MODEL, Model
from bean.notebook import Notebook

# How much of the conversation rides along. Enough for "actually, make it 60 days" to resolve against
# the turn before it; short enough that a long session doesn't grow the (uncached) user message
# without bound. Same reasoning as conversation._MAX_IN_PROMPT.
MAX_TURNS = 8
TURN_MAX_CHARS = 600

# The about-me block is flavour, and it rides in the cached prefix of every chat turn. Capped so a
# file someone pastes an essay into cannot quietly double what each turn costs.
ABOUT_MAX_CHARS = 1200

# The sections a proposal can land in, and the notebook attribute each one edits.
SECTIONS = {"fact": "facts", "bucket": "buckets", "macro": "macros", "note": "notes"}

_INSTRUCTIONS = """You are Bean, talking with the support operator about her own NOTEBOOK — the \
file below, in her words, that every draft you write is grounded in. She is telling you how she \
works, or asking what you already know. You are not a general assistant and you have no other job.

Every message resolves into exactly ONE of two things:

1. PROPOSAL — she stated or changed a rule ("we replace broken headphones within 60 days now", \
"stop offering the discount"). Restate it as a CLAIM in her own voice, one sentence, the way it \
should read in her notebook. Never write it yourself — she confirms it first.

   - `section`: where it belongs. `bucket` if it changes what she would DO in a situation (a cliff); \
`fact` if it is something she would cite in a reply (a price, a window, a policy); `macro` if it is \
a standard answer; `note` if it is judgment about how she handles something. The litmus is "if this \
changes, how many replies change?" — a cliff moves every reply in that situation, a fact moves only \
the replies that cite it.
   - `supersedes`: if the notebook ALREADY says something about this, quote that existing line back \
EXACTLY, character for character, so it can be replaced rather than contradicted. Look hard — a \
notebook that accumulates two answers to one question drafts worse than one that has neither. Leave \
it empty only when nothing in the notebook covers this yet.
   - `consequence`: one plain sentence about what confirming changes for her mail.

2. ANSWER — she asked what you know ("what do i say about lost packages?"). Answer ONLY from the \
notebook below. Cite the lines you leaned on as `notebook:<the bucket or section name>`. If the \
notebook does not cover it, say so plainly — "me not know that one, you have never told me" — and \
cite nothing. Never invent a policy, and never guess at one to be helpful.

Speak in Bean's voice: warm, short, a little broken ("me thinks", "me not know"). Never claim to \
have written something down — she is the one who decides that.

If an ABOUT ME block appears below, it is who you are and who made you. Use it ONLY for questions \
about yourself or the people who built you, keep it light, and come straight back to the work — \
helping her with her mail is the job. It is NOT knowledge about her store: a question about \
policies, prices or products is answered from the notebook or not at all."""

CHAT_TOOL = {
    "name": "answer_or_propose",
    "description": "Answer the operator from her notebook, or propose a change to it for approval.",
    "input_schema": {
        "type": "object",
        "properties": {
            "kind": {
                "type": "string",
                "enum": ["answer", "proposal"],
                "description": "'proposal' when she stated or changed a rule; 'answer' when she asked.",
            },
            "text": {
                "type": "string",
                "description": "What Bean says, in her voice. For a proposal this introduces the "
                               "card ('Me hears a rule in that. Say it back before me writes "
                               "anything down:'); for an answer it IS the answer.",
            },
            "cites": {
                "type": "array",
                "items": {"type": "string"},
                "description": "Answers only: the notebook lines leaned on, as notebook:<label>. "
                               "Empty when the notebook does not cover it.",
            },
            "claim": {
                "type": "string",
                "description": "Proposals only: the rule as one sentence in HER voice, ready to "
                               "read in the notebook. Empty for an answer.",
            },
            "section": {
                "type": "string",
                "enum": ["fact", "bucket", "macro", "note"],
                "description": "Proposals only: which part of the notebook this belongs in.",
            },
            "supersedes": {
                "type": "string",
                "description": "Proposals only: the EXACT existing notebook line this replaces, "
                               "character for character. Empty if nothing covers this yet.",
            },
            "consequence": {
                "type": "string",
                "description": "Proposals only: one sentence on what confirming changes.",
            },
        },
        "required": ["kind", "text"],
    },
}


@dataclass(frozen=True)
class ChatReply:
    """One turn of Bean's, in the shape the transcript renders.

    Mirrors the message objects `web/bean-chat.jsx` reads, so `to_dict` IS the wire format — there
    is no second mapping to drift.
    """

    kind: str                       # "answer" | "proposal"
    text: str
    cites: list[str] = field(default_factory=list)
    claim: str = ""
    section: str = "fact"
    supersedes: str = ""
    consequence: str = ""

    def to_dict(self) -> dict:
        if self.kind == "proposal":
            return {
                "kind": "proposal", "text": self.text, "claim": self.claim,
                "provenance": "stated",  # she said it out loud; that is what `stated` means
                "section": self.section, "supersedes": self.supersedes,
                "consequence": self.consequence,
                "receipts": "replaces what you told me before" if self.supersedes else "",
            }
        return {"kind": "answer", "text": self.text, "cites": list(self.cites)}


def notebook_lines(notebook: Notebook) -> dict[str, list[str]]:
    """Every line a proposal could supersede, by section. The lookup `_resolve_supersedes` checks
    against, and the same text `render()` puts in front of the model — so what the model is asked to
    quote back is exactly what it was shown."""
    return {
        "bucket": [b.cliff for b in notebook.buckets],
        "macro": [m.text for m in notebook.macros],
        "fact": [f.text for f in notebook.facts],
        "note": [n.text for n in notebook.notes],
    }


def _norm(text: str) -> str:
    return " ".join((text or "").split()).strip().lower()


def _resolve_supersedes(claimed: str, section: str, notebook: Notebook) -> str:
    """The actual notebook line `claimed` refers to, or "" when it refers to none.

    Exact match first, then whitespace/case-insensitive — a model that re-wraps a line has still
    identified it, and rejecting that would push a real replacement into an append. Anything looser
    is refused: guessing which line she meant is how you delete the wrong policy.
    """
    if not (claimed or "").strip():
        return ""
    lines = notebook_lines(notebook).get(section, [])
    if claimed in lines:
        return claimed
    target = _norm(claimed)
    return next((line for line in lines if _norm(line) == target), "")


def _coerce(data: dict, notebook: Notebook) -> ChatReply:
    """Raw tool output → a validated ChatReply, degrading DOWN only.

    Same stance as engine._coerce: the model's self-report is never taken at face value where taking
    it at face value could write something wrong into her brain.
    """
    text = str(data.get("text") or "").strip()
    kind = "proposal" if str(data.get("kind") or "").lower() == "proposal" else "answer"
    claim = str(data.get("claim") or "").strip()

    # A proposal with nothing to judge is not a proposal. Degrade rather than render a card whose
    # confirm button would write an empty line into the notebook.
    if kind == "proposal" and not claim:
        kind = "answer"

    if kind == "answer":
        cites = [c for c in (data.get("cites") or []) if str(c).strip()]
        return ChatReply(kind="answer", text=text or "Me not sure what to do with that one.", cites=cites)

    section = str(data.get("section") or "fact").lower()
    if section not in SECTIONS:
        section = "fact"

    consequence = str(data.get("consequence") or "").strip()
    resolved = _resolve_supersedes(str(data.get("supersedes") or ""), section, notebook)
    if data.get("supersedes") and not resolved:
        # It meant to replace something and named a line that is not there — she edited the notebook
        # since, or it paraphrased. Either way this is now an ADD, and saying so is the whole point:
        # a silent fallback is how the notebook ends up holding two answers to one question.
        consequence = (consequence + " " if consequence else "") + (
            "Me thought this replaced something you already told me, but me can't find that line any "
            "more — so this would be added alongside what's there."
        )
    if resolved:
        consequence = (consequence + "\n\n" if consequence else "") + f'Replaces: "{resolved}"'

    return ChatReply(
        kind="proposal", text=text or "Me hears a rule in that. Say it back before me writes anything down:",
        claim=claim, section=section, supersedes=resolved, consequence=consequence.strip(),
    )


def _user_message(message: str, transcript: list[dict]) -> str:
    """The turn, with enough of the conversation before it to resolve a follow-up.

    The transcript goes in the USER message rather than a messages array on purpose — see the module
    docstring. It is bounded here because this half of the prompt is never cached.
    """
    parts: list[str] = []
    prior = [t for t in (transcript or []) if str(t.get("text") or "").strip()][-MAX_TURNS:]
    if prior:
        parts.append("EARLIER IN THIS CONVERSATION (oldest first):")
        for turn in prior:
            who = "She said" if str(turn.get("from")) == "you" else "You said"
            body = " ".join(str(turn.get("text") or "").split())[:TURN_MAX_CHARS]
            parts.append(f"{who}: {body}")
        parts.append("")
    parts.append("SHE JUST SAID:")
    parts.append(message.strip())
    return "\n".join(parts)


def load_about(path) -> str:
    """The few lines about who Bean is, or "" when there is no such file.

    Bounded and forgiving: this is flavour, and failing to read it must never cost her a turn of
    conversation. Absent ⇒ "" ⇒ no block ⇒ a prompt byte-identical to before the file existed.
    """
    try:
        text = path.read_text(encoding="utf-8").strip()
    # Absent is the NORMAL case — no tenant ships one — and this is flavour, not grounding. An
    # unreadable file costs Bean a joke about itself; it cannot cost her an answer, because nothing
    # about her store lives here. Failing the turn over it would trade something that matters for
    # something that doesn't.
    except (FileNotFoundError, OSError, UnicodeDecodeError):
        return ""
    return text[:ABOUT_MAX_CHARS]


def reply(
    notebook: Notebook,
    message: str,
    transcript: list[dict],
    adapter: Model,
    *,
    about: str = "",
    max_tokens: int = 900,
) -> ChatReply:
    """One model call: (notebook + what she said) → an answer or a proposal.

    The notebook rides in the cached system prefix, exactly as it does for drafting — a second
    cached prefix, distinct from the engine's because the tool and instructions differ. With the 1h
    TTL that breaks even after about two turns, and chat sessions are multi-turn.
    """
    system = [
        {"type": "text", "text": _INSTRUCTIONS},
        {"type": "text", "text": notebook.render()},
    ]
    # Appended AFTER the notebook so the cached prefix stays ordered the way it has always been, and
    # so an empty `about` leaves the blocks byte-identical to before this existed.
    if about.strip():
        system.append({"type": "text", "text": "ABOUT ME (not store knowledge):\n" + about.strip()})
    result = adapter.structured(
        system=system, user=_user_message(message, transcript), tool=CHAT_TOOL, max_tokens=max_tokens
    )
    return _coerce(result.data, notebook)


DEFAULT_MODEL = DRAFT_MODEL
