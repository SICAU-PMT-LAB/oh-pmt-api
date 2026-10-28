/* 公开数据页（/data）。

   版式、动效与 https://opencode.ai/data 对齐：等宽字体、hairline 分区、
   堆叠柱状图逐段染色、悬停时只有被指到的那一段保留颜色、其余变灰，
   明细浮层贴着柱子的左右侧出现。样式在 /assets/data.css（从对方构建产物
   里裁出本页用到的规则），这里只管数据与交互。

   数据全部来自 /api/public/data，页面自己不做统计口径换算：
   数字一旦在服务端算好，前端只负责显示，避免两边算出不同的数。
*/

import { fmtInt, fmtCompact, fmtMoney, esc, shortDate, stamp } from '/assets/site.js';
import { mountBackdrop } from '/assets/backdrop.js';

const $ = (sel) => document.querySelector(sel);
const fmtPct = (n, digits = 1) => (n === null || n === undefined || Number.isNaN(n)
  ? '—' : Number(n).toFixed(digits) + '%');

/* 分类色板。与 opencode 的柱状图用色一致：从品红到橙黄循环一遍。 */
const SERIES = [
  '#ed6aff', '#a684ff', '#7c86ff', '#51a2ff', '#00d3f2',
  '#00d5be', '#00bc7d', '#9ae600', '#ffb900', '#ff8904',
];

const MONTH = ['JAN', 'FEB', 'MAR', 'APR', 'MAY', 'JUN',
  'JUL', 'AUG', 'SEP', 'OCT', 'NOV', 'DEC'];

/* 轴上那种 SEP 2 的写法。宽屏与窄屏都用它，只是显示哪一份由 CSS 决定。 */
function axisDate(iso) {
  const [, m, d] = String(iso).split('-').map(Number);
  if (!m) return String(iso);
  return `${MONTH[m - 1]} ${d}`;
}

/* ---------- 主题 ---------- */

const THEME_KEY = 'pmt_site_theme';

function currentTheme() {
  const forced = document.documentElement.dataset.theme;
  if (forced === 'dark' || forced === 'light') return forced;
  return matchMedia('(prefers-color-scheme: dark)').matches ? 'dark' : 'light';
}

function setTheme(next, event) {
  const apply = () => {
    document.documentElement.dataset.theme = next;
    try { localStorage.setItem(THEME_KEY, next); } catch { /* 隐私模式 */ }
    paintThemeOptions();
  };
  const reduce = matchMedia('(prefers-reduced-motion: reduce)').matches;
  if (!document.startViewTransition || reduce) { apply(); return; }
  const x = event?.clientX ?? innerWidth - 60;
  const y = event?.clientY ?? 60;
  const r = Math.hypot(Math.max(x, innerWidth - x), Math.max(y, innerHeight - y));
  const vt = document.startViewTransition(apply);
  vt.ready.then(() => {
    document.documentElement.animate(
      { clipPath: [`circle(0px at ${x}px ${y}px)`, `circle(${r}px at ${x}px ${y}px)`] },
      { duration: 600, easing: 'ease-in-out', pseudoElement: '::view-transition-new(root)' },
    );
  }).catch(() => {});
}

/* 右上角那颗圆形按钮与主页一致：亮色显示月亮（点一下去暗），暗色显示太阳。 */
const MOON = `<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.5" aria-hidden="true"><path stroke-linecap="round" stroke-linejoin="round" d="M21.752 15.002A9.72 9.72 0 0 1 18 15.75c-5.385 0-9.75-4.365-9.75-9.75 0-1.33.266-2.597.748-3.752A9.753 9.753 0 0 0 3 11.25C3 16.635 7.365 21 12.75 21a9.753 9.753 0 0 0 9.002-5.998Z"/></svg>`;
const SUN = `<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.5" aria-hidden="true"><path stroke-linecap="round" stroke-linejoin="round" d="M12 3v2.25m6.364.386-1.591 1.591M21 12h-2.25m-.386 6.364-1.591-1.591M12 18.75V21m-4.773-4.227-1.591 1.591M5.25 12H3m4.227-4.773L5.636 5.636M15.75 12a3.75 3.75 0 1 1-7.5 0 3.75 3.75 0 0 1 7.5 0Z"/></svg>`;

function paintThemeOptions() {
  const dark = currentTheme() === 'dark';
  const toggle = $('#theme-toggle');
  if (!toggle) return;
  const label = dark ? '切换到亮色' : '切换到暗色';
  toggle.setAttribute('aria-label', label);
  toggle.title = label;
  toggle.setAttribute('aria-pressed', String(dark));
  toggle.innerHTML = dark ? SUN : MOON;
}

function initTheme() {
  $('#theme-toggle')?.addEventListener('click', (e) => {
    setTheme(currentTheme() === 'dark' ? 'light' : 'dark', e);
  });
  // 系统主题变了、用户又没手动选过时跟着走
  matchMedia('(prefers-color-scheme: dark)').addEventListener?.('change', () => {
    let saved = null;
    try { saved = localStorage.getItem(THEME_KEY); } catch { /* 同上 */ }
    if (saved === 'dark' || saved === 'light') return;
    document.documentElement.dataset.theme =
      matchMedia('(prefers-color-scheme: dark)').matches ? 'dark' : 'light';
    paintThemeOptions();
  });
  paintThemeOptions();
}

/* ---------- 顶栏 ---------- */

function initHeader() {
  const top = $('[data-component=top]');
  const menu = $('#stats-mobile-nav');
  const btn = $('[data-slot=menu-button]');
  // 展开状态挂在 header 上（data-menu-open），CSS 靠它把菜单从 display:none
  // 切出来。只加 hidden 属性不够：裁出来的规则里没有 [hidden] 兜底。
  const close = () => {
    btn.setAttribute('aria-expanded', 'false');
    menu.setAttribute('hidden', '');
    delete top.dataset.menuOpen;
  };
  btn?.addEventListener('click', () => {
    const open = btn.getAttribute('aria-expanded') !== 'true';
    btn.setAttribute('aria-expanded', String(open));
    if (open) { menu.removeAttribute('hidden'); top.dataset.menuOpen = 'true'; }
    else close();
  });
  // 点了某个分区就收起菜单，否则菜单盖住刚跳到的位置
  menu?.querySelectorAll('a').forEach((a) => a.addEventListener('click', close));
  matchMedia('(min-width: 90rem)').addEventListener?.('change', (e) => {
    if (e.matches) close();
  });
}

/* 顶部那一行「已更新」。opencode 的写法是每个字段一条两行的轨道，
   时间变了就把轨道从第一行滑到第二行（@keyframes stats-hero-meta-ticker）。 */
/* 两条 ticker 的文字。首屏先画 UTC 再翻到本地时间——上游的 SSR 产出
   是服务端时区，水合之后才换成访问者的时区，那一翻就是页面上唯一一次
   自动动画。照抄过来：不是装饰，是这页真实发生过的事。 */
function tickerTexts(ts, useUtc) {
  const d = new Date((Number(ts) || 0) * 1000);
  const mm = useUtc ? d.getUTCMonth() : d.getMonth();
  const dd = useUtc ? d.getUTCDate() : d.getDate();
  let time;
  if (useUtc) {
    time = `UTC ${String(d.getUTCHours()).padStart(2, '0')}:` +
      String(d.getUTCMinutes()).padStart(2, '0');
  } else {
    const off = -d.getTimezoneOffset() / 60;
    const sign = off >= 0 ? '+' : '−';
    time = `GMT${sign}${Math.abs(off)} ` +
      `${String(d.getHours()).padStart(2, '0')}:${String(d.getMinutes()).padStart(2, '0')}`;
  }
  return { date: `${mm + 1}月${dd}日`, time };
}

function paintTicker(ts, opts = {}) {
  const { date, time } = tickerTexts(ts, !!opts.utc);

  for (const [id, text] of [['#ticker-date', date], ['#ticker-time', time]]) {
    const track = $(id);                    // 轨道：里面装两行
    if (!track) continue;
    const ticker = track.closest('[data-slot=hero-meta-ticker]');
    const item = `<span data-slot="hero-meta-ticker-item">${esc(text)}</span>`;
    const prev = track.dataset.value;
    if (prev === text || !prev) {
      // 没有「上一行」可比时放两行同样的内容：轨道高度才是单行的两倍，
      // -50% 的位移才不会错位
      track.innerHTML = item + item;
      track.dataset.value = text;
      continue;
    }
    track.innerHTML = `<span data-slot="hero-meta-ticker-item">${esc(prev)}</span>` + item;
    track.dataset.value = text;
    if (!ticker) continue;
    ticker.dataset.ticking = 'false';
    void ticker.offsetWidth;        // 强制一帧，让同名动画重新触发
    ticker.dataset.ticking = 'true';
    track.addEventListener('animationend', () => {
      ticker.dataset.ticking = 'false';
      track.innerHTML = item + item;
    }, { once: true });
  }
}

/* 首屏：先用 UTC 填一次（无动画），下一帧翻成本地时间 */
function primeTicker(ts) {
  paintTicker(ts, { utc: true });
  requestAnimationFrame(() => {
    requestAnimationFrame(() => paintTicker(ts));
  });
}

/* ---------- 共享的「当前模型」状态 ----------

   上游把 activeModel 放在数据页最外层，同一个值同时传给热门模型图、
   独立用户图和排行榜卡片。所以悬停一张卡，上面两张图里那个模型的所有分段
   保持颜色、其余全部变灰；反方向同理。这里用同样的做法：一个订阅者集合，
   谁都能写，谁都能读。 */

const modelFocus = (() => {
  const listeners = new Set();
  let current;
  return {
    get: () => current,
    set(model) {
      const next = model || undefined;
      if (next === current) return;
      current = next;
      listeners.forEach((fn) => fn(current));
    },
    subscribe(fn) {
      listeners.add(fn);
      if (current) fn(current);
    },
  };
})();

/* ---------- 堆叠柱状图 ---------- */

/* 把一堆序列按天合成柱子的分段。累计值决定每段的相对高度；
   总计决定柱子相对全窗口峰值的绝对高度。 */
function stackRows(series, labels) {
  const total = labels.map((_, i) => series.reduce((sum, s) => sum + (s.values[i] || 0), 0));
  const peak = Math.max(1, ...total);
  return { total, peak };
}

/* 密集坐标轴：opencode 在宽屏每隔 7 天给一个刻度，窄屏每隔 14 天。
   规律是从最后一天往前数，保证最新的一天永远有标签。 */
function denseLabelIndexes(count, step) {
  const out = [];
  for (let i = count - 1; i >= 0; i -= step) out.unshift(i);
  return out;
}

/* 手机上的周聚合：按自然周（周一为界）把日序列折成周序列，
   每根柱子代表一周，60 天折成 ~9 根，不用横滑就能看全。
   返回 null 表示降级（无日期或聚合失败），调用方回退到日粒度。 */
function weeklyBuckets(labels, series) {
  if (!labels.length) return null;
  const parse = (iso) => {
    const [y, m, d] = String(iso).split('-').map(Number);
    if (!y || !m || !d) return null;
    return new Date(Date.UTC(y, m - 1, d));
  };
  const weekKey = (iso) => {
    const dt = parse(iso);
    if (!dt) return null;
    // 周一为一周的开始；周四所属的周即 ISO 周，用它反推周一的日期
    const day = (dt.getUTCDay() + 6) % 7;
    dt.setUTCDate(dt.getUTCDate() - day);
    return dt.toISOString().slice(0, 10);
  };
  const keys = labels.map(weekKey);
  if (keys.some((k) => !k)) return null;
  const weeks = [...new Set(keys)];
  // 不满 5 周的尾巴没有聚合价值（比如窗口只有几天），按日粒度画反而更准
  if (weeks.length < 5) return null;
  const values = series.map((s) =>
    weeks.map((_, wi) =>
      labels.reduce((sum, _, li) =>
        keys[li] === weeks[wi] ? sum + (s.values[li] || 0) : sum, 0)));
  return { labels: weeks, values };
}

function renderStackChart(host, spec) {
  const { labels, series, metric } = spec;
  // 窄屏（≤47.999rem）改周粒度：60 根日柱在 390px 屏上需要 1400px 横向滚动，
  // 折成周后只有 ~9 根，正好塞进屏内。悬停浮层照常工作，只是刻度变成周。
  const wide = matchMedia('(min-width: 47.999rem)').matches;
  let drawLabels = labels;
  let drawSeries = series;
  if (!wide) {
    const weekly = weeklyBuckets(labels, series);
    if (weekly) {
      drawLabels = weekly.labels;
      drawSeries = weekly.values.map((v, i) => ({ ...series[i], values: v }));
    }
  }
  const days = drawLabels.length;
  const peak = Math.max(1, ...stackRows(drawSeries, drawLabels).total);
  host.style.setProperty('--top-models-count', String(days));

  const step = wide ? 7 : 1;
  const shown = new Set(denseLabelIndexes(days, step));

  const axis = host.querySelector('[data-slot=top-models-axis]');
  axis.innerHTML = drawLabels.map((iso, i) => {
    const total = drawSeries.reduce((sum, s) => sum + (s.values[i] || 0), 0);
    const hidden = shown.has(i) ? '' : ' data-label-hidden="true"';
    return `<div${hidden}>
      <span data-slot="axis-label">
        <span data-slot="axis-total">${fmtCompact(total)}</span>
        <span data-slot="axis-date">
          <span data-slot="axis-date-full">${axisDate(iso)}</span>
          <span data-slot="axis-date-mobile">${axisDate(iso)}</span>
        </span>
      </span>
    </div>`;
  }).join('');

  // 柱子里的分段自下而上堆：最上面的那一段是占比最大的
  const bars = host.querySelector('[data-slot=top-models-bars]');
  bars.innerHTML = drawLabels.map((iso, i) => {
    const rows = drawSeries
      .map((s) => ({ name: s.name, value: s.values[i] || 0, color: s.color }))
      .filter((r) => r.value > 0);
    const dayTotal = rows.reduce((sum, r) => sum + r.value, 0);
    const height = (dayTotal / peak) * 100;
    // grid 的行序是视觉上的从上到下，所以要把累计序列反过来
    const tracks = rows.slice().reverse();
    const gridRows = tracks.map((r) => `${(r.value / dayTotal) * 100}%`).join(' ');
    const cells = tracks.map((r) =>
      `<i data-model="${esc(r.name)}" style="background:${r.color}"></i>`).join('');
    return `<div data-slot="top-models-bar" role="button" tabindex="0"
      aria-label="${axisDate(iso)} ${fmtCompact(dayTotal)} 总计"
      style="--top-models-bar-height:${height}%">
      <div data-slot="top-models-stack" style="grid-template-rows:${gridRows}">${cells}</div>
    </div>`;
  }).join('');

  bindStackHover(host, { labels: drawLabels, series: drawSeries, metric });
  writeStackTable($(`#${host.id}-table`), drawLabels, drawSeries, metric);
  // 宽屏不溢出，这句无副作用；窄屏周粒度下也不溢出，同样无副作用。
  // 保留它以防未来窗口变长又需要滚到最右端看最新数据。
  host.scrollLeft = host.scrollWidth;
}

/* 悬停一列：这一列的分段保持原色，同一天的其余柱子里同名分段也保持原色，
   其他分段变成 idle 灰。右侧浮层列出当天每段的数值，被指到的那段高亮。 */
function bindStackHover(host, { labels, series, metric }) {
  const bars = [...host.querySelectorAll('[data-slot=top-models-bar]')];
  const axis = [...host.querySelectorAll('[data-slot=top-models-axis] > div')];
  const IDLE = 'rgb(238, 238, 238)';
  const colors = new Map(series.map((s) => [s.name, s.color]));
  let tooltip = null;

  const place = (bar, index) => {
    tooltip?.remove();
    const total = series.reduce((sum, s) => sum + (s.values[index] || 0), 0);
    const rows = series
      .map((s) => ({ name: s.name, value: s.values[index] || 0 }))
      .filter((r) => r.value > 0)
      .sort((a, b) => b.value - a.value);
    const el = document.createElement('div');
    el.setAttribute('data-component', 'chart-tooltip');
    el.setAttribute('data-placement', index < labels.length / 2 ? 'right' : 'left');
    el.innerHTML = `<strong>${axisDate(labels[index])}</strong>
      <span>${fmtCompact(total)} 总计</span>
      <div data-slot="tooltip-divider"></div>` +
      rows.map((r) => `<p data-muted="true">
        <span data-slot="tooltip-label"><i style="background:${colors.get(r.name)}"></i><span data-slot="tooltip-name">${esc(r.name)}</span></span>
        <b>${fmtCompact(r.value)}</b>
      </p>`).join('');
    bar.appendChild(el);
    tooltip = el;
    return el;
  };

  const clear = ({ keepAnchor = false } = {}) => {
    tooltip?.remove();
    tooltip = null;
    if (!keepAnchor) { pinned = null; modelFocus.set(null); }
    bars.forEach((b) => {
      delete b.dataset.active;
      b.querySelectorAll('[data-slot=top-models-stack] i').forEach((seg) => {
        seg.style.background = colors.get(seg.dataset.model) || '';
      });
    });
    axis.forEach((d) => { delete d.dataset.active; });
  };

  /* 上色与标记分开：卡片能把某个模型点亮（index 为 null），但柱子自己的
     data-active（点阵加深、z-index）只跟指针落在哪一列有关。 */
  const paint = (index, model) => {
    const active = new Set();
    if (index !== null) series.forEach((s) => { if ((s.values[index] || 0) > 0) active.add(s.name); });
    for (const bar of bars) {
      const segs = [...bar.querySelectorAll('[data-slot=top-models-stack] i')];
      const onThis = bar === bars[index];
      for (const seg of segs) {
        const name = seg.dataset.model;
        // 被点到的那一段在所有列里都保留颜色，其余全部变灰
        const keep = model ? name === model : (onThis && active.has(name));
        seg.style.background = keep ? colors.get(name) : IDLE;
      }
    }
  };

  const mark = (index) => {
    bars.forEach((b, i) => {
      if (i === index) b.dataset.active = 'true'; else delete b.dataset.active;
    });
    axis.forEach((d, i) => {
      if (i === index) d.dataset.active = 'true'; else delete d.dataset.active;
    });
  };

  const highlight = (index, model) => {
    paint(index, model);
    mark(index);
  };

  /* pointerenter 的 target 是柱子本身，拿不到具体是哪一段；段的位置要靠
     pointermove 跟。所以这里先用 enter 建立浮层，再用 move 更新高亮的那一段，
     与 opencode 的行为一致：指到哪一段，浮层里对应的那一行就亮。 */
  // 本图当前点亮的模型；模型被清空时要把锚点摘掉
  let pinned = null;
  const mirror = (model) => {
    if (model) paint(null, model);
    else if (pinned !== null) { pinned = null; clear({ keepAnchor: true }); }
  };
  modelFocus.subscribe(mirror);

  const focusSegment = (bar, index, event) => {
    const seg = event.target.closest?.('i');
    const model = seg?.dataset.model || null;
    highlight(index, model);
    if (!tooltip) place(bar, index);
    tooltip.querySelectorAll('p').forEach((p) => {
      const on = model && p.querySelector('[data-slot=tooltip-name]')?.textContent === model;
      if (on) { p.dataset.active = 'true'; delete p.dataset.muted; }
      else { delete p.dataset.active; p.dataset.muted = 'true'; }
    });
    pinned = model;
    modelFocus.set(model);
    return model;
  };

  bars.forEach((bar, index) => {
    bar.addEventListener('pointerenter', (event) => { focusSegment(bar, index, event); });
    bar.addEventListener('pointermove', (event) => { focusSegment(bar, index, event); });
    bar.addEventListener('focus', () => {
      highlight(index, null);
      place(bar, index);
    });
    bar.addEventListener('pointerleave', () => clear());
    bar.addEventListener('blur', () => clear());
  });
  host.addEventListener('pointerleave', () => clear());
}

/* 图表的数据表兜底：屏幕阅读器和「复制数据」用 */
function writeStackTable(host, labels, series, metric) {
  if (!host) return;
  const head = `<tr><th scope="col">${labels.length && /^\d{4}-\d{2}-\d{2}$/.test(labels[0])
    && labels.length > 1 && labels[1].slice(0, 8) === labels[0].slice(0, 8) ? '周开始' : '日期'}</th>` +
    series.map((s) => `<th scope="col">${esc(s.name)}</th>`).join('') +
    `<th scope="col">合计</th></tr>`;
  const rows = labels.map((iso, i) => {
    const cells = series.map((s) => `<td>${fmtCompact(s.values[i] || 0)}</td>`).join('');
    const total = series.reduce((sum, s) => sum + (s.values[i] || 0), 0);
    return `<tr><th scope="row">${axisDate(iso)}</th>${cells}<td>${fmtCompact(total)}</td></tr>`;
  }).join('');
  host.innerHTML = `<table><caption>${metric === 'users' ? '按模型显示的独立用户' : '热门模型用量'}</caption>
    <thead>${head}</thead><tbody>${rows}</tbody></table>`;
}


/* ---------- 作者图标 ---------- */

/* 图标来自 /assets/model-icons.svg 的 symbol 表（opencode 的 sprite 里
   与本页相关的部分）。symbol 用 currentColor，所以颜色跟着所在元素走：
   排行榜卡片里是 hairline 灰，模型对比的方形头像里是白色。

   svg 一定要带 viewBox：外部 <use> 引用 symbol 时，尺寸由 svg 自身的
   viewBox 决定，缺了它会按 CSS 宽高硬拉伸，图标会糊掉。 */
function labIcon(lab, extra = '') {
  if (!lab || !lab.icon) {
    // 没有图标的作者用首字兜底，保证卡片上不会留空
    const ch = (lab?.label || '?').slice(0, 1);
    return `<svg viewBox="0 0 40 40" fill="none" aria-hidden="true" ${extra}>
      <text x="20" y="27" text-anchor="middle" font-size="22" fill="currentColor">${esc(ch)}</text></svg>`;
  }
  return `<svg viewBox="0 0 40 40" fill="none" aria-hidden="true" ${extra}>
    <use href="/assets/model-icons.svg#${lab.icon}"></use></svg>`;
}

/* ---------- 排行榜 ---------- */

function changeText(cell) {
  if (cell.is_new) return { text: '新', negative: false };
  if (cell.change_pct === null || cell.change_pct === undefined) return null;
  const pct = Math.round(cell.change_pct);
  return { text: `${pct >= 0 ? '+' : ''}${pct}%`, negative: pct < 0 };
}

function leaderCard(cell, index, size, period) {
  const tokens = period === 'daily' ? cell.tokens_day : cell.tokens_week;
  const change = period === 'daily' ? null : changeText(cell);
  const lab = { slug: cell.lab, label: cell.lab_label, icon: cell.lab_icon };
  return `<a data-component="leader-card" data-size="${size}" href="#top-models"
      data-model="${esc(cell.model)}" aria-label="${esc(cell.model)}，${fmtCompact(tokens)} token">
    <span data-slot="rank">${String(index + 1).padStart(2, '0')}</span>
    ${labIcon(lab, 'data-component="provider-icon" data-slot="leader-watermark"')}
    <div data-slot="leader-body">
      ${labIcon(lab, 'data-component="provider-icon" data-slot="leader-avatar"')}
      <div data-slot="leader-copy">
        <div><strong>${esc(cell.model)}</strong><span>${fmtCompact(tokens)}<span data-slot="visually-hidden"> token</span></span></div>
        <div><span>${esc(cell.lab_label)}</span>${change
          ? `<span data-slot="delta"${change.negative ? ' data-negative="true"' : ''}>${change.text}<span data-slot="visually-hidden"> 较上周</span></span>`
          : ''}</div>
      </div>
    </div>
  </a>`;
}

function renderLeaderboard(data, period) {
  // 每日窗口下按当天用量重排：60 天合计第一的模型当天不一定还在用
  const key = period === 'daily' ? 'tokens_day' : 'tokens_week';
  const rows = data.leaderboard
    .filter((c) => (c[key] || 0) > 0)
    .slice()
    .sort((a, b) => (b[key] || 0) - (a[key] || 0))
    .slice(0, 18);
  const featured = rows.slice(0, 3);
  const rest = rows.slice(3);
  $('[data-slot=leaderboard-featured]').innerHTML =
    featured.map((c, i) => leaderCard(c, i, 'featured', period)).join('');
  $('[data-slot=leaderboard-compact]').innerHTML =
    rest.map((c, i) => leaderCard(c, i + 3, 'compact', period)).join('');
  $('[data-slot=leaderboard-mobile]').innerHTML =
    rows.map((c, i) => leaderCard(c, i, 'featured', period)).join('');
  const empty = period === 'daily'
    ? '今天还没有新的调用。'
    : '最近 7 天没有调用记录。';
  if (!rows.length) {
    $('[data-slot=leaderboard-featured]').innerHTML = `<p class="empty-row">${empty}</p>`;
  }
  bindLeaderHover();
}

/* 卡片和图表共用 modelFocus：悬停一张卡，上方两张图里那个模型的所有分段
   保持颜色、其余变灰；反过来悬停图表里的某一段，对应卡片也会抬起来。
   桌面卡与移动卡是同一批模型的两份 DOM，所以按 data-model 一起点亮。 */
function bindLeaderHover() {
  const cards = [...document.querySelectorAll('[data-component=leader-card]')];
  if (!cards.length) return;

  const paint = (model) => {
    cards.forEach((card) => {
      if (model && card.dataset.model === model) card.dataset.active = 'true';
      else delete card.dataset.active;
    });
  };
  modelFocus.subscribe(paint);

  cards.forEach((card) => {
    card.addEventListener('pointerenter', () => modelFocus.set(card.dataset.model));
    card.addEventListener('pointerleave', () => modelFocus.set(null));
    card.addEventListener('focus', () => modelFocus.set(card.dataset.model));
    card.addEventListener('blur', () => modelFocus.set(null));
  });
}

/* 「每周 / 每日」两个按钮：只切排行榜的统计窗口（本周 7 天 / 最后一天），
   上面的堆叠图不动——这一点与 opencode 一致，图表的 2M 窗口是固定的。 */
function initPeriodFilter(data) {
  const buttons = [...document.querySelectorAll('[data-slot=leaderboard-period] [data-component=usage-filter] button')];
  const set = (period) => {
    buttons.forEach((b, i) => {
      const on = (i === 0) === (period === 'weekly');
      if (on) { b.dataset.active = 'true'; b.setAttribute('aria-pressed', 'true'); }
      else { delete b.dataset.active; b.setAttribute('aria-pressed', 'false'); }
    });
    renderLeaderboard(data, period);
  };
  buttons.forEach((b, i) => b.addEventListener('click', () => set(i === 0 ? 'weekly' : 'daily')));
  set('weekly');
}

/* ---------- 留存 ---------- */

function renderRetention(data) {
  const rows = data.retention.slice(0, 15);
  const body = $('#retention-body');
  if (!rows.length) {
    body.innerHTML = `<tr data-slot="retention-row" role="row"><th data-column="model" role="rowheader" scope="row">暂无数据</th>
      <td data-column="marker" role="cell"></td><td data-column="rate" role="cell">—</td>
      <td data-column="eligible" role="cell">—</td></tr>`;
    return;
  }
  body.innerHTML = rows.map((r, i) => `<tr data-slot="retention-row" role="row">
    <td data-column="rank" role="cell">${String(i + 1).padStart(2, '0')}</td>
    <th data-column="model" role="rowheader" scope="row">
      <a href="#top-models" aria-label="${esc(r.model)}，每周留存 ${fmtPct(r.rate)}，样本 ${fmtCompact(r.eligible)}">${esc(r.model)}</a>
    </th>
    <td data-column="marker" role="cell"><i data-component="retention-marker" style="--retention-position:${r.rate}%" aria-hidden="true"><span></span><span></span><span></span><span></span><em></em></i></td>
    <td data-column="rate" role="cell">${fmtPct(r.rate)}</td>
    <td data-column="eligible" role="cell">${fmtCompact(r.eligible)}</td>
  </tr>`).join('');
  body.querySelectorAll('[data-slot=retention-row]').forEach((row) => {
    const mark = row.querySelector('[data-component=retention-marker]');
    row.addEventListener('pointerenter', () => {
      row.dataset.active = 'true';
      if (mark) mark.dataset.active = 'true';
    });
    row.addEventListener('pointerleave', () => {
      delete row.dataset.active;
      if (mark) delete mark.dataset.active;
    });
  });
}

/* ---------- 成本两条（会话成本 / Token 成本） ---------- */

/* 一个可悬停的横条列表。浮层跟着指针所在的行移动，行内被指到的
   两个（或一个）进度条换成强调色，其余保持墨色。 */
function renderCostRows(host, { rows, variant }) {
  host.innerHTML = rows.map((r, i) => {
    const bars = r.bars.map((fill) =>
      `<i data-component="metric-bar" style="--metric-bar-fill:${fill}%"><b></b><em></em></i>`).join('');
    return `<button type="button" data-component="token-row"${variant === 'session' ? ' data-variant="session"' : ''}
      data-i="${i}"><strong>${esc(r.label)}</strong><span>${esc(r.name)}</span>${bars}</button>`;
  }).join('');

  const buttons = [...host.querySelectorAll('[data-component=token-row]')];
  const tooltip = document.createElement('div');
  tooltip.setAttribute('data-component', 'token-tooltip');
  if (variant === 'session') tooltip.setAttribute('data-variant', 'session');
  tooltip.innerHTML = '';           // 没指到行之前不显示内容
  tooltip.style.display = 'none';
  host.appendChild(tooltip);

  const paint = (index) => {
    buttons.forEach((b, i) => {
      const on = i === index;
      const row = rows[i];
      if (on) b.dataset.active = 'true'; else delete b.dataset.active;
      b.querySelectorAll('[data-component=metric-bar]').forEach((bar) => {
        if (on) bar.dataset.active = 'true'; else delete bar.dataset.active;
      });
      if (on && row) {
        tooltip.innerHTML = row.tip.map((p) =>
          `<p><span>${esc(p[0])}</span><strong>${esc(p[1])}</strong></p>`).join('');
        tooltip.style.display = '';
        // 浮层贴着这一行：行高 28 + 8 间距，与 opencode 的间距一致
        tooltip.style.top = `${index * 36 + 28}px`;
      }
    });
  };
  const clear = () => {
    buttons.forEach((b) => {
      delete b.dataset.active;
      b.querySelectorAll('[data-component=metric-bar]').forEach((bar) => delete bar.dataset.active);
    });
    tooltip.style.display = 'none';
    tooltip.innerHTML = '';
  };
  buttons.forEach((b, i) => {
    b.addEventListener('pointerenter', () => paint(i));
    b.addEventListener('focus', () => paint(i));
    b.addEventListener('pointerleave', clear);
    b.addEventListener('blur', clear);
  });
  host.addEventListener('pointerleave', clear);
  if (buttons.length) paint(0);
}

function renderSessionCosts(data) {
  const rows = data.session_costs
    .filter((r) => r.cost_per_session > 0)
    .slice()
    .sort((a, b) => a.cost_per_session - b.cost_per_session)
    .slice(0, 5);
  const host = $('#session-cost-body');
  if (!rows.length) { host.innerHTML = '<p class="empty-row">还没有足够的数据。</p>'; return; }
  const maxCost = Math.max(...rows.map((r) => r.cost_per_session), 1e-9);
  const maxTokens = Math.max(...rows.map((r) => r.tokens_per_session), 1);
  renderCostRows(host, {
    variant: 'session',
    rows: rows.map((r) => ({
      name: r.model,
      label: fmtMoney(r.cost_per_session),
      bars: [
        (r.cost_per_session / maxCost) * 100,
        (r.tokens_per_session / maxTokens) * 100,
      ],
      tip: [
        ['成本/会话', fmtMoney(r.cost_per_session)],
        ['Token/会话', fmtCompact(r.tokens_per_session)],
      ],
    })),
  });
  if (host.firstChild) {
    host.insertAdjacentHTML('afterbegin', `<div data-slot="session-heading" aria-hidden="true">
      <strong></strong><span></span><p>成本 / 会话</p><p>Token / 会话</p></div>`);
  }
}

function renderTokenCosts(data) {
  // 用实付均价（成本 ÷ token），不是牌价加权：命中缓存的那部分输入按缓存价
  // 结算，差十几倍，拿牌价排会得出与实际相反的结论。
  //
  // 请求数低于 MIN_REQUESTS 的剔除：单价是「实付总额 ÷ token」的比值，
  // 样本太少时一个特例就能把均价拉到任意值。按 token 量筛反而会错——
  // GLM-5.3-Flash 有 133 个请求但总共才 8 万 token（每次会话很短），
  // 用 token 门槛会把这种真实在用的模型滤掉。
  const MIN_REQUESTS = 20;
  const rows = data.leaderboard
    .filter((m) => m.effective_mtok > 0 && m.requests >= MIN_REQUESTS)
    .slice()
    .sort((a, b) => a.effective_mtok - b.effective_mtok)
    .slice(0, 7);
  const host = $('#token-cost-body');
  if (!rows.length) { host.innerHTML = '<p class="empty-row">还没有足够的数据。</p>'; return; }
  const max = Math.max(...rows.map((r) => r.effective_mtok), 1e-9);
  renderCostRows(host, {
    variant: 'token',
    rows: rows.map((r) => ({
      name: r.model,
      label: fmtMoney(r.effective_mtok),
      bars: [(r.effective_mtok / max) * 100],
      tip: [
        ['实付/百万', fmtMoney(r.effective_mtok)],
        ['输入', fmtMoney(r.price.input)],
        ['输出', fmtMoney(r.price.output)],
        ['缓存命中', fmtMoney(r.price.cache_read)],
      ],
    })),
  });
}

/* ---------- 缓存比例 ---------- */

function renderCache(data) {
  const rows = data.cache_rates.filter((r) => r.cached > 0).slice(0, 10);
  const host = $('#cache-ratio-body');
  if (!rows.length) { host.innerHTML = '<p class="empty-row">还没有缓存数据。</p>'; return; }
  host.innerHTML = `<div data-slot="cache-ratio-heading" aria-hidden="true">
      <strong>比例</strong><span>模型</span><b>0-100%</b></div>
    <div data-slot="cache-ratio-rows">${rows.map((r, i) => `
      <button type="button" data-component="cache-ratio-row" data-i="${i}" data-active="${i === 0 ? 'true' : 'false'}">
        <strong>${fmtPct(r.rate, 0)}</strong>
        <span>${esc(r.model)}</span>
        <i data-component="cache-ratio-marker" style="--cache-ratio-fill:${r.rate}%"><em></em></i>
      </button>`).join('')}</div>`;

  const buttons = [...host.querySelectorAll('[data-component=cache-ratio-row]')];
  const tooltip = document.createElement('div');
  tooltip.setAttribute('data-component', 'token-tooltip');
  tooltip.setAttribute('data-variant', 'cache-ratio');
  tooltip.style.display = 'none';
  host.appendChild(tooltip);

  const paint = (i) => {
    buttons.forEach((b, k) => {
      const on = k === i;
      b.dataset.active = on ? 'true' : 'false';
      const marker = b.querySelector('[data-component=cache-ratio-marker]');
      if (on) marker.dataset.active = 'true'; else delete marker.dataset.active;
    });
    const r = rows[i];
    tooltip.innerHTML = [
      ['缓存比例', fmtPct(r.rate, 0)],
      ['缓存命中', fmtCompact(r.cached)],
      ['未命中', fmtCompact(r.uncached)],
    ].map((p) => `<p><span>${p[0]}</span><strong>${p[1]}</strong></p>`).join('');
    tooltip.style.display = '';
    tooltip.style.top = `${i * 36 + 28}px`;
  };
  buttons.forEach((b, i) => {
    b.addEventListener('pointerenter', () => paint(i));
    b.addEventListener('focus', () => paint(i));
  });
  paint(0);
}

/* ---------- 模型对比 ---------- */

/* 四张对照卡，配对规则与 opencode 相同：榜首对次席、榜首对第三、
   相邻两名、榜首对最优替代。链接指向本页的排行榜，不做详情页。 */
function renderCompare(data) {
  const rows = data.leaderboard;
  const grid = $('#compare-grid');
  const pick = (i) => rows[i]?.model;
  if (rows.length < 2) {
    grid.innerHTML = '<p class="empty-row">模型数量不足，暂时无法配对。</p>';
    return;
  }
  const pairs = [
    ['使用量前二', pick(0), pick(1)],
    ['领先与挑战者', pick(0), pick(2) || pick(1)],
    ['相邻排名', pick(1), pick(2) || pick(3)],
    ['最佳替代', pick(0), pick(3) || pick(2)],
  ];
  const byModel = new Map(rows.map((r) => [r.model, r]));
  // 对比页的 URL 是一段一段的 slug：<lab>/<model>/<lab>/<model>。
  // 模型名里的空格、点、大写在 URL 里都不好读，这里统一转成 kebab。
  const slug = (text) => String(text || '').toLowerCase()
    .replace(/[^a-z0-9]+/g, '-').replace(/^-|-$/g, '');
  const href = (a, b) => {
    const la = byModel.get(a), lb = byModel.get(b);
    return `/data/compare/${slug(la?.lab)}/${slug(a)}/${slug(lb?.lab)}/${slug(b)}`;
  };
  const avatar = (cell) => `<span data-slot="compare-home-avatar" data-size="small"
      data-lab="${esc(cell?.lab || 'other')}" aria-label="${esc(cell?.lab_label || '其他')}">
      ${labIcon({ label: cell?.lab_label, icon: cell?.lab_icon }, 'data-component="provider-icon"')}</span>`;
  grid.innerHTML = pairs.map(([title, a, b]) => {
    const la = byModel.get(a), lb = byModel.get(b);
    return `<a data-component="compare-home-card" href="${href(a, b)}">
      <span data-slot="compare-home-card-head">
        <span>
          <strong>${esc(title)}</strong>
          <em>${esc(a)} vs ${esc(b)}</em>
        </span>
        <b aria-hidden="true"><svg width="16" height="16" viewBox="0 0 16 16" fill="none"><path d="M12.9509 12.9884L14.4069 14.4444M2.44431 2.44434H6.44431V6.44434H2.44431V2.44434ZM2.44431 9.55542H6.44431V13.5554H2.44431V9.55542ZM9.55539 2.44434H13.5554V6.44434H9.55539V2.44434ZM13.5554 11.5554C13.5554 12.66 12.66 13.5554 11.5554 13.5554C10.4508 13.5554 9.55539 12.66 9.55539 11.5554C9.55539 10.4509 10.4508 9.55542 11.5554 9.55542C12.66 9.55542 13.5554 10.4509 13.5554 11.5554Z" stroke="#808080"></path></svg></b>
      </span>
      <span data-slot="compare-home-card-divider" aria-hidden="true"></span>
      <span data-slot="compare-home-card-models"><span>${esc(a)}</span><i aria-hidden="true">·</i><span>${esc(b)}</span></span>
      <span data-slot="compare-home-card-avatars" aria-hidden="true">${avatar(la)}${avatar(lb)}</span>
    </a>`;
  }).join('');
}

/* ---------- 启动 ---------- */

async function boot() {
  mountBackdrop();
  initTheme();
  initHeader();
  $('#year').textContent = new Date().getFullYear();

  let data;
  try {
    data = await fetch('/api/public/data').then((r) => {
      if (!r.ok) throw new Error(`HTTP ${r.status}`);
      return r.json();
    });
  } catch (err) {
    document.querySelector('[data-slot=hero-copy]').textContent =
      `数据暂时不可用（${err.message}），请稍后重试。`;
    return;
  }

  primeTicker(data.generated_at);
  $('#method-window').textContent = `最近 ${data.days} 天`;
  // 摘要放在 visually-hidden 里：可见的那句是固定说明（与 opencode 一致），
  // 这行数字仍然写进 DOM，屏幕阅读器与搜索引擎能读到
  $('#hero-summary').textContent = summaryText(data);

  renderStackChart($('#chart-tokens'), {
    labels: data.labels,
    series: data.stack_models.series.map((s, i) => ({ ...s, color: SERIES[i % SERIES.length] })),
    metric: 'tokens',
  });
  renderStackChart($('#chart-users'), {
    labels: data.labels,
    series: data.stack_users.series.map((s, i) => ({ ...s, color: SERIES[i % SERIES.length] })),
    metric: 'users',
  });
  initPeriodFilter(data);
  renderRetention(data);
  renderSessionCosts(data);
  renderTokenCosts(data);
  renderCache(data);

  renderCompare(data);
}

/* 顶部那句概述：与 opencode 一样用「截至某日，谁第一、其次是……」 */
function summaryText(data) {
  const t = data.totals;
  const top = data.leaderboard[0];
  const d = new Date(data.generated_at * 1000);
  const when = `${d.getFullYear()} 年 ${d.getMonth() + 1} 月 ${d.getDate()} 日`;
  if (!top) return '站点尚未产生可统计的调用记录，接入成员后这里会自动填充。';
  const rest = data.leaderboard.slice(1, 3)
    .map((m) => `${m.model}（${fmtCompact(m.tokens)}）`);
  return `截至 ${when}，本站共处理 ${fmtInt(t.requests)} 次请求、${fmtCompact(t.tokens)} token，` +
    `${top.model} 以 ${fmtCompact(top.tokens)} token 居首` +
    (rest.length ? `，其次是 ${rest.join(' 和 ')}。` : '。');
}

boot();
