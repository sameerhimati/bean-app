// Bean roast — the thinking/loading mark. One loop is one cup of coffee: raw bean → roast →
// grind → pour-over → the finished cup, then a fresh bean drops in. Same glyph, viewBox and
// drop-in contract as BeanMark (bean-pixel.jsx); at rest it sits at --bean and does the existing
// 2.6s bob.
//
// Two modes. mode="brew" (default) is the full journey, ~6s — right at 44px and up. mode="roast"
// is the compact 2.4s loop: the bean roasts and nothing else, for 20–28px inline slots where a
// dripper is three pixels of noise.
//
// All motion is CSS keyframes (see the `Bean roast` block in Bean.html). This file only builds the
// SVG and picks classes — nothing animates in JS, and prefers-reduced-motion is honoured by the
// stylesheet, not here. Palette comes from CSS custom properties so `tone="light"` (for the dark
// toast) swaps every colour without a second set of keyframes.

const _R = (typeof React !== 'undefined' && React) || window.React;

// Identical path/geometry to BeanMark. Kept as its own constant on purpose: bean-pixel.jsx may load
// after this file, so we can't read BEAN_PATH off it at module scope.
const ROAST_PATH = 'M 34 18 C 22 20 14 34 14 50 C 14 76 32 92 56 92 C 80 92 104 78 104 52 C 104 32 96 18 84 18 C 74 18 66 30 60 34 C 54 30 46 18 34 18 Z';

// A stylised ember, only used by heat="flame".
const FLAME_OUTER = 'M 59 80 C 67 91 71 96 71 102 C 71 108.6 65.6 114 59 114 C 52.4 114 47 108.6 47 102 C 47 96 51 91 59 80 Z';
const FLAME_INNER = 'M 59 93 C 63.5 99 65.5 101.5 65.5 104.5 C 65.5 108.1 62.6 111 59 111 C 55.4 111 52.5 108.1 52.5 104.5 C 52.5 101.5 54.5 99 59 93 Z';

const CONE = 'M 18 42 L 59 92 L 100 42';                                   // dripper walls
const BED  = 'M 24 48 C 38 54 80 54 94 48 L 62 87 C 60.6 88.4 57.4 88.4 56 87 Z'; // grounds in the filter
const CUP  = 'M 26 38 L 33 90 C 34 98 41 103 49 103 L 69 103 C 77 103 84 98 85 90 L 92 38 Z';
const HANDLE = 'M 88 50 C 101 50 105 61 100 69 C 97 74 92 76 87 76';
const STEAM_A = 'M 50 30 C 45 23 55 19 50 11';
const STEAM_B = 'M 69 32 C 64 26 73 22 69 15';

// Where the grind spray comes from. Fixed, not random — a loading mark that reshuffles every mount
// reads as jitter rather than as the same object doing the same thing.
const SPECKS = [[40, 26], [59, 17], [76, 28], [47, 35], [71, 36], [59, 33]];

let _roastUid = 0;

function BeanRoast({
  size = 28,
  running = true,
  mode = 'brew',        // 'brew' (full journey) | 'roast' (compact 2.4s bean-only loop)
  heat = 'glow',        // 'glow' (A) | 'flame' (B) | 'none'
  tone = 'dark',        // 'dark' on paper | 'light' on the ink-coloured toast
  tilt = -14,
  staticMode = false,   // force the reduced-motion rendering (for demos/tests)
  style = {},
  className = '',
}) {
  const uid = _R.useMemo(() => 'br' + (++_roastUid), []);
  const gid = uid + '-heat';
  const cid = uid + '-cup';
  const brewing = mode === 'brew';

  const cls = [
    'bean-roast',
    running ? 'is-on' : 'is-rest',
    brewing ? 'mode-brew' : 'mode-roast',
    running ? 'heat-' + heat : '',
    tone === 'light' ? 'tone-light' : '',
    staticMode ? 'is-static' : '',
    running ? '' : 'bean-bob',
    className,
  ].filter(Boolean).join(' ');

  return _R.createElement('svg', {
    width: size, height: size, viewBox: '-6 -6 128 118',
    className: cls,
    style: { display: 'block', flex: 'none', overflow: 'visible', ...style },
    'aria-hidden': true, focusable: 'false',
  },
    _R.createElement('defs', null,
      _R.createElement('radialGradient', { id: gid, cx: '50%', cy: '50%', r: '50%' },
        _R.createElement('stop', { className: 'br-h1', offset: '0%', stopOpacity: 0.85 }),
        _R.createElement('stop', { className: 'br-h2', offset: '45%', stopOpacity: 0.42 }),
        _R.createElement('stop', { className: 'br-h2', offset: '100%', stopOpacity: 0 })
      ),
      brewing && _R.createElement('clipPath', { id: cid },
        _R.createElement('path', { d: CUP })
      )
    ),

    // 1 — heat, beneath the glyph. Both variants share one envelope keyframe.
    heat === 'glow' && _R.createElement('ellipse', {
      className: 'br-heat', cx: 59, cy: 96, rx: 46, ry: 17, fill: 'url(#' + gid + ')',
    }),
    heat === 'flame' && _R.createElement('g', { className: 'br-heat br-flame' },
      _R.createElement('ellipse', { cx: 59, cy: 100, rx: 34, ry: 13, fill: 'url(#' + gid + ')' }),
      _R.createElement('g', { className: 'br-flicker' },
        _R.createElement('path', { className: 'br-fl-o', d: FLAME_OUTER }),
        _R.createElement('path', { className: 'br-fl-i', d: FLAME_INNER })
      )
    ),

    // 2 — the bean. Outer <g> owns the tilt (an attribute, so the CSS transforms below are free).
    _R.createElement('g', { transform: 'rotate(' + tilt + ' 59 55)' },
      _R.createElement('g', { className: 'br-judder' },
        _R.createElement('g', { className: 'br-swell' },
          _R.createElement('path', { className: 'br-body', d: ROAST_PATH }),
          _R.createElement('g', { transform: 'rotate(-32 40 42)' },
            _R.createElement('ellipse', { className: 'br-sheen', cx: 40, cy: 42, rx: 13, ry: 7 })
          )
        )
      )
    ),

    // 3–5 — grind, pour, cup. Skipped entirely in roast mode, so the compact mark stays two shapes.
    brewing && _R.createElement('g', { className: 'br-brewkit' },
      _R.createElement('g', { className: 'br-specks' },
        SPECKS.map(([x, y], i) => _R.createElement('circle', { key: i, cx: x, cy: y, r: 3.5 }))
      ),
      _R.createElement('path', { className: 'br-bed', d: BED }),
      _R.createElement('g', { className: 'br-dripper' },
        _R.createElement('path', { d: CONE }),
        _R.createElement('path', { d: 'M 12 42 L 106 42' })
      ),
      _R.createElement('rect', { className: 'br-stream', x: 55.5, y: -4, width: 7, height: 48, rx: 3.5 }),
      _R.createElement('g', { className: 'br-cup' },
        _R.createElement('path', { className: 'br-cup-body', d: CUP }),
        _R.createElement('path', { className: 'br-cup-handle', d: HANDLE }),
        _R.createElement('g', { clipPath: 'url(#' + cid + ')' },
          _R.createElement('rect', { className: 'br-brew', x: 20, y: 34, width: 78, height: 72 })
        )
      ),
      _R.createElement('g', { className: 'br-steam' },
        _R.createElement('path', { d: STEAM_A }),
        _R.createElement('path', { d: STEAM_B })
      )
    )
  );
}

// The Beanary — the mark, but you can press it.
//
// The roast only ever played during waits, which are short and rare, so the nicest thing in the
// product was something the operator never actually saw. This makes it hers to trigger: press Bean
// and he does one full run, then settles back to the idle bob.
//
// A drop-in for BeanMark, deliberately. At rest BeanRoast is the same path, viewBox, colour and
// 2.6s bob, so swapping it in changes NOTHING until someone clicks — no new chrome, no new control
// to explain, and it works on every page the mark already appears on.
//
// One loop, not a toggle: it stops on its own. A mark left spinning reads as "Bean is busy", which
// is a lie the rest of the app works hard to tell truthfully.
function PlayfulMark({ size = 28, mode, title, autoPlay, className = '', style = {}, ...rest }) {
  const [playing, setPlaying] = _R.useState(false);
  const timer = _R.useRef(null);
  const runMode = mode || (size >= 44 ? 'brew' : 'roast');

  _R.useEffect(() => () => { if (timer.current) clearTimeout(timer.current); }, []);

  // It plays IN PLACE, at its own size. Nothing scales, nothing floats.
  //
  // This went through two worse versions worth not repeating. Scaling the mark up on press made an
  // 84px bean hang out of a 56px header and onto the greeting card. Moving that to a floating card
  // fixed the collision but put a SECOND cup on screen beside the one already brewing — solving a
  // problem that only existed because the mark was too small to begin with. The mark is simply big
  // enough now, which is where this should have started.
  function play() {
    if (playing) return;                 // let the loop finish; re-triggering mid-run stutters
    setPlaying(true);
    timer.current = setTimeout(() => setPlaying(false), runMode === 'brew' ? 6000 : 2400);
  }

  // Play WITHOUT a press, when the caller says so. `autoPlay` is a truthy key, not a boolean: it
  // fires on mount when truthy and again whenever it CHANGES to a new truthy value, so the caller
  // decides how often — this component never decides for itself.
  //
  // WHO DECIDES MATTERS. The mark lives inside the inbox, which remounts on every return from a
  // draft, so a self-owned "play on mount" would run this ~20x a morning. That is the failure mode
  // the whole feature exists against: the roast was invisible because it only played during waits,
  // and the fix for invisible is not constant. bean-root.jsx holds the once-a-session ref.
  _R.useEffect(() => { if (autoPlay) play(); }, [autoPlay]);  // eslint-disable-line

  return _R.createElement('button', {
    type: 'button',
    className: 'beanary-btn ' + className,
    onClick: play,
    // Says what it does rather than naming a feature nobody has heard of yet.
    'aria-label': title || 'Press Bean',
    title: title || 'Press me',
    style,
  }, _R.createElement(BeanRoast, { size, mode: runMode, running: playing, ...rest }));
}

window.BeanRoast = BeanRoast;
window.PlayfulMark = PlayfulMark;
