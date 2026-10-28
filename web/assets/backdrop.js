/* 站点通用动效背景：星空 + 星轨 + 透视网格。
 *
 * 与登录页同一套画法，抽出来给所有页面用，避免每个页面各写一份。页面只要
 * 在自己的容器里放两个空节点：
 *
 *   <div class="pmt-backdrop" aria-hidden="true">
 *     <div class="pmt-backdrop-nebula"></div>
 *     <svg class="pmt-backdrop-stars"></svg>
 *   </div>
 *   <canvas class="pmt-backdrop-grid"></canvas>
 *
 * 然后 `import { mountBackdrop } from '/assets/backdrop.js'` 调一次即可。
 * 深色/浅色跟随 `html[data-theme]`（登录页）或 `prefers-color-scheme`。
 */

import { initGridScan } from '/assets/gridscan.js';

const GOLDEN_ANGLE = Math.PI * (3 - Math.sqrt(5));

/* 确定性星图：同一个种子刷新后位置不变，避免每次加载都在跳。 */
const STARS = (() => {
  const out = [];
  const COUNT = 144;
  for (let i = 0; i < COUNT; i += 1) {
    const t = (i + 0.5) / COUNT;
    const rNorm = Math.sqrt(t);
    const theta = i * GOLDEN_ANGLE;
    const jx = ((Math.sin(i * 12.9898 + 78.233) * 43758.5453) % 1 + 1) % 1 - 0.5;
    const jy = ((Math.sin(i * 45.164 + 19.13) * 28461.321) % 1 + 1) % 1 - 0.5;
    const magSeed = ((Math.sin(i * 93.712 + 51.47) * 19283.741) % 1 + 1) % 1;
    out.push({
      x: Math.round((((0.5 + 0.68 * rNorm * Math.cos(theta) + jx * 0.06) % 1 + 1) % 1) * 1440),
      y: Math.round((((0.5 + 0.68 * rNorm * Math.sin(theta) + jy * 0.06) % 1 + 1) % 1) * 900),
      r: +(0.65 + Math.pow(magSeed, 2.2) * 1.65).toFixed(2),
      o: +(0.28 + Math.pow(magSeed, 2.2) * 0.65).toFixed(2),
      delay: `${((i % 19) * 0.31).toFixed(2)}s`,
      dur: `${(2.8 + (i % 8) * 0.55).toFixed(2)}s`,
      twinkle: i % 3 !== 0,
    });
  }
  return out;
})();

const FEATURED = [
  [185, 135, 2.2, '0s'], [335, 142, 1.9, '1.2s'], [1115, 178, 2.4, '0.6s'],
  [1275, 205, 2.0, '2.1s'], [265, 685, 2.1, '1.7s'], [1148, 632, 2.3, '0.9s'],
  [485, 215, 1.9, '2.8s'], [945, 255, 2.1, '1.4s'], [840, 710, 1.9, '2.4s'],
  [640, 145, 1.8, '0.4s'], [420, 745, 2.0, '1.9s'],
];

function paintStars(svg) {
  if (!svg) return;
  const gid = `pmt-star-glow-${Math.random().toString(36).slice(2, 8)}`;
  svg.innerHTML = `
    <defs>
      <radialGradient id="${gid}" cx="50%" cy="50%" r="50%">
        <stop offset="0%" stop-color="currentColor" stop-opacity=".95"/>
        <stop offset="35%" stop-color="currentColor" stop-opacity=".35"/>
        <stop offset="100%" stop-color="currentColor" stop-opacity="0"/>
      </radialGradient>
    </defs>
    <g class="pmt-orbits" fill="none" stroke="currentColor">
      <ellipse cx="720" cy="450" rx="560" ry="260" stroke-width=".6"
               stroke-dasharray="3 9" transform="rotate(-14 720 450)"/>
      <ellipse cx="720" cy="450" rx="390" ry="175" stroke-width=".5"
               stroke-dasharray="2 12" transform="rotate(18 720 450)"/>
      <circle cx="720" cy="450" r="310" stroke-width=".45" stroke-dasharray="1 8"/>
    </g>
    <g class="pmt-constellations" fill="none" stroke="currentColor"
       stroke-width=".65" stroke-dasharray="2 4">
      <polyline points="140,155 210,125 268,168 335,142 398,195"/>
      <polyline points="1030,135 1115,178 1190,148 1275,205"/>
      <line x1="1115" y1="178" x2="1085" y2="255"/>
      <polyline points="195,640 265,685 320,665 375,725"/>
      <polyline points="1060,675 1148,632 1225,688 1310,645"/>
    </g>
    <g fill="currentColor">
      ${FEATURED.map(([x, y, r, d]) => `
        <g class="pmt-twinkle" style="animation-delay:${d}" transform="translate(${x},${y})">
          <circle r="7" fill="url(#${gid})"/>
          <path d="M-5.5 0H5.5M0-5.5V5.5" stroke="currentColor" stroke-width=".7"/>
          <circle r="${r}"/>
        </g>`).join('')}
      ${STARS.map((d) => `
        <circle class="${d.twinkle ? 'pmt-twinkle' : ''}" cx="${d.x}" cy="${d.y}"
                r="${d.r}" opacity="${d.o}"
                style="animation-delay:${d.delay};animation-duration:${d.dur}"/>`).join('')}
    </g>`;
}

function isDark() {
  // data-theme 由 theme-toggle.js / 各页首屏脚本写入，indexed 到手动选择；
  // 没有就跟随系统。
  const explicit = document.documentElement.dataset.theme;
  if (explicit === 'dark') return true;
  if (explicit === 'light') return false;
  return window.matchMedia('(prefers-color-scheme: dark)').matches;
}

/**
 * 挂载背景。
 * @param {{ root?: Element, grid?: boolean }} [opts]
 * @returns {{ setTheme(dark: boolean): void, destroy(): void }}
 */
export function mountBackdrop(opts = {}) {
  const root = opts.root || document;
  document.documentElement.classList.add('has-backdrop');
  const svg = root.querySelector('.pmt-backdrop-stars');
  paintStars(svg);

  const canvas = root.querySelector('.pmt-backdrop-grid');
  let grid = null;
  if (canvas && opts.grid !== false) {
    try {
      // 线条用后台那套金色：浅色纸白底上走青铜金，深色黑底上走亮金。
      // 其余动效参数（扫描周期、涟漪回弹）保持 gridscan 默认值。
      grid = initGridScan(canvas, {
        linesColorLight: '#b5b0a3',
        linesColorDark: '#706c61',
        scanColorLight: '#1c1c1a',
        scanColorDark: '#fff3d1',
      });
    } catch (err) {
      // 拿不到 WebGL 时静默降级：星空仍在，不出现空白层
      console.warn('背景网格不可用', err);
    }
  }

  const api = {
    setTheme(dark) {
      if (grid && grid.setTheme) grid.setTheme(dark);
    },
    destroy() {
      if (grid && grid.destroy) grid.destroy();
    },
  };

  api.setTheme(isDark());

  // theme-toggle.js 改 <html data-theme> 时把新配色推给 WebGL
  const observer = new MutationObserver(() => api.setTheme(isDark()));
  observer.observe(document.documentElement, {
    attributes: true, attributeFilter: ['data-theme'],
  });

  // 跟随系统主题变化（页面自己切主题时也会调 setTheme）
  const mq = window.matchMedia('(prefers-color-scheme: dark)');
  const onChange = () => api.setTheme(isDark());
  if (mq.addEventListener) mq.addEventListener('change', onChange);
  else if (mq.addListener) mq.addListener(onChange);
  api.destroy = ((orig) => () => {
    if (mq.removeEventListener) mq.removeEventListener('change', onChange);
    else if (mq.removeListener) mq.removeListener(onChange);
    observer.disconnect();
    orig();
  })(api.destroy);

  return api;
}
