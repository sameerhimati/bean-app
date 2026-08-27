"""conversationKey / conversationOf / conversationIds / folded / unansweredCount, under node.

These decide what the queue folds away and what it flags, so their failure mode is a real customer
email that never gets looked at. The keying MUST agree with bean/conversation.py — the server groups
a conversation to draft it once, and the client groups it again to render and to act on it; if the
two disagree, a message is folded into a draft that never covered it.

The REAL source is executed under node (mount/globals stubbed), so the test can't drift from what
ships. Skips (never lies) when node is absent.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
from pathlib import Path

import pytest

from bean.conversation import conversation_key

_WEB = Path(__file__).parent.parent / "web"

pytestmark = pytest.mark.skipif(shutil.which("node") is None, reason="node not installed")

_HARNESS = r"""
const fs = require('fs');
const path = require('path');
const WEB = process.env.BEAN_WEB;

global.window = { friendlyKind: k => k, beanTimeMs: t => (t ? Date.parse(t) : null) };
global.React = { createElement: () => null };
new Function('window', 'React', fs.readFileSync(path.join(WEB, 'bean-inbox.jsx'), 'utf8'))
  .call(global.window, global.window, global.React);

const { conversationKey, conversationOf, conversationIds, folded, unansweredCount } = window;

const mk = (id, subject, opts = {}) => Object.assign({
  id, subject,
  from: { name: 'Dana', email: opts.sender || 'dana@example.com' },
  time: opts.time || '2026-07-09T10:00:00Z',
  conversation: opts.conversation || [],
  rolledInto: opts.rolledInto || '',
  filed: !!opts.filed,
}, {});

// a→b→c: one conversation, nobody replied, b and c folded into... nothing yet.
const chase = [
  mk('a', 'Return Label', { time: '2026-07-09T10:00:00Z' }),
  mk('b', 'Re: Return Label', { time: '2026-07-10T10:00:00Z' }),
  mk('c', 'RE: [EXTERNAL] Re: Return Label', { time: '2026-07-11T10:00:00Z' }),
];
const rolled = [
  mk('a', 'Return Label', { time: '2026-07-09T10:00:00Z', rolledInto: 'c' }),
  mk('b', 'Re: Return Label', { time: '2026-07-10T10:00:00Z', rolledInto: 'c' }),
  mk('c', 'Re: Return Label', { time: '2026-07-11T10:00:00Z' }),
];
const answered = [
  mk('a', 'Return Label', { time: '2026-07-09T10:00:00Z' }),
  mk('b', 'Re: Return Label', { time: '2026-07-10T10:00:00Z',
      conversation: [{ who: 'Support', when: '', text: 'On it.', side: 'other' }] }),
];
const unknownSide = [
  mk('a', 'Return Label', { time: '2026-07-09T10:00:00Z' }),
  mk('b', 'Re: Return Label', { time: '2026-07-10T10:00:00Z',
      conversation: [{ who: '', when: '', text: 'hm', side: 'unknown' }] }),
];

const byId = (list, id) => list.find(e => e.id === id);

console.log(JSON.stringify({
  keys: {
    plain: conversationKey(mk('x', 'Return Label')),
    re: conversationKey(mk('x', 'Re: Return Label')),
    stacked: conversationKey(mk('x', 'RE: [EXTERNAL] Re: Fwd: Return Label')),
    otherSender: conversationKey(mk('x', 'Return Label', { sender: 'sam@example.com' })),
    upperSender: conversationKey(mk('x', 'Return Label', { sender: 'DANA@Example.com' })),
  },
  convo_ids: conversationOf(byId(chase, 'c'), chase).map(e => e.id),
  ids_for_action_rolled: conversationIds(byId(rolled, 'c'), rolled).sort(),
  ids_for_action_unrolled: conversationIds(byId(chase, 'c'), chase).sort(),
  folded_when_target_present: folded(byId(rolled, 'a'), rolled),
  folded_when_target_absent: folded(byId(rolled, 'a'), rolled.filter(e => e.id !== 'c')),
  folded_without_rolledInto: folded(byId(chase, 'a'), chase),
  chase_on_newest: unansweredCount(byId(chase, 'c'), chase, {}),
  chase_on_older: unansweredCount(byId(chase, 'a'), chase, {}),
  chase_after_she_sent_one: unansweredCount(byId(chase, 'c'), chase, { a: 'approved' }),
  chase_when_store_spoke_last: unansweredCount(byId(answered, 'b'), answered, {}),
  chase_when_side_unknown: unansweredCount(byId(unknownSide, 'b'), unknownSide, {}),
  chase_ignores_filed: unansweredCount(
    byId(chase, 'c'), [mk('f', 'Return Label', { filed: true, time: '2026-07-08T10:00:00Z' }), ...chase], {}),
}));
"""


def _run() -> dict:
    proc = subprocess.run(
        ["node", "--input-type=commonjs", "-e", _HARNESS], capture_output=True, text=True,
        env={**os.environ, "BEAN_WEB": str(_WEB)},
    )
    if proc.returncode != 0:
        raise AssertionError(f"node failed:\n{proc.stderr}")
    return json.loads(proc.stdout)


def test_the_key_matches_the_python_one_byte_for_byte():
    """The load-bearing agreement. The server groups a conversation to draft it once; the client
    groups it again to render and to act on it. Two different implementations of one key is exactly
    the shape that drifts, so pin them against each other."""
    keys = _run()["keys"]
    assert keys["plain"] == conversation_key("dana@example.com", "Return Label")
    assert keys["re"] == conversation_key("dana@example.com", "Re: Return Label")
    assert keys["stacked"] == conversation_key("dana@example.com", "RE: [EXTERNAL] Re: Fwd: Return Label")
    assert keys["upperSender"] == conversation_key("DANA@Example.com", "Return Label")


def test_reply_prefixes_collapse_but_a_different_sender_does_not():
    keys = _run()["keys"]
    assert keys["plain"] == keys["re"] == keys["stacked"] == keys["upperSender"]
    assert keys["otherSender"] != keys["plain"]


def test_conversation_members_come_back_oldest_first():
    assert _run()["convo_ids"] == ["a", "b", "c"]


def test_one_action_covers_every_message_its_draft_answered():
    r = _run()
    # Rolled in ⇒ approving 'c' must mark a and b too; they no longer render a draft of their own.
    assert r["ids_for_action_rolled"] == ["a", "b", "c"]
    # Not rolled in ⇒ each still has its own draft, so acting on 'c' acts on 'c' alone.
    assert r["ids_for_action_unrolled"] == ["c"]


def test_folding_needs_the_draft_that_answered_it_to_be_on_screen():
    r = _run()
    assert r["folded_when_target_present"] is True
    # Filtered out, or already sent: the row stands on its own rather than vanishing with nothing
    # left to open. Silent-hide is the worse trust failure.
    assert r["folded_when_target_absent"] is False
    assert r["folded_without_rolledInto"] is False


def test_the_chase_badge_counts_once_on_the_newest_message():
    r = _run()
    assert r["chase_on_newest"] == 3
    assert r["chase_on_older"] == 0  # flagged once per conversation, not once per row


def test_a_reply_she_already_sent_ends_the_chase():
    assert _run()["chase_after_she_sent_one"] == 2


def test_a_store_reply_in_the_quoted_history_ends_the_chase():
    # She answers plenty of mail in Proton without marking it here, so "no status" is not proof
    # nobody replied. This mirrors conversation.store_spoke_last.
    assert _run()["chase_when_store_spoke_last"] == 0


def test_unknown_attribution_is_not_read_as_a_reply():
    # Only a POSITIVE identification counts, or a real chase gets silently downgraded.
    assert _run()["chase_when_side_unknown"] == 2


def test_filed_mail_never_counts_toward_a_chase():
    assert _run()["chase_ignores_filed"] == 3
