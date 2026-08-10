// Shared Bean UI: confidence config, badges, meter, buttons, toast.

const CONF = {
  high: {
    key: 'high',
    label: 'Ready to send',
    short: 'High confidence',
    verb: 'Trust it',
    blurb: 'Matches a template cleanly. Approve blind if you like.',
    color: '#2E7D3E',
    bg: '#EAF6EC',
    line: '#BFE3C6',
    dot: '#4CA35A',
  },
  low: {
    key: 'low',
    label: 'Worth a look',
    short: 'Low confidence',
    verb: 'Give it a glance',
    blurb: 'Drafted, but there\'s a judgment call. Here\'s why.',
    color: '#9A6A12',
    bg: '#FBF1DA',
    line: '#EAD6A2',
    dot: '#E0A92E',
  },
  flag: {
    key: 'flag',
    label: 'Needs you',
    short: 'Flagged',
    verb: 'Over to you',
    blurb: 'I won\'t fake this one. Context gathered below.',
    color: '#B5462E',
    bg: '#FBE7E0',
    line: '#F1C3B4',
    dot: '#E06A4E',
  },
};

function ConfidenceBadge({ level, size = 'md' }) {
  const c = CONF[level];
  const pad = size === 'sm' ? '3px 9px' : '5px 12px';
  const fs = size === 'sm' ? 11 : 12.5;
  return React.createElement('span', {
    className: 'conf-badge',
    style: {
      display: 'inline-flex', alignItems: 'center', gap: 7,
      padding: pad, borderRadius: 999, fontSize: fs, fontWeight: 700,
      color: c.color, background: c.bg, border: `1.5px solid ${c.line}`,
      whiteSpace: 'nowrap', letterSpacing: '.2px',
    },
  },
    React.createElement('span', { style: { width: 8, height: 8, borderRadius: 999, background: c.dot, boxShadow: `0 0 0 2px ${c.bg}` } }),
    c.label
  );
}

function CategoryTag({ children }) {
  return React.createElement('span', { className: 'cat-tag' }, children);
}

// Stepped confidence meter (pixel-ish blocks)
function ConfidenceMeter({ level }) {
  const filled = level === 'high' ? 5 : level === 'low' ? 3 : 1;
  const c = CONF[level];
  const blocks = [];
  for (let i = 0; i < 5; i++) {
    blocks.push(React.createElement('div', {
      key: i,
      style: {
        flex: 1, height: 12,
        background: i < filled ? c.dot : '#E6DECF',
        borderRadius: 2,
        transition: 'background .3s',
      },
    }));
  }
  return React.createElement('div', { style: { display: 'flex', flexDirection: 'column', gap: 6 } },
    React.createElement('div', { style: { display: 'flex', gap: 4 } }, blocks),
    React.createElement('div', { style: { display: 'flex', justifyContent: 'space-between', fontSize: 11.5, fontWeight: 700, color: c.color } },
      React.createElement('span', null, c.short),
      React.createElement('span', { style: { color: '#9A8F7E', fontWeight: 600 } }, filled + '/5')
    )
  );
}

function Btn({ kind = 'primary', children, onClick, full = false, disabled = false, style = {} }) {
  const base = {
    display: 'inline-flex', alignItems: 'center', justifyContent: 'center', gap: 8,
    padding: '13px 20px', borderRadius: 10, fontSize: 14.5, fontWeight: 700,
    fontFamily: 'inherit', letterSpacing: '.2px', cursor: disabled ? 'not-allowed' : 'pointer',
    border: '1.5px solid transparent', width: full ? '100%' : 'auto',
    boxShadow: '0 1px 2px rgba(58,48,32,.08)',
    transition: 'transform .08s ease, box-shadow .15s ease, background .15s ease',
    opacity: disabled ? 0.5 : 1,
  };
  const kinds = {
    primary: { background: '#2E7D3E', color: '#fff' },
    amber: { background: '#D49327', color: '#2a2010' },
    danger: { background: '#C2573B', color: '#fff' },
    ghost: { background: '#FFFDF8', color: '#3a342c', border: '1.5px solid #E0D4C0' },
    soft: { background: '#EFE7D8', color: '#5a5044', boxShadow: 'none' },
  };
  return React.createElement('button', {
    className: 'bean-btn bean-btn-' + kind,
    onClick: disabled ? undefined : onClick,
    style: { ...base, ...kinds[kind], ...style },
  }, children);
}

// `expr` used to be accepted and then ignored, so a "me couldn't save that" toast looked exactly like
// "Sent!" apart from the words — a failure wearing a success's clothes, which is the bug this whole
// codebase spent a session removing. 'sleepy' is the failure face: an amber rail and a dimmed mark, so
// a dropped write reads as wrong at a glance, before the operator has parsed a single word.
const _TOAST_EXPR = {
  cheer: { mark: '#D8B48C' },
  happy: { mark: '#D8B48C' },
  sleepy: { mark: '#E8A87C' },
};

function Toast({ show, expr = 'cheer', onUndo = null, children }) {
  const face = _TOAST_EXPR[expr] || _TOAST_EXPR.cheer;
  return React.createElement('div', {
    className: 'bean-toast' + (show ? ' show' : '') + (expr === 'sleepy' ? ' is-warn' : ''),
  },
    React.createElement(window.BeanMark, { size: 22, color: face.mark, tilt: -14 }),
    React.createElement('span', null, children),
    onUndo && React.createElement('button', {
      onClick: onUndo,
      style: {
        marginLeft: 12, background: 'transparent', border: 'none', color: 'inherit',
        font: 'inherit', fontWeight: 800, textDecoration: 'underline', cursor: 'pointer', padding: 0,
      },
    }, 'Undo')
  );
}

// Who Bean is talking TO. Bean greets the operator by name, and that name used to be a string
// literal in two components — which meant every deployment of Bean said good morning to one
// specific person. It is config now: `settings.operatorName`, which rides to the browser on
// window.SETTINGS exactly like shopifyStore does (bean/server.py bakes window.CONFIG.settings into
// the page; bean/config.py keeps arbitrary string keys on round-trip, so nothing else had to change).
//
// Unset is the NORMAL case, not an error — a fresh tenant has no name on file, so callers get ''
// and are expected to fall back to a nameless greeting rather than print a placeholder. Trimmed
// because a settings box is a text input and " " is not a name.
function operatorName() {
  return ((window.SETTINGS && window.SETTINGS.operatorName) || '').trim();
}

// Human words for Bean's internal filing enum — the operator never sees the raw "gateKind" values.
const FRIENDLY_KIND = {
  newsletter: 'Newsletter', promo: 'Promotion', receipt: 'Receipt',
  notification: 'Order update', spam: 'Spam', 'cold-outreach': 'Sales pitch',
  other: 'FYI', customer: 'Customer note',
};
function friendlyKind(kind) {
  return FRIENDLY_KIND[kind] || 'Set aside';
}

// Older-browser clipboard path: a hidden textarea + execCommand('copy'). Returns success so the
// caller only flips to "Copied ✓" when the copy actually landed.
function fallbackCopy(text) {
  try {
    const ta = document.createElement('textarea');
    ta.value = text;
    ta.style.position = 'fixed'; ta.style.top = '0'; ta.style.left = '0'; ta.style.opacity = '0';
    document.body.appendChild(ta);
    ta.focus(); ta.select();
    document.execCommand('copy');
    document.body.removeChild(ta);
    return true;
  } catch (e) { return false; }
}

// The draft's clothes, put on at COPY time rather than in the app's own styling. Bean's UI font is
// Bean's; the reply has to look like the operator's mail, and a plain-text paste takes whatever the
// composer defaults to — which meant reformatting every single send by hand.
const COPY_STYLE = 'font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Helvetica, Arial, sans-serif; font-size: 14pt;';

function escapeHtml(text) {
  return text.replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;');
}

// The seatbelt for markdown that reaches a real customer.
//
// The model was never told what format to write in, so it sometimes emitted markdown — and roughly a
// third of real drafts carried it. Nothing rendered it: the preview showed the asterisks, the
// clipboard carried the asterisks, and one customer was actually sent
// "try entering **15106534576**". It was APPROVED untouched, because `**` is exactly the kind of
// thing a quick read slides over.
//
// `bean/engine.py` now forbids markdown, which is the real fix. This is the seatbelt, because a
// prompt is a request and not a guarantee. Bold becomes REAL bold rather than being stripped: the
// model reached for emphasis deliberately, and <strong> is what it was reaching for. Deliberately
// ONLY `**bold**` — the one form observed in production. Anything wider is a markdown renderer, and
// this is a safety net, not a feature.
const _MD_BOLD = /\*\*([^*\n]+)\*\*/g;

function stripMarkdown(text) {
  return String(text == null ? '' : text).replace(_MD_BOLD, '$1');
}

// Newlines become <br> inside ONE wrapper div — deliberately not <p> or a div-per-line. A copy
// pasted back INTO Bean goes through htmlToTextWithLinks (bean-admin.jsx), where <br> is one
// newline and a paragraph tag is two; <br> is what round-trips to the text we started with.
// Escape FIRST, then promote bold, so the promotion can emit real tags without opening an
// injection path through the draft.
function draftAsHtml(text) {
  const body = escapeHtml(String(text == null ? '' : text))
    .replace(_MD_BOLD, '<strong>$1</strong>')
    .replace(/\n/g, '<br>');
  return '<div style="' + COPY_STYLE + '">' + body + '</div>';
}

// THE core PoC action: Bean never sends — the operator copies the draft and sends it from their own inbox.
// TWO flavors on the clipboard, not one: a rich target (Proton's composer, Gmail) takes the html
// and pastes ready to send, a plain target takes byte-identical text. Degrades through
// navigator.clipboard to the execCommand fallback; flips to "Copied ✓" for ~1.5s (setTimeout,
// no Date.now).
function CopyButton({ text, kind = 'primary', full = false, label = 'Copy draft', style = {} }) {
  const [copied, setCopied] = React.useState(false);
  const timer = React.useRef(null);
  React.useEffect(() => () => { if (timer.current) clearTimeout(timer.current); }, []);
  function flash() {
    setCopied(true);
    if (timer.current) clearTimeout(timer.current);
    timer.current = setTimeout(() => setCopied(false), 1500);
  }
  function plainCopy(t) {
    if (navigator.clipboard && navigator.clipboard.writeText) {
      navigator.clipboard.writeText(t).then(flash, () => { if (fallbackCopy(t)) flash(); });
    } else if (fallbackCopy(t)) { flash(); }
  }
  function doCopy() {
    // The two flavors need DIFFERENT sources, which is easy to get wrong: the html one must see the
    // RAW text so it still has `**` spans to promote to <strong>, while the plain one gets them
    // removed, because a plain-text target cannot render bold and would otherwise show the customer
    // the syntax. Stripping once up front (the obvious version) silently disables the promotion and
    // leaves the html flavor unemphasised.
    const raw = text || '';
    const t = stripMarkdown(raw);
    // ClipboardItem is the only way to put two flavors down at once. Where it is missing or the
    // write is refused, fall through to the plain-text path — the exact behaviour Bean shipped
    // before, so a browser without it loses the font and nothing else.
    if (navigator.clipboard && navigator.clipboard.write && window.ClipboardItem) {
      try {
        const item = new ClipboardItem({
          'text/html': new Blob([draftAsHtml(raw)], { type: 'text/html' }),
          'text/plain': new Blob([t], { type: 'text/plain' }),
        });
        navigator.clipboard.write([item]).then(flash, () => plainCopy(t));
        return;
      } catch (e) { /* Blob/ClipboardItem construction refused — plain text still works */ }
    }
    plainCopy(t);
  }
  return React.createElement(Btn, { kind, full, onClick: doCopy, style },
    copied ? 'Copied ✓' : ('⧉ ' + label));
}

Object.assign(window, { CONF, ConfidenceBadge, CategoryTag, ConfidenceMeter, Btn, Toast, friendlyKind, CopyButton, operatorName, stripMarkdown, draftAsHtml, MD_BOLD_RE: _MD_BOLD });
