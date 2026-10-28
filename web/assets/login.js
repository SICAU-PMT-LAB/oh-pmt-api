/* 登录页的三段式入场：闸机 → 吊牌 → 翻转登录卡。

   状态机：
     gate    液态玻璃入口面板，还没做出选择
     badge   吊牌从天而降，可以拖着玩，点一下唤起面板
     panel   登录卡翻转+希区柯克变焦入场
     exit    登录成功后吊牌拉起、面板翻回，再放行跳转

   吊牌绳用 Verlet 积分模拟。12 段定长约束，18 次迭代收敛，
   落体和甩动看起来才像一根真绳子而不是弹簧。
*/

import { initGridScan } from '/assets/gridscan.js';

/* ---------------- 主题 ---------------- */

const THEME_KEY = 'pmt_login_theme';
function currentTheme() {
  return document.documentElement.dataset.theme || 'light';
}
function applyTheme(theme) {
  const root = document.documentElement;
  root.dataset.theme = theme;
  root.style.background = theme === 'dark' ? '#111110' : '#fbfaf7';
  localStorage.setItem(THEME_KEY, theme);
  document.getElementById('theme-toggle')?.setAttribute('aria-pressed', theme === 'dark');
  paintThemeIcon(theme);
  gridApi?.setTheme(theme === 'dark');
}
/* 切换主题。与站点页（site.js 的 setTheme）同一套动效：新配色从点击位置
   做圆形揭示铺满全屏。改 data-theme 必须留在 startViewTransition 的回调里，
   否则旧快照拍到的已经是新配色，扩散就看不见了。 */
function setTheme(theme, event) {
  const reduce = window.matchMedia?.('(prefers-reduced-motion: reduce)').matches;
  if (!document.startViewTransition || reduce) {
    applyTheme(theme);
    return;
  }
  const x = event?.clientX ?? window.innerWidth - 60;
  const y = event?.clientY ?? 60;
  // 点击点到视口最远角的距离，保证圆能盖满全屏
  const r = Math.hypot(
    Math.max(x, window.innerWidth - x),
    Math.max(y, window.innerHeight - y),
  );
  const vt = document.startViewTransition(() => applyTheme(theme));
  vt.ready.then(() => {
    document.documentElement.animate(
      { clipPath: [`circle(0px at ${x}px ${y}px)`, `circle(${r}px at ${x}px ${y}px)`] },
      { duration: 600, easing: 'ease-in-out',
        pseudoElement: '::view-transition-new(root)' },
    );
  }).catch(() => {});
}
function paintThemeIcon(theme) {
  const btn = document.getElementById('theme-toggle');
  if (!btn) return;
  btn.innerHTML = theme === 'dark'
    ? `<svg width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="currentColor"
         stroke-width="1.5" aria-hidden="true"><path stroke-linecap="round" stroke-linejoin="round"
         d="M12 3v2.25m6.364.386-1.591 1.591M21 12h-2.25m-.386 6.364-1.591-1.591M12 18.75V21m-4.773-4.227-1.591 1.591M5.25 12H3m4.227-4.773L5.636 5.636M15.75 12a3.75 3.75 0 1 1-7.5 0 3.75 3.75 0 0 1 7.5 0Z"/></svg>`
    : `<svg width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="currentColor"
         stroke-width="1.5" aria-hidden="true"><path stroke-linecap="round" stroke-linejoin="round"
         d="M21.752 15.002A9.72 9.72 0 0 1 18 15.75c-5.385 0-9.75-4.365-9.75-9.75 0-1.33.266-2.597.748-3.752A9.753 9.753 0 0 0 3 11.25C3 16.635 7.365 21 12.75 21a9.753 9.753 0 0 0 9.002-5.998Z"/></svg>`;
  btn.title = theme === 'dark' ? '切换到亮色' : '切换到暗色';
}

/* ---------------- 星空背板 ---------------- */

const GOLDEN_ANGLE = Math.PI * (3 - Math.sqrt(5));
function paintCosmos() {
  const svg = document.getElementById('cosmos');
  if (!svg) return;
  const STAR_COUNT = 144;
  const dust = [];
  for (let i = 0; i < STAR_COUNT; i += 1) {
    const t = (i + 0.5) / STAR_COUNT;
    const rNorm = Math.sqrt(t);
    const theta = i * GOLDEN_ANGLE;
    // 确定性扰动：刷新后星图不变
    const jx = ((Math.sin(i * 12.9898 + 78.233) * 43758.5453) % 1 + 1) % 1 - 0.5;
    const jy = ((Math.sin(i * 45.164 + 19.13) * 28461.321) % 1 + 1) % 1 - 0.5;
    const magSeed = ((Math.sin(i * 93.712 + 51.47) * 19283.741) % 1 + 1) % 1;
    const x = Math.round((((0.5 + 0.68 * rNorm * Math.cos(theta) + jx * 0.06) % 1 + 1) % 1) * 1440);
    const y = Math.round((((0.5 + 0.68 * rNorm * Math.sin(theta) + jy * 0.06) % 1 + 1) % 1) * 900);
    const powerMag = Math.pow(magSeed, 2.2);
    dust.push({
      x, y,
      r: +(0.65 + powerMag * 1.65).toFixed(2),
      o: +(0.28 + powerMag * 0.65).toFixed(2),
      delay: `${((i % 19) * 0.31).toFixed(2)}s`,
      dur: `${(2.8 + (i % 8) * 0.55).toFixed(2)}s`,
      twinkle: i % 3 !== 0,
    });
  }
  const featured = [
    [185, 135, 2.2, '0s'], [335, 142, 1.9, '1.2s'], [1115, 178, 2.4, '0.6s'],
    [1275, 205, 2.0, '2.1s'], [265, 685, 2.1, '1.7s'], [1148, 632, 2.3, '0.9s'],
    [485, 215, 1.9, '2.8s'], [945, 255, 2.1, '1.4s'], [840, 710, 1.9, '2.4s'],
    [640, 145, 1.8, '0.4s'], [420, 745, 2.0, '1.9s'],
  ];

  svg.innerHTML = `
    <defs>
      <radialGradient id="star-glow-lg" cx="50%" cy="50%" r="50%">
        <stop offset="0%" stop-color="currentColor" stop-opacity=".95"/>
        <stop offset="35%" stop-color="currentColor" stop-opacity=".35"/>
        <stop offset="100%" stop-color="currentColor" stop-opacity="0"/>
      </radialGradient>
    </defs>
    <g class="cosmos-orbits" fill="none" stroke="currentColor">
      <ellipse cx="720" cy="450" rx="560" ry="260" stroke-width=".6" stroke-dasharray="3 9"
               transform="rotate(-14 720 450)"/>
      <ellipse cx="720" cy="450" rx="390" ry="175" stroke-width=".5" stroke-dasharray="2 12"
               transform="rotate(18 720 450)"/>
      <circle cx="720" cy="450" r="310" stroke-width=".45" stroke-dasharray="1 8"/>
    </g>
    <g class="cosmos-constellations" fill="none" stroke="currentColor"
       stroke-width=".65" stroke-dasharray="2 4">
      <polyline points="140,155 210,125 268,168 335,142 398,195"/>
      <polyline points="1030,135 1115,178 1190,148 1275,205"/>
      <line x1="1115" y1="178" x2="1085" y2="255"/>
      <polyline points="195,640 265,685 320,665 375,725"/>
      <polyline points="1060,675 1148,632 1225,688 1310,645"/>
    </g>
    <g fill="currentColor">
      ${featured.map(([x, y, r, d]) => `
        <g class="star-twinkle" style="animation-delay:${d}" transform="translate(${x},${y})">
          <circle r="7" fill="url(#star-glow-lg)"/>
          <path d="M-5.5 0H5.5M0-5.5V5.5" stroke="currentColor" stroke-width=".7"/>
          <circle r="${r}"/>
        </g>`).join('')}
      ${dust.map((d) => `
        <circle class="${d.twinkle ? 'star-twinkle' : ''}" cx="${d.x}" cy="${d.y}"
                r="${d.r}" opacity="${d.o}"
                style="animation-delay:${d.delay};animation-duration:${d.dur}"/>`).join('')}
    </g>`;
}

/* ---------------- 吊牌绳 ---------------- */

const SEGMENTS = 12;
const GRAVITY = 1400;
const DAMPING = 0.985;
const ITERATIONS = 18;
const FIXED_DT = 1 / 60;
const CARD_WIDTH = 170;

class Lanyard {
  constructor() {
    this.layer = document.getElementById('lanyard');
    this.rope = document.getElementById('rope');
    this.ropePath = document.getElementById('rope-path');
    this.anchor = document.getElementById('rope-anchor');
    this.card = document.getElementById('badge');
    this.points = [];
    this.anchorX = 0;
    this.anchorY = 0;
    this.restLength = 0.0001;
    this.fullRestLength = 160;
    this.lengthScale = 0;
    this.lengthTarget = 0;
    this.lengthSpeed = 2.2;
    this.onLengthDone = null;
    this.flipTarget = false;
    this.flipDeg = 0;
    this.dragging = false;
    this.moved = false;
    this.downTime = 0;
    this.downX = 0;
    this.downY = 0;
    this.dragX = 0;
    this.dragY = 0;
    this.smoothAngle = 0;
    this.swayPhase = Math.random() * Math.PI * 2;
    this.onActivate = null;
    this.raf = null;
    this.accumulator = 0;
    this.last = 0;
    this.bind();
    this.layout();
    window.addEventListener('resize', () => this.layout());
    this.tick();
  }

  layout() {
    const rect = this.layer.getBoundingClientRect();
    this.anchorX = rect.width / 2;
    this.anchorY = 6;
    this.fullRestLength = Math.max(rect.height * 0.32, 120);
    if (this.points.length !== SEGMENTS + 1) {
      this.points = Array.from({ length: SEGMENTS + 1 }, () => ({
        x: this.anchorX, y: this.anchorY, px: this.anchorX, py: this.anchorY,
      }));
    }
  }

  bind() {
    this.card.addEventListener('pointerdown', (e) => this.onDown(e));
  }

  onDown(e) {
    this.dragging = true;
    this.moved = false;
    this.downTime = performance.now();
    const rect = this.layer.getBoundingClientRect();
    this.downX = e.clientX - rect.left;
    this.downY = e.clientY - rect.top;
    this.dragX = this.downX;
    this.dragY = this.downY;
    const tip = this.points[this.points.length - 1];
    tip.x = tip.px = this.dragX;
    tip.y = tip.py = this.dragY;
    const move = (ev) => {
      this.dragX = ev.clientX - rect.left;
      this.dragY = ev.clientY - rect.top;
      if (Math.hypot(this.dragX - this.downX, this.dragY - this.downY) > 12) this.moved = true;
    };
    const up = () => {
      const quick = performance.now() - this.downTime < 600;
      this.dragging = false;
      window.removeEventListener('pointermove', move);
      window.removeEventListener('pointerup', up);
      window.removeEventListener('pointercancel', up);
      if (!this.moved && quick && !this.flipTarget) this.onActivate?.();
    };
    window.addEventListener('pointermove', move);
    window.addEventListener('pointerup', up);
    window.addEventListener('pointercancel', up);
    e.preventDefault();
  }

  step(time) {
    const tipIndex = this.points.length - 1;
    if (this.lengthScale !== this.lengthTarget) {
      const dir = Math.sign(this.lengthTarget - this.lengthScale);
      this.lengthScale += dir * this.lengthSpeed * FIXED_DT;
      if ((dir > 0 && this.lengthScale >= this.lengthTarget) ||
          (dir < 0 && this.lengthScale <= this.lengthTarget)) {
        this.lengthScale = this.lengthTarget;
        const cb = this.onLengthDone;
        this.onLengthDone = null;
        cb?.();
      }
    }
    this.restLength = (this.fullRestLength * this.lengthScale) / SEGMENTS || 0.0001;

    const tip = this.points[tipIndex];
    const speed = Math.hypot(tip.x - tip.px, tip.y - tip.py);
    const sway = !this.dragging && speed < 0.6 && this.lengthScale > 0.95
      ? Math.sin(time * 0.9 + this.swayPhase) * 5
      : 0;

    for (let i = 1; i <= tipIndex; i += 1) {
      const p = this.points[i];
      const vx = (p.x - p.px) * DAMPING;
      const vy = (p.y - p.py) * DAMPING;
      p.px = p.x;
      p.py = p.y;
      p.x += vx + (i === tipIndex ? sway : 0) * FIXED_DT * FIXED_DT;
      p.y += vy + GRAVITY * FIXED_DT * FIXED_DT;
    }
    if (this.dragging) {
      tip.x = this.dragX;
      tip.y = this.dragY;
    }
    for (let iter = 0; iter < ITERATIONS; iter += 1) {
      this.points[0].x = this.anchorX;
      this.points[0].y = this.anchorY;
      for (let i = 0; i < tipIndex; i += 1) {
        const a = this.points[i];
        const b = this.points[i + 1];
        let dx = b.x - a.x;
        let dy = b.y - a.y;
        const dist = Math.hypot(dx, dy) || 0.0001;
        const diff = (dist - this.restLength) / dist;
        if (i === 0) {
          b.x -= dx * diff;
          b.y -= dy * diff;
        } else {
          dx *= diff * 0.5;
          dy *= diff * 0.5;
          a.x += dx;
          a.y += dy;
          b.x -= dx;
          b.y -= dy;
        }
      }
      if (this.dragging) {
        tip.x = this.dragX;
        tip.y = this.dragY;
      }
    }
  }

  path() {
    const p = this.points;
    if (p.length < 2) return '';
    let d = `M ${p[0].x.toFixed(1)} ${p[0].y.toFixed(1)}`;
    for (let i = 1; i < p.length - 1; i += 1) {
      const mx = (p[i].x + p[i + 1].x) / 2;
      const my = (p[i].y + p[i + 1].y) / 2;
      d += ` Q ${p[i].x.toFixed(1)} ${p[i].y.toFixed(1)} ${mx.toFixed(1)} ${my.toFixed(1)}`;
    }
    const last = p[p.length - 1];
    d += ` L ${last.x.toFixed(1)} ${last.y.toFixed(1)}`;
    return d;
  }

  render() {
    this.ropePath.setAttribute('d', this.path());
    this.anchor.setAttribute('cx', this.anchorX.toFixed(1));
    this.anchor.setAttribute('cy', this.anchorY.toFixed(1));

    const p = this.points;
    const tip = p[p.length - 1];
    const before = p[p.length - 2];
    const targetAngle = Math.atan2(tip.x - before.x, tip.y - before.y);
    this.smoothAngle += (targetAngle - this.smoothAngle) * 0.2;

    const rotateY = Math.max(-30, Math.min(30, (tip.x - tip.px) * 1.6));
    const rotateX = Math.max(-15, Math.min(15, -(tip.y - tip.py) * 1.2));

    const target = this.flipTarget ? 90 : 0;
    this.flipDeg += (target - this.flipDeg) * 0.12;
    if (Math.abs(target - this.flipDeg) < 0.1) this.flipDeg = target;

    this.card.style.transform =
      `translate(${(tip.x - CARD_WIDTH / 2).toFixed(1)}px, ${tip.y.toFixed(1)}px) ` +
      `rotate(${(-this.smoothAngle * 180 / Math.PI).toFixed(2)}deg) ` +
      `rotateX(${rotateX.toFixed(2)}deg) rotateY(${(rotateY + this.flipDeg).toFixed(2)}deg)`;
  }

  tick = () => {
    this.raf = requestAnimationFrame(this.tick);
    const now = performance.now();
    if (!this.last) this.last = now;
    this.accumulator += Math.min((now - this.last) / 1000, 0.1);
    this.last = now;
    while (this.accumulator >= FIXED_DT) {
      this.step(now / 1000);
      this.accumulator -= FIXED_DT;
    }
    this.render();
  };

  drop() {
    this.lengthTarget = 1;
    this.lengthSpeed = 1.6;
    const tip = this.points[this.points.length - 1];
    if (tip) tip.px = tip.x - 6;
    return new Promise((resolve) => { this.onLengthDone = resolve; });
  }

  retract() {
    this.lengthTarget = 0;
    this.lengthSpeed = 2.6;
    return new Promise((resolve) => { this.onLengthDone = resolve; });
  }
}

/* ---------------- 状态机 ---------------- */

let gridApi = null;
let lanyard = null;
let state = 'gate';

const gate = document.getElementById('gate');
const panelWrap = document.getElementById('panel-wrap');
const panel = document.getElementById('panel');
const backBtn = document.getElementById('back');

function setState(next) {
  state = next;
  backBtn.toggleAttribute('data-show', next === 'badge' || next === 'panel');
}

function playGateIntro() {
  if (window.matchMedia('(prefers-reduced-motion: reduce)').matches) return;
  const words = document.querySelectorAll('#gate-wordmark span');
  gate.animate(
    [{ opacity: 0, transform: 'scale(.96) translateY(8px)' },
     { opacity: 1, transform: 'none' }],
    { duration: 500, easing: 'cubic-bezier(.2,.7,.25,1)', fill: 'both' });
  words.forEach((el, i) => {
    el.animate(
      [{ filter: 'blur(10px)', opacity: 0, transform: 'translateY(8px)' },
       { filter: 'blur(0)', opacity: 1, transform: 'none' }],
      { duration: 520, delay: 60 + i * 26, easing: 'cubic-bezier(.22,.9,.28,1)', fill: 'both' });
  });
  document.querySelectorAll('.gate-actions, .gate-sub, .gate-divider').forEach((el, i) => {
    el.animate(
      [{ opacity: 0, transform: 'translateY(12px)' }, { opacity: 1, transform: 'none' }],
      { duration: 400, delay: 120 + i * 70, easing: 'cubic-bezier(.2,.7,.25,1)', fill: 'both' });
  });
}

async function enter() {
  gate.setAttribute('data-leaving', '');
  setState('badge');
  lanyard.layer.setAttribute('data-active', '');
  await new Promise((r) => setTimeout(r, 260));
  gate.hidden = true;
  lanyard.flipTarget = false;
  await lanyard.drop();
}

async function openPanel() {
  if (state !== 'badge') return;
  setState('panel');
  lanyard.flipTarget = true;
  panelWrap.hidden = false;
  await new Promise((r) => setTimeout(r, 60));
  panel.classList.add('enter');
  setTimeout(() => panel.querySelector('.panel-body')?.classList.add('ready'), 260);
  setTimeout(() => document.getElementById('email')?.focus(), 520);
}

function closePanel() {
  if (state !== 'panel') return;
  panel.classList.remove('enter');
  document.querySelector('.panel-body')?.classList.remove('ready');
  lanyard.flipTarget = false;
  setState('badge');
  setTimeout(() => { panelWrap.hidden = true; }, 420);
}

async function backToGate() {
  if (state === 'panel') { closePanel(); return; }
  if (state !== 'badge') return;
  setState('gate');
  await lanyard.retract();
  gate.hidden = false;
  gate.removeAttribute('data-leaving');
  playGateIntro();
}

function shakePanel() {
  panel.classList.remove('shake');
  void panel.offsetWidth;
  panel.classList.add('shake');
  setTimeout(() => panel.classList.remove('shake'), 500);
}

/* ---------------- 登录提交 ---------------- */

const form = document.getElementById('login-form');
const submit = document.getElementById('submit');
const errBox = document.getElementById('form-error');

function setBusy(busy) {
  submit.disabled = busy;
  submit.innerHTML = busy
    ? '<span class="spin"></span> 正在登录…'
    : `${submit.dataset.icon} <span>登录</span>`;
}

form.addEventListener('submit', async (ev) => {
  ev.preventDefault();
  const username = document.getElementById('email').value.trim();
  const password = document.getElementById('password').value;
  if (!username || !password) return;
  errBox.textContent = '';
  setBusy(true);
  try {
    const res = await fetch('/api/login', {
      method: 'POST',
      credentials: 'same-origin',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ username, password }),
    });
    const data = await res.json().catch(() => ({}));
    if (res.ok && data.ok) {
      setState('exit');
      panel.classList.remove('enter');
      lanyard.flipTarget = false;
      setTimeout(() => { location.href = data.redirect || '/me'; }, 420);
      return;
    }
    errBox.textContent = (data.error && data.error.message) || '登录失败';
    shakePanel();
    setBusy(false);
  } catch (e) {
    errBox.textContent = '网络错误：' + e.message;
    shakePanel();
    setBusy(false);
  }
});

/* 密码显示切换 */
document.getElementById('reveal').addEventListener('click', (ev) => {
  const input = document.getElementById('password');
  const on = input.type === 'password';
  input.type = on ? 'text' : 'password';
  ev.currentTarget.setAttribute('aria-pressed', String(on));
  ev.currentTarget.innerHTML = on
    ? `<svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.5" aria-hidden="true"><path d="M3 3l18 18M10.6 10.6a2 2 0 002.8 2.8M6.7 6.7C4.6 8 3 10 2 12c2 4 6 7 10 7 1.7 0 3.3-.5 4.7-1.3M17.9 17.9C19.6 16.6 21 14.4 22 12c-2-4-6-7-10-7-.9 0-1.8.2-2.6.5"/></svg>`
    : `<svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.5" aria-hidden="true"><path d="M2 12s4-7 10-7 10 7 10 7-4 7-10 7-10-7-10-7Z"/><circle cx="12" cy="12" r="3"/></svg>`;
});

/* ---------------- 初始化 ---------------- */

applyTheme(
  localStorage.getItem(THEME_KEY) ||
  (window.matchMedia?.('(prefers-color-scheme: dark)').matches ? 'dark' : 'light')
);
paintCosmos();

gridApi = initGridScan(document.getElementById('grid-canvas'));
gridApi.setTheme(currentTheme() === 'dark');

lanyard = new Lanyard();
lanyard.onActivate = openPanel;

setState('gate');
playGateIntro();

document.getElementById('to-badge').addEventListener('click', enter);
document.getElementById('to-badge-2').addEventListener('click', () => {
  // 「注册」暂未开放：走同一个入口并在面板里给出说明
  enter().then(() => setTimeout(openPanel, 700));
});
document.getElementById('badge').addEventListener('keydown', (e) => {
  if (e.key === 'Enter' || e.key === ' ') { e.preventDefault(); openPanel(); }
});
backBtn.addEventListener('click', backToGate);
document.getElementById('panel-close').addEventListener('click', closePanel);
panelWrap.addEventListener('pointerdown', (e) => {
  if (e.target === panelWrap) closePanel();
});
document.getElementById('theme-toggle').addEventListener('click', (event) => {
  // 传事件而不是按钮，圆形揭示的圆心落在点击处
  setTheme(currentTheme() === 'dark' ? 'light' : 'dark', event);
});
window.addEventListener('keydown', (e) => {
  if (e.key === 'Escape' && state !== 'gate') backToGate();
});
document.getElementById('year').textContent = new Date().getFullYear();
