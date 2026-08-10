// Bean root app — navigation, status state, persistence, toast.
const { useState: useS, useEffect: useE, useRef: useR } = React;

// One canonical "the API credits ran dry" line, in Bean's voice. Global (on window) so the toast
// here and the inline error boxes in the model-spending views all say the exact same thing.
window.BEAN_OOC_MSG = 'Ruh roh — me’s out of thinking-juice! Someone tell Sam to top up Bean’s monies.';

// Deep-link the initial view from the URL hash (#admin, #draft/<id>) — shareable + lets a
// screenshot land on a specific screen. Defaults to the inbox.
function initialView() {
  const h = (window.location.hash || '').replace(/^#/, '');
  if (h === 'admin') return { name: 'admin' };
  if (h === 'notebook') return { name: 'notebook' };
  if (h === 'questionnaire') return { name: 'questionnaire' };
  if (h.indexOf('draft/') === 0) return { name: 'draft', id: h.slice(6) };
  return { name: 'inbox' };
}

// The review-queue "teach the reply" affordance. A FLAG email needs a human reply; the operator
// writes it here, Bean logs it as a grounded exemplar (onReplyFromEmail), and they send it.
// DELIBERATELY node-less: it never grafts a reusable answer. Teaching from ONE real email must not
// forge a template — that yes-man trap (a customer-specific reply greeting every future customer) is
// the exact failure this redesign closes.
function BeanReply({ email, onSave, onClose }) {
  const h = React.createElement;
  const [reply, setReply] = useS('');
  const bodyText = Array.isArray(email.body) ? email.body.join('\n\n') : (email.body || '');
  const name = (email.from && email.from.name) || 'A customer';
  const send = () => { const r = reply.trim(); if (r) onSave(r); };
  return h('div', { className: 'es-scrim', onClick: onClose },
    h('div', { className: 'es-sheet', onClick: e => e.stopPropagation() },
      h('div', { className: 'es-head' },
        h('span', { className: 'es-title' }, 'Teach the reply · ' + name),
        h('button', { className: 'es-x', onClick: onClose }, '✕')),
      h('div', { className: 'es-body' },
        h('div', { className: 'why-block subtle', style: { marginBottom: 12 } },
          h('div', { className: 'why-label' }, 'The email you’re answering'),
          h('div', { style: { fontWeight: 700, fontSize: 13.5 } }, name),
          h('div', { style: { fontSize: 12, color: 'var(--ink-faint)', marginBottom: 6 } }, email.subject || ''),
          h(window.EmailBody, { text: bodyText })),
        h('div', { className: 'es-lab' }, 'Your reply'),
        h('div', { className: 'es-hint' }, 'Bean sends this and learns from it — it won’t become a template on its own.'),
        h('textarea', { className: 'admin-textarea', rows: 6, value: reply, autoFocus: true,
          onChange: e => setReply(e.target.value),
          onPaste: e => pasteWithLinks(e, setReply),
          placeholder: 'Type the reply you’d send…' }),
        h('div', { className: 'es-char' }, reply.length + ' characters'),
        reply.trim() && h('div', { style: { background: 'var(--paper)', border: '1.5px solid var(--line)', borderRadius: 10, padding: '10px 12px', marginTop: 8 } },
          h('div', { className: 'why-label' }, 'How the customer sees it'),
          h(window.EmailBody, { text: reply }))),
      h('div', { className: 'es-foot' },
        h('span', { className: 'es-grow' }),
        h('button', { className: 'es-btn es-save', onClick: send,
          style: reply.trim() ? null : { opacity: 0.5, cursor: 'default' } }, 'Save & send'))));
}
window.BeanReply = BeanReply;

// First-run onboarding. Shows when the operator opens Bean with no live mail yet and hasn't dismissed
// it. One job: get them to forward their support inbox in. Polls the live inbox; the moment the first
// real email lands it flips to "you're in!" and drops them into the inbox (the aha moment). `onDone(live)`
// persists dismissal + hands any live mail back to App.
function Onboarding({ onDone }) {
  const [addr, setAddr] = useS('');
  const [gotMail, setGotMail] = useS(false);
  const pollTimer = useR(null);
  const doneRef = useR(onDone);
  doneRef.current = onDone;

  // The inbound forwarding address to show her (empty → friendly placeholder). Behind the passcode,
  // so she's already authed; a static/offline demo just 404s and we show the placeholder.
  useE(() => {
    let alive = true;
    fetch('/api/meta').then(r => (r.ok ? r.json() : {})).then(d => {
      if (alive) setAddr((d && d.inbound_address) || '');
    // Cosmetic: the forwarding address shown during onboarding. The UI falls back to a friendly
    // placeholder, so a failed fetch costs nothing and there is nothing to tell her.
    }).catch(() => {});
    return () => { alive = false; };
  }, []);

  // Poll the live inbox while the overlay is open; first real email → celebrate, then auto-advance.
  useE(() => {
    pollTimer.current = setInterval(() => {
      window.beanStore.loadInbox().then(live => {
        if (live && live.length) {
          if (pollTimer.current) { clearInterval(pollTimer.current); pollTimer.current = null; }
          setGotMail(true);
          setTimeout(() => doneRef.current(live), 1400);  // hold the ✓ a beat, then drop into the inbox
        }
      });
    }, 4000);
    return () => { if (pollTimer.current) clearInterval(pollTimer.current); };
  }, []);

  const hasAddr = !!addr;
  const shownAddr = hasAddr ? addr : 'you@your-bean-inbox';
  const steps = [
    'Open Proton Mail. Click the ⚙️ gear (top-right) → All settings.',
    'In the left menu, open “Forward and auto-reply.”',
    'Click “Add forwarding rule.”',
    'Paste your Bean address (copied above) into the “Forward to” box. If it asks “Forward from,” pick your support address.',
    'Hit Save. Proton emails a confirmation to activate it — leave that one to me, me’ll take it from there.',
  ];

  return React.createElement('div', {
    style: {
      position: 'fixed', inset: 0, zIndex: 100, overflowY: 'auto',
      background: 'var(--cream)', padding: '32px 18px',
      display: 'flex', alignItems: 'flex-start', justifyContent: 'center',
    },
  },
    React.createElement('div', {
      style: {
        width: '100%', maxWidth: 520, background: 'var(--paper)',
        border: '1.5px solid var(--line)', borderRadius: 20, padding: '26px 26px 24px',
        boxShadow: 'var(--shadow-lg)', margin: '0 auto',
      },
    },
      // Welcome
      React.createElement('div', { style: { display: 'flex', alignItems: 'center', gap: 14, marginBottom: 14 } },
        React.createElement(window.BeanMark, { size: 46, bob: true }),
        React.createElement('div', null,
          React.createElement('h1', { style: { margin: 0, fontSize: 22, fontWeight: 600 } }, 'Hi, me Bean 🫘'),
          React.createElement('div', { style: { fontSize: 14, color: 'var(--ink-soft)', marginTop: 2 } },
            'Me read your support mail and draft replies for you to approve.')
        )
      ),
      React.createElement('div', {
        style: { fontSize: 13.5, color: 'var(--ink-soft)', background: 'var(--cream-2)', border: '1.5px dashed var(--line)', borderRadius: 12, padding: '11px 14px', marginBottom: 20, lineHeight: 1.5 },
      }, 'You stay in control — me never send anything. You copy the reply and send it yourself.'),

      // The one setup step: forwarding
      React.createElement('div', { style: { fontSize: 13, fontWeight: 800, color: 'var(--ink)', marginBottom: 8 } },
        'Copy your Bean address:'),
      React.createElement('div', { style: { display: 'flex', gap: 8, marginBottom: hasAddr ? 14 : 6 } },
        React.createElement('input', {
          readOnly: true, value: shownAddr,
          style: {
            flex: 1, minWidth: 0, border: '1.5px solid var(--line)', borderRadius: 10,
            padding: '10px 12px', fontFamily: 'inherit', fontSize: 13.5,
            color: hasAddr ? 'var(--ink)' : 'var(--ink-faint)', background: 'var(--cream-2)',
          },
          onFocus: e => e.target.select(),
        }),
        hasAddr && React.createElement(window.CopyButton, { text: addr, kind: 'ghost', label: 'Copy' })
      ),
      !hasAddr && React.createElement('div', { style: { fontSize: 12, color: 'var(--ink-faint)', marginBottom: 14, lineHeight: 1.5 } },
        '(your address will appear here once Bean is connected)'),

      React.createElement('div', { style: { fontSize: 12.5, fontWeight: 800, color: 'var(--ink)', marginBottom: 8 } },
        'How to set up forwarding in Proton Mail'),
      React.createElement('ol', { style: { margin: '0 0 14px', paddingLeft: 20, color: 'var(--ink-soft)', fontSize: 13, lineHeight: 1.7 } },
        steps.map((s, i) => React.createElement('li', { key: i }, s))),

      // The one thing that must not go wrong: a forwarding *rule*, not the per-email Forward button
      // (which rewrites From and hides the customer). Spelled out because it silently breaks triage.
      React.createElement('div', {
        style: { fontSize: 12.5, color: 'var(--ink-soft)', background: '#FBF3E7', border: '1.5px solid #E8D9BF', borderRadius: 12, padding: '10px 13px', marginBottom: 18, lineHeight: 1.55 },
      }, '⚠️ Use “Add forwarding rule” — not the Forward button on a single email. The rule sends every new email to me on its own and keeps each customer’s real address. The button only forwards one, and hides who actually wrote it — so me’d reply to the wrong person.'),

      // Live status
      React.createElement('div', {
        style: {
          display: 'flex', alignItems: 'center', gap: 9, fontSize: 13.5, fontWeight: 700,
          color: gotMail ? '#2E7D3E' : 'var(--ink-soft)',
          background: gotMail ? '#EAF6EC' : 'var(--cream-2)',
          border: '1.5px solid ' + (gotMail ? '#BFE3C6' : 'var(--line)'),
          borderRadius: 12, padding: '11px 14px', marginBottom: 18,
        },
      },
        React.createElement('span', {
          style: { width: 9, height: 9, borderRadius: 999, background: gotMail ? '#4CA35A' : '#C7BCA8', flex: 'none' },
        }),
        gotMail ? '✓ Got your first email — you’re in!' : 'Waiting for your first email…'
      ),

      // Escape hatch
      React.createElement(window.Btn, { kind: 'ghost', full: true, onClick: () => onDone() },
        'I’ve set this up — take me to my inbox')
    )
  );
}

function App() {
  const [status, setStatus] = useS({}); // seeded empty; hydrated from /api/status on mount (async)
  // Config now persists server-side (async). Seed synchronously from the generated window.CONFIG
  // so first render has data, then replace it with the backend's copy once it loads.
  const [config, setConfig] = useS(() => window.beanStore.defaultConfig());
  const [notebook, setNotebook] = useS(null);  // her editable brain; null until loaded / if none distilled yet
  const [reviewProgress, setReviewProgress] = useS(null);  // her answers-so-far in the onboarding walk; loaded fresh each open
  const [view, setView] = useS(initialView);
  const [pasted, setPasted] = useS([]); // live-triaged pasted emails (not in the fixture inbox)
  // The inbox facet filter. Lifted to App (not local to Inbox) so it survives opening a draft and
  // coming back — the whole point of narrowing a 100+ inbox is to work THROUGH the narrowed set, not
  // to have it reset the moment you open one. Ephemeral per session; {} = the un-filtered default.
  const [inboxFilter, setInboxFilter] = useS({});
  const [cite, setCite] = useS(null); // the tapped citation chip: the raw 'kind:label' string | null
  const [replyEmail, setReplyEmail] = useS(null); // the review-queue "teach the reply" affordance (BeanReply)
  const [toast, setToast] = useS(null); // {expr, msg, onUndo}
  const toastTimer = useR(null);
  const pendingCommit = useR(null); // deferred correction for an undoable action; fires on toast-expiry
  const configLoaded = useR(false); // gate saves until the initial GET resolves (don't PUT the seed)
  // First-run onboarding: shown until the operator dismisses it (persisted) or the first live email lands.
  // The public demo starts onboarded (server flag, bean/server.py:_serve_data_jsx). The overlay is a
  // mail-forwarding walk, and a demo visitor has no mailbox to forward — so on that one deployment it
  // is pure friction across the three seconds the demo exists for. Not deleted, and not touched for a
  // real tenant: BEAN_DEMO_TENANT is off unless a deployment explicitly sets it.
  const [onboarded, setOnboarded] = useS(() => {
    if (window.BEAN_DEMO_TENANT) return true;
    try { return !!localStorage.getItem('bean_onboarded'); } catch (e) { return false; }
  });

  // Hydrate the status map from the server once on mount (async now — server-side, not localStorage).
  useE(() => {
    let alive = true;
    window.beanStore.loadStatus().then(s => {
      if (alive) setStatus(s || {});
    });
    return () => { alive = false; };
  }, []);
  // Status is persisted per-id at the point of change (setOne / approveAllHigh), NOT by an effect
  // that pushes the whole map. A whole-map write from this tab would overwrite anything her other
  // device recorded since this tab loaded — and it also fired on the empty seed before the initial
  // GET resolved, which is why it needed a `statusLoaded` guard at all. An upsert needs neither.
  // Load the persisted config once on mount; on failure keep the seeded default (offline demo).
  useE(() => {
    let alive = true;
    window.beanStore.loadConfig().then(c => {
      if (alive) { setConfig(c); configLoaded.current = true; }
    });
    return () => { alive = false; };
  }, []);
  // Load her notebook (the editable brain) once on mount. null = none distilled yet or offline demo;
  // the editor renders an empty state, and the Notebook nav button still opens it.
  // `notebook` is null BOTH while this is in flight and when none has been distilled, so a second
  // flag is needed to tell those apart. The questionnaire builds its 45 cards ONCE from the notebook
  // it was mounted with (deliberately — a card must not reshuffle mid-walk), so mounting it a moment
  // too early freezes an empty walk that never refills.
  const [notebookSettled, setNotebookSettled] = useS(false);
  useE(() => {
    let alive = true;
    window.beanStore.loadNotebook().then(nb => {
      if (!alive) return;
      setNotebook(nb);
      setNotebookSettled(true);
    });
    return () => { alive = false; };
  }, []);
  // Load her onboarding-walk resume state whenever there is none loaded — on mount (the Teach Bean
  // button reads it for its "N left" resume hint, and that button is up on every view), and again
  // each time openQuestionnaire nulls it to force a fresh copy. null → the walk shows a brief loader
  // rather than starting at card 1 and then jumping when progress arrives.
  useE(() => {
    if (reviewProgress !== null) return;
    let alive = true;
    window.beanStore.loadReviewProgress().then(p => { if (alive) setReviewProgress(p || {}); });
    return () => { alive = false; };
  }, [reviewProgress]);
  // Pull the live triaged inbox (webhook-delivered mail) once on mount. Non-empty → it becomes the
  // inbox source (all reads go through window.EMAILS live, so overwriting + a bump re-renders).
  // Empty or failed → keep the baked fixtures, so the SSG demo and paste flow are unchanged.
  const [, bumpInbox] = useS(0);
  // Honest connection signal: real forwarded mail has landed → the pill reads "Connected".
  const [connected, setConnected] = useS(false);
  // Has that fetch come back yet? Only this tells "her mail hasn't arrived in the tab" apart from
  // "this email isn't here" — and a #draft/<id> deep link lands while EMAILS still holds fixtures,
  // so without it every shared draft link is a wrong answer or a crash.
  const [inboxSettled, setInboxSettled] = useS(false);
  useE(() => {
    let alive = true;
    window.beanStore.loadInbox().then(live => {
      if (!alive) return;
      if (live.length) { window.EMAILS = live; setConnected(true); bumpInbox(n => n + 1); }
      setInboxSettled(true);
    });
    return () => { alive = false; };
  }, []);
  // Persist edits (skip the seed/initial-load write — only save genuine user changes).
  useE(() => {
    if (!configLoaded.current) return;
    window.beanStore.saveConfig(config);
  }, [config]);


  // Dismiss onboarding (persisted). If the first live email triggered it, adopt that mail as the
  // inbox source + bump so the app renders it underneath — the operator lands straight on that first email.
  // Re-open the onboarding overlay (the forwarding-setup screen) — triggered from the connection
  // pill. Reversible: its "I've set this up" button dismisses it again via finishOnboarding.
  function reopenOnboarding() {
    try { localStorage.removeItem('bean_onboarded'); } catch (e) {}
    setOnboarded(false);
    window.scrollTo({ top: 0 });
  }

  function finishOnboarding(live) {
    // Quota or private browsing. Worst case onboarding shows once more; no data, no loss.
    try { localStorage.setItem('bean_onboarded', '1'); } catch (e) {}
    if (live && live.length) { window.EMAILS = live; bumpInbox(n => n + 1); }
    setOnboarded(true);
  }

  // Flush any still-pending deferred commit before showing the next toast, so rapid successive
  // actions never drop an earlier commit (single shared timer). An undoable toast gets a longer
  // window and stashes its commit in pendingCommit — which fires only if the window closes un-undone.
  function flashToast(msg, expr = 'cheer', opts = {}) {
    if (toastTimer.current) clearTimeout(toastTimer.current);
    if (pendingCommit.current) { pendingCommit.current(); pendingCommit.current = null; }
    setToast({ msg, expr, onUndo: opts.onUndo || null });
    pendingCommit.current = opts.onCommit || null;
    toastTimer.current = setTimeout(() => {
      setToast(null);
      if (pendingCommit.current) { pendingCommit.current(); pendingCommit.current = null; }
    }, opts.onUndo ? 5000 : 2600);
  }

  // Undo the current undoable toast: cancel its deferred commit, dismiss, then run the revert.
  function undoToast() {
    if (toastTimer.current) clearTimeout(toastTimer.current);
    pendingCommit.current = null;
    const revert = toast && toast.onUndo;
    setToast(null);
    if (revert) revert();
  }

  // The store (bean-inbox) persists writes but can't reach the toast, so it fires bean:write-failed
  // when the server actively refused a save (a network error / 404 is the offline demo and stays
  // silent at the source). Surface it honestly — a dropped write must never read as success, least of
  // all a lost correction (the learning signal). expr 'sleepy' is the one non-cheerful Bean face.
  useE(() => {
    function onWriteFailed(e) {
      const d = e.detail || {};
      // `queued` is the difference between "me kept it" and "that one is gone". Say which.
      const msg = d.queued
        ? 'Me couldn’t reach the log just now — me kept that reply and me try again.'
        : d.what === 'correction'
          ? 'Me couldn’t save that reply to learn from — that one’s lost, sorry.'
          : d.what === 'config'
            ? 'Me couldn’t save your setup — try again?'
            : 'Me couldn’t save that one — try again?';
      flashToast(msg, 'sleepy');
    }
    window.addEventListener('bean:write-failed', onWriteFailed);
    return () => window.removeEventListener('bean:write-failed', onWriteFailed);
  }, []);

  // Out-of-credits is an app-wide condition (Bean can't think at all), not one action's failure, so
  // the store's model-spending fetches fire bean:out-of-credits and we flash the one sleepy toast.
  // Separate from write-failed: a dry balance is a "tell Sam to top up" state, not a "try again" one.
  useE(() => {
    function onOutOfCredits() { flashToast(window.BEAN_OOC_MSG, 'sleepy'); }
    window.addEventListener('bean:out-of-credits', onOutOfCredits);
    return () => window.removeEventListener('bean:out-of-credits', onOutOfCredits);
  }, []);

  // Drain anything a previous session parked. A correction that failed against a restarting server
  // survives the reload in localStorage, so the loop repairs itself without the operator doing anything.
  useE(() => { window.beanStore.flushCorrectionOutbox(); }, []);

  // Every single-email state change goes through here, so this is the one place that has to persist.
  function setOne(id, st) {
    setStatus(s => ({ ...s, [id]: st }));
    window.beanStore.upsertStatus(id, st);
  }

  function nextPendingAfter(id) {
    const list = window.EMAILS;
    const idx = list.findIndex(e => e.id === id);
    // A pasted email (recovery/preview) isn't in the fixture list — don't advance into an
    // unrelated fixture draft; send the flow back to the inbox instead.
    if (idx === -1) return null;
    // Filed mail never auto-advances into view — it's demoted, opened only on purpose.
    const actionable = e => !e.filed && (!status[e.id] || status[e.id] === 'pending');
    for (let i = idx + 1; i < list.length; i++) {
      if (actionable(list[i])) return list[i].id;
    }
    // wrap from start
    for (let i = 0; i < idx; i++) {
      if (actionable(list[i])) return list[i].id;
    }
    return null;
  }

  function open(id) { setView({ name: 'draft', id }); window.scrollTo({ top: 0 }); }
  function back() { setView({ name: 'inbox' }); window.scrollTo({ top: 0 }); }
  function openAdmin() { setView({ name: 'admin' }); window.scrollTo({ top: 0 }); }
  function openNotebook() { setView({ name: 'notebook' }); window.scrollTo({ top: 0 }); }
  // The card-by-card review of the distilled notebook — her approval conversation. Deep-linkable
  // (#questionnaire) because it's the thing to send her a link to after a distillation run. Load her
  // resume state fresh on open (the 45-card walk spans sittings). The actual load is an effect keyed
  // on the view, so the deep-link (#questionnaire) path resumes too, not just this button.
  function openQuestionnaire() {
    setReviewProgress(null);
    setView({ name: 'questionnaire' }); window.scrollTo({ top: 0 });
  }
  // Persist an approved notebook edit; adopt the server's saved copy (its new ETag rides inside the
  // store) and confirm. This is the ONLY writer — BeanNotebook never fetches (the sheet's moat).
  function saveNotebook(nb) {
    return window.beanStore.saveNotebook(nb).then(saved => {
      setNotebook(saved);
      // Every notebook PUT drops the resume file server-side (server.py:1434), and the saved notebook
      // may have shed rows — so the decisions we're holding are now keyed to indices that moved. Null
      // it and let the effect refetch, or the Teach Bean count would be arithmetic over stale keys.
      setReviewProgress(null);
      flashToast('Notebook saved — Bean drafts from this now.', 'happy');
      return saved;
    });
  }
  // A tapped citation chip. The sheet is an overlay over whatever view is up, so
  // this only holds the citation string — resolving it to a notebook line or a past reply is the
  // sheet's job, and the fetch it needs rides the onLoadReply prop (the moat).
  function openCite(c) { setCite(c); }
  function tryEmail() { setView({ name: 'paste' }); window.scrollTo({ top: 0 }); }

  // Paste → live triage via /api/preview → open the same DraftView a fixture email uses.
  async function submitPaste(input) {
    const em = await window.beanStore.previewEmail(input);
    setPasted(p => [em, ...p.filter(x => x.id !== em.id)]);
    setView({ name: 'draft', id: em.id });
    window.scrollTo({ top: 0 });
  }

  function advance(id, st, msg, expr) {
    // Walk to the next pending email in the inbox; when none remain, land back on the inbox.
    const nxt = nextPendingAfter(id);
    setOne(id, st);
    flashToast(msg, expr);
    if (nxt) { setView({ name: 'draft', id: nxt }); window.scrollTo({ top: 0 }); }
    else { setView({ name: 'inbox' }); }
  }

  const current = view.name === 'draft'
    ? (window.EMAILS.find(e => e.id === view.id) || pasted.find(e => e.id === view.id))
    : null;

  // Persist one human action as a correction (the learning signal). `extra` comes from DraftView:
  // the chosen category (a relabel if it differs from the model's), and for edits the original +
  // final text so the backend tags cosmetic vs substantive. meta.model_category preserves the
  // model's original bucket; email_subject/body give few-shot the context to learn from.
  function record(email, action, extra) {
    const x = extra || {};
    window.beanStore.recordCorrection({
      email_id: email.id,
      category: x.category || email.category,
      confidence: email.confidence,
      action,
      original_draft: x.originalDraft,
      final_text: x.finalText,
      note: x.note || '',          // 💬 the operator's comment (strongest signal)
      liked: !!x.liked,            // 👍 positive reinforcement
      meta: {
        model_category: email.category,
        // The gate matches on sender + subject (bean/gate.py _match_rule), so without the sender a
        // logged gate error can never become a gate rule — the log knows she was wrong but not about
        // whom. Every correction carries it; only the gate pair (misfile / should-file) reads it.
        sender_email: (email.from && email.from.email) || '',
        email_subject: email.subject,
        email_body: Array.isArray(email.body) ? email.body.join('\n\n') : (email.body || ''),
        // corrections.meta is the documented free-dict extension point, and few-shot reads
        // email_subject/email_body out of it already.
        ...(x.meta || {}),
      },
    });
  }

  // A FLAG email in the review queue → teach its reply. This creates NO structure: it opens a
  // reply-only affordance (BeanReply) whose save logs the reply as a grounded exemplar and sends it.
  // It never writes a reusable answer — teaching from one real email must not forge a template (the
  // yes-man trap).
  function teachFromEmail(email) {
    setReplyEmail(email);
  }

  // REVIEW-QUEUE teach (BeanReply) — EXEMPLAR-ONLY: log the reply the operator is sending as a
  // grounded `teach` correction and NOTHING else. Teaching from ONE real email must never forge a
  // reusable template (the yes-man trap this redesign closes). The reply lands in the corrections
  // log, which is what the retrieval shelf reads back — their own past words, ranked against the
  // next email.
  function onReplyFromEmail(email, reply) {
    if (reply && email) record(email, 'teach', { category: email.category, finalText: reply });
    flashToast('Got it — Bean learned from that reply.', 'happy');
  }

  function approve(extra) {
    record(current, (extra && extra.edited) ? 'edit' : 'approve', extra);
    advance(current.id, 'approved', 'Sent. On to the next one.', 'cheer');
  }
  // Take it over → Bean steps aside. The takeover is logged only once the undo window closes, so an
  // accidental tap that's undone leaves NO trace in the learning log (the moat stays clean).
  function handle(extra) {
    const email = current, id = current.id;
    const nxt = nextPendingAfter(id);
    setOne(id, 'handled');
    if (nxt) { setView({ name: 'draft', id: nxt }); window.scrollTo({ top: 0 }); }
    else { setView({ name: 'inbox' }); }
    flashToast('All yours — marked as handled.', 'happy', {
      onCommit: () => record(email, 'takeover', extra),
      onUndo: () => {
        setOne(id, 'pending');
        setView({ name: 'draft', id });
        window.scrollTo({ top: 0 });
        flashToast('Back to you — nothing logged.', 'happy');
      },
    });
  }
  function skip(extra) {
    record(current, 'skip', extra);
    advance(current.id, 'skipped', 'Snoozed for later.', 'sleepy');
  }

  // The mirror of `needsReply`, for mail Bean DID draft. She's telling us the gate should have filed
  // it (a follow-up on a thread she already answered, an internal note, a pitch) — a labeled
  // false-positive, the half of the pair `misfile` never covered. Filing costs nothing and drafting
  // one of these is the yes-man failure: a confident reply to an email needing none.
  //
  // Recorded as the exact inverse of `misfile`: `category` is the corrected verdict ('Filed / FYI',
  // matching the literal applyResult assigns at bean-inbox.jsx:378 so the badge and filters agree),
  // while record() keeps Bean's own bucket in meta.model_category. That pair is machine-readable —
  // "Bean said Needs-a-human, she said file it" — where a bare note would be prose no consumer reads.
  // It is also the FeedbackBar's only submit on an email she'll neither send nor snooze.
  function shouldFile(extra) {
    record(current, 'should-file', { ...extra, category: 'Filed / FYI' });
    // Deliberately claims only what's true today: the correction is logged. It does not promise
    // "me won't draft that next time" — nothing consumes this yet (see the gate-proposals work).
    advance(current.id, 'handled', 'Filed — me noted that one.', 'happy');
  }

  // Filed-mail actions. "Keep it filed" confirms the gate (a labeled true-negative); "needs a
  // reply" is the recovery: log the mis-file (the gate's training signal — same JSONL log as
  // every correction), then re-preview past the gate and open the real triage result.
  function keepFiled() {
    record(current, 'keep-filed', { note: current.gateReason || '' });
    advance(current.id, 'handled', 'Filed it stays.', 'happy');
  }
  async function needsReply() {
    const filed = current;  // capture — `current` is derived from view and will change on setView
    const em = await window.beanStore.previewEmail({
      name: filed.from.name, email: filed.from.email, subject: filed.subject,
      body: Array.isArray(filed.body) ? filed.body.join('\n\n') : (filed.body || ''),
    }, { skipGate: true });
    // Log the mis-file only once the recovery actually succeeded — a failed/retried preview
    // must not double-log the correction (the gate's training signal). Record what the recovery
    // FOUND, not just that it happened: `em` is the correctly-routed result, so its category is the
    // corrected half of the pair. meta.model_category keeps the gate's own call ('Filed / FYI'), and
    // gate_kind makes the specific wrong call machine-readable — the note alone is prose no consumer
    // can read.
    record(filed, 'misfile', {
      category: em.category,
      note: 'Bean set this aside — looked like a ' + window.friendlyKind(filed.gateKind).toLowerCase() + '.',
      meta: { gate_kind: filed.gateKind },
    });
    setOne(filed.id, 'handled');  // the filed copy is superseded by the routed one
    setPasted(p => [em, ...p.filter(x => x.id !== em.id)]);
    setView({ name: 'draft', id: em.id });
    flashToast('Got it — me routes it properly now.', 'happy');
    window.scrollTo({ top: 0 });
  }

  function approveAllHigh() {
    const highs = window.EMAILS.filter(e => e.confidence === 'high' && (!status[e.id] || status[e.id] === 'pending'));
    highs.forEach(e => record(e, 'approve', { finalText: e.draft }));
    setStatus(s => { const n = { ...s }; highs.forEach(e => n[e.id] = 'approved'); return n; });
    highs.forEach(e => window.beanStore.upsertStatus(e.id, 'approved'));  // one upsert each; no map push
    flashToast(`Sent ${highs.length} ready replies. The judgment calls are still yours.`, 'cheer');
  }

  // Clear the FYI lane. Confirm-gated (it deletes, even if the server keeps a backup), and it
  // reloads from disk so the count reflects what actually survived — never an optimistic guess.
  function clearFiled() {
    const filedCount = window.EMAILS.filter(e => e.filed).length;
    if (!filedCount) return;
    if (!window.confirm(`Delete ${filedCount} filed FYI email${filedCount === 1 ? '' : 's'}? Your real customer mail is untouched, and these are recoverable from a backup.`)) return;
    window.beanStore.clearFiled().then(res => {
      window.beanStore.loadInbox().then(live => { window.EMAILS = live; bumpInbox(n => n + 1); });
      flashToast(`Cleared ${res.removed} filed email${res.removed === 1 ? '' : 's'}.`, 'cheer');
    }).catch(() => flashToast("Couldn't clear the filed mail — nothing was deleted.", 'warn'));
  }

  let main;
  if (view.name === 'admin') {
    main = React.createElement(window.AdminView, { config, setConfig, onBack: back });
  } else if (view.name === 'paste') {
    main = React.createElement(window.PasteView, { onSubmit: submitPaste, onBack: back });
  } else if (view.name === 'notebook') {
    main = React.createElement(window.BeanNotebook, {
      notebook, onSave: saveNotebook, onClose: back,
      // The way into the card-by-card walk. Passed as a callback so BeanNotebook stays dumb — it
      // renders the banner, bean-root owns the navigation.
      onReview: openQuestionnaire,
    });
  } else if (view.name === 'questionnaire') {
    // The NOTEBOOK is still written once, at the end (the consent invariant — Bean never drafts from
    // a half-approved brain). What's new is a SEPARATE resume store: each card tap saves her
    // answers-so-far via onSaveProgress, so the 45-card walk survives being closed and reopened;
    // onComplete's saveNotebook is what clears it server-side. Wait for progress to load so we resume
    // on the right card instead of flashing card 1 — and for the NOTEBOOK, because the walk is built
    // from it once at mount. Two fetches race here (both start on mount); mounting when only the
    // faster one has landed is what white-screened prod.
    main = (reviewProgress === null || !notebookSettled)
      ? React.createElement('div', { className: 'draft-view', style: { maxWidth: 620, margin: '48px auto', textAlign: 'center', color: 'var(--ink-faint)', padding: '0 20px' } }, 'Finding where you left off…')
      : React.createElement(window.BeanQuestionnaire, {
          notebook, onClose: back,
          initialProgress: reviewProgress,
          onSaveProgress: window.beanStore.saveReviewProgress,
          onComplete: nb => saveNotebook(nb).then(saved => { back(); return saved; }),
        });
  } else if (view.name === 'draft' && !current) {
    // A draft link whose email isn't in this tab. #draft/<id> is a SHAREABLE link by design, so it
    // gets opened cold — before the live inbox has replaced the fixtures — and it gets opened stale,
    // after the mail was cleared. Both used to reach DraftView with `email` undefined, which reads
    // `email.engine` on its first line: a white screen, from a link that is meant to be sent to
    // someone. Wait while the fetch is still out; say so plainly once it has settled.
    main = React.createElement('div', { className: 'draft-view', style: { maxWidth: 620, margin: '48px auto', textAlign: 'center', padding: '0 20px' } },
      inboxSettled
        ? React.createElement(React.Fragment, null,
            React.createElement('div', { style: { fontSize: 18, fontWeight: 800, color: '#3a342c' } }, 'Me can’t find that email'),
            React.createElement('div', { style: { fontSize: 14, color: 'var(--ink-faint)', marginTop: 8, lineHeight: 1.5 } },
              'It may have been cleared, or the link points somewhere me can’t see any more.'),
            React.createElement('button', { className: 'back-btn', style: { marginTop: 18 }, onClick: back }, '← inbox'))
        : React.createElement('div', { style: { color: 'var(--ink-faint)' } }, 'Finding that email…'));
  } else if (view.name === 'draft') {
    main = React.createElement(window.DraftView, {
      // key on the email id so a new email remounts DraftView — otherwise its useState
      // initializers (category, edit text, like, comment) keep the PREVIOUS email's values
      // when advancing/recovering to the next one.
      key: current && current.id,
      email: current, config, notebook, onApprove: approve, onHandle: handle, onSkip: skip,
      onTeach: teachFromEmail, onShouldFile: shouldFile,
      onKeepFiled: keepFiled, onNeedsReply: needsReply, onBack: back,
      onCite: openCite,
    });
  } else {
    main = React.createElement(window.Inbox, { status, pasted, filter: inboxFilter, onFilterChange: setInboxFilter, onOpen: open, onApproveAllHigh: approveAllHigh, onClearFiled: clearFiled });
  }

  // The resume hint on the Teach Bean button — cards left in her notebook walk, or null when she
  // isn't mid-walk. Owned by the questionnaire, so the button and the walk count the same things.
  const teachLeft = window.walkRemaining(notebook, reviewProgress);

  return React.createElement(React.Fragment, null,
    !onboarded && React.createElement(Onboarding, { onDone: finishOnboarding }),
    React.createElement('div', { className: 'app-shell' },
      React.createElement(window.TopBar, { onOpenAdmin: openAdmin, onOpenNotebook: openNotebook, onTryEmail: tryEmail, onReopenOnboarding: reopenOnboarding, onOpenTeach: openQuestionnaire, teachLeft, connected }),
      React.createElement('main', { className: 'app-main' }, main),
      React.createElement(window.Toast, {
        show: !!toast, expr: toast ? toast.expr : 'cheer',
        onUndo: toast && toast.onUndo ? undoToast : null,
      }, toast ? toast.msg : '')
    ),
    // The citation-chip sheet — the other end of "Grounded in 📓 …". Same overlay discipline as
    // the notebook editor: keyed per open so its edit state re-seeds, and it gets the SAME
    // saveNotebook writer the notebook editor uses (one writer, one ETag), plus a bound reply
    // reader so the sheet itself never fetches.
    cite && React.createElement(window.BeanCiteSheet, {
      key: cite,
      cite, notebook,
      onClose: () => setCite(null),
      onSaveNotebook: saveNotebook,
      onLoadReply: id => window.beanStore.loadReply(id),
    }),
    // The review-queue "teach the reply" affordance — node-less by construction, so it can only log a
    // grounded exemplar (onReplyFromEmail), never graft a template.
    replyEmail && React.createElement(window.BeanReply, {
      key: replyEmail.id,
      email: replyEmail,
      onClose: () => setReplyEmail(null),
      onSave: reply => { setReplyEmail(null); onReplyFromEmail(replyEmail, reply); },
    })
  );
}

ReactDOM.createRoot(document.getElementById('root')).render(React.createElement(App));
