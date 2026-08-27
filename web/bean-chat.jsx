// Bean chat — the floating surface where the operator TELLS Bean things.
//
// The shape of a turn is the whole design. Most turns are not questions, they are the operator
// saying a rule changed ("we replace broken headphones within 60 days now"). Bean must not reply
// "Got it!" and quietly write to her brain — that is the auto-send failure wearing a friendlier
// hat. A turn RESOLVES INTO A ProposalCard: the claim in her words, its provenance, what it will
// change, and confirm · fix · decline. Declining writes nothing. Same invariant as the draft view:
// **Bean does the writing, the operator does the judging.**
//
// ⚠️ DUMB BY CONSTRUCTION — the same moat as ProposalCard, BeanNotebook and BeanCiteSheet. This
// component NEVER fetches and NEVER persists. It renders the `messages` it is handed and calls
// onSend / onConfirmProposal / onDeclineProposal / onFixProposal / onOpenCite. Open state and the
// transcript live in bean-root (which is why they survive inbox → draft → stats), and the notebook
// ETag stays with the one writer up there. Do NOT add a fetch here.
//
// Mounts in bean-root's root Fragment beside Toast, OUTSIDE the view switch.

const _CR = (typeof React !== 'undefined' && React) || window.React;
const { useState: useChatState, useEffect: useChatEffect, useRef: useChatRef } = _CR;

// How a citation reads on a chip in the transcript. Same vocabulary as bean-cite.jsx's CITE_KINDS —
// a chip that says one thing here and another there is a small lie about the grounding.
const CHAT_CITE = {
  notebook: { icon: '📓', ink: '#5A5044', bg: '#F4EEE2', line: '#E4DCCB' },
  corpus:   { icon: '📎', ink: '#3F6B54', bg: '#EAF3EC', line: '#CFE4D5' },
  store:    { icon: '🏷', ink: '#6E4327', bg: '#F1E7D6', line: '#E2D0B6' },
};

function chatCiteLabel(cite) {
  const raw = String(cite || '');
  const i = raw.indexOf(':');
  const kind = i < 0 ? 'notebook' : raw.slice(0, i);
  const label = i < 0 ? raw : raw.slice(i + 1);
  return { kind: CHAT_CITE[kind] ? kind : 'notebook', label: label || 'a note' };
}

function CiteChip({ cite, onOpen }) {
  const { kind, label } = chatCiteLabel(cite);
  const c = CHAT_CITE[kind];
  return _CR.createElement('button', {
    type: 'button', className: 'chat-cite', onClick: () => onOpen && onOpen(cite),
    style: { color: c.ink, background: c.bg, borderColor: c.line },
  }, _CR.createElement('span', { 'aria-hidden': true }, c.icon), label);
}

// Bean's side of the transcript: no bubble. She is the page; the operator is the visitor.
function BeanTurn({ children, mark }) {
  return _CR.createElement('div', { className: 'chat-turn chat-turn-bean' },
    _CR.createElement('div', { className: 'chat-gutter' }, mark),
    _CR.createElement('div', { className: 'chat-said' }, children));
}

function BeanChat({
  open = false,
  onOpen, onClose,
  messages = [],
  thinking = false,
  pending = 0,
  onSend, onConfirmProposal, onDeclineProposal, onFixProposal, onOpenCite,
  className = '',
}) {
  const h = _CR.createElement;
  const [text, setText] = useChatState('');
  const launcherRef = useChatRef(null);
  const inputRef = useChatRef(null);
  const scrollRef = useChatRef(null);
  const wasOpen = useChatRef(open);

  // Focus goes into the composer on open and back to the launcher on close — the launcher is the
  // thing she pressed, so it is the thing that should be under her hands again afterwards.
  useChatEffect(() => {
    if (open && !wasOpen.current && inputRef.current) inputRef.current.focus();
    if (!open && wasOpen.current && launcherRef.current) launcherRef.current.focus();
    wasOpen.current = open;
  }, [open]);

  useChatEffect(() => {
    if (!open) return;
    const onKey = e => { if (e.key === 'Escape') { e.stopPropagation(); onClose && onClose(); } };
    document.addEventListener('keydown', onKey);
    return () => document.removeEventListener('keydown', onKey);
  }, [open, onClose]);

  useChatEffect(() => {
    const el = scrollRef.current;
    if (el) el.scrollTop = el.scrollHeight;
  }, [messages.length, thinking, open]);

  function fit(el) { if (el) { el.style.height = 'auto'; el.style.height = Math.min(el.scrollHeight, 132) + 'px'; } }

  function send() {
    const t = text.trim();
    if (!t) return;
    setText('');
    if (inputRef.current) { inputRef.current.style.height = 'auto'; }
    onSend && onSend(t);
  }

  const mark = size => window.BeanMark
    ? h(window.BeanMark, { size, color: '#C4A882' })
    : h('span', { style: { width: size, height: size, display: 'block' } });

  const empty = h('div', { className: 'chat-empty' },
    window.BeanMark ? h(window.BeanMark, { size: 40, bob: true }) : null,
    h('div', { className: 'chat-empty-lead' }, 'Tell me when something changes.'),
    h('div', { className: 'chat-empty-sub' }, 'Policies, prices, what to say when someone’s angry. Me writes nothing down until you say it’s right.'));

  const body = messages.map(m => {
    if (m.from === 'you') {
      return h('div', { className: 'chat-turn chat-turn-you', key: m.id },
        h('div', { className: 'chat-bubble' }, m.text));
    }
    if (m.kind === 'proposal') {
      return h(BeanTurn, { key: m.id, mark: mark(20) },
        m.text ? h('p', { className: 'chat-line' }, m.text) : null,
        h('div', { style: { marginTop: 9 } },
          window.ProposalCard ? h(window.ProposalCard, {
            claim: m.claim,
            provenance: m.provenance || 'stated',
            receipts: m.receipts,
            consequence: m.consequence,
            confirmLabel: m.confirmLabel,
            declineLabel: m.declineLabel,
            onConfirm: claim => onConfirmProposal && onConfirmProposal(m, claim),
            onFix: () => onFixProposal && onFixProposal(m),
            onDecline: () => onDeclineProposal && onDeclineProposal(m),
          }) : null));
    }
    return h(BeanTurn, { key: m.id, mark: mark(20) },
      h('p', { className: 'chat-line' }, m.text),
      m.cites && m.cites.length
        ? h('div', { className: 'chat-cites' },
            h('span', { className: 'chat-cites-lead' }, 'Grounded in'),
            m.cites.map((c, i) => h(CiteChip, { key: i, cite: c, onOpen: onOpenCite })))
        : null);
  });

  return h('div', { className: 'bean-chat-root' + (open ? ' is-open' : '') + (className ? ' ' + className : '') },
    h('button', {
      ref: launcherRef, type: 'button', className: 'bean-chat-launcher',
      'aria-label': pending
        ? 'Talk to Bean — ' + pending + ' waiting on you'
        : 'Talk to Bean',
      'aria-expanded': open,
      onClick: () => (open ? onClose && onClose() : onOpen && onOpen()),
    },
      // Keeps roasting the whole time the panel is open, not just while a turn is in flight.
      //
      // `thinking` alone was the honest-status reading of it, but it made the corner dead for most
      // of a conversation. While she has Bean open, Bean being visibly alive down here is the
      // point — and it cannot be mistaken for a status claim, because the panel above it is already
      // saying "me thinking…" or "me listens" in words.
      //
      // The compact loop, not the full journey: at 30px a dripper and a cup are a smudge, and this
      // one runs continuously rather than for six seconds, so it has to stay quiet.
      window.BeanRoast
        ? h(window.BeanRoast, { size: 30, mode: 'roast', running: open || thinking })
        : (window.BeanMark ? h(window.BeanMark, { size: 30, bob: !open }) : null),
      pending ? h('span', { className: 'bean-chat-dot', 'aria-hidden': true }) : null),

    h('div', { className: 'bean-chat-panel', role: 'dialog', 'aria-label': 'Talk to Bean', 'aria-hidden': !open },
      h('div', { className: 'chat-head' },
        // Roasts in the header too, so the panel says "still going" wherever she is looking in it.
        window.BeanRoast
          ? h(window.BeanRoast, { size: 22, mode: 'roast', running: thinking })
          : (window.BeanMark ? h(window.BeanMark, { size: 22 }) : null),
        h('div', { style: { flex: 1, minWidth: 0 } },
          h('div', { className: 'chat-head-title' }, 'Bean'),
          h('div', { className: 'chat-head-sub' }, thinking ? 'me thinking…' : 'me listens')),
        h('button', { type: 'button', className: 'chat-x', onClick: () => onClose && onClose(), 'aria-label': 'Close' }, '×')),

      h('div', { className: 'chat-scroll', ref: scrollRef, 'aria-live': 'polite' },
        messages.length ? body : empty,
        thinking ? h('div', { className: 'chat-turn chat-turn-bean chat-thinking' },
          h('div', { className: 'chat-gutter' },
            window.BeanRoast
              ? h(window.BeanRoast, { size: 20, mode: 'roast', running: true })
              : null),
          h('div', { className: 'chat-said' }, h('p', { className: 'chat-line chat-line-faint' }, 'me thinking…'))) : null),

      h('div', { className: 'chat-composer' },
        h('textarea', {
          ref: inputRef, rows: 1, value: text, className: 'chat-input',
          placeholder: 'Tell me what changed…',
          onChange: e => { setText(e.target.value); fit(e.target); },
          onKeyDown: e => {
            if (e.key === 'Enter' && !e.shiftKey) { e.preventDefault(); send(); }
          },
        }),
        h('button', {
          type: 'button', className: 'chat-send', onClick: send,
          disabled: !text.trim(), 'aria-label': 'Send',
        }, '↑'))));
}

window.BeanChat = BeanChat;
