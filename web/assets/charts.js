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

/* 人民币金额。单价来自各家官方中文定价页（元/百万 tokens），所以成本、
   额度、图表统一用元。单次请求常在 ¥0.0001 量级，固定小数位会全显示成 0，
   小额给到 3 位有效数字，上了 1 分再回到四位、上了 1 元回到两位。 */
const fmtMoney = n => {
  const v = Number(n || 0);
  if (!v) return '¥0';
  const a = Math.abs(v);
  if (a >= 1) return '¥' + v.toFixed(2);
  if (a >= 0.01) return '¥' + v.toFixed(4);
  return '¥' + Number(v.toPrecision(3)).toString();
};

/* 同一根轴上的刻度必须用同样的小数位，否则会出现 ¥0 / ¥1.25 / ¥2.50
   和 ¥0.6 / ¥1.2 混排。位数由「刻度间隔」决定，不是轴顶：轴顶 8 分 4 份，
   间隔是 2，整数就够，不该写成 8.00。 */
const decimalsFor = step => {
  const a = Math.abs(Number(step) || 0);
  if (a >= 1) return 0;
  if (a >= 0.1) return 1;
  if (a >= 0.01) return 2;
  if (a >= 0.001) return 3;
  return 4;
};

const fmtAxisMoney = top => {
  const d = decimalsFor(Number(top) / 4);
  return n => {
    const v = Number(n) || 0;
    return v ? '¥' + v.toFixed(d) : '¥0';
  };
};

/* 积分刻度同理：上游单次可能只扣千分之几个积分，位数跟着间隔走 */
const fmtAxisNum = top => {
  const d = decimalsFor(Number(top) / 4);
  return n => {
    const v = Number(n) || 0;
    if (!v) return '0';
    if (Math.abs(v) >= 10000) return (v / 1000).toFixed(0) + 'k';
    return v.toFixed(d);
  };
};

const fmtCompact = n => {
  const v = Number(n || 0);
  if (v >= 1e6) return (v / 1e6).toFixed(v >= 1e7 ? 0 : 1) + 'M';
  if (v >= 1000) return (v / 1000).toFixed(v >= 1e4 ? 0 : 1) + 'k';
  return String(Math.round(v * 100) / 100);
};

/* 把数组映射到坐标，多个序列共用一套 y 轴刻度 */
/* 把轴顶切成 steps 份，每份都是好读的数：1 / 2 / 2.5 / 5 × 10^n。
   这样刻度是 0 / 5 / 10 而不是 0 / 1.25 / 2.5 / 3.75 这种怪数。 */
function axisTicks(max, steps = 4) {
  return Array.from({ length: steps + 1 }, (_, i) => (max / steps) * i);
}

/* 轴顶：取一个不小于 v、且能被 steps 整除成好读刻度的数。
   不能写死下限（以前是 4）：一张图的最大值常常只有 0.02，
   被抬到 4 之后整片面积压在轴底，看上去就是「图没画出来」。 */
function niceMax(v, steps = 4) {
  if (!v || v <= 0) return steps;
  // 在 1e-9 ~ 1e12 的每个数量级里找最小的、能整除成好读刻度的轴顶。
  // 只在 v 自己的数量级里找是不够的：v=11.52 时步长必须小于 10，
  // 否则轴顶直接被抬到 40，曲线压在下半截。
  for (let exp = -9; exp <= 12; exp++) {
    const base = Math.pow(10, exp);
    // 步长只用 1 / 2 / 5 这类，刻度的尾数才不会出现 0.025 这种数
    for (const step of [1, 2, 5]) {
      const top = step * base * steps;
      if (top >= v * (1 - 1e-9)) return top;
    }
  }
  return Math.ceil(v / steps) * steps;
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
  const max = niceMax(Math.max(1e-9, ...series.flatMap(s => s.values)), 4);
  const ticks = axisTicks(max, 4);
  const n = labels.length;
  const x = i => padL + (n <= 1 ? iw / 2 : (i * iw) / (n - 1));
  const y = v => padT + ih - (v / max) * ih;

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

function drawStacked(el, { labels, series, height = 200, yFmt = fmtCompact }) {
  const W = chartWidth(el), H = height;
  const { padL, padR, padT, padB, narrow } = padsFor(W);
  const iw = W - padL - padR, ih = H - padT - padB;
  const n = labels.length;
  const totals = labels.map((_, i) => series.reduce((a, s) => a + (s.values[i] || 0), 0));
  const max = niceMax(Math.max(1e-9, ...totals), 4);
  const ticks = axisTicks(max, 4);
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
                       yFmt = fmtCompact, unit = '', bare = false }) {
  const W = chartWidth(el), H = height;
  // bare：缩略柱状图（KPI 卡里那种 40 来像素高的），不要坐标轴和标签，
  // 否则 46px 的高度里 28px 都被刻度和留白占了
  const { padL, padR, padT, padB, narrow } = bare
    ? { padL: 2, padR: 2, padT: 4, padB: 4, narrow: W < 460 }
    : padsFor(W, { padL: 40, padR: 10, padT: 16, padB: 28 });
  const iw = W - padL - padR, ih = H - padT - padB;
  const n = labels.length;
  const max = niceMax(Math.max(1e-9, ...values, ...(values2 || [])), 4);
  const ticks = axisTicks(max, 4);
  const slot = iw / Math.max(1, n);
  const gap = Math.min(6, slot * 0.24);
  const rawBw = values2 ? (slot - gap) / 2 : slot - gap * 2;
  // bare 缩略图铺满整格：14 天摊在一张卡里，每格只有十几像素，
  // 再按柱宽上限收窄就只剩右边几根细线
  const bw = bare ? Math.min(24, Math.max(2, slot - Math.min(3, slot * 0.3)))
                  : Math.min(46, rawBw);
  // bare 时柱子靠左对齐，天数少的时候几根细柱不会飘在整格正中
  const inset = bare ? 1
                     : (slot - (values2 ? bw * 2 + 2 : bw)) / 2;
  const y = v => padT + ih - (v / max) * ih;

  const grid = bare ? '' : ticks.map(t => `
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
  // 每格只画一个标签时，长名字会互相压在一起（模型名普遍十几字符），
  // 按格子可用宽度截断，鼠标悬停时靠图例和 title 看全名
  const maxChars = Math.max(4, Math.floor(slot / 5.6));
  const clip = s => (s.length > maxChars ? s.slice(0, maxChars - 1) + '\u2026' : s);
  const xLabels = bare ? '' : labels.map((l, i) => (i % labelStep === 0)
    ? `<text x="${padL + i * slot + slot / 2}" y="${H - 9}" text-anchor="middle">${esc(clip(String(l)))}</text>` : '').join('');

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
  // 原先是 pct <= 0.1 就画红色，方向反了：刚用一点点反而报警，
  // 用到八九成却是普通色。阈值改成从低到高：正常青 → 接近上限琥珀 → 超限红。
  const color = pct >= 1 ? 'var(--p-dv-3, var(--err))'
    : pct >= 0.8 ? 'var(--p-dv-2, var(--warn))'
    : 'var(--p-dv-1, var(--gold))';
  el.innerHTML = `<svg class="chart" viewBox="0 0 ${size} ${size}" width="${size}" height="${size}"
      style="width:${size}px;height:${size}px" role="img" aria-label="额度 ${Math.round(pct * 100)}%">
    <circle cx="${cx}" cy="${cy}" r="${r}" fill="none"
      stroke="var(--p-line, var(--rule))" stroke-width="${stroke}"/>
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
  // 分母取最大的那一行；全为 0 时退到 1，避免除零。不能写死下限 1，
  // 否则整张榜的数字都在 1 以下时，所有条形都缩成头发丝。
  const max = Math.max(1e-9, ...rows.map(r => Number(r[valueKey]) || 0));
  el.innerHTML = rows.map((r, i) => {
    const v = Number(r[valueKey]) || 0;
    // 末位补一点宽度，很小的值也能看见一条线而不是完全消失
    const w = v > 0 ? Math.max(0.8, (v / max) * 100) : 0;
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
  const max = niceMax(Math.max(1e-9, ...bins.map(b => b.count)));
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

export function applyTheme(theme, origin = null) {
  const root = document.documentElement;
  const commit = () => {
    // 清掉首屏脚本写死的内联底色，否则切到深色时外围一直是白底
    root.style.background = '';
    root.dataset.theme = theme;
    localStorage.setItem(THEME_KEY, theme);
    document.querySelectorAll('[data-theme-opt]').forEach(o => {
      o.classList.toggle('on', o.dataset.themeOpt === theme);
    });
    // 主题切换后图表用到的颜色变了，让页面自己重绘
    document.dispatchEvent(new CustomEvent('themechange', { detail: { theme } }));
  };

  // 用 View Transitions 做以切换按钮为圆心的扩散：新主题从按钮位置
  // 向外铺开。不支持或用户开启了减弱动效时直接换，不留半秒空白。
  const reduce = matchMedia('(prefers-reduced-motion: reduce)').matches;
  if (!origin || reduce || !document.startViewTransition) {
    commit();
    return;
  }
  const r = origin.getBoundingClientRect();
  const x = r.left + r.width / 2;
  const y = r.top + r.height / 2;
  const radius = Math.hypot(Math.max(x, innerWidth - x),
                            Math.max(y, innerHeight - y));
  const vt = document.startViewTransition(commit);
  vt.ready.then(() => {
    root.animate(
      { clipPath: [`circle(0px at ${x}px ${y}px)`,
                   `circle(${radius.toFixed(0)}px at ${x}px ${y}px)`] },
      { duration: 600, easing: 'ease-in-out',
        pseudoElement: '::view-transition-new(root)' },
    );
  }).catch(() => { /* 动画失败不影响主题已切换 */ });
}

// 单图标切换按钮：亮色时画月亮（点了变暗），暗色时画太阳。
// 显示的永远是「点下去会变成什么」，不是当前状态。
/* 图标沿用官网导航那颗：细线条 SVG，切换时图标本身也换面。
   用文字符号（☀/☾）不同字体渲染出的粗细差异很大，深浅两套背景上
   一眼就能看出不齐。 */
const SUN = `<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.5"
  aria-hidden="true"><path stroke-linecap="round" stroke-linejoin="round"
  d="M12 3v2.25m6.364.386-1.591 1.591M21 12h-2.25m-.386 6.364-1.591-1.591M12 18.75V21m-4.773-4.227-1.591 1.591M5.25 12H3m4.227-4.773L5.636 5.636M15.75 12a3.75 3.75 0 1 1-7.5 0 3.75 3.75 0 0 1 7.5 0Z"/></svg>`;
const MOON = `<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.5"
  aria-hidden="true"><path stroke-linecap="round" stroke-linejoin="round"
  d="M21.752 15.002A9.72 9.72 0 0 1 18 15.75c-5.385 0-9.75-4.365-9.75-9.75 0-1.33.266-2.597.748-3.752A9.753 9.753 0 0 0 3 11.25C3 16.635 7.365 21 12.75 21a9.753 9.753 0 0 0 9.002-5.998Z"/></svg>`;

export function paintThemeIcon() {
  const dark = currentTheme() === 'dark';
  document.querySelectorAll('.theme-icon').forEach(b => {
    // 显示的永远是「点下去会变成什么」，不是当前状态
    b.innerHTML = dark ? SUN : MOON;
    const label = dark ? '切换到亮色' : '切换到暗色';
    b.title = label;
    b.setAttribute('aria-label', label);
    b.setAttribute('aria-pressed', String(dark));
  });
}

export function initTheme() {
  // 默认白底金边；用户手动切过就按记住的来
  applyTheme(localStorage.getItem(THEME_KEY) || 'light');
  paintThemeIcon();
  document.addEventListener('themechange', paintThemeIcon);
  document.addEventListener('click', e => {
    // 单图标按钮：直接翻面。旧的双按钮胶囊（[data-theme-opt]）继续支持，
    // 各页面的标记可以分步迁移
    if (e.target.closest('.theme-icon')) {
      e.preventDefault();
      // 传按钮作为扩散原点，圆形揭示从按钮中心长出来
      applyTheme(currentTheme() === 'dark' ? 'light' : 'dark',
                 e.target.closest('.theme-icon'));
      return;
    }
    const opt = e.target.closest('[data-theme-opt]');
    if (!opt) return;
    e.preventDefault();
    if (opt.dataset.themeOpt === currentTheme()) return;
    applyTheme(opt.dataset.themeOpt, opt);
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

export { esc, fmtInt, fmtCompact, fmtMoney, niceMax, cssVar, fmtAxisMoney, fmtAxisNum };
