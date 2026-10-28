# oh-pmt-api

把订阅制 AI 账号的额度封装成 OpenAI 兼容 API，通过 API Key 分发给多人使用。用量按 token × 单价折算成人民币计费，每个成员有日额度与总额度，来源 IP 与可用模型按密钥限制。

目前真实可用的接入来源是 **Qoder** 和 **WorkBuddy**；**Cline / CommandCode** 的账号模型、接口和界面已经按厂商分开，补上各自的适配器即可启用。

## 为什么不用现成的

sub2api 是通用 AI 网关，但不支持 Qoder 平台，它的平台常量里只有 Anthropic、OpenAI、Gemini、Antigravity、Grok、Kimi、智谱、DeepSeek、MiniMax 等。Qoder 的接入协议是私有的，无法当作普通 OpenAI 兼容上游接进去。

WorkBuddy 那一侧有人做过单账号反代（`workbuddy2api`，一个本地 FastAPI 进程）。本项目不依赖它：协议直接实现在 `src/workbuddy.py` 里，账号进的是同一个池子，能被同一套密钥、限额、IP 白名单和统计口径管住。

`shuishuipingan/qoder2api-hub` 实现了完整协议，但它没有 IP 白名单（全仓搜索 `ip_whitelist` 零命中），限额也是账号级的而不是 Key 级的（5 个护栏键都挂在 `Account` 对象上，`key_id` 只用于用量归属）。这两个缺口正好是本项目的核心需求，加上它有 31.7k 行代码包含大量不需要的功能，所以本项目是精简重写。

## 功能

- 多厂商账号池：同一个池里按厂商与区域分开管理，账号表带 `provider` 列
- 已实现两家的完整协议：Qoder（PAT）与 WorkBuddy（官方导出的账号文件），两家的国际版与国内版各自调度
- 批量导入：一次选中多个导出的账号文件，逐条回报成功与失败原因
- 账号池轮询（选中即占用，并发请求不会挤到同一个账号上），故障账号自动冷却，job token 到期自动续期
- 成员体系：为每人设定日额度与总额度（元）、优先使用的账号、可用模型
- **按 token × 官方单价计费**：输入/输出/缓存读/缓存写四列价，人民币，取自各家官方定价页；`prompt_tokens` 里已含的缓存部分先扣掉再计价
- **限额到 100% 就停服**：日额度本地 0 点重置，总额度不重置，任一窗口用满即 429
- **定价页可覆盖**：内置一张参考价表，逐条改单价后立即生效，不用重启
- **成员控制台**：成员用自己的密钥登录查看剩余额度、消耗曲线和可用模型
- 账号页按厂商分卡片，点进去看该厂商的账号，再点账号进入详情页（额度构成、近 14 天曲线、按模型明细、最近调用）
- **公开站点**：首页 `/`、套餐页 `/plan`、数据看板 `/data`、文档 `/docs`，都不需要登录，配色与排版对齐 opencode.ai
- **公开数据看板** `/data`：版式与动效对齐 opencode.ai/data——热门模型与独立用户两条堆叠图、排行榜（每周 / 每日两档）、每周留存、会话成本、Token 成本（实付均价，含缓存命中）、缓存比例、模型对比卡。对比卡点进去是独立的对照页 `/data/compare/…`，两侧列出单价、用量走势、缓存与留存。只统计聚合数字，成员以「成员 01」序号出现
- **套餐页** `/plan`：Go / Plus / Pro / Max 四档，按每日与每周预算换算各模型的预估请求数
- 后台数据页：14 天柱状图、24 小时柱状图、Token 分布直方图、模型堆叠面积图、星期 × 小时热力图、按厂商与按成员排行
- 明暗双主题：白底金边 / 黑底金边，右上角切换，选择记在本地
- 每个密钥独立配置 IP 白名单（支持 CIDR）
- OpenAI 兼容的 `/v1/chat/completions`（支持流式）和 `/v1/models`

## 部署

需要 Python 3.9 以上，没有第三方依赖。

```bash
git clone <本仓库>
cd oh-pmt-api
PMT_ADMIN_TOKEN=$(openssl rand -hex 24) PMT_PORT=8791 python3 src/server.py
```

打开 `http://127.0.0.1:8791/` 进入站点首页，管理员从右上角「登录」进入，登录后落在 `/admin`。首次启动用 `./start.sh` 会自动生成并保存管理令牌。

管理员账号用账号密码登录，不依赖管理令牌。管理员不能被停用、不能取消管理员身份、也不能删除：它是进后台的唯一钥匙，改掉之后已经没有界面能改回来。真的把数据库里的管理员弄坏了（停用或删行），本机直连 `http://127.0.0.1:8791/` 仍然放行管理界面，可以从那里改回来；从外部只能登录，不能靠本机通道绕过。

数据库文件默认是 `data/oh-pmt-api.db`。从旧版 `qoder2api` 升级时，`data/qoder2api.db` 会在首次启动时自动改名接过来，然后自动迁移表结构：老的 `pat` 列变成 `credential`，所有账号的 `provider` 补成 `qoder`，唯一约束改成 `(provider, uid, region)`。两个文件同时存在时不动任何文件，以 `oh-pmt-api.db` 为准。

### 环境变量

| 变量 | 默认值 | 说明 |
|---|---|---|
| `PMT_HOST` / `Q2A_HOST` | `127.0.0.1` | 监听地址 |
| `PMT_PORT` / `Q2A_PORT` | `8791` | 监听端口 |
| `PMT_ADMIN_TOKEN` / `Q2A_ADMIN_TOKEN` | 空 | 旧版的管理接口令牌，当前版本的管理后台已改为账号密码登录，不再读这个变量 |
| `PMT_DATA` / `Q2A_DATA` | `./data` | 数据库目录 |
| `PMT_TRUST_FORWARDED` / `Q2A_TRUST_FORWARDED` | `0` | 设为 `1` 时读取 `X-Forwarded-For` **最右一段**作为客户端 IP。**只有前面有可信反向代理时才打开**；打开时也要求反代用 `$proxy_add_x_forwarded_for` 追加，否则最右段就是你反代的地址 |
| `PMT_COSY_VERSION` / `Q2A_COSY_VERSION` | `1.1.75` | 签名版本号，Qoder 客户端升级后可能需要调整 |
| `PMT_ACCESS_LOG` / `Q2A_ACCESS_LOG` | `0` | 设为 `1` 打印访问日志 |

### 反向代理

服务默认只监听本机，对外暴露需要经过 nginx：

```nginx
server {
    listen 443 ssl http2;
    server_name api.example.com;

    ssl_certificate     /etc/letsencrypt/live/api.example.com/fullchain.pem;
    ssl_certificate_key /etc/letsencrypt/live/api.example.com/privkey.pem;

    client_max_body_size 64m;

    location / {
        proxy_pass http://127.0.0.1:8791;
        proxy_http_version 1.1;
        proxy_set_header Host              $host;
        proxy_set_header X-Real-IP         $remote_addr;
        proxy_set_header X-Forwarded-For   $proxy_add_x_forwarded_for;
        proxy_set_header X-Forwarded-Proto $scheme;

        # 流式响应必须关缓冲，否则客户端收不到逐字输出
        proxy_buffering off;
        proxy_cache off;
        proxy_read_timeout 3600s;
        proxy_send_timeout 3600s;
    }
}
```

环境变量两套名字都认：`PMT_*` 优先，`Q2A_*` 作为老名字继续生效，线上已有的 systemd unit 和 env 文件不需要改就能升级。

启用反代后记得设置 `PMT_TRUST_FORWARDED=1`（老名字 `Q2A_TRUST_FORWARDED` 同样有效），否则所有请求的来源 IP 都会显示为 127.0.0.1，IP 白名单会失效。

取的是 `X-Forwarded-For` 最右一段，不是最左：`$proxy_add_x_forwarded_for` 拼出来是「调用方自带的值, 真实来源」，真实来源在最后。调用方自己塞进来的前缀改不了判定结果。上面这份配置里 `X-Real-IP` 是 nginx 覆盖写入的 `$remote_addr`，所以它在 `X-Forwarded-For` 缺失时也能作为回退。

## 使用

### 接入账号

在 Qoder 的 Integrations 页面创建 Personal Access Token：

- 国际版 https://qoder.com/account/integrations
- 国内版 https://qoder.com.cn/account/integrations

令牌以 `pt-` 开头。在账号页点「接入账号」，选厂商与区域，粘贴令牌，点「校验并接入」。

PAT 本身是长期令牌，但它换来的 job token 只有 24 小时有效期，所以 PAT 会被本地保存用于自动续期。不要移除账号后到处散布 PAT。

### 分发给组员

在成员页新增成员，设定日额度与总额度（单位元，0 表示不限），在「可用模型」里勾选要开放的模型。

同一个模型可能有好几家厂商都能跑（例如 DeepSeek-V4.1-Flash 在 Qoder 和 WorkBuddy
都有），但对外只有一个名字。成员页的每个模型下面会列出各家来源，逐个勾选：

- 勾几家就是允许走几家。只勾 WorkBuddy 的来源，请求就不会落到 Qoder 的账号上。
- 都勾上时，按「优先账号」所属的厂商先试；那家连不上或账号全在冷却，
  自动换另一家重连同款模型，对客户端就是一个模型名、一条流。
- 一个都不勾表示不限模型，任何账号池都能用。

再在他那一行点「密钥」看他的密钥。**管理员只能看前缀、只能停用，不能代建、重置或删除。**
密钥由成员自己在控制台创建与删除，明文只在创建那一刻给到成员。原因是不想让
「库里只存 hash」这层保护被后门抹掉：代建与重置都会把一把可用的密钥交到管理人
手上，那和把明文存进库里在效果上没区别。一把密钥泄露了，管理员能停用它；要
换新的，由成员自己删掉重建。

库里只存密钥的 sha256，不存明文，所以列表里只有前缀 `sicau-PMT-xxxxxx…`。

服务端也把这个边界钉住：`POST /api/keys`、`PATCH /api/keys/<id>/reset`、
`DELETE /api/keys/<id>` 对管理员一律返回 403 `admin_key_immutable`，
`PATCH /api/keys/<id>` 只认 `enabled` 一个字段。管理员手上拿不到任何可用的密钥，
停用是他唯一的硬手段。这是刻意的取舍——数据库文件被读走时，泄露的只是一堆
hash，不是全部成员密钥和全部上游账号令牌；同理，管理员账号被拿到也换不出成员的密钥。

组员这样调用：

```bash
curl https://api.example.com/v1/chat/completions \
  -H "Authorization: Bearer sicau-PMT-..." \
  -H "Content-Type: application/json" \
  -d '{"model":"DeepSeek-V4.1-Flash","messages":[{"role":"user","content":"你好"}]}'
```

模型名写显示名、上游 key（`dfmodel`）或 `厂商:上游key`（`qoder:dfmodel`）都认。

在 pi、OpenAI SDK、各类客户端里填写同一个 base URL 和密钥即可。

### 接入 WorkBuddy

WorkBuddy（腾讯 CodeBuddy）的凭证不是一个 token，而是官方客户端导出的账号文件。在账号页点「导入文件」，选厂商 WorkBuddy，然后选中文件（可多选）：

- `workbuddy-<uid>.json` —— 单账号对象
- `cockpit-<uid>.json` —— 数组，一个文件里可以有多条

文件原样提交即可，不用改名、不用合并。面板会逐条回报结果：哪条进来了、剩余多少积分，哪条失败了以及原因。同一个账号重复导入是覆盖，不会出现两条。

也可以把文件内容整个粘进「接入账号」的文本框，或直接调接口：

```bash
curl -X POST https://api.example.com/api/accounts/import \
  -H "Content-Type: application/json" \
  -b "q2a_session=<管理员会话>" \
  -d '{"provider":"workbuddy","files":[{"name":"cockpit-x.json","content":"<文件原文>"}]}'
```

区域由文件里的 `domain` 决定，不看界面上选的：`codebuddy.cn` 是国内版，其余是国际版。两套凭证互不通用。

签到只有国内版有（每日 100 积分，`/v2/billing/meter/daily-checkin`），国际版返回「签到活动未开放」。

### 客户端接入注意

思考档位要客户端自己声明才发得出去。以 DeepSeek Harness 为例，`models` 里
必须写 `reasoningEfforts`，否则选择器上不会出现档位，请求里也不会带参数：

```yaml
providers:
  oh-pmt:
    apiKeyEnv: OH_PMT_API_KEY
    api: openai-completions
    baseURL: https://your.host/v1
    compat:
      supportsReasoningEffort: true
      thinkingFormat: openai      # 发 reasoning_effort 字段
    models:
      - id: DeepSeek-V4.1-Flash
        contextWindow: 262144
        maxTokens: 32768
        reasoningEfforts:          # 不写这段就没有档位可选
          off:
          low: low
          medium: medium
          high: high
```

### 思考强度

客户端怎么写都行，网关统一收口成上游认的 `reasoning_effort`：

```json
{"reasoning_effort": "high"}
{"thinking": {"type": "enabled"}}
{"thinking": {"effort": "low"}}
{"reasoning": {"effort": "medium"}}
```

档位是 `off` / `low` / `medium` / `high`，`max`、`minimal`、`default` 这类别名会被归一化。

WorkBuddy 上游**不传这个参数就完全不思考**，所以网关必须透传；Qoder 那边则相反，只在模型声明了档位集合时才下发，两边规则各自在适配器里实现。思考内容通过 `reasoning_content` 字段返回（流式在 `delta.reasoning_content`）。

### 计费与限额

每个模型有一张 **每 100 万 token** 的四列价目表（输入 / 输出 / 缓存读 /
缓存写，单位人民币元），一次请求的成本是

```
成本 = (输入 token × 输入价 + 输出 token × 输出价
       + 命中缓存 token × 缓存读价 + 写入缓存 token × 缓存写价) / 1,000,000
```

`prompt_tokens` 里已经包含命中缓存的部分，计价时会先扣掉再按输入价算，
不会重复收费；`completion_tokens` 已包含思考 token，不再额外加。上游没报
缓存明细时全部按输入价算，属于偏高估计，比漏账好。

单价取自各家**官方中文定价页**（2026-10 抓取），不是拿美元价乘汇率：

| 厂商 | 来源 | 口径 |
|---|---|---|
| DeepSeek | api-docs.deepseek.com | 空闲时段价，高峰为其两倍 |
| 智谱 GLM | docs.bigmodel.cn | 官方元价，按输入长度分档取最低档 |
| Moonshot Kimi | platform.kimi.com | 官方元价，K3 系列单收缓存写入 |
| MiniMax | platform.minimax.cn | 标准档，M3 取 ≤512k 的五折价 |
| 阿里通义千问 | help.aliyun.com | 华北 2（北京）地域价 |
| 小米 MiMo | mimo.mi.com | 国内定价 |
| 腾讯混元 | cloud.tencent.com | TokenHub 广州价 |
| 阶跃星辰 | platform.stepfun.com | 官方元价 |

没有人民币官方价的境外模型（Grok、Claude、GPT）按 7 元/美元折算，只作为
估算口径，真接入时按实际账单在定价页改。

成员的额度也用元表示，只有两个窗口：

| 窗口 | 重置 | 字段 |
|---|---|---|
| 日额度 | 每天本地 0 点 | `members.limit_daily` |
| 总额度 | 不重置 | `members.limit_total` |

`0` 表示不限。用量百分比 = 已花 / 额度，任一窗口到 100% 就拒绝请求
（HTTP 429，错误码 `quota_daily_exceeded` / `quota_total_exceeded`）。

单价有两层来源：`src/pricing.py` 里的内置表是默认值，管理界面「定价」页改过的
值存在 `settings.model_prices` 里覆盖它。查价按上游 key → 对外模型 id → 显示名
的顺序匹配，都查不到时用默认价并在定价页标成「默认价」。Qoder 的内部 key
（`dfmodel`、`qfmodel` 这类）在 `pricing.ALIASES` 里对到具体模型。

上游自己报的积分（`usage.credits`）仍然照记，但只用于对账，不参与限额。

### 加一家新厂商

`src/providers.py` 里每个厂商是一个适配器，需要实现：换凭据（`connect`）、续期（`refresh`）、列模型（`list_models`）、发请求（`build_chat_body` + `chat_stream`）、读额度（`fetch_quota`）。把 `available` 改成 `True` 并填上区域，界面上的「待接入」卡片就会变成可用的账号入口。

Qoder（`src/qoder.py`）与 WorkBuddy（`src/workbuddy.py`）已经实现完整协议；Cline 和 CommandCode 仍是 `PendingProvider`，调用 `connect` 会明确抛 `Unsupported`，界面上标「待接入」。

## 风险说明

这一节请认真读完再决定要不要分发给别人。

**协议是逆向后兼容的，不是官方接口。** 本项目复刻了 Qoder 客户端的 COSY 签名协议（AES-128-CBC 加密用户信息、RSA 加密对称密钥、MD5 计算签名）。理解这套协议的来源是官方扩展 `pi-provider-qoder` 的可执行代码和 `qoder2api-hub` 的实现，两者交叉验证过。它随时可能因为客户端升级而失效，届时需要更新签名版本号和实现。

**有被限制的风险。** 参考项目的风控文档给过三条硬约束，这里原样照搬：

1. 队列是模型级的，多账号不增加队列容量。
2. 数据中心 IP 无法用协议头伪装成住宅 IP。如果上游按 ASN 或地理做风控，唯一解是换出口，不是换头。
3. 设备身份是按账号派生的「稳定假设备」，能降低被判为爬虫的概率，但不等于官方客户端。

所以风险排序是：出口 > 身份 > 调度。把本项目部署在云服务器上（机房 IP）比家用宽带的暴露面更大。如果只是给身边几个人用，降低并发、限制每人每天的用量，比任何伪装都有效。

**密钥与令牌在库里是明文或可用的凭据。** API Key 现在只存 hash，但 `accounts` 表里的上游 `credential`（Qoder 的 PAT 原文、WorkBuddy 的账号文件）必须能解密使用，所以是明文。数据库文件启动时已被设为 `0600`，同机其他用户读不到；但备份、镜像、误传到别处的库文件仍然等于账号被拿走。备份时按密码来对待。

**共享账号席位本身可能违反服务条款。** 一个账号的订阅额度分给多人使用，和多人共用一个账号是同一种性质的行为。这一条不取决于本项目的实现方式。

## 与参考项目的差异

| 能力 | qoder2api-hub | 本项目 |
|---|---|---|
| 多厂商 | 单平台 | 账号模型与界面已分区，Qoder 已接入 |
| IP 白名单 | 无 | 按 Key 配置，支持 CIDR |
| 限额粒度 | 账号级 | 成员级，日额度 + 总额度 |
| 计费依据 | 账号级护栏 | token × 官方人民币单价，口径同 opencode Go |
| 成员控制台 | 无 | 有 |
| 模型限制 | 全局 | 按 Key 配置，界面点选 |
| 代码量 | 约 31.7k 行 | 约 2.2k 行 |
| 三方依赖 | 零 | 零 |
| Anthropic 协议 | 支持 | 不支持 |
| 签到与活动 | 支持 | 支持（仅 Qoder） |

## 开发

```bash
python3 -m unittest discover -s tests -v
```

代码结构：

```
src/providers.py  厂商注册表与适配器（新增厂商只需加一个类）
src/cosy.py       COSY 签名、AES/RSA、自定义 Base64、设备指纹派生
src/qoder.py      与 Qoder 上游通信：PAT 交换、令牌刷新、模型列表、对话、签到
src/workbuddy.py  与 WorkBuddy 上游通信：账号文件解析、令牌刷新、对话、额度、签到
src/store.py      SQLite：账号、Key、用量、设置、各种统计口径
src/pricing.py    人民币价目表与计费公式，内置表 + 管理端覆盖
src/guard.py      访问控制：IP 白名单、模型白名单、配额
src/convert.py    SSE 归一：拆 Qoder 的信封，直通 WorkBuddy 的原生 chunk
src/server.py     HTTP 服务与路由
web/assets/panel.css   后台面板样式：沿用官网外壳（居中竖栏 + 顶部导航 + 动态背景）
web/assets/ui.css      旧设计系统（已不再被后台页面引用）
web/assets/charts.js 手写 SVG 图表（折线/柱状/环形/堆叠面积/直方图/热力图）
web/admin.html    管理后台
web/account.html  账号详情页
web/member.html   成员详情页
web/vendor.html   厂商详情页
web/console.html  成员控制台
web/login.html    统一登录页
tools/backfill_cost.py  历史缺价回填，默认 dry-run
```

### 给 AI 协作者与接手的人

- [`AGENTS.md`](AGENTS.md)：代码地图、部署流程、什么时候该问用户、已知的坑。**改代码前先读**
- [`docs/部署.md`](docs/部署.md)：生产环境事实、三类改动的部署流程、回滚、看日志的规矩
- [`docs/本地调试.md`](docs/本地调试.md)：起临时实例、播种数据、注入登录态、无头 Chrome 断言
- [`docs/故障排查.md`](docs/故障排查.md)：按症状查，含计费、协议、数据事故
- [`docs/协议备忘.md`](docs/协议备忘.md)：Qoder 协议速查

`CONTEXT.md` 记录了从源码提取的 Qoder 协议细节，包括端点、请求体结构、签名算法和已知的密码学弱点，改动协议相关代码前先读它。
