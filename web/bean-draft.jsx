// Bean draft view — the open email. A sticky bar carries the subject, ONE confidence pill and the
// actions; the email reads on the left (newest first), the draft Bean wrote on the right.
//
// Why the layout is what it is: this is the operator's most-used screen, and the grade buttons used
// to sit ~1.6 screens down beneath a summary card, a confidence meter and a reason list that said
// the same sentence twice. Grading fell from 57 a week to 12 — and an ungraded draft is one Bean
// cannot learn from. So the actions ride in a bar that never leaves the screen (docked at the
// bottom on a phone), the reason is said once, and the one-tap primary both copies the reply AND
// records the grade, because "Copy" alone recorded nothing server-side.
const { useState: useStateD, useEffect: useEffectD, useRef: useRefD, useLayoutEffect: useLayoutEffectD } = React;
const hD = React.createElement;

// The customer's Shopify records, as quiet inline links in the sender line. Rendered only when the
// tenant has a store handle; the links are search URLs, so a wrong guess costs one empty result.
function ShopifyLinks({ email }) {
  const store = (window.SETTINGS && window.SETTINGS.shopifyStore) || '';
  if (!store) return null;
  const orders = email.orders || [];
  const orderUrl = (o) => 'https://admin.shopify.com/store/' + store + '/orders?query=' + encodeURIComponent(o);
  const customerUrl = 'https://admin.shopify.com/store/' + store + '/customers?query=' + encodeURIComponent(email.from.email);
  const link = (href, label, key) => hD('a', { key, className: 'shopify-link', href, target: '_blank', rel: 'noreferrer' }, label);
  return hD(React.Fragment, null,
    link(customerUrl, 'Customer ↗', 'cust'),
    orders.map(o => link(orderUrl(o), o + ' ↗', o))
  );
}

// The quoted history as a list of messages, oldest first.
//
// What this replaced rendered `email.thread` — a single raw string holding the ENTIRE quoted
// conversation — inside one <p>. HTML collapses newlines to spaces, so a thirteen-message chain
// came out as one run-on paragraph of quote markers and auto-responder boilerplate, under a label
// that said "· 1 message" because the list it counted always had exactly one element. Now
// bean/quoting.py splits it into real messages.
function threadMessages(email) {
  return (email.conversation && email.conversation.length)
    ? email.conversation
    // Baked fixtures (bean-data.jsx) carry hand-written paragraph arrays and no `conversation`.
    : (email.thread || []).map(m => ({ who: '', when: '', text: Array.isArray(m) ? m.join('\n\n') : m, side: 'unknown' }));
}

// The earlier emails this ONE draft is answering (they folded into it in the inbox).
function coveredEmails(email) {
  const covered = (window.EMAILS || []).filter(e => e.rolledInto === email.id);
  return [...covered].sort((a, b) => window.beanTimeMs(a.time) - window.beanTimeMs(b.time));
}

// One earlier message. Collapsed rows show a byline and a one-line peek; open rows render through
// window.EmailBody, which is what gives them real paragraphs and live links.
function ThreadMessage({ msg, defaultOpen }) {
  const [open, setOpen] = useStateD(!!defaultOpen);
  const byline = [msg.who, msg.when].filter(Boolean).join(' · ') || 'Earlier message';
  const peek = String(msg.text || '').replace(/\s+/g, ' ').slice(0, 90);
  return hD('div', { className: 'thread-msg' + (open ? '' : ' is-collapsed') },
    hD('button', { className: 'thread-msg-head', onClick: () => setOpen(!open), 'aria-expanded': open },
      hD('span', { className: 'thread-caret' }, open ? '▾' : '▸'),
      hD('span', { className: 'thread-who' }, byline),
      !open && hD('span', { className: 'thread-peek' }, peek)
    ),
    open && hD(window.EmailBody, { text: msg.text, className: 'thread-body' })
  );
}

// The quoted history, inside the "earlier messages" fold. Messages positively identified as NOT
// from the customer (her own autoresponder, mostly) start collapsed; the most recent message that
// is not the store's own opens by default — that is the context worth reading. Opening "the newest,
// unless it's theirs" left threads showing nothing at all: her autoresponder is very often the last
// thing in the chain. UNKNOWN attribution renders open — collapse what you have identified, never
// on an absence of evidence.
function ThreadHistory({ msgs }) {
  if (!msgs.length) return null;
  let openAt = msgs.length - 1;
  while (openAt > 0 && msgs[openAt].side === 'other') openAt--;
  return hD('div', { className: 'thread-history' },
    msgs.map((msg, i) => hD(ThreadMessage, { key: i, msg, defaultOpen: i === openAt })));
}

// The earlier emails this ONE draft is answering. Without this the operator sees a reply that
// addresses three questions while the panel shows one email, and has no way to check the other two.
// Folding rows away is only honest if the thing they folded into shows what it swallowed.
function ConversationStrip({ covered }) {
  if (!covered.length) return null;
  return hD('div', { className: 'convo-strip' },
    hD('div', { className: 'convo-label' }, 'Also answered by this draft'),
    covered.map(e => hD('div', { className: 'convo-msg', key: e.id },
      hD('div', { className: 'convo-when' }, window.beanTimeLabel(e.time)),
      hD(window.EmailBody, { text: e.body, className: 'convo-body' })
    ))
  );
}

// Everything before the latest message, folded into ONE row beneath it. The latest message is the
// thing being answered, so it reads first; the history is one tap away instead of sitting above
// the subject line pushing the actual question down the page.
function EarlierFold({ covered, msgs }) {
  const [open, setOpen] = useStateD(false);
  const n = covered.length + msgs.length;
  if (!n) return null;
  return hD('div', { className: 'mail-earlier' + (open ? ' is-open' : '') },
    hD('button', { type: 'button', className: 'mail-earlier-head', 'aria-expanded': open, onClick: () => setOpen(!open) },
      hD('span', { className: 'thread-caret' }, open ? '▾' : '▸'),
      n + (n === 1 ? ' earlier message' : ' earlier messages')),
    open && hD('div', { className: 'mail-earlier-body' },
      hD(ConversationStrip, { covered }),
      hD(ThreadHistory, { msgs }))
  );
}

function EmailColumn({ email }) {
  // On a phone the email is clamped to a few lines so the draft (above it) and the docked actions
  // share the first screen; wider screens never clamp — the CSS only applies the clamp ≤760px.
  const [whole, setWhole] = useStateD(false);
  const covered = coveredEmails(email);
  const initials = email.from.name.split(' ').map(n => n[0]).join('').slice(0, 2);
  return hD('div', { className: 'mail-col' },
    hD('div', { className: 'mail-card' },
      hD('div', { className: 'mail-head' },
        hD('div', { className: 'email-avatar' }, initials),
        hD('div', { className: 'mail-who' },
          hD('div', { className: 'email-from' }, email.from.name),
          hD('div', { className: 'mail-addr' },
            hD('span', { className: 'mail-addr-text' }, email.from.email),
            hD(ShopifyLinks, { email }))
        ),
        // "Jul 23, 1:13 PM", not "Thu, 23 Jul 2026 16:29:02 +0000 (UTC)". The raw Date header is a
        // wire format; label to read, raw string on hover — same treatment the inbox rows give it.
        hD('div', { className: 'email-time', title: email.time }, window.beanTimeLabel(email.time) || email.time)
      ),
      covered.length > 0 && hD('div', { className: 'mail-repeat' },
        'They wrote ' + (covered.length + 1) + ' times with no reply — this draft answers all of them'),
      // Customer-view render: links live, order refs deep-linked. `email.body` is an array of paragraphs.
      hD('div', { className: 'mail-body' + (whole ? '' : ' is-clamped') }, hD(window.EmailBody, { text: email.body })),
      !whole && hD('button', { type: 'button', className: 'mail-more', onClick: () => setWhole(true) }, 'Read the whole email')
    ),
    hD(EarlierFold, { covered, msgs: threadMessages(email) })
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
    out.push(hD('strong', { key: 'b' + i }, window.linkifyText(m[1], 'b' + i)));
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

// The textarea grows with the reply instead of scrolling inside itself — editing in place should
// feel like the same sheet of paper, not a box within a box.
function DraftEditor({ value, onChange, placeholder }) {
  const ref = useRefD(null);
  useLayoutEffectD(() => {
    const el = ref.current;
    if (!el) return;
    el.style.height = 'auto';
    el.style.height = el.scrollHeight + 'px';
  }, [value]);
  return hD('textarea', {
    ref, className: 'draft-edit', value, autoFocus: true, rows: 6, placeholder,
    'aria-label': 'Your reply', onChange: e => onChange(e.target.value),
  });
}

// The draft, readable and editable in the same box. Read mode keeps `.draft-text` + onCopy, so a
// hand-selected copy is still authored (see draftCopyHandler); Edit swaps in a textarea in place —
// no separate pane, no separate "approve my version" step. Whatever sits in the box is what
// "Copy & mark sent" copies and grades.
function DraftBox({ text, original, editing, authoring, onChange, onRevert }) {
  const edited = text !== original;
  const label = editing ? (authoring ? 'Your reply — Bean learns it' : 'Editing your reply')
    : (edited ? 'Your version' : 'Reply draft');
  return hD('div', { className: 'draft-box' + (editing ? ' is-editing' : '') },
    hD('div', { className: 'draft-box-head' },
      hD('span', null, label),
      hD('span', { className: 'draft-box-tools' },
        edited && !authoring && hD('button', { type: 'button', className: 'draft-tool', onClick: onRevert }, '↺ Bean’s draft'),
        // Copy WITHOUT grading — for when she wants the text but isn't done with the email.
        !editing && text && hD(window.CopyButton, { text, label: 'Copy only', className: 'draft-tool' })
      )
    ),
    editing
      ? hD(DraftEditor, {
          value: text, onChange,
          placeholder: authoring ? 'Type the reply you’d send — Bean will draft it next time.' : undefined,
        })
      // pre-wrap preserves the draft's own line breaks; linkifyText makes URLs/order refs clickable.
      // `**bold**` is promoted to real bold here exactly as the clipboard promotes it to <strong> —
      // a preview showing asterisks the copy silently removes would be a preview that lies.
      : hD('div', { className: 'draft-text', onCopy: draftCopyHandler(text) }, draftNodes(text))
  );
}

// Nothing on file to lean on, so no draft: say so plainly and hand her the pen. Writing it teaches
// (the correction becomes a shelf exemplar) and resolves it in the same tap. No faked confident
// reply, ever.
function NoDraft({ onWrite }) {
  const name = window.operatorName();
  return hD('div', { className: 'no-draft' },
    hD(window.BeanMark, { size: 24, color: '#A89478' }),
    hD('span', null, (name ? 'Me not know this one, ' + name + '. ' : 'Me not know this one. ') +
      'Nothing close on file to lean on — me won’t guess. Write it once and me learns it.'),
    hD('button', { type: 'button', className: 'draft-tool', onClick: onWrite }, '✍ Write it')
  );
}

// The grounding, made visible AND tappable: the sources the notebook engine leaned on. A `notebook:`
// chip names the bucket/fact it cited; a `corpus:` chip means one of her own past replies. This is
// the whole trust story on the card — a green she can SEE is grounded is a green she'll approve
// without re-reading, and a chip she can TAP is one she can correct in place (→ BeanCiteSheet).
function CitationChips({ citations, onCite }) {
  if (!citations || !citations.length) return null;
  return hD('div', { className: 'cite-chips' },
    citations.map((cit, i) => {
      // How each kind reads is owned by bean-cite (window.beanCite.chipFor) — the chip and the sheet
      // it opens must agree on WHAT a citation is. A chip that says "a past reply" and opens a store
      // record is a small lie about the grounding, which is the one thing here she's meant to trust.
      const c = window.beanCite.chipFor(cit);
      return hD('button', {
        key: i, type: 'button', className: 'cite-chip' + (onCite ? '' : ' is-inert'),
        title: onCite ? 'See where this came from — and fix it if it’s wrong' : cit,
        onClick: onCite ? () => onCite(cit) : undefined,
        style: { color: c.ink, background: c.bg, borderColor: c.line },  // per-kind, owned by bean-cite
      }, hD('span', null, c.icon), c.text);
    })
  );
}

// The sources in one line — "Based on 4 notebook lines and 2 past replies ▸" — instead of a row of
// clipped chips competing with the draft. The chips (and the cite sheet behind them) are one tap
// away. Counted by prefix, the same kinds bean-cite distinguishes.
function sourcesSentence(citations) {
  const cits = citations || [];
  const notes = cits.filter(c => String(c).indexOf('notebook:') === 0).length;
  const past = cits.filter(c => String(c).indexOf('corpus:') === 0).length;
  const plural = (n, one, many) => n + ' ' + (n === 1 ? one : many);
  const parts = [];
  if (notes) parts.push(plural(notes, 'notebook line', 'notebook lines'));
  if (past) parts.push(plural(past, 'past reply', 'past replies'));
  if (!parts.length) return cits.length ? 'Based on ' + plural(cits.length, 'source', 'sources') : '';
  return 'Based on ' + parts.join(' and ') + (past ? '' : ' · no past reply like this one');
}

function SourcesLine({ citations, onCite, lead }) {
  const [open, setOpen] = useStateD(false);
  const sentence = sourcesSentence(citations);
  if (!sentence) return null;
  return hD('div', { className: 'sources' + (open ? ' is-open' : '') },
    hD('button', { type: 'button', className: 'sources-head', 'aria-expanded': open, onClick: () => setOpen(!open) },
      lead && hD('span', { className: 'why-label' }, lead),
      hD('span', { className: 'sources-text' }, sentence),
      hD('span', { className: 'sources-caret', 'aria-hidden': true }, open ? '▾' : '▸')),
    open && hD(CitationChips, { citations, onCite })
  );
}

// The reason, said ONCE. It used to be said three ways: a summary card whose body was why[0], a
// reason list whose first bullet was why[0] again, and a five-block meter repeating the colour the
// pill already showed. Green gets one line (its sources ARE the reason); yellow and red get one
// short list of what to check before it goes.
function WhyBlock({ email, onCite }) {
  const tone = email.confidence;
  if (tone === 'high') {
    const reasoning = email.reasoning || [];
    return hD('div', { className: 'why-block is-high' },
      hD(SourcesLine, { citations: email.citations, onCite, lead: 'Why me’s sure' }),
      // `reasoning` survives for fixture emails that carry a prose list — rendered when present.
      reasoning.length > 0 && hD('ul', { className: 'why-list' }, reasoning.map((r, i) => hD('li', { key: i }, r)))
    );
  }
  const concerns = email.concerns || [];
  return hD(React.Fragment, null,
    concerns.length > 0 && hD('div', { className: 'why-block is-' + tone },
      // Bean-speak, kept light: the same two headings the old flag/low cards used.
      hD('div', { className: 'why-label' }, tone === 'flag' ? 'Why me won’t stand behind it' : 'Why me’s not sure'),
      hD('ul', { className: 'why-list' }, concerns.map((r, i) => hD('li', { key: i }, r)))),
    hD(SourcesLine, { citations: email.citations, onCite })
  );
}

// Feedback that rides whatever action the operator takes next: 👍 a good draft (positive
// reinforcement) and/or 💬 a comment explaining why (the strongest learning signal). Both are
// captured into the correction. It sits in the draft column, by the text she just changed — at the
// page bottom it was the thing nobody scrolled to.
function FeedbackBar({ liked, onToggleLike, comment, onComment, canLike, edited }) {
  return hD('div', { className: 'feedback-bar' },
    hD('div', { className: 'feedback-head' },
      hD('label', { htmlFor: 'bean-note' }, edited ? 'Tell Bean why you changed it' : 'Tell Bean why'),
      canLike && hD('button', {
        className: 'feedback-like' + (liked ? ' is-on' : ''), type: 'button', onClick: onToggleLike,
      }, liked ? '👍 Good job, Bean!' : '👍 Good draft?')
    ),
    hD('textarea', {
      id: 'bean-note', className: 'feedback-comment', rows: 2, value: comment,
      placeholder: '“Too formal”, “always offer the adapter”… optional, but it’s the strongest teacher',
      onChange: e => onComment(e.target.value),
    })
  );
}

// The recategorize control — the operator's bucket override. Changing it is a classification
// correction (caught by the parent as category != model_category); it rides whatever action they
// then take. Lives in the ⋯ menu: one click away, never competing with the send.
function CategoryPicker({ config, notebook, value, onChange }) {
  // Offer the vocabulary the brain that drafted this actually thinks in: the notebook's situation
  // buckets. Offering anything else would invite a "correction" into a name nothing downstream
  // reads. The config's categories remain the fallback for a store with no notebook distilled yet.
  const buckets = ((notebook && notebook.buckets) || []).map(b => b.name).filter(Boolean);
  const names = buckets.length ? buckets
    : ((config && config.categories) || []).map(c => c.name);
  if (value && names.indexOf(value) === -1) names.unshift(value);  // keep the model's pick selectable (incl. the synthetic 'General')
  return hD('label', { className: 'category-picker' },
    hD('span', { className: 'category-picker-label' }, 'Change category'),
    hD('select', { className: 'category-select', value, onChange: e => onChange(e.target.value) },
      names.map(n => hD('option', { key: n, value: n }, n)))
  );
}

// The ⋯ menu: the rare actions, one click away. Closes on an outside press or Escape.
function MoreMenu({ children }) {
  const [open, setOpen] = useStateD(false);
  const ref = useRefD(null);
  useEffectD(() => {
    if (!open) return undefined;
    const away = (e) => { if (ref.current && !ref.current.contains(e.target)) setOpen(false); };
    const esc = (e) => { if (e.key === 'Escape') setOpen(false); };
    document.addEventListener('mousedown', away);
    document.addEventListener('keydown', esc);
    return () => { document.removeEventListener('mousedown', away); document.removeEventListener('keydown', esc); };
  }, [open]);
  return hD('div', { className: 'dv-more', ref },
    hD('button', {
      type: 'button', className: 'dv-btn is-soft dv-more-btn', 'aria-label': 'More actions',
      'aria-haspopup': 'menu', 'aria-expanded': open, onClick: () => setOpen(!open),
    }, '⋯'),
    open && hD('div', { className: 'dv-menu', role: 'menu' }, children)
  );
}

// The bar: back, subject + sender line, the one confidence pill, and the actions. Sticky under the
// top of the page, so grading never costs a scroll; on a phone the actions dock at the bottom.
function DraftBar({ email, category, pill, onBack, actions, notice }) {
  const from = [email.from.name, window.beanTimeLabel(email.time) || email.time, category].filter(Boolean);
  return hD('div', { className: 'dv-bar' },
    hD('div', { className: 'dv-bar-in' },
      hD('button', { className: 'back-btn', onClick: onBack, 'aria-label': 'Back to inbox' }, '← Inbox'),
      hD('div', { className: 'dv-title' },
        hD('h2', null, email.subject),
        hD('div', { className: 'dv-from' }, from.join(' · '))),
      pill,
      hD('div', { className: 'dv-acts' }, actions)
    ),
    notice && hD('div', { className: 'dv-notice', role: 'alert' }, notice)
  );
}

// ---- FILED ----
// The gate filed this one BEFORE any drafting: no confidence bucket, no draft, $0 spent. The whole
// state exists to keep the decision reversible in one tap — "needs a reply" re-drafts past the
// gate AND logs the mis-file (the gate's training signal, wired in bean-root).
function FiledView({ email, onKeepFiled, onNeedsReply, onBack }) {
  const [busy, setBusy] = useStateD(false);
  const [err, setErr] = useStateD(null);
  const recover = () => {
    if (busy) return;
    setBusy(true); setErr(null);
    Promise.resolve(onNeedsReply()).catch(e => {
      setErr(e.outOfCredits ? window.BEAN_OOC_MSG : 'Bean couldn’t re-read it — is the server running? (' + e.message + ')');
      setBusy(false);
    });
  };
  const actions = [
    hD('button', { key: 'reply', type: 'button', className: 'dv-btn is-primary', onClick: recover, disabled: busy },
      busy ? 'Bean is re-reading…' : '↩ This needs a reply'),
    hD('button', { key: 'keep', type: 'button', className: 'dv-btn', onClick: onKeepFiled }, '✓ Keep it filed'),
  ];
  return hD('div', { className: 'draft-view' },
    hD(DraftBar, {
      email, category: window.friendlyKind(email.gateKind), onBack, actions, notice: err,
      pill: hD('span', { className: 'conf-badge is-filed' }, 'Filed'),
    }),
    hD('div', { className: 'draft-grid' },
      hD(EmailColumn, { email }),
      hD('div', { className: 'bean-panel' },
        hD('div', { className: 'bean-says is-filed' },
          hD(window.BeanMark, { size: 36 }),
          hD('div', null,
            hD('div', { className: 'bean-says-title' }, 'Me filed this one — no reply needed.'),
            hD('div', { className: 'bean-says-body' },
              (email.gateKind && email.gateKind !== 'rule' ? 'Looks like a ' + window.friendlyKind(email.gateKind).toLowerCase() + '. ' : '') + (email.gateReason || '')))
        ),
        hD('div', { className: 'no-draft' },
          hD(window.BeanMark, { size: 24, color: '#A89478' }),
          hD('span', null, 'No draft was generated — filed mail costs nothing. Wrong call? Tell me and me learns.'))
      )
    )
  );
}

function DraftView({ email, config, notebook, onApprove, onHandle, onSkip, onTeach, onShouldFile, onKeepFiled, onNeedsReply, onBack, onCite }) {
  const original = email.draft || '';
  const [editing, setEditing] = useStateD(false);
  const [text, setText] = useStateD(original);
  const [category, setCategory] = useStateD(email.category);
  const [liked, setLiked] = useStateD(false);
  const [comment, setComment] = useStateD('');
  const [sending, setSending] = useStateD(false);
  const [copyErr, setCopyErr] = useStateD(null);
  // The copy is async (and can sit behind a permission prompt). If she leaves this email before it
  // resolves, the late resolve must not grade an email she has already moved past.
  const alive = useRefD(true);
  useEffectD(() => () => { alive.current = false; }, []);

  // Filed emails never reached the engine: no category, no feedback bar — just the filed verdict
  // and the one-tap recovery. Branches AFTER the hooks so a recovered email re-rendering in place
  // keeps a stable hook order.
  if (email.filed) return hD(FiledView, { email, onKeepFiled, onNeedsReply, onBack });

  // Authoring from a blank red (no draft) is teaching, so name it that — the same box, a different
  // promise. Either way the primary logs the edit (→ a shelf exemplar) and resolves in one tap.
  const authoring = email.confidence === 'flag' && !original;
  const edited = text !== original;

  // Wrap the bare callbacks so every action carries the operator's chosen category, and their
  // feedback (👍 like + 💬 comment) — captured as part of the same correction row.
  const fb = { liked, note: comment };
  // While a copy is in flight every other exit waits: Snooze or Should've-been-filed landing first
  // would record a second verdict on this email, then the late copy would grade it again.
  const idle = f => (...a) => { if (!sending) f(...a); };
  const skip = idle(() => onSkip({ category, ...fb }));
  // Spreads `fb` like every other action, which is the point: this is the one gesture available on an
  // email they want neither to send nor to snooze, so without it their 💬 comment has nowhere to go.
  const shouldFile = idle(() => onShouldFile({ category, ...fb }));

  // THE action. Copies whatever is in the box (the same two clipboard flavors as Copy only) AND
  // grades it: `approve` when she sends Bean's draft untouched, `edit` with both texts when she
  // changed it — the server computes edit_ratio from the pair. Recorded only once the copy has
  // actually landed: marking a reply "sent" that never reached her clipboard would log a grade for
  // an email she then has to come back and redo, and she would have no way to tell.
  const markSent = () => {
    if (sending || !text.trim()) return;
    setSending(true); setCopyErr(null);
    window.copyDraft(text).then(ok => {
      if (!alive.current) return;
      if (!ok) {
        setSending(false);
        setCopyErr('The copy didn’t land, so me marked nothing sent. Try again, or select the draft and copy it by hand.');
        return;
      }
      onApprove(edited
        ? { category, originalDraft: original, finalText: text, edited: true, ...fb }
        : { category, finalText: original, edited: false, ...fb });
    });
  };

  const blank = !text.trim();
  const primary = (authoring && blank)
    ? hD('button', { key: 'p', type: 'button', className: 'dv-btn is-primary', onClick: () => setEditing(true) }, '✍ Write the reply')
    : hD('button', { key: 'p', type: 'button', className: 'dv-btn is-primary', onClick: markSent, disabled: sending || blank }, '⧉ Copy & mark sent');
  const actions = [
    primary,
    !(authoring && blank && !editing) && hD('button', {
      key: 'e', type: 'button', className: 'dv-btn', onClick: () => setEditing(!editing), 'aria-pressed': editing,
    }, editing ? '✓ Done' : '✎ Edit'),
    hD('button', { key: 's', type: 'button', className: 'dv-btn is-soft dv-opt', onClick: skip }, 'Snooze'),
    hD(MoreMenu, { key: 'm' },
      hD('button', { type: 'button', role: 'menuitem', className: 'dv-menu-item', onClick: shouldFile }, '↩ Should’ve been filed'),
      hD(CategoryPicker, { config, notebook, value: category, onChange: setCategory }),
      hD('button', { type: 'button', role: 'menuitem', className: 'dv-menu-item dv-menu-narrow', onClick: skip }, 'Snooze'),
      hD('div', { className: 'dv-menu-hint' }, 'Me never sends. Copy, paste into your mail, send.')
    ),
  ];

  return hD('div', { className: 'draft-view' },
    hD(DraftBar, {
      email, category, onBack: idle(onBack), actions, notice: copyErr,
      pill: hD(window.ConfidenceBadge, { level: email.confidence }),
    }),
    hD('div', { className: 'draft-grid' },
      hD(EmailColumn, { email }),
      hD('div', { className: 'bean-panel' },
        (authoring && blank && !editing)
          ? hD(NoDraft, { onWrite: () => setEditing(true) })
          : hD(DraftBox, { text, original, editing, authoring, onChange: setText, onRevert: () => setText(original) }),
        email.beanNote && hD('div', { className: 'bean-note' }, hD('b', null, 'Heads up: '), email.beanNote),
        hD(WhyBlock, { email, onCite }),
        hD(FeedbackBar, {
          liked, onToggleLike: () => setLiked(v => !v),
          comment, onComment: setComment, canLike: email.confidence !== 'flag', edited,
        })
      )
    )
  );
}

window.DraftView = DraftView;
