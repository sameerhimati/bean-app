"""The 8 golden emails (from the design-studio prototype, store "Maple & Moss") plus the
confidence label each one *should* get, AND the supporting fixture data the loop grounds
against: the operator's reply templates, the knowledge space (policy/FAQ/product docs), and the
order records.

This is the calibration target: the whole Slice-1 rep is "can the routing reproduce these
labels, and never fabricate in a HIGH draft?"

TWO invented stores live here, because one shape cannot cover both jobs:

  - Maple & Moss (CATEGORIES / TEMPLATES / KNOWLEDGE_DOCS / ORDERS / GOLDEN) — an apparel store,
    and the DEMO TENANT: `Config.from_fixtures()` is what a tenant with no `config.json` yet gets
    served, and its first save persists whatever it was served. So this data can BECOME someone's
    live config — which is exactly why nothing real may live here.
  - Sable & Wren (`sablewren_seed.json` → `sw_config()` / SW_KNOWLEDGE_DOCS / SW_GOLDEN) — a
    mid-market FURNITURE retailer, and a FROZEN TEST FIXTURE, never a default. The second store
    exists because the goldens that grade calibration are big-ticket, freight-shaped emails
    (frame warranty vs return window, fabric grades, restocking fees on bulky items); re-pointing
    them at an apparel store would make the reasoning nonsense ("the sofa frame on my wool sweater
    started creaking").

  DO NOT re-seed either one from a live tenant's `config.json`.

That instruction used to say the opposite, and it was the bug. This file once served three masters
at once — (1) the default config for a new tenant, (2) the frozen fixture the goldens are graded
against, and (3) a backup of the live customer's config. (2) wants to never change; (3) wants to
track prod continuously. No sync cadence can satisfy both, so the seed silently rotted — and worse,
`PRODUCT_TREES` used to read the REAL tree out of it, meaning a brand-new tenant booted into another
customer's routing tree and their answers. On 2026-07-13 the demo default was found to have been
persisted OVER that live config in production (the live tenant's categories were these Maple & Moss
ones, byte-for-byte), which is the same conflation firing in the other direction.

So: job (1) is the Maple & Moss constants. Job (2) is `sablewren_seed.json`. Both are wholly
invented and safe to hand to a stranger. Job (3) is a backup of the volume, and does not belong in
git at all (a tenant's runtime data is gitignored, and must stay that way).

(`PRODUCT_TREES` and its synthetic `demo_tree.json` are gone with the routing tree itself — the
notebook engine walks no tree, so the demo default is now just the constants below. The history
above is kept because the conflation it describes is the shape of bug this file exists to prevent.)
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path

from bean.contract import Confidence, Email

# The Sable & Wren config snapshot — a FROZEN TEST FIXTURE (sw_config / SW_GOLDEN), never a default
# and never re-seeded from prod. See the module docstring for why that distinction is load-bearing.
_SABLEWREN_SEED = json.loads((Path(__file__).parent / "sablewren_seed.json").read_text())

# The operator's pre-defined fields (placeholder set, from the prototype).
CATEGORIES = [
    "Order Status",
    "Returns & Exchanges",
    "Sizing & Fit",
    "Product Question",
    "Shipping",
    "Refunds",
    "Wholesale",
    "Escalation",
]

# One-line description per field — feeds the classifier so it knows where each email belongs.
CATEGORY_DESCRIPTIONS = {
    "Order Status": "Where-is-my-order (WISMO): tracking, shipping status, has-it-shipped questions for an existing order.",
    "Returns & Exchanges": "Customer wants to return or exchange an item (size swap, changed mind, fit).",
    "Sizing & Fit": "Pre-purchase or general sizing advice — what size should I order, does it run large/small.",
    "Product Question": "Questions about a product itself — color, material, fit description, availability, details.",
    "Shipping": "Shipping policy questions — do you ship to X, how much, how long, methods (not a specific order's status).",
    "Refunds": "Customer is asking about a refund's status or amount on a return that's already in motion.",
    "Wholesale": "Bulk / B2B / stockist / net-terms inquiries from a business.",
    "Escalation": "Angry, repeat-contact, billing dispute, chargeback, legal threat, or anything needing a human now.",
}

# The operator's reply templates, in their voice. Categories with NO template (Wholesale, Escalation)
# are intentionally absent — the loop FLAGs them (a business/judgment call, never an auto-draft).
TEMPLATES = {
    "Order Status": (
        "Hi {name}, thanks for reaching out! I checked on order {order} — {status_sentence}. "
        "{tracking_sentence} Let me know if there's anything else I can help with.\n\nWarmly,\nRobin"
    ),
    "Returns & Exchanges": (
        "Hi {name}, happy to help with that. {eligibility_sentence} {next_step_sentence} "
        "Just reply here and I'll get it moving.\n\nWarmly,\nRobin"
    ),
    "Sizing & Fit": (
        "Hi {name}, great question! {fit_sentence} {recommendation_sentence} "
        "Happy to help if you're between sizes.\n\nWarmly,\nRobin"
    ),
    "Product Question": (
        "Hi {name}, thanks for asking! {answer_sentence} {caveat_sentence} "
        "Let me know if you'd like anything else.\n\nWarmly,\nRobin"
    ),
    "Shipping": (
        "Hi {name}, thanks for checking before you order! {shipping_sentence} "
        "Let me know if you have any other questions.\n\nWarmly,\nRobin"
    ),
    "Refunds": (
        "Hi {name}, thanks for your patience. {refund_status_sentence} "
        "Reach back out if anything looks off.\n\nWarmly,\nRobin"
    ),
}

# The knowledge space — a simple doc store for Slice 1 (vector retrieval is Slice 2). The
# whole store is small enough to live in the cached system prefix; KnowledgeStore.search()
# exists so the retrieval seam is ready to swap to embeddings later.
KNOWLEDGE_DOCS = {
    "company-info": (
        "Maple & Moss is a small apparel brand based in Portland, OR, est. 2019. We sell boots, "
        "denim, and knitwear. Customer support is run by Robin. We pride ourselves on a warm, "
        "personal reply to every customer."
    ),
    "shipping-policy": (
        "SHIPPING POLICY.\n"
        "Domestic (US): standard shipping is free on orders over $75, otherwise $6. Delivery is "
        "3-5 business days after dispatch. We do not offer guaranteed delivery dates and cannot "
        "guarantee weekend or next-day arrival — we have no expedited or Saturday-guaranteed option.\n"
        "International: we ship to Canada and the United Kingdom. Canada is a $15 flat rate, "
        "6-10 business days; the customer is responsible for any duties or taxes charged at customs. "
        "We do not ship anywhere else at this time.\n"
        "Tracking is emailed automatically when an order dispatches."
    ),
    "return-policy": (
        "RETURNS & EXCHANGES POLICY.\n"
        "Returns and exchanges are accepted within 30 days of delivery. Items must be UNWORN, "
        "unwashed, and have their original tags attached. Size exchanges are free when the "
        "requested size is in stock — we ship the new size and include a prepaid label for the "
        "original.\n"
        "Worn, washed, or used items are NOT eligible for a standard return or exchange. The only "
        "exceptions are genuine manufacturing defects, which are handled case-by-case at Robin's "
        "discretion — there is no automatic entitlement to a return on a worn item."
    ),
    "size-chart": (
        "SIZE & FIT GUIDE.\n"
        "Birch wool sweater: runs true to size — order your usual size. If you are between sizes, "
        "size down for a trimmer fit.\n"
        "Alder straight-leg jeans: run true to size; numeric waist sizing (26-34).\n"
        "Fenwick boots: run a half size large — most people size down a half size."
    ),
    "product-info": (
        "PRODUCT NOTES.\n"
        "Birch overshirt colors: Ecru (warm off-white), Sage (a muted olive green), and Slate "
        "(blue-grey). NOTE: on-screen color depends on the customer's display, and we cannot "
        "confirm whether a particular lifestyle photo versus the product-page photo is color-"
        "accurate for any given screen — we describe the color in words and offer free returns if "
        "it isn't right.\n"
        "Fenwick boots: full-grain leather, Goodyear welt. Alder jeans: 12oz selvedge denim."
    ),
}

# The demo store's display name — what the notebook is titled ("# Maple & Moss — support notebook").
DEMO_STORE = "Maple & Moss"

# The demo store's CLIFFS, keyed by category name (bean/coldstart.py: notebook_from_config).
#
# A category DESCRIPTION and a bucket CLIFF are different objects. The description says what the
# situation IS ("customer wants to return or exchange an item"); the cliff says what the operator
# DOES at the boundary, because that is the thing a model cannot interpolate its way to.
# Cold start falls back to descriptions for a store that hasn't told us
# its cliffs — but the demo tenant is the surface Bean is judged on, so its cliffs are AUTHORED, and
# authored from the knowledge docs above: every boundary below (30 days, unworn, half-size-large,
# US/CA/UK, no expedited) traces to a line in KNOWLEDGE_DOCS.
#
# Written first-person because the notebook is the operator's own voice, and deliberately including
# what they DON'T do — the honest "I say we don't offer it" lines are what keep a draft from
# confidently inventing an expedited option that doesn't exist.
DEMO_CLIFFS = {
    "Order Status": (
        "I look the order up and say where it actually is. Dispatched — tracking already went out "
        "automatically, so I resend it. Not dispatched and past the 3-5 business day window — I say "
        "so plainly rather than repeat the policy back. If I can't see the order at all, I ask for "
        "the number instead of guessing at it."
    ),
    "Returns & Exchanges": (
        "Within 30 days, unworn, tags still on — I just do it, and a size swap is free when the size "
        "is in stock (new size out, prepaid label for the original). Worn or washed, I don't take it "
        "back. The one exception is something that reads like a genuine manufacturing defect, which "
        "I judge case-by-case instead of quoting the policy at them — there's no automatic "
        "entitlement on a worn item, so that call is mine."
    ),
    "Sizing & Fit": (
        "I answer from the size guide and name the specific garment, because they don't run alike: "
        "Fenwick boots run a half size large so most people size down, while the Birch sweater and "
        "the Alder jeans are true to size. Between sizes, I say size down for a trimmer fit. If the "
        "garment isn't in the guide I say I don't have a measurement rather than estimate one."
    ),
    "Product Question": (
        "I describe the product in the words from the product notes. On colour I never confirm "
        "whether a particular photo is accurate — screens differ — so I describe the shade and point "
        "at free returns instead of promising it'll look like the picture."
    ),
    "Shipping": (
        "Policy questions I answer straight from the shipping doc. If they want a guaranteed date, "
        "expedited, or Saturday delivery, I tell them plainly we don't offer it rather than hedge. "
        "The US, Canada and the UK are the only places we ship — anywhere else I say no outright. "
        "Canadian duties are the customer's, and I say that up front rather than let it surprise them."
    ),
    "Refunds": (
        "For a return already in motion I answer the refund's status and amount from the order "
        "record. If the money question isn't already settled — an amount nobody agreed, or a charge "
        "I can't see in the order — it stops being a status question and comes to me."
    ),
    "Wholesale": (
        "Bulk, stockist and net-terms enquiries I answer myself. There's no standard reply — the "
        "terms depend entirely on who's asking and what they want — so surface it to me, never draft it."
    ),
    "Escalation": (
        "Angry, asking a second time, billing disputes, chargebacks, or anything that mentions legal "
        "— I take these myself and immediately. I'd rather read one of these raw than pre-drafted."
    ),
}

# The DEMO store's Shopify handle — used to build order/customer deep-links (US-T10) until the
# native read-only Admin API lands. Surfaced + editable in the admin Settings page.
#
# This was a REAL customer's handle until 2026-07-29, sitting in the constant a config-less tenant is
# served — so a stranger opening the demo got deep-links into a live store. Same class of bug as
# PRODUCT_TREES once reading the seed's `tree`, and the module docstring's rule ("this data can
# BECOME someone's live config, which is exactly why nothing real may live here") already forbade it.
# A real tenant's own handle lives in their persisted config and is unaffected by this default.
SHOPIFY_STORE = "maple-and-moss"

# Sable & Wren's display name — what its notebook is titled ("# Sable & Wren — support notebook").
#
# It is also the store the PUBLIC DEMO runs as, and the two stores' jobs decide which one gets that.
# Maple & Moss is the default a config-less tenant is SERVED, so it must stay boring and safe; it has
# templates but its knowledge docs are four short policies. Sable & Wren has seven source-of-truth
# docs with real cliffs inside them (a 5-year frame warranty vs a 30-day return window,
# wrapped-vs-unwrapped, per-product restocking fees) AND nine goldens written against exactly those —
# which is what a demo has to have, because a demo of Bean is a demo of *drafting from a real brain*.
# Nothing here is a default; the demo tenant is materialized by an explicit script (bean/demo.py),
# same as its notebook.
SW_STORE = "Sable & Wren"

# Sable & Wren's knowledge space — the second invented store's source-of-truth docs (voice-and-tone,
# return policy, sofa product/returns, frame warranty, delivery & assembly), from its seed snapshot.
# SEPARATE from KNOWLEDGE_DOCS (Maple & Moss) so the flat path's calibration + the
# cache-snapshot byte-identity stay untouched.
SW_KNOWLEDGE_DOCS = {d["title"]: d["body"] for d in _SABLEWREN_SEED["knowledgeDocs"]}


def sw_config():
    """Sable & Wren's `Config` — the seed snapshot's categories + knowledge + settings.
    The snapshot on disk also carries the deleted routing tree; `Config.from_dict` reads past it,
    which is exactly what makes it a live regression guard for that compatibility (a real tenant's
    config.json on the volume still carries the same dead key). Imports config lazily to avoid an
    import cycle."""
    from bean.config import Config

    return Config.from_dict(_SABLEWREN_SEED)


# The Stage-2 multi-chunk golden: three heterogeneous units of work in one email — two product
# answers + one action — exercising chunk→route→resolve→assemble→reduce and the worst-chunk rule
# (two HIGH product chunks + one LOW action chunk → overall LOW, one assembled reply). Lives apart
# from GOLDEN (which is the flat path's Maple & Moss calibration set).
SW_MULTICHUNK = Email(
    "sw-multi", "Jordan Reese", "jordan.reese@gmail.com",
    "Fabric + doorway + a quick call?",
    "Three quick things: which fabric grade should I pick if we have a golden retriever? "
    "Will the Harlow 3-seater fit through a 30-inch doorway? And can we set up a quick call "
    "this week to walk me through the room-of-choice delivery options?",
)

# Order records — the OrderLookup fixture. The HIGH cases must be groundable here; MM-0951's
# charge/payment facts are deliberately NOT present (they live in Stripe, which Bean can't see)
# so a double-charge claim cannot be grounded -> FLAG.
ORDERS = {
    "MM-1043": {
        "status": "shipped",
        "carrier": "UPS",
        "tracking": "1Z999AA10123456784",
        "shipped_on": "2026-06-18",
        "est_delivery": "2026-06-24",
        "items": ["Fenwick boots"],
    },
    "MM-1021": {
        "status": "delivered",
        "delivered_on": "2026-06-10",
        "within_return_window": True,
        "items": ["Alder straight-leg jeans, size 30"],
        "exchange_requested_size": "28",
        "size_28_in_stock": True,
    },
    "MM-0998": {
        "status": "delivered",
        "delivered_on": "2026-06-01",
        "within_return_window": True,
        "items": ["Fenwick boots"],
        "note": "Customer states the item has been worn twice.",
    },
    "MM-0951": {
        "status": "delivered",
        "delivered_on": "2026-05-20",
        "items": ["Birch wool sweater"],
        "note": "Charge/payment details are held in Stripe and are NOT available to Bean. A "
                "double-charge claim cannot be verified or refunded from this record.",
    },
}


@dataclass(frozen=True)
class GoldenCase:
    email: Email
    expected_category: str
    expected_confidence: Confidence
    note: str  # why this label — the human judgment we're trying to reproduce
    # Genuinely-ambiguous cases the model legitimately wavers on (e.g. a low/flag boundary):
    # extra labels that also count as correct. Empty for the strict majority of goldens.
    also_accepts: tuple[Confidence, ...] = ()


GOLDEN: list[GoldenCase] = [
    GoldenCase(
        Email("mm-1043", "Devin Brooks", "devin.brooks@gmail.com", "Where's my order? #MM-1043",
              "I ordered the Fenwick boots last Tuesday and still have not gotten a shipping "
              "update. Order #MM-1043. Getting a little anxious — can you check?",
              thread=[
                  "Hi! I just placed my order for the Fenwick boots (#MM-1043) — wanted to "
                  "make sure it came through on your end. Thanks so much!",
              ]),
        "Order Status", Confidence.LOW,
        "The category is clear and the bucket's cliff says she LOOKS THE ORDER UP — but the notebook "
        "engine is never handed order data (`draft_email` takes notebook + exemplars + thread + "
        "history + email, and no lookup). So the interesting assertion here is the NEGATIVE one: "
        "HIGH is the one verdict that would be wrong, because it could only be reached by inventing "
        "a ship status. LOW (hedge and ask) and FLAG (hand it over) are both honest; which one it "
        "picks is a judgement call this golden deliberately does not force. Restore HIGH as the "
        "expectation when Bean can actually look an order up.",
        also_accepts=(Confidence.FLAG,),
    ),
    GoldenCase(
        Email("mm-1021", "Priya Nair", "priya.nair@gmail.com", "Exchange — Alder jeans too big",
              "The Alder straight-leg jeans are a size too big. How do I exchange for a 28? "
              "Unworn with tags on. Order #MM-1021."),
        "Returns & Exchanges", Confidence.HIGH,
        "Textbook in-policy exchange: in-window, unworn, tags on. But the bucket's own cliff makes "
        "the free swap conditional on the new size being IN STOCK, and the notebook engine receives "
        "no inventory. Hedging on the one fact it cannot check is correct, so LOW is accepted.",
        also_accepts=(Confidence.LOW,),
    ),
    GoldenCase(
        Email("mm-ship-ca", "Marc Liu", "marc.liu@gmail.com", "Do you ship to Canada?",
              "Quick question before I order — do you ship to Canada, and roughly how much / how long?"),
        "Shipping", Confidence.HIGH,
        "Plain FAQ fully covered by the international-shipping policy doc.",
    ),
    GoldenCase(
        Email("mm-size-wool", "Tess Okafor", "tess.okafor@gmail.com", "Sizing on the wool sweater",
              "What size would you recommend for the Birch wool sweater? I am usually a medium."),
        "Sizing & Fit", Confidence.HIGH,
        "Clear answer from the size chart; runs true to size.",
    ),
    GoldenCase(
        Email("mm-0998", "Greg Mahoney", "g.mahoney@gmail.com", "Boots don't fit right",
              "I've worn the Fenwick boots twice and the left one rubs my heel raw. I want to "
              "send them back. Can I? Order #MM-0998."),
        "Returns & Exchanges", Confidence.LOW,
        "Category clear, but policy is unworn-only and he's worn them — a judgment call, not "
        "automatic. The model wavers low↔flag on exactly this boundary, and both are defensible "
        "(draft-with-low-trust vs escalate), so flag also counts here.",
        also_accepts=(Confidence.FLAG,),
    ),
    GoldenCase(
        Email("mm-sage", "Sofia Reyes", "sofia.reyes@gmail.com",
              "Is 'Sage' the green in the photo? Need it by Sat",
              "I want the Birch overshirt in Sage for a wedding Saturday. Is Sage the olive in the "
              "lifestyle photo or the brighter green on the product page? And can it arrive by Saturday?"),
        "Product Question", Confidence.LOW,
        "Two-part ask: a swatch Bean can't visually confirm + a delivery date it can't promise. "
        "Under the TREE this was LOW for a structural reason — it chunked into two, and the reduce "
        "took the worst chunk, so multi-intent mechanically meant downgrade. The finding recorded "
        "here was that the classifier conflated multi-intent with category-doubt, and the fix asked "
        "for was: bucket by PRIMARY intent, address the secondary in the reply. "
        "The notebook engine does exactly that, so GREEN is accepted: both of the operator's cliffs "
        "say to DECLINE PLAINLY (never confirm a photo's colour; never promise a date), and a "
        "confident, cited, on-policy 'no' to both is a one-tap send — not a hedge. Declining is an "
        "answer. What would make this yellow is uncertainty about the POLICY, not bad news in it.",
        also_accepts=(Confidence.HIGH, Confidence.FLAG),
    ),
    GoldenCase(
        Email("mm-0951", "Alan Whitfield", "alan.whitfield@gmail.com",
              "THIRD time — double charged, no response",
              "This is my THIRD email. I was charged twice for order #MM-0951 and nobody has "
              "replied. I've opened a dispute with my bank and will leave reviews until this is fixed.",
              thread=[
                  "Hi, I think I was charged twice for order #MM-0951 — there are two identical "
                  "charges on my card. Could someone take a look? Thank you.",
                  "Following up on the below — I still see both charges for #MM-0951 and haven't "
                  "heard anything back. Can you please advise?",
              ]),
        "Escalation", Confidence.FLAG,
        "Billing dispute + angry repeat-emailer; payment facts need a human in Stripe. Don't fake it.",
    ),
    GoldenCase(
        Email("mm-foxglove", "Lena Hart", "lena@foxglovegoods.com", "Bulk order + net-30 terms?",
              "We're a Portland boutique and would love to stock you — ~200 units across boots and "
              "knitwear, ideally net-30. Who's the right person to talk to?"),
        "Wholesale", Confidence.FLAG,
        "No wholesale template; pricing and payment terms are business decisions, not a draft.",
    ),
]


# The Sable & Wren golden inbox — 9 realistic customer emails written against Sable & Wren's
# tree + docs (sablewren_seed.json), triaged by the tree pipeline:
# the tree engine, via `sw_config()`. This is the inbox the retired `bean.cli` rendered
# into web/bean-data.jsx; GOLDEN (Maple & Moss) stays the flat path's calibration set.
#
# expected_category = the tree TOP-branch label the PRIMARY chunk should land in
# (the category was path[0] of the worst chunk, or "General" on a top-level stall).
# Order-status replies (WISMO) now ground on the email-derived order index (SW_NOTIFICATIONS →
# bean/order_index.py) — matched by order number or the customer's email — while the high-stakes
# nodes (cancel / refund / return) still escalate; their [FLAG FOR NORA] facts have no source.
SW_GOLDEN: list[GoldenCase] = [
    GoldenCase(
        Email("sw-fabric", "Melissa Grant", "melissa.grant@gmail.com",
              "Which fabric for a house with two dogs?",
              "Hi! I'm about to order the Harlow 3-seater and I see three fabric grades at checkout. "
              "We have two dogs and a four-year-old, and I can't tell which one actually holds up. "
              "Which should I pick? Thanks!"),
        "Sofas & Upholstery", Confidence.HIGH,
        "Textbook fabric-grade question: pets and small children → Performance Weave is stated "
        "verbatim in the sofa-product-info doc and the Which-fabric-grade answer node. Low stakes, "
        "fully grounded → the blind-trust bucket.",
    ),
    GoldenCase(
        Email("sw-assembly", "Dale Whitcomb", "dale.whitcomb@yahoo.com",
              "Bed frame slats don't line up",
              "I got the Calder bed frame last week (order #SW-20931). I put it together the way the "
              "sheet shows, but the slat holes on the side rail don't line up with the bolts at all. "
              "Is there a step I'm missing? Happy to send a photo of how it's assembled."),
        "Delivery & Assembly", Confidence.HIGH,
        "Assembly/parts-not-lining-up leaf: the taught reply gathers order details, offers photo "
        "troubleshooting, and points to the paid White-Glove Assembly visit — everything it asserts "
        "is in the delivery-and-assembly doc. Low stakes → HIGH.",
    ),
    GoldenCase(
        Email("sw-wismo", "Angela Pruitt", "angela.pruitt@gmail.com",
              "Order status?",
              "Hello, I placed an order about a week ago and haven't received a shipping "
              "confirmation yet. What information do you need from me to look it up? Thank you!"),
        "Orders, Returns & Refunds", Confidence.HIGH,
        "Plain WISMO with no order number. Bean grounds it via the email-derived order index — "
        "Angela's shipping notification (order #SW-21050) is on file, matched by her email address "
        "— so the Where-is-my-order leaf answers with the real status + tracking instead of asking. "
        "Low stakes, fully grounded → HIGH. Without the index (an unseen customer) it falls back to "
        "the ask, a valid LOW.",
        also_accepts=(Confidence.LOW,),
    ),
    GoldenCase(
        Email("sw-wismo-number", "Brian Cole", "brian.cole@gmail.com",
              "Update on my order?",
              "Hi, could you give me an update on order #SW-20988 (the Wexford bookshelf)? Just want "
              "to know where it is in transit. Thanks!"),
        "Orders, Returns & Refunds", Confidence.LOW,
        "WISMO grounded by ORDER NUMBER (vs sw-wismo, which grounds by customer email). SW-20988 is "
        "in the index — Brian Cole, already delivered, UPS tracking on file — so the Where-is-my-"
        "order leaf answers with the real status + tracking instead of asking. Routing + grounding "
        "are unambiguous; the confidence sits LOW because the customer assumes 'in transit' while "
        "the order shows delivered, a mismatch worth Nora's glance — HIGH is equally defensible "
        "when the model treats a fully-cited status as blind-trust.",
        also_accepts=(Confidence.HIGH,),
    ),
    GoldenCase(
        Email("sw-cancel", "Priyanka Rao", "priyanka.rao@gmail.com",
              "Cancel order #SW-21044 please",
              "I placed order #SW-21044 about an hour ago and changed my mind — can you cancel it "
              "before it goes out on the truck? I'd rather not have to send a sofa back."),
        "Orders, Returns & Refunds", Confidence.FLAG,
        "Cancel-order is a taught answer node but stakes:high, and the taught reply's "
        "[FLAG FOR NORA] half (one-time exception? exact refund minus the delivery already booked?) "
        "needs facts Bean can't source — ungrounded + high stakes → FLAG, no faked commitment. On "
        "runs where the model treats the pure policy statement as fully covering, a "
        "confirm-before-send LOW is also defensible.",
        also_accepts=(Confidence.LOW,),
    ),
    GoldenCase(
        Email("sw-warranty", "Tom Ferris", "tom.ferris@gmail.com",
              "Sofa frame creaking after six weeks",
              "I bought the Harlow II 3-seater about six weeks ago (order #SW-19488). The right arm "
              "creaks every time anyone sits down and this week it started to feel loose. There's "
              "no damage — nobody has stood on it. I have photos if that helps. Can I get this "
              "replaced under warranty?"),
        "Sofas & Upholstery", Confidence.HIGH,
        "Comfortably inside the 5-year frame warranty (six weeks + photos + no impact damage, all "
        "stated in the email — the customer's own email is a legitimate source) and the taught reply "
        "only gathers info, committing to a replacement strictly 'if confirmed defective'. Fully "
        "grounded → HIGH under the current map. FINDING: stakes:high only gates UNGROUNDED facts "
        "(route() in bean/routing.py), so a fully-grounded money node can one-tap — if Nora wants "
        "every warranty reply confirmed, high stakes should cap at LOW; a routing change, not a "
        "fixture one. FLAG also counts when the model requires the Shopify delivery date over the "
        "customer's word (frame-warranty says 'step one: find the delivery date in Shopify').",
        also_accepts=(Confidence.FLAG,),
    ),
    GoldenCase(
        Email("sw-mattress", "Carol Jenkins", "carol.jenkins@aol.com",
              "Which mattress should I get?",
              "We're furnishing a guest room and I'm a side sleeper with a bad shoulder. Which of "
              "your mattresses would you recommend? And do I need a foundation to go with it?"),
        "Mattresses", Confidence.FLAG,
        "Untaught product line: the Mattresses branch is a depth-0 escalate leaf (no template yet), "
        "so Bean flags for Nora to answer and teach rather than bluff. The route model sometimes "
        "reads 'which mattress should I buy' as Product Recommendation — whose own mattress leaf is "
        "also escalate — so either top branch FLAGs; Mattresses is the expected primary.",
    ),
    GoldenCase(
        Email("sw-refund-angry", "Marcus Bell", "marcus.bell@hotmail.com",
              "Still no refund — third email",
              "This is the THIRD time I've written. I returned the mattress over three weeks "
              "ago — tracking shows you received it on the 12th — and I still haven't seen a "
              "refund. If I don't hear back this week I'm disputing the charge with my card "
              "company.",
              thread=[
                  "Hi, I returned the mattress (order #SW-18066) last week and wanted to make "
                  "sure it arrived okay. When should I expect the refund?",
                  "Following up on my return — tracking shows it was delivered to you on the 12th. "
                  "Can someone confirm the refund is being processed?",
              ]),
        "General / New / Needs Nora", Confidence.FLAG,
        "Angry repeat-contact + a chargeback threat on money Bean can't verify (was the refund "
        "issued? what amount, after the 45% mattress restocking fee?). The General/New/Needs-"
        "Nora trigger names exactly this; routing to Orders → Refund status FLAGs too "
        "(ungrounded facts on a stakes:high node). Either way: no draft, hand to Nora.",
    ),
    GoldenCase(
        Email("sw-wholesale", "Jenna Kowalski", "jenna@haldenstudio.example",
              "Trade inquiry — stocking the Harlow range",
              "I run a small interior design studio in Boulder and clients ask for a sofa like the "
              "Harlow constantly. Would you be open to a trade arrangement on the Harlow sofas and "
              "the Aspen tables? We'd start around 50 pieces a year. Do you offer net-30 terms to "
              "trade accounts?"),
        "General / New / Needs Nora", Confidence.FLAG,
        "No tree home for wholesale: pricing and terms are Nora's business call. Two legitimate "
        "FLAG routes — the General/New/Needs-Nora escalate leaf (its trigger names wholesale) or "
        "a top-level stall (category 'General'). Both are correct behavior; never a draft.",
    ),
    GoldenCase(
        SW_MULTICHUNK,
        "Delivery & Assembly", Confidence.LOW,
        "Three units of work: fabric grade (HIGH, taught answer) + doorway fit (HIGH, taught "
        "answer) + schedule a call (action node — drafted but needs Nora's hand, LOW). "
        "Worst-chunk reduce drags the assembled reply to LOW; the primary (worst) chunk should be "
        "the call, so category = Delivery & Assembly. KNOWN WOBBLE: the resolver sometimes leaks "
        "the email's OTHER asks into an answer chunk's coverage concerns (the mm-sage multi-intent "
        "finding), turning chunk 1 LOW and the category 'Sofas & Upholstery' — same LOW overall, "
        "same correct paths, different primary.",
    ),
]

# Demo-only non-customer mail, so the built fixture inbox exercises the Filed/FYI lane (the demoted
# "sorted & demoted, NEVER hidden" surface). Run through the real gate in cli.run_inbox — NOT
# hardcoded as filed — so the demo shows a genuine gate verdict; the goldens above skip the gate
# (they're the calibration customers, definitionally needs-reply). An obvious newsletter so the
# fail-open gate reliably files it.
SW_DEMO_FILED: list[Email] = [
    Email("sw-newsletter", "Packaging Digest", "newsletter@packagingdigest.com",
          "This week: 10 sustainable packaging trends 📦",
          "Hi there! Your weekly Packaging Digest is here.\n\nThis week's top stories: compostable "
          "mailers, the rise of mono-material pouches, and 8 more trends shaping DTC packaging in "
          "2026.\n\nRead the full roundup on our site. You're receiving this because you subscribed. "
          "Unsubscribe anytime."),
]

# Store order-notification mail — the source Bean's order index is built from (bean/order_index.py,
# bean/notification_parser.py). These are the merchant-facing Shopify notifications that land in
# Nora's inbox (hello@sableandwren.example) and forward into Bean: an order confirmation + a shipping
# confirmation for the SAME order (they merge into one full record), plus a delivered update for a
# second order. Note the customer email lives in the BODY (the From is the store/Shopify), which is
# what lets a numberless WISMO ground by customer email. Order SW-21050 is Angela Pruitt — the
# sw-wismo golden's sender — so her "where's my order" grounds once these are indexed.
#
# The confirmation deliberately carries the STORE's own address ABOVE the customer's, because that
# is how the platform's mail is actually laid out and it is the case that breaks: the parser keys
# the customer on the first address in the body, so without `settings.storeDomains` telling it that
# sableandwren.example is the merchant, every order would be indexed against the support inbox and no
# customer would ever match. See bean/notification_parser._store_domains.
SW_NOTIFICATIONS: list[Email] = [
    Email("sw-notif-21050-confirm", "Sable & Wren", "orders@shopify.com",
          "New order #SW-21050",
          "Sent to hello@sableandwren.example\n\n"
          "You've received a new order.\n\n"
          "Order date: 2026-06-30\n"
          "Customer: Angela Pruitt <angela.pruitt@gmail.com>\n\n"
          "Items:\n"
          "1 × Aspen Extension Dining Table\n\n"
          "Estimated delivery: 2026-07-08\n"
          "Ship to: Angela Pruitt, 44 Cedar Lane, Austin, TX 78701\n"),
    Email("sw-notif-21050-ship", "Sable & Wren", "orders@shopify.com",
          "A shipment from order #SW-21050 is on the way",
          "Your order is on the way.\n\n"
          "Customer: Angela Pruitt <angela.pruitt@gmail.com>\n"
          "Shipped on: 2026-07-01\n"
          "Carrier: USPS\n"
          "Tracking number: 9400111899223344556677 "
          "https://tools.usps.com/go/TrackConfirmAction?tLabels=9400111899223344556677\n\n"
          "Items:\n"
          "1 × Aspen Extension Dining Table\n"),
    Email("sw-notif-20988-delivered", "Sable & Wren", "orders@shopify.com",
          "Your order #SW-20988 was delivered",
          "Good news — your order has been delivered.\n\n"
          "Customer: Brian Cole <brian.cole@gmail.com>\n"
          "Carrier: UPS\n"
          "Tracking number: 1Z999AA10123456784\n\n"
          "Items:\n"
          "1 × Wexford Bookshelf\n"),
]
