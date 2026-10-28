/* 站点共用：格式化工具 + 无依赖的小图表。
   只导出站点页面用到的东西，管理后台的 charts.js 仍然独立。 */

export function fmtInt(n) {
  return new Intl.NumberFormat('zh-CN').format(Math.round(Number(n) || 0));
}

const COMPACT_UNITS = [
  [1e12, 'T'], [1e9, 'B'], [1e6, 'M'], [1e3, 'K'],
];

export function fmtCompact(n) {
  const v = Number(n) || 0;
  if (v < 1000) return String(Math.round(v));
  for (const [edge, unit] of COMPACT_UNITS) {
    if (v >= edge) {
      const x = v / edge;
      return (x >= 100 ? x.toFixed(0) : x >= 10 ? x.toFixed(1) : x.toFixed(1)) + unit;
    }
  }
  return String(Math.round(v));
}

export function fmtMoney(n, symbol = '¥') {
  const v = Number(n) || 0;
  if (v === 0) return symbol + '0';
  if (v < 0.01) return symbol + v.toFixed(4);
  if (v < 1) return symbol + v.toFixed(3);
  if (v < 100) return symbol + v.toFixed(2);
  return symbol + fmtInt(v);
}

export function fmtPct(n, digits = 0) {
  if (n === null || n === undefined) return '—';
  return (Number(n) || 0).toFixed(digits) + '%';
}

export function esc(s) {
  return String(s ?? '').replace(/[&<>"']/g, (c) =>
    ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]));
}

/** 轴上用的短标签：SEP 1 / OCT 26 */
export function shortDate(iso) {
  const [y, m, d] = String(iso).split('-').map(Number);
  if (!y) return String(iso);
  const months = ['JAN', 'FEB', 'MAR', 'APR', 'MAY', 'JUN',
                  'JUL', 'AUG', 'SEP', 'OCT', 'NOV', 'DEC'];
  return `${months[m - 1]} ${d}`;
}

/** 本地时间的人话格式：10月26日, GMT+8 21:28 */
export function stamp(ts) {
  const d = new Date((Number(ts) || 0) * 1000);
  const off = -d.getTimezoneOffset() / 60;
  const sign = off >= 0 ? '+' : '−';
  const hh = String(d.getHours()).padStart(2, '0');
  const mm = String(d.getMinutes()).padStart(2, '0');
  return `${d.getMonth() + 1}月${d.getDate()}日, GMT${sign}${Math.abs(off)} ${hh}:${mm}`;
}

/* ---------- 主题 ---------- */

/* 手动选择优先，没选过才跟随系统。
   存储键与后台页面（ohpmt_theme）和登录页（pmt_login_theme）都分开，
   这样调一个站点的主题不会连带改掉另一个。 */
export const THEME_KEY = 'pmt_site_theme';

export function currentTheme() {
  const forced = document.documentElement.dataset.theme;
  if (forced === 'dark' || forced === 'light') return forced;
  return window.matchMedia?.('(prefers-color-scheme: dark)').matches ? 'dark' : 'light';
}

/** 页面首屏前调用：把存过的选择落到 <html data-theme>，避免闪一下白。 */
export function initTheme() {
  let saved = null;
  try { saved = localStorage.getItem(THEME_KEY); } catch { /* 隐私模式下读写会抛错 */ }
  if (saved === 'dark' || saved === 'light') {
    document.documentElement.dataset.theme = saved;
  }
  return currentTheme();
}

/**
 * 切换主题。支持 View Transition 时从点击位置做圆形扩散，
 * 否则直接换色。
 * @param {'light'|'dark'} next
 * @param {MouseEvent} [event] 用来取点击坐标，缺省则从右上角扩散
 */
export function setTheme(next, event) {
  const apply = () => {
    // 清掉首屏脚本写死的内联底色，让 html 回到 site.css 的 --color-site-bg。
    // 内联样式优先级高于样式表，留着它切到深色时页面外围会一直是白底。
    document.documentElement.style.background = '';
    document.documentElement.dataset.theme = next;
    try { localStorage.setItem(THEME_KEY, next); } catch { /* 同上 */ }
    // 后台页的手写 SVG 图表颜色取自 CSS 变量，需要页面重绘一次
    document.dispatchEvent(new CustomEvent('themechange', { detail: { theme: next } }));
  };
  const reduce = window.matchMedia?.('(prefers-reduced-motion: reduce)').matches;
  if (!document.startViewTransition || reduce) { apply(); return Promise.resolve(); }

  // 圆心取真实点击坐标，扩散才是从手指/鼠标的位置长出来的
  const x = event?.clientX ?? window.innerWidth - 60;
  const y = event?.clientY ?? 60;
  // 点击点到视口最远角的距离，保证圆能盖满全屏
  const r = Math.hypot(
    Math.max(x, window.innerWidth - x),
    Math.max(y, window.innerHeight - y),
  );

  const vt = document.startViewTransition(apply);
  vt.ready.then(() => {
    document.documentElement.animate(
      { clipPath: [`circle(0px at ${x}px ${y}px)`, `circle(${r}px at ${x}px ${y}px)`] },
      { duration: 600, easing: 'ease-in-out',
        pseudoElement: '::view-transition-new(root)' },
    );
  }).catch(() => {});
  return vt.finished.catch(() => {});
}

export function cssVar(name, fallback = '') {
  const v = getComputedStyle(document.documentElement).getPropertyValue(name).trim();
  return v || fallback;
}

/* ---------- 通用：滚动淡入 ---------- */

export function revealOnScroll(root = document) {
  const nodes = [...root.querySelectorAll('[data-reveal]')];
  if (!nodes.length) return;
  if (!('IntersectionObserver' in window)) {
    nodes.forEach((n) => n.setAttribute('data-visible', ''));
    return;
  }
  const io = new IntersectionObserver((entries) => {
    entries.forEach((e) => {
      if (!e.isIntersecting) return;
      e.target.setAttribute('data-visible', '');
      io.unobserve(e.target);
    });
  }, { rootMargin: '0px 0px -8% 0px', threshold: 0.02 });
  nodes.forEach((n) => io.observe(n));
}

/* ---------- 堆叠柱状图 ---------- */

// 图表分类色板：改成与后台 /me、/admin 一致的白金/黑金体系。
// 十个色相都压在金色到青铜的区间里，靠明度而不是色相拉开区分度，
// 这样堆叠柱仍然分得清，又不会在纸白底上炸出一堆杂色。
export const SERIES_COLORS_DARK = [
  '#ffffff', '#d2d2d7', '#a1a1a6', '#86868b', '#6e6e73',
  '#c7c7cc', '#48484a', '#e8e8ea', '#9a9aa0', '#5a5a5f',
];
export const SERIES_COLORS_LIGHT = [
  '#1d1d1f', '#6e6e73', '#a1a1a6', '#48484a', '#86868b',
  '#c7c7cc', '#2c2c2e', '#b8b8bd', '#d2d2d7', '#8e8e93',
];

export function seriesColors() {
  return currentTheme() === 'dark' ? SERIES_COLORS_DARK : SERIES_COLORS_LIGHT;
}

/**
 * 堆叠柱状图。
 * @param {HTMLElement} el 容器
 * @param {{labels:string[], series:{name:string,values:number[]}[], height?:number,
 *          yFmt?:(n:number)=>string, valueFmt?:(n:number)=>string,
 *          gap?:number, radius?:number}} spec
 */
export function stackedBars(el, spec) {
  const {
    labels, series, height = 320, yFmt = fmtCompact,
    valueFmt = fmtCompact, gap = 2, radius = 0,
  } = spec;
  const W = Math.max(320, el.clientWidth);
  const padL = 44, padR = 8, padT = 14, padB = 30;
  const plotW = W - padL - padR;
  const plotH = height - padT - padB;
  const n = labels.length || 1;
  const step = plotW / n;
  const barW = Math.max(1, step - gap);

  // 每日合计 → 决定 y 轴上限
  const totals = labels.map((_, i) =>
    series.reduce((s, ser) => s + (ser.values[i] || 0), 0));
  const max = Math.max(1, ...totals);
  const ticks = niceTicks(max, 4);
  const yMax = ticks[ticks.length - 1] || max;
  const y = (v) => padT + plotH - (v / yMax) * plotH;

  const colors = seriesColors();
  const parts = [];

  // 网格线 + y 轴刻度
  ticks.forEach((t) => {
    const yy = y(t);
    parts.push(`<line x1="${padL}" y1="${yy.toFixed(1)}" x2="${W - padR}" y2="${yy.toFixed(1)}"
      stroke="currentColor" stroke-opacity="0.12" stroke-width="1"/>`);
    parts.push(`<text x="${padL - 8}" y="${(yy + 4).toFixed(1)}" text-anchor="end"
      font-size="11" fill="currentColor" fill-opacity="0.5">${esc(yFmt(t))}</text>`);
  });

  // 柱体：自下而上堆叠
  labels.forEach((label, i) => {
    const x = padL + i * step + gap / 2;
    let acc = 0;
    series.forEach((ser, si) => {
      const v = ser.values[i] || 0;
      if (v <= 0) return;
      const y0 = y(acc);
      const y1 = y(acc + v);
      const h = Math.max(0, y0 - y1);
      acc += v;
      const color = colors[si % colors.length];
      parts.push(`<rect x="${x.toFixed(2)}" y="${y1.toFixed(2)}" width="${barW.toFixed(2)}"
        height="${h.toFixed(2)}" fill="${color}" rx="${radius}">
        <title>${esc(label)} · ${esc(ser.name)}: ${esc(valueFmt(v))}</title></rect>`);
    });
  });

  // x 轴标签：等距抽稀，首尾各留一个
  const every = Math.max(1, Math.round(n / 8));
  labels.forEach((label, i) => {
    if (i % every && i !== n - 1) return;
    const x = padL + i * step + step / 2;
    parts.push(`<text x="${x.toFixed(1)}" y="${height - 10}" text-anchor="middle"
      font-size="10.5" fill="currentColor" fill-opacity="0.5"
      transform="rotate(-90 ${x.toFixed(1)} ${height - 10})">${esc(label)}</text>`);
  });

  el.innerHTML = `<svg class="bar-chart" viewBox="0 0 ${W} ${height}"
    preserveAspectRatio="none" role="img" style="width:100%;height:${height}px">${parts.join('')}</svg>`;
}

export function niceTicks(max, count = 4) {
  const raw = max / count;
  const mag = Math.pow(10, Math.floor(Math.log10(raw || 1)));
  const norm = raw / mag;
  const step = (norm <= 1 ? 1 : norm <= 2 ? 2 : norm <= 5 ? 5 : 10) * mag;
  const out = [];
  for (let v = 0; v <= max + step * 0.999; v += step) out.push(v);
  return out;
}

/* ---------- 横向条形 ---------- */

export function hbars(el, rows, { max = null, fmt = fmtCompact, barClass = '' } = {}) {
  const top = max ?? Math.max(1, ...rows.map((r) => r.value));
  el.innerHTML = rows.map((r) => `
    <div class="hbar">
      <div class="hbar-head">
        <span class="hbar-label">${esc(r.label)}</span>
        <span class="hbar-value mono-num">${esc(fmt(r.value))}</span>
      </div>
      <div class="hbar-track"><span class="hbar-fill ${barClass}"
        style="width:${((r.value / top) * 100).toFixed(2)}%"></span></div>
    </div>`).join('');
}

/** 排名列表（opencode 数据页的 01 / 02 / 03 卡片） */
export function rankCards(el, rows, { fmt = fmtCompact, limit = 3 } = {}) {
  el.innerHTML = rows.slice(0, limit).map((r, i) => `
    <article class="rank-card">
      <div class="rank-no">${String(i + 1).padStart(2, '0')}</div>
      <div class="rank-body">
        <div class="rank-name">${esc(r.label)}</div>
        <div class="rank-sub">${esc(r.sub || '')}</div>
      </div>
      <div class="rank-value mono-num">${esc(fmt(r.value))}</div>
    </article>`).join('');
}
