# 前端设计约定

这份文件写给接手界面的模型。它记录的是**为什么这样做**和**改了会坏在哪里**，
不是逐条罗列 CSS。要改样式先读这里，再动文件。

范围：`web/` 下所有页面与 `web/assets/` 下的样式和脚本。后端逻辑看 `../CONTEXT.md`。

## 一眼看懂

界面分两套皮肤，共用同一份背景动效：

| 皮肤 | 用在 | 变量作用域 | 基调 |
|---|---|---|---|
| 官网 | `/` `/plan` `/data` `/docs` | `[data-page="site"]`、`[data-page="go"]`、`[data-page="stats"]` | 黑白单色、等宽字体、居中竖栏 |
| 后台 | `/admin` `/members/<id>` `/console` | 同上 + `panel.css` 的 `--p-*` | 复用官网外壳，密度更高 |

登录页 `/login` 是第三种：暖纸色 + 白金质感 + 吊牌动画，独立于上面两套。
详见 [登录页](#登录页)。

视觉语言抄的是 opencode.ai：等宽字体撑满全站、内容区 67.5rem 居中、左右各一道
1px 细线、不用圆角不用阴影不用彩色强调。层次靠字号、字重和留白拉开。

## 文件分工

```
web/assets/
  site.css        官网骨架：字体、色板、容器、顶栏、hero、卡片、页脚、背景层
  plan.css        Plan 页专属：档位色阶、价格卡、额度表
  data.css        数据页专属：从 opencode.ai/data 的构建产物裁出来的规则（见「数据页」）
  model-icons.svg 模型作者图标（symbol 表），数据页的排行榜与对比卡用
  docs.css        文档页专属（自带一套色板，不依赖 site.css 的变量）
  panel.css       后台专属：--p-* 语义层 + 统计卡/表格/表单/弹层/进度条
  login.css       登录页：暖纸色、玻璃面板、吊牌

  site.js         官网工具函数：格式化、图表、主题读写
  data.js         数据页：拉 /api/public/data、画堆叠图、悬停浮层、明暗切换
  backdrop.js     动态背景（星图 + 透视网格）挂载
  gridscan.js     背景的 WebGL 网格，被 backdrop.js 调用
  wordmark.js     字标滚动折叠
  theme-toggle.js 明暗切换按钮
  login.js        登录页状态机
  charts.js       后台图表（lineChart/ringChart/...），官网不用
```

`charts.js` 和 `site.js` 都提供图表，区别是前者给后台（要 ring、histogram、heatmap
这些指标图），后者给官网（要堆叠柱、排名卡、紧凑数字格式化）。别混用。

## 变量分层

三层，改样式只该动最上面那层：

1. **语义层** `--color-site-bg` / `--color-text-strong` / `--p-surface` —— 组件只读这层
2. **色板层** 上面那层由这些值算出来，例如 `--p-line: var(--color-border-weak)`
3. **刻度层** `--space-*`（4px 基数）、`--font-size-*`

关键是**变量挂在哪个元素上**。`--color-site-*` 定义在 `[data-page="site"]`，
而这个属性落在 `<body>` 上，不是 `:root`。所以：

```css
/* 对：同样的作用域，能读到 */
body[data-page="site"] { --p-line: var(--color-border-weak); }

/* 错：:root 上没有 --color-border-weak，读到空值，边框消失 */
:root { --p-line: var(--color-border-weak); }
```

这条踩过两次，后台面板整片丢边框就是因为它。新加变量层时先确认作用域一致。

## 主题

### 键与属性

| 键 | 用途 |
|---|---|
| `pmt_site_theme` | 官网 + 后台共用 |
| `pmt_login_theme` | 登录页 |

键名差异是有意的：登录页的暖纸色和官网的黑白是两套观感，主题选择不该互相污染。
`pmt_data_theme`、`pmt_docs_theme`、`ohpmt_theme` 都已废弃，代码里再看到就是漏改。

存的值只有 `'dark'` / `'light'`，没存过则跟随系统。落到
`<html data-theme="dark|light">`，没设时让 `prefers-color-scheme` 决定。

### 三条选择器必须分开写

这是最容易写错的地方。手写 CSS 里常见的是只留 `@media`：

```css
/* 错：系统是亮色、用户手动切到暗色时，这段永远不生效，
   页面底色已经变黑，组件却还停在亮色档 */
@media (prefers-color-scheme: dark) { ... }
```

正确写法是两份完整声明，各配一个选择器：

```css
:root[data-theme="dark"] [data-page="site"] { /* 显式暗色 */ }
:root[data-theme="light"] [data-page="site"] { /* 显式亮色 */ }
@media (prefers-color-scheme: dark) {
  :root:not([data-theme="light"]) [data-page="site"] { /* 没选过才跟随系统 */ }
}
```

`site.css`、`data.css`、`panel.css`、`plan.css` 都已按这个结构写好。新增深色规则时
照抄这个形状，别只加 `@media`。验证要跑六种组合：系统亮/暗 × 未选/存 light/存 dark，
每种都读一次计算值确认。

### 切换动画

`site.js` 的 `setTheme(next, event)` 做圆形揭示：圆心取真实点击坐标
（`event.clientX/clientY`），半径取点击点到最远角的距离，600ms `ease-in-out`
作用于 `::view-transition-new(root)`。三点不能省：

- `::view-transition-new(root)` 要 `z-index: 2147483646`，否则新的主题快照
  盖不住 `z-index: 0` 的背景层，扩散时能看到接缝
- `html` 的底色必须跟着主题走。`body` 和 `main` 都是透明的，真正垫底的只有 `html`；
  它要是写死深色，浅色主题下扩散到边缘会露出一圈黑
- `prefers-reduced-motion` 下直接切，不做动画

后台和官网都用 `/assets/theme-toggle.js`，页面只要放一个空的
`<button id="theme-toggle">`，图标由脚本注入。不要再写第二套监听——两套监听会互相
覆盖，表现为「点了没反应」。

## 背景

`backdrop.js` 画两样东西：确定性星图（144 颗，Vogel 螺旋布点，同一个种子刷新后
位置不变）和透视网格（`gridscan.js`，WebGL 逐像素着色）。

页面只需要放两个空节点：

```html
<div class="pmt-backdrop" aria-hidden="true">
  <div class="pmt-backdrop-nebula"></div>
  <svg class="pmt-backdrop-stars" viewBox="0 0 1440 900"
       preserveAspectRatio="xMidYMid slice"></svg>
</div>
<canvas class="pmt-backdrop-grid" aria-hidden="true"></canvas>
```

然后 `import { mountBackdrop } from '/assets/backdrop.js'; mountBackdrop();`。
`backdrop.js` 只导出函数、**不会自动执行**，忘了调就是一片空白。
`theme-toggle.js` 相反，import 就自跑。

### 层次不能乱

```
z-index 0    .pmt-backdrop（星图 + 网格，position: fixed）
z-index 1    内容容器
```

`html` 保留不透明的底色，`body` 和内容容器透明，背景才透得出来。内容容器
（`main`、`.site-header`、`.layout`、`[data-component="container"]`）都要
`position: relative; z-index: 1`。

反面教材：曾经试过给 `html` 加个 `has-backdrop` 类去透明化，结果整页白底、
浅色字全看不见。背景必须压在 `html` 底色之上、内容之下，中间不能有其他不透明层。

### 半透明与模糊

固定背景会让滚动的文字和星星叠在一起。凡是粘性条都要半透明加模糊：

```css
background: color-mix(in srgb, var(--color-site-bg) 82%, transparent);
backdrop-filter: blur(14px);
```

**只给导航、页脚、弹层这类"页框"用。** 卡片、表格、图表容器一律保持透明，
靠 1px 边框区分层次，让网格在中间穿过去——这是这套设计的辨识度来源。
例外是装饰性实体，比如演示视频占位块必须是不透明的近黑色，反过来做了会漏星点。

### 调整网格

`backdrop.js` 传参给 `initGridScan`，改一处就能统一调色：

```js
linesColorLight: '#b5b0a3', linesColorDark: '#706c61',
scanColorLight: '#1c1c1a', scanColorDark: '#fff3d1',
```

后台因为要读数据，背景压得更低（`panel.css` 末尾整块负责）：

| | 星图 | 网格 |
|---|---|---|
| 官网 | 1 | 1 |
| 后台（亮） | 0.28 | 0.34 |
| 后台（暗） | 0.34 | 0.42 |

后台的内容栏还额外垫了一层 `color-mix(… 72%, transparent)` 加 `blur(2px)`，
让网格从栏外穿过、栏内保持干净。官网不加这层衬底。

### 排障

- **网格不出现**：先看 canvas 的 `width/height` 属性。`resize()` 如果量的是
  `canvas.clientHeight`，会在首帧读到整个文档高度，缓冲区变成 1440x3713，
  只显示出被压扁的第一屏。高度必须取 `window.innerHeight`
- **`readPixels` 全是 0**：正常。没有开 `preserveDrawingBuffer`，合成后缓冲区就清了。
  验证用 `canvas.toDataURL()` 或直接截图
- **闪烁**：`gridscan.js` 的扫描周期是 `scanDuration + scanDelay`，相位窗口之外
  没有光。要么调长 `scanDelay`，要么接受它本来就是脉冲式的
- **整页截图里网格特别密**：全页截图会把 `position: fixed` 元素拉伸到整个文档高度。
  判断背景只能用视口截图

## 字标

`wordmark.js` 把 `<span class="wordmark">PROMETHEUS<span class="wm-suffix">&nbsp;API</span></span>`
拆成逐字母 `<span>`，滚动超过 120px 时给非保留字母加 `[data-folded]`，
`P`/`M`/`T` 三字母自然收拢成 `PMT`，`.wm-suffix` 的 ` API` 始终可见。

宽度动画必须走 `width`。字标在 flex 行里，只有真的改变占位宽度，右侧导航才会跟着
左移；`transform: scale` 做得到视觉效果但推不动后面的元素。

字母间隙由 `.wordmark` 的 `gap` 控制，折叠时才变成 `0.08em`。**不要在单个字母上加
`margin-right` 当间隙**——展开状态下它也会生效，`P` 和 `R` 之间会多出一道缝。

`data-page="site"` 的页头字标是 17px，`/data` 和 `/docs` 的窄页头用 14px
（`[data-slot="brand"] .wordmark, .site-header .wordmark`）。

## 页面结构

### 官网页

```html
<body data-page="site">          <!-- 变量作用域挂在 body 上 -->
  <div class="pmt-backdrop">…</div>
  <canvas class="pmt-backdrop-grid"></canvas>
  <main data-page="opencode">
    <div data-component="container">
      <section data-component="top">…</section>
      <section data-component="hero">…</section>
      …
    </div>
    <div data-component="legal">…</div>
  </main>
</body>
```

`container` 撑起 67.5rem 的居中竖栏，左右 1px 边线，顶栏粘在顶部。
各分区之间用 `border-top` 分隔，不用背景色块。

### 后台页

`admin.html` / `member.html` / `console.html` 用同一套外壳：

```html
<body data-page="site">
  <div class="pmt-backdrop">…</div>
  <canvas class="pmt-backdrop-grid"></canvas>
  <div data-component="container">
    <section data-component="top">
      <a class="brand">字标 + <span class="kicker">管理后台</span></a>
      <nav id="nav">…</nav>
      <div class="top-actions">主题 / 退出 / 汉堡</div>
    </section>
    <div id="nav-mobile-menu">…</div>
    <main class="main" id="main">…</main>
    <footer class="panel-foot">…</footer>
  </div>
  <div class="modal-mask" id="…">…</div>   <!-- 弹层在 container 外面 -->
</body>
```

后台的 JS 只依赖两个结构：`#nav a[data-page]` 用来切激活态，`.page#page-*`
用来切内容。外壳怎么改都不影响那一千多行面板逻辑，但这两个不能动。

导航链接分两类，处理方式不同：

- `#overview` 这类页内锚点 → 拦截 `preventDefault`，调 `show()` 切分区
- `/admin#members` 这类跨页跳转 → 交给浏览器

`admin.html` 全是页内锚点，可以无脑全拦。`console.html` 两种混用，必须按 href 区分：

```js
if ((a.getAttribute('href') || '').startsWith('/admin')) return;  // 跨页的放行
```

判断写反会让导航点不动，而且没有任何报错。

### 数据页

`data.html` 的根是 `<main data-page="stats">`，`body` 上**没有** `data-page="site"`。
所以它不继承官网色板，用自己的 `--stats-*` 一整套。粘性顶栏（72px）、
分区导航、`--stats-page-padding` 都是独立定义的。

这一页是**照 opencode.ai/zh/data 复刻**的，`data.css` 里绝大部分规则直接从对方
构建产物 `entry-client-*.css` 裁出来，只删掉本页不存在的章节（模型详情页、
对比详情页、订阅弹层）与用不上的组件。所以：

- **不要按官网那套去改它**。字号、间距、hairline 都是对方的原值，
  改一个 `padding` 就会和截图对不上。要改先量，用无头 Chrome 读
  `getBoundingClientRect()` 对照，别凭肉眼。
- 对方用 `<html data-stats-theme>` + `<main data-theme>` 两层控制主题，
  本站统一到 `<html data-theme>`（`/assets/theme-toggle.js` 写的也是这个），
  裁规则时已经把选择器改写过来。新增深色规则请沿用 `:root[data-theme=dark]`。
- 字体文件在 `web/assets/fonts/`，CSS 里的路径已改成 `/assets/fonts/`。
- 厂商排名已整节删除（它会泄露账号池的构成），排行榜与作者归组的配色仍来自
  `src/labs.py`，前端不自己定色。`data.css` 里还留着上游的 `market-*` 规则，
  因为它们是和排行榜、`top-models-chart` 写在同一批分组选择器里的死代码，
  删一条要验证一页，收益不抵风险。
- 背景是主站那套动效背景（`mountBackdrop()`：星空 + 星轨 + 透视网格）。
  数据页只引 `data.css`、不引 `site.css`，所以 `.pmt-backdrop*` 那几条定位规则
  在 `data.css` 里自带了一份——漏了它们，背景层没有 `position:fixed`，
  会以 1440×900 的身位挤进文档流，把整页往下推一屏。
- 页面各层底色要放空（`main`、section、hero 的 h1 与说明句全是 transparent），
  背景才透得上来；卡片、浮层、头像仍保留自己的底色，否则内容糊在星点上。
  亮色下网格压到 `.18`，这一页文字比主页密得多，全不透明会盖住正文。
- 顶栏与页脚只留 `DATA` 字标与一行版权。页脚那四列导航与三态主题组是上游的
  私货，已经删掉；右上角换成主站那颗圆形按钮（`data-variant=round`，34px，`border-radius:999px`）。
- 作者图标来自 `web/assets/model-icons.svg`（对方的 sprite 里与本页相关的
  symbol）。外部 `<use>` 引用 symbol 时尺寸由 svg 自己的 `viewBox` 决定，
  缺了它会被 CSS 宽高硬拉伸，图标会糊。

动效要点（改交互前先看）：

| 位置 | 行为 |
|---|---|
| 堆叠柱状图 | 指到哪一段，**该模型在当天所有列**都保留原色，其余变 `#eee`；浮层贴在柱子的左/右侧（由索引是否过半决定）。渲染完要把 `scrollLeft` 推到最右：日数据都集中在最近几天，停在最左会看到一整屏空柱（opencode 也是这么做的） |
| 排行榜卡片 | `transform: translateY(-1px)` + 描边加深 + 阴影，0.14s |
| 会话/Token 成本 | 浮层按行号定位（`top = index*36 + 28`），指到哪行就移到哪行，不走过渡动画 |
| 缓存比例 | 浮层同理，指到哪行就移到哪行 |
| 首屏时间 | 两行轨道 `.68s cubic-bezier(.16,1,.3,1)`，首次渲染必须是两行同样内容，否则 -50% 会错位 |
| 明暗切换 | 圆形揭示（View Transition），圆心是点击位置；不支持时直接换色 |
| 首屏时间 | 先画服务端时区（`UTC 12:47`），两帧后翻成访问者时区（`GMT+8 20:47`）。这不是装饰：上游 SSR 出的是服务端时区，水合后才换成本地，那一翻是页面上唯一一次自动动画 |

改这一页之后至少在 1440 / 700 / 420 三个宽度各截图一次：柱子的
`--top-models-bar-gap`、坐标轴的旋转标签、排行榜的横向滚动卡片
都是按断点写死的。

#### 模型对比详情页

`/data/compare/<lab>/<model>/<lab>/<model>` 与数据页共用 `data.css`，
结构与上游的对比页一致：面包屑 → 头像标题 → 模型选择条 → 六轴雷达 →
指标矩阵。几处必须知道的：

- 网格列宽靠 `--compare-detail-grid` 这个**内联变量**（写在
  `[data-component=compare-detail-comparison]` 的 `style` 上）。CSS 里只有
  它的消费者，没有定义者，漏了它整个矩阵会塌成一列。
- 雷达图的六个轴是本站真有的六件事（用量份额、价格优势、缓存命中、留存、
  成功率、用量规模），**不是 benchmark 分数**。上游那六轴是它自己的评测
  数据，本页没有，也不该编。
- 轴标签位置写死在 `AXIS_POS` / `MOBILE_POS` 两张表里，对应上游
  12% / 29% / 71% / 88% 那五档。
- 页面路径里的模型名是 kebab-case（`deepseek-v4-1-flash`），后端按
  「去掉所有非字母数字再比」匹配回显示名，所以大小写与连字符写法都能进来。

### 文档页

`docs.html` 三栏：左侧分级导航、中间正文、右侧"本页目录"（滚动高亮）。
`docs.css` 自带色板且**深色优先**（`:root {}` 里是深色默认值，
`:root[data-theme="light"]` 覆盖成浅色），和其他文件相反。

## 登录页

唯一保留暖色系的页面，`login.css` 独立成体系，不引用 `site.css`。

结构是"门 → 吊牌 → 面板"三段：

1. `.gate-panel` 竖排 `prometheuslab` 字标 + `API RELAY` + 登录/注册
2. `.lanyard-badge-layer` 吊牌从上方落下
3. `.flip-panel` 翻转展开，里面是账号密码表单

几个必须保留的细节：

- `.gate-panel` 是 `display: flex`，会让 HTML 的 `hidden` 属性失效。
  `login.css` 顶部有全局 `[hidden] { display: none !important }` 兜底
- 吊牌层的可见性靠 `[data-active]` 控制。`lengthScale` 为 0 时所有绳点挤在锚点上，
  卡片会盖住整个门板
- 表单字段标签必须写「账号」，不是「邮箱」——后端 `get_member_by_username`
  只查 `members.username`

不支持注册和找回密码，界面上对应的入口要么给解释、要么指向 `/plan`，
不能做成会失败的假表单。

## 常见坑

都是实际踩过并修好的，别重新引入。

**`hidden` 属性被 `display` 覆盖。** 给某个类写了 `display: flex/block`，
再用 `el.hidden = true` 就失效了。每个样式表顶部都该有：

```css
[hidden] { display: none !important; }
```

`site.css`、`data.css`、`panel.css`、`login.css` 都有（`docs.css` 目前没有，
遇到 hidden 失效时先补这条）。新样式表照抄。

**选择器列表插进 `@media`。** 写成
`:root[data-theme="dark"] [data-page],` 后面直接跟 `@media {…}`，
会让媒体查询整段失效，深色反而总是胜出。拆成两条完整规则。

**改 DOM 没清 JS。** 删掉 `span#quota-basis` 却留着写它的代码，
`renderTable()` 会在 `null` 上设属性然后静默中断，后面的渲染全不执行。
删节点就同时删写入方。

**用 `querySelector('span')` 写文字。** 按钮里若有嵌套元素，`textContent`
会把它们全冲掉。给目标元素单独 `id`，按 `id` 写。

**`backdrop-filter` 会成为 `position:fixed` 子元素的包含块。** 数据页顶栏加了
`backdrop-filter:blur(14px)` 做磨砂，结果顶栏内部那层 `inset:72px 0 0` 的
移动菜单以 74px 高的顶栏为包含块，算出来的高度只有 2px，点开菜单看不到
任何链接（`display`、`z-index` 全都正常）。要么把菜单移出顶栏，要么给它
写死 `height:calc(100dvh - 72px)`。选后者，因为 `data-menu-open` 的
选择器依赖菜单是顶栏的后代。

**缓存欺骗。** 手写静态资源也会被 Chrome 缓存，改了样式看不到效果时，
`Network.setCacheDisabled` 加给 URL 加 `?v=时间戳` 再判断。

## 验证

改完必须真的跑起来看，语法检查不算。

```bash
# 本地实例（/tmp/prod-copy 是生产库副本，有真实数据）
lsof -ti:8899 | xargs -r kill -9
nohup env PMT_DATA=/tmp/prod-copy PMT_PORT=8899 python3 src/server.py > /tmp/srv.log 2>&1 & disown
cd /Users/craig/Documents/oh-pmt-api && python3 -m unittest discover -s tests
```

**改了 `src/*.py` 要重启实例**，Python 不会热重载。只改 `web/` 下的文件不用重启。

用 CDP 走无头 Chrome 截图和读计算值：

- 视口固定用 `Emulation.setDeviceMetricsOverride`，**不要用 `--window-size`**，
  这个环境下它会静默不生效，导致误判布局溢出
- 手机端验证同理，`{width: 390, height: 844, deviceScaleFactor: 2, mobile: true}`
- 主题验证用 `Emulation.setEmulatedMedia` 指定 `prefers-color-scheme`
- 只改颜色这种判断，读 `getComputedStyle` 比看截图可靠；也可以取像素采样

判断文字换行不要猜高度，用 `Range` 逐字符取 `getBoundingClientRect().top`
分组，能还原真实的每一行。

## 改之前的检查单

1. 新变量挂在正确的元素上（`[data-page="site"]` 还是 `:root`？）
2. 深色规则是不是写了两条选择器 + 一条 `@media`
3. 新样式表有没有 `[hidden]` 兜底
4. 卡片是不是保持透明、只留边框
5. 粘性条有没有半透明 + 模糊
6. 改动是否只碰必须碰的地方——没坏的不重构
7. 跑一遍 `/` `/plan` `/data` `/docs` `/login` `/admin`，控制台是否干净
   （`/assets/demo.mp4` 的 404 是预期内的，不用管）
8. `python3 -m unittest discover -s tests` 是否全绿
