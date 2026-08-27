// Bean cite sheet — the chip's other end. Every grounded draft names its sources as citation chips
// ("Grounded in 📓 Cracked tube · 📎 a past reply"); tapping one opens THIS and answers the only
// question a chip raises: *what exactly did you lean on, and is it right?* A notebook line opens
// editable in place (fix it here, Bean drafts from the fix immediately); a past reply opens
// read-only (it already happened — the record, not a thing to edit).
//
// This is the spine of the trust loop: grounding you can SEE is grounding you can CORRECT, and a
// green she can correct in one tap is a green she'll approve without re-reading.
//
// ⚠️ THE MOAT: like BeanSheet and BeanNotebook, this component NEVER fetches and NEVER persists.
// The past reply arrives through the `onLoadReply(id)` prop (bean-root binds it to beanStore); a
// notebook fix goes out through `onSaveNotebook(wholeUpdatedNotebook)` — the SAME single-commit
// seam the notebook editor uses, so there is exactly one writer. Do NOT add a fetch/PUT here.

const { useState: useCiteState, useEffect: useCiteEffect } = React;

// Parse `kind:label` out of a raw citation. A citation with no colon is all label (the model
// occasionally emits a bare line) — treat it as a notebook reference rather than dropping it.
function parseCite(cit) {
  const raw = String(cit || '');
  const i = raw.indexOf(':');
  if (i < 0) return { kind: 'notebook', label: raw };
  return { kind: raw.slice(0, i), label: raw.slice(i + 1) };
}

// Find the notebook line a `notebook:<label>` chip points at. The label is MODEL-WRITTEN prose ("the
// bucket or fact it came from"), not an id, so match generously: exact first, then case-insensitive,
// then containment either way. A miss is a real outcome, not a bug — the sheet says so plainly.
function resolveNotebookCite(notebook, label) {
  if (!notebook) return null;
  const want = String(label || '').trim();
  if (!want) return null;
  const norm = s => String(s || '').trim().toLowerCase();
  const target = norm(want);
  const lists = [
    { kind: 'bucket', section: 'How I route', rows: notebook.buckets || [], key: 'name' },
    { kind: 'macro', section: 'My standard answers', rows: notebook.macros || [], key: 'name' },
    { kind: 'fact', section: 'Store facts', rows: notebook.facts || [], key: 'text' },
    { kind: 'note', section: 'Judgment notes', rows: notebook.notes || [], key: 'text' },
  ];
  const scan = (test) => {
    for (const l of lists) {
      for (let i = 0; i < l.rows.length; i++) {
        const v = norm(l.rows[i][l.key]);
        if (v && test(v)) return { kind: l.kind, section: l.section, index: i, row: l.rows[i] };
      }
    }
    return null;
  };
  return scan(v => v === target)
    || scan(v => v.indexOf(target) === 0)
    || scan(v => v.indexOf(target) >= 0 || target.indexOf(v) >= 0);
}

// How each citation kind reads on the CHIP. Lives here, beside the sheet that opens it, because the
// two must agree: a chip labelled "a past reply" that opens a store record is a small lie about the
// grounding, and grounding is the one thing on that card the operator is meant to trust at a glance.
const CITE_KINDS = {
  'notebook': { icon: '📓', label: null, ink: '#5A5044', bg: '#F4EEE2', line: '#E4DCCB' },  // label = the cited line
  'corpus': { icon: '📎', label: 'a past reply', ink: '#3F6B54', bg: '#EAF3EC', line: '#CFE4D5' },
  'store': { icon: '⬚', label: 'a store record', ink: '#7A5A12', bg: '#FBF1DA', line: '#EAD6A2' },
};

function chipFor(cit) {
  const { kind, label } = parseCite(cit);
  const k = CITE_KINDS[kind] || CITE_KINDS.notebook;
  return { kind, icon: k.icon, text: k.label || label, ink: k.ink, bg: k.bg, line: k.line };
}

const CITE_PROVENANCE = [['stated', 'you told me'], ['observed', 'me saw it in your replies'], ['store-record', 'from your store']];

function BeanCiteSheet({ cite, notebook, onSaveNotebook, onLoadReply, onClose }) {
  const h = React.createElement;
  const { kind, label } = parseCite(cite);
  const hit = kind === 'notebook' ? resolveNotebookCite(notebook, label) : null;

  // Seed the edit fields from the resolved row (clone — the prop notebook stays untouched until save).
  const [name, setName] = useCiteState(hit ? (hit.row.name || '') : '');
  const [text, setText] = useCiteState(hit ? (hit.row.cliff || hit.row.text || '') : '');
  const [prov, setProv] = useCiteState(hit ? (hit.row.provenance || 'stated') : 'stated');
  const [saving, setSaving] = useCiteState(false);

  // The past reply behind a `corpus:` chip. 'loading' until the prop resolves; null = nothing on
  // file (say it — an empty box would read like a reply that got lost).
  const [reply, setReply] = useCiteState('loading');
  useCiteEffect(() => {
    if (kind !== 'corpus' || !onLoadReply) { setReply(null); return; }
    let alive = true;
    Promise.resolve(onLoadReply(label)).then(r => { if (alive) setReply(r || null); })
      .catch(() => { if (alive) setReply(null); });
    return () => { alive = false; };
  }, [kind, label]);

  const head = (icon, title) => h('div', { className: 'es-head' },
    h('span', { style: { fontSize: 16, flex: 'none' } }, icon),
    h('span', { className: 'es-title' }, title),
    h('button', { className: 'es-x', onClick: onClose }, '✕'));

  const lab = (t) => h('div', { className: 'es-lab' }, t);
  const hint = (t) => h('div', { className: 'es-hint' }, t);

  // ---- the notebook line, editable in place ------------------------------------------------
  // Save rebuilds the WHOLE notebook with this one row patched and hands it up — the same object
  // shape BeanNotebook commits, so bean-root's saveNotebook is the only writer either way.
  const commitNotebook = () => {
    if (!hit || saving) return;
    const patched = hit.kind === 'bucket' ? { ...hit.row, name, cliff: text }
      : hit.kind === 'macro' ? { ...hit.row, name, text }
      : { ...hit.row, text, provenance: prov };
    const listKey = hit.kind === 'bucket' ? 'buckets' : hit.kind === 'macro' ? 'macros'
      : hit.kind === 'fact' ? 'facts' : 'notes';
    const rows = (notebook[listKey] || []).map((r, i) => (i === hit.index ? patched : r));
    setSaving(true);
    Promise.resolve(onSaveNotebook({ ...notebook, [listKey]: rows }))
      .then(() => onClose())
      .catch(() => setSaving(false));  // the caller already told her it failed; keep her edits on screen
  };

  let body, foot;
  if (kind === 'notebook' && hit) {
    const isNamed = hit.kind === 'bucket' || hit.kind === 'macro';
    body = h('div', { className: 'es-body' },
      lab('LIVES UNDER'),
      h('div', { className: 'es-path' }, 'Your notebook › ' + hit.section),
      isNamed && h('div', { className: 'es-field' },
        lab(hit.kind === 'bucket' ? 'Bucket' : 'Answer'),
        h('input', { className: 'admin-input', value: name, onChange: e => setName(e.target.value) })),
      h('div', { className: 'es-field' },
        lab(hit.kind === 'bucket' ? 'How you decide' : hit.kind === 'macro' ? 'The reply' : 'The line me leaned on'),
        hint(hit.kind === 'bucket'
          ? 'In your own words — what you DO here. Bean drafts off this the moment you save.'
          : 'Fix it here and every future draft that leans on it changes with it.'),
        h('textarea', {
          className: 'admin-textarea', rows: hit.kind === 'macro' ? 6 : 4, value: text, autoFocus: true,
          onChange: e => setText(e.target.value),
        })),
      !isNamed && h('div', { className: 'es-field' },
        lab('Where it came from'),
        h('select', {
          className: 'admin-input', value: prov, onChange: e => setProv(e.target.value),
        }, CITE_PROVENANCE.map(([v, l]) => h('option', { key: v, value: v }, l)))));
    foot = h('div', { className: 'es-foot' },
      h('button', { className: 'es-btn', onClick: onClose }, 'Close'),
      h('span', { className: 'es-grow' }),
      h('button', { className: 'es-btn es-save', onClick: commitNotebook }, saving ? 'Saving…' : 'Save & approve'));
  } else if (kind === 'notebook') {
    // Cited a notebook line that isn't in the notebook (or the notebook hasn't loaded). Don't
    // fabricate one — name the miss, since an unresolvable citation is itself a grounding smell.
    body = h('div', { className: 'es-body' },
      h('div', { className: 'es-note' },
        'Me cited “', h('b', null, label), '” but can’t find that exact line in your notebook any more — ',
        'it may have been reworded or removed. Open your notebook to check what me’s actually working from.'));
    foot = h('div', { className: 'es-foot' },
      h('span', { className: 'es-grow' }),
      h('button', { className: 'es-btn es-save', onClick: onClose }, 'Close'));
  } else if (kind === 'corpus') {
    body = h('div', { className: 'es-body' },
      reply === 'loading'
        ? h('div', { className: 'es-note' }, 'Me’s finding that one…')
        : !reply
          ? h('div', { className: 'es-note' },
              'Me leaned on a past reply of yours, but can’t pull that one up any more — it’s not in the log.')
          : h(React.Fragment, null,
              lab('THE EMAIL YOU ANSWERED'),
              h('div', { style: { fontWeight: 800, fontSize: 13.5, marginBottom: 4 } }, reply.subject || '(no subject)'),
              window.EmailBody
                ? h(window.EmailBody, { text: reply.body || '' })
                : h('div', { style: { whiteSpace: 'pre-wrap' } }, reply.body || ''),
              h('div', { style: { height: 18 } }),
              lab('WHAT YOU SENT'),
              hint('This already went out — me only shows it so you can see what me copied the shape of.'),
              h('div', {
                style: { whiteSpace: 'pre-wrap', fontSize: 13.5, lineHeight: 1.6, color: '#4A4236',
                         background: 'var(--paper)', border: '1.5px solid var(--line)', borderRadius: 10, padding: '12px 14px' },
              }, reply.reply || '')));
    foot = h('div', { className: 'es-foot' },
      h('span', { className: 'es-grow' }),
      h('button', { className: 'es-btn es-save', onClick: onClose }, 'Close'));
  } else {
    // store:<product_id> — the third grounding source. The citation
    // format is reserved; the records themselves don't exist yet, so say that rather than 404.
    body = h('div', { className: 'es-body' },
      h('div', { className: 'es-note' },
        'This one points at a record from your store (', h('b', null, label), '). ',
        'Me can’t show store records yet — that lands when your store’s connected.'));
    foot = h('div', { className: 'es-foot' },
      h('span', { className: 'es-grow' }),
      h('button', { className: 'es-btn es-save', onClick: onClose }, 'Close'));
  }

  const icon = kind === 'notebook' ? '📓' : kind === 'corpus' ? '📎' : '⬚';
  const title = kind === 'notebook' ? (hit ? 'From your notebook' : 'Me can’t find that line')
    : kind === 'corpus' ? 'One of your past replies' : 'From your store';

  return h('div', { className: 'es-scrim', onClick: onClose },
    h('div', { className: 'es-sheet', onClick: e => e.stopPropagation() },
      head(icon, title), body, foot));
}

// ---- applying an approved chat proposal -----------------------------------------------------
// The same operation commitNotebook does above — rebuild the WHOLE notebook with one row changed,
// and hand it to bean-root's single writer — except the row is found by TEXT rather than by index,
// and there may be no row to find. It lives here, beside its twin, rather than in bean-chat.jsx,
// which has no notebook knowledge by construction.
//
// `section` → list mapping is deliberately the same one commitNotebook uses; a second, subtly
// different mapping is how a fact ends up written into `notes`.
const CLAIM_LISTS = { bucket: 'buckets', macro: 'macros', fact: 'facts', note: 'notes' };

function normalizeLine(s) { return String(s == null ? '' : s).replace(/\s+/g, ' ').trim().toLowerCase(); }

// Returns { notebook, replaced } — `replaced` is the line actually removed, or '' when this was an
// addition. The caller says which happened; it never assumes.
//
// ⚠️ The supersede invariant: a row is removed ONLY if it is found. If the model named a line that
// is no longer there (she edited the notebook since it proposed), this appends and reports ''. It
// never deletes a near-match and never claims a replacement it did not make — guessing which line
// she meant is how you silently drop the wrong policy.
function applyClaim(notebook, message, claim) {
  const text = String(claim == null ? '' : claim).trim();
  const section = (message && message.section) || 'fact';
  const listKey = CLAIM_LISTS[section] || 'facts';
  const rows = (notebook[listKey] || []).slice();
  if (!text) return { notebook, replaced: '' };

  const target = normalizeLine(message && message.supersedes);
  const field = listKey === 'buckets' ? 'cliff' : 'text';
  const at = target ? rows.findIndex(r => normalizeLine(r && r[field]) === target) : -1;

  if (at >= 0) {
    const replaced = rows[at][field];
    rows[at] = { ...rows[at], [field]: text };
    return { notebook: { ...notebook, [listKey]: rows }, replaced };
  }
  // A new line. Buckets and macros are NAMED, and a chat proposal carries no name — so an unmatched
  // bucket/macro proposal lands as a fact instead of inventing a heading she never wrote. Bean can
  // propose changing a cliff she already has; it cannot conjure a new bucket out of one sentence.
  if (listKey === 'buckets' || listKey === 'macros') {
    const facts = (notebook.facts || []).concat([{ text, provenance: 'stated' }]);
    return { notebook: { ...notebook, facts }, replaced: '' };
  }
  const provenance = listKey === 'notes' ? 'observed' : 'stated';
  return { notebook: { ...notebook, [listKey]: rows.concat([{ text, provenance }]) }, replaced: '' };
}

window.BeanCiteSheet = BeanCiteSheet;
window.beanCite = { parseCite, resolveNotebookCite, chipFor };
window.applyClaim = applyClaim;
if (typeof module !== 'undefined' && module.exports) { module.exports = { applyClaim, normalizeLine }; }
