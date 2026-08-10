"""The notebook — a per-store, human-readable, model-neutral brain.

This is the thing that replaces the routing tree. Not a data structure the code walks: a few KB
of markdown that states, in the operator's own terms, how she handles her mail —

  - the SITUATION buckets, each with its cliff stated as *what she does*, not the rule she cites
    (cosine can't interpolate a policy cliff, so the cliff is prose);
  - her standard answers (macros);
  - store facts, each tagged by provenance (`stated` by her / `store-record` from Shopify / a crawl
    / `observed` — distilled from what she actually did). Precedent (`observed`) outranks stated
    policy inside the same call that drafts;
  - judgment notes distilled from real replies.

It renders to the cached system-prompt prefix the engine sends on every call (bean/engine.py). She
can read and edit the file — it's hers — which is why the format is markdown a human writes, not
JSON a parser prefers. `load` reads it back so the engine can enumerate her buckets and the distiller
can round-trip an edit; the file IS the render (save writes exactly what render emits).
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path

# Provenance tags on facts/notes — the trust label that rides into the prompt. `observed` is
# precedent (what she DID) and outranks `stated` policy (what she SAYS) when they conflict, per the
# shipping-doc contradiction the audit found. `store-record` is ground truth from Shopify/a crawl.
PROVENANCE = ("stated", "observed", "store-record")

_H2 = re.compile(r"^##\s+(.*?)\s*$")
_H3 = re.compile(r"^###\s+(.*?)\s*$")
_BULLET = re.compile(r"^-\s+(?:\[(?P<tag>[a-z-]+)\]\s*)?(?P<text>.*?)\s*$")

# A bucket's stakes gate the one-tap approve: a `high`-stakes bucket (money out the door, a
# commitment she'd want eyes on — warranty replacements, refunds, escalations) never auto-approves;
# its grounded drafts are held at YELLOW for her to eyeball. Encoded as a standalone marker line in
# the bucket body so the markdown stays hers to edit; render emits it ONLY for `high`, and load
# strips it back out so it never leaks into the cliff prose. Default `normal` = green-eligible.
_STAKES_HIGH_MARKER = "[stakes: high]"

# Section headers — kept as constants so render() and load() can never drift apart.
_SEC_BUCKETS = "How I route (situation buckets)"
_SEC_MACROS = "My standard answers (macros)"
_SEC_FACTS = "Store facts"
_SEC_NOTES = "Judgment notes (from what I've actually done)"


@dataclass
class Bucket:
    """One situation bucket. `cliff` is the discriminator stated as behaviour — 'within 90 days I
    send a replacement; months of wear I frame lifespan and offer a discount' — not '90-day policy'."""

    name: str
    cliff: str
    stakes: str = "normal"  # "normal" (green-eligible) | "high" (grounded drafts capped at yellow)


@dataclass
class Macro:
    name: str
    text: str


@dataclass
class Fact:
    text: str
    provenance: str = "stated"


@dataclass
class Note:
    text: str
    provenance: str = "observed"


@dataclass
class Notebook:
    store: str
    buckets: list[Bucket] = field(default_factory=list)
    macros: list[Macro] = field(default_factory=list)
    facts: list[Fact] = field(default_factory=list)
    notes: list[Note] = field(default_factory=list)

    def bucket_names(self) -> list[str]:
        return [b.name for b in self.buckets]

    def render(self) -> str:
        """The cached system-prompt prefix — the whole notebook as markdown. Stable across emails
        (only the per-email user message changes), so it caches byte-for-byte."""
        lines: list[str] = [f"# {self.store} — support notebook", ""]

        lines.append(f"## {_SEC_BUCKETS}")
        lines.append(
            "Put every email in exactly ONE bucket below, chosen by what I would DO about it — "
            "not by the product it mentions. When two could fit, the cliff text decides."
        )
        lines.append("")
        for b in self.buckets:
            lines.append(f"### {b.name}")
            lines.append(b.cliff)
            if b.stakes == "high":
                lines.append(_STAKES_HIGH_MARKER)
            lines.append("")

        lines.append(f"## {_SEC_MACROS}")
        lines.append("")
        for m in self.macros:
            lines.append(f"### {m.name}")
            lines.append(m.text)
            lines.append("")

        lines.append(f"## {_SEC_FACTS}")
        for f in self.facts:
            lines.append(f"- [{f.provenance}] {f.text}")
        lines.append("")

        lines.append(f"## {_SEC_NOTES}")
        lines.append(
            "Precedent (what I actually did, tagged `observed`) outranks stated policy when they clash."
        )
        for n in self.notes:
            lines.append(f"- [{n.provenance}] {n.text}")
        lines.append("")

        return "\n".join(lines).rstrip() + "\n"

    def save(self, path: Path) -> None:
        """Write the notebook markdown atomically. It's the operator's brain (PII): a crash or a
        redeploy mid-write must never blank it, and a concurrent reader (the draft path) must never
        see a half-written file. So: write a temp file, snapshot the prior version to `.bak`, then
        atomically swap it in (`Path.replace` is an atomic rename on the same filesystem). Never call
        this under a live customer without consent — the distiller writes to a review path, the
        operator approves, only then does it become hers."""
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(path.suffix + ".tmp")
        # Not write_json_atomic because this is markdown, not JSON — but it IS the same discipline:
        # the only non-atomic writes here are to .tmp and .bak (scratch paths no reader ever opens),
        # and the live file changes solely through the atomic tmp.replace() below.
        tmp.write_text(self.render(), encoding="utf-8")
        if path.exists():
            bak = path.with_suffix(path.suffix + ".bak")
            # A copy of the PREVIOUS version, for recovery. A torn .bak costs a recovery option, never
            # the live notebook — which is still the old file until the replace() lands.
            bak.write_text(path.read_text(encoding="utf-8"), encoding="utf-8")
        tmp.replace(path)

    def to_dict(self) -> dict:
        """Structured JSON for the editor (GET /api/notebook binds fields to this). The markdown
        render() stays the source of truth for the model prefix and the ETag; this is the wire shape
        the human edits, round-tripped back through from_dict()."""
        return {
            "store": self.store,
            "buckets": [{"name": b.name, "cliff": b.cliff, "stakes": b.stakes} for b in self.buckets],
            "macros": [{"name": m.name, "text": m.text} for m in self.macros],
            "facts": [{"text": f.text, "provenance": f.provenance} for f in self.facts],
            "notes": [{"text": n.text, "provenance": n.provenance} for n in self.notes],
        }

    @classmethod
    def from_dict(cls, data: dict) -> "Notebook":
        """Build a Notebook from the editor's JSON, clamping at the boundary: stakes to {normal,high},
        provenance to PROVENANCE, and blank rows dropped (the editor leaves empty add-slots). This is
        the validation seam — PUT /api/notebook trusts nothing the client sends past here."""

        def prov(v: object, default: str) -> str:
            return v if v in PROVENANCE else default

        buckets = [
            Bucket(name, str(b.get("cliff", "")).strip(), "high" if b.get("stakes") == "high" else "normal")
            for b in data.get("buckets", [])
            if (name := str(b.get("name", "")).strip())
        ]
        macros = [
            Macro(name, str(m.get("text", "")).strip())
            for m in data.get("macros", [])
            if (name := str(m.get("name", "")).strip())
        ]
        facts = [
            Fact(text, prov(f.get("provenance"), "stated"))
            for f in data.get("facts", [])
            if (text := str(f.get("text", "")).strip())
        ]
        notes = [
            Note(text, prov(n.get("provenance"), "observed"))
            for n in data.get("notes", [])
            if (text := str(n.get("text", "")).strip())
        ]
        return cls(
            store=str(data.get("store", "Store")).strip() or "Store",
            buckets=buckets,
            macros=macros,
            facts=facts,
            notes=notes,
        )


# A macro name like `INTL_SHIPPING_PAUSED` — a config template KEY, not a phrase a human wrote. It
# leaks in when a distilled notebook inherits her old config's template ids. Harmless to the model,
# but it renders on her questionnaire card as "your usual reply for INTL_SHIPPING_PAUSED — true?",
# which is exactly the internal-vocabulary-in-her-face the notebook UX forbids.
_KEY_LIKE = re.compile(r"^[A-Z0-9]+(?:_[A-Z0-9]+)+$")


def humanize_macro_names(notebook: Notebook) -> Notebook:
    """Return a copy of `notebook` with any KEY_LIKE macro name turned into a plain phrase
    (`INTL_SHIPPING_PAUSED` → "Intl shipping paused"). Deterministic and conservative: a name that
    already reads like a human wrote it (has a space, or any lowercase) is left exactly as-is, so
    running this twice — or over a notebook she's already reworded — changes nothing. The macro TEXT
    (her actual reply) is never touched. Mechanical only: it fixes the shouting, not the wording —
    they can reword any of these in the walk, and the distiller prompt asks the model for human
    names up front, so future stores don't need this at all."""
    def humanize(name: str) -> str:
        if not _KEY_LIKE.match(name or ""):
            return name  # already human — leave it
        phrase = name.lower().replace("_", " ").strip()
        return phrase[:1].upper() + phrase[1:]
    return Notebook(
        store=notebook.store,
        buckets=list(notebook.buckets),
        macros=[Macro(humanize(m.name), m.text) for m in notebook.macros],
        facts=list(notebook.facts),
        notes=list(notebook.notes),
    )


def load(path: Path) -> Notebook:
    """Parse a notebook markdown file back into a Notebook. Tolerant of hand edits: unknown sections
    are ignored, a bullet without a `[tag]` defaults its provenance, blank lines collapse."""
    if not path.exists():
        raise FileNotFoundError(f"no notebook at {path}")
    return loads(path.read_text(encoding="utf-8"))


def loads(text: str) -> Notebook:
    store = "Store"
    buckets: list[Bucket] = []
    macros: list[Macro] = []
    facts: list[Fact] = []
    notes: list[Note] = []

    section: str | None = None
    sub_name: str | None = None
    sub_body: list[str] = []

    def flush_sub() -> None:
        nonlocal sub_name, sub_body
        if sub_name is None:
            return
        if section == _SEC_BUCKETS:
            # Pull the stakes marker out of the body so it gates routing without leaking into the cliff.
            stakes = "high" if any(ln.strip() == _STAKES_HIGH_MARKER for ln in sub_body) else "normal"
            body = "\n".join(ln for ln in sub_body if ln.strip() != _STAKES_HIGH_MARKER).strip()
            buckets.append(Bucket(sub_name, body, stakes))
        elif section == _SEC_MACROS:
            macros.append(Macro(sub_name, "\n".join(sub_body).strip()))
        sub_name, sub_body = None, []

    for line in text.splitlines():
        if line.startswith("# ") and not line.startswith("## "):
            store = line[2:].split(" — ")[0].strip()
            continue
        if m := _H2.match(line):
            flush_sub()
            section = m.group(1)
            continue
        if m := _H3.match(line):
            flush_sub()
            sub_name = m.group(1)
            continue
        if section in (_SEC_FACTS, _SEC_NOTES) and (bm := _BULLET.match(line)):
            tag = bm.group("tag")
            body = bm.group("text").strip()
            if not body:
                continue
            if section == _SEC_FACTS:
                facts.append(Fact(body, tag if tag in PROVENANCE else "stated"))
            else:
                notes.append(Note(body, tag if tag in PROVENANCE else "observed"))
            continue
        if sub_name is not None:
            sub_body.append(line)

    flush_sub()
    return Notebook(store=store, buckets=buckets, macros=macros, facts=facts, notes=notes)
