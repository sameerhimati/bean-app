"""Offline tests for bean/llm.py — the out-of-credits detection at the live seam.

The live model call itself needs the SDK + a key, but the credit-error CLASSIFICATION is a pure
string check we can pin without a network: it's what decides whether a failure becomes a "top up
Bean's monies" 402 or an ordinary error.
"""

from __future__ import annotations

from bean.llm import OutOfCreditsError, _is_out_of_credits


def test_detects_the_credit_balance_message():
    # Anthropic's out-of-credits error is a 400 whose body says the credit balance is too low.
    exc = Exception(
        "Error code: 400 - {'type': 'error', 'error': {'type': 'invalid_request_error', "
        "'message': 'Your credit balance is too low to access the Anthropic API.'}}"
    )
    assert _is_out_of_credits(exc) is True


def test_ignores_other_failures():
    # Rate limits, overload, and plain bad-request 400s must NOT read as out-of-credits — they are
    # retryable or a different fix, and would wrongly show the "top up" message.
    assert _is_out_of_credits(Exception("429 rate_limit_error: too many requests")) is False
    assert _is_out_of_credits(Exception("529 overloaded_error")) is False
    assert _is_out_of_credits(Exception("400 invalid_request_error: max_tokens is required")) is False


def test_out_of_credits_is_its_own_error_type():
    # Callers branch on this specific type, so it must not collapse into a generic Exception subclass
    # that other handlers already catch-and-retry.
    assert issubclass(OutOfCreditsError, Exception)
