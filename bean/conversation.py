"""When one customer writes two or three times before anyone answers.

The queue is keyed per Postmark `MessageID` (bean/inbound.py) and `/api/inbox` hands the client the
raw append-only log with no sort, filter, or dedupe. So three emails from one person render as three
unrelated rows and earn three separate drafts — and the operator reads all three to send one reply.

Measured on the live corpus: 81 of 224 senders wrote more than once, and 145 of 186 consecutive
same-sender pairs land within 72 hours. This is normal traffic, not an edge case.

The distinction that matters is NOT "did this person write before" — bean/customer_history.py
already answers that. It is which of the things they wrote is still owed an answer, and there are
two shapes:

  - a PILE-UP: they wrote twice or three times and nobody replied. One reply owes all of it, and it
    should acknowledge that they had to chase.
  - a BACK-AND-FORTH: she answered, they wrote back. Only the new message is owed anything, and a
    reply that re-litigates what she already settled is worse than no reply.

Telling them apart needs to know who spoke last, which is exactly what bean/quoting.py recovered
from the quoted history. Two independent signals, either sufficient: an earlier message in this
conversation that she marked approved/handled, or a store-side message sitting at the end of the
newest email's own quoted thread.

Pure: every function takes already-loaded logs. No I/O, no model calls.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from bean.quoting import OTHER, split_quoted
from bean.shelf import thread_key

# The operator actually dealt with it. Anything else — pending, skipped, or no entry at all — means
# the customer is still waiting. Same list, and the same reasoning, as customer_history._RESOLVED.
_RESOLVED = ("approved", "handled")

# How many messages of a conversation go in front of the model. The corpus worst case is a customer
# who wrote five times about one unresolved return; past that the older ones add tokens on every
# draft without changing the reply.
_MAX_IN_PROMPT = 5

# Each body in that block. Long enough to carry a real question, short enough that one ranting email
# cannot balloon a prompt that is billed per draft.
_BODY_MAX = 700


def conversation_key(sender_email: str, subject: str) -> str:
    """The conversation an email belongs to: who sent it, plus the subject with reply prefixes gone.

    `thread_key` (bean/shelf.py) already peels `Re:`/`Fwd:`/`[EXTERNAL]` repeatedly, and documents
    itself as crude because it over-groups identical subjects from DIFFERENT customers — which is
    precisely the failure the sender address closes. No Message-ID or References header survives the
    forward into Postmark, so this pair is the whole conversation handle Bean can have.
    """
    return f"{(sender_email or '').strip().lower()}\n{thread_key(subject)}"


def _result(item: dict) -> dict:
    return item.get("result") or {}


def is_filed(item: dict) -> bool:
    return _result(item).get("disposition") == "filed"


def rolled_into(item: dict) -> str:
    """The email whose draft now answers this one, or "" if it still stands alone."""
    return str(_result(item).get("rolled_into") or "")


def is_resolved(item: dict, status: dict) -> bool:
    return status.get(item.get("id")) in _RESOLVED


def store_spoke_last(item: dict) -> bool:
    """Does this email's own quoted history end with a message from someone other than the customer?

    That is the fingerprint of a back-and-forth: she replied, and they wrote back quoting her. Only
    a POSITIVE identification counts — `quoting.split_quoted` leaves attribution UNKNOWN when the
    delimiter carried no address, and treating unknown as "she replied" would silently downgrade a
    real pile-up into a routine follow-up.
    """
    messages = split_quoted(
        "\n".join(item.get("thread") or []),
        item.get("sender_email") or "",
        item.get("sender_name") or "",
    )
    return bool(messages) and messages[-1].side == OTHER


@dataclass(frozen=True)
class Conversation:
    """One customer's thread as the queue should present it: oldest→newest, with the tail that is
    still owed a reply called out."""

    key: str
    items: list[dict] = field(default_factory=list)
    unanswered: list[dict] = field(default_factory=list)
    answered_before: bool = False

    @property
    def pile_up(self) -> bool:
        """They wrote more than once and nobody has replied to any of it."""
        return len(self.unanswered) > 1 and not self.answered_before

    @property
    def latest(self) -> dict | None:
        return self.items[-1] if self.items else None


def build(items: list[dict], status: dict, key: str = "") -> Conversation:
    """Assemble one conversation from its emails (any order in, oldest→newest out)."""
    ordered = list(items)
    answered_before = any(is_resolved(i, status) for i in ordered[:-1])
    if ordered and store_spoke_last(ordered[-1]):
        answered_before = True

    # Everything after the last message she actually dealt with is still owed a reply. With nothing
    # resolved, that is the whole conversation.
    last_resolved = -1
    for idx, item in enumerate(ordered):
        if is_resolved(item, status):
            last_resolved = idx
    unanswered = [i for i in ordered[last_resolved + 1:] if not is_filed(i)]
    return Conversation(key=key, items=ordered, unanswered=unanswered, answered_before=answered_before)


def group(inbox: list[dict], status: dict) -> list[Conversation]:
    """Every conversation in the log, each oldest→newest, in order of first contact.

    Filed mail joins its conversation (so a mis-filed message is visible in context) but never
    counts as unanswered — recovering it is the Filed/FYI lane's job, not this one.
    """
    buckets: dict[str, list[dict]] = {}
    for item in inbox:
        key = conversation_key(item.get("sender_email") or "", item.get("subject") or "")
        buckets.setdefault(key, []).append(item)
    return [build(items, status, key) for key, items in buckets.items()]


def open_siblings(item: dict, inbox: list[dict], status: dict) -> list[dict]:
    """The earlier emails in this email's conversation that are still waiting on a reply.

    Called on the inbound path with the new email NOT yet appended to the log, so nothing needs to
    exclude it — but it is filtered by id anyway, because a Postmark webhook retry replays a
    delivered message and Bean must never treat an email as its own predecessor.
    """
    key = conversation_key(item.get("sender_email") or "", item.get("subject") or "")
    return [
        other
        for other in inbox
        if other.get("id") != item.get("id")
        and conversation_key(other.get("sender_email") or "", other.get("subject") or "") == key
        and not is_filed(other)
        and not is_resolved(other, status)
    ]


def _short(text: str, limit: int = _BODY_MAX) -> str:
    text = (text or "").strip()
    return text if len(text) <= limit else text[: limit - 1].rstrip() + "…"


def render_for_prompt(conversation: Conversation) -> str:
    """The block that makes ONE draft answer the whole conversation.

    Returns "" for a conversation with nothing outstanding before the newest message — a single
    email in hand is byte-identical to the pre-grouping prompt, so the common case stays a cache-warm
    no-op and costs nothing.

    Distinct from customer_history.render_prior_contact, which is about this PERSON across every
    subject and deliberately carries subjects only. This is one thread, and it carries the bodies,
    because a reply that answers three messages has to have read three messages.
    """
    earlier = conversation.unanswered[:-1][-_MAX_IN_PROMPT:]
    if not earlier:
        return ""

    lines = [
        f"THIS IS MESSAGE {len(conversation.unanswered)} IN AN ONGOING CONVERSATION. "
        f"{len(earlier)} earlier message(s) from this person are STILL UNANSWERED:"
    ]
    for item in earlier:
        when = (item.get("received_at") or "").strip() or "earlier"
        lines.append(f"--- {when} · \"{item.get('subject') or '(no subject)'}\"")
        lines.append(_short(item.get("body") or ""))

    if conversation.answered_before:
        lines.append(
            "You have already replied earlier in this thread. Answer ONLY what is new in the latest "
            "message. Do not re-explain or re-litigate anything that was already settled, and do not "
            "re-ask for anything they have already given you."
        )
    else:
        lines.append(
            "NOBODY HAS REPLIED TO ANY OF THESE. Write ONE reply that answers all of them together — "
            "not just the latest. Acknowledge that they had to write more than once; that is a "
            "customer losing patience, and it belongs in both the routing and the tone."
        )
    return "\n".join(lines)
