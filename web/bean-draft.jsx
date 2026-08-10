// Bean draft view — renders the open email + Bean's analysis, differently per confidence state.
const { useState: useStateD } = React;

function ShopifyRow({ email }) {
  const store = (window.SETTINGS && window.SETTINGS.shopifyStore) || '';
  if (!store) return null;
  const orders = email.orders || [];
  const orderUrl = (o) => 'https://admin.shopify.com/store/' + store + '/orders?query=' + encodeURIComponent(o);
  const customerUrl = 'https://admin.shopify.com/store/' + store + '/customers?query=' + encodeURIComponent(email.from.email);
  const link = (href, label) => React.createElement('a', { className: 'shopify-link', href, target: '_blank', rel: 'noreferrer' }, label);
  return React.createElement('div', { className: 'shopify-row' },
    orders.map(o => React.createElement(React.Fragment, { key: o }, link(orderUrl(o), '⬚ ' + o + ' ↗'))),
    link(customerUrl, '◴ Customer ↗')
  );
}

function ThreadHistory({ email }) {
  const thread = email.thread || [];
  if (!thread.length) return null;
  return React.createElement('div', { className: 'thread-history' },
    React.createElement('div', { className: 'thread-label' }, 'Earlier from ' + email.from.name + ' · ' + thread.length + (thread.length === 1 ? ' message' : ' messages')),
    thread.map((msg, i) => React.createElement('div', { className: 'thread-msg', key: i },
      (Array.isArray(msg) ? msg : [msg]).map((p, j) => React.createElement('p', { key: j }, p))
    )),
    React.createElement('div', { className: 'thread-current' }, '↓ latest message')
  );
}

function EmailPanel({ email }) {
  return React.createElement('div', { className: 'email-panel' },
    React.createElement('div', { className: 'email-head' },
      React.createElement('div', { className: 'email-avatar' }, email.from.name.split(' ').map(n => n[0]).join('').slice(0, 2)),
      React.createElement('div', { style: { minWidth: 0 } },
        React.createElement('div', { className: 'email-from' }, email.from.name),
        React.createElement('div', { className: 'email-addr' }, email.from.email)
      ),
      React.createElement('div', { className: 'email-time' }, email.time)
    ),
    React.createElement(ShopifyRow, { email }),
    React.createElement(ThreadHistory, { email }),
    React.createElement('div', { className: 'email-subject' }, email.subject),
    // Customer-view render: links live, order refs deep-linked. `email.body` is an array of paragraphs.
    React.createElement(window.EmailBody, { text: email.body })
  );
}

function ReasonList({ items, tone }) {
  return React.createElement('ul', { className: 'reason-list reason-' + tone },
    items.map((r, i) => React.createElement('li', { key: i },
      React.createElement('span', { className: 'reason-mark' }, '·'),
      React.createElement('span', null, r)
    ))
  );
}

// `**bold**` → a real <strong>, everything else → linkified text. Mirrors draftAsHtml in
// bean-ui.jsx; the two must agree, because one is what she READS and the other is what she SENDS.
function draftNodes(text) {
  const src = String(text == null ? '' : text);
  const re = new RegExp(window.MD_BOLD_RE.source, 'g');  // fresh: the shared literal carries lastIndex
  const out = [];
  let last = 0, m, i = 0;
  while ((m = re.exec(src)) !== null) {
    if (m.index > last) out.push(window.linkifyText(src.slice(last, m.index), 'd' + i));
    out.push(React.createElement('strong', { key: 'b' + i }, window.linkifyText(m[1], 'b' + i)));
    last = m.index + m[0].length;
    i += 1;
  }
  if (last < src.length) out.push(window.linkifyText(src.slice(last), 'd' + i));
  return out;
}

// Manual Cmd+C out of the draft box. Without this, a hand-selected copy carries whatever the page
// is rendering — the exact bug that put a typewriter font into the composer on every send. With it,
// the clipboard is authored rather than inherited: the same sans-serif html and asterisk-free plain
// text the Copy button writes.
//
// Selecting the WHOLE draft has to fall back to the raw source, not to the selection string. The
// rendered text has already had its `**` turned into <strong> tags, so copying the selection
// verbatim would hand the clipboard prose with no emphasis left to promote — a silent downgrade on
// the most common gesture (select all, copy). Compare on whitespace-normalised text so a trailing
// newline or a soft-wrapped space doesn't miss the match. A genuinely PARTIAL selection copies what
// the eye picked and loses emphasis, which is the honest trade for respecting the selection.
function draftCopyHandler(text) {
  return (e) => {
    if (!e.clipboardData) return;  // ancient browser — fall through to the default copy
    const raw = String(text == null ? '' : text);
    const sel = window.getSelection ? String(window.getSelection()) : '';
    const flat = (s) => s.replace(/\s+/g, ' ').trim();
    const partial = sel.trim() && flat(sel) !== flat(e.currentTarget.textContent || '');
    const picked = partial ? sel : raw;
    e.clipboardData.setData('text/html', window.draftAsHtml(picked));
    e.clipboardData.setData('text/plain', window.stripMarkdown(picked));
    e.preventDefault();
  };
}

function DraftBox({ text, onEdit }) {
  return React.createElement('div', { className: 'draft-box' },
    React.createElement('div', { className: 'draft-box-head' },
      // "as it'll send" earns the typeface change below. This block deliberately drops Bean's own
      // font because it is a preview of the operator's email, not Bean talking — but unlabelled,
      // that shift just looks like a bug. One clause turns it into a promise the box is keeping.
      React.createElement('span', null, 'Drafted reply — as it’ll send'),
      React.createElement('button', { className: 'draft-edit-btn', onClick: onEdit }, '✎ edit')
    ),
    // pre-wrap preserves the draft's own line breaks; linkifyText makes URLs/order refs clickable.
    // `**bold**` is promoted to real bold here exactly as the clipboard promotes it to <strong> —
    // a preview showing asterisks the copy silently removes would be a preview that lies.
    //
    // onCopy is what lets this box wear Bean's typeface. A manual select + Cmd+C normally copies
    // the RENDERED css, which is how a typewriter font used to ride into the composer on every
    // reply and get reformatted by hand. So the draft no longer has to LOOK like the email to
    // COPY like one: this handler puts the sans-serif html + clean plain text on the clipboard
    // whatever the screen is showing. Both copy paths — this and CopyButton — now emit the same
    // two flavors, which is the only reason the display is free to be styled for reading.
    React.createElement('div', { className: 'draft-text', onCopy: draftCopyHandler(text) }, draftNodes(text))
  );
}

// The grounding, made visible AND tappable: the sources the notebook engine leaned on. A `notebook:`
// chip names the bucket/fact it cited; a `corpus:` chip means one of her own past replies. This is
// the whole trust story on the card — a green she can SEE is grounded is a green she'll approve
// without re-reading, and a chip she can TAP is one she can correct in place (→ BeanCiteSheet).
function CitationChips({ citations, onCite }) {
  if (!citations || !citations.length) return null;
  const chip = (cit, i) => {
    // How each kind reads is owned by bean-cite (window.beanCite.chipFor) — the chip and the sheet it
    // opens must agree on WHAT a citation is. A chip that says "a past reply" and opens a store record
    // is a small lie about the grounding, which is the one thing here she's meant to trust at a glance.
    const c = window.beanCite.chipFor(cit);
    return React.createElement('button', {
      key: i, type: 'button', title: onCite ? 'See where this came from — and fix it if it’s wrong' : cit,
      onClick: onCite ? () => onCite(cit) : undefined,
      style: {
        display: 'inline-flex', alignItems: 'center', gap: 5, padding: '3px 9px', borderRadius: 999,
        fontSize: 11.5, fontWeight: 700, whiteSpace: 'nowrap', maxWidth: 220, overflow: 'hidden', textOverflow: 'ellipsis',
        fontFamily: 'inherit', cursor: onCite ? 'pointer' : 'default',
        color: c.ink, background: c.bg, border: '1.5px solid ' + c.line,
      },
    }, React.createElement('span', null, c.icon), c.text);
  };
  return React.createElement('div', { style: { display: 'flex', alignItems: 'center', flexWrap: 'wrap', gap: 6, margin: '2px 0 8px' } },
    React.createElement('span', { style: { fontSize: 11, fontWeight: 700, color: '#9A8F7E', letterSpacing: '.3px', textTransform: 'uppercase' } }, 'Grounded in'),
    citations.map(chip)
  );
}

// The mirror of FiledState's "↩ This needs a reply": Bean drafted this one, but it needed no reply
// at all (a follow-up on a thread she already answered, an internal note, a pitch). Filing is the
// gate's job, so this logs a `should-file` correction naming the bucket Bean chose — machine-readable
// where a bare note would be "prose no consumer can read". It doubles as the FeedbackBar's submit:
// her 💬 comment rides this action like any other, which is the whole reason it exists.
function ShouldFileBtn({ onClick }) {
  return React.createElement(window.Btn, { kind: 'soft', onClick }, '↩ Should’ve been filed');
}

// The paste-it-yourself reminder that rides under every Copy action — Bean drafts, the operator sends.
function CopyHint() {
  return React.createElement('div', { style: { fontSize: 12, color: '#8A7F6C', fontWeight: 700, marginTop: -4 } },
    'Copy the reply, paste it into your Proton reply, and send — Bean never sends for you.');
}

// ---- HIGH ----
function HighState({ email, onApprove, onEdit, onShouldFile, onBack, onCite }) {
  const c = window.CONF.high;
  return React.createElement(React.Fragment, null,
    React.createElement('div', { className: 'bean-says', style: { background: c.bg, borderColor: c.line } },
      React.createElement(window.BeanMark, { size: 40 }),
      React.createElement('div', null,
        React.createElement('div', { className: 'bean-says-title', style: { color: c.color } }, 'I\'ve got this one.'),
        React.createElement('div', { className: 'bean-says-body' }, email.summary)
      )
    ),
    React.createElement(window.ConfidenceMeter, { level: 'high' }),
    React.createElement('div', { className: 'why-block' },
      React.createElement('div', { className: 'why-label' }, 'Why me\'s sure'),
      // The grounding IS the citation chips. `reasoning` survives for fixture emails that carry a
      // prose list — rendered when present, never as an empty "Why me's sure".
      email.reasoning && email.reasoning.length ? React.createElement(ReasonList, { items: email.reasoning, tone: 'high' }) : null,
      React.createElement(CitationChips, { citations: email.citations, onCite }),
      // Engine results carry no template — omit the chip rather than render "template · null".
      email.template ? React.createElement('div', { className: 'template-chip' }, 'template · ' + email.template) : null
    ),
    React.createElement(DraftBox, { text: email.draft, onEdit }),
    React.createElement('div', { className: 'action-bar' },
      React.createElement(window.CopyButton, { text: email.draft, kind: 'primary', style: { flex: 2 } }),
      React.createElement(window.Btn, { kind: 'ghost', onClick: onApprove, style: { flex: 1 } }, '✓ Approve'),
      React.createElement(window.Btn, { kind: 'ghost', onClick: onEdit, style: { flex: 1 } }, 'Edit first'),
      React.createElement(ShouldFileBtn, { onClick: onShouldFile })
    ),
    React.createElement(CopyHint, null)
  );
}

// ---- LOW ----
function LowState({ email, onApprove, onEdit, onSkip, onShouldFile, onCite }) {
  const c = window.CONF.low;
  return React.createElement(React.Fragment, null,
    React.createElement('div', { className: 'bean-says', style: { background: c.bg, borderColor: c.line } },
      React.createElement(window.BeanMark, { size: 40 }),
      React.createElement('div', null,
        React.createElement('div', { className: 'bean-says-title', style: { color: c.color } }, 'Me drafted it — but take a look.'),
        React.createElement('div', { className: 'bean-says-body' }, email.summary)
      )
    ),
    React.createElement(window.ConfidenceMeter, { level: 'low' }),
    React.createElement('div', { className: 'concern-block' },
      React.createElement('div', { className: 'why-label', style: { color: c.color } }, 'Why me\'s not sure'),
      React.createElement(ReasonList, { items: email.concerns, tone: 'low' })
    ),
    email.reasoning && email.reasoning.length ? React.createElement('div', { className: 'why-block subtle' },
      React.createElement('div', { className: 'why-label' }, 'What me pulled up'),
      React.createElement(ReasonList, { items: email.reasoning, tone: 'neutral' })
    ) : null,
    React.createElement(CitationChips, { citations: email.citations, onCite }),
    React.createElement(DraftBox, { text: email.draft, onEdit }),
    email.beanNote && React.createElement('div', { className: 'bean-note' },
      React.createElement('b', null, 'Heads up: '), email.beanNote
    ),
    React.createElement('div', { className: 'action-bar' },
      React.createElement(window.CopyButton, { text: email.draft, kind: 'amber', style: { flex: 2 } }),
      React.createElement(window.Btn, { kind: 'ghost', onClick: onApprove, style: { flex: 1 } }, '✓ Looks right'),
      React.createElement(window.Btn, { kind: 'ghost', onClick: onEdit, style: { flex: 1 } }, 'Edit'),
      React.createElement(window.Btn, { kind: 'soft', onClick: onSkip }, 'Snooze'),
      React.createElement(ShouldFileBtn, { onClick: onShouldFile })
    ),
    React.createElement(CopyHint, null)
  );
}

// ---- NOTEBOOK RED ----
// The double-tap dies here. On the old tree FLAG, "Take it over" resolved but taught nothing and
// "Teach the reply" taught but didn't resolve — two separate taps. The notebook engine attempts a
// draft even when it's not confident, so RED becomes ONE flow: her edit-in-place IS the teaching
// signal (an approve/edit correction carries the email context → a shelf exemplar) AND it resolves
// the email in the same tap. When there's genuinely nothing to draft, Bean says so plainly and hands
// her the pen — writing the reply teaches and resolves it just the same. No faked confident reply,
// ever; no second tap, ever.
function NotebookFlagState({ email, onApprove, onEdit, onSkip, onShouldFile, onCite }) {
  const c = window.CONF.flag;
  return React.createElement(React.Fragment, null,
    React.createElement('div', { className: 'bean-says', style: { background: c.bg, borderColor: c.line } },
      React.createElement(window.BeanMark, { size: 40 }),
      React.createElement('div', null,
        React.createElement('div', { className: 'bean-says-title', style: { color: c.color } },
          // Bean-speak, addressed to the operator by name when the tenant has set one. Nameless
          // still reads as Bean ("Me not know this one.") — the voice is in the grammar, not the name.
          email.draft ? 'This one’s a judgment call.'
            : (window.operatorName() ? 'Me not know this one, ' + window.operatorName() + '.'
                                     : 'Me not know this one.')),
        React.createElement('div', { className: 'bean-says-body' }, email.summary)
      )
    ),
    React.createElement(window.ConfidenceMeter, { level: 'flag' }),
    (email.concerns && email.concerns.length) ? React.createElement('div', { className: 'flag-reason' },
      React.createElement('div', { className: 'why-label', style: { color: c.color } }, 'Why me won\'t stand behind it'),
      React.createElement(ReasonList, { items: email.concerns, tone: 'low' })
    ) : null,
    React.createElement(CitationChips, { citations: email.citations, onCite }),
    email.draft
      ? React.createElement(React.Fragment, null,
          React.createElement(DraftBox, { text: email.draft, onEdit }),
          React.createElement('div', { className: 'action-bar' },
            React.createElement(window.CopyButton, { text: email.draft, kind: 'amber', style: { flex: 2 } }),
            // Approve/Edit BOTH teach (the correction carries this email → a future exemplar) AND
            // resolve — the merge that kills the teach-then-takeover double-tap.
            React.createElement(window.Btn, { kind: 'ghost', onClick: onApprove, style: { flex: 1 } }, '✓ Send as-is'),
            React.createElement(window.Btn, { kind: 'ghost', onClick: onEdit, style: { flex: 1 } }, 'Fix it first'),
            React.createElement(window.Btn, { kind: 'soft', onClick: onSkip }, 'Snooze'),
            React.createElement(ShouldFileBtn, { onClick: onShouldFile })
          ),
          React.createElement(CopyHint, null)
        )
      : React.createElement(React.Fragment, null,
          React.createElement('div', { className: 'no-draft' },
            React.createElement(window.BeanMark, { size: 24, color: '#A89478' }),
            React.createElement('span', null, 'Nothing close on file to lean on — me won\'t guess. Write it once and me learns it.')
          ),
          React.createElement('div', { className: 'action-bar' },
            React.createElement(window.Btn, { kind: 'danger', onClick: onEdit, style: { flex: 2 } }, '✍ Write the reply'),
            React.createElement(window.Btn, { kind: 'soft', onClick: onSkip }, 'Snooze'),
            React.createElement(ShouldFileBtn, { onClick: onShouldFile })
          )
        )
  );
}

// ---- FILED ----
// The gate filed this one BEFORE any drafting: no confidence bucket, no draft, $0 spent. The whole
// state exists to keep the decision reversible in one tap — "needs a reply" re-drafts past the
// gate AND logs the mis-file (the gate's training signal, wired in bean-root).
function FiledState({ email, onKeepFiled, onNeedsReply }) {
  const [busy, setBusy] = useStateD(false);
  const [err, setErr] = useStateD(null);
  const recover = () => {
    setBusy(true); setErr(null);
    Promise.resolve(onNeedsReply()).catch(e => {
      setErr(e.outOfCredits ? window.BEAN_OOC_MSG : 'Bean couldn’t re-read it — is the server running? (' + e.message + ')');
      setBusy(false);
    });
  };
  return React.createElement(React.Fragment, null,
    React.createElement('div', { className: 'bean-says', style: { background: '#F4EFE6', borderColor: '#E4DCCB' } },
      React.createElement(window.BeanMark, { size: 40 }),
      React.createElement('div', null,
        React.createElement('div', { className: 'bean-says-title', style: { color: '#8A7F6C' } }, 'Me filed this one — no reply needed.'),
        React.createElement('div', { className: 'bean-says-body' },
          (email.gateKind && email.gateKind !== 'rule' ? 'Looks like a ' + window.friendlyKind(email.gateKind).toLowerCase() + '. ' : '') + (email.gateReason || ''))
      )
    ),
    React.createElement('div', { className: 'no-draft' },
      React.createElement(window.BeanMark, { size: 24, color: '#A89478' }),
      React.createElement('span', null, 'No draft was generated — filed mail costs nothing. Wrong call? Tell me and me learns.')
    ),
    err && React.createElement('div', { className: 'gap-warn' }, err),
    React.createElement('div', { className: 'action-bar' },
      React.createElement(window.Btn, { kind: 'amber', onClick: busy ? undefined : recover, style: { flex: 2, opacity: busy ? 0.6 : 1 } },
        busy ? 'Bean is re-reading…' : '↩ This needs a reply'),
      React.createElement(window.Btn, { kind: 'soft', onClick: onKeepFiled, style: { flex: 1 } }, '✓ Keep it filed')
    )
  );
}

// The recategorize control — the operator's bucket override. Changing it is a classification
// correction (caught by the parent as category != model_category); it rides whatever action they
// then take.
function CategoryPicker({ config, notebook, value, onChange }) {
  // Offer the vocabulary the brain that drafted this actually thinks in: the notebook's situation
  // buckets. Offering anything else would invite a "correction" into a name nothing downstream
  // reads. The config's categories remain the fallback for a store with no notebook distilled yet.
  const buckets = ((notebook && notebook.buckets) || []).map(b => b.name).filter(Boolean);
  const names = buckets.length ? buckets
    : ((config && config.categories) || []).map(c => c.name);
  if (value && names.indexOf(value) === -1) names.unshift(value);  // keep the model's pick selectable (incl. the synthetic 'General')
  return React.createElement('div', { className: 'category-picker' },
    React.createElement('label', { className: 'category-picker-label' }, 'Category'),
    React.createElement('select', {
      className: 'category-select', value, onChange: e => onChange(e.target.value),
    }, names.map(n => React.createElement('option', { key: n, value: n }, n)))
  );
}

// Feedback that rides whatever action the operator takes next: 👍 a good draft (positive
// reinforcement) and/or 💬 a comment explaining why (the strongest learning signal). Both are
// captured into the correction so Bean's future drafts lean on the wins and the guidance.
function FeedbackBar({ liked, onToggleLike, comment, onComment, canLike }) {
  return React.createElement('div', { className: 'feedback-bar' },
    canLike && React.createElement('button', {
      className: 'feedback-like' + (liked ? ' is-on' : ''), type: 'button', onClick: onToggleLike,
    }, liked ? '👍 Good job, Bean!' : '👍 Good draft?'),
    React.createElement('textarea', {
      className: 'feedback-comment', rows: 2, value: comment,
      placeholder: 'Tell Bean why — “too formal”, “always offer the adapter”… (optional, but it’s the strongest teacher)',
      onChange: e => onComment(e.target.value),
    })
  );
}

function DraftView({ email, config, notebook, onApprove, onHandle, onSkip, onTeach, onShouldFile, onKeepFiled, onNeedsReply, onBack, onCite }) {
  const [editing, setEditing] = useStateD(false);
  const [text, setText] = useStateD(email.draft || '');
  const [category, setCategory] = useStateD(email.category);
  const [liked, setLiked] = useStateD(false);
  const [comment, setComment] = useStateD('');
  const onEdit = () => setEditing(true);

  // Wrap the bare callbacks so every action carries the operator's chosen category, the right text,
  // and their feedback (👍 like + 💬 comment) — captured as part of the same correction row.
  const fb = { liked, note: comment };
  const approve = () => onApprove({ category, finalText: email.draft, edited: false, ...fb });
  const sendEdited = () => onApprove({ category, originalDraft: email.draft, finalText: text, edited: true, ...fb });
  const handle = () => onHandle({ category, ...fb });
  const skip = () => onSkip({ category, ...fb });
  // Spreads `fb` like every other action, which is the point: this is the one gesture available on an
  // email they want neither to send nor to snooze, so without it their 💬 comment has nowhere to go.
  const shouldFile = () => onShouldFile({ category, ...fb });
  // Teach a flagged email through the shared sheet — carry the operator's chosen category so bean-root
  // resolves (or creates) the right topic and logs the grounded exemplar under it.
  const teach = () => onTeach && onTeach({ ...email, category });

  // Filed emails never reached the engine: no category, no feedback bar — just the filed
  // verdict and the one-tap recovery.
  if (email.filed) {
    return React.createElement('div', { className: 'draft-view' },
      React.createElement('button', { className: 'back-btn', onClick: onBack }, '← inbox'),
      React.createElement('div', { className: 'draft-grid' },
        React.createElement(EmailPanel, { email }),
        React.createElement('div', { className: 'bean-panel' },
          React.createElement(FiledState, { email, onKeepFiled, onNeedsReply })
        )
      )
    );
  }

  // Authoring from a blank red (no draft) is teaching, so name it that — the same edit pane, a
  // different promise. Either way, "Approve my version" logs the edit (→ a shelf exemplar) and
  // resolves in one tap.
  const authoring = email.confidence === 'flag' && !email.draft;
  let right;
  if (editing) {
    right = React.createElement('div', { className: 'edit-pane' },
      React.createElement('div', { className: 'why-label' }, authoring ? 'Write it once — Bean learns it' : 'Editing your reply'),
      React.createElement('textarea', {
        className: 'edit-area', value: text, onChange: e => setText(e.target.value), rows: 12, autoFocus: true,
        placeholder: authoring ? 'Type the reply you’d send — Bean will draft it next time.' : undefined,
      }),
      React.createElement('div', { className: 'action-bar' },
        React.createElement(window.CopyButton, { text, kind: 'primary', label: 'Copy my version', style: { flex: 2 } }),
        React.createElement(window.Btn, { kind: 'ghost', onClick: sendEdited, style: { flex: 1 } }, authoring ? '✓ Send & teach' : '✓ Approve my version'),
        React.createElement(window.Btn, { kind: 'ghost', onClick: () => setEditing(false), style: { flex: 1 } }, 'Cancel')
      ),
      React.createElement(CopyHint, null)
    );
  } else if (email.confidence === 'high') {
    right = React.createElement(HighState, { email, onApprove: approve, onEdit, onShouldFile: shouldFile, onBack, onCite });
  } else if (email.confidence === 'low') {
    right = React.createElement(LowState, { email, onApprove: approve, onEdit, onSkip: skip, onShouldFile: shouldFile, onCite });
  } else {
    // RED: the merged flow — edit-in-place = teach + resolve. No separate take-over/teach buttons.
    right = React.createElement(NotebookFlagState, { email, onApprove: approve, onEdit, onSkip: skip, onShouldFile: shouldFile, onCite });
  }

  return React.createElement('div', { className: 'draft-view' },
    React.createElement('button', { className: 'back-btn', onClick: onBack }, '← inbox'),
    React.createElement('div', { className: 'draft-grid' },
      React.createElement(EmailPanel, { email }),
      React.createElement('div', { className: 'bean-panel' },
        React.createElement(CategoryPicker, { config, notebook, value: category, onChange: setCategory }),
        right,
        !editing && React.createElement(FeedbackBar, {
          liked, onToggleLike: () => setLiked(v => !v),
          comment, onComment: setComment, canLike: email.confidence !== 'flag',
        })
      )
    )
  );
}

window.DraftView = DraftView;
