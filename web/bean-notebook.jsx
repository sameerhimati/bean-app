// Bean notebook — the living home for the operator's brain, rendered as the thing it actually IS: a
// page of markdown they read top-to-bottom and edit in their own words. Not a form of boxed fields —
// a warm paper document. The buckets they sort mail into (with how they decide + the stakes), their
// standard answers, the store facts Bean may cite, the judgment notes that shape a reply.
//
// ⚠️ THE MOAT: this component NEVER fetches and NEVER persists. It seeds local state from `notebook`
// on mount and edits copies; the ONE "Save & approve" button hands the whole updated object back via
// `onSave(updatedNotebook)`. Persistence, the ETag/version compare, and any logging live in the
// caller — do NOT add a `fetch`/PUT or call `onSave` from anywhere but that button. Same discipline
// as bean-sheet's onSave seam: the view stays dumb, the source of truth stays upstream.

const { useState: useNotebookState } = React;

// A borderless textarea that reads as body text and grows to its content — the "written on the page"
// feel. No box, no label chrome; a faint underline only while she's in it.
function PaperText({ value, onChange, placeholder, style }) {
  const ref = React.useRef(null);
  const fit = (el) => { if (el) { el.style.height = 'auto'; el.style.height = el.scrollHeight + 'px'; } };
  React.useEffect(() => { fit(ref.current); }, [value]);
  const [focus, setFocus] = useNotebookState(false);
  return React.createElement('textarea', {
    ref, value: value || '', placeholder, rows: 1,
    onFocus: () => setFocus(true), onBlur: () => setFocus(false),
    onChange: e => { onChange(e.target.value); fit(e.target); },
    style: {
      width: '100%', border: 'none', borderBottom: '1.5px solid ' + (focus ? '#D9CBA6' : 'transparent'),
      background: 'transparent', resize: 'none', outline: 'none', font: 'inherit', lineHeight: 1.7,
      color: '#4A4236', padding: '1px 0', overflow: 'hidden', transition: 'border-color .15s', ...style,
    },
  });
}

// Single-line borderless input for names/titles — same paper feel, sized by its style.
function PaperInput({ value, onChange, placeholder, style }) {
  const [focus, setFocus] = useNotebookState(false);
  return React.createElement('input', {
    value: value || '', placeholder,
    onFocus: () => setFocus(true), onBlur: () => setFocus(false),
    onChange: e => onChange(e.target.value),
    style: {
      border: 'none', borderBottom: '1.5px solid ' + (focus ? '#D9CBA6' : 'transparent'),
      background: 'transparent', outline: 'none', font: 'inherit', color: 'inherit',
      padding: '1px 0', transition: 'border-color .15s', ...style,
    },
  });
}

function BeanNotebook({ notebook, onSave, onClose, onReview }) {
  const h = React.createElement;

  // Friendly empty state — Bean hasn't been distilled yet, so there's nothing to read. Give her the
  // way back, and no false "start from blank" that would look like a real (but empty) notebook.
  if (!notebook) {
    return h('div', { className: 'draft-view' },
      h('button', { className: 'back-btn', onClick: onClose }, '← inbox'),
      h('div', { style: { maxWidth: 620, margin: '48px auto', textAlign: 'center', padding: '0 20px' } },
        window.BeanMark ? h(window.BeanMark, { size: 48, color: '#A89478' }) : null,
        h('div', { style: { fontSize: 18, fontWeight: 800, color: '#3a342c', marginTop: 16 } }, 'No notebook yet'),
        h('div', { style: { fontSize: 14, color: 'var(--ink-faint)', marginTop: 8, lineHeight: 1.5 } },
          'Bean hasn’t been distilled from your mail yet. Once me reads a batch, your buckets, standard answers, and store facts land here for you to read and edit.'))
    );
  }

  // Seed editable state from the notebook (mirror BeanSheet seeding from its node). Clone rows so
  // per-field edits never mutate the prop object — the caller owns the canonical copy until Save.
  const [store, setStore] = useNotebookState(notebook.store || '');
  const [buckets, setBuckets] = useNotebookState(() => (notebook.buckets || []).map(b => ({ ...b })));
  const [macros, setMacros] = useNotebookState(() => (notebook.macros || []).map(m => ({ ...m })));
  const [facts, setFacts] = useNotebookState(() => (notebook.facts || []).map(f => ({ ...f })));
  const [notes, setNotes] = useNotebookState(() => (notebook.notes || []).map(n => ({ ...n })));

  const patchRow = (list, setList) => (i, patch) => setList(list.map((row, j) => j === i ? { ...row, ...patch } : row));
  const dropRow = (list, setList) => (i) => setList(list.filter((_, j) => j !== i));
  const addRow = (list, setList, blank) => () => setList(list.concat([blank]));
  const patchBucket = patchRow(buckets, setBuckets);

  const PROVENANCE = [['stated', 'you told me'], ['observed', 'me saw it in your replies'], ['store-record', 'from your store']];

  // ---- small paper affordances -----------------------------------------------------------------
  // Section heading — a document header, not a form section.
  const heading = (title, sub) => h('div', { style: { marginTop: 44 } },
    h('div', { style: { fontSize: 11.5, fontWeight: 800, letterSpacing: '.12em', textTransform: 'uppercase', color: '#B6A789', borderBottom: '1px solid #EBE3D2', paddingBottom: 7 } }, title),
    sub ? h('div', { style: { fontSize: 12.5, color: '#9A8F7E', fontStyle: 'italic', marginTop: 9, lineHeight: 1.5 } }, sub) : null);

  // A faint per-item remove — low contrast until she wants it.
  const removeX = (onClick) => h('button', {
    onClick, title: 'Remove',
    style: { border: 'none', background: 'transparent', color: '#C9BCA2', cursor: 'pointer', fontSize: 15, lineHeight: 1, padding: '0 2px', flex: 'none' },
  }, '×');

  // The stakes tag rides inline with the bucket name — a quiet pill, not a segmented control.
  const stakesTag = (b, i) => h('button', {
    onClick: () => patchBucket(i, { stakes: b.stakes === 'high' ? 'normal' : 'high' }),
    title: b.stakes === 'high'
      ? 'High-stakes: Bean always checks with you before it sends this bucket, even when it’s sure. Tap to allow auto-approve.'
      : 'Auto-approve ok: Bean may one-tap a grounded draft here. Tap to make it always come to you.',
    style: {
      fontFamily: 'inherit', fontSize: 11, fontWeight: 700, cursor: 'pointer', whiteSpace: 'nowrap', flex: 'none',
      padding: '2px 11px', borderRadius: 999,
      border: '1.5px solid ' + (b.stakes === 'high' ? '#E4B4A6' : '#E7DFCD'),
      background: b.stakes === 'high' ? '#F7E9E3' : 'transparent',
      color: b.stakes === 'high' ? '#B0503A' : '#B6A789',
    },
  }, b.stakes === 'high' ? '🔒 always my confirm' : 'auto-approve ok');

  const provTag = (val, onChange) => h('select', {
    value: val || 'stated', onChange: e => onChange(e.target.value),
    style: { fontFamily: 'inherit', fontSize: 11.5, color: '#A99C86', border: 'none', background: 'transparent', fontStyle: 'italic', cursor: 'pointer', outline: 'none', flex: 'none' },
  }, PROVENANCE.map(([v, l]) => h('option', { key: v, value: v }, l)));

  const addLine = (label, onClick) => h('button', {
    onClick, style: { border: 'none', background: 'transparent', color: '#B08D57', cursor: 'pointer', fontFamily: 'inherit', fontSize: 13, fontWeight: 700, padding: '10px 0 0', marginTop: 4 },
  }, '+ ' + label);

  // ---- the four sections, as document blocks ---------------------------------------------------

  // Buckets: a bold name + a stakes tag on the heading line, the cliff as a paragraph below it.
  const bucketBlocks = buckets.map((b, i) => h('div', { key: 'b' + i, style: { marginTop: 26 } },
    h('div', { style: { display: 'flex', alignItems: 'center', gap: 12 } },
      h(PaperInput, { value: b.name, onChange: v => patchBucket(i, { name: v }), placeholder: 'Name this bucket',
        style: { fontSize: 17, fontWeight: 800, color: '#3a342c', flex: 1, minWidth: 0 } }),
      stakesTag(b, i),
      removeX(() => dropRow(buckets, setBuckets)(i))),
    h('div', { style: { marginTop: 6 } },
      h(PaperText, { value: b.cliff, onChange: v => patchBucket(i, { cliff: v }),
        placeholder: 'In your own words — what you DO here, not the policy you cite. e.g. If it’s within 30 days and the tube’s cracked, me just replaces it, no questions.',
        style: { fontSize: 14.5 } }))
  ));

  // Macros: title + the reply as an indented block, like a quote of herself.
  const patchMacro = patchRow(macros, setMacros);
  const macroBlocks = macros.map((m, i) => h('div', { key: 'm' + i, style: { marginTop: 24 } },
    h('div', { style: { display: 'flex', alignItems: 'center', gap: 12 } },
      h(PaperInput, { value: m.name, onChange: v => patchMacro(i, { name: v }), placeholder: 'Name this answer',
        style: { fontSize: 15, fontWeight: 800, color: '#3a342c', flex: 1, minWidth: 0 } }),
      removeX(() => dropRow(macros, setMacros)(i))),
    h('div', { style: { marginTop: 6, paddingLeft: 14, borderLeft: '2px solid #E7DFCD' } },
      h(PaperText, { value: m.text, onChange: v => patchMacro(i, { text: v }),
        placeholder: 'The standard reply you’d send, in your voice…', style: { fontSize: 13.5, color: '#5A5044' } }))
  ));

  // Facts & notes: bulleted lines, each with a quiet provenance tag — a list you'd jot in a margin.
  const bulletList = (rows, list, setList, patch, placeholder) => rows.map((r, i) =>
    h('div', { key: 'r' + i, style: { display: 'flex', alignItems: 'flex-start', gap: 10, marginTop: 12 } },
      h('span', { style: { color: '#C9BCA2', lineHeight: 1.7, flex: 'none' } }, '•'),
      h('div', { style: { flex: 1, minWidth: 0 } },
        h(PaperText, { value: r.text, onChange: v => patch(i, { text: v }), placeholder, style: { fontSize: 14 } }),
        h('div', { style: { marginTop: 2 } }, provTag(r.provenance, v => patch(i, { provenance: v })))),
      removeX(() => dropRow(list, setList)(i))));

  const patchFact = patchRow(facts, setFacts);
  const patchNote = patchRow(notes, setNotes);

  const commit = () => onSave({ store, buckets, macros, facts, notes });

  return h('div', { className: 'draft-view' },
    h('button', { className: 'back-btn', onClick: onClose }, '← inbox'),
    h('div', { style: { maxWidth: 720, margin: '0 auto', padding: '0 20px 140px' } },
      // The way out of the proofread. Reading a whole page and deciding what you agree with is the
      // taxonomy tax; the walk asks it back one claim at a time instead. The doc stays the room she
      // can always come read — this is just the easier door in.
      onReview && h('div', {
        style: { display: 'flex', alignItems: 'center', gap: 13, flexWrap: 'wrap', marginTop: 22,
                 background: '#FBF1DA', border: '1.5px solid #EAD6A2', borderRadius: 14, padding: '13px 16px' },
      },
        h('div', { style: { flex: 1, minWidth: 200, fontSize: 13, color: '#7A5A12', lineHeight: 1.5 } },
          h('b', null, 'Rather me just asked you?'),
          h('div', { style: { fontSize: 12.5, opacity: .9, marginTop: 2 } },
            'Me’ll go through this one thing at a time — say yes, fix it, or leave it out. Nothing saves till you finish.')),
        h('button', { className: 'es-btn es-save', onClick: onReview, style: { flex: 'none' } }, 'Go over it with me')),
      // The paper page.
      h('div', {
        style: {
          background: '#FCF8EF', border: '1px solid #E7DFCD', borderRadius: 16,
          boxShadow: '0 1px 2px rgba(60,50,30,.05), 0 10px 34px rgba(60,50,30,.06)',
          padding: '48px clamp(24px, 5vw, 60px)', marginTop: 22,
        },
      },
        // Title = the store name, editable in place, like the top line of a page.
        h(PaperInput, { value: store, onChange: setStore, placeholder: 'Your store',
          style: { fontSize: 27, fontWeight: 800, color: '#3a342c', width: '100%' } }),
        h('div', { style: { fontSize: 13, color: '#9A8F7E', fontStyle: 'italic', marginTop: 8, lineHeight: 1.55 } },
          'support notebook — everything me leans on to draft your mail. It’s yours; edit it in your own words and me follows it.'),

        heading('How I route', 'Every email goes in exactly one bucket — chosen by what you’d DO about it, not the product it names.'),
        bucketBlocks,
        addLine('add a bucket', addRow(buckets, setBuckets, { name: '', cliff: '', stakes: 'normal' })),

        heading('My standard answers', 'The replies you reach for again and again — Bean drafts from these, in your voice.'),
        macroBlocks,
        addLine('add an answer', addRow(macros, setMacros, { name: '', text: '' })),

        heading('Store facts', 'The things about your store me may cite in a reply — shipping, warranty, what’s in stock.'),
        bulletList(facts, facts, setFacts, patchFact, 'e.g. Free U.S. shipping on orders $120+.'),
        addLine('add a fact', addRow(facts, setFacts, { text: '', provenance: 'stated' })),

        heading('Judgment notes', 'The calls you make that aren’t plain facts — tone, a line you always add, a thing you never promise.'),
        bulletList(notes, notes, setNotes, patchNote, 'e.g. Never blame the customer for a cracked tube — always apologize first.'),
        addLine('add a note', addRow(notes, setNotes, { text: '', provenance: 'observed' }))
      )
    ),
    // Sticky footer — the ONE way anything here persists.
    h('div', {
      style: { position: 'sticky', bottom: 0, background: 'linear-gradient(180deg, rgba(244,239,230,0) 0%, var(--bg, #F4EFE6) 40%)',
               padding: '18px 20px 14px', display: 'flex', alignItems: 'center', gap: 12, maxWidth: 720, margin: '0 auto' },
    },
      h('button', { className: 'es-btn', onClick: onClose }, '← Back'),
      h('span', { style: { flex: 1 } }),
      h('button', { className: 'es-btn es-save', onClick: commit }, 'Save & approve'))
  );
}

window.BeanNotebook = BeanNotebook;
