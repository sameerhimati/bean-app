# Bean — Claude Code Project Context

Bean is a **human-in-the-loop email triage + reply-drafting agent** for one ecommerce support
operator. It is the author's first agent built from scratch — the rep that closes the
never-built-an-agent gap.

Throughout this repo the person Bean works for is called **"the operator"**: whoever runs support
mail for a store. Bean runs in production for one real operator, and their identity stays out of
the source.

## What Bean is (and is NOT)

**Is:** a thin agent that (1) decides whether an email needs a reply at all, (2) drafts one
grounded in the operator's own notebook, (3) routes by its own confidence, and (4) shows the draft
for **one-tap approval** — never autosend.

**Is NOT (yet):** a "chief-of-staff stack." Not a project manager, not a helpdesk replacement, not
a platform. That's the north star you *earn by nailing the wedge* — it is not the v1 scope. The
moment v1 tries to do more than triage→draft→approve, the edge is gone and it becomes every other
AI-employee startup. Hold the line: thin wedge, one customer.

## The actual product is the "yes-man problem"

Matching email→category→template is the throwaway 80%. **The product is the 20% where there is no
obvious response.** A yes-man that always emits a confident reply is *worse than nothing* — the
operator has to re-read everything, so no time is saved. The feature is **calibrated uncertainty +
escalation-to-human**:
- High confidence → one-tap approve.
- Low confidence → "needs your call, here's the draft AND here's why I'm unsure."
- Genuinely no answer → don't fake one; flag and ask.

Time saved is a function of how much of the high-confidence bucket the operator can trust without
checking. That trust loop is the whole moat.

## Build doctrine (read before writing code)

- **Lean, not heavy.** The loop has earned a thin **stdlib HTTP server + web UI** and a **JSONL
  correction log on a volume** — that's the floor the learning loop needs, not scope creep. NO heavy
  framework (no FastAPI), NO database, NO auth service until something concrete forces it (the
  JSONL→SQLite trigger is documented in `bean/paths.py`). Deploy-once-before-abstracting.
- **Fit beats features.** The first operator had a full helpdesk product and never used it — the gap
  is fit + adoption, not capability. Win on *their* buckets, judgment and voice, not on a longer
  feature list.
- **Measure the loop, not the model.** The number that matters is the share of drafts approved
  untouched. A change that doesn't move it is a change that didn't matter.

## Stack

- Python 3.12+, `anthropic` SDK, `python-dotenv`.
- Models: a cheap classifier for the gate, a workhorse model for drafting. Decide the
  confidence-judgment tier empirically; don't pre-optimize.
- **Mail connection = forward + domain-auth (the helpdesk pattern), NOT stored credentials.**
  Bean never holds a mailbox password: the operator forwards their `support@` address to a Postmark
  inbound-parse address and verifies their domain (SPF/DKIM) so replies send *as* their brand.
  Provider-agnostic — works on Gmail, Outlook, and Proton via a custom domain.
- Secrets in `.env` (just `ANTHROPIC_API_KEY` — never commit). Persistence via `BEAN_DATA_DIR`,
  tenant directory via `BEAN_CUSTOMER`.

## Hard constraints

- Bean never sends. One-tap approve, always human-in-the-loop.
- `corrections.jsonl` is the brain — clear the inbox freely, never the corrections.
- No heavy framework, no database, no auth service. JSONL on a volume.
- **A shipped default must never contain real data.** A demo config once overwrote a live
  operator's config byte-for-byte, because the first save writes the default back.
- Real customer identity never enters the source tree — not in fixtures, not in tests, not in
  screenshots. `tests/test_config_snapshot.py` enforces this and is meant to fail loudly.
  The identifiers it forbids live in `tests/identity_denylist.json`, which is **gitignored on
  purpose**: a public repo carrying the tidy list of names the scrub removed is the scrub undone.
  **After a fresh private clone, restore that file** — without it the identity assertions skip
  rather than fail, and a skipped gate protects nothing. The captured-mail check next to it needs
  no denylist and runs everywhere.

## Where the reasoning lives

`roadmap.md` — what Bean does, what's next, and what it will never do.
`architecture.html` — the system map.
`docs/self-serve.md` — the honest distance between one operator and a stranger signing up.
Long comments in the source record the production incidents that shaped the code; they are the
most valuable thing in the repo. Keep them.

The `docs/` design records were deleted on 2026-08-05: ten documents, ~1,900 lines, almost all of
them marked "Historical design record" and describing the routing-tree engine that no longer
exists. They are in git history if a decision ever needs re-litigating. The rule they leave behind:
**a design record that outlives its design becomes archaeology.** Put the durable reasoning in the
source comment next to the code it explains, where it cannot rot separately from what it describes.
