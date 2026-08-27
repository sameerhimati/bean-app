// Bean stats — what Bean did with her mail. The value view, and deliberately ONLY that.
//
// This is the page a pricing conversation reads from, which is exactly why it carries no cost line:
// no tokens, no dollars, no model names. Spend is the author's question and lives behind the
// owner-only GET /api/usage. If a number here would only ever interest the person sending the
// invoice rather than the person paying it, it does not belong on this screen.
//
// The honesty rule the whole page is built on: every figure has to be one she could falsify from
// memory, because she is the one who did the editing. So a draft she rewrote claims nothing, a draft
// she edited claims only the part she didn't have to redo, and the minute estimates are HERS —
// typed into the two boxes below, not baked in as a flattering constant. A total she can catch
// lying is worth less than a smaller one she can check.
//
// Charts are hand-rolled inline SVG on purpose (no chart library, no build step — see roadmap's
// design constraints). They are bar charts; a bar chart is a rectangle and a number.
const { useState: useStatsState, useEffect: useStatsEffect, useMemo: useStatsMemo } = React;

// Conservative defaults, at the LOW end of the range she gave (4–5 min unassisted, ~1 min with
// Bean). Defaulting to the low end matters: these numbers multiply, and the first render is the one
// that sets her expectation of whether this page flatters itself.
const STATS_DEFAULTS = { minutesUnassisted: 4, minutesWithBean: 1, secondsToFile: 20 };

// What an edit costs when the row predates `edit_ratio` (bean/corrections.py). Half the gap, stated
// in the UI rather than hidden — the alternative is claiming the full saving for work we can't size.
const EDIT_RATIO_FALLBACK = 0.5;

function statsSetting(config, key) {
  const raw = config && config.settings ? config.settings[key] : undefined;
  const n = parseFloat(raw);
  return Number.isFinite(n) && n >= 0 ? n : STATS_DEFAULTS[key];
}

function fmtHours(minutes) {
  if (minutes <= 0) return '0';
  if (minutes < 60) return Math.round(minutes) + ' min';
  const h = minutes / 60;
  return (h < 10 ? h.toFixed(1) : Math.round(h)) + ' hours';
}

// ---- the arithmetic, in one place so the page and its footnote can't disagree ----
function hoursSaved(stats, knobs) {
  const gap = Math.max(0, knobs.minutesUnassisted - knobs.minutesWithBean);
  const loop = stats.loop_lifetime || {};
  const ratio = loop.mean_edit_ratio === null || loop.mean_edit_ratio === undefined
    ? EDIT_RATIO_FALLBACK : loop.mean_edit_ratio;
  const approved = (loop.approved_untouched || 0) * gap;
  // An edited draft lands between "one minute" and "the whole job", depending how much of it she
  // rewrote. edit_ratio is that position, already measured per row — so this claims the part she
  // kept and nothing more.
  const edited = (loop.approved_edited || 0) * gap * (1 - ratio);
  const filed = (stats.totals.filed || 0) * (knobs.secondsToFile / 60);
  return {
    gap, ratio, approved, edited, filed,
    // Takeovers and teaches are absent on purpose, not forgotten: she wrote those herself.
    rewritten: loop.rewritten || 0,
    escalated: loop.escalated || 0,
    total: approved + edited + filed,
  };
}

// ---- inline SVG: daily arrivals, filed stacked under drafted ----
function DailyChart({ rows }) {
  const W = 720, H = 170, PAD_L = 30, PAD_B = 22, PAD_T = 10;
  if (!rows.length) return null;
  const max = Math.max(...rows.map(r => r.filed + r.drafted), 1);
  const plotW = W - PAD_L - 8, plotH = H - PAD_B - PAD_T;
  const slot = plotW / rows.length;
  const bw = Math.max(2, Math.min(26, slot * 0.68));
  // Label every Nth day so a long month doesn't turn the axis into a smear.
  const every = Math.ceil(rows.length / 8);

  const bars = [];
  rows.forEach((r, i) => {
    const x = PAD_L + slot * i + (slot - bw) / 2;
    const hD = (r.drafted / max) * plotH;
    const hF = (r.filed / max) * plotH;
    const yD = PAD_T + plotH - hD;
    const yF = yD - hF;
    if (hF > 0) bars.push(React.createElement('rect', {
      key: 'f' + i, x, y: yF, width: bw, height: hF, fill: '#E7DECE', rx: 2,
    }, React.createElement('title', null, r.day + ' — ' + r.filed + ' filed')));
    if (hD > 0) bars.push(React.createElement('rect', {
      key: 'd' + i, x, y: yD, width: bw, height: hD, fill: 'var(--bean)', rx: 2,
    }, React.createElement('title', null, r.day + ' — ' + r.drafted + ' drafted')));
    if (i % every === 0) bars.push(React.createElement('text', {
      key: 't' + i, x: x + bw / 2, y: H - 6, textAnchor: 'middle',
      fontSize: 10, fill: 'var(--ink-faint)',
    }, r.day.slice(5).replace('-', '/')));
  });

  return React.createElement('div', { style: { overflowX: 'auto' } },
    React.createElement('svg', {
      viewBox: '0 0 ' + W + ' ' + H, width: '100%', role: 'img',
      'aria-label': 'Emails handled per day, filed and drafted',
      style: { display: 'block', minWidth: 420 },
    },
      React.createElement('line', {
        x1: PAD_L, y1: PAD_T + plotH, x2: W - 8, y2: PAD_T + plotH,
        stroke: 'var(--line)', strokeWidth: 1,
      }),
      React.createElement('text', { x: 0, y: PAD_T + 8, fontSize: 10, fill: 'var(--ink-faint)' }, String(max)),
      bars
    )
  );
}

// ---- inline SVG: one horizontal bar, N labelled segments ----
function SplitBar({ segments }) {
  const total = segments.reduce((s, x) => s + x.n, 0);
  if (!total) return null;
  let x = 0;
  return React.createElement('div', null,
    React.createElement('svg', {
      viewBox: '0 0 100 8', width: '100%', height: 16, preserveAspectRatio: 'none',
      role: 'img', 'aria-label': segments.map(s => s.label + ': ' + s.n).join(', '),
      style: { display: 'block', borderRadius: 4, overflow: 'hidden' },
    },
      segments.map((s, i) => {
        const w = (s.n / total) * 100;
        const el = React.createElement('rect', { key: i, x, y: 0, width: w, height: 8, fill: s.color });
        x += w;
        return el;
      })
    ),
    React.createElement('div', {
      style: { display: 'flex', flexWrap: 'wrap', gap: '10px 18px', marginTop: 9 },
    }, segments.filter(s => s.n > 0).map((s, i) =>
      React.createElement('span', {
        key: i, style: { display: 'inline-flex', alignItems: 'center', gap: 6, fontSize: 12.5, color: 'var(--ink-soft)' },
      },
        React.createElement('span', { style: { width: 9, height: 9, borderRadius: 2, background: s.color } }),
        React.createElement('strong', { style: { color: 'var(--ink)' } }, s.n),
        s.label
      )
    ))
  );
}

function StatRow({ label, value, hint, strong }) {
  return React.createElement('div', {
    style: {
      display: 'flex', alignItems: 'baseline', justifyContent: 'space-between', gap: 14,
      padding: '9px 0', borderTop: '1px solid var(--line-soft)',
    },
  },
    React.createElement('div', null,
      React.createElement('div', { style: { fontSize: 13.5, fontWeight: strong ? 800 : 600, color: 'var(--ink)' } }, label),
      hint && React.createElement('div', { style: { fontSize: 11.5, color: 'var(--ink-faint)', marginTop: 2 } }, hint)
    ),
    React.createElement('div', {
      style: { fontSize: 14, fontWeight: 800, color: strong ? 'var(--ink)' : 'var(--ink-soft)', whiteSpace: 'nowrap' },
    }, value)
  );
}

function MinuteBox({ label, value, onChange, suffix }) {
  return React.createElement('label', {
    style: { display: 'inline-flex', alignItems: 'center', gap: 8, fontSize: 12.5, color: 'var(--ink-soft)' },
  },
    label,
    React.createElement('input', {
      type: 'number', min: 0, step: 1, value,
      onChange: e => onChange(e.target.value),
      style: {
        width: 58, fontFamily: 'inherit', fontSize: 13, fontWeight: 700, color: 'var(--ink)',
        padding: '5px 7px', borderRadius: 8, border: '1.5px solid var(--line)', background: 'var(--paper)',
      },
    }),
    suffix
  );
}

function StatsView({ config, setConfig, onBack }) {
  const [stats, setStats] = useStatsState(null);
  const [failed, setFailed] = useStatsState(false);

  useStatsEffect(() => {
    let live = true;
    fetch('/api/stats', { credentials: 'same-origin' })
      .then(r => (r.ok ? r.json() : Promise.reject(new Error(String(r.status)))))
      .then(d => { if (live) setStats(d); })
      // No backend (the static demo) or an unreadable log: say so plainly rather than rendering
      // zeroes, which would read as "Bean did nothing for you" — the one lie this page must not tell.
      .catch(() => { if (live) setFailed(true); });
    return () => { live = false; };
  }, []);

  const knobs = {
    minutesUnassisted: statsSetting(config, 'minutesUnassisted'),
    minutesWithBean: statsSetting(config, 'minutesWithBean'),
    secondsToFile: statsSetting(config, 'secondsToFile'),
  };

  // Persisted through bean-root's existing debounced PUT /api/config, exactly like every other
  // setting. Under BEAN_DEMO_READONLY that PUT 404s and is swallowed by design — a demo visitor's
  // edits live in their own tab and leave nothing behind.
  function setKnob(key, raw) {
    const n = parseFloat(raw);
    setConfig(prev => Object.assign({}, prev, {
      settings: Object.assign({}, prev.settings, { [key]: Number.isFinite(n) && n >= 0 ? n : '' }),
    }));
  }

  const saved = useStatsMemo(() => (stats ? hoursSaved(stats, knobs) : null),
    [stats, knobs.minutesUnassisted, knobs.minutesWithBean, knobs.secondsToFile]);

  const head = React.createElement('div', { style: { marginBottom: 20 } },
    React.createElement('button', { className: 'back-btn', onClick: onBack }, '← inbox'),
    React.createElement('h2', { style: { fontSize: 22, fontWeight: 800, color: 'var(--ink)', margin: '14px 0 4px' } },
      'What Bean did'),
    React.createElement('div', { style: { fontSize: 13, color: 'var(--ink-faint)' } },
      'Everything Bean has handled for you since you switched it on.')
  );

  if (failed) {
    return React.createElement('div', { className: 'admin-view', style: { maxWidth: 760, margin: '0 auto' } }, head,
      React.createElement('div', { className: 'admin-note' },
        'Me can’t reach the numbers right now. Nothing is lost — try again in a moment.'));
  }
  if (!stats) {
    return React.createElement('div', { className: 'admin-view', style: { maxWidth: 760, margin: '0 auto' } }, head,
      React.createElement('div', { style: { color: 'var(--ink-faint)', fontSize: 13.5, display: 'flex', alignItems: 'center', gap: 10 } },
        React.createElement(window.BeanRoast, { size: 24, mode: 'roast' }),
        'Counting…'));
  }

  const t = stats.totals;
  const c = stats.confidence;
  const loop = stats.loop_lifetime || {};
  const recent = stats.loop || {};

  // On the public demo the two panels below can only ever read zero, and the difference between
  // "zero yet" and "zero always" is the whole reason to special-case it.
  //
  // Approving a draft POSTs /api/correction. BEAN_DEMO_READONLY 404s that route on purpose
  // (bean/server.py:_DEMO_LOCKED_ROUTES) and the client swallows the 404 by design, so a visitor can
  // approve every draft in the inbox and `loop.graded` stays 0 — the tally lives on the volume the
  // demo does not have. What renders then is "Time you got back: 0 min saved, all in", at the foot
  // of the one page whose entire job is saying what Bean was worth. That is the same lie the
  // `failed` branch above already refuses to tell, arriving by a different road: zeroes read as
  // "Bean did nothing for you", and here they would mean "nobody is allowed to write anything down".
  //
  // A REAL tenant sitting at 0 graded drafts genuinely has not graded any yet, and "No drafts graded
  // yet — approve, edit or rewrite one and it shows up here" is exactly right for them. So the test
  // is the demo flag AND an empty tally, never the tally alone.
  const demoUngraded = !!window.BEAN_DEMO_TENANT && !loop.graded;

  return React.createElement('div', { className: 'admin-view', style: { maxWidth: 760, margin: '0 auto' } },
    head,

    // ---- the headline: mail handled ----
    React.createElement('div', { className: 'admin-card' },
      React.createElement('div', { style: { padding: '17px 17px 15px' } },
        React.createElement('div', { style: { fontSize: 34, fontWeight: 800, color: 'var(--ink)', lineHeight: 1.1 } },
          t.handled.toLocaleString()),
        React.createElement('div', { className: 'admin-hint', style: { marginBottom: 14 } },
          'emails handled' + (stats.undated ? ' · ' + stats.undated + ' with no readable date, counted here but not on the chart' : '')),
        React.createElement(SplitBar, {
          segments: [
            { label: 'filed — you never had to open them', n: t.filed, color: '#C9BCA4' },
            { label: 'drafted a reply', n: t.drafted, color: 'var(--bean)' },
          ],
        })
      )
    ),

    // ---- daily arrivals ----
    stats.daily.length > 0 && React.createElement('div', { className: 'admin-card' },
      React.createElement('div', { style: { padding: '15px 17px 10px' } },
        React.createElement('div', { className: 'admin-label' }, 'Every day'),
        React.createElement('div', { className: 'admin-hint' }, 'Drafted in brown, filed in sand.'),
        React.createElement(DailyChart, { rows: stats.daily })
      )
    ),

    // ---- how sure Bean was ----
    t.drafted > 0 && React.createElement('div', { className: 'admin-card' },
      React.createElement('div', { style: { padding: '15px 17px 17px' } },
        React.createElement('div', { className: 'admin-label' }, 'How sure me was'),
        React.createElement('div', { className: 'admin-hint' },
          'Bean only calls a draft ready to send when it can point at where the answer came from.'),
        React.createElement(SplitBar, {
          segments: [
            { label: 'ready to send', n: c.green, color: CONF.high.dot },
            { label: 'worth a look', n: c.yellow, color: CONF.low.dot },
            { label: 'needs you', n: c.red, color: CONF.flag.dot },
          ].concat(c.unknown ? [{ label: 'unrecognised (a bug — tell Sameer)', n: c.unknown, color: '#9A8F7E' }] : []),
        })
      )
    ),

    // ---- the demo's stand-in for the two panels below ----
    demoUngraded && React.createElement('div', { className: 'admin-card' },
      React.createElement('div', { style: { padding: '15px 17px 17px' } },
        React.createElement('div', { className: 'admin-label' }, 'What you did with them'),
        React.createElement('div', { className: 'admin-hint' },
          'This is where the demo stops. The rest of this page counts what the operator did with '
          + 'each draft — sent as-is, edited, rewritten — and totals the minutes that bought back. '
          + 'The demo saves nothing, so it has nothing to count.'),
        React.createElement('div', { className: 'admin-note' },
          'Me is not being coy: the number me cares about most is the share of drafts sent with no '
          + 'edit at all. It is how me knows whether me is actually helping, and it only means '
          + 'something once a real person has been approving real mail for a few weeks.')
      )
    ),

    // ---- what she did with the drafts ----
    !demoUngraded && React.createElement('div', { className: 'admin-card' },
      React.createElement('div', { style: { padding: '15px 17px 17px' } },
        React.createElement('div', { className: 'admin-label' }, 'What you did with them'),
        loop.graded
          ? React.createElement(React.Fragment, null,
              React.createElement('div', { className: 'admin-hint' },
                'Out of ' + loop.graded + ' drafts you acted on.'),
              React.createElement(StatRow, { label: 'Sent as-is', value: loop.approved_untouched, hint: 'one tap, no changes' }),
              React.createElement(StatRow, { label: 'Sent after edits', value: loop.approved_edited,
                // The "wasn't recorded" note only makes sense when there ARE edits to have recorded.
                hint: !loop.approved_edited ? null
                  : (loop.mean_edit_ratio === null || loop.mean_edit_ratio === undefined
                      ? 'how much you changed them wasn’t recorded'
                      : 'you changed about ' + Math.round(loop.mean_edit_ratio * 100) + '% of the words') }),
              React.createElement(StatRow, { label: 'You wrote your own', value: loop.rewritten, hint: 'Bean claims no time for these' }),
              React.createElement(StatRow, { label: 'You taught Bean the answer', value: loop.escalated, hint: 'Bean had nothing, and you filled the gap' }),
              recent.graded > 0 && recent.graded !== loop.graded && React.createElement('div', { className: 'admin-note' },
                'Lately: ' + Math.round(recent.rate * 100) + '% sent as-is across the ' + recent.graded +
                ' drafts still in your inbox. The numbers above go all the way back, including a version of Bean that no longer exists.')
            )
          : React.createElement('div', { className: 'admin-hint' },
              'No drafts graded yet — approve, edit or rewrite one and it shows up here.')
      )
    ),

    // ---- time saved ----
    !demoUngraded && React.createElement('div', { className: 'admin-card' },
      React.createElement('div', { style: { padding: '15px 17px 17px' } },
        React.createElement('div', { className: 'admin-label' }, 'Time you got back'),
        React.createElement('div', { className: 'admin-hint' },
          'These are your minutes, not Bean’s guess. Change them and every number below moves.'),
        React.createElement('div', { style: { display: 'flex', flexWrap: 'wrap', gap: '10px 20px', margin: '10px 0 16px' } },
          React.createElement(MinuteBox, {
            label: 'A reply on your own takes', value: knobs.minutesUnassisted,
            onChange: v => setKnob('minutesUnassisted', v), suffix: 'min',
          }),
          React.createElement(MinuteBox, {
            label: 'With a draft in front of you', value: knobs.minutesWithBean,
            onChange: v => setKnob('minutesWithBean', v), suffix: 'min',
          }),
          React.createElement(MinuteBox, {
            label: 'Reading and filing one email', value: knobs.secondsToFile,
            onChange: v => setKnob('secondsToFile', v), suffix: 'sec',
          })
        ),
        React.createElement('div', { style: { fontSize: 30, fontWeight: 800, color: 'var(--ink)', lineHeight: 1.15 } },
          fmtHours(saved.total)),
        React.createElement('div', { className: 'admin-hint', style: { marginBottom: 6 } }, 'saved, all in'),
        React.createElement(StatRow, {
          label: 'Mail you never opened', value: fmtHours(saved.filed),
          hint: t.filed + ' filed × ' + knobs.secondsToFile + ' sec',
        }),
        React.createElement(StatRow, {
          label: 'Drafts you sent as-is', value: fmtHours(saved.approved),
          hint: (loop.approved_untouched || 0) + ' × ' + saved.gap + ' min saved each',
        }),
        React.createElement(StatRow, {
          label: 'Drafts you edited', value: fmtHours(saved.edited),
          hint: !loop.approved_edited ? 'none yet'
            : (loop.approved_edited + ' × ' + saved.gap + ' min × the '
               + Math.round((1 - saved.ratio) * 100) + '% you kept'
               + (loop.mean_edit_ratio === null || loop.mean_edit_ratio === undefined ? ' (estimated)' : '')),
        }),
        React.createElement(StatRow, {
          label: 'Replies you wrote yourself', value: '—',
          hint: (saved.rewritten + saved.escalated) + ' of them. Bean did that work for you, so it claims nothing.',
        }),
        React.createElement('div', { className: 'admin-note' },
          'Me only counts what me actually saved you. A draft you rewrote counts as nothing, and a '
          + 'draft you edited counts only for the part you kept.')
      )
    )
  );
}

window.StatsView = StatsView;
window.beanHoursSaved = hoursSaved;  // exported for the arithmetic check in tests/manual replay
