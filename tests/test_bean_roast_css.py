"""The reduced-motion escape hatch on the roast mark actually wins the cascade.

Not a style nit. The running rules are four classes deep (`.bean-roast.mode-brew.is-on .br-body`);
the rules that STOP the animation are three (`.is-static`) and two (the `prefers-reduced-motion`
block). Written without `!important` they lose, and both failures are silent:

  - `.is-static` renders nothing at all — the brewkit is hidden while `bean-brew-bean` still holds
    opacity 0 across 42–93% of the loop, so the mark simply disappears for most of every cycle;
  - a reader who asked their OS for no motion gets the full six-second journey anyway.

A browser is the only thing that can compute a cascade, and this suite has no browser. So the guard
is textual: the declarations that cancel motion must carry `!important`. Cheap, and it fails loudly
the moment someone "cleans up" the important flags.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

_HTML = Path(__file__).parent.parent / "web" / "Bean.html"


@pytest.fixture(scope="module")
def css() -> str:
    return _HTML.read_text(encoding="utf-8")


def _block(css: str, start: str) -> str:
    """The declaration block introduced by the selector list containing `start`."""
    i = css.index(start)
    return css[i : css.index("}", i) + 1]


def test_static_mode_cancels_every_animation_with_important(css):
    block = _block(css, ".bean-roast.is-static .br-body,")
    assert "animation: none !important" in block
    for part in (".br-swell", ".br-judder", ".br-sheen", ".br-flicker", ".br-brewkit *"):
        assert part in block, f"{part} must be in the cancelled set"


def test_reduced_motion_cancels_every_animation_with_important(css):
    reduce_at = css.index("@media (prefers-reduced-motion: reduce)")
    # The roast's block, not the chat panel's or any other reduce block later in the file.
    block = _block(css[reduce_at:], ".bean-roast .br-body,")
    assert "animation: none !important" in block


def test_the_held_heat_glow_also_beats_the_running_rule(css):
    # The one animation reduced motion KEEPS — a slow low-opacity cross-fade — has to outrank
    # `bean-brew-heat` too, or the heat pulses on its six-second envelope instead.
    for hit in re.findall(r"\.br-heat\s*\{[^}]*bean-roast-hold[^}]*\}", css):
        assert "!important" in hit
    assert css.count("bean-roast-hold 2.4s") == 2  # once for .is-static, once for the media query


def test_the_mark_is_loaded_before_the_root_that_renders_it(css):
    # bean-root.jsx calls ReactDOM.createRoot(...).render(...) at module scope, so every window.X
    # it reaches must already be defined.
    assert css.index('src="bean-roast.jsx"') < css.index('src="bean-root.jsx"')
