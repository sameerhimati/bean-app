"""Suite-wide isolation from the developer's own `.env`.

`bean/llm.py` calls `load_dotenv()` at import, so every value in the project-root `.env` is in the
environment of every test that imports anything. That is right for running Bean and wrong for
testing it: the suite's behaviour then depends on which secrets happen to be on the machine.

It cost a full red suite on 2026-08-21. `BEAN_PASSCODE` was added to `.env` so a real prod read
could be made, and 79 tests went red on `assert 401` — `test_server.py` and `test_stats.py` drive
the real handler and had only ever passed because no passcode was present locally. Nothing was
broken; the tests had simply been reading an ambient value nobody declared. Worse than the red: it
blocks `scripts/preflight.sh`, which gates every deploy — so a secret in a file the deploy never
reads could stop the deploy.

So the secrets are cleared for every test, and a test that wants one sets it itself
(`tests/test_passcode_gate.py` does exactly this). Autouse fixtures run before the fixtures and
bodies that follow them, so an explicit `monkeypatch.setenv` in a test still wins.

Deliberately NOT clearing `ANTHROPIC_API_KEY`: the live eval (`-m live`) needs it, and it is
already the one thing every offline test avoids by construction (`FakeModel`).
"""

from __future__ import annotations

import pytest

# The tenant-facing secrets. Both gate real HTTP surfaces, and both are absent on a fresh clone —
# which is the environment the suite should assume, since it is the one CI has.
_AMBIENT_SECRETS = ("BEAN_PASSCODE", "BEAN_OWNER_PASSCODE")


@pytest.fixture(autouse=True)
def _no_ambient_secrets(monkeypatch):
    """Clear the passcodes for every test. See the module docstring for the incident."""
    for name in _AMBIENT_SECRETS:
        monkeypatch.delenv(name, raising=False)
