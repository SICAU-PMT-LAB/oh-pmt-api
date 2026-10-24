/* 手写 SVG 图表。没有第三方依赖，颜色全部走 CSS 变量，
   主题切换时不用重绘就能跟着变（填色用 currentColor / var）。

   每个图表返回 SVG 字符串，容器宽度自适应（viewBox + width:100%）。 */

const CHART_FONT = "'SF Mono', ui-monospace, Menlo, monospace";

/* 图表按容器实际像素宽度绘制：1 个 viewBox 单位 = 1 CSS 像素，
   文字不会被拉伸。容器此时不可见（切到别的页）时回退到 720。 */
function chartWidth(el) {
  const w = el.clientWidth || (el.parentElement && el.parentElement.clientWidth) || 0;
  return Math.max(280, Math.round(w) || 720);
}

/* 窄图的左边距要跟着收，否则绘图区被轴线挤掉三分之一 */
function padsFor(W, base = {}) {
  const narrow = W < 460;
  return { padL: narrow ? 28 : (base.padL || 44), padR: narrow ? 8 : (base.padR || 12),
           padT: base.padT || 14, padB: base.padB || 26, narrow };
}

/* 记下每张图的规格，窗口变化和主题切换时按新宽度重画 */
const chartSpecs = new Map();
function remember(el, kind, spec) {
  chartSpecs.set(el, { kind, spec });
  if (!chartWidthHooked) {
    chartWidthHooked = true;
    let timer = 0;
    addEventListener('resize', () => {
      clearTimeout(timer);
      timer = setTimeout(() => chartSpecs.forEach((v, node) => {
        if (!node.isConnected) { chartSpecs.delete(node); return; }
        RENDERERS[v.kind](node, v.spec);
      }), 180);
    });
  }
}
let chartWidthHooked = false;
export function redrawAll() {
  chartSpecs.forEach((v, node) => {
    if (!node.isConnected) { chartSpecs.delete(node); return; }
    RENDERERS[v.kind](node, v.spec);
  });
}


function cssVar(name, fallback) {
  const v = getComputedStyle(document.documentElement).getPropertyValue(name);
  return (v && v.trim()) || fallback;
}

const fmtInt = n => (n || 0).toLocaleString('en-US');
const fmtCompact = n => {
  const v = Number(n || 0);
  if (v >= 1e6) return (v / 1e6).toFixed(v >= 1e7 ? 0 : 1) + 'M';
  if (v >= 1000) return (v / 1000).toFixed(v >= 1e4 ? 0 : 1) + 'k';
  return String(Math.round(v * 100) / 100);
};

/* 把数组映射到坐标，多个序列共用一套 y 轴刻度 */
function niceMax(v) {
  /* 轴顶取能被 4 整除的整数，刻度才不会出现 13 / 38 这种数字 */
  if (!v || v <= 0) return 4;
  const exp = Math.floor(Math.log10(v));
  const base = Math.pow(10, exp);
  for (const s of [1, 2, 2.5, 4, 5, 8, 10]) {
    const raw = s * base;
    if (v <= raw) return Math.max(4, Math.round(raw / 4) * 4);
  }
  return Math.ceil(v / 4) * 4;
}

function esc(s) {
  return String(s ?? '').replace(/[&<>"]/g, c =>
    ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;' }[c]));
}

/* ---------- 折线 / 面积图 ---------- */
/* series: [{name, color, values:[num]}], labels: [str] */
export function lineChart(el, spec) {
  remember(el, 'line', spec);
  drawLine(el, spec);
}

function drawLine(el, { labels, series, height = 200, yFmt = fmtCompact,
                        area = false, dots = true, unit = '' }) {
  const W = chartWidth(el), H = height;
  const { padL, padR, padT, padB, narrow } = padsFor(W);
  const iw = W - padL - padR, ih = H - padT - padB;
  const max = niceMax(Math.max(1, ...series.flatMap(s => s.values)));
  const n = labels.length;
  const x = i => padL + (n <= 1 ? iw / 2 : (i * iw) / (n - 1));
  const y = v => padT + ih - (v / max) * ih;

  const ticks = [0, 0.25, 0.5, 0.75, 1].map(t => Math.round(max * t));
  const grid = ticks.map(t => `
    <line class="grid-line" x1="${padL}" y1="${y(t)}" x2="${W - padR}" y2="${y(t)}"
          stroke-dasharray="${t === 0 ? '0' : '3,4'}"/>
    <text x="${padL - 8}" y="${y(t) + 3.5}" text-anchor="end">${esc(yFmt(t))}</text>`).join('');

  const labelStep = Math.max(1, Math.ceil(n / (narrow ? 4 : 8)));
  // 末尾标签一定画，紧邻它的那个要跳过，否则两个日期会叠在一起
  const lastStep = Math.floor((n - 1) / labelStep) * labelStep;
  const xLabels = labels.map((l, i) => {
    if (i !== 0 && i !== n - 1 && (i % labelStep || n - 1 - i < labelStep * 0.6)) return '';
    if (i === lastStep && n - 1 - i < labelStep * 0.6) return '';
    const anchor = i === 0 ? 'start' : i === n - 1 ? 'end' : 'middle';
    return `<text x="${x(i)}" y="${H - 8}" text-anchor="${anchor}">${esc(l)}</text>`;
  }).join('');

  const paths = series.map((s, si) => {
    const pts = s.values.map((v, i) => [x(i), y(v)]);
    const d = pts.map((p, i) => `${i ? 'L' : 'M'}${p[0].toFixed(1)},${p[1].toFixed(1)}`).join(' ');
    const color = s.color || 'var(--gold)';
    const areaPath = area
      ? `<path class="area" style="animation-delay:${si * 90}ms"
             d="${d} L${x(n - 1).toFixed(1)},${padT + ih} L${x(0).toFixed(1)},${padT + ih} Z"
             fill="url(#grad-${si})" opacity="1"/>`
      : '';
    const len = 1600;
    return `
      ${areaPath}
      <path class="line" style="--len:${len};animation-delay:${si * 110}ms"
            d="${d}" fill="none" stroke="${color}" stroke-width="1.9"
            stroke-linecap="round" stroke-linejoin="round"/>`;
  }).join('');

  const dotsHtml = dots ? series.map(s => s.values.map((v, i) => `
      <circle class="dot" style="animation-delay:${600 + i * 22}ms" cx="${x(i)}" cy="${y(v)}" r="2.4"
              fill="${s.color || 'var(--gold)'}"><title>${esc(labels[i])} · ${esc(yFmt(v))}</title></circle>`)
    .join('')).join('') : '';

  const grads = series.map((s, si) => `
    <linearGradient id="grad-${si}" x1="0" y1="0" x2="0" y2="1">
      <stop offset="0%" stop-color="${s.color || 'var(--gold)'}" stop-opacity=".26"/>
      <stop offset="100%" stop-color="${s.color || 'var(--gold)'}" stop-opacity="0"/>
    </linearGradient>`).join('');

  el.innerHTML = `<svg class="chart" width="${W}" height="${H}" viewBox="0 0 ${W} ${H}" 
      role="img" aria-label="${esc(unit || '趋势图')}">
    <defs>${grads}</defs>
    <g class="axisy">${grid}</g>
    <g class="axisx">${xLabels}</g>
    ${paths}
    ${dotsHtml}
  </svg>`;
  replay(el);
}

/* ---------- 堆叠面积图（按模型看构成） ---------- */
export function stackedArea(el, spec) {
  remember(el, 'stack', spec);
  drawStacked(el, spec);
}

function drawStacked(el, { labels, series, height = 200 }) {
  const W = chartWidth(el), H = height;
  const { padL, padR, padT, padB, narrow } = padsFor(W);
  const iw = W - padL - padR, ih = H - padT - padB;
  const n = labels.length;
  const totals = labels.map((_, i) => series.reduce((a, s) => a + (s.values[i] || 0), 0));
  const max = niceMax(Math.max(1, ...totals));
  const x = i => padL + (n <= 1 ? iw / 2 : (i * iw) / (n - 1));
  const y = v => padT + ih - (v / max) * ih;

  let acc = labels.map(() => 0);
  const layers = series.map((s, si) => {
    const lower = acc.slice();
    const upper = acc.map((v, i) => v + (s.values[i] || 0));
    acc = upper;
    const top = upper.map((v, i) => `${i ? 'L' : 'M'}${x(i).toFixed(1)},${y(v).toFixed(1)}`).join(' ');
    const bottom = lower.map((v, i) => `${x(i).toFixed(1)},${y(v).toFixed(1)}`).reverse().join(' L');
    return `<path class="area" style="animation-delay:${si * 110}ms"
              d="${top} L${bottom} Z" fill="${s.color}" opacity=".34"
              stroke="${s.color}" stroke-width="1" stroke-opacity=".55"/>`;
  }).join('');

  const ticks = [0, 0.5, 1].map(t => Math.round(max * t));
  const grid = ticks.map(t => `
    <line class="grid-line" x1="${padL}" y1="${y(t)}" x2="${W - padR}" y2="${y(t)}"
          stroke-dasharray="${t === 0 ? '0' : '3,4'}"/>
    <text x="${padL - 8}" y="${y(t) + 3.5}" text-anchor="end">${esc(fmtCompact(t))}</text>`).join('');
  const labelStep = Math.max(1, Math.ceil(n / (narrow ? 4 : 8)));
  // 末尾标签一定画，紧邻它的那个要跳过，否则两个日期会叠在一起
  const lastStep = Math.floor((n - 1) / labelStep) * labelStep;
  const xLabels = labels.map((l, i) => {
    if (i !== 0 && i !== n - 1 && (i % labelStep || n - 1 - i < labelStep * 0.6)) return '';
    if (i === lastStep && n - 1 - i < labelStep * 0.6) return '';
    const anchor = i === 0 ? 'start' : i === n - 1 ? 'end' : 'middle';
    return `<text x="${x(i)}" y="${H - 8}" text-anchor="${anchor}">${esc(l)}</text>`;
  }).join('');

  el.innerHTML = `<svg class="chart" width="${W}" height="${H}" viewBox="0 0 ${W} ${H}"  role="img">
    <g class="axisy">${grid}</g>
    <g class="axisx">${xLabels}</g>
    ${layers}
  </svg>`;
  replay(el);
}

/* ---------- 柱状图（支持双序列对比） ---------- */
export function barChart(el, spec) {
  remember(el, 'bar', spec);
  drawBar(el, spec);
}

function drawBar(el, { labels, values, values2 = null, height = 190,
                       color = 'var(--gold)', color2 = 'var(--rule-strong)',
                       yFmt = fmtCompact, unit = '' }) {
  const W = chartWidth(el), H = height;
  const { padL, padR, padT, padB, narrow } = padsFor(W, { padL: 40, padR: 10, padT: 16, padB: 28 });
  const iw = W - padL - padR, ih = H - padT - padB;
  const n = labels.length;
  const max = niceMax(Math.max(1, ...values, ...(values2 || [])));
  const slot = iw / Math.max(1, n);
  const gap = Math.min(6, slot * 0.24);
  const rawBw = values2 ? (slot - gap) / 2 : slot - gap * 2;
  const bw = Math.min(46, rawBw);
  const inset = (slot - (values2 ? bw * 2 + 2 : bw)) / 2;
  const y = v => padT + ih - (v / max) * ih;

  const ticks = [0, 0.5, 1].map(t => Math.round(max * t));
  const grid = ticks.map(t => `
    <line class="grid-line" x1="${padL}" y1="${y(t)}" x2="${W - padR}" y2="${y(t)}"
          stroke-dasharray="${t === 0 ? '0' : '3,4'}"/>
    <text x="${padL - 8}" y="${y(t) + 3.5}" text-anchor="end">${yFmt(t)}</text>`).join('');

  const bars = values.map((v, i) => {
    const bx = padL + i * slot + inset;
    const h1 = Math.max(v > 0 ? 2 : 0, (v / max) * ih);
    const b1 = `<rect class="bar" style="animation-delay:${i * 28}ms" x="${bx}" y="${padT + ih - h1}"
        width="${bw}" height="${h1}" rx="2" fill="${color}"><title>${esc(labels[i])} · ${esc(yFmt(v))}</title></rect>`;
    if (!values2) return b1;
    const v2 = values2[i] || 0;
    const h2 = Math.max(v2 > 0 ? 2 : 0, (v2 / max) * ih);
    return b1 + `<rect class="bar" style="animation-delay:${i * 28 + 60}ms" x="${bx + bw + 2}"
        y="${padT + ih - h2}" width="${bw}" height="${h2}" rx="2" fill="${color2}"><title>${esc(labels[i])} · ${esc(yFmt(v2))}</title></rect>`;
  }).join('');

  const labelStep = Math.max(1, Math.ceil(n / (narrow ? 5 : 12)));
  const xLabels = labels.map((l, i) => (i % labelStep === 0)
    ? `<text x="${padL + i * slot + slot / 2}" y="${H - 9}" text-anchor="middle">${esc(l)}</text>` : '').join('');

  el.innerHTML = `<svg class="chart" width="${W}" height="${H}" viewBox="0 0 ${W} ${H}" 
      role="img" aria-label="${esc(unit || '柱状图')}">
    <g class="axisy">${grid}</g>
    <g class="axisx">${xLabels}</g>
    ${bars}
  </svg>`;
  replay(el);
}

/* ---------- 环形进度（额度占比） ---------- */
export function ringChart(el, spec) {
  remember(el, 'ring', spec);
  drawRing(el, spec);
}

function drawRing(el, { value, total, size = 128, stroke = 9, label = '' }) {
  const pct = total > 0 ? Math.max(0, Math.min(1, value / total)) : 0;
  const r = (size - stroke) / 2 - 2;
  const c = 2 * Math.PI * r;
  const cx = size / 2, cy = size / 2;
  const color = pct <= 0.1 ? 'var(--err)' : pct <= 0.3 ? 'var(--warn)' : 'var(--gold)';
  el.innerHTML = `<svg class="chart" viewBox="0 0 ${size} ${size}" width="${size}" height="${size}"
      style="width:${size}px;height:${size}px" role="img" aria-label="额度 ${Math.round(pct * 100)}%">
    <circle cx="${cx}" cy="${cy}" r="${r}" fill="none" stroke="var(--rule)" stroke-width="${stroke}"/>
    ${pct > 0.001 ? `<circle class="ring-arc" cx="${cx}" cy="${cy}" r="${r}" fill="none" stroke="${color}"
      stroke-width="${stroke}" stroke-linecap="round"
      stroke-dasharray="${(c * pct).toFixed(2)} ${c.toFixed(2)}"
      transform="rotate(-90 ${cx} ${cy})"/>` : ''}
    <text x="${cx}" y="${cy - 2}" text-anchor="middle"
      style="font-family:var(--serif);font-size:${size * 0.24}px;font-weight:500"
      fill="var(--ink)">${Math.round(pct * 100)}<tspan style="font-size:${size * 0.12}px">%</tspan></text>
    <text x="${cx}" y="${cy + size * 0.18}" text-anchor="middle"
      style="font-family:${CHART_FONT};font-size:${size * 0.075}px;letter-spacing:.1em"
      fill="var(--ink-faint)">${esc(label)}</text>
  </svg>`;
  replay(el);
}

/* ---------- 横向条形榜 ---------- */
export function hbarList(el, spec) {
  remember(el, 'hbar', spec);
  drawHbar(el, spec);
}

function drawHbar(el, { rows, valueKey = 'value', labelKey = 'label',
                        colorKey = 'color', fmt = fmtCompact, unit = '' }) {
  if (!rows || !rows.length) { el.innerHTML = `<div class="empty">暂无数据</div>`; return; }
  const max = Math.max(1, ...rows.map(r => Number(r[valueKey]) || 0));
  el.innerHTML = rows.map((r, i) => {
    const v = Number(r[valueKey]) || 0;
    const w = (v / max) * 100;
    const color = r[colorKey] || 'var(--gold)';
    return `<div class="hbar" style="margin-bottom:11px">
      <div style="display:flex;justify-content:space-between;font-size:13px;gap:12px;margin-bottom:4px">
        <span>${esc(r[labelKey])}</span>
        <span class="mono" style="color:var(--ink-soft);font-size:11.5px">${esc(fmt(v))}${esc(unit)}</span>
      </div>
      <div class="progress" style="height:7px">
        <div data-w="${w.toFixed(1)}" style="background:${color};transition-delay:${i * 45}ms"></div>
      </div>
    </div>`;
  }).join('');
  // 下一帧再上宽度，才能看到生长动效
  requestAnimationFrame(() => {
    el.querySelectorAll('.progress > div[data-w]').forEach(d => {
      d.style.width = d.dataset.w + '%';
    });
  });
}

/* ---------- 直方图（Token 分布） ---------- */
export function histogram(el, spec) {
  remember(el, 'hist', spec);
  drawHist(el, spec);
}

function drawHist(el, { bins, height = 170, color = 'var(--gold)' }) {
  const W = chartWidth(el), H = height;
  const { padL, padR, padT, padB } = padsFor(W, { padL: 36, padR: 10, padT: 14, padB: 32 });
  const iw = W - padL - padR, ih = H - padT - padB;
  const n = bins.length;
  const max = niceMax(Math.max(1, ...bins.map(b => b.count)));
  const slot = iw / Math.max(1, n);
  const y = v => padT + ih - (v / max) * ih;
  const bars = bins.map((b, i) => {
    const h = Math.max(b.count > 0 ? 2 : 0, (b.count / max) * ih);
    return `<rect class="bar" style="animation-delay:${i * 55}ms" x="${padL + i * slot + slot * 0.16}"
      y="${padT + ih - h}" width="${slot * 0.68}" height="${h}" rx="2" fill="${color}">
      <title>${esc(b.label)} tokens · ${fmtInt(b.count)} 次</title></rect>
      <text x="${padL + i * slot + slot / 2}" y="${padT + ih - h - 5}" text-anchor="middle"
        class="value">${b.count ? fmtCompact(b.count) : ''}</text>`;
  }).join('');
  const labels = bins.map((b, i) =>
    `<text x="${padL + i * slot + slot / 2}" y="${H - 11}" text-anchor="middle">${esc(b.label)}</text>`).join('');
  el.innerHTML = `<svg class="chart" width="${W}" height="${H}" viewBox="0 0 ${W} ${H}"  role="img">
    <line class="grid-line" x1="${padL}" y1="${padT + ih}" x2="${W - padR}" y2="${padT + ih}"/>
    <text x="${padL - 8}" y="${y(max) + 3.5}" text-anchor="end">${fmtCompact(max)}</text>
    <g class="axisx">${labels}</g>${bars}</svg>`;
  replay(el);
}

/* ---------- 热力图（星期 × 小时） ---------- */
export function heatmap(el, spec) {
  remember(el, 'heat', spec);
  drawHeat(el, spec);
}

function drawHeat(el, { grid, weekdays, color = 'var(--gold)' }) {
  const padL = 42, padT = 16;
  const avail = chartWidth(el) - padL - 10;
  const cellW = Math.max(12, Math.min(34, avail / 24));
  const cellH = 20;
  const W = padL + 24 * cellW + 8, H = padT + 7 * cellH + 6;
  const max = Math.max(1, ...grid.flat());
  const cells = [];
  grid.forEach((row, d) => row.forEach((v, h) => {
    const o = v ? (0.12 + 0.82 * (v / max)) : 0.035;
    cells.push(`<rect x="${padL + h * cellW}" y="${padT + d * cellH}"
      width="${cellW - 2}" height="${cellH - 2}" rx="2"
      fill="${color}" fill-opacity="${o.toFixed(3)}"
      style="animation:fadeIn .5s ease ${(d * 24 + h) * 3}ms both">
      <title>${weekdays[d]} ${h}:00 · ${v} 次</title></rect>`);
  }));
  const hourTicks = [0, 6, 12, 18, 23].map(h =>
    `<text x="${padL + h * cellW + cellW / 2}" y="${padT - 6}" text-anchor="middle">${h}时</text>`).join('');
  const dayTicks = weekdays.map((w, d) =>
    `<text x="${padL - 8}" y="${padT + d * cellH + cellH / 2 + 3}" text-anchor="end">${esc(w)}</text>`).join('');
  el.innerHTML = `<svg class="chart" width="${W}" height="${H}" viewBox="0 0 ${W} ${H}"  role="img">
    <g class="axisx">${hourTicks}${dayTicks}</g>${cells.join('')}</svg>`;
  replay(el);
}

/* ---------- 迷你火花线（KPI 卡片用） ---------- */
export function sparkline(values, { w = 64, h = 20, color = 'var(--gold)' } = {}) {
  if (!values || values.length < 2) return '';
  const max = Math.max(...values, 1), min = Math.min(...values, 0);
  const span = max - min || 1;
  const pts = values.map((v, i) => [
    (i / (values.length - 1)) * (w - 2) + 1,
    h - 1 - ((v - min) / span) * (h - 3),
  ]);
  const d = pts.map((p, i) => `${i ? 'L' : 'M'}${p[0].toFixed(1)},${p[1].toFixed(1)}`).join(' ');
  return `<svg class="spark" width="${w}" height="${h}" viewBox="0 0 ${w} ${h}">
    <path d="${d}" fill="none" stroke="${color}" stroke-width="1.4" stroke-linecap="round"/></svg>`;
}

const RENDERERS = {
  line: drawLine, stack: drawStacked, bar: drawBar, ring: drawRing,
  hbar: drawHbar, hist: drawHist, heat: drawHeat,
};

/* 重播入场动效：先把动画关掉，强制重排，再打开 */
export function replay(el) {
  el.classList.remove('replay');
  void el.offsetWidth;   // 触发重排，动画才能重新开始
  el.classList.add('replay');
}

/* ---------------- 数字滚动累加 ---------------- */
function groupDigits(text) {
  const [int, dec] = String(text).split('.');
  return int.replace(/\B(?=(\d{3})+(?!\d))/g, ',') + (dec ? '.' + dec : '');
}

export function countUp(node, to, { decimals = 0, duration = 900, suffix = '',
                                    group = true } = {}) {
  const from = Number(node.dataset.value || 0);
  node.dataset.value = to;
  if (matchMedia('(prefers-reduced-motion: reduce)').matches) {
    const txt = Number(to).toFixed(decimals);
    node.textContent = (group ? groupDigits(txt) : txt) + suffix;
    return;
  }
  const t0 = performance.now();
  const tick = now => {
    const p = Math.min(1, (now - t0) / duration);
    const eased = 1 - Math.pow(1 - p, 3);
    const v = from + (to - from) * eased;
    const txt = v.toFixed(decimals);
    node.textContent = (group ? groupDigits(txt) : txt) + suffix;
    if (p < 1) requestAnimationFrame(tick);
  };
  requestAnimationFrame(tick);
}

/* ---------------- 主题 ---------------- */
const THEME_KEY = 'ohpmt_theme';

export function currentTheme() {
  return document.documentElement.dataset.theme || 'light';
}

export function applyTheme(theme) {
  document.documentElement.dataset.theme = theme;
  localStorage.setItem(THEME_KEY, theme);
  document.querySelectorAll('[data-theme-opt]').forEach(o => {
    o.classList.toggle('on', o.dataset.themeOpt === theme);
  });
  // 主题切换后图表用到的颜色变了，让页面自己重绘
  document.dispatchEvent(new CustomEvent('themechange', { detail: { theme } }));
}

export function initTheme() {
  // 默认白底金边；用户手动切过就按记住的来
  applyTheme(localStorage.getItem(THEME_KEY) || 'light');
  document.addEventListener('click', e => {
    const opt = e.target.closest('[data-theme-opt]');
    if (!opt) return;
    e.preventDefault();
    applyTheme(opt.dataset.themeOpt);
  });
}

/* ---------------- 提示条 ---------------- */
export function toast(message, kind = '') {
  let wrap = document.querySelector('.toast-wrap');
  if (!wrap) {
    wrap = document.createElement('div');
    wrap.className = 'toast-wrap';
    document.body.appendChild(wrap);
  }
  const el = document.createElement('div');
  el.className = 'toast ' + kind;
  el.textContent = message;
  wrap.appendChild(el);
  setTimeout(() => {
    el.classList.add('out');
    setTimeout(() => el.remove(), 300);
  }, kind === 'err' ? 5200 : 2600);
}

/* ---------------- 请求 ---------------- */
export async function api(path, opts = {}) {
  const res = await fetch(path, {
    ...opts,
    credentials: 'same-origin',
    headers: { 'Content-Type': 'application/json', ...(opts.headers || {}) },
  });
  const text = await res.text();
  let data = {};
  try { data = JSON.parse(text); } catch { data = { error: { message: text } }; }
  // 只有「没有登录」才跳登录页；403 是业务拒绝（模型/来源不允许等），原样抛给调用方
  if (res.status === 401) {
    location.href = '/login';
    throw new Error('登录已过期');
  }
  if (!res.ok) throw new Error((data.error && data.error.message) || '请求失败');
  return data;
}

export { esc, fmtInt, fmtCompact, niceMax, cssVar };
