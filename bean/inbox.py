"""The live triaged inbox — customer mail received via the inbound webhook, with its result.

When a customer email arrives (bean/server.py POST /api/inbound), Bean triages it once and stores
the outcome here: one JSON line per email, carrying the stored `draft_dict(DraftResult)` so the UI can
render the inbox without re-running the model on every load. Only mail that NEEDS A REPLY lands
here — filed newsletters/notifications never do (the operator shouldn't see spam).

Persistence mirrors the order index (bean/order_index.py) and the correction log exactly: JSONL on
the BEAN_DATA_DIR volume, same JSONL→SQLite migration trigger documented in bean/paths.py.
"""

from __future__ import annotations

import json
import logging
import os
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path

from bean.paths import filed_history_path, inbox_path

log = logging.getLogger("bean.inbox")


@dataclass
class InboxItem:
    """One triaged customer email. `result` is the `draft_dict(DraftResult)` dict — stored so a
    reload renders from disk, not a fresh model call. `reply_to` is who Bean must answer (the
    customer, not the forwarding address). `received_at` comes from the webhook payload's Date
    (kept as-is; never a wall-clock read, so ingestion is deterministic/testable)."""

    id: str
    sender_name: str
    sender_email: str
    reply_to: str
    subject: str
    body: str
    received_at: str
    result: dict = field(default_factory=dict)
    # The quoted reply chain (oldest→newest), as extracted by bean/inbound.py. Persisted because an
    # email Bean already triaged is the raw material of its memory of this customer — dropping it
    # here meant every re-read, every teach, and every look back at "what did they actually say"
    # saw only the latest message, stripped of the conversation it belonged to. Lines written before
    # this field exists simply have no `thread` key; every reader uses `.get` (load_inbox hands back
    # raw dicts, so an old line is not a broken line).
    thread: list[str] = field(default_factory=list)


def append_inbox(item: InboxItem, *, path: Path | None = None) -> InboxItem:
    """Append one InboxItem as a JSON line (mirrors order_index.record_order). `path` defaults to
    inbox_path() resolved LIVE (respects a runtime BEAN_DATA_DIR — how tests point at a tmp dir)."""
    path = path or inbox_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as fh:
        fh.write(json.dumps(asdict(item), sort_keys=True) + "\n")
    return item


def load_inbox(path: Path | None = None) -> list[dict]:
    """Every persisted inbox item as a plain dict (oldest→newest). Returns [] when absent."""
    path = path or inbox_path()
    if not path.exists():
        return []
    out = []
    for line in path.read_text(encoding="utf-8").splitlines():
        if line.strip():
            out.append(json.loads(line))
    return out


def _is_filed(line: str) -> bool:
    """True ONLY for a line that is a gate-FILED item (a newsletter/promo/notification Bean set
    aside), never for a customer email that walked the tree. The predicate is deliberately narrow
    and double-guarded: a filed line's result is exactly `{"disposition": "filed", ...}` and a
    walked email's result carries `chunks` — so requiring disposition=='filed' AND no chunks means
    a parse quirk can only ever KEEP an email, never delete a real one. An unparseable line is not
    filed (kept) — the same fail-safe-toward-keeping stance as the rest of this module."""
    try:
        rec = json.loads(line)
    # Returning False (not filed) on an unparseable line is the SAFE direction, not a swallowed bug:
    # clear_filed KEEPS every non-filed line, so a corrupt line is preserved, never deleted.
    except (json.JSONDecodeError, ValueError):
        return False
    result = rec.get("result") if isinstance(rec, dict) else None
    if not isinstance(result, dict):
        return False
    return result.get("disposition") == "filed" and not result.get("chunks")


def _rollup_drafted(lines: list[str], history: Path) -> None:
    """Append per-day tallies of the WALKED (drafted) lines about to be deleted.

    `_rollup_filed`'s twin, and it exists because the reasoning that said walked mail did not need
    one was wrong. That note argued the record lives in corrections.jsonl — true of the OUTCOME, and
    irrelevant to the two consumers that actually broke: `outcomes.daily_counts` builds the stats
    page's daily "drafted" bars from inbox.jsonl, and `_stats_summary`'s windowed loop id-joins
    corrections to it. So the first real use of the Clear sweep flattened three weeks of drafted
    bars to zero and zeroed the loop, and the page told the operator Bean had done nothing.

    Same file as the filed rollup, on purpose: it is one archive of "what was deleted from the
    inbox, per day", and two files would have to be cleared, backed up and reasoned about together.
    The two loaders each skip a line missing their own key, so the formats cannot collide.

    Never fatal, like its twin: losing a bar on a chart must not stop the operator clearing mail.
    """
    from bean.outcomes import daily_counts  # local: outcomes imports this module (load_inbox)

    records = []
    for ln in lines:
        try:
            records.append(json.loads(ln))
        except (json.JSONDecodeError, ValueError):  # preserved by the caller; just not tallied
            continue
    rows, _undated = daily_counts(records)
    rows = [r for r in rows if r["drafted"]]
    if not rows:
        return
    try:
        history.parent.mkdir(parents=True, exist_ok=True)
        with history.open("a", encoding="utf-8") as fh:
            for r in rows:
                fh.write(json.dumps({"day": r["day"], "drafted": r["drafted"]}, sort_keys=True) + "\n")
    except OSError as exc:
        log.warning("could not write drafted history: %s", exc)


def _rollup_filed(lines: list[str], history: Path) -> None:
    """Append per-day tallies of the filed lines about to be deleted (see paths.filed_history_path).

    Called BEFORE the rewrite, and never fatal: losing a chart tally must not stop the operator
    clearing their inbox, and the `.bak-filed-*` snapshot is written either way — so an OSError here
    costs a bar on a graph, not mail. Same stance as `llm._log_usage`: observability can't take down
    the product.

    Days come from `outcomes.daily_counts`, so the one notion of "which day did this arrive" lives in
    one place and the archived bars can't drift from the live ones (received_at is RFC-2822; it does
    not sort and cannot be sliced — see that function). Undated lines contribute no row: a filed
    email with no readable date is real, but it has nowhere to sit on a daily chart, and inventing a
    day for it would be the quiet lie this log exists to prevent.
    """
    from bean.outcomes import daily_counts  # local: outcomes imports this module (load_inbox)

    records = []
    for ln in lines:
        try:
            records.append(json.loads(ln))
        except (json.JSONDecodeError, ValueError):  # preserved by the caller; just not tallied
            continue
    rows, _undated = daily_counts(records)
    rows = [r for r in rows if r["filed"]]
    if not rows:
        return
    try:
        history.parent.mkdir(parents=True, exist_ok=True)
        with history.open("a", encoding="utf-8") as fh:
            for r in rows:
                fh.write(json.dumps({"day": r["day"], "filed": r["filed"]}, sort_keys=True) + "\n")
    except OSError as exc:
        log.warning("could not write filed history: %s", exc)


def load_filed_history(path: Path | None = None) -> dict[str, int]:
    """Archived filed-mail counts as {day: total}, summed across every clear. `{}` when absent.

    Append-only and additive by construction: clear_filed only ever tallies lines it is deleting in
    the same breath, so a day can be written by several clears and summing them double-counts
    nothing."""
    path = path or filed_history_path()
    if not path.exists():
        return {}
    out: dict[str, int] = {}
    for line in path.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        try:
            rec = json.loads(line)
            out[rec["day"]] = out.get(rec["day"], 0) + int(rec["filed"])
        # A corrupt tally line is skipped, not raised: this log is decoration on a chart, and it must
        # never be the reason the stats page 503s. The live inbox counts still render.
        except (json.JSONDecodeError, ValueError, KeyError, TypeError):
            continue
    return out


def load_drafted_history(path: Path | None = None) -> dict[str, int]:
    """Archived DRAFTED counts as {day: total} — `load_filed_history`'s twin, over the same file.

    A line written by the filed rollup has no `drafted` key and is skipped by the same KeyError
    guard that skips a corrupt one, so the two tallies share a log without colliding."""
    path = path or filed_history_path()
    if not path.exists():
        return {}
    out: dict[str, int] = {}
    for line in path.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        try:
            rec = json.loads(line)
            out[rec["day"]] = out.get(rec["day"], 0) + int(rec["drafted"])
        # A filed-only line (no `drafted`) lands here, as does a corrupt one. Both are correctly
        # skipped: this loader answers one question and a line that cannot answer it is not an error.
        except (json.JSONDecodeError, ValueError, KeyError, TypeError):
            continue
    return out


def clear_filed(path: Path | None = None, *, backup_path: Path | None = None,
                history_path: Path | None = None) -> tuple[int, int]:
    """Delete the gate-FILED (FYI) mail from the inbox log, keeping every customer email.

    inbox.jsonl is append-only with no per-line delete, so this rewrites the file: keep every line
    that is NOT `_is_filed`, drop the rest. A timestamped backup is written to the same volume
    BEFORE the rewrite (recoverable if a filing was wrong), and the replace is atomic (tmp + rename)
    so a crash mid-write can't truncate the log. Returns (removed, kept). No filed mail ⇒ no write,
    no backup — a no-op stays a no-op. `backup_path`/`history_path` are injectable so tests are
    deterministic.

    The deleted lines are also rolled up per-day into `filed_history.jsonl` first (`_rollup_filed`),
    so clearing the inbox no longer erases the record that Bean filed the mail at all — see
    paths.filed_history_path for why that number is worth keeping."""
    path = path or inbox_path()
    if not path.exists():
        return (0, 0)
    lines = [ln for ln in path.read_text(encoding="utf-8").splitlines() if ln.strip()]
    kept = [ln for ln in lines if not _is_filed(ln)]
    removed = len(lines) - len(kept)
    if removed == 0:
        return (0, len(kept))
    _rollup_filed([ln for ln in lines if _is_filed(ln)], history_path or filed_history_path())
    backup = backup_path or path.with_name(
        f"{path.name}.bak-filed-{time.strftime('%Y%m%dT%H%M%SZ', time.gmtime())}"
    )
    backup.write_text("\n".join(lines) + "\n", encoding="utf-8")  # full original, before we touch it
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(("\n".join(kept) + "\n") if kept else "", encoding="utf-8")
    os.replace(tmp, path)  # atomic
    return (removed, len(kept))


def clear_ids(ids: list[str] | set[str], path: Path | None = None, *,
              backup_path: Path | None = None, history_path: Path | None = None) -> tuple[int, int]:
    """Delete SPECIFIC emails from the inbox log by id — the operator's "clear the handled pile".

    clear_filed's sibling, and deliberately a separate function rather than a predicate argument to
    it: that one deletes by what BEAN decided (gate-filed), this one deletes by what SHE decided
    (already actioned). Conflating them would put customer mail one wrong predicate away from the
    newsletter purge.

    Same safety as clear_filed and supersede_results, because inbox.jsonl is append-only with no
    per-line delete: full backup on the volume BEFORE the rewrite, atomic tmp+rename, and an
    unparseable line is KEPT (a corrupt line has no readable id, and the fail-safe direction here is
    always toward keeping mail). Ids not present are ignored; an empty id set is a no-op with no
    write and no backup.

    The caller decides which ids qualify, and it reads them from status.json server-side rather than
    trusting a client-supplied delete list — a POST body naming arbitrary ids would be a delete-any-
    email primitive, and this is the operator's real mail.

    ⚠️ IT ROLLS UP BEFORE IT DELETES. The first version of this did not, on the reasoning that the
    record of a walked email lives in corrections.jsonl. That was wrong, and it shipped: the
    operator cleared three weeks of actioned mail on 2026-08-21 and her stats page went to ~1
    drafted/day with a loop grading 0 drafts, because BOTH of those read inbox.jsonl and not the
    correction log. See `_rollup_drafted`. Deleting mail is allowed to cost the evidence; it is not
    allowed to cost the count.

    Returns (removed, kept).
    """
    path = path or inbox_path()
    wanted = {i for i in (ids or ()) if isinstance(i, str) and i}
    if not path.exists() or not wanted:
        return (0, 0)
    lines = [ln for ln in path.read_text(encoding="utf-8").splitlines() if ln.strip()]
    kept: list[str] = []
    dropped: list[str] = []
    for ln in lines:
        try:
            rec = json.loads(ln)
        # Unparseable ⇒ no id ⇒ never a match ⇒ kept verbatim. Same stance as _is_filed.
        except (json.JSONDecodeError, ValueError):
            kept.append(ln)
            continue
        (dropped if isinstance(rec, dict) and rec.get("id") in wanted else kept).append(ln)
    removed = len(dropped)
    if removed == 0:
        return (0, len(kept))
    # Collected in the loop, not recovered afterwards by differencing against `kept` — two identical
    # lines would make that both quadratic and wrong.
    _rollup_drafted(dropped, history_path or filed_history_path())
    backup = backup_path or path.with_name(
        f"{path.name}.bak-cleared-{time.strftime('%Y%m%dT%H%M%SZ', time.gmtime())}"
    )
    backup.write_text("\n".join(lines) + "\n", encoding="utf-8")  # full original, before we touch it
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(("\n".join(kept) + "\n") if kept else "", encoding="utf-8")
    os.replace(tmp, path)  # atomic
    return (removed, len(kept))


def supersede_results(updates: dict[str, dict], path: Path | None = None, *, backup_path: Path | None = None) -> int:
    """Replace the stored `result` for specific emails in place (rewrites inbox.jsonl).

    `updates` maps email_id → the new `result` dict. When Bean re-triages a stuck pile after the
    operator teaches new questions (bean/retriage.py), a covered email's verdict changes — it moved from an
    escalate node onto a real leaf — and the inbox must show the new verdict, not the stale one.

    Same safety as clear_filed (inbox.jsonl is append-only with no per-line update): a timestamped
    backup of the full original is written BEFORE the rewrite, the replace is atomic (tmp + rename),
    and it is DOUBLE-GUARDED to only ever swap the `result` field of a line whose id is in `updates`
    — every other line passes through byte-for-byte, and no line is ever dropped, so a customer
    email can never go missing. Ids in `updates` that aren't in the file are ignored. Returns the
    number of lines actually replaced; nothing to replace ⇒ no write, no backup (a no-op stays one).
    """
    path = path or inbox_path()
    if not path.exists() or not updates:
        return 0
    lines = [ln for ln in path.read_text(encoding="utf-8").splitlines() if ln.strip()]
    out: list[str] = []
    replaced = 0
    for ln in lines:
        try:
            rec = json.loads(ln)
        # A corrupt line is preserved verbatim (never dropped, never edited) — the same
        # fail-safe-toward-keeping stance as _is_filed/clear_filed.
        except (json.JSONDecodeError, ValueError):
            out.append(ln)
            continue
        rid = rec.get("id") if isinstance(rec, dict) else None
        if rid in updates:
            rec["result"] = updates[rid]  # swap ONLY the verdict; id/body/thread untouched
            out.append(json.dumps(rec, sort_keys=True))
            replaced += 1
        else:
            out.append(ln)  # byte-for-byte
    if replaced == 0:
        return 0
    backup = backup_path or path.with_name(
        f"{path.name}.bak-retriage-{time.strftime('%Y%m%dT%H%M%SZ', time.gmtime())}"
    )
    backup.write_text("\n".join(lines) + "\n", encoding="utf-8")  # full original, before we touch it
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text("\n".join(out) + "\n", encoding="utf-8")
    os.replace(tmp, path)  # atomic
    return replaced
