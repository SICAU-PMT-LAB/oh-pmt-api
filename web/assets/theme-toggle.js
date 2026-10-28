/* 明暗切换按钮：挂到导航里的 #theme-toggle 上。

   按钮形态和图标取自登录页那颗胶囊按钮（/assets/login.js 的 paintThemeIcon），
   切换时用 View Transition 从按钮中心扩散做圆形揭示。

   页面只要 <button id="theme-toggle"> 加一行 import 即可；图标由这里画，
   所以 HTML 里留空按钮就行。
*/

import { currentTheme, setTheme } from '/assets/site.js';

const SUN = `<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.5"
  aria-hidden="true"><path stroke-linecap="round" stroke-linejoin="round"
  d="M12 3v2.25m6.364.386-1.591 1.591M21 12h-2.25m-.386 6.364-1.591-1.591M12 18.75V21m-4.773-4.227-1.591 1.591M5.25 12H3m4.227-4.773L5.636 5.636M15.75 12a3.75 3.75 0 1 1-7.5 0 3.75 3.75 0 0 1 7.5 0Z"/></svg>`;

const MOON = `<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.5"
  aria-hidden="true"><path stroke-linecap="round" stroke-linejoin="round"
  d="M21.752 15.002A9.72 9.72 0 0 1 18 15.75c-5.385 0-9.75-4.365-9.75-9.75 0-1.33.266-2.597.748-3.752A9.753 9.753 0 0 0 3 11.25C3 16.635 7.365 21 12.75 21a9.753 9.753 0 0 0 9.002-5.998Z"/></svg>`;

/** 按当前主题刷新图标：亮色显示月亮（点一下去暗），暗色显示太阳。 */
export function paintToggle(btn = document.getElementById('theme-toggle')) {
  if (!btn) return;
  const dark = currentTheme() === 'dark';
  btn.innerHTML = dark ? SUN : MOON;
  btn.title = dark ? '切换到亮色' : '切换到暗色';
  btn.setAttribute('aria-pressed', String(dark));
  btn.setAttribute('aria-label', btn.title);
}

export function initThemeToggle() {
  const btn = document.getElementById('theme-toggle');
  if (!btn) return;
  paintToggle(btn);
  btn.addEventListener('click', (event) => {
    const next = currentTheme() === 'dark' ? 'light' : 'dark';
    // 传事件而不是按钮，圆形揭示的圆心落在点击处
    setTheme(next, event);
    // View Transition 会替换 DOM 快照，等它跑完再刷图标
    setTimeout(() => paintToggle(btn), 30);
  });

  // 没手动选过时跟随系统变化
  window.matchMedia?.('(prefers-color-scheme: dark)').addEventListener?.('change', () => {
    if (!document.documentElement.dataset.theme) paintToggle(btn);
  });
}

initThemeToggle();
