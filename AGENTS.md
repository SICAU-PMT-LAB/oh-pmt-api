# 给 AI 协作者的项目说明

这份文件写给接手这个仓库的 AI。读它就够开工，细节再翻 `README.md`（面向使用者）
与 `CONTEXT.md`（协议事实与设计取舍）。

## 这是什么

把订阅制 AI 账号的额度封装成 OpenAI 兼容 API，按成员计费、按密钥限制来源 IP 与可用模型。
Python 3.9+，**零第三方依赖**，SQLite 存全部状态，Web 层是标准库 `http.server`。

上游目前两家：Qoder（私有协议，需 COSY 签名）与 WorkBuddy（OpenAI 兼容）。
新增一家在 `src/providers.py` 注册表里加适配器，不需要动调度与计费。

## 代码地图

```
src/server.py     HTTP 服务与全部路由，最大的一个文件。改接口从这里进
src/store.py      SQLite 全部读写与统计口径。表结构变更这里改
src/guard.py      准入：IP 白名单、模型白名单、日/总两个配额窗口
src/pricing.py    价格表（人民币/百万 token）+ lookup/cost_of。内置表 + settings 覆盖
src/convert.py    SSE 归一：拆 Qoder 信封，直通 WorkBuddy 原生 chunk
src/qoder.py      Qoder 上游：PAT 交换、令牌刷新、模型列表、对话、签到
src/workbuddy.py  WorkBuddy 上游：账号文件解析、刷新、对话、额度、签到
src/cosy.py       COSY 签名、手写 AES/RSA、自定义 Base64、设备指纹
src/providers.py  厂商注册表，新厂商从这里挂进去
src/labs.py       模型作者识别表：作者是模型的公开信息，与上游账号无关
web/assets/panel.css  后台面板样式：沿用官网外壳（居中竖栏 + 顶部导航 + 动态背景）
web/data.html     公开数据看板：照 opencode.ai/data 1:1 复刻
web/compare.html  模型对比详情页：/data/compare/<lab>/<model>/<lab>/<model>
web/assets/data.css  数据页与对比页共用的样式，从对方的构建产物裁出来
web/assets/data.js   数据页脚本：拉 /api/public/data、画堆叠图、悬停浮层、明暗切换
web/assets/compare.js 对比页脚本：拉 /api/public/compare、画雷达图与指标矩阵
web/assets/model-icons.svg  模型作者图标 symbol 表（排行榜卡片、对比卡、对比详情页）

web/assets/ui.css     旧设计系统（已不再被后台页面引用）
web/assets/charts.js  手写 SVG 图表，导出 fmtMoney/lineChart/hbarList 等
web/admin.html    管理后台（单页，七个 section 靠 nav 切换，含计费）
                  三个面板共用官网外壳：data-component=container + top + 动态背景
web/account.html  账号详情页      web/vendor.html  厂商详情页
web/member.html   成员详情页      web/console.html 成员控制台
web/login.html    统一登录页
tools/backfill_cost.py  历史缺价回填，默认 dry-run，加 --apply 才写
docs/design.md    前端设计约定：变量分层、主题三条选择器、背景层次、常见坑。改界面前先读
tests/            单元测试，改逻辑前后都跑（`python3 -m unittest discover -s tests`）
```

## 部署（重要：本地改完不等于上线）

**生产环境不在本机，必须显式部署。** 新开对话最容易漏掉这件事。

- 主机 `ubuntu@124.222.217.211`，密钥 `~/.ssh/ubuntu.pem`
- 站点 `https://api.hhy.homes`，nginx 反代到 `127.0.0.1:8791`
- 应用目录 `/opt/oh-pmt-api`，systemd 单元 `oh-pmt-api.service`
- 日志 `logs/q2a.log`（不是 journald，`journalctl` 只显示启停）
- 数据库 `/opt/oh-pmt-api/data/oh-pmt-api.db`
- 环境变量走 `/etc/qoder2api/env`，`PMT_HOST=127.0.0.1`、`PMT_PORT=8791`、`PMT_TRUST_FORWARDED=1`

**生产不是 git 仓库**，别在那边 `git pull`。部署靠 scp 覆盖文件。

### 部署流程

```bash
# 1. 本地验证
python3 -m unittest discover -s tests

# 2. 只传改动的文件，先在远端备份
ssh -i ~/.ssh/ubuntu.pem ubuntu@124.222.217.211 \
  'cd /opt/oh-pmt-api && cp src/server.py src/server.py.bak-$(date +%Y%m%d-%H%M%S)'
scp -i ~/.ssh/ubuntu.pem src/server.py ubuntu@124.222.217.211:/opt/oh-pmt-api/src/

# 3. 改了 .py 先在远端做语法检查，再重启
ssh -i ~/.ssh/ubuntu.pem ubuntu@124.222.217.211 \
  'cd /opt/oh-pmt-api && python3 -m py_compile src/server.py src/store.py \
   && sudo systemctl restart oh-pmt-api && systemctl is-active oh-pmt-api'

# 4. 验证
curl -s -o /dev/null -w "%{http_code}\n" https://api.hhy.homes/health
```

只改 `web/*.html` 或 `web/assets/*` 时**不用重启**，scp 覆盖即可。

### 部署前后必查

- **改数据库结构前先备份**：`cp data/oh-pmt-api.db data/oh-pmt-api.db.bak-$(date +%Y%m%d-%H%M%S)`，
  并确认 `-wal`/`-shm` 一起处理。生产库有真实成员与密钥，不是玩具数据。
- **看日志前先对时间**：`logs/q2a.log` 的 mtime 与报错里的行号可能都是重启前的旧内容。
  先 `ls -l logs/q2a.log` 和 `systemctl show -p ActiveEnterTimestamp oh-pmt-api`，
  再决定那段 Traceback 是否与本次发布有关。`BrokenPipeError` 成对出现是普通 SSE 断连。
- **未登录访问 `/admin` 返回 302 是正常的**（跳登录页），不是部署失败。要验证页面内容
  需带会话，或改成验证 JSON 接口的状态码（`/api/...` 未登录返回 401 说明路由在）。
- **删除生产数据只能按主键**。绝不要 `DELETE ... WHERE member_id=?` 传一个临时变量的 id：
  SQLite 会回收 rowid，临时行删掉后那个 id 可能已属于别人。真实事故：一条这样的
  DELETE 清掉了 32 个密钥。

## 什么时候该问用户

默认直接动手，能自查就自查完再汇报。下面这些事**先问**，因为选错了要返工或造成损失：

1. **要不要部署到生产。** 本地改完、测试通过是一个节点；推到 `api.hhy.homes` 是另一个
   决定。除非用户这次明确说了「部署」，否则停在本地并汇报改了什么、怎么验证的。
2. **删除文件或数据**，包括清理测试数据、删备份、删账号、删密钥。清掉就没了。
3. **改限额、价格、成员权限**这类影响真金白银的数值。用户给的数字按原样落库，
   不要「顺手」改成看起来更合理的值。
4. **多个方案都说得通，且会改变最终做出来的东西**。给两三个选项和推荐项，让用户选；
   纯实现细节自己定，不要拿琐事打扰。
5. **需要动别的仓库或外部系统**（DNS、证书、第三方面板）。

反过来，这些**不用问**，直接做：读代码、跑测试、起临时实例验证、改本地文件、
写文档、看日志排查、可逆的本地实验。

## 这个项目的协作习惯

- **先复现，再动手。** UI 问题用临时实例（`PMT_DATA=/tmp/xxx PMT_PORT=88xx
  python3 src/server.py`）+ 直接写 sqlite 播种数据（`UPDATE members SET is_admin=1`
  才能登录后台），需要登录态就写个小反向代理注入 `q2a_session` cookie。
  改了 CSS 用无头 Chrome + CDP 读 `getComputedStyle` 断言，比截图猜颜色可靠。
- **验证要真跑，不要只看语法。** 历史教训：`node --check` 全过但页面实际在
  `money is not defined` 上白屏。前端改动至少确认页面能渲染出预期元素。
- **改完给出证据**：测试数量、哪个页面哪个元素变了、截图或数值。
  说「已完成」但没验证，等于没做。
- **金额一律人民币**，`fmtMoney` 负责显示精度。上游积分（credits）只用于对账，
  不参与计费，不要混用。
- **不要为假想问题加防御。** 没有第三方依赖、没有复杂部署拓扑，按现有写法加功能即可。

## 数据模型要点

- `members`：档位 `tier`（go/plus/pro/max）决定默认额度；`limit_daily` + `limit_total`
  是管理员给单个成员设的手工额度，单位元，**非 0 时覆盖档位**，0 表示按档位算
- `api_keys`：只存 `key_hash` + `key_prefix`；`kind` 区分两条计费通道——
  `plan` 按档位百分比扣减（走 usage 的 cost 累计），`api` 按 `api_credits` 余额扣减

**密钥只由成员自己建、自己删，管理员只能看前缀 + 启停。** 代码里看不到这条
纪律就会本能地补上「代建」与「重置」，而这两个动作都会把一把可用的密钥明文
交给管理员，使「库里只存 sha256」形同虚设。接口层面已经钉住：
`POST /api/keys`、`PATCH /api/keys/<id>/reset`、`DELETE /api/keys/<id>`
对管理员一律 403 `admin_key_immutable`，`PATCH /api/keys/<id>` 只认 `enabled`。
别把这几条口子重新打开。
- `api_credits`：API 通道的账本，余额 = `SUM(amount)`。没有可变余额字段，
  冲正靠写反向行，账本因此始终可审计
- `usage`：每行一次请求。`cost` + `cache_read_tokens` + `cache_write_tokens` 是计费真相；
  `credits` 只做上游对账。所有统计走 `SUM(cost)`
- `accounts`：带 `provider` 与 `region`，调度不跨厂商
- 表结构变更写在 `store.py` 的迁移里，首次启动自动执行；开发库是空的，
  改结构不用写数据迁移脚本，但生产库有真实数据，改之前先备份

## 已知的坑

- **管理员不能删也不能降权**，且没有 API 能设置 `is_admin`。想测管理员功能，
  在临时实例上做，别在生产上造临时管理员（清理时风险极高）。
- **协议代码不要「加固」。** `cosy.py` 里 AES key 只有 64 bit 熵、key == IV、
  RSA 1024 位，这些都是为了与服务端逐字节一致，改了就不通。
- **SSE 一旦开写不能再补发 HTTP 响应**，`_error` 会检查 `_stream_started`。
- **WorkBuddy 的 `reasoning_effort` 必须透传**，不传上游完全不思考；它也不能像
  Qoder 那样按模型裁剪。客户端自称（system 消息开头）命中模板会被拒，只改开头那段，
  正文不动。
- **没有价目表条目的模型会落到 `DEFAULT_PRICE`**（¥3/¥10），计费不准但不会崩。
  遇到陌生模型先补 `pricing.py` 的表。
- 历史零成本行可用 `tools/backfill_cost.py` 回填，先 dry-run 看总额再 `--apply`。
- **数据页的市场份额按「模型作者」分组，不是按上游厂商。** 按厂商会泄露
  「我们买了哪几家额度」，而作者是模型本身的公开信息。`src/labs.py` 认不出来
  的落到「其他」。
- **厂商排名按上游厂商聚合，厂商名会出现在公开接口里。** 「市场份额」那节
  一度按模型作者分组，现在改成厂商。`usage` 只有 `account_id`，厂商要顺着
  账号表查；账号删掉后的历史行归「未归属」，不按模型名猜——猜错了会把
  「网关路由到别家」的历史算到错的厂商头上。
- **`data.css` 是裁出来的，不要按官网那套审美去改**。改前用无头 Chrome 读
  `getBoundingClientRect()` 对照 opencode.ai/zh/data，1440/700/420 三个宽度都要看。
