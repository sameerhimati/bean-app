"""Offline tests for bean/adapter.py — the cache-control marking (the part that doesn't need a key).

The live call needs the SDK; but WHERE the cache breakpoint lands is pure logic we can pin without a
network. llm.py never marked one (0% hit rate); the adapter must mark the last system block so the
notebook prefix caches, and must be idempotent so re-marking an already-marked block is a no-op.
"""

from __future__ import annotations

from bean.adapter import _with_cache


def test_marks_last_system_block():
    blocks = [{"type": "text", "text": "instructions"}, {"type": "text", "text": "notebook"}]
    out = _with_cache(blocks)
    assert "cache_control" not in out[0], "only the last block is the breakpoint"
    assert out[-1]["cache_control"] == {"type": "ephemeral"}


def test_does_not_mutate_the_input():
    blocks = [{"type": "text", "text": "notebook"}]
    _with_cache(blocks)
    assert "cache_control" not in blocks[0], "must not mutate the caller's list"


def test_idempotent_when_already_marked():
    blocks = [{"type": "text", "text": "notebook", "cache_control": {"type": "ephemeral"}}]
    out = _with_cache(blocks)
    assert out[-1]["cache_control"] == {"type": "ephemeral"}


def test_empty_system_is_untouched():
    assert _with_cache([]) == []
