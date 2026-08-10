"""Image / screenshot context: a customer's photo attachment must reach the model.

Offline (FakeModel records the `images=` it receives, so we assert on the plumbing without a live
API). This is a live path, not a nicety: the yellow "these boots came apart, send us photos" flow
is the product's whole 20%, and it only works if the photos she asks for actually arrive.

Historically this file also covered screenshot-as-knowledge-doc (`Config.knowledge_images`), which
fed images into the routing tree's classify/assess legs. The notebook engine reads no such thing —
`draft_email` passes the EMAIL's attachments and nothing else — so those tests went with the tree,
and `Config.knowledge_images` has now gone with `Config.tree`. A knowledge doc is title + body; the
only images that reach the model are the customer's own attachments, which is what this file pins.
"""

from __future__ import annotations

from pathlib import Path

from bean.contract import Email
from bean.engine import DRAFT_TOOL, draft_email
from bean.llm import FakeModel
from bean.notebook import Bucket, Notebook

PIXEL = str(Path(__file__).resolve().parent / "fixtures_pixel.png")


def _nb() -> Notebook:
    return Notebook("S", buckets=[Bucket("Returns & refunds", "30 days, original condition")])


def _email(image_paths=None) -> Email:
    return Email(
        id="mm-img-1",
        sender_name="Devin",
        sender_email="devin@example.com",
        subject="These came apart",
        body="Photo attached — the sole separated after two weeks.",
        image_paths=image_paths or [],
    )


def _model() -> FakeModel:
    return FakeModel({DRAFT_TOOL["name"]: {
        "bucket": "Returns & refunds", "draft": "Thanks for the photo — that's a defect.",
        "citations": ["notebook:Returns & refunds"], "confidence": "green", "why_unsure": [],
    }})


def test_customer_attachment_reaches_the_drafting_call():
    model = _model()
    draft_email(_nb(), [], [], None, _email(image_paths=[PIXEL]), model)
    assert model.images == [[PIXEL]]  # FakeModel records the `images=` it received per call


def test_no_attachment_passes_no_images():
    model = _model()
    draft_email(_nb(), [], [], None, _email(), model)
    # engine.py sends `images=email.image_paths or None`, so a mail with no photo carries no image
    # block at all; FakeModel records that absence as [] (see bean/llm.py).
    assert model.images == [[]]
