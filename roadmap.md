# Bean — Roadmap

Direction, not dates. Nothing here is a commitment; it is what the project is for, and what it
refuses to become.

## What Bean does today

All of this runs in production against a real store's live support mail.

**Receives mail without credentials.** The operator forwards their support address to a Postmark
inbound-parse address; Postmark POSTs each message to `/api/inbound`. Bean never holds a mailbox
password or an OAuth token.

**Gates before it thinks.** A cheap classifier decides whether an email needs a reply at all.
Newsletters, receipts and notifications are filed without ever reaching the expensive step, so
most mail costs almost nothing. Operators can override the gate with always-reply / always-file
rules, and Bean proposes new rules from the filing mistakes they correct.

**Drafts from a notebook.** The operator's judgment lives in one markdown file, in their own
words: the buckets they sort mail into, the cliffs where their answer changes, the facts about
their store, and notes on calls they have already made. Bean drafts against that, and cites which
part it leaned on.

**Says how sure it is, and what it isn't sure about.** Every draft carries a confidence level
and, when it is not confident, an explicit list of what it could not stand behind. A draft it
cannot ground is not dressed up as one it can.

**Learns from corrections.** Approvals, edits and rewrites append to a log that feeds later drafts
as examples. Corrections about *filing* feed the gate. The operator's edits are the training
signal; there is no separate labeling step.

**Never sends.** One-tap approve copies the reply out. A human sends it.

## What's next

**Prove the loop on one operator before adding a second.** The number that matters is the share
of drafts approved untouched. Until that is high and stable, more features are a distraction.

**Measure it honestly.** The approval rate has to be windowed to the engine currently running,
rather than averaged over every historical verdict including ones made by code that no longer
exists.

**Notice when it goes deaf.** Bean once stopped receiving mail for eleven days because an upstream
forwarding rule silently stopped forwarding. Bean reported the silence correctly the entire time
and nobody was reading it. The instrumentation is honest now; what it needs is a consumer that
tells a human.

**Self-serve onboarding.** Today a new store is set up by hand. The path from "forward your mail
here" to a first useful draft should not require the author's involvement.

## What Bean will never do

This list is the point. It is what makes the thing usable rather than merely capable.

**Send an email on its own.** Not behind a setting, not above a confidence threshold, not for
"obviously safe" categories. The moment an assistant *can* send, every draft has to be reviewed as
though it were about to go out — which is exactly the work it was supposed to remove.

**Fake an answer it cannot ground.** An assistant that always returns something confident-looking
is worse than no assistant, because the operator must re-read everything to catch the
plausible-sounding mistakes. Bean is allowed to say it does not know. That has a real cost — it
will sometimes escalate mail it could have handled — and the cost is worth paying.

**Become a helpdesk.** No ticketing, no SLAs, no queues, no shared inboxes, no agent seats. Those
products exist and are good at that. Bean is the drafting layer for someone who has already
decided they do not want one.

**Become a chief-of-staff stack.** Not a project manager, not a CRM, not an inbox for every kind
of work. Triage → draft → approve is the entire surface. Each adjacent feature makes it worse at
the one thing it is for.

**Hold mailbox credentials.** No stored passwords, and no OAuth token carrying read access to an
entire mailbox. Forwarding hands Bean exactly the mail it was sent and nothing else, and it is
revoked by deleting one rule.

**Train a shared model on one operator's mail.** Learning is per-operator and stays in that
operator's own files. What Bean learns from one store does not surface in another's drafts.

## Design constraints

Deliberate ceilings, held until something concrete breaks them:

- **No database.** JSON and JSONL on a volume. The condition that would justify one is recorded
  in the code rather than left to taste.
- **No web framework.** The standard library's HTTP server.
- **No auth service.** A passcode, plus process isolation where isolation genuinely matters.
- **No build step.** React loaded from plain `.jsx` files as ordinary scripts.

Each is cheap to add the day it is needed and expensive to remove once added.
