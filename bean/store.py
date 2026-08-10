"""Atomic, backup-taking JSON writes — the durability floor under the config volume.

Why this module exists: the customer's config IS the product's moat (the operator's taught
fields/templates/tree), and it lives as a single JSON file on a Railway volume that gets yanked
mid-write on every redeploy. `Path.write_text` opens the target in "w" mode, which TRUNCATES it
to zero bytes *before* the first byte of new content lands — so a crash in that window (redeploy,
OOM, restart) leaves an empty or half-written file. The next read then can't tell "brand-new
customer, never configured" from "their config was just shredded", and the old code guessed the
former: it served demo fixtures and the next save wrote them back over their real data.

This module removes the truncation window entirely: serialize first, take one rolling `.bak` of
the current good file, write the new bytes to a temp file in the SAME directory, fsync it, then
`os.replace` it over the target. On POSIX `os.replace` is an atomic rename — a concurrent reader
sees either the whole old file or the whole new one, never a partial — and a crash at any point
leaves the previous file intact. stdlib only; no framework, no deps (see CLAUDE.md build doctrine).
"""

from __future__ import annotations

import contextlib
import json
import os
import shutil
import tempfile
from pathlib import Path


def write_json_atomic(path: str | Path, obj, *, backup: bool = True) -> None:
    """Durably write ``obj`` as pretty JSON to ``path`` with no truncation window.

    Guarantees, in order:
      1. Serialization happens BEFORE the target is touched. If ``obj`` isn't JSON-serializable
         the call raises and the existing file on disk is completely undisturbed — this is the
         truncation bug in miniature, avoided by never opening the target for writing at all.
      2. If ``backup`` and the target already exists, its current contents are copied to
         ``path + ".bak"`` (a single rolling backup) before anything replaces it. The config has
         no other history, so this `.bak` is the recovery source `load_config` reaches for when the
         live file is found corrupt.
      3. The new bytes are written to a temp file in the SAME directory (same filesystem, so the
         following rename is a true atomic rename and not a cross-device copy), flushed, and
         ``fsync``'d so they are on stable storage before the rename gives them the real name.
      4. ``os.replace(tmp, path)`` atomically swaps the new file in. A reader mid-write sees the
         old file whole; a crash mid-write leaves the old file whole.

    On any failure the temp file is cleaned up so the volume never accumulates ``*.tmp`` turds,
    and the exception propagates (the caller must know the write did not land).
    """
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)

    # Serialize up front: a non-serializable obj must fail loudly here, before we have copied a
    # backup or opened a temp file — the good file stays exactly as it was.
    payload = json.dumps(obj, indent=2)

    # Snapshot the current good file before it's replaced. copy2 preserves mtime so the backup
    # reads as "the previous version", and it survives even if the replace below never runs.
    if backup and path.exists():
        shutil.copy2(path, path.with_suffix(path.suffix + ".bak"))

    # Temp file in the target's own directory — os.replace is only atomic within one filesystem;
    # a temp in /tmp could cross devices and silently degrade to a non-atomic copy.
    fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=path.name + ".", suffix=".tmp")
    try:
        with os.fdopen(fd, "w") as f:
            f.write(payload)
            f.flush()
            os.fsync(f.fileno())  # bytes on stable storage before the rename names them "the file"
        os.replace(tmp, path)
    except BaseException:
        # Never leave the temp behind; the target is untouched (still the previous good file).
        with contextlib.suppress(FileNotFoundError):
            os.unlink(tmp)
        raise

    # Best-effort directory fsync so the rename itself survives a power loss, not just the file
    # contents. Not every platform/filesystem lets you fsync a directory; a failure here doesn't
    # undo the already-durable rename, so it's suppressed rather than surfaced.
    with contextlib.suppress(OSError):
        dir_fd = os.open(path.parent, os.O_RDONLY)
        try:
            os.fsync(dir_fd)
        finally:
            os.close(dir_fd)
