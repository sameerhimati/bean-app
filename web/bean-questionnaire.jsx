// Bean questionnaire — the onboarding walk, and the operator's approval conversation for their
// notebook.
//
// After distillation Bean has a whole notebook it inferred from their mail. Handing them that
// document to proofread is the taxonomy tax again: a page of buckets, cliffs, macros, facts and
// notes, and no idea which parts they're agreeing to. So the walk asks it back one claim at a time,
// in the one idiom (ProposalCard): "me thinks this is how you decide — true?" · confirm · fix · not
// now. Completing the walk IS the approval — for the operator this is gate F's pending human step.
//
// ⚠️ ONE PUT AT COMPLETION (the consent invariant). The walk edits a WORKING COPY; nothing persists
// until they finish the last card, which fires a single onComplete(approvedNotebook) — one ETag'd
// save, their approval. Per-card saving was rejected on purpose: it would rotate the ETag a dozen times
// and leave a half-approved notebook that Bean would start drafting from mid-walk. (The every-confirm-
// is-a-PUT doctrine still governs the DIARY's proposal cards — a different surface, already approved.)
//
// ⚠️ DUMB BY CONSTRUCTION: no fetch, no persist, no ETag. Same seam as BeanNotebook's onSave.

const { useState: useQState } = React;

// The sign-off card. Not a checkbox — a question whose ANSWER is worth keeping: what Bean should do
// when it isn't sure. Her answer lands as a judgment note (`stated` — she told us, right here), so
// the approval step also teaches the one rule that governs every red.
const SIGNOFF_PREFIX = 'When unsure: ';

function buildCards(nb) {
  const cards = [];
  (nb.buckets || []).forEach((b, i) => cards.push({
    kind: 'buckets', index: i, field: 'cliff',
    section: 'How me routes your mail',
    lead: 'me thinks this is how you handle ' + (b.name ? '“' + b.name + '”' : 'these') + ' — true?',
    claim: b.cliff || '',
    // The one consequence worth previewing here: a high-stakes bucket never one-taps, however sure
    // Bean is. That's a promise about her money, so say it while she's deciding.
    consequence: b.stakes === 'high'
      ? 'Me will always check with you before sending one of these, even when me’s sure.'
      : null,
  }));
  (nb.macros || []).forEach((m, i) => cards.push({
    kind: 'macros', index: i, field: 'text',
    section: 'Your standard answers',
    lead: 'me thinks this is your usual reply for ' + (m.name ? '“' + m.name + '”' : 'this') + ' — true?',
    claim: m.text || '',
  }));
  (nb.facts || []).forEach((f, i) => cards.push({
    kind: 'facts', index: i, field: 'text',
    section: 'Things about your store',
    lead: 'me may tell customers this — true?',
    claim: f.text || '', provenance: f.provenance,
  }));
  (nb.notes || []).forEach((n, i) => cards.push({
    kind: 'notes', index: i, field: 'text',
    section: 'How you like things said',
    lead: 'me noticed this about how you reply — true?',
    claim: n.text || '', provenance: n.provenance,
  }));
  cards.push({ kind: 'signoff', section: 'One last thing' });
  return cards;
}

// Where to drop her back in when she reopens a half-finished walk: the first content card she hasn't
// yet confirmed OR left out. If she's answered them all, that's the sign-off card (the end). Keys
// match keep/leaveOut — `kind:index` — so a card decided last session is skipped this session.
function firstUnanswered(cards, progress) {
  const patched = (progress && progress.patched) || {};
  const dropped = (progress && progress.dropped) || {};
  for (let n = 0; n < cards.length; n++) {
    const c = cards[n];
    if (c.kind === 'signoff') return n;  // every content card decided → straight to sign-off
    const key = c.kind + ':' + c.index;
    if (patched[key] === undefined && !dropped[key]) return n;
  }
  return Math.max(0, cards.length - 1);
}

// The resume hint on the 🫘 Teach Bean button: how many cards she still has to decide — but ONLY once
// she has actually started. null before her first tap, and null again after she approves (the server
// clears her progress at approval, so an empty overlay means "not mid-walk", never "45 to go"). Uses
// firstUnanswered's keys and its decided-rule, so the button can't disagree with where the walk
// resumes. The sign-off card is excluded: it's the closing question, not a claim waiting on her.
function walkRemaining(notebook, progress) {
  const patched = (progress && progress.patched) || {};
  const dropped = (progress && progress.dropped) || {};
  if (!notebook || (!Object.keys(patched).length && !Object.keys(dropped).length)) return null;
  const left = buildCards(notebook).filter(c => c.kind !== 'signoff'
    && patched[c.kind + ':' + c.index] === undefined && !dropped[c.kind + ':' + c.index]).length;
  return left > 0 ? left : null;
}
window.walkRemaining = walkRemaining;

function BeanQuestionnaire({ notebook, onComplete, onClose, initialProgress, onSaveProgress }) {
  const h = React.createElement;

  // The walk is built ONCE, from the notebook as it was on mount — so confirming or dropping a card
  // can never reshuffle the cards still ahead of her.
  const [cards] = useQState(() => (notebook ? buildCards(notebook) : []));
  // Her decisions, keyed by card. Held here rather than applied to the arrays as she goes, so the
  // indices the cards were built against stay valid for the whole walk. Seeded from initialProgress
  // so a reopened walk carries last session's answers (the 45-card review spans several sittings).
  const [patched, setPatched] = useQState(() => ({ ...(initialProgress && initialProgress.patched) }));   // 'buckets:0' -> her wording
  const [dropped, setDropped] = useQState(() => ({ ...(initialProgress && initialProgress.dropped) }));   // 'buckets:0' -> true (leave it out)
  const [signoff, setSignoff] = useQState(() => (initialProgress && initialProgress.signoff) || '');
  // Resume on the first card she hasn't decided — not card 1 — so reopening doesn't re-ask answered ones.
  const [i, setI] = useQState(() => firstUnanswered(notebook ? cards : [], initialProgress));
  const [saving, setSaving] = useQState(false);

  // `cards` is frozen at mount, so a notebook that ARRIVES later never refills it — the walk would
  // be permanently empty while `notebook` now reads truthy, and the render below would index off the
  // end of it. Treat an empty walk as the empty state whatever the notebook says: the caller must
  // mount this only once the notebook has settled, and if it gets that wrong the answer is a screen
  // that says so, not a blank page.
  if (!notebook || !cards.length) {
    return h('div', { className: 'draft-view' },
      h('button', { className: 'back-btn', onClick: onClose }, '← inbox'),
      h('div', { style: { maxWidth: 620, margin: '48px auto', textAlign: 'center', padding: '0 20px' } },
        window.BeanMark ? h(window.BeanMark, { size: 48, color: '#A89478' }) : null,
        h('div', { style: { fontSize: 18, fontWeight: 800, color: '#3a342c', marginTop: 16 } }, 'Nothing to go over yet'),
        h('div', { style: { fontSize: 14, color: 'var(--ink-faint)', marginTop: 8, lineHeight: 1.5 } },
          'Me hasn’t read enough of your mail to have anything worth asking about. Once me has, this is where you tell me what me got right.')));
  }

  // Fold her decisions into the notebook — the ONE object that leaves this component.
  const assemble = (signoffText) => {
    const roll = (kind, field) => (notebook[kind] || []).map((row, n) => {
      const key = kind + ':' + n;
      if (dropped[key]) return null;
      return patched[key] !== undefined ? { ...row, [field]: patched[key] } : { ...row };
    }).filter(Boolean);
    const notes = roll('notes', 'text');
    const answer = (signoffText || '').trim();
    if (answer) notes.push({ text: SIGNOFF_PREFIX + answer, provenance: 'stated' });
    return {
      ...notebook,
      store: notebook.store || '',
      buckets: roll('buckets', 'cliff'),
      macros: roll('macros', 'text'),
      facts: roll('facts', 'text'),
      notes,
    };
  };

  const finish = (signoffText) => {
    if (saving) return;
    setSaving(true);
    Promise.resolve(onComplete(assemble(signoffText))).catch(() => setSaving(false));
  };

  const card = cards[i];
  const total = cards.length;
  // Functional update, not setI(i + 1): two taps that land before a re-render (a mobile double-tap)
  // both close over the same `i` and the second one is swallowed.
  const advance = () => setI(n => Math.min(n + 1, total - 1));
  // Save her answer the moment she taps it — so closing the walk mid-way loses nothing. The whole
  // decisions object goes each time (a resume snapshot, not an event log); pass the NEW value
  // explicitly since setState hasn't applied yet in this closure.
  const persist = (p, d, s) => onSaveProgress && onSaveProgress({ patched: p, dropped: d, signoff: s });
  const keep = (key) => (text) => {
    const p = { ...patched, [key]: text };
    setPatched(p); persist(p, dropped, signoff);
    advance();
  };
  const leaveOut = (key) => () => {
    const d = { ...dropped, [key]: true };
    setDropped(d); persist(patched, d, signoff);
    advance();
  };

  // ---- chrome ---------------------------------------------------------------------------------
  const progress = h('div', { style: { marginBottom: 22 } },
    h('div', { style: { display: 'flex', alignItems: 'baseline', justifyContent: 'space-between', gap: 12, marginBottom: 8 } },
      h('span', { style: { fontSize: 11.5, fontWeight: 800, letterSpacing: '.12em', textTransform: 'uppercase', color: '#B6A789' } },
        card.section),
      h('span', { style: { fontSize: 12, color: '#B6A789', fontWeight: 700 } }, 'Card ' + (i + 1) + ' of ' + total)),
    h('div', { style: { height: 4, borderRadius: 999, background: '#EBE3D2', overflow: 'hidden' } },
      h('div', { style: { height: '100%', width: Math.round(((i + 1) / total) * 100) + '%', background: '#B08D57', transition: 'width .2s' } })));

  let body;
  if (card.kind === 'signoff') {
    body = h('div', {
      style: { background: 'var(--cream-2, #FCF8EF)', border: '1.5px solid var(--line)', borderRadius: 16,
               boxShadow: 'var(--shadow)', padding: '16px 18px' },
    },
      h('div', { style: { fontSize: 15, fontWeight: 700, color: '#3a342c', lineHeight: 1.55 } },
        'When me’s not sure about an email, what should me do?'),
      h('div', { style: { fontSize: 12.5, color: '#A99C86', fontStyle: 'italic', marginTop: 8, lineHeight: 1.5 } },
        'Me will never fake a confident reply — but tell me what you’d rather me did instead, and me follows it every time.'),
      h('textarea', {
        className: 'admin-textarea', rows: 3, value: signoff, autoFocus: true,
        placeholder: 'e.g. Hand it to me with what you found — don’t guess at anything about a refund.',
        onChange: e => setSignoff(e.target.value),
        // Persist on blur (not per keystroke) so a sign-off she typed but hasn't submitted survives
        // her closing the walk — the approval PUT, when it comes, is what actually commits it.
        onBlur: () => persist(patched, dropped, signoff),
        style: { marginTop: 12 },
      }),
      h('div', { style: { display: 'flex', alignItems: 'center', gap: 10, marginTop: 15, flexWrap: 'wrap' } },
        h('button', { className: 'es-btn es-save', onClick: () => finish(signoff) },
          saving ? 'Saving…' : '✓ That’s my notebook — save it'),
        h('span', { style: { flex: 1 } }),
        h('button', {
          type: 'button', onClick: () => finish(''),
          style: { border: 'none', background: 'transparent', color: '#A99C86', fontFamily: 'inherit',
                   fontSize: 12.5, fontWeight: 700, cursor: 'pointer', padding: '10px 4px' },
        }, 'skip this bit')));
  } else {
    const key = card.kind + ':' + card.index;
    body = h(React.Fragment, null,
      h('div', { style: { fontSize: 13.5, color: '#7A705F', lineHeight: 1.55, marginBottom: 12 } }, card.lead),
      h(window.ProposalCard, {
        key,  // remount per card so its inline-edit state can't bleed into the next one
        claim: card.claim, provenance: card.provenance, consequence: card.consequence,
        confirmLabel: '✓ that’s right', declineLabel: 'leave it out',
        onConfirm: keep(key), onDecline: leaveOut(key),
      }));
  }

  return h('div', { className: 'draft-view' },
    h('button', { className: 'back-btn', onClick: onClose }, '← inbox'),
    h('div', { style: { maxWidth: 620, margin: '0 auto', padding: '18px 20px 80px' } },
      h('div', { style: { fontSize: 20, fontWeight: 800, color: '#3a342c' } }, 'Let’s go over what me learned'),
      h('div', { style: { fontSize: 13, color: '#9A8F7E', fontStyle: 'italic', marginTop: 6, marginBottom: 26, lineHeight: 1.55 } },
        'Me read your mail and wrote down how me thinks you work. Tell me what me got right — nothing is saved until you finish.'),
      progress,
      body));
}

window.BeanQuestionnaire = BeanQuestionnaire;
