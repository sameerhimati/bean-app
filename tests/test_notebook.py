"""Offline tests for bean/notebook.py — the model-neutral brain round-trips and renders.

The notebook is markdown a human edits and the engine caches as its system prefix. Two properties
must hold: what save() writes, load() reads back (so a distilled notebook survives a hand edit), and
provenance tags survive the trip (precedent vs stated policy is a load-bearing distinction).
"""

from __future__ import annotations

from bean.notebook import (
    _STAKES_HIGH_MARKER, Bucket, Fact, Macro, Note, Notebook, humanize_macro_names, loads,
)


def _sample() -> Notebook:
    return Notebook(
        store="Sable & Wren",
        buckets=[
            Bucket("Not working / broken", "within ~90 days I replace; months of wear I offer a discount"),
            Bucket("Returns & refunds", "30 days, original condition, customer pays return shipping"),
        ],
        macros=[Macro("Return label", "Here is your prepaid label...")],
        facts=[Fact("30-day return window", "stated"), Fact("Sofa legs work loose after 3-6 months", "observed")],
        notes=[Note("honored a just-outside-90-day claim on a barely-used frame", "observed")],
    )


def test_round_trip_preserves_structure():
    nb = _sample()
    back = loads(nb.render())
    assert back.store == "Sable & Wren"
    assert back.bucket_names() == ["Not working / broken", "Returns & refunds"]
    assert back.buckets[0].cliff.startswith("within ~90 days")
    assert back.macros[0].name == "Return label"
    assert "prepaid label" in back.macros[0].text


def test_round_trip_preserves_provenance():
    # The observed-vs-stated tag is what lets precedent outrank policy in the prompt — it must survive.
    back = loads(_sample().render())
    tags = {f.text: f.provenance for f in back.facts}
    assert tags["30-day return window"] == "stated"
    assert tags["Sofa legs work loose after 3-6 months"] == "observed"
    assert back.notes[0].provenance == "observed"


def test_render_is_stable_across_calls():
    # The prefix caches only if it's byte-identical call to call.
    nb = _sample()
    assert nb.render() == nb.render()


def test_bare_bullet_defaults_provenance():
    # A hand-edited bullet without a [tag] must not crash the parser; it takes the section default.
    md = "# S — support notebook\n\n## Store facts\n- a plain fact with no tag\n"
    nb = loads(md)
    assert nb.facts[0].text == "a plain fact with no tag"
    assert nb.facts[0].provenance == "stated"


def test_stakes_round_trips_and_cliff_stays_clean():
    # A high-stakes bucket's flag must survive render→load, and the marker must NOT leak into the
    # cliff prose (the cliff is what the model reads to route; a stray marker would pollute it).
    nb = Notebook(
        store="S",
        buckets=[
            Bucket("Not working / broken", "within ~90 days I replace", "high"),
            Bucket("Product questions", "answer the question and stop"),  # defaults to normal
        ],
    )
    back = loads(nb.render())
    assert back.buckets[0].stakes == "high"
    assert back.buckets[0].cliff == "within ~90 days I replace"
    assert _STAKES_HIGH_MARKER not in back.buckets[0].cliff
    assert back.buckets[1].stakes == "normal"


def test_normal_bucket_render_has_no_marker():
    # Normal buckets emit nothing — existing distilled notebooks stay byte-identical after the change.
    nb = Notebook(store="S", buckets=[Bucket("Orders", "check if it shipped")])
    assert _STAKES_HIGH_MARKER not in nb.render()


def test_to_dict_from_dict_round_trips():
    nb = _sample()
    nb.buckets[0].stakes = "high"
    back = Notebook.from_dict(nb.to_dict())
    assert back.render() == nb.render()
    assert back.buckets[0].stakes == "high"


def test_from_dict_clamps_and_drops_blanks():
    # The editor boundary: bad stakes/provenance are clamped, blank add-rows dropped, no crash.
    data = {
        "store": "S",
        "buckets": [
            {"name": "Real", "cliff": "do the thing", "stakes": "SUPER-HIGH"},
            {"name": "  ", "cliff": "blank add-row"},
        ],
        "facts": [{"text": "a fact", "provenance": "made-up"}, {"text": ""}],
    }
    nb = Notebook.from_dict(data)
    assert [b.name for b in nb.buckets] == ["Real"]
    assert nb.buckets[0].stakes == "normal"  # unknown value clamped down
    assert [f.text for f in nb.facts] == ["a fact"]
    assert nb.facts[0].provenance == "stated"  # unknown provenance clamped to default


def test_humanize_macro_names_fixes_key_like_names_only():
    # The distilled-from-config leak: macro names come out as template KEYS, which render on her
    # questionnaire card as gibberish. Humanize the shouting; never touch a name a human wrote, and
    # never touch the reply text.
    nb = Notebook(
        store="Sable & Wren",
        macros=[
            Macro("INTL_SHIPPING_PAUSED", "We've paused international shipping for now."),
            Macro("WARRANTY_EXPIRED_DISCOUNT", "Here's 15% off a replacement."),
            Macro("Shipping times", "Orders ship in 1-2 business days."),  # already human — untouched
        ],
    )
    out = humanize_macro_names(nb)
    assert [m.name for m in out.macros] == [
        "Intl shipping paused", "Warranty expired discount", "Shipping times",
    ]
    # the reply text is never rewritten
    assert out.macros[0].text == "We've paused international shipping for now."


def test_humanize_macro_names_is_idempotent_and_leaves_the_rest_alone():
    nb = Notebook(
        store="S",
        buckets=[Bucket("Not working / broken", "within 90 days I replace", stakes="high")],
        macros=[Macro("GUEST_POST_DECLINE", "No thanks.")],
        facts=[Fact("Free shipping over $120.", "stated")],
        notes=[Note("Apologise first.", "observed")],
    )
    once = humanize_macro_names(nb)
    twice = humanize_macro_names(once)
    assert once.macros[0].name == "Guest post decline"
    assert twice.macros[0].name == "Guest post decline"  # running it again changes nothing
    # buckets/facts/notes ride through untouched (including stakes)
    assert once.buckets == nb.buckets and once.facts == nb.facts and once.notes == nb.notes
