"""Every change ever made to the notebook — the audit trail for the operator's brain.

Supersede shipped without this. A confirmed chat proposal REPLACES a line in her notebook, and the
only record was one rolling `.bak`, so "what did Bean change?" had no answer and "what did it
replace?" had a worse one. This is that answer, as an append-only log beside corrections.jsonl.

Two properties do the work:

  1. **It is written at the SINGLE writer.** Every edit path — the chat, the notebook editor, the
     cite sheet, the questionnaire — goes through PUT /api/notebook. Diffing there makes the trail
     complete by construction; nothing can edit the notebook and forget to log, because logging is
     not something a caller opts into.
  2. **It records the text, not a summary.** `before` and `after` are the lines themselves, so the
     log answers "what exactly did that replace" without anyone having to trust a description of it.

Distinct from corrections.jsonl, which is feedback on DRAFTS. This is edits to the brain those
drafts come from.

Pure diff + append. Same JSONL discipline as bean/corrections.py: append-only, `sort_keys=True`,
unknown keys dropped on read (so a future field cannot make an old image unreadable), absent ⇒ [].
"""

from __future__ import annotations

import json
import time
from dataclasses import asdict, dataclass, fields
from pathlib import Path

from bean.notebook import Notebook
from bean.paths import notebook_history_path

# The notebook sections, and the key each row is identified by. Buckets and macros are NAMED, so a
# reworded cliff under the same name is a CHANGE; facts and notes have no name, so their text is
# their identity and a reworded fact reads as one removal plus one addition. That is honest — there
# is no way to know the two were "the same line" without a name to hold them together.
_SECTIONS = (
    ("buckets", "How I route", "name", "cliff"),
    ("macros", "My standard answers", "name", "text"),
    ("facts", "Store facts", None, "text"),
    ("notes", "Judgment notes", None, "text"),
)

ACTIONS = ("added", "removed", "changed")


@dataclass
class Change:
    """One changed line. `label` is the row's name for a bucket/macro and "" otherwise."""

    ts: str = ""
    section: str = ""       # the operator-facing section name, e.g. "Store facts"
    action: str = ""        # added | removed | changed
    label: str = ""
    before: str = ""
    after: str = ""
    source: str = "editor"  # chat | editor — where the edit came from


_FIELD_NAMES = frozenset(f.name for f in fields(Change))


def _rows(nb: dict, key: str, name_key: str | None, text_key: str):
    for row in (nb.get(key) or []):
        if not isinstance(row, dict):
            continue
        yield (str(row.get(name_key) or "") if name_key else ""), str(row.get(text_key) or "")


def diff(before: Notebook, after: Notebook, *, source: str = "editor", ts: str = "") -> list[Change]:
    """Every line that differs between two notebooks, as Change rows.

    Order is stable (section order, then added/changed before removed) so two runs over the same
    pair produce the same log. Returns [] when nothing changed — and the caller must not write a
    file for an empty diff, so a save that changes nothing leaves no trace.
    """
    stamp = ts or time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
    b, a = before.to_dict(), after.to_dict()
    out: list[Change] = []

    for key, label, name_key, text_key in _SECTIONS:
        old = list(_rows(b, key, name_key, text_key))
        new = list(_rows(a, key, name_key, text_key))

        if name_key:
            # Named rows: match on the name, so a reworded cliff is one `changed`, not a
            # remove+add pair that loses the connection between the two texts.
            old_by, new_by = dict(old), dict(new)
            for nm, text in new:
                if nm not in old_by:
                    out.append(Change(stamp, label, "added", nm, "", text, source))
                elif old_by[nm] != text:
                    out.append(Change(stamp, label, "changed", nm, old_by[nm], text, source))
            for nm, text in old:
                if nm not in new_by:
                    out.append(Change(stamp, label, "removed", nm, text, "", source))
            continue

        # Unnamed rows: identity IS the text. Multiset difference, so duplicates are handled and an
        # unchanged line never shows up as churn.
        old_texts, new_texts = [t for _, t in old], [t for _, t in new]
        remaining = list(old_texts)
        added = []
        for text in new_texts:
            if text in remaining:
                remaining.remove(text)
            else:
                added.append(text)
        removed = remaining

        # A single edit to one line is the overwhelmingly common case, and reporting it as
        # remove+add would hide the very thing this log exists to show: what replaced what.
        while added and removed:
            out.append(Change(stamp, label, "changed", "", removed.pop(0), added.pop(0), source))
        out += [Change(stamp, label, "added", "", "", t, source) for t in added]
        out += [Change(stamp, label, "removed", "", t, "", source) for t in removed]

    return out


def record(changes: list[Change], *, log_path: Path | None = None) -> int:
    """Append changes to the log. Returns how many rows were written; [] writes nothing at all."""
    if not changes:
        return 0
    path = log_path or notebook_history_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as fh:
        for change in changes:
            fh.write(json.dumps(asdict(change), sort_keys=True) + "\n")
    return len(changes)


def load(log_path: Path | None = None) -> list[Change]:
    """The log, oldest first. Absent ⇒ []. Unknown keys are dropped rather than raising, so adding a
    field later cannot make an older image unreadable — the lesson bean/corrections.py records."""
    path = log_path or notebook_history_path()
    try:
        text = path.read_text(encoding="utf-8")
    # Degrade-and-continue is CORRECT here, unlike corrections.load which raises. An absent log is
    # the ordinary case (she has edited nothing yet), and an unreadable one costs a VIEW OF THE
    # PAST — never the notebook, never a draft. corrections.py refuses because drafting without
    # her corrections produces an untaught reply that goes to a customer; nothing here can.
    except (FileNotFoundError, OSError):
        return []
    out: list[Change] = []
    for line in text.splitlines():
        if not line.strip():
            continue
        # One torn line — a crash mid-append, a truncated write — must not hide every other change
        # she has made. Skipping it shows the rest of the history; raising would show none of it.
        try:
            row = json.loads(line)
        except (json.JSONDecodeError, ValueError):
            continue
        if isinstance(row, dict):
            out.append(Change(**{k: v for k, v in row.items() if k in _FIELD_NAMES}))
    return out
