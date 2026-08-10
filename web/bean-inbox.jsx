// Bean main app: triage inbox + draft view (3 confidence states) + state/persistence.
const { useState, useEffect, useRef } = React;
const CONFIG_KEY = 'bean_config_v1';

// Per-email action state (id -> approved|handled|skipped|pending) now persists server-side at
// /api/status — NOT localStorage — so it's one Bean across the operator's devices: mark mail handled
// on desktop and their phone agrees. Async like loadConfig; both degrade to {} on the offline/SSG demo.
async function loadStatus() {
  try {
    const r = await fetch('/api/status');
    if (!r.ok) throw new Error('GET /api/status ' + r.status);
    return await r.json();
  } catch (e) { return {}; }  // a READ, and nothing is lost: mail shows un-actioned until it loads
}
// A non-2xx response RESOLVES a fetch (only a network error rejects), so a bare .catch() lets the
// server's active refusal — a 400 bad payload, a 500 mid-deploy — vanish while the UI cheers "saved."
// A dropped write is invisible and permanent (the correction log is the learning moat), so a refusal
// must surface. The one exception is "no backend at all": a rejected fetch or a 404 is the offline
// static-SSG demo answering for /api/*, which is deliberately silent. bean-root owns the toast and we
// can't reach it from the store, so we fire a decoupled event it listens for.
// `queued` says whether the write survived the failure. "Me kept it, me try again" and "that one is
// gone" are different facts, and telling her the comfortable one when the other is true is the same
// dishonesty in a new outfit.
function _signalWriteFailed(what, endpoint, status, queued) {
  console.error('Bean write failed: ' + endpoint + ' → ' + status + (queued ? ' (queued for retry)' : ' (LOST)'));
  window.dispatchEvent(new CustomEvent('bean:write-failed', { detail: { what, status, queued: !!queued } }));
}
// Persist ONE email's state, not the whole map. The old whole-map PUT was a lost-update bug on the
// endpoint that needed it least: status exists so phone and laptop agree, and two devices each PUT
// the map they loaded — so approving on the phone, then approving something else on the laptop,
// silently reverted the phone's work. A per-id upsert merges server-side and cannot clobber.
// An empty `state` clears the key (the undo path sets 'pending' rather than clearing, but the
// server supports both). A rejected fetch or a 404 is the no-backend demo — stay silent; any other
// non-ok is the server refusing, and an action that read as saved but wasn't must never be silent.
function upsertStatus(id, state) {
  try {
    fetch('/api/status', {
      method: 'POST', headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ id: id, state: state || '' }),
    }).then(r => {
      if (!r.ok && r.status !== 404) _signalWriteFailed('status', 'POST /api/status', r.status);
    // The .then above already surfaced every refusal the server can express. These two catches only
    // see a rejected fetch (offline demo, or a synchronous fetch throw on a malformed URL), where
    // the worst case is one email showing un-actioned on reload. Nothing the operator taught is lost.
    }).catch(() => {});
  } catch (e) {}
}

// The operator's editable config (categories/templates/knowledge/settings). Phase 2: persisted by the
// local backend (bean/server.py) at /api/config, so edits survive reloads and feed live preview.
// defaultConfig() (deep-copy of the generated window.CONFIG) stays the offline/SSG fallback — if
// the API isn't there (static-file demo), every call degrades to it gracefully.
function defaultConfig() {
  try { return JSON.parse(JSON.stringify(window.CONFIG || { categories: [], knowledgeDocs: [], settings: {} })); }
  catch (e) { return { categories: [], knowledgeDocs: [], settings: {} }; }
}
// The version of the config this tab loaded. A PUT replaces the whole config, so without this a
// stale tab silently overwrites whatever was saved after it loaded (it happened: an import landed,
// then an open tab saved and wiped it, and the server answered 200). We echo the GET's ETag back as
// If-Match; the server 409s if anyone saved in between. Null until the first load — an offline/SSG
// demo never gets one and never PUTs.
let _configEtag = null;

// Did /api/config ever answer? A rejected fetch means two completely different things: on the
// static-file demo there is no backend and silence is right; in the deployed app the network just
// dropped mid-PUT and the operator's taught template exists only in this tab. Both look identical to a
// `catch`. This flag is how they are told apart — the server answered once, so it exists, so a
// rejected write is a real, permanent loss and must be said out loud.
let _hasBackend = false;

// An ETag is a quoted string, and a proxy that re-encodes the body may hand back the weak form
// (W/"abc"). Send back only the hash, exactly as the server compares it.
function _etagValue(raw) {
  return raw ? raw.trim().replace(/^W\//, '').trim().replace(/^"|"$/g, '') : null;
}

// async now (fetch). All three return Promises; callers in bean-root.jsx await them.
async function loadConfig() {
  try {
    const r = await fetch('/api/config');
    if (!r.ok) throw new Error('GET /api/config ' + r.status);
    _hasBackend = true;  // there is a server; a later rejected PUT is a failure, not the demo
    _configEtag = _etagValue(r.headers.get('ETag'));
    return await r.json();
  } catch (e) { return defaultConfig(); }  // offline/SSG demo: fall back to seeded config
}
// The last config PUT, exposed so a follow-up action can wait for it. Re-triage (which re-routes
// stored mail against the CURRENT config on disk) MUST run after the graft it depends on has
// landed — otherwise it reads a config missing the new question and moves nothing. `configSettled()`
// resolves when the most recent save has finished (success OR handled failure); it never rejects.
let _configSettled = Promise.resolve();
function saveConfig(c) {
  const p = _saveConfigImpl(c);
  // Swallow is deliberate and safe: this is only the "has the save SETTLED yet" signal a follow-up
  // (retriage) awaits — it must resolve on failure too, not hang. The failure itself is NOT lost:
  // _saveConfigImpl surfaces every save error via _signalWriteFailed. Callers still get the real
  // promise `p` (with its rejection) — only this tracking copy is neutralised to avoid a spurious
  // unhandled-rejection, and a failed save just means retriage runs against the last-good config.
  _configSettled = p.catch(() => {});
  return p;
}
function configSettled() { return _configSettled; }

async function _saveConfigImpl(c) {
  let r;
  try {
    r = await fetch('/api/config', {
      method: 'PUT',
      headers: { 'Content-Type': 'application/json', 'If-Match': _configEtag || '' },
      body: JSON.stringify(c),
    });
  } catch (e) {
    // A network-level reject. Its non-2xx sibling below has surfaced this for a while; this branch
    // never did, so a dropped connection mid-PUT — a redeploy, a sleeping laptop — silently threw
    // away whatever the operator had just taught Bean while the UI said "Saved". Status 0: there was
    // no response. Not queued: unlike a correction, the config is a whole-document PUT and re-sending
    // a stale copy later is how a good save gets clobbered. Say so now, while it can still be retyped.
    if (_hasBackend) _signalWriteFailed('config', 'PUT /api/config', 0, false);
    return c;  // keep the in-memory config: the edits are still on screen
  }
  // A conflict is NOT a transient error to swallow — swallowing it is how work gets destroyed.
  // Tell her plainly, and don't reload for her: her unsaved edits are still in this tab.
  if (r.status === 409 || r.status === 428) {
    window.alert(
      'Me got a newer copy of your setup — someone saved while this tab was open.\n\n' +
      'Me did NOT save, so nothing of theirs got wiped. Copy anything you just typed, ' +
      'then reload the page and put it back in.'
    );
    return c;
  }
  if (!r.ok) {
    // 404 is the no-backend static demo (silent, like the reject-catch above). Any other non-ok —
    // e.g. a transient 500 mid-deploy — is not a conflict but is still a lost save, so surface it.
    if (r.status !== 404) _signalWriteFailed('config', 'PUT /api/config', r.status);
    return c;
  }
  _configEtag = _etagValue(r.headers.get('ETag'));  // adopt the saved version (the server normalizes what it persists)
  return await r.json();
}

// The notebook (her editable brain) — twins of loadConfig/_saveConfigImpl, same optimistic-
// concurrency ETag so two open editors can't silently clobber each other. Distinct from the config:
// a 503 means "no notebook distilled yet" (returns null → the editor shows an empty state), and the
// offline/SSG demo has none either, so a failed GET is also null, never a fabricated default.
let _notebookEtag = null;
async function loadNotebook() {
  try {
    const r = await fetch('/api/notebook');
    if (r.status === 503) return null;  // not distilled/approved yet
    if (!r.ok) throw new Error('GET /api/notebook ' + r.status);
    _hasBackend = true;
    _notebookEtag = _etagValue(r.headers.get('ETag'));
    return await r.json();
  } catch (e) { return null; }
}
async function saveNotebook(nb) {
  let r;
  try {
    r = await fetch('/api/notebook', {
      method: 'PUT',
      headers: { 'Content-Type': 'application/json', 'If-Match': _notebookEtag || '' },
      body: JSON.stringify(nb),
    });
  } catch (e) {
    if (_hasBackend) _signalWriteFailed('notebook', 'PUT /api/notebook', 0, false);
    return nb;  // keep her edits on screen
  }
  if (r.status === 409 || r.status === 428) {
    window.alert(
      'Me got a newer copy of your notebook — someone saved while this was open.\n\n' +
      'Me did NOT save, so nothing got wiped. Copy anything you just changed, reload, and put it back in.'
    );
    return nb;
  }
  if (!r.ok) {
    if (r.status !== 404) _signalWriteFailed('notebook', 'PUT /api/notebook', r.status);
    return nb;
  }
  _notebookEtag = _etagValue(r.headers.get('ETag'));
  return await r.json();
}
// ---- the correction outbox -------------------------------------------------------------------
// A correction is the learning signal — the only record of what the operator would have said instead
// of Bean. Losing one leaves a permanent hole in the few-shot pool for that category, and the loss is
// invisible: they clicked approve, the UI said "Sent", and the POST died against a restarting server.
// A toast says it's gone; it doesn't bring it back. So a correction that can't be delivered is
// PARKED in localStorage and re-sent on the next page load and after the next successful write.
//
// Bounds, because an unbounded queue is its own failure mode:
//   - `_OUTBOX_MAX` entries, oldest dropped first. A permanently-broken backend must not fill her
//     storage, and the newest corrections are the ones worth keeping.
//   - `_OUTBOX_TTL_MS`. A correction stuck for a week is not evidence of what she'd write today, and
//     replaying it into the few-shot pool would teach Bean from stale context. Expired entries are
//     dropped, loudly, rather than silently resurrected.
// Delivery is at-least-once, not exactly-once: the server appends to a JSONL log, so a duplicate is
// a repeated exemplar, not corruption. Losing one is far worse than logging one twice.
const _OUTBOX_KEY = 'bean.correction.outbox';
const _OUTBOX_MAX = 50;
const _OUTBOX_TTL_MS = 7 * 24 * 60 * 60 * 1000;

function _outboxRead() {
  try {
    const raw = window.localStorage.getItem(_OUTBOX_KEY);
    const list = raw ? JSON.parse(raw) : [];
    return Array.isArray(list) ? list : [];
  } catch (e) { return []; }  // private browsing / quota / corrupt value: behave as an empty outbox
}
function _outboxWrite(list) {
  try { window.localStorage.setItem(_OUTBOX_KEY, JSON.stringify(list.slice(-_OUTBOX_MAX))); }
  // Console-only on purpose. This runs INSIDE the failure path (the POST already failed and a toast
  // already told her), and localStorage throwing means quota or private browsing — neither of which
  // a second toast can fix. It is a lossy last resort, not a silent success: the correction was
  // already reported as failed before we tried to park it.
  catch (e) { console.error('Bean outbox: cannot persist queued corrections', e); }
}
function _outboxPark(payload) {
  const list = _outboxRead();
  list.push({ at: Date.now(), payload: payload });
  if (list.length > _OUTBOX_MAX) console.error('Bean outbox full — dropping the oldest correction');
  _outboxWrite(list);
}

// Retry every parked correction. Called on load and after any successful send, so a recovered server
// drains the backlog without the operator doing anything. Sequential on purpose: the log is
// append-only and order is the only thing that makes "their last three replies" mean anything.
async function flushCorrectionOutbox() {
  const list = _outboxRead();
  if (!list.length) return;
  const fresh = list.filter(e => Date.now() - (e.at || 0) < _OUTBOX_TTL_MS);
  if (fresh.length !== list.length) {
    console.error('Bean outbox: dropped ' + (list.length - fresh.length) + ' correction(s) older than the TTL');
  }
  const stuck = [];
  for (const entry of fresh) {
    let r;
    try { r = await _postCorrection(entry.payload); }
    catch (e) { stuck.push(entry); continue; }         // still offline; keep it for next time
    if (r.ok || r.status === 404) continue;            // delivered, or no backend to deliver to
    if (r.status >= 500) { stuck.push(entry); continue; }
    console.error('Bean outbox: server rejected a queued correction (' + r.status + '); discarding it');
  }                                                    // a 4xx will never succeed — don't retry forever
  _outboxWrite(stuck);
  if (stuck.length) _signalWriteFailed('correction', 'POST /api/correction (queued)', 0, true);
}

function _postCorrection(payload) {
  return fetch('/api/correction', {
    method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(payload),
  });
}

// One human action → the correction log. A rejected fetch or a 404 is the no-backend static demo and
// stays silent. A 5xx is a likely-transient restart: retry once after a beat (an instant retry just
// hits the same dead server), then PARK it rather than drop it. A 4xx means the payload itself is
// bad — resending it would fail identically, so surface it and don't queue a poison pill.
function recordCorrection(payload) {
  try {
    _postCorrection(payload).then(r => {
      if (r.ok) return flushCorrectionOutbox();  // a working server: drain anything parked earlier
      if (r.status === 404) return;
      if (r.status >= 500) {
        return new Promise(res => setTimeout(res, 1500))
          .then(() => _postCorrection(payload))
          .then(r2 => {
            if (r2.ok) return flushCorrectionOutbox();
            if (r2.status === 404) return;
            const parked = r2.status >= 500;
            if (parked) _outboxPark(payload);            // keep it; the server will come back
            _signalWriteFailed('correction', 'POST /api/correction', r2.status, parked);
          })
          .catch(() => { _outboxPark(payload); });        // network died mid-retry: park, don't drop
        }
      _signalWriteFailed('correction', 'POST /api/correction', r.status, false);  // 4xx: truly lost
    }).catch(() => { _outboxPark(payload); });            // offline after the click: park, don't drop
  } catch (e) { _outboxPark(payload); }
}

// ---------- Header ----------
function TopBar({ onOpenAdmin, onOpenNotebook, onTryEmail, onReopenOnboarding, onOpenTeach, teachLeft, connected }) {
  return React.createElement('header', { className: 'topbar' },
    React.createElement('div', { className: 'brand' },
      React.createElement(window.BeanMark, { size: 30, bob: true }),
      React.createElement('div', null,
        React.createElement('div', { className: 'wordmark' }, 'Bean.'),
        React.createElement('div', { className: 'tagline' }, 'inbox triage')
      )
    ),
    React.createElement('div', { className: 'topbar-right' },
      // Honest connection state: green "Connected" once real forwarded mail has arrived (the only
      // signal that proves her setup works); otherwise amber "Finish setup". Click re-opens the
      // forwarding onboarding either way.
      React.createElement('span', {
        className: 'proton-pill' + (connected ? ' is-live' : ''),
        onClick: onReopenOnboarding,
        style: onReopenOnboarding ? { cursor: 'pointer' } : undefined,
        title: connected ? 'Bean is receiving your forwarded mail' : 'Finish your forwarding setup',
      },
        React.createElement('span', { className: 'proton-dot' }),
        connected ? 'Connected' : 'Finish setup'
      ),
      onTryEmail && React.createElement('button', { className: 'teach-btn', onClick: onTryEmail, title: 'Paste a real customer email and watch Bean triage it live' }, '✎ Paste email'),
      // Teaching Bean is walking her notebook — going over what Bean inferred from her mail, one
      // claim at a time. Always present (there is always something to refine), with a "N left" hint
      // only mid-walk, so a half-finished 45-card review advertises its own resume.
      // Emphasis follows WORK TO DO, not identity. This button used to carry the accent border
      // permanently, which in a row of four otherwise-identical peers reads as "currently selected"
      // — so the eye kept landing on a tab that was never active. Now the accent appears only
      // mid-walk, where it means something ("you left 12 cards") and matches the count beside it.
      onOpenTeach && React.createElement('button', {
        className: 'teach-btn', onClick: onOpenTeach, title: 'Go over what Bean learned from your mail — one thing at a time, and tell me what me got right',
        style: teachLeft ? { color: 'var(--bean)', borderColor: 'var(--bean)' } : undefined,
      }, '🫘 Teach Bean' + (teachLeft ? ' (' + teachLeft + ' left)' : '')),
      onOpenNotebook && React.createElement('button', { className: 'teach-btn', onClick: onOpenNotebook, title: 'Read and edit Bean’s brain — your buckets, standard answers, facts and judgment' }, '📓 Notebook'),
      onOpenAdmin && React.createElement('button', { className: 'teach-btn', onClick: onOpenAdmin, title: 'Bean’s policy — when to check with you, what me knows about your store' }, '⚙ Settings')
    )
  );
}

// ---------- Paste-an-email (live triage of a real, pasted email via POST /api/preview) ----------
// Maps the backend's snake_case DraftResult into the prototype email shape the DraftView reads,
// so a pasted email flows through the exact same review UI as a fixture email.
// A model-spending fetch came back non-OK. If the backend says the API credit balance is dry
// (402 out_of_credits), announce it app-wide (bean-root flashes the one sleepy "top up" toast) and
// return an error the inline catches recognise (`.outOfCredits`) so they show the credit line
// instead of the misleading "is the server running?". Any other failure → a plain status error.
async function modelFetchError(r, where) {
  let code = '';
  try { code = ((await r.json()) || {}).error_code || ''; } catch (e) { /* non-JSON body */ }
  if (r.status === 402 && code === 'out_of_credits') {
    window.dispatchEvent(new CustomEvent('bean:out-of-credits'));
    const e = new Error('out of API credits');
    e.outOfCredits = true;
    return e;
  }
  return new Error(where + ' ' + r.status);
}

async function previewEmail(input, opts) {
  const r = await fetch('/api/preview', {
    method: 'POST', headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ email: {
      id: 'paste-' + Date.now(),
      sender_name: input.name, sender_email: input.email,
      subject: input.subject, body: input.body,
    }, skipGate: !!(opts && opts.skipGate) }),
  });
  if (!r.ok) throw await modelFetchError(r, 'POST /api/preview');
  const res = await r.json();
  const base = {
    id: res.email_id || ('paste-' + Date.now()),
    from: { name: input.name || 'Customer', email: input.email || '' },
    subject: input.subject || '(no subject)',
    time: 'just now',
    body: (input.body || '').split(/\n\s*\n/).map(s => s.trim()).filter(Boolean),
    thread: [], orders: [],
  };
  return applyResult(res, base);
}

// The notebook engine's groundedness axis → the card's three confidence states. green is one-tap
// (like HIGH), yellow is "looks right?" (LOW), red is "yours" (FLAG) — the same three affordances,
// so DraftView needs no third vocabulary.
const GROUNDING_TO_CONF = { green: 'high', yellow: 'low', red: 'flag' };

// A one-line summary for the card body — a DraftResult carries no `summary`, so derive one from its
// groundedness + the first thing it's unsure of.
function notebookSummary(grounding, why) {
  if (grounding === 'green') return 'Grounded in your notebook and your own past replies.';
  return (why && why[0]) || (grounding === 'yellow'
    ? 'A couple of things worth a glance before it goes.'
    : 'This one needs your judgment — me won’t fake it.');
}

// Map a snake_case DraftResult (from /api/preview OR a stored /api/inbox item) onto a prototype
// email `base`. The single mapping both the paste path and the live inbox reuse, so a fixture,
// a pasted email, and a webhook-delivered email all render through the exact same DraftView.
function applyResult(res, base) {
  // The triage gate filed it — no draft was generated. The UI shows it demoted, never hidden;
  // "needs a reply" re-previews with skipGate (and logs the mis-file — see bean-root).
  if (res.disposition === 'filed') {
    return { ...base, filed: true, gateKind: res.kind, gateReason: res.reason,
             category: 'Filed / FYI', summary: res.reason || 'No reply needed.' };
  }
  // The engine stores a situation bucket + groundedness (green/yellow/red) + the sources it cited.
  // Mapped onto the view model DraftView renders — green→high, yellow→low, red→flag. A RED may
  // still carry a draft attempt: it is kept and rendered, never a faked confident reply.
  const why = res.why_unsure || [];
  return {
    ...base, category: res.bucket, confidence: GROUNDING_TO_CONF[res.confidence] || 'flag',
    draft: res.draft || '', citations: res.citations || [],
    summary: notebookSummary(res.confidence, why),
    concerns: why,            // yellow: "why me's not sure"
    flagReason: why.join(' '),  // red: why it wants her eyes
  };
}

// Pull the live triaged inbox (webhook-delivered mail) from the backend, mapped into the prototype
// email shape. Returns [] on any failure or an empty inbox — the caller then keeps window.EMAILS
// (the baked fixtures), so the offline/SSG demo and the paste flow are untouched.
async function loadInbox() {
  try {
    const r = await fetch('/api/inbox');
    if (!r.ok) throw new Error('GET /api/inbox ' + r.status);
    const data = await r.json();
    return (data.emails || []).map(item => applyResult(item.result || {}, {
      id: item.id,
      from: { name: item.sender_name || 'Customer', email: item.sender_email || '' },
      subject: item.subject || '(no subject)',
      time: item.received_at || '',
      body: (item.body || '').split(/\n\s*\n/).map(s => s.trim()).filter(Boolean),
      thread: [], orders: [],
    }));
  } catch (e) { return []; }  // offline/empty: fall back to window.EMAILS
}

async function loadLearning() {
  // Per-category learning stats (GET /api/learning) — how many of the operator's past replies now feed
  // each branch's drafts. Empty {} offline/pre-loop, so the maturity meter just reads "not yet".
  try {
    const r = await fetch('/api/learning');
    if (!r.ok) throw new Error('GET /api/learning ' + r.status);
    return await r.json();
  } catch (e) { return {}; }
}

// Gate rules Bean derived from the mis-files the operator already logged (GET /api/gate-proposals) —
// the consumer the gate never had. Empty [] offline or on a fresh install, so "What I handle" just
// renders its two textareas as before; a proposal surface that errors must not cost them the control.
async function loadGateProposals() {
  try {
    const r = await fetch('/api/gate-proposals');
    if (!r.ok) throw new Error('GET /api/gate-proposals ' + r.status);
    return ((await r.json()) || {}).proposals || [];
  } catch (e) { return []; }
}

// The raw correction log, browsable (GET /api/corrections) — the operator view of what Bean has
// learned. Each row: what the operator did + whether it became a usable exemplar vs a takeover
// (teaches nothing). Empty offline/pre-loop. Passcode-gated (it carries their mail + replies).
async function loadCorrections() {
  try {
    const r = await fetch('/api/corrections');
    if (!r.ok) throw new Error('GET /api/corrections ' + r.status);
    const d = (await r.json()) || {};
    return { corrections: d.corrections || [], count: d.count || 0, exemplars: d.exemplars || 0 };
  } catch (e) { return { corrections: [], count: 0, exemplars: 0 }; }
}

// One past reply by email id — what a tappable `corpus:<email_id>` citation chip resolves to. Lives
// here (not in the cite sheet) so the sheet stays fetch-free: bean-root binds this as its onLoadReply
// prop, the same seam BeanSheet/BeanNotebook use. null = nothing on file (the sheet says so plainly
// rather than showing an empty box that reads like a lost reply).
async function loadReply(id) {
  try {
    const r = await fetch('/api/reply?id=' + encodeURIComponent(id));
    if (!r.ok) return null;
    return await r.json();
  } catch (e) { return null; }
}

// The onboarding walk's resume state — her answers-so-far, so a 45-card review survives being closed
// and reopened. Twins of loadStatus/upsertStatus: a READ that degrades to {} (she just starts fresh),
// and a fire-and-forget save on each card. Distinct from the notebook — the notebook is written ONCE
// at approval; this is the in-progress overlay, cleared server-side the moment that approval lands.
async function loadReviewProgress() {
  try {
    const r = await fetch('/api/notebook/review');
    if (!r.ok) throw new Error('GET /api/notebook/review ' + r.status);
    return (await r.json()) || {};
  } catch (e) { return {}; }  // offline/pre-start: an empty walk, nothing lost
}
// Save the WHOLE decisions object on each tap (a resume snapshot, not an event log — the server
// replaces, not merges). Fire-and-forget like upsertStatus: the worst case on a dropped write is she
// re-answers a card or two on reload, never a lost notebook (that's the approval PUT, which is guarded).
function saveReviewProgress(decisions) {
  try {
    fetch('/api/notebook/review', {
      method: 'PUT', headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(decisions || {}),
    }).then(r => {
      if (!r.ok && r.status !== 404) _signalWriteFailed('notebook-review', 'PUT /api/notebook/review', r.status);
    // Every refusal the server can express is surfaced above. These two catches only see a rejected
    // fetch (offline demo, sleeping laptop) on a RESUME snapshot — the next card tap re-sends the
    // whole decisions object, so a dropped one costs nothing. Her approval is a different, guarded
    // write (PUT /api/notebook); nothing she has taught can be lost here.
    }).catch(() => {});
  } catch (e) {}
}

function PasteField({ label, value, onChange, placeholder }) {
  return React.createElement('div', { className: 'admin-field' },
    React.createElement('div', { className: 'admin-label' }, label),
    React.createElement('input', { className: 'admin-input', value, placeholder,
      onChange: e => onChange(e.target.value) })
  );
}

function PasteView({ onSubmit, onBack }) {
  const [name, setName] = useState('');
  const [email, setEmail] = useState('');
  const [subject, setSubject] = useState('');
  const [body, setBody] = useState('');
  const [busy, setBusy] = useState(false);
  const [err, setErr] = useState(null);
  const canSubmit = body.trim() && !busy;
  async function go() {
    setBusy(true); setErr(null);
    try { await onSubmit({ name, email, subject, body }); }
    catch (e) {
      setErr(e.outOfCredits ? window.BEAN_OOC_MSG : 'Bean couldn’t reach the server — is it running? (' + e.message + ')');
      setBusy(false);
    }
  }
  return React.createElement('div', { className: 'draft-view', style: { maxWidth: 640, margin: '0 auto' } },
    React.createElement('button', { className: 'back-btn', onClick: onBack }, '← inbox'),
    React.createElement('div', { className: 'greet-card', style: { marginBottom: 18 } },
      React.createElement(window.BeanMark, { size: 50, bob: true, className: 'greet-bean' }),
      React.createElement('div', { className: 'greet-text' },
        React.createElement('h1', null, 'Try a real email'),
        React.createElement('p', null, 'Paste a customer email and I’ll triage it against your knowledge — draft, confidence, and why. I never autosend.')
      )
    ),
    React.createElement('div', { className: 'email-panel', style: { position: 'static' } },
      React.createElement(PasteField, { label: 'From — name', value: name, onChange: setName, placeholder: 'e.g. Dana R.' }),
      React.createElement(PasteField, { label: 'From — email', value: email, onChange: setEmail, placeholder: 'dana@example.com' }),
      React.createElement(PasteField, { label: 'Subject', value: subject, onChange: setSubject, placeholder: 'Will the 3-seater fit through a 30-inch doorway?' }),
      React.createElement('div', { className: 'admin-field' },
        React.createElement('div', { className: 'admin-label' }, 'Email body'),
        React.createElement('textarea', { className: 'admin-textarea', rows: 7, value: body,
          placeholder: 'Paste the customer’s message here…', onChange: e => setBody(e.target.value) })
      ),
      err && React.createElement('div', { className: 'gap-warn', style: { marginTop: 12 } }, err),
      React.createElement('div', { style: { marginTop: 16 } },
        React.createElement('button', {
          className: 'admin-add-btn', onClick: canSubmit ? go : undefined, disabled: !canSubmit,
          style: { opacity: canSubmit ? 1 : 0.5, cursor: canSubmit ? 'pointer' : 'default' },
        }, busy ? 'Bean is thinking…' : 'Ask Bean →')
      )
    )
  );
}

// ---------- Inbox ----------
// Filed mail has no confidence bucket (the gate ran, no draft was generated) — it gets its own
// neutral palette instead of a window.CONF lookup.
const FILED_STYLE = { dot: '#C7BCA8', bg: '#F4EFE6', color: '#8A7F6C', line: '#E4DCCB' };

// ---------- Inbox filter (client-side facet lens) ----------
// A live inbox runs long (100+ on the first real operator). These pure helpers narrow it by facets already present on
// each loaded email — no server round-trip, no new fields. Side-effect-free so the JS-eval harness
// can test them without a DOM (tests/test_bean_inbox_js.py), and so the one invariant that matters
// can be proven: an empty selection returns the input UNCHANGED. A filter is a lens over the same
// mail, never a gate that hides it — dropping a real customer email is a worse failure than a bad
// draft, so the default (nothing selected) must stay byte-identical to the un-filtered inbox.

// The facet VALUE each email carries, normalised once so the filter and its option list agree.
// Filed mail filters by its FYI KIND (Promotion, Sales pitch, Order update, …), not the flat
// 'Filed / FYI' bucket — "show me only the sales pitches" is the real ask. The displayed badge
// (e.category) still reads 'Filed / FYI'; this seam is the filter's view, not the row's.
function inboxCategory(e) { return e.filed ? window.friendlyKind(e.gateKind) : (e.category || ''); }
function inboxConfidence(e) { return e.filed ? 'filed' : e.confidence; }  // gate-filed mail has none
// status.json stores approved|handled|skipped|pending|undefined; the inbox only ever cared about
// three buckets (open vs done vs snoozed), so the filter speaks those, not the raw enum.
function inboxStatusBucket(e, statusMap) {
  const s = (statusMap || {})[e.id];
  if (!s || s === 'pending') return 'unhandled';  // no status and 'pending' are the same "not yet"
  if (s === 'skipped') return 'snoozed';
  return 'handled';                                 // approved | handled
}

// Return the subset of `emails` that passes the current selection. Facets AND together; within one
// facet the selected values OR. THE INVARIANT: nothing selected ⇒ return `emails` itself (same
// array, same order), so the default inbox is identical to before this feature existed.
function applyInboxFilters(emails, filter, statusMap) {
  const f = filter || {};
  const cats = f.categories || [], confs = f.confidences || [], stats = f.statuses || [];
  if (!cats.length && !confs.length && !stats.length) return emails;  // additive: no lens, no change
  return (emails || []).filter(e => {
    if (cats.length && cats.indexOf(inboxCategory(e)) === -1) return false;
    if (confs.length && confs.indexOf(inboxConfidence(e)) === -1) return false;
    if (stats.length && stats.indexOf(inboxStatusBucket(e, statusMap)) === -1) return false;
    return true;
  });
}

// The option lists for the filter bar, DERIVED FROM THE LOADED MAIL — never a hard-coded copy of her
// 26-category tree, which changes. Confidence and status are fixed enums shown in canonical order,
// but still only when actually present, so the bar reflects THIS inbox and nothing more.
function inboxFacets(emails, statusMap) {
  // Topics (routed tree categories) and fyiKinds (gate-filed lanes) are split so the label dropdown
  // can group them under headers — but both are still `inboxCategory` VALUES, so one `categories`
  // selection filters either without a second facet dimension.
  const cats = new Set(), kinds = new Set(), confs = new Set(), stats = new Set();
  (emails || []).forEach(e => {
    (e.filed ? kinds : cats).add(inboxCategory(e));
    confs.add(inboxConfidence(e));
    stats.add(inboxStatusBucket(e, statusMap));
  });
  const CONF_ORDER = ['high', 'low', 'flag', 'filed'];
  const STAT_ORDER = ['unhandled', 'handled', 'snoozed'];
  const bylabel = (a, b) => a.localeCompare(b);
  return {
    categories: Array.from(cats).filter(Boolean).sort(bylabel),
    fyiKinds: Array.from(kinds).filter(Boolean).sort(bylabel),
    confidences: CONF_ORDER.filter(k => confs.has(k)),
    statuses: STAT_ORDER.filter(k => stats.has(k)),
  };
}

function InboxRow({ email, status, onOpen }) {
  const c = email.filed ? FILED_STYLE : window.CONF[email.confidence];
  const done = status && status !== 'pending';
  return React.createElement('button', {
    className: 'inbox-row' + (done ? ' is-done' : ''),
    onClick: () => onOpen(email.id),
    style: { borderLeft: `4px solid ${c.dot}` },
  },
    React.createElement('div', { className: 'row-avatar', style: { background: c.bg, color: c.color } },
      email.from.name.split(' ').map(n => n[0]).join('').slice(0, 2)
    ),
    React.createElement('div', { className: 'row-main' },
      React.createElement('div', { className: 'row-line1' },
        React.createElement('span', { className: 'row-from' }, email.from.name),
        React.createElement(window.CategoryTag, null, email.category),
        email.thread && email.thread.length > 0 && React.createElement('span', {
          className: 'thread-pill', title: 'Part of an ongoing conversation',
        }, '↩ ' + (email.thread.length + 1) + ' msgs')
      ),
      React.createElement('div', { className: 'row-subject' }, email.subject),
      React.createElement('div', { className: 'row-snippet' }, email.summary)
    ),
    React.createElement('div', { className: 'row-right' },
      done
        ? React.createElement('span', { className: 'row-status ' + status }, statusLabel(status))
        : email.filed
          ? React.createElement('span', { className: 'row-status',
              style: { color: FILED_STYLE.color, background: FILED_STYLE.bg } },
              window.friendlyKind(email.gateKind))
          : React.createElement(window.ConfidenceBadge, { level: email.confidence, size: 'sm' }),
      React.createElement('span', { className: 'row-time' }, email.time)
    )
  );
}

function statusLabel(s) {
  return s === 'approved' ? '✓ Sent' : s === 'handled' ? '✓ Handled' : s === 'skipped' ? 'Snoozed' : '';
}

function InboxGroup({ level, emails, status, onOpen }) {
  if (!emails.length) return null;
  const c = window.CONF[level];
  return React.createElement('section', { className: 'inbox-group' },
    React.createElement('div', { className: 'group-head' },
      React.createElement('span', { className: 'group-dot', style: { background: c.dot } }),
      React.createElement('h2', null, c.label),
      React.createElement('span', { className: 'group-blurb' }, c.blurb),
      React.createElement('span', { className: 'group-count' }, emails.length)
    ),
    React.createElement('div', { className: 'group-rows' },
      emails.map(e => React.createElement(InboxRow, { key: e.id, email: e, status: status[e.id], onOpen }))
    )
  );
}

// The demoted lane: filed mail is sorted & demoted, NEVER hidden (silent-hide is a worse trust
// failure than a bad draft — a mis-filed customer is a ghosted customer). Collapsed by default,
// one glance away; opening a row offers the "needs a reply" recovery.
function FiledGroup({ emails, status, onOpen, onClearFiled }) {
  const [open, setOpen] = useState(false);
  if (!emails.length) return null;
  return React.createElement('section', { className: 'inbox-group done-group' },
    React.createElement('div', { className: 'group-head filed-head' },
      React.createElement('button', {
        type: 'button', onClick: () => setOpen(o => !o),
        className: 'filed-toggle', style: { flex: 1, textAlign: 'left', display: 'flex', alignItems: 'center', gap: 8, background: 'none', border: 'none', cursor: 'pointer', fontFamily: 'inherit', padding: 0 },
      },
        React.createElement('span', { className: 'group-dot', style: { background: FILED_STYLE.dot } }),
        React.createElement('h2', null, 'Filed / FYI'),
        React.createElement('span', { className: 'group-blurb' }, 'no reply needed — me filed these, but never hides them'),
        React.createElement('span', { className: 'group-count' }, emails.length),
        React.createElement('span', { style: { color: '#B6A789', fontSize: 12, marginLeft: 6 } }, open ? '▾' : '▸')
      ),
      onClearFiled && React.createElement('button', {
        type: 'button', className: 'clear-fyi', onClick: onClearFiled,
        title: 'Delete the filed FYI mail (your customer mail is untouched; recoverable from a backup)',
      }, 'Clear FYI')
    ),
    open && React.createElement('div', { className: 'group-rows' },
      emails.map(e => React.createElement(InboxRow, { key: e.id, email: e, status: status[e.id], onOpen }))
    )
  );
}

// The facet bar. A pill row per facet, options DERIVED from the loaded mail (window.inboxFacets), so
// it tracks her tree instead of a frozen copy of it. A facet group is shown only when it has more
// than one value — filtering by the sole present value is a no-op, and a bar full of dead controls is
// worse than no bar. Reuses the app's tokens (var(--*)) + the CONF/FILED palette so it reads as Bean,
// not a bolted-on widget. Purely additive: it changes the SELECTION, never the mail.
function InboxFilterBar({ facets, filter, onChange, shown, total }) {
  const f = filter || {};
  const cats = f.categories || [], confs = f.confidences || [], stats = f.statuses || [];
  const active = cats.length + confs.length + stats.length > 0;
  // Toggle one value in a facet array (add if absent, remove if present) — the OR within a facet.
  const toggle = (key, val) => {
    const cur = f[key] || [];
    const next = cur.indexOf(val) === -1 ? cur.concat([val]) : cur.filter(v => v !== val);
    onChange({ ...f, [key]: next });
  };
  const confMeta = lvl => lvl === 'filed'
    ? { label: 'Filed', dot: FILED_STYLE.dot }
    : { label: window.CONF[lvl].label, dot: window.CONF[lvl].dot };
  const STAT_LABEL = { unhandled: 'Unhandled', handled: 'Handled', snoozed: 'Snoozed' };

  const pill = (on, onClick, dot, label, key) => React.createElement('button', {
    key, type: 'button', className: 'filter-pill' + (on ? ' is-on' : ''), onClick,
  },
    dot && React.createElement('span', { className: 'fp-dot', style: { background: dot } }),
    label
  );

  const groups = [];
  // Label as a single-select dropdown — she has up to 26 topics, too many for a pill row, and
  // "show me only Sofas & Upholstery" is the driving case. Topics and the filed FYI kinds (Promotion, Sales
  // pitch, …) live in one dropdown under two optgroup headers. Empty value = all (additive default).
  const optgroup = (label, values) => values.length
    ? React.createElement('optgroup', { key: label, label },
        values.map(c => React.createElement('option', { key: c, value: c }, c)))
    : null;
  if (facets.categories.length + facets.fyiKinds.length > 1) {
    groups.push(React.createElement('div', { className: 'inbox-filter-group', key: 'cat' },
      React.createElement('span', { className: 'inbox-filter-label' }, 'Label'),
      React.createElement('div', { className: 'category-select-wrap' },
        React.createElement('select', {
          className: 'category-select', value: cats[0] || '',
          onChange: e => onChange({ ...f, categories: e.target.value ? [e.target.value] : [] }),
        },
          React.createElement('option', { value: '' }, 'All labels'),
          optgroup('Topics', facets.categories),
          optgroup('Filed / FYI', facets.fyiKinds)
        )
      )
    ));
  }
  if (facets.confidences.length > 1) {
    groups.push(React.createElement('div', { className: 'inbox-filter-group', key: 'conf' },
      React.createElement('span', { className: 'inbox-filter-label' }, 'Confidence'),
      facets.confidences.map(lvl => {
        const m = confMeta(lvl);
        return pill(confs.indexOf(lvl) !== -1, () => toggle('confidences', lvl), m.dot, m.label, lvl);
      })
    ));
  }
  if (facets.statuses.length > 1) {
    groups.push(React.createElement('div', { className: 'inbox-filter-group', key: 'stat' },
      React.createElement('span', { className: 'inbox-filter-label' }, 'Status'),
      facets.statuses.map(s =>
        pill(stats.indexOf(s) !== -1, () => toggle('statuses', s), null, STAT_LABEL[s], s))
    ));
  }
  if (!groups.length) return null;  // nothing worth filtering in this inbox

  return React.createElement('div', { className: 'inbox-filter' },
    groups,
    React.createElement('div', { className: 'inbox-filter-count' },
      React.createElement('span', null,
        active ? ('Showing ' + shown + ' of ' + total) : (total + ' email' + (total === 1 ? '' : 's'))
      ),
      active && React.createElement('button', {
        className: 'inbox-filter-clear', onClick: () => onChange({}),
      }, 'Clear filters')
    )
  );
}

function Inbox({ status, pasted, filter, onFilterChange, onOpen, onApproveAllHigh, onClearFiled }) {
  const emails = window.EMAILS;
  const isOpen = e => !status[e.id] || status[e.id] === 'pending';
  // --- The morning summary counts what ARRIVED, always the whole inbox, never the lens — otherwise a
  // label filter would rewrite "73 came in" to "3" and read as a lie. So the greet + Approve-all use
  // these full sets; only the rendered groups below get filtered.
  const open = emails.filter(isOpen);
  // Filed pasted mail (a gate-filed newsletter you tried) joins the demoted lane too, so its
  // "needs a reply" recovery survives navigating back to the inbox — never hidden.
  const pastedFiled = (pasted || []).filter(e => e.filed && isOpen(e));
  const filed = [...pastedFiled, ...open.filter(e => e.filed)];
  const pending = open.filter(e => !e.filed);
  const high = pending.filter(e => e.confidence === 'high');
  const low = pending.filter(e => e.confidence === 'low');
  const flag = pending.filter(e => e.confidence === 'flag');
  const allDone = pending.length === 0;

  // --- The lens over the rendered groups. Empty filter ⇒ applyInboxFilters returns the same arrays,
  // so every group below is byte-identical to the un-filtered inbox (the additive invariant). Options
  // are derived from the WHOLE inbox so narrowing never removes a choice you might want next.
  const facets = window.inboxFacets([...emails, ...pastedFiled], status);
  const vEmails = window.applyInboxFilters(emails, filter, status);
  const vPastedFiled = window.applyInboxFilters(pastedFiled, filter, status);
  const vOpen = vEmails.filter(isOpen);
  const vFiled = [...vPastedFiled, ...vOpen.filter(e => e.filed)];
  const vPending = vOpen.filter(e => !e.filed);
  const vHigh = vPending.filter(e => e.confidence === 'high');
  const vLow = vPending.filter(e => e.confidence === 'low');
  const vFlag = vPending.filter(e => e.confidence === 'flag');
  const vDone = vEmails.filter(e => status[e.id] && status[e.id] !== 'pending');
  const totalCount = emails.length + pastedFiled.length;
  const shownCount = vEmails.length + vPastedFiled.length;
  const filterActive = ((filter && filter.categories) || []).length
    + ((filter && filter.confidences) || []).length
    + ((filter && filter.statuses) || []).length > 0;

  return React.createElement('div', { className: 'inbox' },
    // Greeting / triage summary
    React.createElement('div', { className: 'greet-card' },
      React.createElement(window.BeanMark, { size: 58, bob: true, className: 'greet-bean' }),
      React.createElement('div', { className: 'greet-text' },
        allDone
          ? React.createElement(React.Fragment, null,
              React.createElement('h1', null, 'Inbox zero. Nice.'),
              React.createElement('p', null, 'Everything triaged. I only ever drafted — you approved every send. Reset the demo to run it again.')
            )
          : React.createElement(React.Fragment, null,
              // Named when the tenant has told Bean who it is talking to, plain when it hasn't —
              // "Morning." is a complete greeting, and a placeholder name would be worse than none.
              React.createElement('h1', null,
                window.operatorName() ? 'Morning, ' + window.operatorName() + '.' : 'Morning.'),
              React.createElement('p', null,
                React.createElement('b', null, pending.length + ' emails'), ' came in. Me sorted them, surest first: ',
                React.createElement('b', { style: { color: window.CONF.high.color } }, high.length + ' ready'), ' to send, ',
                React.createElement('b', { style: { color: window.CONF.low.color } }, low.length + ' worth a look'), ', and ',
                React.createElement('b', { style: { color: window.CONF.flag.color } }, flag.length + ' me\'s escalating'), ' to you. Me never autosend.',
                filed.length > 0 ? React.createElement('span', null, ' (Plus ',
                  React.createElement('b', { style: { color: FILED_STYLE.color } }, filed.length + ' filed as FYI'),
                  ' — below, never hidden.)') : null
              )
            )
      ),
      !allDone && high.length > 0 && React.createElement('div', { className: 'greet-action' },
        React.createElement(window.Btn, { kind: 'ghost', onClick: onApproveAllHigh },
          'Approve all ' + high.length + ' ready'
        )
      )
    ),
    React.createElement(InboxFilterBar, {
      facets, filter, onChange: onFilterChange, shown: shownCount, total: totalCount,
    }),
    // A filter that matches nothing must say so, not look like an empty inbox — and it must always
    // offer the one tap back to every email (the lens is never a trap).
    filterActive && shownCount === 0 && React.createElement('div', { className: 'inbox-empty-filter' },
      React.createElement('span', null, 'No mail matches these filters. '),
      React.createElement('button', { className: 'inbox-filter-clear', onClick: () => onFilterChange({}) }, 'Clear filters')
    ),
    React.createElement(InboxGroup, { level: 'high', emails: vHigh, status, onOpen }),
    React.createElement(InboxGroup, { level: 'low', emails: vLow, status, onOpen }),
    React.createElement(InboxGroup, { level: 'flag', emails: vFlag, status, onOpen }),
    React.createElement(FiledGroup, { emails: vFiled, status, onOpen, onClearFiled }),
    // Already-handled, collapsed at bottom
    (() => {
      const done = vDone;
      if (!done.length) return null;
      return React.createElement('section', { className: 'inbox-group done-group' },
        React.createElement('div', { className: 'group-head' },
          React.createElement('span', { className: 'group-dot', style: { background: '#C7BCA8' } }),
          React.createElement('h2', null, 'Handled today'),
          React.createElement('span', { className: 'group-count' }, done.length)
        ),
        React.createElement('div', { className: 'group-rows' },
          done.map(e => React.createElement(InboxRow, { key: e.id, email: e, status: status[e.id], onOpen }))
        )
      );
    })()
  );
}

window.Inbox = Inbox;
window.TopBar = TopBar;
window.PasteView = PasteView;
// The pure filter helpers — exported so the component and the JS-eval test share one implementation.
window.applyInboxFilters = applyInboxFilters;
window.inboxFacets = inboxFacets;
// Delete the FYI (gate-filed) mail server-side. The server keeps a volume backup and only ever
// removes filed lines (never a customer email), so this is a one-tap cleanup, not a risky purge.
async function clearFiled() {
  const r = await fetch('/api/clear-filed', { method: 'POST', headers: { 'Content-Type': 'application/json' } });
  if (!r.ok) throw new Error('POST /api/clear-filed ' + r.status);
  return r.json();  // { ok, removed, kept }
}

window.beanStore = { loadStatus, upsertStatus, loadConfig, saveConfig, configSettled, defaultConfig, loadNotebook, saveNotebook, recordCorrection, flushCorrectionOutbox, previewEmail, loadInbox, loadLearning, loadGateProposals, loadCorrections, loadReply, loadReviewProgress, saveReviewProgress, clearFiled };
