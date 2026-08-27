"""The model interface and its implementations.

`Model` is the seam the harness owns: swapping a cheaper or stronger model is config, and
unit tests run against `FakeModel` with zero network. Every call
forces a tool so the result is validated JSON (no brittle parsing), and the cached system
prefix is passed straight through so caching "just works" when the prefix is byte-identical.
"""

from __future__ import annotations

import base64
import json
import logging
import os
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable, Protocol

from dotenv import load_dotenv

from bean.paths import usage_path

load_dotenv()  # project-root .env; harmless if absent (unit tests use FakeModel)

log = logging.getLogger("bean.llm")

# Current model ids (see ~/.claude/ai_api_best_practices.md for pricing / cache thresholds).
CLASSIFY_MODEL = "claude-haiku-4-5"
DRAFT_MODEL = "claude-sonnet-4-6"
JUDGE_MODEL = "claude-opus-4-8"
# Teach-ahead sample emails are display-only practice fodder — never persisted, never a grounding
# source — so they run on the cheapest tier, not the (pricier) draft model.
SAMPLE_MODEL = CLASSIFY_MODEL


class OutOfCreditsError(Exception):
    """The Anthropic account is out of usage credits (HTTP 400 'credit balance is too low').

    Raised at the one live seam so every model-spending path can surface a 'top up Bean's monies'
    message instead of failing silently. Deliberately NOT a retryable error: unlike 429 rate limits
    or 529 overload (which the SDK retries), no amount of retrying makes credits appear — the human
    has to top up. Callers map it to a distinct 402 rather than a generic 5xx."""


def _is_out_of_credits(exc: Exception) -> bool:
    # The credit-exhaustion error shares HTTP 400 with ordinary malformed-request errors, so match
    # on the message text ("Your credit balance is too low to access the Anthropic API…") rather
    # than the status code alone. str() of an anthropic APIStatusError includes that body.
    return "credit balance" in str(exc).lower()


@dataclass
class Usage:
    input_tokens: int = 0
    output_tokens: int = 0
    cache_creation_input_tokens: int = 0
    cache_read_input_tokens: int = 0


@dataclass
class LLMResult:
    data: dict  # the forced tool_use input — already schema-validated by the API
    usage: Usage


class Model(Protocol):
    name: str

    def structured(
        self,
        *,
        system: list[dict],
        user: str,
        tool: dict,
        images: list[str] | None = ...,
        max_tokens: int = ...,
    ) -> LLMResult: ...


def _image_block(path: str) -> dict:
    raw = Path(path).read_bytes()
    media = "image/png" if path.lower().endswith(".png") else "image/jpeg"
    return {
        "type": "image",
        "source": {"type": "base64", "media_type": media, "data": base64.standard_b64encode(raw).decode()},
    }


def _log_usage(model: str, usage: Usage, purpose: str, customer: str | None = None,
               cache_ttl: str | None = None) -> None:
    """Append one line to the per-customer usage log — tokens + cost signal per REAL model call, so
    Bean's spend is inspectable over time (GET /api/learning's read-surface sibling). Called only
    from the live `AnthropicModel` path (FakeModel has no real token counts, so it never lands here),
    and never fatal: a logging failure must not break a triage — observability can't take down the
    product, so an OSError is swallowed. `purpose` is the forced tool's name (classify/assess/… ),
    which the call already knows — no end-to-end plumbing needed.

    `customer` names whose volume this line lands on. It used to be absent, so every line landed
    under `default_customer()` regardless of whose request triggered the call — harmless while
    `bean/server.py` set one `CUSTOMER` constant per process, and silently wrong the moment one
    process served two tenants, which is exactly when a cost report starts being read.

    WHY THE CONSTRUCTOR AND NOT `Model.structured()`: the note that stood here said to fix this by
    widening the protocol. Widening it is worse. The customer is a property of the CLIENT, not of
    the call — every call an adapter makes belongs to the same tenant for the adapter's whole life —
    so a per-call parameter would make every call site restate a constant, and it would drag
    `FakeModel` (which never writes here, having no real token counts) into carrying a field it has
    no use for. So `AnthropicModel`/`ModelAdapter` take it at construction and pass it down, and
    `structured()` is untouched. The singleton cache is keyed on `(model_id, customer)` for the same
    reason — two tenants sharing a process must not share a client that knows only one of their
    names.

    Still NOT a module-level mutable "current customer": that was the right warning and it stands.
    It would move this bug into every log rather than fix it. `None` keeps the old implicit default,
    which is correct for the single-tenant path and for tests."""
    # `cache_write` is not decoration. Total prompt = cache_creation + cache_read + input_tokens, so
    # without it you cannot tell "the prefix was never cached" from "it cached and then expired" —
    # both look like cache_read: 0. Bean shipped 28 production calls with a 0% hit rate and no way to
    # see why. Below a model's minimum cacheable prefix the API silently declines to cache (Haiku 4.5
    # wants 4,096 tokens; Bean's prefix is ~2,262), and it reports that by leaving this field at 0.
    #
    # `cache_ttl` is what makes the write PRICEABLE. Anthropic bills a cache write at 1.25x input
    # for the 5-minute TTL and 2.0x for the 1-hour one, and the response says nothing about which
    # was used — only the request knew. bean/usage.py priced every write at 1.25x on the grounds
    # that Bean had only ever requested the default, and left a note that this constant must become
    # per-line data the day that changed. bean/adapter.py now requests 1h, so it is that day.
    # Recording it per line (rather than flipping the constant) keeps the ~1,600 historical lines
    # priced correctly instead of retroactively marking them all up 1.6x. Absent ⇒ 5-minute, which
    # is true of every line written before this field existed.
    line = {
        "ts": datetime.now(timezone.utc).isoformat(),
        "model": model,
        "input_tokens": usage.input_tokens,
        "output_tokens": usage.output_tokens,
        "cache_read": usage.cache_read_input_tokens,
        "cache_write": usage.cache_creation_input_tokens,
        "purpose": purpose,
    }
    if cache_ttl:
        line["cache_ttl"] = cache_ttl
    try:
        path = usage_path(customer)
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(line, sort_keys=True) + "\n")
    except OSError as exc:
        log.warning("could not write usage log: %s", exc)


def _tool_input(resp, tool_name: str) -> dict:
    for block in resp.content:
        if getattr(block, "type", None) == "tool_use" and block.name == tool_name:
            return dict(block.input)
    raise ValueError(f"model did not call tool {tool_name!r}; got {[b.type for b in resp.content]}")


class AnthropicModel:
    """Live Anthropic client. One per model id; the SDK client is cheap to hold."""

    def __init__(self, model: str, temperature: float | None = None, *, customer: str | None = None):
        import anthropic  # imported lazily so unit tests need no SDK/key

        self.name = model
        # Whose volume this client's usage lines land on. See _log_usage for why it lives here
        # rather than on structured().
        self.customer = customer
        # None → omit the param → the SDK default (1.0), which production uses (natural drafts).
        # The eval sets temperature=0 for a DETERMINISTIC run so a single-run score is a real gate,
        # not a sample of a temperature-1.0 model (where one case flipping HIGH↔LOW is just noise).
        self.temperature = temperature
        self._client = anthropic.Anthropic()

    def structured(self, *, system, user, tool, images=None, max_tokens=1500) -> LLMResult:
        import anthropic  # lazy, like __init__ — the SDK is only needed on the live path

        content: list[dict] = [_image_block(p) for p in (images or [])]
        content.append({"type": "text", "text": user})
        kwargs: dict = {}
        if self.temperature is not None:
            kwargs["temperature"] = self.temperature
        try:
            resp = self._client.messages.create(
                model=self.name,
                max_tokens=max_tokens,
                system=system,
                tools=[tool],
                tool_choice={"type": "tool", "name": tool["name"]},
                messages=[{"role": "user", "content": content}],
                **kwargs,
            )
        # Translate only the billing case into our typed error; everything else (429/529 the SDK
        # already retried, malformed 400s, connection errors) propagates unchanged.
        except anthropic.APIStatusError as exc:
            if _is_out_of_credits(exc):
                raise OutOfCreditsError(str(exc)) from exc
            raise
        u = resp.usage
        result = LLMResult(
            data=_tool_input(resp, tool["name"]),
            usage=Usage(
                input_tokens=getattr(u, "input_tokens", 0) or 0,
                output_tokens=getattr(u, "output_tokens", 0) or 0,
                cache_creation_input_tokens=getattr(u, "cache_creation_input_tokens", 0) or 0,
                cache_read_input_tokens=getattr(u, "cache_read_input_tokens", 0) or 0,
            ),
        )
        _log_usage(self.name, result.usage, tool["name"], self.customer)
        return result


class FakeModel:
    """Deterministic, offline model for unit tests. `responses` maps a tool name to either a
    dict (returned every call), a list (popped FIFO), or a callable(user_text) -> dict."""

    def __init__(self, responses: dict[str, dict | list | Callable[[str], dict]], name: str = "fake"):
        self.name = name
        self._responses = responses
        self.calls: list[dict] = []
        self.images: list[list[str]] = []  # the `images=` each call received (Phase 3 image-path plumbing)

    def structured(self, *, system, user, tool, images=None, max_tokens=1500) -> LLMResult:
        self.calls.append({"tool": tool["name"], "user": user, "system": system})
        self.images.append(list(images or []))
        spec = self._responses[tool["name"]]
        if isinstance(spec, list):
            data = spec.pop(0)
        elif callable(spec):
            data = spec(user)
        else:
            data = spec
        return LLMResult(data=dict(data), usage=Usage())


# Lazily-built singletons so the live default path doesn't spin up a client until used.
#
# Keyed on (model_id, customer), not model id alone. A client carries the tenant its usage is billed
# to (_log_usage), so a cache keyed only by model would hand tenant B the client built for tenant A
# and quietly file B's spend under A's name — the exact silent misattribution the customer argument
# exists to close. Two tenants in one process cost two clients; an SDK client is cheap to hold.
_LIVE: dict[tuple[str, str | None], AnthropicModel] = {}


def live_model(model_id: str, customer: str | None = None) -> AnthropicModel:
    key = (model_id, customer)
    if key not in _LIVE:
        _LIVE[key] = AnthropicModel(model_id, customer=customer)
    return _LIVE[key]


def has_api_key() -> bool:
    return bool(os.getenv("ANTHROPIC_API_KEY"))
