// Bean mark — a simple, elegant kidney-bean glyph. Color shifts to signal confidence state.
const BEAN_PATH = 'M 34 18 C 22 20 14 34 14 50 C 14 76 32 92 56 92 C 80 92 104 78 104 52 C 104 32 96 18 84 18 C 74 18 66 30 60 34 C 54 30 46 18 34 18 Z';

function BeanMark({ size = 28, color = '#6E4327', tilt = -14, bob = false, style = {}, className = '' }) {
  return React.createElement('svg', {
    width: size, height: size, viewBox: '-6 -6 128 118',
    className: 'bean-mark ' + (bob ? 'bean-bob ' : '') + className,
    style: { display: 'block', flex: 'none', overflow: 'visible', ...style },
    'aria-hidden': true,
  },
    React.createElement('g', { transform: `rotate(${tilt} 59 55)` },
      React.createElement('path', { d: BEAN_PATH, fill: color }),
      React.createElement('ellipse', {
        cx: 40, cy: 42, rx: 13, ry: 7, fill: '#ffffff', opacity: 0.20,
        transform: 'rotate(-32 40 42)',
      })
    )
  );
}

window.BeanMark = BeanMark;
