/* 站点头部字标：全大写 PROMETHEUS LAB，滚动时收成 PMT。

   灵感来自 anthropic.com 的页头——字标随滚动从全称收成缩写，
   位置和基线不动，只让中间的字母退场。

   做法是把每个字母拆成独立 span，滚动跨过阈值时给非首字母加
   [data-folded]，它们靠 width/opacity 过渡到 0，P、M、T 三个
   字母就自然靠拢成 PMT，不需要另外准备一套图形。

   宽度动画必须用 width 而不是 transform：字标在 flex 行里，
   只有真正改变占位宽度，右侧导航才会跟着左移。
*/

const WORD = 'PROMETHEUS';
// P R O M E T H E U S 里 P=0、M=3、T=5
const KEEP = new Set([0, 3, 5]);

/**
 * 把 <span class="wordmark">PROMETHEUS</span> 就地拆成逐字母 span。

 * 返回 { root, setFolded(bool) }。
 */
export function mountWordmark(el) {
  if (!el || el.dataset.mounted) return null;
  el.dataset.mounted = '1';

  // LAB 后缀整体退场，先摘出来，等字母重建完再放回
  const suffix = el.querySelector('.wm-suffix');

  el.textContent = '';
  const letters = [...WORD].map((ch, i) => {
    const s = document.createElement('span');
    s.className = 'wm-letter';
    s.textContent = ch;
    // 序号喂给 CSS 的 --i，用来把每个字母的过渡错开十几毫秒
    s.style.setProperty('--i', String(i));
    if (KEEP.has(i)) s.dataset.keep = '';
    el.append(s);
    return s;
  });

  if (suffix) {
    suffix.style.display = 'inline-block';
    el.append(suffix);
  }

  let folded = null;
  function setFolded(next) {
    if (next === folded) return;
    folded = next;
    el.dataset.folded = next ? '' : null;
    if (!next) el.removeAttribute('data-folded');
  }
  setFolded(false);
  return { root: el, setFolded, letters };
}

/**
 * 监听滚动，越过 threshold 就把字标收起来。
 * @param {HTMLElement} el .wordmark 元素
 * @param {number} threshold 触发像素，默认 120
 */
export function initWordmarkScroll(el, threshold = 120) {
  const wm = mountWordmark(el);
  if (!wm) return;

  let ticking = false;
  const update = () => {
    ticking = false;
    wm.setFolded(window.scrollY > threshold);
  };
  const onScroll = () => {
    if (ticking) return;
    ticking = true;
    requestAnimationFrame(update);
  };

  window.addEventListener('scroll', onScroll, { passive: true });
  update();
  return wm;
}
