"""Offline tests for the token/cost usage log (bean/llm.py `_log_usage` + bean/paths.usage_path).

The live `AnthropicModel` writes one line per real model call; `FakeModel` (all unit tests) has no
real token counts and must NEVER write. There's no clean way to exercise the live write without an
API call, so we test the write function directly with a synthetic `Usage` and a tmp data root, and
prove FakeModel stays silent. A logging failure must be non-fatal (observability can't take Bean
down), so we also prove a broken path is swallowed.
"""

from __future__ import annotations

import json

from bean import llm
from bean.llm import FakeModel, Usage, _log_usage
from bean.paths import usage_path


def test_log_usage_writes_one_line(tmp_path, monkeypatch):
    monkeypatch.setenv("BEAN_DATA_DIR", str(tmp_path))
    _log_usage("claude-sonnet-4-6", Usage(input_tokens=120, output_tokens=30, cache_read_input_tokens=90), "classify")

    path = usage_path()
    rows = [json.loads(l) for l in path.read_text().splitlines() if l.strip()]
    assert len(rows) == 1
    row = rows[0]
    assert row["model"] == "claude-sonnet-4-6"
    assert row["input_tokens"] == 120 and row["output_tokens"] == 30
    assert row["cache_read"] == 90 and row["purpose"] == "classify"
    assert row["ts"]  # server-stamped ISO timestamp


def test_fake_model_does_not_write_usage(tmp_path, monkeypatch):
    # FakeModel has no real token counts — the usage log must stay untouched on the offline path.
    monkeypatch.setenv("BEAN_DATA_DIR", str(tmp_path))
    model = FakeModel({"classify": {"category": "Order Status"}})
    model.structured(system=[], user="where is my order", tool={"name": "classify"})
    assert not usage_path().exists()


def test_log_usage_is_non_fatal(tmp_path, monkeypatch):
    # A logging failure must never propagate — a broken path is swallowed, not raised.
    def boom(customer=None) -> None:
        raise OSError("volume gone")

    monkeypatch.setattr(llm, "usage_path", boom)
    _log_usage("claude-sonnet-4-6", Usage(), "assess")  # must not raise


def test_log_usage_records_cache_writes_not_just_reads(tmp_path, monkeypatch):
    """cache_read alone cannot distinguish "never cached" from "cached, then expired" — both are 0.
    Total prompt = cache_creation + cache_read + input_tokens, so the write count is what tells you
    whether `cache_control` was honoured at all. Bean ran 28 prod calls at a 0% hit rate without
    being able to answer that question."""
    monkeypatch.setenv("BEAN_DATA_DIR", str(tmp_path))
    _log_usage("claude-sonnet-4-6", Usage(input_tokens=120, output_tokens=30,
                                          cache_creation_input_tokens=2262,
                                          cache_read_input_tokens=0), "route")
    line = json.loads(usage_path().read_text().splitlines()[0])
    assert line["cache_write"] == 2262, "a cache write must be visible, or 0% hit rate is unexplainable"
    assert line["cache_read"] == 0
    assert line["input_tokens"] == 120


def test_cache_ttl_is_recorded_so_the_write_can_be_priced(tmp_path, monkeypatch):
    """The response never says which TTL a write used — only the request knew. Without this field
    bean/usage.py has to assume one rate for the whole log, and the log now holds both: ~1,600
    historical writes billed at 1.25x and every new one at 2.0x."""
    monkeypatch.setenv("BEAN_DATA_DIR", str(tmp_path))
    _log_usage("claude-sonnet-4-6", Usage(cache_creation_input_tokens=2262), "draft", None, "1h")
    assert json.loads(usage_path().read_text())["cache_ttl"] == "1h"


def test_a_line_with_no_ttl_stays_absent_rather_than_guessing(tmp_path, monkeypatch):
    """An uncached call writes no cache, so stamping a TTL on it would price a write that never
    happened. Absence is also what every pre-existing line looks like, and it means 5-minute."""
    monkeypatch.setenv("BEAN_DATA_DIR", str(tmp_path))
    _log_usage("claude-sonnet-4-6", Usage(input_tokens=10), "draft")
    assert "cache_ttl" not in json.loads(usage_path().read_text())
