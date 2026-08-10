PYTHON ?= .venv/bin/python

.PHONY: test eval preflight

## The free, offline gate: every test that does not spend a token. Zero --ignore flags.
test:
	$(PYTHON) -m pytest

## Grade the agent against the goldens. LIVE — spends tokens, needs ANTHROPIC_API_KEY.
## -s so the calibration scoreboard reaches the terminal; BEAN_EVAL_OUT captures it as JSON
## so two runs can be diffed rather than eyeballed.
eval:
	$(PYTHON) -m pytest -m live -s

## The pre-deploy verifier. Exits nonzero on anything that has taken prod down before.
preflight:
	./scripts/preflight.sh
