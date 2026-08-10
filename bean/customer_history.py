"""What Bean already knows about the person who just wrote in.

Bean met every customer as a stranger. On real traffic that is not a nicety — 32% of the operator's
mail (23 of 73 emails, 9 senders) is a repeat contact, and one customer wrote FIVE times in six days
about ONE unresolved return, each time landing as a brand-new email with no memory of the last.
Worse, their routing tree has an escalate trigger that literally reads "angry or repeat-contact
customers": a rule the model could never fulfil, because nothing ever told it this was the second
(or fifth) time. This module is what tells it.

The signal that matters is NOT "have we met" — it is "did anyone ever actually answer them".
"They have asked three times and nobody replied" and "we resolved this last week" are different
emails that deserve different replies, and only the join of inbox.jsonl (what arrived + what Bean
did) with status.json (what the operator did about it) can tell them apart.

A pure reader over the append-only logs — no writes, no model calls.
Bounded on purpose: the last few emails, subjects not bodies. It rides in the per-email USER message
(never the cached system prefix), so every token here is paid for on every leg of the walk; a chatty
history block would quietly tax every email Bean ever drafts.

`load_inbox()` reads the whole file per call (126 lines today, so: fine). When that stops being
fine, it is the same JSONL→SQLite migration trigger documented in bean/paths.py — a format upgrade,
not a re-architecture, and this reader is the shape of the query that would move.
"""

from __future__ import annotations

import json
from email.utils import parsedate_to_datetime
from pathlib import Path

from bean.inbox import load_inbox
from bean.paths import inbox_path, status_path

# The last N emails from this sender. Five is enough to see a pattern (the operator's worst real
# case was five) and few enough that the block stays a handful of lines on every model call.
_LIMIT = 5

# Subjects are the cheap answer to "what was it about". Truncated so one ranting subject line can't
# balloon the block on every leg of the walk.
_SUBJECT_MAX = 70

# The operator actually dealt with it. Anything else — pending, skipped, or no entry at all —
# means the customer is still waiting, which is the whole reason this module exists.
_RESOLVED = ("approved", "handled")


def _short(text: str, limit: int = _SUBJECT_MAX) -> str:
    text = " ".join((text or "").split())  # a subject can carry newlines; the block is line-oriented
    return text if len(text) <= limit else text[: limit - 1].rstrip() + "…"


def _when(received_at: str) -> str:
    """"Jul 9" from a payload Date. Falls back to the raw string — a date we can't parse is still
    worth showing (the ORDER of contact is the signal), and must never raise on the inbound path."""
    try:
        return parsedate_to_datetime(received_at).strftime("%b %-d")
    except (TypeError, ValueError):
        return (received_at or "").strip() or "earlier"


def _what_bean_did(result: dict) -> str:
    if result.get("disposition") == "filed":
        return f"Bean filed it ({result.get('kind') or 'not a reply'})"
    confidence = result.get("confidence")
    if confidence == "flag":
        return "Bean escalated it to you"
    if confidence == "low":
        return "Bean drafted a reply (unsure)"
    if confidence == "high":
        return "Bean drafted a reply"
    return "Bean triaged it"


def _what_the_operator_did(status: str | None) -> tuple[str, bool]:
    """(rendered outcome, resolved?). Absent status = nobody got to it — the loudest case."""
    if status == "approved":
        return "you approved + sent it", True
    if status == "handled":
        return "you handled it", True
    if status == "skipped":
        return "you skipped it", False
    return "NOBODY HAS REPLIED", False


def prior_contact(
    sender_email: str,
    inbox: list[dict],
    status: dict,
    *,
    exclude_id: str = "",
    limit: int = _LIMIT,
) -> list[dict]:
    """This sender's earlier emails, oldest→newest, capped at the `limit` most recent.

    Pure: takes the already-loaded logs. `exclude_id` drops the email being triaged right now — it
    is normally not in the log yet (the inbound handler appends AFTER triage), but a Postmark retry
    of a delivered webhook would replay it, and Bean must not cite the current email as its own
    prior contact. Unknown/blank sender ⇒ [] (never match everyone on "").
    """
    who = (sender_email or "").strip().lower()
    if not who:
        return []

    seen = []
    for item in inbox:
        if (item.get("sender_email") or "").strip().lower() != who:
            continue
        if exclude_id and item.get("id") == exclude_id:
            continue
        result = item.get("result") or {}
        # `.get` throughout: `thread` and friends are newer fields, and the real prod log predates
        # them — an old line missing a key is normal, not corrupt.
        outcome, resolved = _what_the_operator_did(status.get(item.get("id")))
        seen.append({
            "email_id": item.get("id") or "",
            "when": _when(item.get("received_at") or ""),
            "subject": _short(item.get("subject") or "(no subject)"),
            "category": result.get("category") or "",
            "bean_did": _what_bean_did(result),
            "outcome": outcome,
            "resolved": resolved,
        })
    return seen[-limit:]


def render_prior_contact(entries: list[dict]) -> str:
    """The block Bean puts in front of the model. "" when this is a genuinely new customer — so a
    first-time email is byte-identical to the pre-memory prompt (a cache-warm, zero-cost no-op)."""
    if not entries:
        return ""
    unresolved = sum(1 for e in entries if not e["resolved"])
    plural = "" if len(entries) == 1 else "s"
    header = f"PRIOR CONTACT — this person has written in {len(entries)} time{plural} before"
    header += f", and {unresolved} of those got no reply:" if unresolved else ", all resolved:"

    lines = []
    for e in entries:
        cat = f" · {e['category']}" if e["category"] else ""
        lines.append(f"- {e['when']} · \"{e['subject']}\"{cat} · {e['bean_did']} · {e['outcome']}")

    footer = (
        "This is NOT a new customer. Do not ask them for anything they already gave you above. "
        "An unanswered repeat contact is a customer losing patience — weigh that in both the "
        "routing and the tone."
        if unresolved else
        "This is NOT a new customer, and their earlier emails were dealt with — do not re-litigate "
        "them or re-ask for what they already gave you."
    )
    return "\n".join([header, *lines, footer])


def history_block(sender_email: str, *, exclude_id: str = "", customer: str | None = None) -> str:
    """The rendered prior-contact block for this sender, read live from the volume. "" for a new
    customer, an empty inbox, or an unreadable log — memory is an enhancement, and failing to
    remember must never take down the triage of the email actually in hand."""
    inbox = load_inbox(inbox_path(customer))
    status = _read_status_map(status_path(customer))
    return render_prior_contact(prior_contact(sender_email, inbox, status, exclude_id=exclude_id))


def _read_status_map(path: Path) -> dict:
    """status.json as a dict, or {} when absent/corrupt. Deliberately forgiving and local: a status
    file Bean can't read means "we don't know what she actioned", which degrades this block to
    "nobody has replied" — loud, and safe. (bean/server.py:_read_status is the same read for the UI;
    it logs, because there a silent reset would erase what she marked done.)"""
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    # Swallow-and-degrade is CORRECT here (not silent success): an unreadable status file means
    # "we don't know what she actioned", which renders as "nobody has replied" — loud and safe —
    # and must never take down the triage of the email in hand. The UI's read (server._read_status)
    # logs instead, because there a silent {} would erase what she marked done; this reader can't.
    except (FileNotFoundError, json.JSONDecodeError, OSError):
        return {}
    return data if isinstance(data, dict) else {}
