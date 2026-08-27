"""Filesystem layout for Bean's mutable, per-customer state.

Everything that must survive a container redeploy lives under the DATA ROOT. In dev that's
`<repo>/data`; in prod set `BEAN_DATA_DIR` to a mounted Railway volume so redeploys don't wipe
the correction log — which *is* the moat.

Per-customer state is one subfolder per customer (`data/{customer}/`): no cross-tenant mixing,
no schema, no DB. The shared baseline (fixtures seed, calibration goldens) ships in the image;
only accrued per-customer state lives here. Adding customer #2 is a new folder, not a migration.

MIGRATION TRIGGER (decided in office-hours, not yet due): the per-customer logs (corrections
AND the order index) stay JSONL until EITHER (a) true product-page RAG is added, OR (b) loading
the whole log per draft is wasteful. At that point it becomes SQLite + sqlite-vec on this same
volume — a format upgrade, not a re-architecture.

MULTITENANCY TRIGGER (not yet due, and further off than it looks): every function here already
takes a `customer`, and `bean/server.py` now passes one from a single named constant instead of
letting the default fire at eight call sites. That is the whole seam. Customer #2 today costs an
env var (BEAN_CUSTOMER) and a directory — deliberately NOT auth, signup, billing, or a tenant
table, none of which a zero-second-customer product has earned.

What forces the real thing: A SECOND PAYING CUSTOMER. Not a second folder, not a demo account —
a second party whose mail must not be visible to the first. That is the moment the one shared
BEAN_PASSCODE stops being authentication and becomes a password everyone knows, because "who is
this request for?" gets answered by an env var baked into the process rather than by the request
itself. What changes when it fires: identity moves from the process to the request (a session
carrying a customer id), `CUSTOMER` stops being a module constant, and the passcode becomes a
per-customer credential. Until that day, one process serves one customer, and the isolation
guarantee is the operating system's, not ours.
"""

from __future__ import annotations

import os
from pathlib import Path
from zoneinfo import ZoneInfo

_REPO_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_DATA_ROOT = _REPO_ROOT / "data"

# The tenant this process serves when BEAN_CUSTOMER is unset. It is the DEMO store, not a real one:
# a checkout with no env set must not resolve to a live customer's folder, or a stray local script
# reads — or writes — someone's actual mail. Production names its tenant explicitly (BEAN_CUSTOMER is
# set on the Railway service), so this default is what developers, tests, and demos get, and the safe
# answer for all three is the fictional store. This default WAS a real live tenant's folder name
# until 2026-07-29 — i.e. for most of Bean's life, any unset checkout pointed straight at
# production data. That is the bug this constant now exists to prevent.
DEFAULT_CUSTOMER = "maplemoss"


def data_root() -> Path:
    """The volume root. Override with BEAN_DATA_DIR (the mount point in prod)."""
    return Path(os.environ.get("BEAN_DATA_DIR", DEFAULT_DATA_ROOT))


def default_customer() -> str:
    """The single tenant this process serves. Override with BEAN_CUSTOMER."""
    return os.environ.get("BEAN_CUSTOMER", DEFAULT_CUSTOMER)


def customer_dir(customer: str | None = None) -> Path:
    return data_root() / (customer or default_customer())


def waitlist_path() -> Path:
    """Beta-waitlist signups — app-global, NOT per-customer, so it sits at the data root rather than
    under a customer folder. One JSON line per signup (email + ts). Same volume, same append-only
    shape as the per-customer logs; it holds prospects, not any tenant's mail."""
    return data_root() / "waitlist.jsonl"


def last_inbound_path(customer: str | None = None) -> Path:
    """The inbound heartbeat: an ISO timestamp, rewritten ONLY by a successful `/api/inbound`.

    The alarm this feeds ("has mail stopped arriving?") used to read the mtime of `inbox.jsonl` /
    `orders.jsonl` — every file inbound writes. That looked equivalent and wasn't: those files have
    OTHER writers. A maintenance push (re-drafting her inbox and copying it back) reset the mtime and
    the alarm reported `hours_since_inbound: 0.0` with zero mail arriving, for 24h, while inbound had
    been dead for four days. The one monitor that exists to detect silence was forged by a routine
    write — see [[data-captured-then-dropped]], inverted: the writer was fine, the READER attributed
    the timestamp to the wrong cause.

    A dedicated marker with exactly one writer cannot be forged that way. It holds the timestamp as
    CONTENT rather than relying on its own mtime, so restoring a volume backup can't fake a heartbeat
    either — and it's greppable by hand, which is what an operator debugging a dead pipe actually needs.
    """
    return customer_dir(customer) / "last_inbound.txt"


def config_path(customer: str | None = None) -> Path:
    return customer_dir(customer) / "config.json"


def notebook_path(customer: str | None = None) -> Path:
    """The per-store notebook (bean/notebook.py) — the model-neutral brain rendered as the engine's
    cached system prefix. Lives on the volume like every other per-customer artifact; there is NO
    shipped default beside it. A fixture here would be the fixtures-leaked-into-prod bug again: the
    first save would persist a demo brain over her live one. The distiller writes it once, she
    approves it in the editor, and from then on it's hers."""
    return customer_dir(customer) / "notebook.md"


def about_path(customer: str | None = None) -> Path:
    """A few lines about who Bean is and who made it — read ONLY by the chat, never by the engine.

    ⚠️ The separation is the whole point. The notebook rides in the cached system prefix of every
    DRAFT and its lines are citable, so a fact about the author living in it could be quoted into a
    reply to a customer. This file is loaded by bean/chat.py alone; bean/engine.py never opens it,
    and a test pins that. Conversation and drafting are different audiences.

    On the volume with no shipped default, for the same two reasons notebook.md has none: the source
    tree is public, and a shipped default containing real data is the fixtures-leaked-into-prod bug.
    Absent ⇒ no block at all ⇒ the chat prompt is byte-identical to what it was before this existed.

    It is NOT a second brain. Nothing here should answer a question about the store — the "me not
    know that one" refusal for anything outside the notebook stays exactly as it is.
    """
    return customer_dir(customer) / "about.md"


def notebook_history_path(customer: str | None = None) -> Path:
    """Every change ever made to the notebook, append-only — the audit trail for the operator's brain.

    One line per changed row: what section, whether it was added/removed/changed, the text before and
    after, and where the edit came from. Written at PUT /api/notebook, the single writer every path
    goes through (the chat, the notebook editor, the cite sheet, the questionnaire), so the trail is
    complete by construction rather than by each caller remembering to log.

    It exists because supersede shipped without it: a confirmed chat proposal REPLACES a line in her
    brain, and the only record was one rolling `.bak`. "What did Bean change?" had no answer.
    Distinct from corrections.jsonl, which is feedback on DRAFTS; this is edits to the notebook.
    Same volume + migration trigger as the corrections log."""
    return customer_dir(customer) / "notebook_history.jsonl"


def notebook_review_path(customer: str | None = None) -> Path:
    """Her in-progress answers while walking the onboarding questionnaire — a small resume file, NOT
    the notebook. The 45-card review is meant to be done across several sittings; this holds what she
    has decided so far (which cliffs she reworded, which rows she dropped, her sign-off) so leaving
    mid-walk loses nothing. Deliberately separate from notebook.md: the notebook is written ONCE, at
    approval (the consent invariant — Bean never drafts from a half-approved brain), and this file is
    cleared the moment that approval lands. Same small-JSON-on-the-volume shape as status.json."""
    return customer_dir(customer) / "notebook_review.json"


def status_path(customer: str | None = None) -> Path:
    """Per-email action state (approved | handled | skipped | pending) keyed by email id — a small
    JSON map, not a log. Server-side (not browser localStorage) so the operator's "what's done" is
    one Bean across their devices; open it on phone and desktop and the same mail reads as handled."""
    return customer_dir(customer) / "status.json"


def corrections_path(customer: str | None = None) -> Path:
    return customer_dir(customer) / "corrections.jsonl"


def usage_path(customer: str | None = None) -> Path:
    """The token/cost log: one line per REAL model call (bean/llm.py), carrying tokens + purpose so
    Bean's spend is inspectable per category over time. FakeModel calls (unit tests) never write here
    — no real token counts. Same volume + migration trigger as the corrections log."""
    return customer_dir(customer) / "usage.jsonl"


# The operator's own working day. Every date the product SHOWS her — the stats chart's bars, the
# inbox timestamps (web/bean-ui.jsx BEAN_TZ, which must stay in step with this) — is rendered in
# this zone, never in UTC and never in the sender's.
#
# Mail arrives stamped with the SENDER's offset, so bucketing on that splits one of her days across
# two bars whenever a customer writes from another zone; bucketing on UTC pushes everything after
# 7pm Central into tomorrow. Neither is the day she lived through.
#
# A constant because Bean serves one operator. The day it serves a second in another zone this
# becomes a per-tenant setting — which is a config field and a plumbing change, not a redesign, and
# it is deliberately NOT built ahead of that day. (Same stance as this module's MULTITENANCY
# TRIGGER.) `zoneinfo` is stdlib and DST-aware, so CST/CDT is handled without a dependency.
OPERATOR_TZ = ZoneInfo("America/Chicago")


def filed_history_path(customer: str | None = None) -> Path:
    """Per-day tallies of gate-filed mail that has since been DELETED from inbox.jsonl.

    `inbox.clear_filed` rewrites the inbox to drop filed mail, which is the right behaviour for an
    inbox and the wrong one for a record: "Bean filed 430 emails you never had to open" is the
    strongest number the stats page has, and it lived entirely in a log one tap wipes. Before each
    rewrite, clear_filed rolls up what it is about to delete into this log — day + count, nothing
    else. It is append-only and never rewritten, so the counts survive every future clear.

    Deliberately NOT the `.bak-filed-*` snapshot clear_filed already writes: that is a full recovery
    copy of the mail (right for undoing a bad filing, wrong to re-read on every page load), and it
    accumulates one file per clear. This carries the two fields the chart needs and nothing that
    could leak a customer's words. Same volume + migration trigger as the corrections log."""
    return customer_dir(customer) / "filed_history.jsonl"


def order_index_path(customer: str | None = None) -> Path:
    """The email-derived order index: one line per OrderRecord parsed from the store's own order-
    and shipping-confirmation mail (bean/order_index.py). This is what grounds a WISMO reply
    without a Shopify token — Bean learns each order from the notifications the store already
    emails. Same volume + migration trigger as the corrections log."""
    return customer_dir(customer) / "orders.jsonl"


def inbox_path(customer: str | None = None) -> Path:
    """The live triaged inbox: one line per email received via the inbound webhook (bean/inbox.py),
    each carrying its stored DraftResult so the UI renders without re-running the model. Mail the
    gate FILES (non-receipt newsletters/promos/spam) also lands here — with a filed verdict, not a
    draft — so a gate false-negative is recoverable, never silently dropped; store notification mail
    is the exception (it feeds the order index instead). Same volume + migration trigger as the
    corrections log."""
    return customer_dir(customer) / "inbox.jsonl"
