"""Tests for bean/usage.py — the cost/cache report over usage.jsonl (offline, synthetic lines only).

Covers the cost math per model, the opus 0.5x cache-read exception (every other model is 0.1x), the
unknown-model case (must flag, never silently cost $0), and tolerance for pre-cache_write log lines.
"""

from __future__ import annotations

import json

import pytest

from bean.usage import (
    CACHE_READ_MULTIPLIER,
    CACHE_READ_MULTIPLIER_OPUS,
    CACHE_WRITE_MULTIPLIER,
    PRICES,
    aggregate,
    call_cost,
    render_table,
    report,
    totalize,
)


def _line(model="claude-sonnet-4-6", purpose="classify", input_tokens=0, output_tokens=0,
          cache_read=0, cache_write=0, drop_cache_write=False) -> dict:
    d = {
        "ts": "2026-07-14T00:00:00+00:00",
        "model": model,
        "purpose": purpose,
        "input_tokens": input_tokens,
        "output_tokens": output_tokens,
        "cache_read": cache_read,
        "cache_write": cache_write,
    }
    if drop_cache_write:
        del d["cache_write"]
    return d


# ---- cost math --------------------------------------------------------------------------------


def test_call_cost_sonnet_no_cache():
    # 1000 input @ $3/M + 500 output @ $15/M = $0.003 + $0.0075
    cost = call_cost("claude-sonnet-4-6", input_tokens=1000, output_tokens=500, cache_read=0, cache_write=0)
    assert cost == pytest.approx(1000 * 3.00 / 1_000_000 + 500 * 15.00 / 1_000_000)


def test_call_cost_haiku():
    cost = call_cost("claude-haiku-4-5", input_tokens=2000, output_tokens=100, cache_read=0, cache_write=0)
    assert cost == pytest.approx(2000 * 1.00 / 1_000_000 + 100 * 5.00 / 1_000_000)


def test_call_cost_includes_cache_write_at_input_price():
    # cache write is priced off the model's INPUT price at CACHE_WRITE_MULTIPLIER (1.25x), not a
    # separate rate.
    input_price = PRICES["claude-sonnet-4-6"][0]
    cost = call_cost("claude-sonnet-4-6", input_tokens=0, output_tokens=0, cache_read=0, cache_write=2262)
    assert cost == pytest.approx(2262 * input_price * CACHE_WRITE_MULTIPLIER / 1_000_000)


def test_call_cost_cache_read_standard_model_is_point_one_x():
    input_price = PRICES["claude-sonnet-4-6"][0]
    cost = call_cost("claude-sonnet-4-6", input_tokens=0, output_tokens=0, cache_read=1000, cache_write=0)
    assert cost == pytest.approx(1000 * input_price * CACHE_READ_MULTIPLIER / 1_000_000)


def test_call_cost_cache_read_opus_is_point_five_x_not_point_one_x():
    # The verified exception: opus prices a cache read at 0.5x its input price, 5x every other model.
    input_price = PRICES["claude-opus-4-8"][0]
    cost = call_cost("claude-opus-4-8", input_tokens=0, output_tokens=0, cache_read=1000, cache_write=0)
    assert cost == pytest.approx(1000 * input_price * CACHE_READ_MULTIPLIER_OPUS / 1_000_000)
    assert CACHE_READ_MULTIPLIER_OPUS != CACHE_READ_MULTIPLIER


def test_call_cost_unknown_model_returns_none_not_zero():
    # A silently-wrong $0 is worse than no number — an unpriced model must be visibly absent, not
    # invisibly free.
    assert call_cost("claude-nonexistent-9", input_tokens=1000, output_tokens=1000, cache_read=0, cache_write=0) is None


# ---- aggregation --------------------------------------------------------------------------------


def test_aggregate_groups_by_model_and_purpose_and_sums_tokens():
    lines = [
        _line(model="claude-sonnet-4-6", purpose="classify", input_tokens=100, output_tokens=10),
        _line(model="claude-sonnet-4-6", purpose="classify", input_tokens=200, output_tokens=20),
        _line(model="claude-opus-4-8", purpose="assess", input_tokens=300, output_tokens=30),
    ]
    rows = aggregate(lines)
    assert set(rows) == {("claude-sonnet-4-6", "classify"), ("claude-opus-4-8", "assess")}
    sonnet = rows[("claude-sonnet-4-6", "classify")]
    assert sonnet.calls == 2
    assert sonnet.input_tokens == 300 and sonnet.output_tokens == 30
    assert sonnet.avg_input == 150 and sonnet.avg_output == 15


def test_aggregate_cache_hit_rate_is_fraction_of_calls_with_cache_read():
    lines = [
        _line(cache_read=500),
        _line(cache_read=0),
        _line(cache_read=0),
        _line(cache_read=200),
    ]
    row = aggregate(lines)[("claude-sonnet-4-6", "classify")]
    assert row.cache_hit_calls == 2
    assert row.cache_hit_rate == 0.5


def test_aggregate_tolerates_old_lines_missing_cache_write():
    # Real logs predate the cache_write field; a KeyError here would break the report on day-one data.
    lines = [_line(cache_write=999), _line(drop_cache_write=True)]
    row = aggregate(lines)[("claude-sonnet-4-6", "classify")]
    assert row.cache_write == 999  # the dropped line contributed 0, not an error
    assert row.calls == 2


def test_aggregate_unpriced_model_is_flagged_not_costed():
    lines = [_line(model="claude-mystery-1", input_tokens=1000, output_tokens=1000)]
    row = aggregate(lines)[("claude-mystery-1", "classify")]
    assert row.unpriced_calls == 1
    assert row.total_cost == 0.0  # nothing priced, so nothing summed — not a fabricated $0 total


def test_aggregate_mixed_priced_and_unpriced_keeps_them_distinguishable():
    lines = [
        _line(model="claude-sonnet-4-6", input_tokens=1000, output_tokens=0),
        _line(model="claude-sonnet-4-6", input_tokens=1000, output_tokens=0),
    ]
    row = aggregate(lines)[("claude-sonnet-4-6", "classify")]
    assert row.unpriced_calls == 0
    assert row.total_cost > 0


def test_totalize_sums_every_row():
    lines = [
        _line(model="claude-sonnet-4-6", purpose="classify", input_tokens=100, cache_read=10),
        _line(model="claude-opus-4-8", purpose="assess", input_tokens=200, cache_read=20),
    ]
    rows = aggregate(lines)
    total = totalize(rows)
    assert total.calls == 2
    assert total.input_tokens == 300
    assert total.cache_read == 30
    assert total.cache_hit_calls == 2


# ---- report() reads the on-disk log, table renders without crashing ---------------------------


def test_report_reads_jsonl_from_path(tmp_path):
    path = tmp_path / "usage.jsonl"
    path.write_text("\n".join(json.dumps(_line(input_tokens=n)) for n in (100, 200)) + "\n")
    rows = report(path=path)
    assert rows[("claude-sonnet-4-6", "classify")].calls == 2


def test_report_missing_file_is_empty(tmp_path):
    assert report(path=tmp_path / "nope.jsonl") == {}


def test_render_table_includes_unpriced_flag_and_total_row():
    lines = [_line(model="claude-mystery-1", purpose="classify", input_tokens=1000)]
    table = render_table(aggregate(lines))
    assert "UNPRICED" in table
    assert "TOTAL" in table


def test_render_table_empty_report_does_not_crash():
    assert "no usage" in render_table({}).lower()
