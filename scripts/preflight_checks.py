"""The three structural checks preflight cannot express in grep. Run via scripts/preflight.sh.

Each check corresponds to a way this project has actually broken, and each is written so that
REVERTING THE FIX MAKES IT FAIL. A check nobody watched fail is not a check.

  c. silent success — an exception handler that swallows a failure and returns a default, or a
     handler that answers 2xx from inside an `except`.
  d. non-atomic write — a whole-file replace on the data volume that skips write_json_atomic.
  e. fail-open auth — an auth check that returns True when its secret is absent, reached from a
     call site that is not guarded by _production() first.

For (c) and (d) an inline comment IS the escape hatch, deliberately. Most of these hits are correct:
a read fallback, an append-only log, a best-effort instrumentation write. What is not acceptable is
a hit nobody looked at. So the rule is: a hit must be fixed, or explained in place, in a comment on
the line or in the three lines above it. The reviewer's question — "is this one of the good ones?" —
gets answered where the code is, not in a wiki.

Zero hits would mean the check is broken. This repo shipped four bugs of this exact shape in a day.
"""

from __future__ import annotations

import ast
import io
import re
import sys
import tokenize
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
BEAN = sorted((REPO / "bean").glob("*.py"))
WEB = sorted((REPO / "web").glob("*.jsx"))

# The whole-file writers that legitimately bypass write_json_atomic, and why.
_ATOMIC_WRITER = "store.py"          # write_json_atomic itself — this IS the atomic path


class Hit:
    def __init__(self, path: Path, line: int, source: str, why: str):
        self.path, self.line, self.source, self.why = path, line, source.strip(), why

    @property
    def rel(self) -> str:
        return str(self.path.relative_to(REPO))

    def __str__(self) -> str:
        return f"  {self.rel}:{self.line}  {self.source}\n      → {self.why}"


def _comment_lines(path: Path) -> set[int]:
    """Line numbers carrying a comment, for the annotation rule."""
    if path.suffix == ".py":
        out = set()
        with path.open("rb") as fh:
            for tok in tokenize.tokenize(fh.readline):
                if tok.type == tokenize.COMMENT:
                    out.add(tok.start[0])
        return out
    return {i for i, ln in enumerate(path.read_text().splitlines(), 1) if "//" in ln}


def _annotated(path: Path, line: int, comments: set[int]) -> bool:
    """Annotated = a comment on this line, or on any of the three lines above it."""
    return any(n in comments for n in range(line - 3, line + 1))


def _returns_a_default(node: ast.stmt) -> bool:
    if isinstance(node, (ast.Pass, ast.Continue)):
        return True
    if isinstance(node, ast.Return):
        v = node.value
        if v is None:
            return True
        if isinstance(v, ast.Constant) and v.value in (None, False, 0, ""):
            return True
        if isinstance(v, (ast.Dict, ast.List, ast.Tuple)) and not getattr(v, "elts", getattr(v, "keys", [])):
            return True
    return False


def _is_logging(node: ast.stmt) -> bool:
    return (
        isinstance(node, ast.Expr) and isinstance(node.value, ast.Call)
        and isinstance(node.value.func, ast.Attribute)
        and node.value.func.attr in {"warning", "error", "info", "debug", "exception", "print"}
    )


# ---- c. silent success ------------------------------------------------------------------------

def check_silent_success() -> tuple[list[Hit], list[Hit]]:
    """Returns (unannotated, annotated). Unannotated hits fail the build."""
    hits: list[Hit] = []

    for path in BEAN:
        src = path.read_text()
        tree = ast.parse(src)
        lines = src.splitlines()
        for node in ast.walk(tree):
            if isinstance(node, ast.ExceptHandler):
                body = [s for s in node.body if not _is_logging(s)]
                if body and all(_returns_a_default(s) for s in body):
                    hits.append(Hit(path, node.lineno, lines[node.lineno - 1],
                                    "except handler swallows the failure and returns a default"))
            # A 2xx sent from inside an except block: success reported from the error path.
            if isinstance(node, ast.ExceptHandler):
                for sub in ast.walk(node):
                    if (isinstance(sub, ast.Call) and isinstance(sub.func, ast.Attribute)
                            and sub.func.attr == "_send_json" and sub.args
                            and isinstance(sub.args[0], ast.Constant)
                            and isinstance(sub.args[0].value, int)
                            and 200 <= sub.args[0].value < 300):
                        hits.append(Hit(path, sub.lineno, lines[sub.lineno - 1],
                                        "2xx returned from inside an exception handler"))

    js_swallow = re.compile(r"\.catch\(\s*\(\s*\w*\s*\)\s*=>\s*(\{\s*\}|null|undefined|\[\])\s*\)"
                            r"|catch\s*(\(\s*\w+\s*\))?\s*\{\s*\}")
    for path in WEB:
        for i, ln in enumerate(path.read_text().splitlines(), 1):
            if js_swallow.search(ln):
                hits.append(Hit(path, i, ln, "JS catch swallows the failure"))

    annotated, unannotated = [], []
    for h in hits:
        (annotated if _annotated(h.path, h.line, _comment_lines(h.path)) else unannotated).append(h)
    return unannotated, annotated


# ---- d. non-atomic writes to the volume -------------------------------------------------------

def check_non_atomic_writes() -> tuple[list[Hit], list[Hit]]:
    hits: list[Hit] = []
    for path in BEAN:
        if path.name == _ATOMIC_WRITER:
            continue
        src = path.read_text()
        lines = src.splitlines()
        for node in ast.walk(ast.parse(src)):
            if not isinstance(node, ast.Call):
                continue
            fn = node.func
            if isinstance(fn, ast.Attribute) and fn.attr in {"write_text", "write_bytes"}:
                hits.append(Hit(path, node.lineno, lines[node.lineno - 1],
                                "whole-file write that is not write_json_atomic"))
            # open(..., "w") — a truncating write. Append mode is the JSONL logs; those are correct.
            if isinstance(fn, ast.Name) and fn.id == "open" or (isinstance(fn, ast.Attribute) and fn.attr == "open"):
                mode = next((a.value for a in node.args if isinstance(a, ast.Constant) and isinstance(a.value, str)), "")
                mode = mode or next((kw.value.value for kw in node.keywords
                                     if kw.arg == "mode" and isinstance(kw.value, ast.Constant)), "")
                if mode.startswith("w"):
                    hits.append(Hit(path, node.lineno, lines[node.lineno - 1],
                                    "open(..., 'w') truncates; use write_json_atomic"))

    annotated, unannotated = [], []
    for h in hits:
        (annotated if _annotated(h.path, h.line, _comment_lines(h.path)) else unannotated).append(h)
    return unannotated, annotated


# ---- e. fail-open auth ------------------------------------------------------------------------

def _fail_open_functions(tree: ast.Module) -> dict[str, int]:
    """Functions containing `if not <something>: return True` — an auth check that opens when its
    secret is empty. Legitimate for local dev; lethal in prod unless a _production() guard runs first.
    """
    found: dict[str, int] = {}
    for fn in ast.walk(tree):
        if not isinstance(fn, ast.FunctionDef):
            continue
        for node in ast.walk(fn):
            if (isinstance(node, ast.If) and isinstance(node.test, ast.UnaryOp)
                    and isinstance(node.test.op, ast.Not) and len(node.body) == 1
                    and isinstance(node.body[0], ast.Return)
                    and isinstance(node.body[0].value, ast.Constant)
                    and node.body[0].value.value is True):
                found[fn.name] = node.lineno
    return found


def _guarded_call_sites(tree: ast.Module, target: str) -> list[tuple[int, bool]]:
    """Every call to `target`, with whether _production() is invoked earlier in the same function."""
    sites: list[tuple[int, bool]] = []
    for fn in ast.walk(tree):
        if not isinstance(fn, ast.FunctionDef):
            continue
        prod_lines = [n.lineno for n in ast.walk(fn)
                      if isinstance(n, ast.Call) and isinstance(n.func, ast.Name) and n.func.id == "_production"]
        for node in ast.walk(fn):
            if isinstance(node, ast.Call):
                name = node.func.attr if isinstance(node.func, ast.Attribute) else getattr(node.func, "id", None)
                if name == target:
                    sites.append((node.lineno, any(p <= node.lineno for p in prod_lines)))
    return sites


def check_fail_open_auth() -> list[Hit]:
    server = REPO / "bean" / "server.py"
    src = server.read_text()
    tree = ast.parse(src)
    lines = src.splitlines()
    bad: list[Hit] = []
    for name, lineno in _fail_open_functions(tree).items():
        sites = _guarded_call_sites(tree, name)
        if not sites:
            continue  # defined but never called from this module; nothing to gate
        for call_line, guarded in sites:
            if not guarded:
                bad.append(Hit(server, call_line, lines[call_line - 1],
                               f"{name}() fails open on an absent secret and this call site "
                               f"(defined at line {lineno}) is not behind a _production() guard"))
    return bad


# ---- report -----------------------------------------------------------------------------------

def main() -> int:
    failed = False

    ss_bad, ss_ok = check_silent_success()
    print(f"[c] silent-success signatures: {len(ss_bad) + len(ss_ok)} hits "
          f"({len(ss_ok)} annotated, {len(ss_bad)} unannotated)")
    if ss_bad:
        failed = True
        print("    UNANNOTATED — fix it, or say in a comment why it is correct:")
        for h in ss_bad:
            print(h)

    nw_bad, nw_ok = check_non_atomic_writes()
    print(f"[d] non-atomic volume writes: {len(nw_bad) + len(nw_ok)} hits "
          f"({len(nw_ok)} annotated, {len(nw_bad)} unannotated)")
    if nw_bad:
        failed = True
        for h in nw_bad:
            print(h)

    auth = check_fail_open_auth()
    print(f"[e] fail-open auth reached without a _production() guard: {len(auth)}")
    if auth:
        failed = True
        for h in auth:
            print(h)

    # A grep that finds nothing on a codebase with this history is a broken grep, not a clean repo.
    if len(ss_bad) + len(ss_ok) == 0:
        print("[c] FAIL: zero hits. The detector is broken — this repo has these patterns.")
        failed = True

    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
