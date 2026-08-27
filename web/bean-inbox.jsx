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
// `source` says where the edit came from ('chat' when Bean proposed it and she confirmed, else
// 'editor'). It rides in a HEADER, not the body — the body IS the notebook, and a stray key in it
// would be pushed through Notebook.from_dict on the server.
async function saveNotebook(nb, source) {
  let r;
  try {
    r = await fetch('/api/notebook', {
      method: 'PUT',
      headers: {
        'Content-Type': 'application/json',
        'If-Match': _notebookEtag || '',
        'X-Bean-Source': source === 'chat' ? 'chat' : 'editor',
      },
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
function TopBar({ onOpenInbox, onOpenAdmin, onOpenStats, onOpenNotebook, onTryEmail, onReopenOnboarding, onOpenTeach, teachLeft, connected, whatsNew }) {
  return React.createElement('header', { className: 'topbar' },
    React.createElement('div', { className: 'brand' },
      React.createElement(window.PlayfulMark, { size: 44, title: 'Press me — me make a coffee' }),
      // The wordmark is the way home, the way it is in every other app — and the mark beside it is
      // NOT. Two jobs, deliberately split down the middle of the brand: pressing the bean brews,
      // pressing the words goes to the inbox. Rolling them together would mean either losing the
      // Beanary or making "go home" a coin flip.
      React.createElement('button', {
        type: 'button', className: 'brand-home', onClick: onOpenInbox,
        title: 'Back to your inbox', 'aria-label': 'Back to your inbox',
      },
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
      // State on the left of the rule, actions on the right — six controls read as two groups
      // rather than one undifferentiated row.
      React.createElement('span', { className: 'topbar-sep', 'aria-hidden': true }),
      // NO ICONS on these five. They used to carry ✎ 🫘 📓 📊 ⚙ — two text glyphs and three
      // full-colour emoji, so a row of identical buttons rendered in two different weights and in
      // a palette (emoji blue, emoji red) that appears nowhere else in Bean. The colour ones also
      // shift with the OS emoji font, which is not a thing a brand should hand to Apple. The words
      // were already doing the work; the bean beside the wordmark is the only mark this row needs.
      onTryEmail && React.createElement('button', { className: 'teach-btn', onClick: onTryEmail, title: 'Paste a real customer email and watch Bean triage it live' }, 'Paste email'),
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
      },
        'Teach Bean',
        // A badge, not "(43 left)". The words cost ~45px and that was exactly enough to push
        // Settings onto a second row; the count reads as a count either way.
        teachLeft ? React.createElement('span', { className: 'teach-count', title: teachLeft + ' cards left' }, teachLeft) : null),
      onOpenNotebook && React.createElement('button', { className: 'teach-btn', onClick: onOpenNotebook, title: 'Read and edit Bean’s brain — your buckets, standard answers, facts and judgment' }, 'Notebook'),
      onOpenStats && React.createElement('button', { className: 'teach-btn', onClick: onOpenStats, title: 'What Bean has actually done with your mail — and the time it saved you' }, 'Report'),
      onOpenAdmin && React.createElement('button', {
        className: 'teach-btn', onClick: onOpenAdmin,
        title: whatsNew
          ? 'Me learned something new — “' + whatsNew.title + '”'
          : 'Bean’s policy — when to check with you, what me knows about your store',
      }, 'Settings',
        // The dot is the whole discoverability plan. A changelog buried in Settings, with nothing
        // pointing at it, tells her about new features only after she has already found them.
        whatsNew ? React.createElement('span', { className: 'new-dot', 'aria-label': 'something new' }) : null)
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

// Re-draft ONE conversation so a single reply answers everything still open in it. Server-side this
// rewrites inbox.jsonl (the new verdict, plus `rolled_into` on the earlier messages), so the caller
// re-fetches the inbox rather than patching state by hand — the log is the source of truth.
async function redraftEmail(emailId) {
  const r = await fetch('/api/redraft', {
    method: 'POST', headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ email_id: emailId }),
  });
  if (!r.ok) {
    const err = await modelFetchError(r, 'POST /api/redraft');
    // 409 is a real answer, not a fault: there is nothing else waiting in that conversation.
    if (r.status === 409) err.nothingToDo = true;
    throw err;
  }
  return await r.json();
}

// One turn of the operator talking to Bean about her notebook. Returns the message object the
// transcript renders — an `answer` with cites, or a `proposal` she has to confirm.
//
// This endpoint READS the notebook and never writes it. Applying an approved proposal goes through
// saveNotebook below, the one ETag-guarded writer, exactly like every other change to her brain.
async function askBean(message, transcript) {
  const r = await fetch('/api/chat', {
    method: 'POST', headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ message, transcript: transcript || [] }),
  });
  if (!r.ok) {
    const err = await modelFetchError(r, 'POST /api/chat');
    // Carry a line Bean can SAY. A chat that answers a rate limit with "POST /api/chat 429" is
    // talking to a developer, not to the operator.
    err.beanMessage = r.status === 429
      ? 'Me needs a breath — try that again in a moment.'
      : r.status === 503
        ? 'Me has no notebook to talk about yet.'
        : 'Me couldn’t reach my brain just then — say that again?';
    throw err;
  }
  return await r.json();
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
  // Set when a later message in the same conversation was drafted to answer this one too, so this
  // row folds into that draft instead of standing as a second near-duplicate reply to write.
  base = { ...base, rolledInto: res.rolled_into || '' };
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
      // The quoted conversation this email arrived with. This used to be hardcoded `[]`, so the
      // "↩ 3 msgs" pill and the draft screen's "Earlier from …" block only ever rendered for the
      // baked fixtures and NEVER for real mail — 108 of her 410 emails carry one. The writer
      // (bean/inbound.py) was right, the API served it, and the last hop dropped it on the floor.
      thread: item.thread || [],
      // The same history, unpacked by bean/quoting.py into the real messages it contains:
      // [{who, when, text, side}], oldest first. `thread` is one raw blob that hid up to thirteen
      // messages behind its quote markers, which is why every count derived from it read "1".
      conversation: item.conversation || [],
      orders: [],
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
// What changed in BEAN — the product. A static file in web/, so there is no endpoint: the app
// already serves this directory, and a changelog that describes the code should ship with it.
//
// ⚠️ Distinct from loadNotebookHistory below, and the two must not be confused: that one is what
// changed in HER NOTEBOOK, this one is what changed in the software. Same word, different subject.
async function loadWhatsNew() {
  try {
    const r = await fetch('/whats-new.json');
    if (!r.ok) throw new Error('GET /whats-new.json ' + r.status);
    const d = (await r.json()) || {};
    const entries = (d.entries || []).filter(e => e && e.date && e.title);
    // Newest first, and sorted here rather than trusting the file's order — the file is hand-edited.
    return entries.sort((a, b) => String(b.date).localeCompare(String(a.date)));
  } catch (e) { return []; }  // a READ of a static file; nothing is lost if it can't load
}

// The newest entry she has NOT seen, or null. Drives the dot on the ⚙ Settings button — a changelog
// whose job is telling her about new features fails if it is buried somewhere she never opens.
function whatsNewUnread(entries) {
  if (!entries || !entries.length) return null;
  let seen = '';
  try { seen = localStorage.getItem('bean_whatsnew_seen') || ''; } catch (e) { return null; }
  return String(entries[0].date) > seen ? entries[0] : null;
}

function markWhatsNewSeen(entries) {
  if (!entries || !entries.length) return;
  // Private browsing or storage disabled. The only cost is the dot coming back, so there is
  // nothing to tell her and nothing to retry.
  try { localStorage.setItem('bean_whatsnew_seen', String(entries[0].date)); } catch (e) {}
}

// Every change ever made to her notebook, newest first. Distinct from loadCorrections above:
// corrections are feedback on DRAFTS; this is edits to the brain those drafts come from.
async function loadNotebookHistory() {
  try {
    const r = await fetch('/api/notebook/history');
    if (!r.ok) throw new Error('GET /api/notebook/history ' + r.status);
    const d = (await r.json()) || {};
    return { changes: d.changes || [], count: d.count || 0 };
  } catch (e) { return { changes: [], count: 0 }; }  // a READ; nothing is lost if it can't load
}

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
      React.createElement(window.PlayfulMark, { size: 50, className: 'greet-bean', title: 'Press me — me do a little roast' }),
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
        },
          // The one wait in the app the operator triggers on purpose, so it gets the mark inline:
          // a button that only changes its words reads as frozen, one that is visibly roasting reads
          // as working. `mode: roast` at 20px — a dripper this small is three pixels of noise.
          busy
            ? React.createElement('span', { style: { display: 'inline-flex', alignItems: 'center', gap: 9 } },
                React.createElement(window.BeanRoast, { size: 20, mode: 'roast' }),
                'Bean is thinking…')
            : 'Ask Bean →')
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

function InboxRow({ email, status, onOpen, onClear, covers }) {
  const c = email.filed ? FILED_STYLE : window.CONF[email.confidence];
  const done = status && status !== 'pending';
  return React.createElement('button', {
    className: 'inbox-row' + (done ? ' is-done' : ''),
    onClick: () => onOpen(email.id),
    style: { borderLeft: `4px solid ${c.dot}` },
    // The demo tour's only hook into the inbox (web/bean-tour.jsx). It picks the row to point at by
    // CONFIDENCE, never by id, because which email lands in which bucket is the engine's call and
    // moves whenever the demo fixture is regenerated. Filed mail reads 'filed', not its confidence:
    // it is in the collapsed lane, and a tour that scrolls to something the visitor cannot see is
    // worse than one that skips the step.
    'data-conf': email.filed ? 'filed' : email.confidence,
  },
    React.createElement('div', { className: 'row-avatar', style: { background: c.bg, color: c.color } },
      email.from.name.split(' ').map(n => n[0]).join('').slice(0, 2)
    ),
    React.createElement('div', { className: 'row-main' },
      React.createElement('div', { className: 'row-line1' },
        React.createElement('span', { className: 'row-from' }, email.from.name),
        React.createElement(window.CategoryTag, null, email.category),
        // The real message count. This was `thread.length + 1`, and since `thread` is always a
        // ONE-element list holding the whole quoted history, that pill read "↩ 2 msgs" on every
        // email that had ever been replied to — including a thirteen-message chain.
        window.msgCount(email) > 1 && React.createElement('span', {
          className: 'thread-pill', title: 'Part of an ongoing conversation',
        }, '↩ ' + window.msgCount(email) + ' msgs'),
        // They wrote again before anyone answered. The count is the thing worth seeing from the
        // list: it is how long someone has been waiting, and this one draft answers all of it.
        // Worded as a fact about the customer, because that is true whether or not Bean grouped
        // the draft: mail triaged before conversations existed still shows the chase, it just does
        // not fold. "This draft answers all of them" is only claimed where it is actually the case
        // — inside the draft view, by ConversationStrip.
        covers > 1 && React.createElement('span', {
          className: 'chase-pill',
          title: 'They wrote ' + covers + ' times and none of it has been answered yet',
        }, 'wrote ' + covers + '× · no reply')
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
      React.createElement('span', { className: 'row-time', title: email.time },
        window.beanTimeLabel(email.time)),
      // Clear it without opening it. She answers plenty of this mail in Proton, and plenty more
      // she simply never wants Bean to have drafted — before this, both cost her a click into the
      // draft view and a button ("Take it over") whose name claims something she didn't do.
      //
      // A SPAN, not a button: `.inbox-row` is itself a <button>, and a nested button is invalid
      // HTML that browsers reparent out of the row. role+tabIndex+key handling gives the same
      // affordance without the nesting.
      //
      // Filed mail is excluded on purpose — the FYI lane has its own one-tap purge, and two
      // different clears on one row is two things to think about.
      !done && !email.filed && onClear && React.createElement('span', {
        className: 'row-clear', role: 'button', tabIndex: 0,
        title: 'Clear this one — me stops showing it, and me learns nothing from it',
        onClick: ev => { ev.stopPropagation(); onClear(email.id); },
        onKeyDown: ev => {
          if (ev.key !== 'Enter' && ev.key !== ' ') return;
          ev.stopPropagation(); ev.preventDefault(); onClear(email.id);
        },
      }, 'Clear')
    )
  );
}

function statusLabel(s) {
  return s === 'approved' ? '✓ Sent' : s === 'handled' ? '✓ Handled' : s === 'skipped' ? 'Snoozed' : '';
}

// --- conversations ------------------------------------------------------------------------------
// One customer writing two or three times before anyone answers. Measured on real traffic: 81 of
// 224 senders wrote more than once, and 145 of 186 consecutive same-sender pairs landed within 72h.
// She sends ONE reply to those, so the queue must show her one thing to act on, not three rows.
//
// The keying mirrors bean/conversation.py exactly — sender address plus the subject with every
// reply prefix peeled. Peeled repeatedly, because a long chain accretes them
// ("RE: [EXTERNAL] Re: Fwd: …"), and the address is what stops identical subjects from different
// customers collapsing into one conversation.
const THREAD_PREFIX_RE = /^\s*((re|fwd|fw|aw|sv)\s*:|\[[^\]]{1,20}\]\s*)+/i;

function conversationKey(email) {
  let s = (email.subject || '').trim();
  for (let prev = null; prev !== s; ) { prev = s; s = s.replace(THREAD_PREFIX_RE, '').trim(); }
  return ((email.from && email.from.email) || '').trim().toLowerCase() + '\n' + s.toLowerCase();
}

// Every email that shares `email`'s conversation, oldest first. Includes `email` itself, so a lone
// message returns a one-element list and every caller can treat both cases the same way.
function conversationOf(email, emails) {
  const key = conversationKey(email);
  return (emails || []).filter(e => conversationKey(e) === key).sort((a, b) => -byNewestFirst(a, b));
}

// The ids one action should cover: this email plus any earlier ones its draft was written to answer.
// Approving a conversation's draft has to mark the whole conversation, or the rows it folded in sit
// in the queue forever with no way to act on them — they no longer render a draft of their own.
function conversationIds(email, emails) {
  return conversationOf(email, emails)
    .filter(e => e.id === email.id || e.rolledInto === email.id)
    .map(e => e.id);
}

// Is this row already answered by another row in the same list? Only when the email it was folded
// into is actually PRESENT — otherwise a filter lens, or a sibling she has already sent, would make
// a message vanish with nothing left to open. Silent-hide is the worse trust failure; when in doubt
// the row stands on its own.
//
// Note what this does NOT do: fold on conversation membership alone. `rolledInto` means a later
// draft was actually WRITTEN to answer this message. Mail triaged before conversations existed has
// no such draft, and hiding it behind one that never addressed it would ghost a real customer.
function folded(email, emails) {
  return !!email.rolledInto && (emails || []).some(e => e.id === email.rolledInto);
}

// How many messages of this conversation are still waiting on a reply — counting the ones Bean
// never grouped. This is a fact about what the CUSTOMER did, so it is true for mail triaged before
// any of this existed, which is why the badge is computed here and not read off `rolledInto`.
// Reported only against the newest open message, so a pile-up is flagged once, not once per row.
function unansweredCount(email, emails, status) {
  const open = conversationOf(email, emails).filter(
    e => !e.filed && (!status[e.id] || status[e.id] === 'pending')
  );
  if (!open.length || open[open.length - 1].id !== email.id) return 0;
  // She answers plenty of mail in Proton without ever marking it here, so "no status" is NOT proof
  // nobody replied. The quoted history is: if the last message inside this email is one of OURS,
  // they were answered and then wrote back — a back-and-forth, not a chase. Mirrors
  // conversation.store_spoke_last, and like it, only a POSITIVE identification counts ('unknown'
  // attribution must not be read as a reply, or a real chase gets silently downgraded).
  const conv = email.conversation || [];
  if (conv.length && conv[conv.length - 1].side === 'other') return 0;
  return open.length;
}

if (typeof window !== 'undefined') {
  Object.assign(window, { conversationKey, conversationOf, conversationIds, folded, unansweredCount });
}

// Newest first. Mail we cannot date sorts to the BOTTOM rather than the top: an unreadable Date
// header is not evidence that something just arrived, and floating it above real new mail would put
// the least trustworthy row in the most important position. Never dropped — every email renders.
function byNewestFirst(a, b) {
  const ta = window.beanTimeMs(a.time);
  const tb = window.beanTimeMs(b.time);
  if (ta === null && tb === null) return 0;
  if (ta === null) return 1;
  if (tb === null) return -1;
  return tb - ta;
}

// The main lane: everything awaiting her, in arrival order, confidence carried by colour per row.
// One customer's unanswered conversation, as ONE thing to act on.
//
// The row she taps is the newest message — that is where the draft lives, and where a reply belongs.
// The earlier ones fold underneath it rather than sitting in the stream as separate errands, because
// she is going to send exactly one reply. They stay one tap away: ⚠️ folding is only honest while the
// card that swallowed them SHOWS what it swallowed, so the expander is not a nicety, it is the thing
// that makes hiding them allowed at all.
//
// `redraftable` is the mail Bean never grouped — triaged before conversations existed, so its draft
// only ever read the last message. One tap fixes that conversation and no others; a sweep would
// spend real money on mail she may never reopen.
function ConversationCard({ members, status, onOpen, onClear, onRedraft, busy }) {
  const [open, setOpen] = useState(false);
  const newest = members[members.length - 1];
  const earlier = members.slice(0, -1);
  const redraftable = earlier.some(e => e.rolledInto !== newest.id);
  const h = React.createElement;

  return h('div', { className: 'convo-card' },
    h(InboxRow, { email: newest, status: status[newest.id], onOpen, onClear, covers: members.length }),
    h('div', { className: 'convo-card-foot' },
      h('button', {
        type: 'button', className: 'convo-more', onClick: () => setOpen(o => !o),
        'aria-expanded': open,
      }, (open ? '▾ ' : '▸ ') + earlier.length + ' earlier from ' + newest.from.name.split(' ')[0]),
      redraftable && h('button', {
        type: 'button', className: 'convo-redraft', disabled: !!busy,
        onClick: () => onRedraft && onRedraft(newest),
        title: 'Bean drafted these one at a time. Write one reply that answers all of them.',
      }, busy
        ? h('span', { style: { display: 'inline-flex', alignItems: 'center', gap: 7 } },
            window.BeanRoast ? h(window.BeanRoast, { size: 16, mode: 'roast' }) : null, 'thinking…')
        : '↻ one reply for all ' + members.length)
    ),
    open && h('div', { className: 'convo-card-rows' },
      // No Clear on the folded rows: clearing the card's newest message covers the whole
      // conversation (bean-root setOne fans out over conversationIds), so a second control here
      // would offer to clear a part of something that only ever moves as a whole.
      earlier.slice().reverse().map(e => h(InboxRow, {
        key: e.id, email: e, status: status[e.id], onOpen,
      }))
    )
  );
}

function ChronoGroup({ emails, status, onOpen, onClear, onRedraft, redraftBusy }) {
  if (!emails.length) return null;
  // Build the stream out of ENTRIES: a lone email, or a whole conversation collapsed into one card.
  // Grouping is by the same key the server uses (window.conversationKey), so what the queue treats
  // as one conversation is exactly what one draft was written to answer.
  const byKey = new Map();
  const entries = [];
  [...emails].sort(byNewestFirst).reverse().forEach(e => {           // oldest→newest within a card
    const key = conversationKey(e);
    const open = !status[e.id] || status[e.id] === 'pending';
    // Only mail still awaiting her groups. A conversation she has already actioned has nothing left
    // to collapse, and folding a handled row under an open one would hide work she finished.
    if (!open) { entries.push({ single: e }); return; }
    if (byKey.has(key)) { byKey.get(key).members.push(e); return; }
    const entry = { key, members: [e] };
    byKey.set(key, entry);
    entries.push(entry);
  });

  const rows = entries
    .map(en => (en.single || en.members.length === 1
      ? { at: en.single || en.members[0], single: en.single || en.members[0] }
      : { at: en.members[en.members.length - 1], members: en.members }))
    .sort((a, b) => byNewestFirst(a.at, b.at));

  return React.createElement('section', { className: 'inbox-group' },
    React.createElement('div', { className: 'group-head' },
      React.createElement('span', { className: 'group-dot', style: { background: 'var(--bean)' } }),
      React.createElement('h2', null, 'Your inbox'),
      React.createElement('span', { className: 'group-blurb' }, 'newest first — colour says how sure me is'),
      React.createElement('span', { className: 'group-count' }, rows.length)
    ),
    React.createElement('div', { className: 'group-rows' },
      rows.map(r => (r.members
        ? React.createElement(ConversationCard, {
            key: r.at.id, members: r.members, status, onOpen, onClear, onRedraft,
            busy: redraftBusy === r.at.id,
          })
        : React.createElement(InboxRow, {
            key: r.single.id, email: r.single, status: status[r.single.id], onOpen, onClear,
            covers: unansweredCount(r.single, emails, status),
          })))
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
      [...emails].sort(byNewestFirst).map(e =>
        React.createElement(InboxRow, { key: e.id, email: e, status: status[e.id], onOpen }))
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

// "What me can do now", on the morning screen instead of three taps into Settings.
//
// ONE entry — the newest she has not seen. A changelog that dumps five entries on the inbox is a
// wall she scrolls past; one thing she can actually read is a thing she might actually try. The rest
// stay in Settings, and "See everything me changed" is the door to them (it opens the same tab the
// ⚙ dot pointed at, which is what marks the whole list seen).
//
// Dismiss marks it seen for good — same localStorage key as the Settings tab, so acknowledging it
// here does not leave the dot burning there.
function WhatsNewCard({ entry, onDismiss, onMore }) {
  const h = React.createElement;
  return h('div', { className: 'whatsnew-card', role: 'note' },
    window.PlayfulMark ? h(window.PlayfulMark, { size: 34, className: 'whatsnew-bean' }) : null,
    h('div', { className: 'whatsnew-body' },
      h('div', { className: 'whatsnew-kicker' }, 'What me can do now'),
      h('h3', null, entry.title),
      h('p', null, entry.body),
      h('div', { className: 'whatsnew-foot' },
        entry.where ? h('span', { className: 'whatsnew-where' }, '→ ' + entry.where) : null,
        onMore ? h('button', { type: 'button', className: 'whatsnew-more', onClick: onMore },
          'See everything me changed') : null
      )
    ),
    h('button', {
      type: 'button', className: 'whatsnew-x', onClick: onDismiss, 'aria-label': 'Got it',
    }, '\u00d7')
  );
}

function Inbox({ status, pasted, filter, onFilterChange, onOpen, onClear, onClearHandled, onApproveAllHigh, onClearFiled, onRedraft, redraftBusy, whatsNew, onWhatsNewDismiss, onWhatsNewMore, danceKey }) {
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
  const vDone = vEmails.filter(e => status[e.id] && status[e.id] !== 'pending');
  const totalCount = emails.length + pastedFiled.length;
  const shownCount = vEmails.length + vPastedFiled.length;
  const filterActive = ((filter && filter.categories) || []).length
    + ((filter && filter.confidences) || []).length
    + ((filter && filter.statuses) || []).length > 0;

  return React.createElement('div', { className: 'inbox' },
    // What changed in BEAN. It lived only behind ⚙ Settings with a dot on it, which asks her to
    // notice a dot, open a settings page, and find a tab — three steps to be told about a feature
    // she has not been told about yet. So it comes to her, on the one screen she opens every
    // morning. Still not a modal: it sits ABOVE the greeting, it never blocks the queue, and one
    // tap dismisses it for good (localStorage, same key the Settings tab marks).
    whatsNew && React.createElement(WhatsNewCard, {
      entry: whatsNew, onDismiss: onWhatsNewDismiss, onMore: onWhatsNewMore,
    }),
    // Greeting / triage summary
    React.createElement('div', { className: 'greet-card' },
      // One brew when she lands, and only when bean-root says this is the session's first landing
      // (see its `dancedRef`). Not on every return from a draft: the inbox remounts each time, and a
      // mark that brews twenty times a morning is wallpaper, not a surprise.
      React.createElement(window.PlayfulMark, {
        size: 58, className: 'greet-bean', autoPlay: danceKey,
        title: 'Press me — me do a little roast',
      }),
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
                // Describes the ORDER the rows are actually in. This said "me sorted them, surest
                // first" when the inbox was three confidence lanes; it is now newest-first, and a
                // greeting that describes an ordering the page doesn't use is a small lie in the
                // one place the operator is told what Bean did.
                React.createElement('b', null, pending.length + ' emails'), ' came in, newest first: ',
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
    // ONE STREAM, newest first — the way mail actually arrives.
    //
    // This used to be three lanes by confidence ("surest first"). That ordering optimised for
    // blitzing the greens, and it cost the operator the thing an inbox is FOR: knowing what just
    // came in. A customer who wrote an hour ago sat below one who wrote yesterday because Bean felt
    // surer about the older one, and there was no way to see the morning in order.
    //
    // Confidence has not gone anywhere — it moved from POSITION to MARKING. Every row still carries
    // the coloured left border, the tinted avatar and the confidence badge, so the mix is readable
    // at a glance without dictating the reading order. The greeting above still counts the lanes,
    // and "Approve all N ready" still does the blitz in one tap.
    React.createElement(ChronoGroup, { emails: vPending, status, onOpen, onClear, onRedraft, redraftBusy }),
    React.createElement(FiledGroup, { emails: vFiled, status, onOpen, onClearFiled }),
    // Already-handled, collapsed at bottom
    (() => {
      const done = vDone;
      if (!done.length) return null;
      // Everything she has finished with, and the one tap that makes the screen actually empty.
      // The sweep counts only what the SERVER will delete (approved + handled), never the snoozed
      // rows that also live in this lane — they mean "come back to this", and a count that included
      // them would promise a deletion the server correctly refuses to make.
      const sweepable = done.filter(e => ['approved', 'handled'].indexOf(status[e.id]) !== -1);
      return React.createElement('section', { className: 'inbox-group done-group' },
        React.createElement('div', { className: 'group-head' },
          React.createElement('span', { className: 'group-dot', style: { background: '#C7BCA8' } }),
          React.createElement('h2', null, 'Handled today'),
          React.createElement('span', { className: 'group-count' }, done.length),
          onClearHandled && sweepable.length > 0 && React.createElement('button', {
            type: 'button', className: 'clear-fyi', style: { marginLeft: 'auto' },
            onClick: onClearHandled,
            title: 'Take these off the screen for good. Snoozed mail stays, and what me learned from them stays.',
          }, 'Clear ' + sweepable.length + ' handled')
        ),
        React.createElement('div', { className: 'group-rows' },
          [...done].sort(byNewestFirst).map(e =>
            React.createElement(InboxRow, { key: e.id, email: e, status: status[e.id], onOpen }))
        )
      );
    })()
  );
}

window.Inbox = Inbox;
window.WhatsNewCard = WhatsNewCard;
window.TopBar = TopBar;
window.PasteView = PasteView;
// The pure filter helpers — exported so the component and the JS-eval test share one implementation.
window.applyInboxFilters = applyInboxFilters;
window.inboxFacets = inboxFacets;
window.byNewestFirst = byNewestFirst;
// Delete the FYI (gate-filed) mail server-side. The server keeps a volume backup and only ever
// removes filed lines (never a customer email), so this is a one-tap cleanup, not a risky purge.
async function clearFiled() {
  const r = await fetch('/api/clear-filed', { method: 'POST', headers: { 'Content-Type': 'application/json' } });
  if (!r.ok) throw new Error('POST /api/clear-filed ' + r.status);
  return r.json();  // { ok, removed, kept }
}

// Delete the mail she has already actioned. The server picks the ids off status.json itself and
// never trusts a list from here — see _handle_clear_handled for why that matters on real customer
// mail. Throws on any non-ok so the caller can say so rather than silently claiming a clean sweep.
async function clearHandled() {
  const r = await fetch('/api/clear-handled', { method: 'POST', headers: { 'Content-Type': 'application/json' } });
  if (!r.ok) throw new Error('POST /api/clear-handled ' + r.status);
  return r.json();  // { ok, removed, kept }
}

window.beanStore = { loadStatus, upsertStatus, loadConfig, saveConfig, configSettled, defaultConfig, loadNotebook, saveNotebook, recordCorrection, flushCorrectionOutbox, previewEmail, askBean, redraftEmail, loadInbox, loadLearning, loadGateProposals, loadCorrections, loadNotebookHistory, loadWhatsNew, whatsNewUnread, markWhatsNewSeen, loadReply, loadReviewProgress, saveReviewProgress, clearFiled, clearHandled };
