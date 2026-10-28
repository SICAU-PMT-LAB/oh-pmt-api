/* 移动端汉堡菜单：官网（home / plan / docs）与后台（admin / console / member）
   共用同一套行为。

   页面标记固定成两段：

     <button type="button" class="nav-toggle" aria-expanded="false"
             aria-controls="nav-mobile-menu" aria-label="打开菜单">
       <span class="sr-only">打开菜单</span>
       <svg class="icon-hamburger">…</svg>
       <svg class="icon-close">…</svg>
     </button>
     <div id="nav-mobile-menu" data-component="nav-mobile-menu-list" hidden>…</div>

   汉堡与关闭图标都写在 HTML 里，显示哪一个由 CSS 读 aria-expanded 决定，
   所以这个脚本只切状态、不换 DOM：图标不会在脚本报错时停在错误的那一枚。

   菜单用 hidden 属性开关，两份样式表都有 [hidden] { display: none !important }
   兜底。展开状态同时写到 <html data-nav-open>，给需要压住内容的样式用。
*/

export function initMobileNav() {
  const toggle = document.querySelector('.nav-toggle');
  const menu = toggle && document.getElementById(toggle.getAttribute('aria-controls'));
  if (!toggle || !menu) return null;

  // 菜单是 position: fixed 的浮层，但祖先只要带了 backdrop-filter / transform，
  // 就会成为它的包含块，bottom: 0 指向那个祖先的底边而不是视口。
  // 后台的内容容器正好有 backdrop-filter: blur(2px)，不搬出去菜单会拉到两千多像素高。
  if (menu.parentElement !== document.body) document.body.append(menu);

  const setOpen = (open) => {
    menu.hidden = !open;
    toggle.setAttribute('aria-expanded', String(open));
    // 文案跟着状态走：读屏念的是「关闭菜单」，不只是视觉上换成叉
    const label = open ? '关闭菜单' : '打开菜单';
    toggle.setAttribute('aria-label', label);
    const sr = toggle.querySelector('.sr-only');
    if (sr) sr.textContent = label;
    document.documentElement.toggleAttribute('data-nav-open', open);
  };

  toggle.addEventListener('click', () => setOpen(menu.hidden));

  // 点了菜单里的链接就收起：菜单铺满一屏，留着会把刚跳到的位置盖住
  menu.addEventListener('click', (e) => {
    if (e.target.closest('a')) setOpen(false);
  });

  // 点菜单外面收起。toggle 自己交给上面的 click 处理，否则会开完立刻关
  document.addEventListener('click', (e) => {
    if (menu.hidden) return;
    if (menu.contains(e.target) || toggle.contains(e.target)) return;
    setOpen(false);
  });

  document.addEventListener('keydown', (e) => {
    if (e.key !== 'Escape' || menu.hidden) return;
    setOpen(false);
    toggle.focus();
  });

  // 视口变宽时收起。断点官网是 40rem、后台是 60rem，与其各写一份，
  // 不如直接问按钮自己还在不在：按钮被样式隐藏了就把菜单关掉。
  let pending = 0;
  addEventListener('resize', () => {
    clearTimeout(pending);
    pending = setTimeout(() => {
      if (menu.hidden) return;
      if (getComputedStyle(toggle).display === 'none') setOpen(false);
    }, 120);
  });

  return { setOpen };
}

/* 数据页（/data）的顶栏是另一套结构（data-slot="menu-button" + #stats-mobile-nav），
   逻辑归 data.js，这里不接管。 */
