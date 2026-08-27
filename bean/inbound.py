"""Convert a Postmark inbound-parse webhook payload into Bean's Email contract.

Bean's designed onboarding is a mailbox-level auto-forward: the customer forwards support@ to a
Postmark inbound address and verifies their domain, and Bean sends *as* their brand. With an
auto-forward/redirect rule (not the manual "Forward" button) the original headers are preserved,
so `From`/`FromFull.Email` is the ORIGINAL CUSTOMER — not the operator's forwarding address. This module
maps that payload to an Email; the reply target (who Bean must actually answer) is exposed
separately so the handler can persist it without bloating the immutable Email dataclass.

Pure and testable: no I/O, stdlib only (no bs4) — an HTML body is tag-stripped with html/re.
"""

from __future__ import annotations

import html
import re

from bean.contract import Email
from bean.quoting import THREAD_DELIM_RE as _THREAD_DELIM_RE

_TAG_RE = re.compile(r"<[^>]+>")
_WS_RE = re.compile(r"[ \t]*\n[ \t\n]*")

# Where a reply's latest message ends and the quoted history begins lives in bean/quoting.py now —
# that module splits the SAME delimiters into the individual messages the UI and the prompt read, so
# a second copy here would be two definitions that drift. Not exhaustive — the model also gets the
# thread, so a near-miss just moves a little text between body and thread, it doesn't lose anything.


def _strip_html(raw: str) -> str:
    """Minimal HTML→text: drop tags, unescape entities, collapse runs of whitespace. Good enough
    to give the model readable text; not a full renderer (no dependency for that)."""
    # Block-ish tags become newlines so paragraphs don't run together, then all tags go.
    text = re.sub(r"(?i)<br\s*/?>|</p>|</div>|</li>", "\n", raw)
    text = _TAG_RE.sub("", text)
    text = html.unescape(text).replace("\xa0", " ")  # &nbsp; → a normal space
    text = _WS_RE.sub("\n", text)
    return text.strip()


def _split_thread(text: str) -> tuple[str, str]:
    """Split a reply into (latest message, quoted history). The quoted history is kept whole
    (delimiter line included) so the model reads it as prior context. No delimiter → all latest."""
    m = _THREAD_DELIM_RE.search(text)
    if not m:
        return text.strip(), ""
    return text[: m.start()].strip(), text[m.start():].strip()


def _body_and_thread(payload: dict) -> tuple[str, list[str]]:
    """The latest message for `body`; earlier quoted messages for `thread` (context for the draft
    and the confidence call). `StrippedTextReply` is the latest message with quotes already removed;
    `TextBody`/HTML carries the full conversation — we keep body = latest and thread = the remainder.
    Falls through TextBody → stripped HTML when a field is empty (same order as before)."""
    full = (payload.get("TextBody") or "").strip() or _strip_html(payload.get("HtmlBody") or "")
    latest, prior = _split_thread(full)
    body = (payload.get("StrippedTextReply") or "").strip() or latest
    return body, ([prior] if prior else [])


def _sender_email(payload: dict) -> str:
    return (payload.get("FromFull") or {}).get("Email") or payload.get("From") or ""


def _sender_name(payload: dict) -> str:
    return (payload.get("FromFull") or {}).get("Name") or payload.get("FromName") or ""


def reply_target(payload: dict) -> str:
    """Where Bean's reply must go: the customer's Reply-To if present, else their From. NOT the
    forwarding address — Bean answers the customer, not the operator's mailbox."""
    return (payload.get("ReplyTo") or "").strip() or payload.get("From") or ""


def email_from_postmark(payload: dict) -> Email:
    """Map a Postmark inbound payload to an Email. Sender = the ORIGINAL customer (headers are
    preserved by an auto-forward rule, Bean's onboarding pattern); a relay/contact-form whose real
    customer is in the body is resolved later, dynamically, by the gate. `thread` carries the quoted
    history so the draft and confidence call have context."""
    body, thread = _body_and_thread(payload)
    return Email(
        id=str(payload.get("MessageID") or ""),
        sender_name=_sender_name(payload),
        sender_email=_sender_email(payload),
        subject=payload.get("Subject") or "",
        body=body,
        thread=thread,
    )
