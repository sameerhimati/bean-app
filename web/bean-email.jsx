// Shared email renderer. Plain text is canonical (it's what the operator writes, what Bean stores as
// a template, and what they copy to send); THIS is a derived, read-only "customer view" of that text:
// it autolinks bare URLs and turns Shopify order refs (#SW-1234) into admin deep-links, so a sample
// email — or a drafted reply — reads the way the customer will actually see it, links live instead
// of inert.
//
// Safe by construction: every non-link fragment is handed to React as a string child, which React
// escapes — there is NO dangerouslySetInnerHTML and no HTML-injection surface. That is why v1 needs
// no sanitizer: we only ever BUILD markup from escaped text. (Rendering untrusted forwarded HTML
// mail is a later, sandboxed-iframe + DOMPurify job — gated on real HTML mail existing.)

// One combined matcher (capturing, global) so String.split keeps the delimiters as tokens.
const _EMAIL_TOKEN_RE = /(https?:\/\/[^\s<>()]+|#?SW-\d+)/g;
const _EMAIL_LINK_STYLE = { color: '#4257d6', textDecoration: 'underline' };

// Pure: text -> [{type:'text'|'url'|'order', value, href?}]. No React, no window — so it can be
// exercised under plain node without a browser or a test framework (the repo has neither for JS).
function tokenizeEmailText(text, store) {
  const out = [];
  String(text == null ? '' : text).split(_EMAIL_TOKEN_RE).forEach(part => {
    if (!part) return;
    if (/^https?:\/\//.test(part)) {
      // Trailing sentence punctuation clings to a bare URL ("…see https://x/y.") — peel it off so
      // the link target is clean and the period stays as text.
      const m = part.match(/[.,;:!?)\]]+$/);
      const tail = m ? m[0] : '';
      const url = tail ? part.slice(0, -tail.length) : part;
      out.push({ type: 'url', value: url, href: url });
      if (tail) out.push({ type: 'text', value: tail });
    } else if (/^#?SW-\d+$/.test(part)) {
      const q = part.replace(/^#/, '');
      out.push({
        type: 'order', value: part,
        href: store ? ('https://admin.shopify.com/store/' + store + '/orders?query=' + encodeURIComponent(q)) : null,
      });
    } else {
      out.push({ type: 'text', value: part });
    }
  });
  return out;
}

// text -> array of React nodes (plain strings + <a>). Newlines are left intact for the caller to
// render (a pre-wrap container preserves them); used for drafts, which are a single pre-wrap string.
function linkifyText(text, keyPrefix) {
  const kp = keyPrefix || 'lk';
  const store = (window.SETTINGS && window.SETTINGS.shopifyStore) || '';
  return tokenizeEmailText(text, store).map((tok, i) => {
    if (tok.type === 'text') return tok.value;
    if (tok.type === 'order' && !tok.href) return tok.value;  // no store configured → plain text
    return React.createElement('a', {
      key: kp + '_' + i, href: tok.href, target: '_blank', rel: 'noreferrer noopener', style: _EMAIL_LINK_STYLE,
    }, tok.value);
  });
}

// The customer-view body renderer: paragraphs with links live. `text` is either an array of
// paragraph strings (email.body, already split on blank lines) or a single string (split here).
function EmailBody({ text, className }) {
  const paras = Array.isArray(text) ? text : String(text == null ? '' : text).split(/\n\s*\n/);
  return React.createElement('div', { className: className || 'email-body' },
    paras.map((para, pi) => {
      const s = String(para == null ? '' : para);
      if (!Array.isArray(text) && s.trim() === '') return null;  // drop blank splits from a raw string
      const lines = s.split('\n');
      const kids = [];
      lines.forEach((line, li) => {
        if (li) kids.push(React.createElement('br', { key: 'br' + pi + '_' + li }));
        linkifyText(line, 'p' + pi + '_' + li).forEach(n => kids.push(n));
      });
      return React.createElement('p', { key: pi }, kids);
    })
  );
}

// How many real messages this email is: the quoted history plus the one in hand.
//
// `email.conversation` comes from bean/quoting.py, which unpacks the stored `thread` blob into the
// messages actually inside it. The `thread.length + 1` fallback is only for the baked fixtures in
// bean-data.jsx, which carry a hand-written array and no `conversation` — on real mail `thread` is
// always exactly one element, so that expression could only ever say "2".
function msgCount(email) {
  if (!email) return 1;
  if (email.conversation && email.conversation.length) return email.conversation.length + 1;
  return ((email.thread && email.thread.length) || 0) + 1;
}

if (typeof window !== 'undefined') { Object.assign(window, { EmailBody, linkifyText, tokenizeEmailText, msgCount }); }
// Also reachable under node (no window) for a quick pure-function sanity check — harmless in browser.
if (typeof module !== 'undefined' && module.exports) { module.exports = { tokenizeEmailText }; }
