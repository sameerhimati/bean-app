"""Split a quoted email history into the real messages it actually contains.

`bean/inbound.py` splits an inbound reply into `body` (the latest message) and `thread` (everything
from the first "On … wrote:" onward, kept whole). That was written for the model, which reads a wall
of `>` fine. The review UI does not, and neither, it turns out, does the prompt:

  - Measured on the live corpus: 108 emails carry a thread and EVERY ONE has `len(thread) == 1`,
    because the whole history is one un-split string. Un-nest the quote markers and those single
    blobs contain up to THIRTEEN real messages; 61 of the 108 contain two or more. The UI label
    "· 1 message" was never once telling the truth.
  - `engine._user_message` announces the block as "EARLIER IN THIS THREAD (oldest first)". A raw
    quoted blob is the exact opposite — outermost is newest, and each nesting level goes further
    back. The model was told oldest-first and handed newest-first.

So: one pure function that turns the blob into an ordered list of attributed messages, oldest→newest.
Lossless on purpose — no truncation here. Display trimming is the UI's job (it needs an expander
either way) and the prompt wants the whole text; a parser that quietly drops content is the
data-captured-then-dropped bug this module exists to fix.

Attribution comes from the delimiter line, which describes the message that FOLLOWS it. There is no
store support address in config to compare against, so a message is the customer's when its address
matches theirs and the other side's when it doesn't.

`side` is deliberately THREE-valued, not a bool. Measured on the corpus: only 134 of 222 delimiter
lines carry an angle-bracketed address at all, so a two-valued "is it the customer" would resolve 88
real messages to "no" by default — and since the UI collapses the other side, that default would
hide things the customer actually said. Unknown stays unknown and renders expanded. Collapse what
you have positively identified as boilerplate; never collapse on an absence of evidence.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

# Where one message ends and the quoted one begins. Covers the common clients: Gmail/Proton/Apple
# "On <date> <someone> wrote:", Outlook's "-----Original Message-----", and a bare underscore rule.
# Defined HERE and imported by inbound.py — the split and the parse must never drift apart.
THREAD_DELIM_RE = re.compile(
    r"(?ms)^[ \t]*On\b.{0,400}?\bwrote:[ \t]*$"
    r"|^[ \t]*-{2,}\s*Original Message\s*-{2,}"
    r"|^[ \t]*_{5,}",
)

# One level of quote markers, one line at a time. Stripped once per recursion, not repeatedly:
# the nesting depth IS the thread depth, so eating it all at once would flatten the structure we
# are here to recover.
_QUOTE_LEVEL_RE = re.compile(r"(?m)^[ \t]*>[ \t]?")

# "On Fri, Aug 14, 2026 at 4:45 PM Northwind Support <hello@example.com> wrote:"
# ONE pattern for the whole family, deliberately. Splitting the timestamp from the name with regex
# alternation does not work: `when` is non-greedy, so it stops at the first thing that lets `who`
# match and the byline comes out as "Aug 14, 2026 … Support". The date/name boundary is decided by
# `_split_name_and_date` instead, which walks back from the end over tokens that still look like a
# name — that boundary is a token question, not a character-class one.
_ON_WROTE_RE = re.compile(r"(?is)^\s*On\s+(?P<rest>.*?)\s*wrote:")
# Every address-shaped run, bracketed or bare. Applied with `sub`, not a single `search`: the
# HTML→text strip in inbound.py turns `<a href="mailto:x">x</a>` into TWO address-shaped runs on one
# line, and removing only the first left "<mailto:someone@example.com>" sitting in the byline.
_ADDR_RE = re.compile(r"<\s*(?:mailto:)?(?P<addr>[^<>\s@]+@[^<>\s]+?)\s*>|(?:mailto:)?(?P<bare>\S+@\S+\.\S+)")

# A bare underscore rule is Outlook's separator AND the most common signature separator in the
# corpus. Treating it as a message boundary unconditionally turned every signature block into a
# phantom "message" — one real thread reported 17 where it holds 13. So it only counts as a
# boundary when an Outlook header block actually follows it.
_UNDERSCORE_RULE_RE = re.compile(r"^[ \t]*_{5,}")

# Outlook drops a header block after its "-----Original Message-----" rule instead of an On-line.
_HDR_FROM_RE = re.compile(r"(?im)^\s*From:\s*(?P<who>.*?)\s*(?:<(?P<addr>[^<>\s]+@[^<>\s]+)>)?\s*$")
_HDR_SENT_RE = re.compile(r"(?im)^\s*(?:Sent|Date):\s*(?P<when>.*?)\s*$")
_HDR_LINE_RE = re.compile(r"(?im)^\s*(?:From|Sent|Date|To|Cc|Subject|Reply-To):.*$")

# A quoted chain is deep, not infinite. Real mail tops out at 13 levels in the live corpus; past
# this we stop descending and keep the remainder as one message rather than recursing forever on
# something pathological. Nothing is dropped either way.
_MAX_DEPTH = 25

# Tokens that are part of a timestamp, not part of a name — used to find where the date stops and
# the sender starts on an On-line that carries no angle-bracketed address.
_TIME_WORDS = frozenset({"at", "am", "pm", "a.m.", "p.m.", "utc", "gmt", "est", "edt", "pst", "pdt"})


CUSTOMER, OTHER, UNKNOWN = "customer", "other", "unknown"


@dataclass(frozen=True)
class QuotedMessage:
    """One real message recovered from the quoted history.

    `who`/`when` are empty when the delimiter line was unparseable (11 of 108 real blobs have no
    recognizable delimiter at all) — the UI renders those without a byline rather than guessing.
    `side` is CUSTOMER / OTHER / UNKNOWN; see the module docstring for why UNKNOWN is a real state.
    """

    who: str
    when: str
    text: str
    side: str = UNKNOWN


def _unquote_one_level(text: str) -> str:
    return _QUOTE_LEVEL_RE.sub("", text)


def _unquote_fully(text: str) -> str:
    """Peel quote markers off a stretch with no delimiter left in it.

    The recursion normally eats exactly one level per descent, because the nesting depth IS the
    thread depth. But a run with nothing left to split — a blob that is quoted top to bottom and
    names no sender, or the tail past `_MAX_DEPTH` — has no structure left to preserve, so the
    markers are just noise on the reader's screen. Only fires when EVERY non-blank line is quoted;
    a message that merely quotes a line or two keeps its `>` exactly where the writer put it.
    """
    while True:
        lines = [ln for ln in text.splitlines() if ln.strip()]
        if not lines or not all(ln.lstrip().startswith(">") for ln in lines):
            return text
        text = _unquote_one_level(text)


def _split_name_and_date(rest: str) -> tuple[str, str]:
    """"Fri, Aug 14, 2026 at 4:45 PM Northwind Support" → ("Northwind Support", the date).

    Walks back from the end while the token still looks like part of a name (no digits, not a
    timezone/at/AM). Everything before that is the timestamp. Crude, but it only ever runs on the
    minority of On-lines with no address to key off, and a wrong guess costs a byline — never text.
    """
    tokens = rest.replace("\n", " ").split()
    cut = len(tokens)
    while cut > 0:
        tok = tokens[cut - 1].strip(",").lower()
        if not tok or any(ch.isdigit() for ch in tok) or tok in _TIME_WORDS:
            break
        cut -= 1
    who = " ".join(tokens[cut:]).strip(" ,")
    when = " ".join(tokens[:cut]).strip(" ,")
    # All-name or all-date is more likely a parse we should not trust than a real one-word case.
    return (who, when) if who and when else ("", rest.strip(" ,"))


def _parse_delimiter(line: str) -> tuple[str, str, str]:
    """(who, when, addr) from a delimiter line. Blanks where it could not be read."""
    m = _ON_WROTE_RE.match(line)
    if not m:
        return "", "", ""  # Outlook rule / underscore rule — attribution is in the header block
    rest = m.group("rest")
    found: list[str] = []

    def take(match: re.Match) -> str:
        found.append((match.group("addr") or match.group("bare") or "").strip(" <>,"))
        return " "

    rest = _ADDR_RE.sub(take, rest).strip()  # every address out; name and date are what's left
    who, when = _split_name_and_date(rest)
    return who.strip(" ,\"'"), when, (found[0] if found else "")


def _take_outlook_headers(text: str) -> tuple[str, str, str, str]:
    """Pull a leading `From:/Sent:/To:/Subject:` block off an Outlook-quoted message.

    Returns (who, when, addr, remaining text). Only consumes the block when it actually starts with
    `From:` — a message whose body merely mentions "From:" keeps every byte.
    """
    if not _HDR_FROM_RE.match(text):
        return "", "", "", text
    lines = text.splitlines()
    consumed = 0
    for line in lines:
        if not line.strip() or _HDR_LINE_RE.match(line):
            consumed += 1
            continue
        break
    block, rest = "\n".join(lines[:consumed]), "\n".join(lines[consumed:])
    fm, sm = _HDR_FROM_RE.search(block), _HDR_SENT_RE.search(block)
    who = (fm.group("who") if fm else "").strip(" ,\"'")
    addr = (fm.group("addr") or "" if fm else "").strip()
    when = (sm.group("when") if sm else "").strip()
    return who, when, addr, rest.strip()


def _side(addr: str, who: str, customer_email: str, customer_name: str) -> str:
    """Whose side of the conversation this message is. An address decides it outright; a bare name
    can only ever promote to CUSTOMER (a name that isn't theirs may still be theirs under a display
    name we've never seen). Everything else stays UNKNOWN rather than guessing OTHER, because OTHER
    is what the UI collapses."""
    if addr and customer_email:
        return CUSTOMER if addr.strip().lower() == customer_email.strip().lower() else OTHER
    if who and customer_name and who.strip().lower() == customer_name.strip().lower():
        return CUSTOMER
    return UNKNOWN


def _next_delimiter(text: str) -> re.Match | None:
    """The next real message boundary, skipping underscore rules that are only signature separators.

    Outlook's rule is `_____` followed by a `From:` header block. A signature is `_____` followed by
    a name and a phone number. Only the first is a message.
    """
    pos = 0
    while (m := THREAD_DELIM_RE.search(text, pos)) is not None:
        if not _UNDERSCORE_RULE_RE.match(m.group(0)):
            return m
        if _HDR_FROM_RE.match(_unquote_fully(text[m.end():]).strip()):
            return m
        pos = m.end()
    return None


def _walk(
    text: str, customer_email: str, customer_name: str, depth: int
) -> tuple[str, list[QuotedMessage]]:
    """(trailing text belonging to the CALLER's message, messages below it) — newest→oldest.

    The head/message split is the subtle part. At any level the layout is:

        …tail of the message the enclosing delimiter named…
        On <date> <someone> wrote:      ← names the writer of everything below
        > …quoted content…

    So text before a delimiter is never its own message — it is the rest of a message whose byline
    lives one level OUT. Returning it to the caller instead of emitting it is what stopped every
    signature block from becoming a phantom message with no sender.
    """
    m = None if depth >= _MAX_DEPTH else _next_delimiter(text)
    if m is None:
        return _unquote_fully(text).strip(), []

    head = text[: m.start()]
    who, when, addr = _parse_delimiter(m.group(0))
    body, deeper = _walk(_unquote_one_level(text[m.end():]), customer_email, customer_name, depth + 1)
    # An Outlook rule names nobody; the real sender is in the header block that follows it.
    if not who and not when:
        who, when, addr, body = _take_outlook_headers(body)
    side = _side(addr, who, customer_email, customer_name)
    below = ([QuotedMessage(who, when, body.strip(), side)] if body.strip() else []) + deeper
    return head, below


def split_quoted(blob: str, customer_email: str = "", customer_name: str = "") -> list[QuotedMessage]:
    """The real messages inside a quoted history, OLDEST FIRST.

    Lossless: every non-blank stretch of the input comes back in exactly one message. Empty input,
    or input that is nothing but quote markers, returns []. Never raises — this runs on the inbound
    webhook path and on every inbox fetch, and a malformed chain must degrade to "one message" and
    not take down the mail.
    """
    if not (blob or "").strip():
        return []
    try:
        head, messages = _walk(blob, customer_email or "", customer_name or "", 0)
    except RecursionError:  # pathological nesting past what _MAX_DEPTH anticipated
        return [QuotedMessage("", "", blob.strip(), UNKNOWN)]
    if head.strip():
        # Text ahead of the first delimiter is the tail of the email's OWN latest message — usually
        # the sender's signature, which inbound._split_thread cut into `thread` because a signature
        # rule looks like a thread boundary. It is theirs, so say so rather than showing it
        # bylineless. Only when something was quoted BELOW it, though: a blob with no delimiter
        # anywhere is the unrecognized-client case, where we know nothing and should claim nothing.
        who, side = (customer_name or "", CUSTOMER) if messages else ("", UNKNOWN)
        messages.insert(0, QuotedMessage(who, "", head.strip(), side))
    messages.reverse()
    return messages


def render_for_prompt(messages: list[QuotedMessage]) -> list[str]:
    """The de-quoted messages as the strings the engine and the gate put in front of the model.

    Keeps the existing `list[str]` shape both consumers already take (bean/engine.py:_user_message,
    bean/gate.py), so nothing downstream changes type — but the list is now genuinely oldest-first
    and one entry per message, which is what those call sites have always claimed it was.
    """
    out = []
    for m in messages:
        byline = " · ".join(p for p in (m.who, m.when) if p)
        out.append(f"[{byline}]\n{m.text}" if byline else m.text)
    return out


def thread_for_prompt(raw_thread: list[str], sender_email: str = "", sender_name: str = "") -> list[str]:
    """A stored `thread` field, ready to put in front of a model: de-quoted, one entry per real
    message, oldest first, each bylined.

    Call this ONCE, at the seam that builds the prompt — the output is prose, not quoted mail, so
    running it over its own result would collapse the whole list back into a single message.
    """
    return render_for_prompt(split_quoted("\n".join(raw_thread or []), sender_email, sender_name))
