"""The backend: one stdlib `http.server` serving both the API and the web UI, same-origin (no CORS,
one process). It drafts with the notebook engine (`bean/engine.py`) — the routing tree it used to
carry was deleted once the notebook replaced it in production.

  * `GET  /api/config`   → the current Config as JSON (fixtures default when the file is absent)
  * `PUT  /api/config`   → persist the operator's edits to the volume; round-trips on the next GET
  * `GET/PUT /api/notebook` → the operator's editable brain, and the questionnaire that fills it
  * `POST /api/preview`  → draft one email and return the result
  * `POST /api/inbound`  → the Postmark inbound-parse webhook: gate → draft → append to the inbox
  * everything else      → static files from web/

Two seams keep it testable offline (no live model, no repo file):
  * `CONFIG_PATH` and its sibling path constants — monkeypatched to tmp paths in tests.
  * `DRAFT_MODEL` / `gate` — module-level, monkeypatched to FakeModel-backed callables in tests.

Robustness: a malformed config on disk degrades to the fixtures default (never 500); bad request
bodies are 400, not 500; a model failure is a 502 that leaves the mail for Postmark to retry.
"""

from __future__ import annotations

import base64
import binascii
import hashlib
import hmac
import json
import logging
import mimetypes
import os
import re
import threading
import time
from datetime import datetime, timezone
from dataclasses import replace
from http.cookies import SimpleCookie
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from urllib.parse import parse_qs, urlsplit

from bean.config import Config, ConfigCorruptError, load_config
from bean.corrections import Correction, CorrectionsCorruptError, few_shot_examples, load, record
from bean.paths import (
    config_path, corrections_path, customer_dir, data_root, default_customer, inbox_path,
    last_inbound_path, notebook_path, notebook_review_path, order_index_path,
    status_path, waitlist_path,
)
from bean.adapter import ModelAdapter
from bean.contract import Email, draft_dict
from bean.customer_history import history_block
from bean.engine import draft_email
from bean.gate import needs_reply as gate  # injectable gate seam (monkeypatched offline in tests)
from bean.inbound import email_from_postmark, reply_target
from bean.inbox import InboxItem, append_inbox, clear_filed, load_inbox
from bean.llm import DRAFT_MODEL, OutOfCreditsError
from bean.notebook import Notebook, load as load_notebook
from bean.shelf import top_exemplars
from bean.notification_parser import parse_notification
from bean.outcomes import approval_rates, drafting_stalled, recent_outcomes
from bean.store import write_json_atomic
from bean.order_index import record_order

log = logging.getLogger("bean.server")

# The one tenant this process serves. Every path below is per-customer already (bean/paths.py takes
# a `customer` argument); until now the server let that argument default, so the live tenant's name
# was a fact spread across eight call sites instead of a value passed from one. Naming it here is the whole
# tenant seam: adding customer #2 is an env var and a directory, not a hunt for implicit defaults.
# It is NOT multitenancy — one process still serves exactly one customer. See paths.py for the
# event that forces the real thing.
CUSTOMER: str = default_customer()

# Paths. CONFIG_PATH lives on the data volume (survives redeploys, see bean/paths.py);
# WEB_DIR is the static root, shipped in the image.
_REPO_ROOT = Path(__file__).resolve().parent.parent
CONFIG_PATH: Path = config_path(CUSTOMER)
NOTEBOOK_PATH: Path = notebook_path(CUSTOMER)  # her editable brain (bean/notebook.py); markdown on the volume
CORRECTIONS_PATH: Path = corrections_path(CUSTOMER)  # the learning-signal log; same volume, overridable in tests
STATUS_PATH: Path = status_path(CUSTOMER)  # per-email action state; server-side so it's one Bean across devices
NOTEBOOK_REVIEW_PATH: Path = notebook_review_path(CUSTOMER)  # questionnaire resume state; cleared on approval
WEB_DIR: Path = _REPO_ROOT / "web"

# Request-body caps (bytes). A Postmark inbound payload legitimately carries base64-encoded
# attachments (Postmark's own ceiling is ~35 MB), so dropping a real customer email is the exact
# sin to avoid — the webhook gets a generous cap and every other endpoint a tight one. An uncapped
# read of the client's Content-Length is a trivial pre-auth OOM.
_MAX_BODY = 1 * 1024 * 1024
_MAX_INBOUND_BODY = 35 * 1024 * 1024

# One process, one volume: a single lock makes every read-modify-write (config PUT, status upsert)
# atomic under ThreadingHTTPServer, closing the read→compare→write races. It does NOT cover a future
# multi-instance deploy — that would need a file lock or a real store.
_STATE_LOCK = threading.Lock()

# Is THIS process the demo tenant? Only a deployment can say so, which is why it's an env var and not
# inferred from the customer name — inferring it would let a real tenant turn off their own
# serving-demo-data alarm by being unluckily named. Serving the demo store's fields is a FAILURE for
# a real operator (see `_health`) and the definition of correct for the demo, and nothing in the
# config itself distinguishes those two cases. Off unless explicitly set.
DEMO_TENANT: bool = os.environ.get("BEAN_DEMO_TENANT", "").strip().lower() in ("1", "true", "yes")

# The write lock that makes a Bean safe to point the internet at (bean/demo.py). Set
# BEAN_DEMO_READONLY=1 and every route below stops existing: nothing persists, nothing spends a
# token, and the state a visitor creates lives only in their React tree until they refresh. Two
# visitors never see each other's clicks, and neither one can leave anything behind.
#
# 404 IS LOAD-BEARING — DO NOT "FIX" IT TO A 403. The client already has a name for a write that
# 404s: "there is no backend here, this is the static demo", and it stays deliberately silent
# (web/bean-inbox.jsx:41, :134, :176, :238/:260/:266, :477). Any other status — 403 included — is
# read as the server actively refusing a save, which fires `_signalWriteFailed` and puts a red toast
# in front of a visitor who did nothing wrong. So the honest-looking status is the one that produces
# the dishonest experience here, and the "wrong" one produces the right one: a fully clickable UI
# that quietly forgets. (Two routes do NOT have that tolerance and will surface an error —
# /api/clear-filed and /api/preview; see _DEMO_LOCKED_ROUTES.)
DEMO_READONLY: bool = os.environ.get("BEAN_DEMO_READONLY", "").strip().lower() in ("1", "true", "yes")

# Every route that would write to the volume or spend a token. Matched on the PATH, and checked only
# in the POST/PUT routers — so `GET /api/config`, `GET /api/status` and `GET /api/notebook` keep
# working normally while their write twins vanish. A route belongs here if it persists anything
# (correction/status/config/notebook/review/clear-filed/inbound) or calls a model (preview).
#
# /api/waitlist is deliberately ABSENT: it is the public beta signup, it is the one write a demo
# visitor legitimately wants to make, and it is already rate-limited, deduped and hard-capped for
# exactly this exposure. /api/auth is absent because it writes nothing to disk.
#
# Two of these are known to break the silence, and it is worth knowing which: `POST /api/clear-filed`
# raises on any non-ok (bean-inbox.jsx:880) and bean-root.jsx:570 turns that into a "Couldn't clear
# the filed mail — nothing was deleted" toast, which is at least TRUE. `POST /api/preview` likewise
# throws (bean-inbox.jsx:343), and the paste screen renders "Bean couldn't reach the server — is it
# running?" — misleading, but it is also the only route that spends money, so it stays locked and the
# copy is the thing to fix if that screen matters for the demo.
_DEMO_LOCKED_ROUTES = frozenset({
    "/api/correction", "/api/status", "/api/clear-filed", "/api/config",
    "/api/notebook", "/api/notebook/review", "/api/preview", "/api/inbound",
})

# The parsed notebook, memoized on (path, mtime): a PUT rewrites the file (Notebook.save → os.replace,
# atomic, so a concurrent read never tears), mtime bumps, and the next draft reparses. Keyed on path
# too because tests point NOTEBOOK_PATH at a fresh tmp file each run. Its own lock, not _STATE_LOCK —
# the atomic save makes the read safe without entangling the drafting path's lock discipline.
_notebook_cache: dict = {}
_notebook_cache_lock = threading.Lock()


def _current_notebook() -> Notebook:
    """The live notebook, reparsed only when the file changes. Raises FileNotFoundError if absent —
    the caller (the notebook engine path) treats that as a 503, never a fabricated default."""
    path = NOTEBOOK_PATH
    mtime = path.stat().st_mtime  # FileNotFoundError propagates when there's no notebook yet
    with _notebook_cache_lock:
        if _notebook_cache.get("key") == (path, mtime):
            return _notebook_cache["nb"]
        nb = load_notebook(path)
        _notebook_cache["key"], _notebook_cache["nb"] = (path, mtime), nb
        return nb

# In-process rate limiting — a dict + a lock, no dependency, sufficient for a single-instance deploy.
# _RATE_LOCK guards all three structures.
_RATE_LOCK = threading.Lock()
_auth_failures: dict[str, list[float]] = {}   # ip -> recent failed-auth timestamps
_auth_lockouts: dict[str, float] = {}          # ip -> locked-until (epoch seconds)
_inbound_hits: list[float] = []                # recent inbound-webhook timestamps (global — one tenant)
_preview_hits: list[float] = []                # recent /api/preview timestamps (global — one operator)
_waitlist_hits: list[float] = []               # recent /api/waitlist timestamps (global — public)
_AUTH_MAX_FAILURES = 10    # failures within the window before an IP is locked out
_AUTH_WINDOW = 300         # seconds the failure count looks back over
_AUTH_LOCKOUT = 300        # seconds an IP stays locked out
_INBOUND_MAX_PER_MIN = 60  # ceiling for a one-customer webhook; a spend guard on unbounded model calls

# /api/waitlist is PUBLIC (no passcode — that is the point) and it WRITES to the volume. That combination
# is exactly what the auth-lockout note refused to build: an unauthenticated write is a way to fill the
# disk the operator's corrections live on. So it is defended three ways. The rate limit is the real defense —
# 15/min is generous for humans typing an email and slow enough that filling the disk would take weeks.
# The count cap is a hard backstop, and it refuses HONESTLY rather than faking success (a real person
# past the cap is told to email, not lied to). And the email is length-capped before it is ever stored.
_WAITLIST_MAX_PER_MIN = 15
_WAITLIST_MAX = 20_000
_EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")

# /api/preview is the most expensive call in the app — a full drafting call over the notebook +
# shelf exemplars, plus any attached images. It had no ceiling.
#
# 20/min is drawn from what one human clicking can plausibly generate: a preview takes several
# seconds of model latency to return, so one operator cannot personally exceed ~10/min even pasting
# as fast as they read. Double that is headroom for a burst of pasted emails; anything beyond it is a
# loop or a stolen passcode, and neither should be able to spend unbounded tokens.
#
# Counted globally rather than per-IP on purpose: behind Railway's proxy every request presents the
# same client address, so a per-IP window would be a global window wearing a disguise. One customer,
# one operator, one ceiling.
_PREVIEW_MAX_PER_MIN = 20

# _auth_failures / _auth_lockouts stay IN MEMORY, and reset on redeploy. That is a real weakness —
# a patient attacker retries exactly when the process restarts — and it is still the right call:
# persisting them would put a WRITE on the volume behind an UNAUTHENTICATED endpoint (/api/auth is
# reachable before any secret is checked), trading a brute-force slowdown for a way to fill the disk
# that the operator's corrections live on. The lockout was never the defense; the passcode's entropy is,
# backed by a spend cap in the Anthropic console. A redeploy is operator-initiated and rare, and it
# only helps an attacker who is mid-window at that exact moment. Decided, not deferred.


class _PayloadTooLarge(Exception):
    """The client's declared Content-Length exceeds the endpoint cap. `_read_json` sends the 413
    itself and raises this; `do_POST`/`do_PUT` catch it to unwind without touching the socket."""


def _production() -> bool:
    """True only when a deploy has set BEAN_ENV=production — the switch that turns a missing gate
    secret from fail-OPEN (dev convenience) into fail-CLOSED (prod safety). Anything else — unset,
    "dev", "test" — keeps the fail-open convention, so local dev and the offline suite need no env."""
    return os.environ.get("BEAN_ENV", "").strip().lower() == "production"


# The commit this container is running. Railway leaves RAILWAY_GIT_COMMIT_SHA empty for `railway up`
# (it uploads a directory, not a git build), so scripts/deploy.sh writes the SHA here just before
# uploading. The file is deliberately NOT gitignored: `railway up` honours .gitignore, so an ignored
# file would never reach the image — verified against the running container.
_BUILD_SHA_FILE = Path(__file__).resolve().parent / "_build_sha.txt"


def _build_sha() -> str:
    try:
        return _BUILD_SHA_FILE.read_text(encoding="utf-8").strip() or "unknown"
    except OSError:
        return "unknown"


def _session_token(secret: str) -> str:
    """The value of the `bean_auth` cookie: derived from the passcode, never the passcode itself.

    Bean used to set `bean_auth=<the passcode>`. The password WAS the session token — sent on every
    request, sitting in her browser for a month, and written verbatim into anything that logs a
    Cookie header. HttpOnly/Secure/SameSite cover most of that, but not the part that matters: a
    passcode is typed by a human, and humans reuse passcodes.

    This does not make the cookie less powerful — it is still a bearer token, and anyone holding it
    is authenticated. It makes the cookie no longer *be* the secret. Rotating BEAN_PASSCODE still
    invalidates every session, because the token is derived from it.
    """
    return hmac.new(secret.encode(), b"bean-session-v1", hashlib.sha256).hexdigest()


def _stamp_inbound() -> None:
    """Record that mail just landed. The ONLY writer of the inbound heartbeat (bean/paths.py).

    Called at each successful `/api/inbound` exit and nowhere else — that exclusivity IS the fix, so
    resist calling it from a script, a backfill, or a retriage. A rejected or retried inbound (401,
    429, 402, or a 503 the gate/triage raised) deliberately does not stamp: Postmark will retry, and
    until one succeeds no mail has actually been written down.

    Best-effort. A monitor that can take down the mail path is worse than a monitor that misses a
    beat, so a failure here is logged and swallowed — the email has already been persisted by the
    caller, and the next inbound re-stamps.
    """
    try:
        path = last_inbound_path(CUSTOMER)
        path.parent.mkdir(parents=True, exist_ok=True)
        # Not write_json_atomic, deliberately: this is a bare timestamp, not JSON, and a torn write
        # cannot corrupt anything a reader trusts. _last_inbound() already refuses to guess at an
        # unparseable marker and reports "never" instead — so the worst a half-written stamp can do
        # is under-report freshness, which fails toward the alarm rather than away from it. The
        # atomicity rule exists to stop a crash from destroying accumulated state; there is no
        # accumulated state here, and the next inbound rewrites the file whole.
        path.write_text(datetime.now(timezone.utc).isoformat(timespec="seconds"), encoding="utf-8")
    except OSError as exc:
        log.warning("could not stamp the inbound heartbeat: %s", exc)


def _last_inbound() -> tuple[str | None, float | None]:
    """When did mail last actually LAND? (iso timestamp, hours ago) — or (None, None) if never.

    Reads the dedicated marker, whose only writer is `_stamp_inbound` on a successful `/api/inbound`.
    It used to read the mtime of `inbox.jsonl`/`orders.jsonl`, which have other writers: a maintenance
    push forged a 24-hour green while inbound had been dead for four days (bean/paths.py
    last_inbound_path tells the full story). One writer, one meaning.

    The marker's CONTENT is the timestamp, not its mtime — so a volume restore can't fake a heartbeat.
    """
    try:
        stamped = last_inbound_path(CUSTOMER).read_text(encoding="utf-8").strip()
    except (FileNotFoundError, OSError):
        return None, None
    try:
        last = datetime.fromisoformat(stamped)
    # A marker we can't parse is not a heartbeat. Report "never" rather than guess at a time — the
    # alarm's whole value is that it does not invent good news.
    except ValueError:
        log.warning("inbound heartbeat %r is not an ISO timestamp — reporting no heartbeat", stamped)
        return None, None
    return last.isoformat(timespec="seconds"), round((datetime.now(timezone.utc) - last).total_seconds() / 3600, 1)


def _health() -> tuple[bool, dict]:
    """What `/healthz` reports. Cheap, no customer data, and ZERO model calls.

    "Deploy complete" from Railway means an image was pushed. It does not mean Python exists inside
    it — a root package.json once made nixpacks build a Node image with no interpreter, and the only
    thing that noticed was a human running curl. So the deploy script believes this endpoint and
    nothing else, and it checks the three things that have actually broken:

      sha            — is the code I just shipped the code that is serving?
      config_ok      — did the volume mount, with a parseable config on it?
      corrections    — how many corrections loaded? The learning loop is connected iff this is > 0.
      config_is_demo — is this tenant serving the DEMO store's fields? See below.
      inbound_stale  — has mail STOPPED ARRIVING? See below.
      drafting_stalled — is mail arriving and Bean drafting NOTHING? The one check that measures
                       whether Bean is USEFUL rather than merely well-configured. See below.

    Counts only. A correction count says nothing about what any email contained.

    (`config_nodes` / `taught_leaves` / `answer_leaves` were the routing tree's adoption counters.
    They went with the tree in the notebook cutover — an inert count of a structure nothing walks
    is worse than no count, because it reads as a health signal. `drafting_stalled` is the honest
    successor: it measures outcomes on real mail instead of the shape of the config.)

    `config_is_demo_default` exists because "the config parsed" is not enough. The live tenant's
    config loaded fine and healthz stayed green while their CATEGORIES had been silently replaced by
    the Maple & Moss demo store's — byte-for-byte — and every model call carried "Sizing & Fit" for
    a store that sells nothing of the kind. It went unnoticed for weeks because nothing looked at
    that half of the config. An
    absent config.json serves the demo default and the next save persists it (bean/config.py), so
    this is a real failure mode, not a hypothetical: it already fired once. A tenant serving demo
    fields is NOT healthy, so it fails the check — loud beats silent, per this codebase's doctrine.
    """
    out: dict = {"sha": _build_sha(), "customer": CUSTOMER}
    ok = True

    try:
        cfg = load_config(CONFIG_PATH)
        out["config_ok"] = True
        # Identity against the whole shipped category set — NOT name overlap. A real tenant may
        # legitimately share a name with the demo ("Wholesale", "Escalation"); the first real one
        # did. Only an exact match on names+descriptions+templates means "this is the demo store,
        # not a tenant".
        out["config_is_demo_default"] = cfg.categories == Config.from_fixtures().categories
        # Still REPORTED for the demo tenant, just not fatal: the demo serving demo fields is the
        # point, and failing it would make the demo instance permanently un-deployable
        # (scripts/deploy.sh gates on this endpoint). For everyone else it stays a hard failure.
        ok = ok and (DEMO_TENANT or not out["config_is_demo_default"])
    except Exception as exc:  # noqa: BLE001 - healthz must never 500; it reports, it does not raise
        ok = False
        out["config_ok"] = False
        out["config_error"] = type(exc).__name__

    # IS MAIL ACTUALLY ARRIVING? The one question a mail-triage agent must be able to answer, and
    # the one thing nothing here used to report. Inbound died at the Postmark test-mode cap on
    # 2026-07-11 and every counter above stayed green for two days: Bean was perfectly healthy and
    # perfectly deaf. Silence is this product's real failure mode, and it looked exactly like health.
    #
    # REPORTED, NOT FAILED — deliberately. `ok` drives a 503, and scripts/deploy.sh gates on that,
    # so failing here would mean a quiet mailbox blocks every deploy and rolls back good code. The
    # deploy gate answers "is the code I shipped serving?"; this answers "is the pipe open?". Two
    # different questions, and conflating them breaks the one that has to stay trustworthy.
    try:
        last, hours = _last_inbound()
        out["last_inbound_at"] = last
        out["hours_since_inbound"] = hours
        # A store taking 30-50 emails/day does not go a full day silent. Longer than that means the
        # forward, Postmark, or the webhook is down — go look, don't wait for someone to wonder.
        if hours is not None:
            out["inbound_stale"] = hours > 24
        else:
            # No heartbeat yet, which is two very different situations. A tenant with an inbox but no
            # marker HAS received mail before (this deploy predates the marker, or it was lost) — that
            # is genuinely unexplained silence. A tenant with neither has simply never received mail:
            # a fresh deploy or a new store, where nothing has *stopped*. Reporting the latter as
            # stale is a false alarm on every new tenant's first day, and an alarm that cries on day
            # one is an alarm nobody reads by day ten.
            out["inbound_stale"] = inbox_path(CUSTOMER).exists()
    except OSError as exc:  # a stat() failure is not worth a 503, and is not a CONFIG error
        log.warning("could not read the inbound timestamps: %s", exc)
        out["inbound_stale"] = None

    # IS BEAN ACTUALLY DRAFTING? Every field above answers "is Bean correctly configured and
    # honestly cautious?" — and a Bean that flags 100% of the operator's mail passes all of them. It
    # did, for weeks: the config counters that used to sit here could read "0 of 48 leaves taught" —
    # i.e. it flags 100% of the mail — and that number was never compared to anything. `drafting_stalled`
    # is that comparison, made against real outcomes instead of the config: mail IS arriving and
    # Bean drafted NONE of it.
    #
    # REPORTED, NOT FAILED — same reasoning as `inbound_stale`, and it matters more here. `ok` drives
    # a 503, a 503 makes scripts/deploy.sh roll back, and rolling back the code does not teach the
    # tree. The failure this reports lives in the operator's config, not in the build; failing the deploy
    # gate on it would revert good code and still flag every email.
    try:
        recent = recent_outcomes(load_inbox(inbox_path(CUSTOMER)))
        out["walked_emails"] = recent.walked
        out["drafted_fraction"] = round(recent.drafted_fraction, 3)
        out["drafting_stalled"] = drafting_stalled(recent)
    except Exception as exc:  # noqa: BLE001 - an unreadable inbox is not a 503; healthz reports
        log.warning("could not read the recent outcomes: %s", exc)
        out["drafting_stalled"] = None

    # The data root exists and is writable. A permission check, not a probe write: /healthz is
    # unauthenticated, and a write per poll would be a way to spin the disk from the outside.
    # A volume that failed to mount is caught anyway — config_ok/config_is_demo_default would flip.
    root = data_root()
    out["volume_writable"] = root.is_dir() and os.access(root, os.W_OK)
    ok = ok and out["volume_writable"]

    try:
        _corrections = load(CORRECTIONS_PATH)
        out["corrections_loaded"] = len(_corrections)
        out["corrections_ok"] = True
        # The north star, surfaced: of the drafts she acted on, what share went out untouched. This is
        # the only number that says Bean saved her time — drafted_fraction says it TRIED, this says it
        # LANDED. Reported, never failed (an empty denominator reads 0.0, not a 503).
        _rates = approval_rates(_corrections)
        out["approval_rate"] = round(_rates.rate, 3)
        out["approval_outcomes"] = _rates.counts
    except CorrectionsCorruptError as exc:
        ok = False
        out["corrections_ok"] = False
        out["corrections_error"] = str(exc)

    return ok, out


def _safe_image_paths(paths: list) -> list[str]:
    """Confine client-supplied image paths to the data root. A preview payload's `image_paths` flow
    verbatim to `Path(path).read_bytes()` in the model call (bean/llm.py), so an unconfined path is
    an arbitrary local-file read — `["/app/.env"]` would base64 a secret into an Anthropic request.
    Config-supplied knowledge images were trusted separately and never passed
    here. Raises ValueError (→ 400 at the boundary) on anything outside the root."""
    root = data_root().resolve()
    safe: list[str] = []
    for p in paths:
        resolved = Path(str(p)).resolve()
        if not resolved.is_relative_to(root):
            raise ValueError(f"image path outside data root: {p}")
        safe.append(str(resolved))
    return safe


# bean/llm.py can only encode PNG and JPEG, and it declares the media type from the EXTENSION — so
# the extension has to follow the bytes, not the sender's claim, or a mislabelled image is a 400 the
# inbound path doesn't catch (which loses the email + retries forever). We therefore sniff the magic
# bytes and rename to match; the .heic an iPhone actually sends, a GIF, a PDF, or non-image bytes
# wearing a .png name are all skipped, not written.
_PNG_MAGIC = b"\x89PNG\r\n\x1a\n"
_JPEG_MAGIC = b"\xff\xd8\xff"
# Per-image and per-email ceilings. _MAX_INBOUND_BODY (35 MB) bounds the whole payload, but not one
# image (the API has a per-image ceiling a real 26 MB photo would blow) nor the COUNT (a 35 MB body
# of ~70-byte PNGs is >100k files). Both would otherwise become an uncaught downstream failure.
_MAX_IMAGE_BYTES = 4 * 1024 * 1024
_MAX_IMAGES_PER_EMAIL = 10


def _sniff_image(raw: bytes) -> str | None:
    """The extension the truth of the BYTES demands ('.png'/'.jpg'), or None for a non-image. The
    sender's Name and ContentType are a claim; these bytes are what actually get base64'd into the
    model call, so they are what we trust."""
    if raw.startswith(_PNG_MAGIC):
        return ".png"
    if raw.startswith(_JPEG_MAGIC):
        return ".jpg"
    return None


def _save_inbound_images(payload: dict, email_id: str, *, customer: str | None = None) -> list[str]:
    """Persist a Postmark payload's image attachments under the data root; return their paths.

    Two of the operator's real stalled emails say "see attached photo/video" — on a defect claim the
    photo IS the evidence, and Bean was reasoning about a reply it could not see. Attachments were extracted
    by Postmark and dropped on the floor here.

    Hostile by assumption: `Name` is attacker-controlled, so the basename is taken (killing
    `../../.env` and absolute paths), and the composed path is re-checked against the data root by
    `_safe_image_paths` — belt AND braces, because a path escape here writes attacker bytes to disk
    AND feeds them to a model read. Content is trusted by its MAGIC BYTES, not its name/type, so
    garbage wearing a .png name never reaches the API. A single bad attachment (a non-image,
    oversize, undecodable base64, unwritable disk) is skipped with a log, never raised: an unreadable
    photo must not cost the operator the email it came with (Postmark would retry a 500 forever).
    """
    saved: list[str] = []
    attachments = payload.get("Attachments") or []
    if not isinstance(attachments, list):
        return saved
    # One folder per email keeps names from colliding across senders who all attach "IMG_1234.jpg".
    # email_id is sanitized to one path segment: "/" and friends become "_", and leading/trailing
    # dots are stripped — a bare ".." would otherwise survive as a real parent-dir hop and land the
    # file one level up, in the customer dir itself. (`_safe_image_paths` still contains it to the
    # data root regardless — a webhook simply must not choose its own write directory.)
    safe_id = re.sub(r"[^A-Za-z0-9._-]", "_", email_id).strip(".") or "unknown"
    outdir = customer_dir(customer) / "attachments" / safe_id

    for att in attachments:
        if not isinstance(att, dict) or len(saved) >= _MAX_IMAGES_PER_EMAIL:
            continue
        claimed = Path(str(att.get("Name") or "")).name  # basename only — for the log line, never trusted
        try:
            raw = base64.b64decode(str(att.get("Content") or ""), validate=True)
            kind = _sniff_image(raw)
            if kind is None or len(raw) > _MAX_IMAGE_BYTES:
                log.info("inbound %s: skipping attachment %r (%d bytes, sniffed %r)",
                         email_id, claimed, len(raw), kind)
                continue
            # The stored name is stem-from-the-claim + extension-from-the-bytes: a readable filename
            # that can't lie to bean/llm.py's extension→media-type inference.
            stem = re.sub(r"[^A-Za-z0-9._-]", "_", Path(claimed).stem)[:64] or "image"
            dest = _safe_image_paths([outdir / f"{stem}{kind}"])[0]  # confinement re-checked
            Path(dest).parent.mkdir(parents=True, exist_ok=True)
            Path(dest).write_bytes(raw)
        except (ValueError, binascii.Error, OSError) as exc:
            log.warning("inbound %s: skipping attachment %r (%s)", email_id, claimed, exc)
            continue
        saved.append(dest)
    return saved


# Serve .jsx as real JavaScript. The app's scripts are plain React.createElement (no JSX syntax),
# loaded as ordinary <script> tags now that Babel is gone — Safari refuses to execute a script
# served as application/octet-stream (the default for .jsx), which would blank the app.
mimetypes.add_type("application/javascript", ".jsx")

# Branding assets that bypass the passcode gate (see _gate): no data, needed by the tab + unauthed
# link-preview crawlers. Served statically from web/.
#
# Everything /join embeds has to clear the passcode gate, for the same reason /join itself is
# unauthed: the whole point is that someone who does NOT have a passcode can see it. The video
# shipped 401ing on the first deploy — the page rendered fine and the frame was dead, which is the
# worst version of that bug, because nothing errors and the only person who sees it working is the
# one already holding a session cookie.
#
# `/demo.mp4` was removed from this set on 2026-08-10 along with the file. The note here had said
# "it is the fictional demo store's mail, so there is nothing in it to protect" — the BRAND in that
# footage was fictional, but the product category in it paraphrased the real customer's, which is
# identifying on its own. A made-up name over a real vertical is not a synthetic fixture.
#
# It is back, later the same day, re-recorded against the CURRENT demo tenant (furniture) by
# scripts/record_demo.mjs. The condition it returned under is the one to re-check if it is ever
# regenerated: the footage must be of whatever tenant the demo actually serves today.
_PUBLIC_ASSETS = {
    "/favicon.png", "/favicon.ico", "/apple-touch-icon.png",
    "/apple-touch-icon-precomposed.png", "/og-bean.png", "/demo.mp4",
}

# The app's front door. Unauthed, these serve the passcode page with a 200, because a login page is
# a real page — every other path gets a 401 (see _gate).
#
# This set exists only because `/` currently does two jobs: the front door AND the login screen. The
# og: tags therefore live on the passcode page, and iMessage/Slack only render a preview from a 2xx.
# WHEN A LANDING PAGE SHIPS, SHRINK THIS SET: `/` becomes a public marketing page (200, cacheable,
# no gate at all, and it carries the og: tags), the app lives behind /Bean.html, and this can go to
# the empty set — every unauthed path 401s, which is what we actually want.
_ENTRY_PATHS = {"/", "", "/Bean.html"}

# The origin this deployment is publicly reachable at, e.g. "https://bean.example.com". Unset by
# default ON PURPOSE. This used to be one instance's hostname, hardcoded — which meant every fork
# and every self-hosted copy shipped og: tags pointing at somebody ELSE's server: their link
# previews fetched an image from a host they don't run, and og:url told every crawler the canonical
# address of this page was an instance the operator has no control over.
PUBLIC_URL = os.environ.get("BEAN_PUBLIC_URL", "").rstrip("/")

# Open Graph + favicon head, shared by the passcode page and the app so a shared link previews as
# Bean (iMessage/Slack) and the tab shows the bean. Crawlers want an ABSOLUTE og:image, so we emit
# one only when BEAN_PUBLIC_URL says what "absolute" means here; with it unset we fall back to the
# relative path (most scrapers resolve it against the fetched URL) and omit og:url entirely. A
# degraded preview on an unconfigured instance beats a correct preview of the wrong instance.
# NOTE: web/Bean.html carries a STATIC copy of these same tags — it is plain HTML with no server
# templating, so it can only ever use the relative form. Change one, change the other.
_OG_IMAGE = f"{PUBLIC_URL}/og-bean.png" if PUBLIC_URL else "/og-bean.png"
_OG_URL = f'<meta property="og:url" content="{PUBLIC_URL}">\n' if PUBLIC_URL else ""
_BRAND_HEAD = f"""<link rel="icon" type="image/png" href="/favicon.png">
<link rel="apple-touch-icon" href="/apple-touch-icon.png">
<meta property="og:type" content="website">
<meta property="og:title" content="Bean — inbox triage">
<meta property="og:description" content="Bean reads your support mail and drafts replies for you to approve. You stay in control — Bean never sends.">
<meta property="og:image" content="{_OG_IMAGE}">
{_OG_URL}<meta name="twitter:card" content="summary_large_image">"""

# Shown for every unauthed page GET when BEAN_PASSCODE is set. Self-contained (inline CSS+JS,
# no external assets), so the browser never asks for the app's jsx/css until the cookie is set.
_PASSCODE_PAGE = ("""<!doctype html>
<html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Bean</title>
""" + _BRAND_HEAD + """
<style>
  :root { color-scheme: light; }
  * { box-sizing: border-box; }
  body { margin: 0; min-height: 100vh; display: grid; place-items: center;
         background: #F4EEE1; color: #2C2620;
         font: 15px/1.5 "Courier Prime", "Courier New", Courier, monospace; }
  form { width: min(90vw, 340px); text-align: center; background: #FFFDF8;
         border: 1.5px solid #E7DECE; border-radius: 18px; padding: 30px 26px 26px;
         box-shadow: 0 12px 30px rgba(44,38,32,0.08); }
  .beanmark { display: block; margin: 0 auto 8px; }
  .mark { font-size: 22px; font-weight: 700; margin-bottom: 6px; }
  .sub { color: #6E6457; margin-bottom: 22px; font-size: 14px; }
  input { width: 100%; padding: 11px 13px; border-radius: 10px; border: 1.5px solid #E7DECE;
          background: #FBF7EE; color: #2C2620; font-size: 15px; font-family: inherit; }
  input:focus { outline: none; border-color: #6E4327; }
  button { width: 100%; margin-top: 10px; padding: 11px; border: 0; border-radius: 10px;
           background: #6E4327; color: #FBF7EE; font-size: 15px; font-weight: 700;
           font-family: inherit; cursor: pointer; }
  button:hover { background: #5A3620; }
  .err { color: #B4472E; min-height: 20px; margin-top: 12px; font-size: 13px; }
</style></head>
<body>
  <form id="bean-passcode" onsubmit="return unlock(event)">
    <svg class="beanmark" width="44" height="44" viewBox="-6 -6 128 118" aria-hidden="true">
      <g transform="rotate(-14 59 55)">
        <path d="M 34 18 C 22 20 14 34 14 50 C 14 76 32 92 56 92 C 80 92 104 78 104 52 C 104 32 96 18 84 18 C 74 18 66 30 60 34 C 54 30 46 18 34 18 Z" fill="#6E4327"/>
        <ellipse cx="40" cy="42" rx="13" ry="7" fill="#ffffff" opacity="0.2" transform="rotate(-32 40 42)"/>
      </g>
    </svg>
    <div class="mark">Bean</div>
    <div class="sub">Pop in the passcode and me let you in.</div>
    <input id="pc" type="password" autofocus autocomplete="current-password" placeholder="Passcode">
    <button type="submit">Unlock</button>
    <div class="err" id="err"></div>
  </form>
  <script>
    async function unlock(e) {
      e.preventDefault();
      const err = document.getElementById('err');
      err.textContent = '';
      const r = await fetch('/api/auth', {
        method: 'POST', headers: {'Content-Type': 'application/json'},
        body: JSON.stringify({passcode: document.getElementById('pc').value}),
      });
      if (r.ok) { location.reload(); } else { err.textContent = "Hmm, me don't know that one."; }
      return false;
    }
  </script>
</body></html>
""")


# The public beta landing page (GET /join). Self-contained + inline, exactly like _PASSCODE_PAGE, so
# it ships in the image and needs no static-file serving through the gate. Carries _BRAND_HEAD so the
# link itself previews as Bean when the beta invite is shared.
_JOIN_PAGE = ("""<!doctype html>
<html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Bean — private beta</title>
""" + _BRAND_HEAD + """
<style>
  :root { color-scheme: light; }
  * { box-sizing: border-box; }
  body { margin: 0; min-height: 100vh; display: grid; place-items: center; padding: 24px;
         background: #F4EEE1; color: #2C2620;
         font: 15px/1.6 "Courier Prime", "Courier New", Courier, monospace; }
  main { width: min(92vw, 560px); text-align: center; background: #FFFDF8;
         border: 1.5px solid #E7DECE; border-radius: 20px; padding: 38px 30px 30px;
         box-shadow: 0 14px 34px rgba(44,38,32,0.09); }
  .badge { display: inline-block; font-size: 12px; letter-spacing: .04em; color: #6E4327;
           background: #F1E7D6; border: 1px solid #E1D4BE; border-radius: 999px;
           padding: 4px 12px; margin-bottom: 18px; }
  h1 { font-size: 22px; margin: 0 0 6px; }
  .lede { color: #4A4238; margin: 0 auto 20px; font-size: 15px; max-width: 34ch; }
  ul { list-style: none; padding: 0; margin: 0 0 24px; text-align: left; display: inline-block;
       color: #4A4238; font-size: 14px; }
  li { margin: 5px 0; padding-left: 20px; position: relative; }
  li::before { content: "•"; color: #6E4327; position: absolute; left: 4px; }
  form { display: flex; flex-direction: column; gap: 10px; }
  input { width: 100%; padding: 12px 13px; border-radius: 10px; border: 1.5px solid #E7DECE;
          background: #FBF7EE; color: #2C2620; font-size: 15px; font-family: inherit; }
  input:focus { outline: none; border-color: #6E4327; }
  button { padding: 12px; border: 0; border-radius: 10px; background: #6E4327; color: #FBF7EE;
           font-size: 15px; font-weight: 700; font-family: inherit; cursor: pointer; }
  button:hover { background: #5A3620; }
  button:disabled { opacity: .55; cursor: default; }
  .msg { min-height: 20px; margin-top: 6px; font-size: 13px; }
  .msg.ok { color: #3E6B4A; }
  .msg.err { color: #B4472E; }
  .foot { margin-top: 18px; font-size: 12px; color: #8A8073; }
  /* The page used to be three bullet points of claims. A visitor deciding whether to hand over an
     email address wants to SEE the thing sort mail and say what it isn't sure about — that is the
     whole pitch, and it is not a sentence. Muted+playsinline+loop so it behaves like a GIF but at
     a third the bytes; `preload=metadata` so the page still paints fast on a phone. */
  .demo { margin: 0 0 20px; }
  .demo video { width: 100%; display: block; border-radius: 12px; border: 1.5px solid #E7DECE;
                background: #F4EEE1; }
  .demo .cap { margin-top: 9px; font-size: 12px; color: #8A8073; }
  .demo .cap a { color: #6E4327; }
</style></head>
<body>
  <main>
    <svg width="46" height="46" viewBox="-6 -6 128 118" aria-hidden="true">
      <g transform="rotate(-14 59 55)">
        <path d="M 34 18 C 22 20 14 34 14 50 C 14 76 32 92 56 92 C 80 92 104 78 104 52 C 104 32 96 18 84 18 C 74 18 66 30 60 34 C 54 30 46 18 34 18 Z" fill="#6E4327"/>
        <ellipse cx="40" cy="42" rx="13" ry="7" fill="#ffffff" opacity="0.2" transform="rotate(-32 40 42)"/>
      </g>
    </svg>
    <div class="badge">PRIVATE BETA · one shop at a time</div>
    <h1>Bean reads your support mail<br>and drafts the replies.</h1>
    <p class="lede">You approve with one tap. Bean never sends on its own, and never fakes an answer it isn't sure of.</p>
    <!-- Pulled 2026-08-10 and restored the same day. The first recording showed the previous demo
         tenant's actual product category, which is identifying even with a fictional brand over it;
         this one is the current furniture tenant, regenerated by scripts/record_demo.mjs. The live
         demo link stays regardless — it is the same inbox, interactive, and it cannot go stale. -->
    <div class="demo">
      <video src="/demo.mp4" autoplay muted loop playsinline preload="metadata"
             aria-label="Bean sorting an inbox into ready-to-send, worth-a-look, and needs-you"></video>
      <div class="cap">A real inbox, sorted surest first &mdash;
        <a href="https://bean-demo-production.up.railway.app">click through it yourself</a>.
        No signup, no API key.</div>
    </div>
    <ul>
      <li>Learns your fields, your templates, your voice</li>
      <li>Flags the ones it can't answer instead of guessing</li>
      <li>Works on your existing inbox — no migration</li>
    </ul>
    <form id="wl" onsubmit="return join(event)">
      <input id="email" type="email" autocomplete="email" placeholder="you@yourshop.com" required>
      <button type="submit" id="go">Get on the list</button>
      <div class="msg" id="msg"></div>
    </form>
    <div class="foot">me Bean. me be in touch.</div>
  </main>
  <script>
    async function join(e) {
      e.preventDefault();
      const msg = document.getElementById('msg');
      const go = document.getElementById('go');
      msg.className = 'msg'; msg.textContent = '';
      go.disabled = true;
      try {
        const r = await fetch('/api/waitlist', {
          method: 'POST', headers: {'Content-Type': 'application/json'},
          body: JSON.stringify({email: document.getElementById('email').value}),
        });
        const d = await r.json().catch(() => ({}));
        if (r.ok && d.ok) {
          msg.className = 'msg ok';
          msg.textContent = "You're on the list. me be in touch.";
          document.getElementById('email').disabled = true;
        } else {
          msg.className = 'msg err';
          msg.textContent = d.error || "Hmm, that didn't go through. Try again?";
          go.disabled = false;
        }
      } catch (err) {
        msg.className = 'msg err';
        msg.textContent = "Me couldn't reach the server. Try again?";
        go.disabled = false;
      }
      return false;
    }
  </script>
</body></html>
""")


def _run_engine(email: Email, config, corrections: list) -> tuple[object, dict]:
    """One email → (result, stored-dict) via the notebook engine.

    Returns a DraftResult (situation bucket, groundedness, citations) from ONE model call over the
    notebook + shelf exemplars, serialized by draft_dict and stored opaquely by the caller. Called
    INSIDE the caller's try/except, so a model/network failure delays the mail (Postmark retries).
    The notebook load is mtime-cached and self-locked; the model call itself holds no lock (the
    caller appends under _STATE_LOCK afterwards).

    `config` is unused by this engine and kept only so the two call sites read the same — the
    notebook, not the config, is where the operator's judgment lives now."""
    notebook = _current_notebook()  # FileNotFoundError if the notebook isn't approved yet
    email_text = f"{email.subject}\n{email.body}"
    exemplars = top_exemplars(corrections, email_text, k=3)
    history = history_block(email.sender_email, exclude_id=email.id, customer=CUSTOMER)
    adapter = ModelAdapter(DRAFT_MODEL)
    result = draft_email(notebook, exemplars, list(email.thread), history, email, adapter)
    return result, draft_dict(result)


def _read_status(path: Path) -> dict:
    """The per-email action map from the volume, or {} when absent or corrupt.

    A MISSING file is the normal case (fresh customer, offline demo) and stays silent. A file that
    exists but won't parse is not normal: it silently resets everything the operator marked handled,
    and they'd only notice as "didn't I already do this?". There is no `.bak` here and nothing
    customer-facing is lost, so we still degrade to {} rather than refuse — but never without saying
    so. `load_config` holds that line; this was the one place that quietly didn't.
    """
    try:
        data = json.loads(path.read_text())
    # An absent status file is a customer who has actioned nothing yet, not a failure.
    except FileNotFoundError:
        return {}
    # Unreadable: degrade to {} (worst case she re-marks a few emails handled — no .bak exists and
    # nothing customer-facing is lost) but never quietly. The log line above is the "never quietly".
    except (json.JSONDecodeError, OSError) as exc:
        log.warning("status file %s is unreadable (%s) — starting from an empty action map", path, exc)
        return {}
    if not isinstance(data, dict):
        log.warning("status file %s is not a JSON object — starting from an empty action map", path)
        return {}
    return data


def _read_notebook_review(path: Path) -> dict:
    """Her in-progress questionnaire answers, or {} when absent/corrupt. Absent is the normal case
    (she hasn't started, or she finished and it was cleared). Same degrade-loudly-to-{} stance as
    _read_status: this is resume convenience, never customer-facing data, so a bad file costs her at
    most a restart of the walk — but we say so rather than silently drop her progress."""
    try:
        data = json.loads(path.read_text())
    # Absent is the NORMAL state, not a failure: she hasn't started the walk, or she finished it and
    # the approval cleared this file. Nothing to say — {} means "start at card 1", which is correct.
    except FileNotFoundError:
        return {}
    # Present but unreadable IS a failure, so it is logged below before degrading. Her progress is
    # resume convenience only (the notebook itself is written by the guarded approval PUT), so the
    # cost is re-walking cards, never lost work.
    except (json.JSONDecodeError, OSError) as exc:
        log.warning("notebook-review file %s is unreadable (%s) — starting the walk fresh", path, exc)
        return {}
    if not isinstance(data, dict):
        log.warning("notebook-review file %s is not a JSON object — starting the walk fresh", path)
        return {}
    return data


def _learning_summary(corrections: list[Correction]) -> dict:
    """Aggregate the correction log per category — the first read surface over the moat. For each of
    the operator's final buckets: counts by action, their 👍/💬 reinforcement, how many edits actually
    changed content (substantive), how many usable few-shot exemplars the DRAFT step now has (the real
    selector, not a reimplementation), and a small recent drafted-vs-sent sample (what Bean wrote vs
    what they actually sent) — the honest answer to 'is Bean improving?'."""
    by_cat: dict[str, list[Correction]] = {}
    for c in corrections:
        by_cat.setdefault(c.category, []).append(c)
    out: dict[str, dict] = {}
    for cat, rows in by_cat.items():
        actions: dict[str, int] = {}
        for r in rows:
            actions[r.action] = actions.get(r.action, 0) + 1
        edits = [r for r in rows if r.action == "edit" and r.original_draft and r.final_text]
        out[cat] = {
            "actions": actions,
            "liked": sum(1 for r in rows if r.liked),
            "noted": sum(1 for r in rows if r.note),
            "substantive_edits": sum(1 for r in rows if r.edit_kind == "substantive"),
            "few_shot_exemplars": len(few_shot_examples(corrections, cat)),
            "drafted_vs_sent": [
                {"original_draft": r.original_draft, "final_text": r.final_text} for r in edits[-3:]
            ],
        }
    return out


# The gate's labelled errors, both directions. `misfile` = Bean filed it and she wanted a reply;
# `should-file` = Bean drafted a reply and she wanted it filed.
_GATE_ERROR_DIRECTION = {"misfile": "alwaysReply", "should-file": "alwaysFile"}

# Two sightings before Bean says anything. One is an anecdote — a single odd email from a domain she
# otherwise wants — and proposing on it would train her to dismiss the surface.
_GATE_PROPOSAL_MIN = 2

# Consumer mailbox providers. A DOMAIN rule is never proposed for these, because this is where her
# actual customers live: two filed emails from @gmail.com is two customers, not a pattern, and
# "always file everything from @gmail.com" would bury her real support mail silently. Caught by
# running the surface on demo data and watching it offer @yahoo.com — exactly the mistake the gate
# is not allowed to make. The specific ADDRESS is still proposable; only the domain roll-up is not.
_PUBLIC_MAIL_DOMAINS = frozenset({
    "@gmail.com", "@yahoo.com", "@hotmail.com", "@outlook.com", "@live.com", "@msn.com",
    "@icloud.com", "@me.com", "@mac.com", "@aol.com", "@proton.me", "@protonmail.com",
    "@gmx.com", "@mail.com", "@yandex.com", "@zoho.com", "@comcast.net", "@verizon.net",
    "@att.net", "@sbcglobal.net", "@bellsouth.net", "@cox.net", "@charter.net", "@earthlink.net",
})


def _gate_proposals(corrections: list[Correction], config) -> list[dict]:
    """Gate rules Bean thinks are worth adding, derived from the mis-files she has already logged.

    This exists because the gate was the one part of Bean that could not learn. `misfile` and
    `keep-filed` are described in the UI as "the gate's training signal", but nothing ever read them:
    `needs_reply()` takes the email and `config.gate`, and `config.gate` was only ever hand-typed in
    Settings. So every time she corrected the gate, the correction went into the log and changed
    nothing — the writer was right and the consumer did not exist.

    Proposals only. Nothing here is applied: an accepted proposal is one tap in "What I handle", which
    keeps the operator on the one decision Bean must never take by itself. Filing a real customer is
    the single mistake the gate is not allowed to make, and an auto-applied `alwaysFile` is exactly
    how that would happen quietly.

    Grouped by sender address AND its domain, because both are real rules: one noisy newsletter
    address, or a whole internal domain whose mail is never customer support.
    """
    existing = {
        str(p).strip().lower()
        for key in ("alwaysReply", "alwaysFile")
        for p in (getattr(config, "gate", None) or {}).get(key, [])
        if str(p).strip()
    }
    # pattern -> {direction: {"count": n, "subjects": [...]}}; a pattern can be evidenced in both
    # directions (she filed some and rescued others), which is a signal NOT to propose it at all.
    seen: dict[str, dict[str, dict]] = {}
    for c in corrections:
        direction = _GATE_ERROR_DIRECTION.get(c.action)
        if direction is None:
            continue
        sender = str((c.meta or {}).get("sender_email") or "").strip().lower()
        if not sender or "@" not in sender:
            continue  # pre-dates sender capture, or a synthesized id — nothing matchable
        subject = str((c.meta or {}).get("email_subject") or "").strip()
        domain = "@" + sender.split("@", 1)[1]
        patterns = [sender] if domain in _PUBLIC_MAIL_DOMAINS else [sender, domain]
        for pattern in patterns:
            if pattern in existing:
                continue  # she already has a rule that covers this
            row = seen.setdefault(pattern, {}).setdefault(direction, {"count": 0, "subjects": []})
            row["count"] += 1
            if subject and subject not in row["subjects"]:
                row["subjects"] = (row["subjects"] + [subject])[:3]

    out: list[dict] = []
    for pattern, directions in seen.items():
        if len(directions) > 1:
            continue  # contradictory evidence — a rule here would be wrong half the time
        direction, row = next(iter(directions.items()))
        if row["count"] < _GATE_PROPOSAL_MIN:
            continue
        out.append({
            "pattern": pattern,
            "direction": direction,
            "count": row["count"],
            "examples": row["subjects"],
            # A domain rule is broader than the address that evidenced it — say so, since accepting
            # it silently catches senders she has never seen.
            "scope": "domain" if pattern.startswith("@") else "sender",
        })
    # Most-evidenced first; ties broken by pattern so the list is stable between reads.
    out.sort(key=lambda r: (-r["count"], r["pattern"]))
    return out


def _is_exemplar(c: Correction) -> bool:
    """True when this correction is a usable few-shot exemplar — the loop was actually fed. Mirrors
    what `few_shot_examples` accepts: an approve/edit/teach carrying the reply she actually sent AND
    the email context it answers. A takeover/skip/misfile (or a row with no final_text) teaches
    nothing — the exact distinction the operator needs to see."""
    meta = c.meta or {}
    return (c.action in ("approve", "edit", "teach")
            and bool((c.final_text or "").strip())
            and bool(meta.get("email_subject") or meta.get("email_body")))


def _corrections_list(corrections: list[Correction], *, limit: int = 200) -> dict:
    """The raw correction log as browsable rows (newest first) — the operator's window on the learning
    signal without shelling into the volume. Per row: what she did, whether it became a real exemplar
    vs a takeover that teaches nothing, and the text (capped). `count`/`exemplars` are over the WHOLE
    log; `corrections` is the most recent `limit` rows."""
    def cap(s: str | None, n: int = 800) -> str:
        s = (s or "").strip()
        return s if len(s) <= n else s[:n] + "…"

    rows = []
    for c in corrections[-limit:]:
        meta = c.meta or {}
        rows.append({
            "email_id": c.email_id,
            "subject": meta.get("email_subject") or "",
            "category": c.category,
            "model_category": meta.get("model_category") or "",
            "relabeled": bool(meta.get("model_category") and meta.get("model_category") != c.category),
            "action": c.action,
            "is_exemplar": _is_exemplar(c),
            "edit_kind": c.edit_kind or "",
            "liked": bool(c.liked),
            "note": c.note or "",
            "final_text": cap(c.final_text),
            "original_draft": cap(c.original_draft),
        })
    rows.reverse()  # newest first
    return {"corrections": rows, "count": len(corrections),
            "exemplars": sum(1 for c in corrections if _is_exemplar(c))}


def _email_from_payload(data: dict) -> Email:
    """Build an Email from a preview payload. Tolerant of camelCase `from` shape and snake_case."""
    frm = data.get("from") or {}
    return Email(
        id=str(data.get("id", "preview")),
        sender_name=data.get("sender_name") or frm.get("name") or "",
        sender_email=data.get("sender_email") or frm.get("email") or "",
        subject=data.get("subject", ""),
        body=data.get("body", ""),
        thread=list(data.get("thread", []) or []),
        # Client-supplied paths are confined to the data root — otherwise this field is an arbitrary
        # local-file read (see _safe_image_paths). A path outside the root raises → 400 at the boundary.
        image_paths=_safe_image_paths(list(data.get("image_paths", []) or [])),
    )


# The inbound webhook carries its shared secret on the URL (Postmark can't send a cookie), so the
# access log would otherwise write BEAN_INBOUND_TOKEN into the log store in plaintext, forever. Strip
# every query string rather than allow-listing key names: the next secret added to a URL then can't
# leak by omission. Nothing Bean logs needs a query value to be debuggable.
_QUERY_RE = re.compile(r"\?\S*")


def _redact_query(line: str) -> str:
    """`'POST /api/inbound?token=abc HTTP/1.1'` → `'POST /api/inbound?<redacted> HTTP/1.1'`."""
    return _QUERY_RE.sub("?<redacted>", line)


def _config_etag(cfg: dict) -> str:
    """A content hash of the config, used as the optimistic-concurrency token. Computed from the
    canonical dict (not the file bytes) so GET and PUT agree regardless of on-disk formatting."""
    return hashlib.sha256(json.dumps(cfg, sort_keys=True).encode()).hexdigest()[:32]


def _notebook_etag(nb: Notebook) -> str:
    """Optimistic-concurrency token for the notebook. Hashed from the CANONICAL render() — provably
    stable across calls (tests pin it) — not the to_dict JSON, whose key order isn't guaranteed. So a
    GET and the PUT that follows agree on the version regardless of how the client reshaped the dict."""
    return hashlib.sha256(nb.render().encode()).hexdigest()[:32]


def _etag_header(etag: str) -> str:
    """RFC 7232 requires an ETag to be a QUOTED string. An unquoted one is malformed, and Cloudflare
    silently drops it — which it did, so the browser never saw a version to send back and every save
    fell through to the 428 path. Quote it."""
    return f'"{etag}"'


def _etag_value(raw: str) -> str:
    """Normalize an inbound ETag/If-Match: strip the weak validator prefix a caching proxy may add
    when it re-encodes the body (`W/"abc"`), then the quotes. Compare only the hash."""
    return raw.strip().removeprefix("W/").strip().strip('"')


class BeanHandler(SimpleHTTPRequestHandler):
    """API + static. Subclasses SimpleHTTPRequestHandler so non-/api paths serve web/ files."""

    def __init__(self, *args, **kwargs):
        super().__init__(*args, directory=str(WEB_DIR), **kwargs)

    # quieter logs (one line per request is enough; full noise is unhelpful overnight)
    def log_message(self, fmt, *args):  # noqa: A003
        log.info("%s - %s", self.address_string(), _redact_query(fmt % args))

    # ---- helpers -----------------------------------------------------------------------------

    def end_headers(self):  # noqa: N802
        # Every response no-cache, so a deploy never strands the operator on stale JS. The API path
        # (_send_json) sets this explicitly; SimpleHTTPRequestHandler serves web/*.jsx with only
        # Last-Modified, which browsers cache HEURISTICALLY — a plain refresh keeps the OLD bundle
        # until a hard refresh (exactly why a shipped feature can look missing). Inject no-store on
        # any response that hasn't already declared it, so no duplicate header where it's explicit.
        if not any(h.lower().startswith(b"cache-control:") for h in self._headers_buffer):
            self.send_header("Cache-Control", "no-store")
        super().end_headers()

    def _send_json(self, status: int, payload, headers: dict | None = None) -> None:
        body = json.dumps(payload).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        # Never cache an API response. Every one of them is either the operator's mail or a fact about
        # this exact instant: a cached /healthz would report the SHA of the container it replaced,
        # which is the one lie the deploy script must never be told.
        self.send_header("Cache-Control", "no-store")
        for k, v in (headers or {}).items():
            self.send_header(k, v)
        self.end_headers()
        self.wfile.write(body)

    def _read_json(self, max_bytes: int | None = None) -> dict:
        limit = _MAX_BODY if max_bytes is None else max_bytes  # module global read live, so tests cap it
        length = int(self.headers.get("Content-Length") or 0)
        if length > limit:
            # Refuse from the header, before reading a byte: an unbounded rfile.read(length) is a
            # cheap pre-auth OOM (Content-Length: 2GB). log.warning so a wrongly-rejected payload
            # leaves a trace rather than vanishing.
            log.warning("413 %s: Content-Length %d exceeds %d-byte cap",
                        self.path.split("?", 1)[0], length, limit)
            # Don't keep-alive: we refuse without draining the (oversized) body, so the socket still
            # holds unread bytes that must not be parsed as the next request. Close after responding.
            self.close_connection = True
            self._send_json(413, {"error": "request body too large"})
            raise _PayloadTooLarge
        raw = self.rfile.read(length) if length else b""
        return json.loads(raw.decode() or "{}")

    def _load_config_or_503(self):
        """The persisted Config, or None after this method has already sent a 503.

        `load_config` now RAISES `ConfigCorruptError` when the file exists, cannot be parsed, and has
        no usable `.bak`. Turn that into a loud 503 rather than a 500 traceback — and NEVER catch it
        into a fixtures fallback. Serving demo templates as the customer's config, then overwriting
        her real ones on the next save, is precisely the silent-success failure this work exists to
        kill. A 503 on the inbound webhook is also correct: Postmark retries, so mail waits for a
        human instead of being triaged against a stranger's templates.
        """
        try:
            return load_config(CONFIG_PATH)
        except ConfigCorruptError as exc:
            log.error("refusing to serve a request: %s", exc)
            self._send_json(503, {"error": "config unreadable — refusing to serve demo data as yours"})
            return None

    def _load_notebook_or_503(self):
        """The persisted Notebook, or None after this method has already sent a 503. No notebook on
        disk means she hasn't been distilled/approved yet — a real 503, never a fabricated default
        (a demo brain saved over hers is the fixtures-leak bug the whole rebuild exists to avoid)."""
        try:
            return load_notebook(NOTEBOOK_PATH)
        except FileNotFoundError:
            self._send_json(503, {"error": "no notebook yet — distill and approve it first"})
            return None

    def _load_corrections_or_503(self):
        """The operator's correction log, or None after this method has already sent a 503.

        Loaded HERE and passed explicitly into the engine rather than left to a callee's default.
        The default did work — but it made the moat an implicit argument: nothing in the request
        path mentioned corrections, no test covered "does the server load them", and a caller that
        passed `corrections=[]` would have silently unlearned two days of teaching with 182 tests
        still green. An explicit argument can be asserted on.

        Absent log ⇒ `[]` (a new customer, drafting without exemplars — fine). Unreadable log ⇒ 503,
        the same refusal `_load_config_or_503` makes, and for the same reason: a Bean that drafts
        confidently while unable to read what it was taught is indistinguishable from one that was
        never taught.
        """
        try:
            return load(CORRECTIONS_PATH)
        except CorrectionsCorruptError as exc:
            log.error("refusing to draft untaught: %s", exc)
            self._send_json(503, {"error": "correction log unreadable — refusing to draft as an untaught Bean"})
            return None

    # ---- passcode gate -----------------------------------------------------------------------
    # A single shared secret (BEAN_PASSCODE) checked per request — no auth service, no session DB.
    # Read live from the env (not cached) so tests can toggle it; empty secret = gate disabled, so
    # local dev and the existing suite behave exactly as before. All compares are constant-time.

    def _authed(self) -> bool:
        secret = os.environ.get("BEAN_PASSCODE", "")
        if not secret:
            return True  # gate disabled
        cookie = SimpleCookie(self.headers.get("Cookie", ""))
        got = cookie["bean_auth"].value if "bean_auth" in cookie else ""
        # Accept the session token we now issue, AND the raw passcode we used to issue. Both compares
        # run (no short-circuit) so this stays constant-time. The legacy arm exists only so this
        # change does not sign the operator out mid-inbox; it can be deleted once their cookie has
        # rotated, which happens the next time they enter the passcode.
        new = hmac.compare_digest(got, _session_token(secret))
        legacy = hmac.compare_digest(got, secret)
        return new or legacy

    def _send_passcode_page(self, status: int = 401) -> None:
        """The passcode page. 401 by default; 200 only at the app's entry paths (see _gate).

        It used to be 200 for EVERY unauthenticated non-API path, including paths that do not exist —
        so the app claimed success for a login screen, and any monitor checking a status code read
        "healthy". That is why `curl /healthz` returned 200 against production before /healthz existed.

        No WWW-Authenticate header on purpose: it would make the browser pop its native basic-auth
        dialog instead of rendering this page. A 401 body renders normally without it.
        """
        body = _PASSCODE_PAGE.encode()
        self.send_response(status)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")  # a cached login page at the app's URL
        self.end_headers()
        self.wfile.write(body)

    def _gate(self) -> bool:
        """Enforce the passcode gate. Returns True if the request may proceed; if it returns False
        it has already written the response (401 JSON for /api/*, the passcode page for a page GET)."""
        if not os.environ.get("BEAN_PASSCODE", "") and _production():
            # Fail CLOSED: an unset passcode in prod means the gate silently opened /api/inbox (every
            # customer's mail) and /api/learning to the internet. Refuse loudly — a 503 the operator
            # can page on — rather than serve it unauthenticated. Checked before _authed(), which
            # would otherwise fail-open on the empty secret.
            log.error("BEAN_PASSCODE unset while BEAN_ENV=production — refusing gated request (503)")
            self._send_json(503, {"error": "server misconfigured: passcode unset"})
            return False
        if self._authed():
            return True
        # Public branding assets carry no data and must be reachable by unauthed link-preview
        # crawlers (iMessage/Slack fetch og:image before any login) and the browser tab.
        if self.path.split("?", 1)[0] in _PUBLIC_ASSETS:
            return True
        path = self.path.split("?", 1)[0]
        if path.startswith("/api/"):
            self._send_json(401, {"error": "unauthorized"})
        elif path in _ENTRY_PATHS:
            # The app's front door IS the login page when you are not authed, and a login page is a
            # real page: 200. This is what /login returns in every framework. It also keeps the
            # og: tags reachable — iMessage and Slack only render a link preview from a 2xx, and
            # _BRAND_HEAD exists so a shared link to this instance previews as Bean.
            self._send_passcode_page(200)
        else:
            # Any other path, including ones that do not exist. Refuse, and say so in the status.
            self._send_passcode_page(401)
        return False

    def _auth_locked(self, ip: str) -> bool:
        """Brute-force guard. One shared passcode IS the whole system's auth, so an IP that fails
        _AUTH_MAX_FAILURES times within _AUTH_WINDOW is locked out for _AUTH_LOCKOUT seconds.
        Checked before the body is read, so lockout can't be dodged."""
        with _RATE_LOCK:
            return _auth_lockouts.get(ip, 0.0) > time.time()

    def _record_auth_failure(self, ip: str) -> None:
        now = time.time()
        with _RATE_LOCK:
            fails = [t for t in _auth_failures.get(ip, []) if now - t < _AUTH_WINDOW]
            fails.append(now)
            _auth_failures[ip] = fails
            if len(fails) >= _AUTH_MAX_FAILURES:
                _auth_lockouts[ip] = now + _AUTH_LOCKOUT
                log.warning("auth lockout for %s: %d failures within %ds", ip, len(fails), _AUTH_WINDOW)

    def _record_auth_success(self, ip: str) -> None:
        with _RATE_LOCK:
            _auth_failures.pop(ip, None)
            _auth_lockouts.pop(ip, None)

    def _handle_auth(self) -> None:
        ip = self.client_address[0]
        if self._auth_locked(ip):
            return self._send_json(429, {"ok": False, "error": "too many attempts — wait a few minutes"})
        secret = os.environ.get("BEAN_PASSCODE", "")
        try:
            data = self._read_json()
        except json.JSONDecodeError:
            data = {}
        supplied = str(data.get("passcode", "")) if isinstance(data, dict) else ""
        if secret and hmac.compare_digest(supplied, secret):
            self._record_auth_success(ip)
            body = json.dumps({"ok": True}).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.send_header(
                "Set-Cookie",
                f"bean_auth={_session_token(secret)}; HttpOnly; Path=/; SameSite=Lax; Max-Age=2592000; Secure",
            )
            self.end_headers()
            self.wfile.write(body)
            return
        self._record_auth_failure(ip)
        return self._send_json(401, {"ok": False})

    # ---- routing -----------------------------------------------------------------------------

    def do_GET(self):  # noqa: N802
        # Before the gate on purpose: a healthcheck that needs the passcode cannot verify a deploy,
        # and this is the one surface an operator (or a deploy script) may touch without a secret.
        # It exposes counts, never content.
        if self.path.split("?", 1)[0] == "/healthz":
            ok, body = _health()
            return self._send_json(200 if ok else 503, body)
        # The public beta landing page — reachable without a passcode (it is how people ASK for one).
        if self.path.split("?", 1)[0] == "/join":
            body = _JOIN_PAGE.encode()
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            return self.wfile.write(body)
        if not self._gate():
            return
        path = self.path.split("?", 1)[0]
        if path == "/api/config":
            loaded = self._load_config_or_503()
            if loaded is None:
                return
            cfg = loaded.to_dict()
            return self._send_json(200, cfg, headers={"ETag": _etag_header(_config_etag(cfg))})
        if path == "/api/notebook":
            nb = self._load_notebook_or_503()
            if nb is None:
                return
            return self._send_json(200, nb.to_dict(), headers={"ETag": _etag_header(_notebook_etag(nb))})
        if path == "/api/meta":
            # The inbound forwarding address the onboarding shows the operator (empty until Bean is wired
            # to a real inbound domain — the UI then falls back to a friendly placeholder).
            return self._send_json(200, {"inbound_address": os.environ.get("BEAN_INBOUND_ADDRESS", "")})
        if path == "/api/inbox":
            return self._send_json(200, {"emails": load_inbox(inbox_path(CUSTOMER))})
        if path == "/api/status":
            return self._send_json(200, _read_status(STATUS_PATH))
        if path == "/api/notebook/review":
            # Her answers-so-far in the onboarding walk — so a 45-card review survives being closed
            # and reopened. {} = nothing saved yet (or she finished and it was cleared). Behind the
            # gate: it carries her reworded cliffs. See _read_notebook_review for the degrade stance.
            return self._send_json(200, _read_notebook_review(NOTEBOOK_REVIEW_PATH))
        if path == "/api/learning":
            corrections = self._load_corrections_or_503()
            if corrections is None:
                return  # 503 already sent — an unreadable log must not render as "you taught nothing"
            return self._send_json(200, _learning_summary(corrections))
        if path == "/api/gate-proposals":
            # The gate's own read surface: rules Bean derives from the mis-files she already logged.
            # Needs the config too, so a pattern she has already added stops being proposed.
            corrections = self._load_corrections_or_503()
            if corrections is None:
                return  # 503 already sent — an unreadable log must not render as "nothing to fix"
            config = self._load_config_or_503()
            if config is None:
                return  # 503 already sent
            return self._send_json(200, {"proposals": _gate_proposals(corrections, config)})
        if path == "/api/corrections":
            # The raw learning log, browsable — the operator's window on what Bean has (and hasn't)
            # learned. Behind the same passcode gate as /api/learning: it carries her mail + replies.
            corrections = self._load_corrections_or_503()
            if corrections is None:
                return  # 503 already sent
            return self._send_json(200, _corrections_list(corrections))
        if path == "/api/reply":
            # One past reply, by email id — the resolver behind a tappable `corpus:<email_id>`
            # citation chip. The shelf ranks and retrieves but has no by-id lookup, and the chip
            # needs exactly one row: the NEWEST correction for that id that actually carries a
            # reply (a later skip/takeover row must not blank out the reply she really sent).
            # Read-only, behind the same gate as /api/corrections — it carries her mail.
            qs = parse_qs(urlsplit(self.path).query)
            email_id = (qs.get("id") or [""])[0].strip()
            if not email_id:
                return self._send_json(400, {"error": "id is required"})
            corrections = self._load_corrections_or_503()
            if corrections is None:
                return  # 503 already sent
            hit = None
            for c in corrections:  # oldest first → the last match is the newest
                if c.email_id == email_id and (c.final_text or "").strip():
                    hit = c
            if hit is None:
                return self._send_json(404, {"error": "no reply on file for that email"})
            meta = hit.meta or {}
            return self._send_json(200, {
                "email_id": hit.email_id,
                "subject": meta.get("email_subject") or "",
                "body": meta.get("email_body") or "",
                "reply": hit.final_text or "",
            })
        if path in ("/", ""):
            self.path = "/Bean.html"  # the app lives at Bean.html; don't list the directory
            return super().do_GET()
        # window.CONFIG/EMAILS. Locally this file is generated on disk (fixture demo) and served
        # statically; in a deploy it's gitignored/absent, so synthesize it from the live config with
        # an empty inbox — the app then fills mail from /api/inbox. Without this the app crashes on
        # mount (undefined window.CONFIG) and renders blank.
        #
        # DEMO_TENANT synthesizes UNCONDITIONALLY, ignoring any file on disk. That file is generated
        # on somebody's laptop from whatever tenant that laptop was pointed at — serving it from the
        # public demo is [[fixtures-leaked-into-prod]] pointing outward — and it is also the reason
        # the demo's own boot flags below would otherwise be missing on a local demo run but present
        # in the deploy, which is the worst way for a flag to behave.
        if path == "/bean-data.jsx" and (DEMO_TENANT or not (WEB_DIR / "bean-data.jsx").exists()):
            return self._serve_data_jsx()
        return super().do_GET()  # static web/ file

    def _serve_data_jsx(self) -> None:
        loaded = self._load_config_or_503()
        if loaded is None:
            return
        config = loaded.to_dict()
        body = (
            "// Synthesized by the server (no baked fixture file present) — live config, empty\n"
            "// inbox; real mail arrives via /api/inbox.\n"
            f"window.EMAILS = [];\nwindow.CONFIG = {json.dumps(config)};\n"
            "window.SETTINGS = window.CONFIG.settings;\n"
            # The one boot flag the client cannot discover asynchronously. It rides this script
            # because Bean.html loads it BEFORE bean-root.jsx, so the very first render already
            # knows it is the demo — anything fetched (/api/meta, /api/config) resolves a frame too
            # late and the overlay it suppresses would flash before disappearing.
            f"window.BEAN_DEMO_TENANT = {json.dumps(DEMO_TENANT)};\n"
        ).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/javascript; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_PUT(self):  # noqa: N802
        # One top-level catch: _read_json raises _PayloadTooLarge after sending its own 413, so every
        # PUT body-read unwinds cleanly — no half-read socket, no ugly handler-thread traceback.
        try:
            self._route_put()
        except _PayloadTooLarge:
            return

    def _demo_locked(self, path: str) -> bool:
        """True when this deployment is a read-only demo and `path` is one of the write routes it
        hides — having already written the 404, so the caller just returns.

        Checked BEFORE the passcode gate on purpose. "This route does not exist on this deployment"
        is a fact about the deployment, not about who is asking, and answering 401 first would make
        the demo's shape depend on whether it happens to be passcode-gated."""
        if not DEMO_READONLY or path not in _DEMO_LOCKED_ROUTES:
            return False
        self._send_json(404, {"error": "not found"})
        return True

    def _route_put(self) -> None:
        path = self.path.split("?", 1)[0]
        if self._demo_locked(path):
            return
        if not self._gate():
            return
        if path == "/api/notebook":
            return self._put_notebook()
        if path == "/api/notebook/review":
            return self._put_notebook_review()
        if path != "/api/config":
            return self._send_json(404, {"error": "not found"})
        # Optimistic concurrency. A PUT replaces the WHOLE config, so two people with the editor
        # open (the operator in a tab, an import script, a second device) silently overwrite each
        # other and the loser gets a 200. Require the caller to name the version it edited: If-Match
        # must carry the ETag from the GET it loaded. Stale → 409 with the current config, so the
        # client can say "reload" instead of wiping someone's work with a cheerful success.
        # The whole read→compare→validate→write runs under _STATE_LOCK: without it two threads can
        # both read the same ETag, both pass the If-Match check, and both write — a TOCTOU where the
        # last writer silently wins and both get 200 (a double-clicked Save is enough).
        with _STATE_LOCK:
            loaded = self._load_config_or_503()
            if loaded is None:
                return
            current = loaded.to_dict()
            current_etag = _config_etag(current)
            if_match = _etag_value(self.headers.get("If-Match", ""))
            if not if_match:
                return self._send_json(428, {"error": "If-Match required", "etag": current_etag})
            if if_match != current_etag:
                return self._send_json(409, {
                    "error": "stale config — someone else saved since you loaded it",
                    "etag": current_etag, "config": current,
                })
            try:
                data = self._read_json()
                config = Config.from_dict(data)  # validate before writing
            except (json.JSONDecodeError, KeyError, TypeError, ValueError) as exc:
                return self._send_json(400, {"error": f"invalid config: {exc}"})
            saved = config.to_dict()
            # Atomic + rolling .bak: write_text truncates the target BEFORE writing, so a redeploy
            # landing in that window left an empty config — and load_config used to answer that with
            # demo fixtures, erasing her real templates on the next save. This is her only history.
            write_json_atomic(CONFIG_PATH, saved)
            # Hand back the ETag of what was actually PERSISTED, not of what was sent: to_dict()
            # normalizes (sorted docs, dropped legacy keys), so the client's next save would look
            # stale against its own write if it kept the ETag it PUT with.
            return self._send_json(200, saved, headers={"ETag": _etag_header(_config_etag(saved))})


    def _put_notebook(self) -> None:
        """Replace the whole notebook — her editable brain — under the same optimistic-concurrency
        guard as the config PUT. If-Match must carry the ETag from the GET; stale → 409 with the
        current notebook so the client reloads instead of clobbering a concurrent edit. All under
        _STATE_LOCK: a concurrent inbound draft reading the notebook can't interleave with the
        rewrite, and two saves can't both pass the If-Match check (the config PUT's TOCTOU reasoning).
        The save itself is atomic + rolling .bak inside Notebook.save — it's her PII, one history."""
        with _STATE_LOCK:
            current = self._load_notebook_or_503()
            if current is None:
                return
            current_etag = _notebook_etag(current)
            if_match = _etag_value(self.headers.get("If-Match", ""))
            if not if_match:
                return self._send_json(428, {"error": "If-Match required", "etag": current_etag})
            if if_match != current_etag:
                return self._send_json(409, {
                    "error": "stale notebook — someone else saved since you loaded it",
                    "etag": current_etag, "notebook": current.to_dict(),
                })
            try:
                data = self._read_json()
                nb = Notebook.from_dict(data)  # clamps stakes/provenance, drops blank rows — the boundary
            except (json.JSONDecodeError, KeyError, TypeError, ValueError) as exc:
                return self._send_json(400, {"error": f"invalid notebook: {exc}"})
            nb.save(NOTEBOOK_PATH)
            # Approving the notebook is the end of the onboarding walk — the resume file has done its
            # job, so drop it. Reopening the walk later then starts clean, not mid-old-session. Best-
            # effort under the same lock: a leftover file would only cost her a stale-looking resume,
            # never her approved notebook (already saved above).
            NOTEBOOK_REVIEW_PATH.unlink(missing_ok=True)
            return self._send_json(200, nb.to_dict(), headers={"ETag": _etag_header(_notebook_etag(nb))})

    def _put_notebook_review(self) -> None:
        """Upsert her in-progress questionnaire answers — the resume snapshot, NOT the notebook. The
        client sends the WHOLE decisions object on each card tap (a resume snapshot, not an event
        log), so this replaces rather than merges. No ETag: it's single-operator convenience state,
        not a concurrent-edit surface — the same stance status.json takes. Runs under _STATE_LOCK so
        it can't interleave with the approval that deletes it."""
        try:
            data = self._read_json()
        except json.JSONDecodeError as exc:
            return self._send_json(400, {"error": f"invalid JSON: {exc}"})
        if not isinstance(data, dict):
            return self._send_json(400, {"error": "review progress must be a JSON object"})
        with _STATE_LOCK:
            write_json_atomic(NOTEBOOK_REVIEW_PATH, data, backup=False)
        return self._send_json(200, {"ok": True})

    def _handle_clear_filed(self) -> None:
        """Delete the FYI (gate-filed) mail from the inbox on the operator's one-tap 'Clear FYI'. Only
        filed newsletters/promos/notifications go — a customer email that walked the tree is never
        touched (bean.inbox.clear_filed is double-guarded), and the full log is backed up on the
        volume first. Under _STATE_LOCK like the other inbox/status writers so a concurrent inbound
        append can't interleave with the rewrite."""
        with _STATE_LOCK:
            removed, kept = clear_filed(inbox_path(CUSTOMER))
        log.info("clear-filed: removed %d filed items, %d kept", removed, kept)
        return self._send_json(200, {"ok": True, "removed": removed, "kept": kept})

    def _handle_post_status(self) -> None:
        """Upsert ONE email's action state (`{"id": ..., "state": ...}`), the fix for PUT's
        lost-update bug: read the current map, set one key, write it back — so a stale second device
        can't revert an action it never saw (phone approves A; laptop, whose in-memory map lacks A,
        must not blow A away). An empty `state` deletes the key. Read-modify-write, so it runs under
        _STATE_LOCK exactly like the config PUT."""
        try:
            data = self._read_json()
        except json.JSONDecodeError as exc:
            return self._send_json(400, {"error": f"invalid JSON: {exc}"})
        eid = data.get("id") if isinstance(data, dict) else None
        state = data.get("state", "") if isinstance(data, dict) else None
        if not isinstance(eid, str) or not eid or not isinstance(state, str):
            return self._send_json(400, {"error": "status upsert requires a non-empty string 'id' and a string 'state'"})
        with _STATE_LOCK:
            current = _read_status(STATUS_PATH)
            if state:
                current[eid] = state
            else:
                current.pop(eid, None)  # empty state = undo/clear this email's action
            write_json_atomic(STATUS_PATH, current, backup=False)
        return self._send_json(200, current)

    def do_POST(self):  # noqa: N802
        # One top-level catch, mirroring do_PUT: _read_json sends its own 413 then raises, so a POST
        # body over the cap unwinds without a half-read socket or an ugly handler-thread traceback.
        try:
            self._route_post()
        except _PayloadTooLarge:
            return
        # Any model-spending POST (preview gate+triage, inbound, sample-email) can hit an empty
        # credit balance. Catch it HERE, once, so every path answers with a clean 402 the UI can
        # render ("top up Bean's monies") instead of the connection dropping into a misleading
        # "is the server running?". 402 is non-2xx, so Postmark also retries inbound until topped up.
        except OutOfCreditsError as exc:
            log.error("out of Anthropic credits — refusing model work: %s", exc)
            return self._send_json(402, {
                "error_code": "out_of_credits",
                "error": "Bean's out of thinking-juice — top up the API monies to keep going.",
            })

    def _route_post(self) -> None:
        path = self.path.split("?", 1)[0]
        if self._demo_locked(path):
            return  # a read-only demo hides every write route, /api/inbound included
        if path == "/api/auth":  # the way to become authed — always reachable, skips the gate
            return self._handle_auth()
        if path == "/api/inbound":  # Postmark webhook — can't carry the passcode cookie; own token
            return self._handle_inbound()
        if path == "/api/waitlist":  # public beta signup — no passcode, so it's rate-limited + capped
            return self._handle_waitlist()
        if not self._gate():
            return
        if path == "/api/preview":
            return self._handle_preview()
        if path == "/api/status":
            return self._handle_post_status()
        if path == "/api/correction":
            return self._handle_correction()
        if path == "/api/clear-filed":
            return self._handle_clear_filed()
        return self._send_json(404, {"error": "not found"})

    def _preview_over_rate(self) -> bool:
        """A sliding-window ceiling on the app's most expensive endpoint (see _PREVIEW_MAX_PER_MIN).
        Mirrors _inbound_over_rate; checked before the body is read, so a 429 costs zero tokens."""
        now = time.time()
        with _RATE_LOCK:
            _preview_hits[:] = [t for t in _preview_hits if now - t < 60]
            _preview_hits.append(now)
            return len(_preview_hits) > _PREVIEW_MAX_PER_MIN

    def _handle_preview(self) -> None:
        if self._preview_over_rate():
            # Refused before the body is parsed and long before a model is reached. A 429 that has
            # already paid for the tokens is not a limit.
            log.warning("preview over %d/min — 429", _PREVIEW_MAX_PER_MIN)
            return self._send_json(429, {"error": "rate limited — too many previews"})
        try:
            data = self._read_json()
        except json.JSONDecodeError as exc:
            return self._send_json(400, {"error": f"invalid JSON: {exc}"})
        if not isinstance(data, dict) or "email" not in data:
            return self._send_json(400, {"error": "missing 'email' in request body"})
        # Resolve the persisted config BEFORE the payload try/except: ConfigCorruptError is a
        # RuntimeError, but a future widening of that net would swallow it back into a 400 and hide
        # a shredded config behind "invalid payload".
        persisted = None
        if not data.get("config"):
            persisted = self._load_config_or_503()
            if persisted is None:
                return
        try:
            email = _email_from_payload(data["email"])
            # No override → triage against the persisted customer config (what the admin shows and
            # what a pasted email should be judged by), falling back to fixtures when absent.
            config = Config.from_dict(data["config"]) if data.get("config") else persisted
        # AttributeError: a non-dict `email`/`config` (e.g. "email": "oops") makes .get() blow up
        # inside the parsers — that's a malformed body (400), not a server fault (500).
        except (KeyError, TypeError, ValueError, AttributeError) as exc:
            return self._send_json(400, {"error": f"invalid payload: {exc}"})
        # The triage gate runs BEFORE the tree: mail that needs no reply is filed with $0 of
        # drafting. `skipGate` is the "needs a reply" recovery — the UI re-previews a filed
        # email past the gate (and logs the mis-file correction separately). Module-level
        # `gate` alias so offline tests monkeypatch it like the triage seams.
        if not data.get("skipGate"):
            try:
                g = gate(email, rules=config.gate)
            except OutOfCreditsError:
                raise  # do_POST answers 402 "top up", not the generic 502 below
            except Exception as exc:  # noqa: BLE001 - a model/network failure must not 500 the preview
                log.warning("preview gate failed: %s", exc)
                return self._send_json(502, {"error": "Bean couldn't reach its brain just now — try again in a moment."})
            if g.disposition == "file":
                return self._send_json(200, {
                    "disposition": "filed", "email_id": email.id,
                    "kind": g.kind, "reason": g.reason,
                })
        # Loaded after the gate: filed mail is never drafted, so it must not 503 on a log it would
        # never have read. Loaded before the model: a request that cannot see her corrections must
        # cost zero tokens, not produce an untaught draft.
        corrections = self._load_corrections_or_503()
        if corrections is None:
            return
        # The cutover: preview runs the routing-tree loop (chunk → route → resolve → assemble →
        # reduce), so the result carries the inspectable per-chunk breakdown the UI renders. The
        # flat `triage` stays imported as the offline test seam / fallback path.
        try:
            result, result_d = _run_engine(email, config, corrections)
        except OutOfCreditsError:
            raise  # do_POST answers 402 "top up", not the generic 502 below
        except OSError as exc:
            # A client image path that passed the data-root check but doesn't exist reaches
            # Path(...).read_bytes() here (bean/llm.py) and raises FileNotFoundError — a bad request
            # (400), not a server error (500).
            return self._send_json(400, {"error": f"unreadable image: {exc}"})
        except Exception as exc:  # noqa: BLE001 - a model/network failure must not 500 the preview
            log.warning("preview triage failed: %s", exc)
            return self._send_json(502, {"error": "Bean couldn't reach its brain just now — try again in a moment."})
        return self._send_json(200, {"disposition": "reply", **result_d})

    def _waitlist_over_rate(self) -> bool:
        """Sliding-window ceiling on the public signup endpoint (see _WAITLIST_MAX_PER_MIN). Checked
        before the body is read, so a flood costs one dict append, not a volume write."""
        now = time.time()
        with _RATE_LOCK:
            _waitlist_hits[:] = [t for t in _waitlist_hits if now - t < 60]
            _waitlist_hits.append(now)
            return len(_waitlist_hits) > _WAITLIST_MAX_PER_MIN

    def _handle_waitlist(self) -> None:
        """Record one beta signup. PUBLIC (no passcode) and it writes to the volume, so it is
        rate-limited, length-capped, deduped, and hard-capped — see the _WAITLIST_* note."""
        if self._waitlist_over_rate():
            log.warning("waitlist over %d/min — 429", _WAITLIST_MAX_PER_MIN)
            return self._send_json(429, {"ok": False, "error": "slow down a moment, then try again"})
        try:
            data = self._read_json()
        except json.JSONDecodeError:
            return self._send_json(400, {"ok": False, "error": "bad request"})
        email = (data.get("email") or "").strip() if isinstance(data, dict) else ""
        if not _EMAIL_RE.match(email) or len(email) > 254:
            return self._send_json(400, {"ok": False, "error": "that doesn't look like an email"})

        path = waitlist_path()
        # Best-effort dedup + count. A waitlist that can't read its own file must NOT drop a signup,
        # so a read failure falls through to the append: a duplicate is harmless, a lost prospect is
        # not. The rate limit — not this count — is the real disk-fill defense.
        existing: set[str] = set()
        try:
            if path.exists():
                for line in path.read_text(encoding="utf-8").splitlines():
                    if line.strip():
                        existing.add((json.loads(line).get("email") or "").lower())
        except (OSError, json.JSONDecodeError) as exc:
            log.warning("waitlist read failed (%s) — appending without dedup", exc)

        if email.lower() in existing:
            return self._send_json(200, {"ok": True})  # already on it; don't reveal, don't double-write
        if len(existing) >= _WAITLIST_MAX:
            # An HONEST refusal, not a faked success. A real person past the cap is told to email,
            # never told "you're on the list" when they are not.
            log.warning("waitlist at cap %d — refusing signup", _WAITLIST_MAX)
            return self._send_json(503, {"ok": False, "error": "list is full for now — email hello@ instead"})

        row = {"email": email, "ts": datetime.now(timezone.utc).isoformat()}
        if note := str((data.get("note") or ""))[:280].strip():
            row["note"] = note  # optional "what would you use Bean for" — captured if the form sends it
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            # Append-only, one JSON line — same shape as every other log on the volume. A whole-file
            # rewrite here would be a corruption risk on every signup; append never rewrites.
            with path.open("a", encoding="utf-8") as fh:
                fh.write(json.dumps(row, sort_keys=True) + "\n")
        except OSError as exc:
            # The write genuinely failed — say so. Never a 200 for a signup that did not land.
            log.error("waitlist append failed: %s", exc)
            return self._send_json(503, {"ok": False, "error": "couldn't save that — try again in a bit"})
        log.info("waitlist signup recorded")
        return self._send_json(200, {"ok": True})

    def _handle_correction(self) -> None:
        """Record one human action (approve | edit | takeover | skip) to the correction log —
        the learning signal, captured from email #1. The log is the moat; this is its only write
        path. `record()` auto-tags whether a text edit was cosmetic vs substantive.

        `meta` carries the orthogonal classification signal + the few-shot context: `model_category`
        (the bucket the model picked — a relabel is any row where it differs from `category`),
        `email_subject`, and `email_body`. An `approve` carries `final_text` = the model draft the
        operator accepted, so the (email -> good reply) pair can train future drafts."""
        try:
            data = self._read_json()
        except json.JSONDecodeError as exc:
            return self._send_json(400, {"error": f"invalid JSON: {exc}"})
        if not isinstance(data, dict) or not data.get("email_id") or not data.get("action"):
            return self._send_json(400, {"error": "correction requires at least 'email_id' and 'action'"})
        try:
            # Under _STATE_LOCK so this append can't be clobbered by the /api/backfill-leaf-path
            # rewrite (read → os.replace of corrections.jsonl), which holds the same lock — mirrors how
            # the inbound/preview paths already hold it around append_inbox. The record itself is a lone
            # append (append-only, one writer), so the lock is contended only during the rare backfill.
            with _STATE_LOCK:
                recorded = record(Correction(
                    email_id=str(data["email_id"]),
                    category=str(data.get("category", "")),
                    confidence=str(data.get("confidence", "")),
                    action=str(data["action"]),
                    original_draft=data.get("original_draft"),
                    final_text=data.get("final_text"),
                    note=str(data.get("note", "")),
                    liked=bool(data.get("liked", False)),
                    meta=dict(data.get("meta") or {}),
                ), log_path=CORRECTIONS_PATH)
        except (KeyError, TypeError, ValueError) as exc:
            return self._send_json(400, {"error": f"invalid correction: {exc}"})
        return self._send_json(200, {"ok": True, "edit_kind": recorded.edit_kind})

    # ---- inbound mail webhook (Postmark) -----------------------------------------------------
    # Reachable by Postmark, which cannot present the passcode cookie — so it carries its own
    # shared secret (BEAN_INBOUND_TOKEN) on the URL instead. Read live from env; unset = disabled
    # (local/test default), same fail-open convention as the passcode gate.

    def _inbound_authed(self) -> bool:
        secret = os.environ.get("BEAN_INBOUND_TOKEN", "")
        if not secret:
            return True  # token check disabled (local/offline)
        supplied = parse_qs(urlsplit(self.path).query).get("token", [""])[0]
        return hmac.compare_digest(supplied, secret)

    def _inbound_over_rate(self) -> bool:
        """A per-minute ceiling on the inbound webhook (global — one tenant). True once more than
        _INBOUND_MAX_PER_MIN have landed in the last 60s; a sliding window over a shared list."""
        now = time.time()
        with _RATE_LOCK:
            _inbound_hits[:] = [t for t in _inbound_hits if now - t < 60]
            _inbound_hits.append(now)
            return len(_inbound_hits) > _INBOUND_MAX_PER_MIN

    def _handle_inbound(self) -> None:
        """Ingest one forwarded email (Postmark inbound-parse payload). Store notification mail
        feeds the order index; a customer message is triaged once and appended to the live inbox;
        everything else is filed to the inbox (demoted, never dropped). Always 200 on a well-formed
        webhook so Postmark doesn't retry a filed email as a failure."""
        if not os.environ.get("BEAN_INBOUND_TOKEN", "") and _production():
            # Fail CLOSED, mirroring the passcode gate: an unset token in prod means anyone can drive
            # unbounded Anthropic spend through this webhook. Refuse loudly rather than run for free.
            log.error("BEAN_INBOUND_TOKEN unset while BEAN_ENV=production — refusing inbound (503)")
            return self._send_json(503, {"error": "server misconfigured: inbound token unset"})
        if not self._inbound_authed():
            return self._send_json(401, {"error": "unauthorized"})
        if self._inbound_over_rate():
            # A spend guard: even a valid token shouldn't drive unbounded model calls (each inbound
            # email = a gate classify + a drafting call). Postmark retries on
            # any non-2xx, so a 429 DELAYS the mail rather than dropping it.
            log.warning("inbound over %d/min — 429; Postmark will retry", _INBOUND_MAX_PER_MIN)
            return self._send_json(429, {"error": "rate limited"})
        try:
            payload = self._read_json(_MAX_INBOUND_BODY)
        except json.JSONDecodeError as exc:
            return self._send_json(400, {"error": f"invalid JSON: {exc}"})
        if not isinstance(payload, dict):
            return self._send_json(400, {"error": "expected a Postmark payload object"})

        config = self._load_config_or_503()
        if config is None:
            return  # 503 already sent; Postmark retries, so the email is delayed, never dropped
        email = email_from_postmark(payload)
        # The live real-mail path: a model call here (gate/triage) can hit an Anthropic overload
        # (529), a connection error, or a forced-tool-decline ValueError. Uncaught, it would kill the
        # webhook thread with no HTTP response — Bean's own doctrine (never 500; refuse loudly so
        # Postmark retries) is followed one paragraph above by _load_config_or_503 and must hold here.
        try:
            g = gate(email, rules=config.gate)
        except OutOfCreditsError:
            raise  # do_POST → 402; non-2xx, so Postmark retries once topped up
        except Exception as exc:  # noqa: BLE001 - delay the mail (Postmark retries), never crash the webhook
            log.warning("inbound gate failed for %s: %s — 503, Postmark will retry", email.id, exc)
            return self._send_json(503, {"error": "temporarily unavailable — retry"})

        # Store notification mail (order/shipping confirmations): parse into the order index so it
        # grounds later WISMO. Filed, never shown to the operator.
        if g.kind in ("receipt", "notification"):
            # The tenant's config carries the order-number prefixes and its own mail domains —
            # without it the parser falls back to the fictional fixture defaults and a real store's
            # notification mail parses as no order at all.
            rec = parse_notification(email, config=config)
            if rec is not None:
                record_order(rec, log_path=order_index_path(CUSTOMER))
                _stamp_inbound()
                return self._send_json(200, {"ok": True, "disposition": "filed", "kind": g.kind})
            # The gate called it a notification but there is no order in it. That is not a store
            # receipt — it is a forwarding/domain-auth confirmation, a DNS check, or a customer the
            # gate misread. Returning here dropped it entirely (the operator's own
            # forwarding-verification mail vanished this way), so fall through to the filed-inbox
            # append below: unparseable is precisely where the gate is least trustworthy, and one
            # tap must be able to recover.
            log.warning("inbound %s gated as %r but parses as no order — filing to inbox", email.id, g.kind)

        # Any other non-reply mail (newsletter/promo/spam/…): filed but NOT dropped. A live gate
        # false-negative (a real customer wrongly filed) must never silently vanish, so persist it to
        # the same inbox log with the gate's verdict in `result` — the exact {disposition,kind,reason}
        # shape the UI reads (bean-inbox.jsx applyResult). It lands in the collapsed Filed/FYI lane
        # (demoted, never hidden), and one-tap "needs a reply" recovery re-routes it + logs a mis-file.
        if g.disposition == "file":
            item = InboxItem(
                id=email.id, sender_name=email.sender_name, sender_email=email.sender_email,
                reply_to=email.sender_email, subject=email.subject, body=email.body,
                received_at=payload.get("Date") or "", thread=email.thread,
                result={"disposition": "filed", "kind": g.kind, "reason": g.reason},
            )
            with _STATE_LOCK:  # serialize against clear_filed / retriage rewrites (see the reply path)
                append_inbox(item, path=inbox_path(CUSTOMER))
            _stamp_inbound()
            return self._send_json(200, {"ok": True, "disposition": "filed", "kind": g.kind})

        # A customer message that needs a reply. The gate resolves the real customer when the sender
        # is a relay (Shopify contact-form, a forwarder): the resolved address differs from the
        # envelope From. Only then do we trust its extraction (real customer + de-boilerplated
        # message) — for mail a customer sent directly we keep the headers and original body exactly,
        # so the model never quietly rewrites a customer's own words. reply_to prefers the resolved
        # target, else the payload's (a rule short-circuit leaves the resolution empty).
        relay = bool(g.customer_email) and g.customer_email.lower() != email.sender_email.lower()
        if relay:
            email = replace(
                email,
                sender_name=g.customer_name or email.sender_name,
                sender_email=g.customer_email,
                body=g.customer_message or email.body,
            )
        # The customer's photos. On a defect claim ("see attached") the photo IS the evidence, and
        # until now the model never saw it — Postmark parsed the attachment and this handler dropped
        # it. Saved HERE, below the gate, and not before it: a newsletter's tracking pixels and a
        # receipt's logo are image attachments too, and writing every one of them would fill the
        # volume with junk. Only mail that is actually getting triaged is worth the bytes.
        email = replace(email, image_paths=_save_inbound_images(payload, email.id, customer=CUSTOMER))
        reply_to = g.reply_target or reply_target(payload)
        # This — not /api/preview — is the path the operator's real mail takes, so it is the path
        # that has to learn. A 503 here delays the mail (Postmark retries); an untaught draft would
        # be stored in the inbox and read as if Bean had considered everything they taught it.
        corrections = self._load_corrections_or_503()
        if corrections is None:
            return
        try:
            result, result_d = _run_engine(email, config, corrections)
        except OutOfCreditsError:
            raise  # do_POST → 402; non-2xx, so Postmark retries once topped up
        except Exception as exc:  # noqa: BLE001 - delay the mail (Postmark retries), never crash the webhook
            log.warning("inbound triage failed for %s: %s — 503, Postmark will retry", email.id, exc)
            return self._send_json(503, {"error": "temporarily unavailable — retry"})
        item = InboxItem(
            id=email.id, sender_name=email.sender_name, sender_email=email.sender_email,
            reply_to=reply_to, subject=email.subject, body=email.body,
            received_at=payload.get("Date") or "", thread=email.thread,
            result=result_d,
        )
        # The append is under _STATE_LOCK — the model call above is deliberately OUTSIDE it.
        # clear_filed rewrites inbox.jsonl (read → os.replace); an unlocked append landing mid-rewrite
        # would be clobbered by the replace, losing a real customer email.
        with _STATE_LOCK:
            append_inbox(item, path=inbox_path(CUSTOMER))
        _stamp_inbound()
        return self._send_json(200, {"ok": True, "disposition": "reply", "email_id": email.id})


def serve(host: str = "127.0.0.1", port: int = 8011) -> None:
    # Without this the root logger sits at WARNING with no handler, so every log.info in this module
    # — including the one-line-per-request access log — is discarded. A dropped inbound email then
    # looks exactly like an email that never arrived, which is how the forwarding-verification drop
    # went unnoticed. Configured here (the entry point), never at import, so tests own their logging.
    logging.basicConfig(
        level=os.environ.get("BEAN_LOG_LEVEL", "INFO").upper(),
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )
    # A one-time nudge so a local dev knows the gate is OFF. In prod (BEAN_ENV=production) an unset
    # passcode fails CLOSED per-request instead (see _gate); here it just fails open, as before.
    if not os.environ.get("BEAN_PASSCODE", ""):
        log.warning("BEAN_PASSCODE unset — passcode gate is OFF (dev). Set BEAN_ENV=production to fail closed.")
    # WHOSE MAIL IS THIS? In production the tenant must be named out loud. The default is the DEMO
    # store (bean/paths.py), so an unset BEAN_CUSTOMER in prod would serve demo fields out of an empty
    # folder and the first save would persist them — the fixtures-leaked-into-prod bug, which has
    # already happened once to a real operator's config. Refuse to start instead: a crashed boot is
    # caught by scripts/deploy.sh and rolled back, while a silent tenant switch is not caught by
    # anything. Dev is unaffected — it just runs as the demo store, which is the safe answer there.
    if _production() and not os.environ.get("BEAN_CUSTOMER", "").strip():
        raise SystemExit(
            "REFUSING TO START: BEAN_ENV=production with BEAN_CUSTOMER unset.\n"
            f"  The default tenant is the demo store ({CUSTOMER!r}) — serving it to a real deployment "
            "would show demo data and overwrite the real config on first save.\n"
            "  Set BEAN_CUSTOMER explicitly on the service."
        )
    try:
        httpd = ThreadingHTTPServer((host, port), BeanHandler)
    except OSError as exc:
        # "Address already in use" is the single most likely first-run failure for anyone who isn't
        # the author — port 8011 is a guess, not a reservation. A socketserver traceback is forty
        # lines that never say "the port is taken" or "here is the flag", so say both.
        raise SystemExit(
            f"REFUSING TO START: cannot bind {host}:{port} — {exc.strerror or exc}.\n"
            f"  Something else is probably already listening there (another Bean, most likely).\n"
            f"  Pick another port:  python -m bean.server --port 8012\n"
            f"  ...or set PORT=8012 in the environment."
        ) from exc
    print(f"Bean server on http://{host}:{port}  (config: {CONFIG_PATH}, static: {WEB_DIR})")
    try:
        httpd.serve_forever()
    # Ctrl-C is how an operator stops a dev server. Not a failure, and nothing to report.
    except KeyboardInterrupt:
        pass
    finally:
        httpd.server_close()


if __name__ == "__main__":
    import argparse

    ap = argparse.ArgumentParser(description="Bean local backend (config persistence + preview + static).")
    # Env-aware defaults so Railway (which injects PORT and requires 0.0.0.0) works with zero flags,
    # while local dev is unchanged: loopback-only on 8011 when PORT is unset.
    ap.add_argument("--host", default="0.0.0.0" if os.environ.get("PORT") else "127.0.0.1")
    ap.add_argument("--port", type=int, default=int(os.environ.get("PORT", 8011)))
    args = ap.parse_args()
    serve(args.host, args.port)
