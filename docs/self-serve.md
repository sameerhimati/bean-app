# What self-serve signup actually requires

Bean runs in production for one operator. Onboarding them took a person. This document is the
honest list of what stands between that and **a stranger signing up and getting a useful draft the
same day** — written so the size of the gap is visible rather than discovered one surprise at a time.

Nothing here is scheduled. It is a dependency map, not a plan.

## Where the line actually is

There are three products hiding behind the phrase "ready":

| | what it needs | rough size |
|---|---|---|
| **One operator** | done — it is running | shipped |
| **A handful, hand-onboarded** | one service per tenant, a person doing setup | days |
| **Self-serve signup** | everything below | months |

The middle row is the cheap one and is worth naming, because it is often what "we need customers"
actually means. A second operator today = one more Railway service, `BEAN_CUSTOMER=<them>`, a
Postmark inbound address, and someone walking them through a forwarding rule. That works now.

## The blockers, in dependency order

### 1. One tenant per process
`bean/paths.py` resolves the tenant from a single env var (`BEAN_CUSTOMER`) at import time, and every
path helper is `data_root / customer`. One process serves exactly one operator. This is deliberate —
the file's own comment says isolation is "an env var and a directory — deliberately NOT auth, signup,
billing, or a tenant system" — and it has been the right call. It is also the thing self-serve breaks.

Two honest options:

- **Service-per-tenant.** Keep the code as-is; provision a container per signup. No multi-tenancy
  code at all, real OS-level isolation, and a per-tenant cost floor of whatever the smallest instance
  costs. Fine to dozens, absurd at thousands.
- **Request-scoped tenancy.** Resolve the tenant per request from the session instead of from the
  environment. Touches every path helper, every module-level constant derived from one, and the
  correction/notebook/config caches. This is the real work, and it is where a cross-tenant data leak
  would come from if it comes from anywhere.

**Do not start this until a second paying operator exists.** Service-per-tenant is the correct
answer for longer than it feels like it should be.

### 2. There is no account
Auth today is a **single shared passcode** for the whole instance. There are no users, no sessions
tied to an identity, no password reset, no email verification, no ownership of a tenant by a person.

Self-serve needs the smallest real thing here: an identity, a session, and a mapping from identity →
tenant. Every "we'll add auth later" system pays for it here; the doctrine of no-auth-service was
right for one operator and stops being right at signup.

### 3. Inbound provisioning is manual
The connection model is deliberate and good — the operator forwards their support address to a
Postmark inbound-parse address, so **Bean never holds a mailbox credential**. What is manual is
everything around it:

- creating a Postmark inbound address per tenant, and routing its webhook to the right tenant
- issuing and storing that tenant's inbound token
- telling the operator what to paste into their mail provider, and **verifying it actually worked**

That last one is not cosmetic. Inbound silently stopped for **eleven days** because an upstream
forwarding rule stopped forwarding while continuing to display as enabled. A self-serve flow that
cannot detect "you set this up wrong" produces silent failures at signup, which is the worst possible
first impression.

### 4. Sending as their brand is designed, not built
Bean never sends — the operator copies the draft. That is a hard product constraint and is not
changing. But the domain-auth path (SPF/DKIM so a reply *would* look native) is design only. It is
not on the critical path for signup; it is on the critical path for the product being pleasant.

### 5. No billing
No plan, no metering, no card. Model calls cost real money per email, and the gate already keeps most
mail from reaching the expensive step — but an unmetered signup form is an unmetered bill.
At minimum: a per-tenant spend cap that fails closed, before a public signup exists.

### 6. The cold-start problem is half-solved
This is the one that decides whether self-serve is *worth* it.

A new store has no corrections, so there is no precedent to draft from. `bean/coldstart.py` handles
this deterministically — it projects the store's stated config (categories, macros, knowledge docs)
into a notebook and **deliberately leaves the judgment-notes section empty**, because inventing a
judgment note would forge exactly the signal the engine trusts most. Day zero, Bean knows what the
store says and nothing about what it does.

So a brand-new tenant's first drafts are grounded only in stated policy. The measured behaviour of the
one real operator is that **stated policy is not the same as their judgment** — they overrode their
own published warranty window based on how used the item looked. That gap is the product, and a new
tenant does not have it filled in yet.

What is built: cold-start projection, a questionnaire, and a notebook the operator edits directly.
What is missing: a path from "20 of your past replies" to a usable voice on day one, without a person
in the loop. That is the difference between a signup that converts and one that churns in a week.

## The order that makes sense

1. **A spend cap that fails closed.** Cheapest, and everything else is unsafe without it.
2. **Inbound provisioning + a setup verification that actually proves mail arrives.** The 11-day
   outage is the argument.
3. **Accounts.** Identity → tenant, one session, one owner.
4. **Service-per-tenant automation.** Keep the isolation; automate the provisioning.
5. **Cold-start from real replies.** The thing that decides retention.
6. **Billing.** Only once someone would pay.

Request-scoped multi-tenancy is deliberately absent from that list. It is the largest, riskiest item
and the one most likely to be unnecessary.

## What would tell us this is worth building

Not a waitlist count. The signal is whether the **one operator already using it keeps using it**, and
whether the share of drafts they send untouched goes up as their corrections accumulate. If that
number does not climb for the operator Bean was shaped around, it will not climb for a stranger who
signed up in ninety seconds.
