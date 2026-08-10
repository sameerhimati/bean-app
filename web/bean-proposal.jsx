// Bean proposal card — the ONE idiom for every change to the operator's brain.
//
// The rule it enforces: **Bean does the writing, the operator does the judging.** They never author
// into a blank structured document — they never have to know why a thing is a bucket-cliff vs a store
// fact vs a judgment note, which is our boundary rule, not their mental model. Instead a change
// arrives as a CLAIM in their language, carrying its receipts, and they confirm · fix · decline.
// Declining writes nothing; "these should stay with you" is always a valid answer.
//
// Every surface that changes the notebook renders this: the onboarding questionnaire (the approval
// conversation), the diary of pending proposals, a promotion out of the store records.
//
// ⚠️ DUMB BY CONSTRUCTION: no fetch, no persist, no notebook knowledge. It calls onConfirm(claim) /
// onFix / onDecline and nothing else. The caller decides what a confirm MEANS (a questionnaire
// keeps it in a working copy; the diary PUTs it) — same seam as BeanSheet's onSave.

const { useState: useProposalState } = React;

// Provenance in HER language — the vocabulary map. The internal words
// (stated / observed / store-record) never render.
const PROVENANCE_SAYS = {
  'stated': 'you told me',
  'observed': 'me saw it in your replies',
  'store-record': 'from your store',
};

const P_GREEN = '#2E7D3E';

function ProposalCard({ claim, provenance, receipts, consequence, confirmLabel, declineLabel, onConfirm, onFix, onDecline }) {
  const h = React.createElement;
  const [editing, setEditing] = useProposalState(false);
  const [text, setText] = useProposalState(claim || '');
  const [done, setDone] = useProposalState(null); // 'confirmed' | 'declined' — the payoff row

  // The payoff. A confirm that just makes the card vanish teaches her nothing about what changed;
  // the ✓ row is the same "here's what that did" move as the retriage strip in bean-teach.
  if (done) {
    const ok = done === 'confirmed';
    return h('div', {
      style: {
        display: 'flex', gap: 11, alignItems: 'flex-start', borderRadius: 14, padding: '13px 15px',
        background: ok ? '#EAF6EC' : 'var(--paper)',
        border: '1.5px solid ' + (ok ? '#BFE3C6' : 'var(--line)'),
      },
    },
      h('span', { style: { fontSize: 15, flex: 'none', color: ok ? P_GREEN : 'var(--ink-faint)' } }, ok ? '✓' : '—'),
      h('div', { style: { fontSize: 13, fontWeight: 700, lineHeight: 1.5, color: ok ? P_GREEN : 'var(--ink-faint)' } },
        ok ? h(React.Fragment, null, 'Got it — me follows this now.',
              consequence ? h('div', { style: { fontWeight: 600, marginTop: 3, opacity: .9 } }, consequence) : null)
           : 'Left it out. Me wrote nothing down.'));
  }

  const confirm = (t) => { setDone('confirmed'); onConfirm && onConfirm(t); };
  const decline = () => { setDone('declined'); onDecline && onDecline(); };

  const claimBlock = editing
    ? h('textarea', {
        className: 'admin-textarea', rows: 3, value: text, autoFocus: true,
        onChange: e => setText(e.target.value),
      })
    : h('div', { style: { fontSize: 15, fontWeight: 700, color: '#3a342c', lineHeight: 1.55 } }, claim);

  const provLine = (provenance || receipts) && h('div', {
    style: { display: 'flex', alignItems: 'center', flexWrap: 'wrap', gap: 8, marginTop: 10,
             fontSize: 12, color: '#A99C86', fontStyle: 'italic' },
  },
    provenance ? h('span', null, PROVENANCE_SAYS[provenance] || provenance) : null,
    provenance && receipts ? h('span', { style: { fontStyle: 'normal', color: '#D3C7AF' } }, '·') : null,
    receipts ? h('span', null, receipts) : null);

  // What a confirm CHANGES, by example — the thing that makes judging cheap. Optional: Bean often
  // has no honest example, and inventing one would be the yes-man move this project exists to kill.
  const consequenceBlock = consequence && !editing && h('div', {
    style: { marginTop: 12, padding: '10px 13px', background: 'var(--paper)', borderRadius: 10,
             borderLeft: '3px solid #E4DCCB', fontSize: 12.5, color: '#7A705F', lineHeight: 1.55 },
  }, consequence);

  return h('div', {
    style: { background: 'var(--cream-2, #FCF8EF)', border: '1.5px solid var(--line)', borderRadius: 16,
             boxShadow: 'var(--shadow)', padding: '16px 18px' },
  },
    claimBlock,
    !editing && provLine,
    consequenceBlock,
    h('div', { style: { display: 'flex', alignItems: 'center', gap: 10, marginTop: 15, flexWrap: 'wrap' } },
      editing
        ? h(React.Fragment, null,
            h('button', { className: 'es-btn es-save', onClick: () => confirm(text) }, '✓ Save my version'),
            h('button', { className: 'es-btn', onClick: () => { setText(claim || ''); setEditing(false); } }, 'Cancel'))
        : h(React.Fragment, null,
            h('button', { className: 'es-btn es-save', onClick: () => confirm(text) }, confirmLabel || '✓ that’s right'),
            onFix !== false && h('button', {
              className: 'es-btn', onClick: () => { setEditing(true); onFix && onFix(); },
            }, '✎ fix it'),
            h('span', { style: { flex: 1 } }),
            h('button', {
              type: 'button', onClick: decline,
              style: { border: 'none', background: 'transparent', color: '#A99C86', fontFamily: 'inherit',
                       fontSize: 12.5, fontWeight: 700, cursor: 'pointer', padding: '10px 4px' },
            }, declineLabel || 'not now'))));
}

window.ProposalCard = ProposalCard;
window.PROVENANCE_SAYS = PROVENANCE_SAYS;
