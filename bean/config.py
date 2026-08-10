"""Config-as-data: the injected object the engine reads instead of `bean.fixtures` constants.

Phase 1 turns the operator's editable data (categories + templates + knowledge docs + settings)
into a `Config` dataclass the routing loop is fed, so swapping in their real data is a file
swap, not a code edit. The fixtures remain the *default* — `Config.from_fixtures()` and the
no-file `load_config()` path both reproduce them, and the system prefix they build is
byte-identical to the pre-refactor one (the cache invariant; guarded by test_config_snapshot).

On-disk shape mirrors `cli.build_config()` (camelCase, the admin UI's `window.CONFIG`):

    {
      "categories":  [{"name", "description", "template": str|null, "alwaysEscalate": bool}],
      "knowledgeDocs": [{"title", "body"}],
      "settings": {"shopifyStore": str}
    }

A category with no template (or `alwaysEscalate: true`) routes to FLAG — a business/judgment
call Bean never auto-drafts.

`load_config()` distinguishes ABSENT from CORRUPT, and that distinction is the fix for the
worst bug this file ever had. A *missing* file is a brand-new customer → fixtures default + a
warning (safe). A file that *exists but can't be parsed* is the operator's real config shredded by
a mid-write crash on the volume — serving fixtures there is catastrophic: Bean would answer from
demo templates and the next save would write those demos back over their taught config, erasing
the moat behind one warning line. So a corrupt live file is NEVER impersonated by fixtures: we
recover from the rolling `.bak` written by the last atomic save, or raise `ConfigCorruptError`.
The old docstring promised "never raises" — that promise WAS the bug; loud failure beats silent
data-loss. Writes go through `bean.store.write_json_atomic` (re-exported here) so there is no
truncation window to corrupt the file in the first place.
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field
from pathlib import Path

from bean.fixtures import (
    CATEGORIES,
    CATEGORY_DESCRIPTIONS,
    KNOWLEDGE_DOCS,
    SHOPIFY_STORE,
    TEMPLATES,
)
from bean.store import write_json_atomic  # re-exported: the atomic write path for callers (server.py)

log = logging.getLogger("bean.config")

__all__ = ["Config", "Category", "load_config", "prefix_args",
           "write_json_atomic", "ConfigCorruptError"]


class ConfigCorruptError(RuntimeError):
    """Raised by `load_config` when the config file EXISTS but is unparseable and no usable `.bak`
    backup is available. Deliberately NOT a subclass of anything the loop swallows: a corrupt live
    config must surface (fail the request / crash loudly) rather than be papered over with demo
    fixtures — see this module's docstring for why that silent fallback was the moat-eating bug."""


@dataclass(frozen=True)
class Category:
    name: str
    description: str
    template: str | None  # None ⇒ no auto-draft for this field
    always_escalate: bool = False


@dataclass(frozen=True)
class Config:
    categories: tuple[Category, ...]
    knowledge_docs: dict[str, str]
    settings: dict[str, str]
    # Triage-gate rules ({"alwaysReply": [...], "alwaysFile": [...]}, case-insensitive substrings
    # matched against sender + subject — see bean/gate.py). Default empty ⇒ omitted on disk and
    # the gate goes straight to its classifier; edited in the admin "What I handle" tab.
    gate: dict = field(default_factory=dict)
    # Bean's pending proposals (starter templates for untaught leaves, new nodes) awaiting the
    # operator's approve/edit/dismiss in "What I handle". Plain dicts, schema-free in v1; approving
    # mutates the tree (provenance "bean") and removes the proposal. Omitted on disk when empty.
    proposals: tuple = ()

    # ---- the args the engine actually consumes -------------------------------------------

    def category_names(self) -> list[str]:
        return [c.name for c in self.categories]

    def descriptions(self) -> dict[str, str]:
        return {c.name: c.description for c in self.categories}

    def templates(self) -> dict[str, str]:
        """Only categories that carry a template; absence ⇒ FLAG (matches fixtures behavior)."""
        return {c.name: c.template for c in self.categories if c.template is not None}

    def category(self, name: str) -> Category | None:
        return next((c for c in self.categories if c.name == name), None)

    @classmethod
    def from_fixtures(cls) -> "Config":
        """The default Config — byte-identical-prefix equivalent of the old module constants."""
        categories = tuple(
            Category(
                name=name,
                description=CATEGORY_DESCRIPTIONS.get(name, ""),
                template=TEMPLATES.get(name),
                always_escalate=name not in TEMPLATES,
            )
            for name in CATEGORIES
        )
        return cls(
            categories=categories,
            knowledge_docs=dict(KNOWLEDGE_DOCS),
            settings={"shopifyStore": SHOPIFY_STORE},
        )

    @classmethod
    def from_dict(cls, data: dict) -> "Config":
        """Parse the on-disk (camelCase) shape into a Config.

        `.get()`-based on purpose: tolerant of missing keys AND of keys it no longer knows. Every
        live config.json on the volume still carries the routing tree's `tree` key (and per-doc
        `image` paths) from the engine that was deleted in the notebook cutover — they are read
        past and dropped, so an old file keeps loading and the next save simply stops writing them.
        """
        categories = tuple(
            Category(
                name=c["name"],
                description=c.get("description", ""),
                template=c.get("template"),
                always_escalate=bool(c.get("alwaysEscalate", False)),
            )
            for c in data.get("categories", [])
        )
        docs = data.get("knowledgeDocs", [])
        knowledge_docs = {d["title"]: d["body"] for d in docs}
        settings = dict(data.get("settings", {}))
        return cls(
            categories=categories,
            knowledge_docs=knowledge_docs,
            settings=settings,
            gate=dict(data.get("gate", {})),
            proposals=tuple(data.get("proposals", [])),
        )

    def to_dict(self) -> dict:
        """Deterministic on-disk shape (sorted docs) — mirrors cli.build_config()."""
        return {
            "categories": [
                {
                    "name": c.name,
                    "description": c.description,
                    "template": c.template,
                    "alwaysEscalate": c.always_escalate,
                }
                for c in self.categories
            ],
            "knowledgeDocs": [
                {"title": k, "body": v} for k, v in sorted(self.knowledge_docs.items())
            ],
            "settings": dict(self.settings),
            **({"gate": dict(self.gate)} if self.gate else {}),
            **({"proposals": list(self.proposals)} if self.proposals else {}),
        }


def prefix_args(config: Config, knowledge_docs: dict[str, str]) -> dict:
    """Derive `build_system_prefix(**kwargs)` from a Config.

    `knowledge_docs` is passed explicitly (from the KnowledgeStore) so the retrieval seam
    stays the source of truth for the cached doc block — the store and config carry the same
    docs by construction, but the store is what feeds the prefix today and embeddings later.
    """
    return {
        "categories": config.category_names(),
        "category_descriptions": config.descriptions(),
        "templates": config.templates(),
        "knowledge_docs": knowledge_docs,
    }


def _try_parse(p: Path) -> Config | None:
    """Parse one config file, or return None if it's unreadable or misshapen.

    None means "this file failed, try the next fallback" — it is deliberately NOT "use fixtures".
    Only `load_config` decides what a None means for a given path (a corrupt live file is very
    different from a corrupt backup)."""
    try:
        data = json.loads(p.read_text())
        return Config.from_dict(data)
    # Not a swallow: None means "this candidate failed, try the next fallback". `load_config` is
    # what decides, and it raises ConfigCorruptError when no candidate works — it never serves
    # fixtures as the customer's config.
    except (json.JSONDecodeError, OSError, KeyError, TypeError) as exc:
        log.warning("config file %s is unreadable (%s)", p, exc)
        return None


def load_config(path: str | Path | None) -> Config:
    """Load a Config from a JSON file.

    ABSENT vs CORRUPT — the two cases the old "never raises" code fatally conflated:
      - None path or a file that does not exist → brand-new customer → fixtures default + warning.
      - Present but unparseable → NEVER fixtures. Recover from the `.bak` sibling written by the
        last atomic save (log at ERROR that the live file was corrupt), or raise `ConfigCorruptError`.

    Raises `ConfigCorruptError` when the live file exists, can't be parsed, and no usable backup is
    found. This is intentional: impersonating the customer's config with demo fixtures — then
    overwriting her real data on the next save — is the exact silent-success failure this change
    eradicates. A loud raise is recoverable; a quiet fixtures swap is not.
    """
    if path is None:
        return Config.from_fixtures()

    p = Path(path)
    if not p.exists():
        # ERROR, not WARNING. For a genuinely new tenant this is expected — but it is also what a
        # lost/remounted volume looks like for an EXISTING one, and the very next save persists the
        # demo store as their config. That happened to a real live tenant (see bean/fixtures.py);
        # it went unseen because it whispered. `/healthz` reports `config_is_demo_default` for the
        # same reason.
        log.error(
            "config file %s not found — serving the DEMO default (Maple & Moss). Correct for a new "
            "tenant; for an existing one this means their config is GONE and the next save will "
            "overwrite it with demo data.", p,
        )
        return Config.from_fixtures()

    cfg = _try_parse(p)
    if cfg is not None:
        return cfg

    # The live file exists but couldn't be parsed. Do NOT fall back to fixtures — reach for the
    # rolling backup that write_json_atomic leaves on every save.
    bak = p.with_suffix(p.suffix + ".bak")
    if bak.exists():
        recovered = _try_parse(bak)
        if recovered is not None:
            log.error(
                "config file %s was CORRUPT — recovered from backup %s. The live file was NOT "
                "overwritten with fixtures; the next save will rewrite a clean copy.", p, bak,
            )
            return recovered

    raise ConfigCorruptError(
        f"config file {p} exists but could not be parsed, and no usable backup ({bak}) was found "
        f"— refusing to serve demo fixtures as the customer's config (that would erase her data on "
        f"the next save)"
    )
