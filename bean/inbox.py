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
import os
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path

from bean.paths import inbox_path


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


def clear_filed(path: Path | None = None, *, backup_path: Path | None = None) -> tuple[int, int]:
    """Delete the gate-FILED (FYI) mail from the inbox log, keeping every customer email.

    inbox.jsonl is append-only with no per-line delete, so this rewrites the file: keep every line
    that is NOT `_is_filed`, drop the rest. A timestamped backup is written to the same volume
    BEFORE the rewrite (recoverable if a filing was wrong), and the replace is atomic (tmp + rename)
    so a crash mid-write can't truncate the log. Returns (removed, kept). No filed mail ⇒ no write,
    no backup — a no-op stays a no-op. `backup_path` is injectable so tests are deterministic."""
    path = path or inbox_path()
    if not path.exists():
        return (0, 0)
    lines = [ln for ln in path.read_text(encoding="utf-8").splitlines() if ln.strip()]
    kept = [ln for ln in lines if not _is_filed(ln)]
    removed = len(lines) - len(kept)
    if removed == 0:
        return (0, len(kept))
    backup = backup_path or path.with_name(
        f"{path.name}.bak-filed-{time.strftime('%Y%m%dT%H%M%SZ', time.gmtime())}"
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
