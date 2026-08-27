"""The model adapter — the one file a provider swap touches.

`bean/llm.py` grew the live Anthropic client with the key sourced implicitly from the process env
and clients cached as module singletons by model id. The rebuild needs two things that shape can't
give: a **per-tenant API key** (BYOK, §8 — the key is a constructor parameter, not an env global)
and **prompt caching on the notebook prefix** (llm.py never wrote a `cache_control` marker, so its
~2.2 KB prefix cached 0% — see its `_log_usage` note). `ModelAdapter` is that client.

It implements the same `Model` protocol as `AnthropicModel`/`FakeModel` (`structured(...)`), so the
engine's unit tests still run offline against `FakeModel` with zero network — the seam is preserved,
only the live implementation changes. The shared plumbing (usage logging, the out-of-credits
translation, the image/tool-extract helpers, `Usage`/`LLMResult`) is reused from `bean/llm.py`
rather than copied, so the two live paths can't drift.
"""

from __future__ import annotations

import copy

from bean.llm import (
    LLMResult,
    OutOfCreditsError,
    Usage,
    _image_block,
    _is_out_of_credits,
    _log_usage,
    _tool_input,
)

# The notebook prefix recurs on every email, so a warm call reads it instead of re-billing it
# (verify via cache_read_input_tokens).
#
# ONE HOUR, not the default five minutes, and the default was measured to be a NET LOSS. Her mail
# does not arrive in bursts: the median gap between drafts is 28 minutes, 67% of gaps are under an
# hour and only 16% are under five. So at the 5-minute TTL the prefix expired between almost every
# pair of emails and Bean paid the 1.25x write over and over without ever reading it back — 68
# writes against 12 reads across 80 August drafts, $2.17 actual versus $2.10 with caching removed
# entirely. A 1-hour TTL costs 2x on the write and simulates to $1.85, because it converts that
# 67% of gaps from a re-write into a read.
#
# The lesson worth keeping is the general one: a cache TTL is a bet about the ARRIVAL PATTERN, not
# a tuning knob. Nobody measured the gap distribution before picking the default, and the default
# happened to be wrong for a one-operator support inbox in a way that no error could ever surface —
# it just cost money quietly. If Bean ever serves a high-volume tenant whose mail really does
# arrive in bursts, re-measure rather than assume this constant transfers.
_CACHE_CONTROL = {"type": "ephemeral", "ttl": "1h"}


def _with_cache(system: list[dict]) -> list[dict]:
    """Mark the LAST system block with cache_control, caching tools + the whole system prefix up to
    it (the API's cache hierarchy is tools → system → messages). The engine puts the stable notebook
    prefix in `system` and the per-email content in the user message, so this caches exactly the part
    that repeats. A block that already carries cache_control is left as-is (idempotent)."""
    if not system:
        return system
    blocks = [copy.deepcopy(b) for b in system]
    if "cache_control" not in blocks[-1]:
        blocks[-1]["cache_control"] = _CACHE_CONTROL
    return blocks


class ModelAdapter:
    """Live Anthropic client with a per-instance key and a cached system prefix.

    `api_key=None` falls back to the SDK's env-var lookup (single-tenant today, the operator's key
    in the process env); a real key makes it per-tenant. `temperature=0` gives the deterministic run the
    replay verifier grades against; `None` omits the param (SDK default 1.0) for natural production
    drafts."""

    def __init__(self, model: str, *, api_key: str | None = None, temperature: float | None = None,
                 customer: str | None = None):
        import anthropic  # lazy, like llm.AnthropicModel — no SDK/key needed for FakeModel tests

        self.name = model
        self.temperature = temperature
        # Whose volume this adapter's usage lines land on (llm._log_usage). Per-instance, not
        # per-call: a tenant's adapter serves only that tenant for its whole life.
        self.customer = customer
        # Running usage across this adapter's calls — lets a caller (the replay verifier) report
        # spend and, crucially, confirm the notebook prefix actually cached (cache_read > 0).
        self.total = Usage()
        self._client = anthropic.Anthropic(api_key=api_key) if api_key else anthropic.Anthropic()

    def structured(
        self, *, system, user, tool, images=None, max_tokens=1500, cache_system: bool = True
    ) -> LLMResult:
        import anthropic  # lazy

        content: list[dict] = [_image_block(p) for p in (images or [])]
        content.append({"type": "text", "text": user})
        kwargs: dict = {}
        if self.temperature is not None:
            kwargs["temperature"] = self.temperature
        try:
            resp = self._client.messages.create(
                model=self.name,
                max_tokens=max_tokens,
                system=_with_cache(system) if cache_system else system,
                tools=[tool],
                tool_choice={"type": "tool", "name": tool["name"]},
                messages=[{"role": "user", "content": content}],
                **kwargs,
            )
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
        self.total = Usage(
            input_tokens=self.total.input_tokens + result.usage.input_tokens,
            output_tokens=self.total.output_tokens + result.usage.output_tokens,
            cache_creation_input_tokens=self.total.cache_creation_input_tokens + result.usage.cache_creation_input_tokens,
            cache_read_input_tokens=self.total.cache_read_input_tokens + result.usage.cache_read_input_tokens,
        )
        # Report the TTL only when caching was actually requested — a `cache_system=False` call
        # writes no cache, and stamping a TTL on it would price a write that never happened.
        _log_usage(self.name, result.usage, tool["name"], self.customer,
                   _CACHE_CONTROL.get("ttl", "5m") if cache_system else None)
        return result


__all__ = ["ModelAdapter", "OutOfCreditsError"]
