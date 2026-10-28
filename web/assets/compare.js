/* 模型对比详情页：/data/compare/<a>/<b>
 *
 * 数据来自 /api/public/compare?model=A&model=B。页面结构与 opencode 的
 * 对比页一致（面包屑 → 头像标题 → 选择条 → 能力雷达 → 指标矩阵），
 * 但雷达图的六个维度用的是本站真实存在的指标，没有编造的 benchmark。 */

import { fmtInt, fmtCompact, fmtMoney, esc, stamp } from '/assets/site.js';
import { mountBackdrop } from '/assets/backdrop.js';

const $ = (sel) => document.querySelector(sel);
const THEME_KEY = 'pmt_site_theme';

/* ---------- 主题（与 data.js 同一套写法） ---------- */

function currentTheme() {
  const t = document.documentElement.dataset.theme;
  if (t === 'dark' || t === 'light') return t;
  return matchMedia('(prefers-color-scheme: dark)').matches ? 'dark' : 'light';
}

function setTheme(next, origin) {
  const apply = () => {
    document.documentElement.dataset.theme = next;
    try { localStorage.setItem(THEME_KEY, next); } catch (e) {}
  };
  const reduce = matchMedia('(prefers-reduced-motion: reduce)').matches;
  if (!document.startViewTransition || reduce || !origin) { apply(); return; }
  const x = origin.clientX ?? innerWidth - 60;
  const y = origin.clientY ?? 40;
  const r = Math.hypot(Math.max(x, innerWidth - x), Math.max(y, innerHeight - y));
  const vt = document.startViewTransition(apply);
  vt.ready.then(() => {
    document.documentElement.animate(
      { clipPath: [`circle(0px at ${x}px ${y}px)`, `circle(${r}px at ${x}px ${y}px)`] },
      { duration: 600, easing: 'ease-out', pseudoElement: '::view-transition-new(root)' },
    );
  }).catch(() => {});
}

const ICON_MOON = `<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.5" aria-hidden="true"><path stroke-linecap="round" stroke-linejoin="round" d="M21.752 15.002A9.72 9.72 0 0 1 18 15.75c-5.385 0-9.75-4.365-9.75-9.75 0-1.33.266-2.597.748-3.752A9.753 9.753 0 0 0 3 11.25C3 16.635 7.365 21 12.75 21a9.753 9.753 0 0 0 9.002-5.998Z"/></svg>`;
const ICON_SUN = `<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.5" aria-hidden="true"><path stroke-linecap="round" stroke-linejoin="round" d="M12 3v2.25m6.364.386-1.591 1.591M21 12h-2.25m-.386 6.364-1.591-1.591M12 18.75V21m-4.773-4.227-1.591 1.591M5.25 12H3m4.227-4.773L5.636 5.636M15.75 12a3.75 3.75 0 1 1-7.5 0 3.75 3.75 0 0 1 7.5 0Z"/></svg>`;

/* 与数据页、主页同一颗圆形按钮：亮色显示月亮，暗色显示太阳。 */
function paintTheme() {
  const dark = currentTheme() === 'dark';
  const btn = $('#theme-toggle');
  if (!btn) return;
  const label = dark ? '切换到亮色' : '切换到暗色';
  btn.innerHTML = dark ? ICON_SUN : ICON_MOON;
  btn.title = label;
  btn.setAttribute('aria-label', label);
  btn.setAttribute('aria-pressed', String(dark));
}

function initTheme() {
  paintTheme();
  $('#theme-toggle')?.addEventListener('click', (e) => {
    setTheme(currentTheme() === 'dark' ? 'light' : 'dark', e);
  });
  matchMedia('(prefers-color-scheme: dark)').addEventListener('change', () => {
    let stored = null;
    try { stored = localStorage.getItem(THEME_KEY); } catch (e) {}
    if (stored !== 'dark' && stored !== 'light') { setTheme(currentTheme(), null); paintTheme(); }
  });
}

/* ---------- 顶栏 ---------- */

function initHeader() {
  const top = $('[data-component=top]');
  const btn = $('[data-slot=menu-button]');
  const nav = $('#stats-mobile-nav');
  const close = () => {
    btn?.setAttribute('aria-expanded', 'false');
    nav?.setAttribute('hidden', '');
    if (top) delete top.dataset.menuOpen;
  };
  btn?.addEventListener('click', () => {
    const open = btn.getAttribute('aria-expanded') !== 'true';
    btn.setAttribute('aria-expanded', String(open));
    if (open) {
      nav?.removeAttribute('hidden');
      if (top) top.dataset.menuOpen = 'true';
    } else {
      close();
    }
  });
  nav?.querySelectorAll('a').forEach((a) => a.addEventListener('click', close));
  $('#year').textContent = String(new Date().getFullYear());
}

/* ---------- 头像 ---------- */

function labIcon(lab, extra = '') {
  const view = extra ? ` ${extra}` : '';
  if (lab.icon) {
    return `<svg viewBox="0 0 40 40" fill="none" aria-hidden="true"${view}>` +
      `<use href="/assets/model-icons.svg#${lab.icon}"></use></svg>`;
  }
  // 没有图标就退回首字，别渲染成一个空方块
  return `<svg viewBox="0 0 40 40" fill="none" aria-hidden="true"${view}>` +
    `<text x="20" y="27" text-anchor="middle" font-size="22" fill="currentColor">${esc((lab.label || '?').slice(0, 1))}</text></svg>`;
}

function avatar(model, size) {
  return `<span data-slot="compare-home-avatar" data-lab="${esc(model.lab)}" data-size="${size}" aria-label="${esc(model.lab_label)}">` +
    labIcon({ label: model.lab_label, icon: model.lab_icon }) + '</span>';
}

/* ---------- 标题与选择条 ---------- */

function renderHero(models) {
  const names = models.map((m) => m.model);
  $('#compare-title').setAttribute('aria-label', `对比 ${names.join(' 与 ')}`);
  $('#compare-title').innerHTML = '<span>对比</span>' +
    `<span data-slot="compare-home-avatar-stack" aria-hidden="true">` +
    models.map((m) => `<span data-slot="compare-home-avatar-frame">${avatar(m, 'large')}</span>`).join('') +
    '</span>' +
    `<span data-slot="visually-hidden">${esc(names.join(' 与 '))}</span>`;

  $('#compare-selector').innerHTML =
    '<div data-slot="compare-detail-selector-spacer" aria-hidden="true"></div>' +
    models.map((m, i) => `
      <button type="button" data-slot="compare-detail-select-model" data-column="${i}"
              ${i === models.length - 1 ? 'data-last="true"' : ''}
              aria-label="模型 ${i + 1}：${esc(m.model)}">
        ${avatar(m, 'small')}
        <span data-slot="compare-detail-select-name">${esc(m.model)}</span>
      </button>`).join('');

  $('#compare-legend').innerHTML = models.map((m, i) => `
    <li><i style="background:${SERIES[i]}" aria-hidden="true"></i>
      <span><strong>${esc(m.model)}</strong><small>${esc(m.lab_label)}</small></span></li>`).join('');
}

/* 两条序列的固定配色：与上游对比页一致地「先蓝后绿」 */
const SERIES = ['#294bdb', '#159447', '#c2410c', '#6d28d9'];

/* ---------- 雷达图 ----------
 * 六轴用的是本站真有的六件事，不是 benchmark 分数：用量份额、单价、
 * 缓存命中、留存、成功率、用量规模。 */

/* 把一组正数压到 0.1~0.9 之间，以参评模型的几何均值为中点：比值 1 落 0.5，
   差 10 倍分到 0.69/0.31，差 100 倍分到 0.80/0.20，差 5000 倍也只到
   0.89/0.11。

   原来每根轴都是「除以这一对里的最大值」，领先的那个永远顶满一圈、落后的
   永远贴住圆心，图上只剩两种形态：全填满或者完全没填。而且相差 5000 倍的
   份额与相差 1.3 倍的单价在一张图上用同一套线性刻度，谁也看不出差多少。

   系数 0.4 把半径限在 0.1~0.9：再大的差距也不会让多边形碰到外环或塌到圆心，
   两条曲线始终能同时看清。tanh 保证对数比值再大也不会溢出。 */
function spread(value, reference) {
  if (!(value > 0) || !(reference > 0)) return 0.5;
  return 0.5 + 0.4 * Math.tanh(0.5 * Math.log10(value / reference));
}

/* 每根轴给出一个正数原值（好 = 大），坐标与基准都从这里算。
   百分比类的加 1 个百分点：0% 与 96% 之间也得算得出比值，
   而 log(0) 是负无穷。 */
const AXES = [
  { key: 'share', label: '用量份额', hint: '在本站全部 token 里占的份额。',
    raw: (m) => m.share + 0.001,
    text: (m) => `${m.share.toFixed(2)}%` },
  { key: 'cost', label: '价格优势', hint: '每百万 token 的实付均价，越便宜越高。',
    raw: (m) => 1 / Math.max(m.effective_mtok || 0, 0.0001),
    text: (m) => (m.effective_mtok == null ? '—' : `${fmtMoney(m.effective_mtok)}/M`) },
  { key: 'cache', label: '缓存命中', hint: '命中缓存的输入占全部输入的比例。',
    raw: (m) => (m.cache_rate || 0) + 1,
    text: (m) => (m.cache_rate == null ? '—' : `${m.cache_rate.toFixed(1)}%`) },
  { key: 'retention', label: '留存', hint: '用过该模型的成员里，下一周还用它的比例。',
    raw: (m) => (m.retention || 0) + 1,
    text: (m) => (m.retention == null ? '—' : `${m.retention.toFixed(0)}%`) },
  { key: 'success', label: '成功率', hint: '请求里返回 200 的比例。',
    raw: (m) => (m.success_rate || 0) + 1,
    text: (m) => (m.success_rate == null ? '—' : `${m.success_rate.toFixed(1)}%`) },
  { key: 'volume', label: '用量规模', hint: '窗口内的 token 总量，越活跃越大。',
    raw: (m) => m.tokens + 1,
    text: (m) => fmtCompact(m.tokens) },
];

function renderRadar(models) {
  const n = AXES.length;
  // 每根轴的基准取参评模型的几何均值：两两对拍时就是 √(a×b)。
  const refs = AXES.map((ax) => {
    const logs = models.map((m) => Math.log10(ax.raw(m))).filter((v) => Number.isFinite(v));
    return logs.length ? 10 ** (logs.reduce((s, v) => s + v, 0) / logs.length) : 1;
  });
  const score = (m, i) => Math.max(0.08, Math.min(0.92, spread(AXES[i].raw(m), refs[i])));
  const pt = (i, r) => {
    const a = (Math.PI * 2 * i) / n - Math.PI / 2;
    return [50 + Math.cos(a) * r * 50, 50 + Math.sin(a) * r * 50];
  };
  const ring = (r) => Array.from({ length: n }, (_, i) =>
    pt(i, r).map((v) => v.toFixed(3)).join(',')).join(' ');

  // 网格圈跟数据同一个带宽：顶点只在 0.08~0.92 之间，圈画到 1 的话最外那圈
  // 永远是空的。0.5 那一圈是「与对方相当」，相当于一条分水岭。
  const RINGS = [0.1, 0.3, 0.5, 0.7, 0.9];
  const grid = `<g data-slot="compare-radar-grid">` +
    RINGS.map((r) => `<polygon points="${ring(r)}"></polygon>`).join('') +
    Array.from({ length: n }, (_, i) => {
      const [x, y] = pt(i, RINGS[RINGS.length - 1]);
      return `<line x1="50" y1="50" x2="${x.toFixed(3)}" y2="${y.toFixed(3)}"></line>`;
    }).join('') + '</g>';

  const series = models.map((m, mi) => {
    const pts = AXES.map((ax, i) => pt(i, score(m, i)));
    return `<g data-slot="compare-radar-series" style="color:${SERIES[mi]}">` +
      `<polygon data-slot="compare-radar-area" points="${pts.map((p) => p.map((v) => v.toFixed(3)).join(',')).join(' ')}"></polygon>` +
      pts.map(([x, y], i) => {
        const label = `${AXES[i].label} ${AXES[i].text(m)}`;
        return `<circle data-slot="compare-radar-point" cx="${x.toFixed(3)}" cy="${y.toFixed(3)}" r="0.95"></circle>` +
          `<circle data-slot="compare-radar-point-hit" cx="${x.toFixed(3)}" cy="${y.toFixed(3)}" r="3" ` +
          `data-model="${esc(m.model)}" data-axis="${i}" aria-label="${esc(m.model)} ${esc(label)}"></circle>`;
      }).join('') + '</g>';
  }).join('');

  $('#compare-radar-svg').innerHTML = grid + series;

  // 轴的文字标签：位置按角度摆放，与上游一样用 --compare-radar-axis-* 定位
  const host = $('#compare-radar');
  host.querySelectorAll('[data-slot=compare-radar-axis]').forEach((el) => el.remove());
  AXES.forEach((ax, i) => {
    // 与上游一致：轴标签按 12% / 29% / 50% / 71% / 88% 落在圆周外，
    // 六个轴一一对应（12 点 → 2 点 → 4 点 → 6 点 → 8 点 → 10 点）。
    const AXIS_POS = [
      { x: 50, y: 8 }, { x: 86.373, y: 29 }, { x: 86.373, y: 71 },
      { x: 50, y: 92 }, { x: 13.627, y: 71 }, { x: 13.627, y: 29 },
    ];
    const MOBILE_POS = [
      { x: 50, y: 8 }, { x: 81.177, y: 29 }, { x: 81.177, y: 71 },
      { x: 50, y: 92 }, { x: 18.823, y: 71 }, { x: 18.823, y: 29 },
    ];
    const pos = AXIS_POS[i % AXIS_POS.length];
    const mobile = MOBILE_POS[i % MOBILE_POS.length];
    const x = pos.x;
    const y = pos.y;
    const el = document.createElement('span');
    el.setAttribute('data-slot', 'compare-radar-axis');
    el.setAttribute('tabindex', '0');
    el.setAttribute('aria-label', `${ax.label}。${ax.hint}`);
    el.style.cssText =
      `--compare-radar-axis-x:${x}%;--compare-radar-axis-mobile-x:${mobile.x}%;` +
      `--compare-radar-axis-y:${y}%;` +
      `--compare-radar-axis-translate-x:${Math.abs(x - 50) < 2 ? '-50%' : (x < 50 ? '-100%' : '0')}`;
    el.innerHTML = `<span data-slot="compare-radar-axis-label">${esc(ax.label)}</span>`;
    host.appendChild(el);

    // 指到轴上时把它点亮，并把两个模型在该维度的分数列成一张小表
    const show = () => {
      el.dataset.active = 'true';
      let tip = host.querySelector('[data-slot=compare-radar-tooltip]');
      if (!tip) {
        tip = document.createElement('div');
        tip.setAttribute('data-slot', 'compare-radar-tooltip');
        tip.style.cssText =
          `--compare-radar-tooltip-x:${x.toFixed(2)}%;` +
          `--compare-radar-tooltip-y:${Math.max(6, y).toFixed(2)}%;` +
          `--compare-radar-tooltip-translate-y:${y > 50 ? '12px' : 'calc(-100% - 12px)'}`;
        host.appendChild(tip);
      }
      tip.innerHTML = `<strong>${esc(ax.label)}</strong>` +
        models.map((m, mi) => `<p><i style="display:inline-block;width:6px;height:6px;margin-right:6px;background:${SERIES[mi]}"></i>` +
          `${esc(m.model)} ${esc(ax.text(m))}</p>`).join('');
    };
    const hide = () => {
      delete el.dataset.active;
      host.querySelector('[data-slot=compare-radar-tooltip]')?.remove();
    };
    el.addEventListener('pointerenter', show);
    el.addEventListener('focus', show);
    el.addEventListener('pointerleave', hide);
    el.addEventListener('blur', hide);
  });
}

/* ---------- 指标矩阵 ---------- */

function rowsFor(models) {
  const price = (m, key) => {
    const v = (m.price || {})[key];
    return v == null ? '—' : `${fmtMoney(v)}<span data-slot="compare-detail-unit">/ 1M</span>`;
  };
  const best = (vals) => {
    // 找出这一行里「更好」的那一列。数字越小越好，缺值的不参与
    const nums = vals.map((v) => (typeof v === 'number' && Number.isFinite(v)) ? v : null);
    const valid = nums.filter((v) => v !== null);
    if (valid.length < 2) return -1;
    const min = Math.min(...valid);
    return nums.indexOf(min);
  };
  const trend = (m) => {
    if (m.change_pct == null) return '';
    const up = m.change_pct >= 0;
    return `<span data-slot="compare-detail-trend" data-trend="${up ? 'up' : 'down'}">` +
      `${up ? '+' : ''}${m.change_pct.toFixed(0)}%</span>`;
  };
  const bars = (m) => {
    const daily = m.daily || [];
    const max = Math.max(...daily, 1);
    return `<span data-slot="compare-detail-bars">` +
      daily.map((v) => `<i style="height:${Math.max(2, (v / max) * 40).toFixed(1)}px"></i>`).join('') +
      '</span>';
  };

  const groups = [
    { name: '概览', rows: [
      ['作者', (m) => esc(m.lab_label)],
      ['用量份额', (m) => `${m.share.toFixed(2)}%`],
      ['Token 总数', (m) => fmtCompact(m.tokens)],
      ['请求数', (m) => fmtInt(m.requests)],
      ['独立成员', (m) => fmtInt(m.users)],
      ['会话数', (m) => fmtInt(m.sessions)],
    ]},
    { name: '价格', best: true, rows: [
      ['实付均价', (m) => (m.effective_mtok == null
        ? '—' : `${fmtMoney(m.effective_mtok)}<span data-slot="compare-detail-unit">/ 1M</span>`),
        (m) => m.effective_mtok],
      ['输入', (m) => price(m, 'input'), (m) => (m.price || {}).input],
      ['输出', (m) => price(m, 'output'), (m) => (m.price || {}).output],
      ['命中缓存', (m) => price(m, 'cache_read'), (m) => (m.price || {}).cache_read],
      ['牌价加权', (m) => fmtMoney(m.cost_per_mtok), (m) => m.cost_per_mtok],
    ]},
    { name: '走势', rows: [
      ['近 7 天', (m) => `${fmtCompact(m.tokens_week)} ${trend(m)}`],
      ['最后一天', (m) => fmtCompact(m.tokens_day)],
      ['每日用量', (m) => `<span data-slot="compare-detail-value" data-chart="true" style="padding:0">${bars(m)}</span>`],
    ]},
    { name: '健康度', rows: [
      ['缓存比例', (m) => (m.cache_rate == null ? '—' : `${m.cache_rate.toFixed(1)}%`)],
      ['成功率', (m) => (m.success_rate == null ? '—' : `${m.success_rate.toFixed(1)}%`)],
      ['留存率', (m) => (m.retention == null ? '—' : `${m.retention.toFixed(1)}%`)],
      ['可留存人数', (m) => fmtInt(m.retention_eligible)],
    ]},
  ];
  return { groups, best };
}

function renderMatrix(models) {
  const { groups, best } = rowsFor(models);
  const last = models.length - 1;
  const spacer = `<div data-slot="compare-detail-label" data-spacer="true" aria-hidden="true"></div>` +
    models.map((_, i) => `<div data-slot="compare-detail-value" data-column="${i}"${i === last ? ' data-last="true"' : ''} data-spacer="true" aria-hidden="true"></div>`).join('');

  $('#compare-matrix').innerHTML = groups.map((group) => {
    const head = `<div data-slot="compare-detail-label" data-heading="true"><strong>${esc(group.name)}</strong></div>` +
      models.map((_, i) => `<div data-slot="compare-detail-value" data-column="${i}"${i === last ? ' data-last="true"' : ''} data-heading="true" aria-hidden="true"></div>`).join('');
    const body = group.rows.map(([label, render, sortKey]) => {
      const bestIdx = group.best && sortKey ? best(models.map(sortKey)) : -1;
      const cells = models.map((m, i) => {
        const cell = render(m);
        // 「更好」那一列只给底色，不给文字，避免把结论说得太满
        const attr = i === bestIdx ? ' data-best="true"' : '';
        return `<div data-slot="compare-detail-value" data-column="${i}"${i === last ? ' data-last="true"' : ''}${attr}>` +
          `<span data-slot="compare-detail-value-main">${cell}</span></div>`;
      }).join('');
      return `<div data-slot="compare-detail-label">${esc(label)}</div>${cells}`;
    }).join('');
    return `<section data-slot="compare-detail-group" aria-label="${esc(group.name)}">${spacer}${head}${body}</section>`;
  }).join('');

  // 表头与数据是两个独立的滚动容器（窄屏下各 420px，内容 680px）。
  // 不绑定的话，横向滚动数据列时表头不动，模型名与列就对不上。
  const heading = document.querySelector('[data-component=compare-detail-heading-scroll]');
  const body = document.querySelector('[data-component=compare-detail-body-scroll]');
  if (heading && body && !heading.dataset.synced) {
    heading.dataset.synced = 'true';
    heading.addEventListener('scroll', () => { body.scrollLeft = heading.scrollLeft; });
    body.addEventListener('scroll', () => { heading.scrollLeft = body.scrollLeft; });
  }
}

/* ---------- 取数与装配 ---------- */

function slugSpace(s) {
  return String(s || '').toLowerCase().replace(/[^a-z0-9]/g, '');
}

function wantedNames() {
  // /data/compare/deepseek/deepseek-v4-1-flash/zhipu/glm-5-3-flash
  // 上游是 <lab>/<model>/<lab>/<model> 四段，本站只关心其中的模型段。
  const parts = location.pathname.replace(/\/+$/, '').split('/').filter(Boolean);
  const rest = parts.slice(2);                      // 去掉 "data" "compare"
  const models = [];
  for (let i = 0; i < rest.length; i++) {
    // 形如 <lab>/<model> 时取第二段；单独一段也接受
    if (i + 1 < rest.length) { models.push(rest[i + 1]); i += 1; }
    else models.push(rest[i]);
  }
  return models.length >= 2 ? models.slice(0, 2) : rest.slice(-2);
}

async function load() {
  const names = wantedNames();
  const url = `/api/public/compare?model=${encodeURIComponent(names[0] || '')}` +
    `&model=${encodeURIComponent(names[1] || '')}`;
  const res = await fetch(url, { headers: { accept: 'application/json' } });
  const data = await res.json();
  if (data.error) throw new Error(data.error);
  return data;
}

async function main() {
  mountBackdrop();
  initTheme();
  initHeader();
  try {
    const data = await load();
    const models = data.models || [];
    if (models.length < 2) throw new Error('需要两个模型才能对比');
    renderHero(models);
    renderMatrix(models);
    renderRadar(models);
    document.title = `${models.map((m) => m.model).join(' vs ')} | 模型对比`;
    const stampEl = document.querySelector('[data-slot=compare-detail-actions]');
    if (stampEl) stampEl.insertAdjacentHTML('afterbegin',
      `<span data-slot="compare-detail-stamp">${esc(stamp(data.generated_at))}</span>`);
  } catch (err) {
    const box = $('#compare-error');
    if (box) {
      box.hidden = false;
      box.textContent = `加载失败：${err.message}`;
    }
    $('#compare-body')?.setAttribute('hidden', '');
  }
}

main();
