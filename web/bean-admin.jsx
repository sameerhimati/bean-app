// Bean settings — the gate, what me learned, knowledge & policy, store settings.
// (Opened by the ⚙ Settings button. Teaching Bean is the notebook walk, not this screen: what Bean
// KNOWS lives in the notebook, and this is only the policy around it.)
// Edits live in React state and persist through bean-root's debounced PUT /api/config.
// Uses a unique hook alias to avoid the shared-global-scope `const` collision the other component
// files dodge the same way.
const { useState: useAdminState, useEffect: useAdminEffect } = React;

function AdminField({ label, hint, children }) {
  return React.createElement('div', { className: 'admin-field' },
    React.createElement('div', { className: 'admin-label' }, label),
    hint && React.createElement('div', { className: 'admin-hint' }, hint),
    children
  );
}

function Toggle({ on, onToggle, label }) {
  return React.createElement('button', {
    className: 'toggle' + (on ? ' is-on' : ''), onClick: onToggle, type: 'button',
  },
    React.createElement('span', { className: 'toggle-knob' }),
    React.createElement('span', { className: 'toggle-label' }, label)
  );
}

// ---- Rich paste → plain text, links intact ----
// A reply pasted from Docs/Gmail/Proton arrives as html. Bean's clipboard is plain text, so taking
// `textContent` would silently drop every product URL out of a reply about products.
//
// Fold each link back into the text as `label (url)`. A bare URL is auto-linked by every mail client
// on send, and survives the round trip out of Bean: CopyButton's html flavor is a single wrapper div
// of <br>-separated text, which walks back through this function to the text it started as.
// Editors disagree about what a "line" is: Docs wraps each paragraph in <p>, Gmail/Proton wrap each
// LINE in <div>. So <p>/<h*> earn a blank line (they're paragraphs) and <div>/<li> earn one newline
// (they're lines) — otherwise a Gmail paste comes out double-spaced. Runs of 3+ collapse to 2 below.
const _PARA_TAG = /^(P|H1|H2|H3|H4|H5|H6|BLOCKQUOTE)$/;
const _LINE_TAG = /^(DIV|LI|TR|SECTION|ARTICLE|UL|OL|TABLE)$/;

function htmlToTextWithLinks(html) {
  const doc = new DOMParser().parseFromString(html, 'text/html');
  let out = '';
  const walk = (node) => {
    if (node.nodeType === 3) { out += node.nodeValue; return; }       // text
    if (node.nodeType !== 1) return;                                   // comments etc.
    const tag = node.tagName.toUpperCase();
    if (tag === 'SCRIPT' || tag === 'STYLE') return;
    if (tag === 'BR') { out += '\n'; return; }
    if (tag === 'A') {
      const href = (node.getAttribute('href') || '').trim();
      const label = node.textContent.replace(/\s+/g, ' ').trim();
      // Only http(s) targets are worth pasting into a customer's inbox. mailto:/relative/javascript:
      // keep their words and lose the target — a broken link in a reply is worse than plain text.
      if (!/^https?:\/\//i.test(href)) { out += label; return; }
      if (!label) { out += href; return; }
      out += label.includes(href) ? label : label + ' (' + href + ')';
      return;
    }
    node.childNodes.forEach(walk);
    if (_PARA_TAG.test(tag)) out += '\n\n';
    else if (_LINE_TAG.test(tag)) out += '\n';
  };
  doc.body.childNodes.forEach(walk);
  return out
    .replace(/ /g, ' ')      // rich-paste non-breaking spaces → real spaces
    .replace(/[ \t]+\n/g, '\n')
    .replace(/\n{3,}/g, '\n\n')
    .trim();
}

// onPaste for a reply box: splice the link-preserving text in at the cursor. A paste with no html
// flavor has no href to lose, so let the browser handle it natively (keeps undo intact).
function pasteWithLinks(e, setValue) {
  const html = e.clipboardData && e.clipboardData.getData('text/html');
  if (!html) return;
  const text = htmlToTextWithLinks(html);
  if (!text) return;
  e.preventDefault();
  const el = e.target;
  setValue(el.value.slice(0, el.selectionStart) + text + el.value.slice(el.selectionEnd));
}

// ---- The gate: what reaches Bean at all ----
// Two override lists the operator controls directly, applied before any judgment. Engine-independent —
// bean/gate.py runs this on every inbound email whichever brain is drafting.
function GateRules({ config, setConfig }) {
  const gate = config.gate || {};
  const [proposals, setProposals] = useAdminState([]);
  // The gate's own read surface. Best-effort: a failure here must never block the two textareas,
  // which are the real control — proposals are a convenience over them, not a replacement.
  useAdminEffect(() => {
    let live = true;
    Promise.resolve(window.beanStore.loadGateProposals())
      .then(rows => { if (live) setProposals(rows || []); })
      // Swallowed ON PURPOSE: loadGateProposals already returns [] for every failure, so reaching
      // here means React itself threw. Proposals are a convenience over the two textareas below —
      // surfacing an error banner where she came to edit her rules would cost her the control to
      // report a suggestion she never asked for. Empty list = the screen she had before this feature.
      .catch(() => {});
    return () => { live = false; };
  }, []);
  const addList = (key, pattern) => setConfig(c => {
    const g = { ...(c.gate || {}) };
    const have = (g[key] || []).filter(s => s.trim());
    if (have.some(s => s.trim().toLowerCase() === pattern)) return c;  // already there — no-op
    return { ...c, gate: { ...g, [key]: [...have, pattern] } };
  });
  const setList = (key) => (e) => {
    const lines = e.target.value.split('\n');
    setConfig(c => {
      const g = { ...(c.gate || {}), [key]: lines };
      // Keep only lists with real content so an untouched config stays byte-identical on disk
      // (trailing blank line allowed while typing).
      const clean = {};
      ['alwaysReply', 'alwaysFile'].forEach(k => {
        const vals = (g[k] || []).filter((s, i, a) => s.trim() || i === a.length - 1);
        if (vals.some(s => s.trim())) clean[k] = vals;
      });
      return { ...c, gate: clean };
    });
  };
  const asText = (key) => (gate[key] || []).join('\n');
  // A proposal is only ever an offer. Bean derived it from mis-files SHE logged, and accepting is
  // one tap that appends to the list below — nothing here applies itself. Filing a real customer is
  // the one mistake the gate may not make, and an auto-applied rule is how that happens quietly.
  const alreadyIn = (key, pattern) =>
    (gate[key] || []).some(s => String(s).trim().toLowerCase() === pattern);
  const proposalEl = (p, i) => {
    const filing = p.direction === 'alwaysFile';
    const done = alreadyIn(p.direction, p.pattern);
    return React.createElement('div', {
      key: i,
      style: { display: 'flex', alignItems: 'center', gap: 10, flexWrap: 'wrap',
        padding: '9px 11px', marginBottom: 7, borderRadius: 10,
        background: 'var(--cream-2)', border: '1.5px dashed var(--line)' },
    },
      React.createElement('div', { style: { flex: '1 1 220px', minWidth: 0 } },
        React.createElement('div', { style: { fontWeight: 800, fontSize: 13 } },
          (filing ? 'Always file ' : 'Always bring me ') +
          (p.scope === 'domain' ? 'everything from ' : '') + p.pattern),
        React.createElement('div', { className: 'admin-hint', style: { marginTop: 2 } },
          'You corrected me on ' + p.count + (p.count === 1 ? ' email' : ' emails') +
          (p.examples && p.examples.length ? ' — e.g. “' + p.examples[0] + '”' : '') +
          (p.scope === 'domain' ? '. This covers senders me hasn’t seen yet.' : '.'))
      ),
      done
        ? React.createElement('span', { className: 'admin-hint', style: { fontWeight: 800 } }, '✓ added')
        : React.createElement('button', {
            type: 'button', className: 'gap-teach-btn',
            style: { marginTop: 0, width: 'auto', padding: '8px 14px' },
            onClick: () => addList(p.direction, p.pattern),
          }, '+ Add this rule')
    );
  };

  return React.createElement('div', { className: 'admin-card', style: { marginBottom: 18 } },
    React.createElement('div', { className: 'admin-card-body', style: { display: 'block' } },
      proposals.length ? React.createElement(AdminField, {
        label: 'Me noticed a pattern',
        hint: 'From the times you told me me got the filing wrong. Me never applies these on me own — tap to add, or ignore them.',
      }, proposals.map(proposalEl)) : null,
      React.createElement(AdminField, {
        label: 'Always bring these to me',
        hint: 'One per line — a sender, domain, or subject word. Matches always get a reply draft, no matter what the filter thinks.',
      },
        React.createElement('textarea', {
          className: 'admin-textarea', rows: 3, value: asText('alwaysReply'),
          placeholder: 'e.g. wholesale\n@retailpartner.com',
          onChange: setList('alwaysReply'),
        })
      ),
      React.createElement(AdminField, {
        label: 'Always file these quietly',
        hint: 'One per line. Matches go to the FYI lane without drafting — still visible in the inbox, one tap to pull back out.',
      },
        React.createElement('textarea', {
          className: 'admin-textarea', rows: 3, value: asText('alwaysFile'),
          placeholder: 'e.g. newsletter@\nnoreply@shopify.com',
          onChange: setList('alwaysFile'),
        })
      ),
      React.createElement('div', { className: 'admin-note' },
        'Everything else me judges myself: real people get reply drafts; newsletters, receipts and spam get filed. When me’s not sure, you get the draft — filing a customer is the one mistake me never risks.')
    )
  );
}

// ---- What me handle ----
// What reaches Bean at all — the gate. Bean's coverage used to be reported here as a per-topic
// readout of the routing tree; under the notebook engine there is no tree to count, and the honest
// coverage signal is the calibration spread on the inbox itself (green/amber/red per email) plus the
// approval rate. So this is now the gate and nothing else.
function ScopeSection({ config, setConfig }) {
  return React.createElement('div', { className: 'admin-section' },
    React.createElement('div', { className: 'admin-section-head' },
      React.createElement('div', null,
        React.createElement('h2', null, 'What me handle'),
        React.createElement('p', null, 'What needs a reply from you, and what me can file away without bothering you.')
      )
    ),
    React.createElement(GateRules, { config, setConfig })
  );
}

// ---- Knowledge & policy ----
function KnowledgeSection({ config, setConfig }) {
  const patch = (i, p) => setConfig(c => ({
    ...c, knowledgeDocs: c.knowledgeDocs.map((x, j) => (j === i ? { ...x, ...p } : x)),
  }));
  const del = (i) => setConfig(c => ({ ...c, knowledgeDocs: c.knowledgeDocs.filter((_, j) => j !== i) }));
  const add = () => setConfig(c => ({ ...c, knowledgeDocs: [...c.knowledgeDocs, { title: 'new-doc', body: '' }] }));
  return React.createElement('div', { className: 'admin-section' },
    React.createElement('div', { className: 'admin-section-head' },
      React.createElement('div', null,
        React.createElement('h2', null, 'Knowledge & policy'),
        React.createElement('p', null, 'The facts Bean grounds every reply in. No source, no claim — that’s the rule.')
      ),
      React.createElement('button', { className: 'admin-add-btn', onClick: add }, '+ Add doc')
    ),
    config.knowledgeDocs.map((doc, i) => React.createElement('div', { className: 'admin-card', key: i },
      React.createElement('div', { className: 'admin-card-body open' },
        React.createElement(AdminField, { label: 'Title' },
          React.createElement('input', {
            className: 'admin-input', value: doc.title,
            onChange: e => patch(i, { title: e.target.value }),
          })
        ),
        React.createElement(AdminField, { label: 'Contents', hint: 'Policy, FAQ, sizing, product notes — whatever Bean should be able to cite.' },
          React.createElement('textarea', {
            className: 'admin-textarea', rows: 5, value: doc.body,
            onChange: e => patch(i, { body: e.target.value }),
          })
        ),
        React.createElement('div', { className: 'admin-card-foot' },
          React.createElement('span', { className: 'admin-usage' }, (doc.body || '').length + ' chars'),
          React.createElement('button', { className: 'admin-del', onClick: () => del(i) }, 'Delete')
        )
      )
    ))
  );
}

// ---- Settings ----
function SettingsSection({ config, setConfig }) {
  const store = config.settings.shopifyStore || '';
  const setStore = (v) => setConfig(c => ({ ...c, settings: { ...c.settings, shopifyStore: v } }));
  const url = store ? 'https://admin.shopify.com/store/' + store + '/orders?query=MM-1043' : '';
  return React.createElement('div', { className: 'admin-section' },
    React.createElement('div', { className: 'admin-section-head' },
      React.createElement('div', null,
        React.createElement('h2', null, 'Settings'),
        React.createElement('p', null, 'Connections that let Bean point you at the source of truth.')
      )
    ),
    React.createElement('div', { className: 'admin-card' },
      React.createElement('div', { className: 'admin-card-body open' },
        React.createElement(AdminField, {
          label: 'Shopify store handle',
          hint: 'Bean turns order mentions (like #MM-1043) into one-click links into your Shopify admin.',
        },
          React.createElement('input', {
            className: 'admin-input', value: store, placeholder: 'your-store',
            onChange: e => setStore(e.target.value),
          })
        ),
        React.createElement('div', { className: 'url-preview' },
          url
            ? React.createElement(React.Fragment, null, 'Links resolve to ',
                React.createElement('a', { href: 'https://admin.shopify.com/store/' + store, target: '_blank', rel: 'noreferrer' }, url))
            : 'Set a handle to enable the “Open in Shopify” links on each email.'
        )
      )
    )
  );
}

// ---- What me learned ----
// The raw correction log, browsable — the operator's window on the learning signal, without shelling
// into the volume. One row per correction: what the operator did, and whether it TAUGHT Bean a reply
// (a usable few-shot exemplar) or was a takeover that teaches nothing (Bean never saw what they sent).
// Read-only; loads GET /api/corrections. This is the honest answer to "is the loop being fed?".
function LearnedSection() {
  const h = React.createElement;
  const [data, setData] = useAdminState(null);
  React.useEffect(() => { window.beanStore.loadCorrections().then(setData); }, []);

  const head = h('div', { className: 'admin-section-head' },
    h('div', null,
      h('h2', null, 'What me learned'),
      h('p', null, 'Every correction you’ve made. ✓ means Bean learned a reply it can reuse; a takeover teaches nothing — Bean never saw what you sent.')));

  if (data === null) return h('div', { className: 'admin-section' }, head,
    h('div', { className: 'tree-flat-empty' }, 'Loading…'));

  const rows = data.corrections || [];
  if (!rows.length) return h('div', { className: 'admin-section' }, head,
    h('div', { className: 'tree-flat-empty' }, 'No corrections yet — approve, edit, or teach a reply and it shows up here.'));

  const summary = h('div', { className: 'admin-label', style: { margin: '4px 0 12px' } },
    data.exemplars + ' of ' + data.count + ' taught Bean a reply' +
    (data.count && !data.exemplars ? ' — nothing has fed the loop yet' : ''));

  const ACT = {
    approve: 'Approved', edit: 'Edited', teach: 'Taught', takeover: 'Took over',
    skip: 'Skipped', misfile: 'Mis-file fix', 'keep-filed': 'Kept filed',
    'should-file': 'Should’ve been filed',
  };
  const chip = (text, color, bg, border) => h('span', {
    style: { fontSize: 10.5, fontWeight: 800, textTransform: 'uppercase', letterSpacing: '.3px',
      padding: '3px 9px', borderRadius: 999, color, background: bg, border: '1px solid ' + border } }, text);

  const rowEl = (r, i) => h('div', { key: i, className: 'admin-card', style: { marginBottom: 10 } },
    h('div', { className: 'admin-card-body', style: { display: 'block' } },
      h('div', { style: { display: 'flex', alignItems: 'center', gap: 8, flexWrap: 'wrap', marginBottom: 7 } },
        chip(ACT[r.action] || r.action, 'var(--ink-soft)', 'var(--cream-2)', 'var(--line)'),
        r.is_exemplar
          ? chip('✓ taught a reply', '#2E7D3E', '#EAF6EC', '#BFE3C6')
          : chip('teaches nothing', '#9A6A12', '#FBF1DA', '#EAD6A2'),
        r.liked ? h('span', { style: { fontSize: 12.5 } }, '👍') : null,
        r.edit_kind ? h('span', { style: { fontSize: 11, color: 'var(--ink-faint)', fontWeight: 700 } }, r.edit_kind) : null),
      h('div', { style: { fontSize: 13.5, fontWeight: 700, color: 'var(--ink)' } }, r.subject || '(no subject)'),
      h('div', { style: { fontSize: 12, color: 'var(--ink-faint)', marginTop: 2 } },
        r.category + (r.relabeled ? ' · you moved it from “' + r.model_category + '”' : '')),
      r.note ? h('div', { style: { fontSize: 12.5, color: 'var(--ink-soft)', marginTop: 6, fontStyle: 'italic' } }, '💬 ' + r.note) : null,
      r.final_text ? h('div', { style: { fontSize: 12.5, color: 'var(--ink-soft)', background: 'var(--cream-2)', border: '1px solid var(--line)', borderRadius: 8, padding: '8px 10px', marginTop: 8, whiteSpace: 'pre-wrap' } }, r.final_text) : null));

  return h('div', { className: 'admin-section' }, head, summary, h('div', null, rows.map(rowEl)));
}
window.LearnedSection = LearnedSection;

function AdminView({ config, setConfig, onBack }) {
  const [tab, setTab] = useAdminState('scope');
  const NAV = [['scope', 'What me handle'], ['learned', 'What me learned'], ['kb', 'Knowledge & policy'], ['set', 'Settings']];
  return React.createElement('div', { className: 'admin-view' },
    React.createElement('button', { className: 'back-btn', onClick: onBack }, '← inbox'),
    React.createElement('div', { className: 'admin-shell' },
      React.createElement('nav', { className: 'admin-nav' },
        React.createElement('div', { className: 'admin-nav-title' }, 'Settings'),
        NAV.map(([k, label]) => React.createElement('button', {
          key: k, className: 'admin-nav-btn' + (tab === k ? ' is-active' : ''), onClick: () => setTab(k),
        }, label))
      ),
      React.createElement('div', { className: 'admin-body' },
        tab === 'scope' && React.createElement(ScopeSection, { config, setConfig }),
        tab === 'learned' && React.createElement(LearnedSection, null),
        tab === 'kb' && React.createElement(KnowledgeSection, { config, setConfig }),
        tab === 'set' && React.createElement(SettingsSection, { config, setConfig })
      )
    )
  );
}

window.AdminView = AdminView;
