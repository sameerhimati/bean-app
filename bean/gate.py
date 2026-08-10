"""The pre-generation triage gate — needs-reply vs file, BEFORE the routing tree.

Not all inbound needs a reply (newsletters, receipts, shipping notifications, spam). The gate
answers one cheap question — "is this a customer message that needs a human reply?" — so the
routing tree stays about "where does this customer question go" (not "is this even my mail")
and filed mail costs $0 of drafting.

Two layers, rules before model:

  1. The operator's rules (Config.gate): case-insensitive substring matches against the sender
     address and subject. `alwaysReply` wins over `alwaysFile` — a customer must never be
     filed by a sloppy rule. Rules short-circuit the model call entirely.
  2. A classify-tier model call with a deliberately asymmetric default: WHEN UNSURE, REPLY.
     A newsletter that reaches the tree wastes cents; a customer filed to the FYI lane is a
     ghosted customer — the same trust failure as a bluffed draft, so the gate fails open.

Filed mail is sorted & demoted, NEVER hidden: it stays visible in a collapsed inbox lane, and
the one-tap "needs a reply" recovery routes it through the tree AND logs a mis-file correction
(the gate's training signal, on the same JSONL log as every other correction).
"""

from __future__ import annotations

from dataclasses import dataclass

from bean.llm import CLASSIFY_MODEL, Model, live_model

GATE_TOOL = {
    "name": "gate",
    "description": "Decide whether this inbound email needs a human reply or can be filed.",
    "input_schema": {
        "type": "object",
        "properties": {
            "needs_reply": {
                "type": "string",
                "enum": ["yes", "no"],
                "description": "Does this email need a reply from the support team? "
                               "If you are not sure, say yes.",
            },
            "kind": {
                "type": "string",
                "enum": ["customer", "newsletter", "promo", "receipt", "notification",
                         "spam", "cold-outreach", "other"],
                "description": "What this email is. 'customer' whenever needs_reply is yes.",
            },
            "reason": {
                "type": "string",
                "description": "One plain sentence the operator will read, e.g. "
                               "'A marketing newsletter from a supplier — no question in it.'",
            },
            # Envelope resolution — filled ONLY when needs_reply is yes. For relay mail (a website
            # contact-form, a forwarded message) the real customer lives in the body, not the From.
            "customer_name": {
                "type": "string",
                "description": "The real customer's name — from the body when the sender is a "
                               "relay/contact-form; otherwise the From name.",
            },
            "customer_email": {
                "type": "string",
                "description": "The real customer's email address — from the body for a "
                               "relay/contact-form; otherwise the From address.",
            },
            "reply_target": {
                "type": "string",
                "description": "Where a reply must be sent — normally the same as customer_email.",
            },
            "customer_message": {
                "type": "string",
                "description": "The customer's own words, VERBATIM (do not paraphrase or "
                               "summarize) — the actual message with any contact-form template "
                               "boilerplate stripped out.",
            },
        },
        "required": ["needs_reply", "kind", "reason"],
    },
}

# Static and tiny — the gate runs on every inbound email, so it gets its own lean system
# prompt instead of the big drafting prefix (which it doesn't need and shouldn't pay for).
GATE_SYSTEM = (
    "You are the front gate of a small ecommerce brand's customer-support inbox. Decide "
    "whether an inbound email NEEDS A HUMAN REPLY or can be quietly filed.\n\n"
    "Needs a reply: anything from a customer or prospective customer — questions, complaints, "
    "order issues, returns, praise that expects acknowledgement, and any human asking the "
    "business for something (including wholesale or partnership inquiries: a human wrote them "
    "and expects an answer, even if the operator handles them personally).\n\n"
    "Can be filed: newsletters, marketing/promotional blasts, automated receipts and shipping "
    "or delivery notifications, platform/system notifications, spam, and untargeted cold "
    "outreach.\n\n"
    "THE DEFAULT IS ASYMMETRIC: filing a real customer ghosts them, which is far worse than "
    "letting an automated email through. If you are at all unsure, answer needs_reply=yes.\n\n"
    "SOME MAIL ARRIVES THROUGH A RELAY. A website contact-form (e.g. a Shopify 'You received a "
    "new message from your online store's contact form' notice) or a forwarded message shows the "
    "platform/relay as the sender, but the REAL customer's name, email, and message are inside "
    "the body. When needs_reply is yes, resolve them: set customer_name, customer_email, and "
    "reply_target to the actual customer (never the relay address), and copy their own words "
    "verbatim into customer_message, dropping the template boilerplate. For ordinary mail a "
    "customer sent directly, these just equal the From address and the body."
)


@dataclass(frozen=True)
class GateResult:
    disposition: str  # "reply" | "file"
    kind: str = "customer"  # GATE_TOOL kinds, or "rule" when one of the operator's rules decided
    reason: str = ""  # one plain sentence for the UI
    # Envelope resolution — populated on the reply path when the model resolves a relay/contact-form
    # to the real customer. Empty on file/rule paths; the handler falls back to the From headers.
    customer_name: str = ""
    customer_email: str = ""
    reply_target: str = ""
    customer_message: str = ""  # the customer's own words, template boilerplate stripped


def gate_user(email) -> str:
    parts = [f"FROM: {email.sender_name} <{email.sender_email}>", f"SUBJECT: {email.subject}", "", email.body]
    if getattr(email, "thread", None):
        parts += ["", "--- earlier in this thread (oldest→newest) ---", *email.thread]
    return "\n".join(parts)


def _match_rule(email, patterns: list[str]) -> str | None:
    """First pattern that appears (case-insensitive) in the sender address or subject."""
    hay = f"{email.sender_email}\n{email.subject}".lower()
    return next((p for p in patterns if p and p.lower() in hay), None)


def needs_reply(email, *, model: Model | None = None, rules: dict | None = None) -> GateResult:
    """Gate one email. `rules` is Config.gate ({'alwaysReply': [...], 'alwaysFile': [...]})."""
    rules = rules or {}
    if hit := _match_rule(email, rules.get("alwaysReply", [])):
        return GateResult("reply", "rule", f"Matches your always-reply rule “{hit}”.")
    if hit := _match_rule(email, rules.get("alwaysFile", [])):
        return GateResult("file", "rule", f"Matches your always-file rule “{hit}”.")

    model = model or live_model(CLASSIFY_MODEL)
    data = model.structured(
        system=[{"type": "text", "text": GATE_SYSTEM}], user=gate_user(email), tool=GATE_TOOL,
        max_tokens=300,
    ).data
    if data.get("needs_reply") == "no":
        return GateResult("file", data.get("kind", "other"), data.get("reason", ""))
    return GateResult(
        "reply", "customer", data.get("reason", ""),
        customer_name=data.get("customer_name", ""),
        customer_email=data.get("customer_email", ""),
        # reply_target defaults to the resolved customer_email when the model omits it.
        reply_target=data.get("reply_target") or data.get("customer_email", ""),
        customer_message=data.get("customer_message", ""),
    )
