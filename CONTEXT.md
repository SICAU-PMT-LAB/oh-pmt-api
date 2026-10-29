# oh-pmt-api

把订阅制 AI 账号的额度封装成 OpenAI 兼容 API，通过 API Key 分发给多人使用，并支持按成员限制成本额度（日 / 总）、按 Key 限制 IP 来源和模型。

账号来源按厂商建模（`src/providers.py`）。Qoder 与 WorkBuddy 已经实现完整协议，
下面 Qoder 那部分协议细节仍然有效，WorkBuddy 的协议单独记在 `### WorkBuddy` 一节；
Cline / CommandCode 注册为待接入，新增一家只需要在注册表里补一个适配器。

## 为什么做这个

sub2api 是通用的 AI 网关，但它不支持 Qoder 平台（`backend/internal/service/domain_constants.go` 的平台列表里只有 Anthropic/OpenAI/Gemini/Antigravity/Grok/Kimi/Zhipu/Deepseek/MiniMax 等，没有 Qoder）。Qoder 的接入协议是私有的，无法当作普通 OpenAI 兼容上游接进去。

参考项目 `shuishuipingan/qoder2api-hub` 实现了完整协议，但有两个缺口正好是本项目的核心需求：

1. **没有 IP 白名单**。对整个仓库 grep `allow_ip|ip_whitelist|trusted_ip` 零命中，也没有反向代理层的 `X-Forwarded-For` 信任校验。
2. **限额是账号级的，不是 Key 级的**。5 个护栏键全部 setattr 到 `Account` 对象上（`qoder_accounts.py:3553`），`key_id` 只用于用量归属统计，无法给每个用户 Key 设独立日限额。

它还有 31.7k 行代码，包含签到、活动、Anthropic 桥接、Web 看板等本项目不需要的功能。因此本项目是精简实现，只做分发所需的能力。

## 协议事实（已从可执行代码验证）

以下细节提取自官方扩展 `pi-provider-qoder-0.4.6`（2195 行，可运行 JS）和 `qoder2api-hub`（v1.7.1），两者互相印证。

### 双区域端点

| 区域 | 推理基址 | openapi 基址 | 管理页 |
|---|---|---|---|
| 国际版 | `https://api3.qoder.sh/` | `https://openapi.qoder.sh` | `https://qoder.com/account/integrations` |
| 国内版 | `https://gateway.qoder.com.cn/` | `https://openapi.qoder.com.cn` | `https://qoder.com.cn/account/integrations` |

### PAT 导入

`POST {openapi}/api/v1/jobToken/exchange`，body `{"personal_token": "pt-..."}`，返回 `{token, refresh_token, expires_at}`。

随后 `GET {openapi}/api/v1/userinfo`，带 `Authorization: Bearer {jobToken}`，返回 `{id, email, name}`。

这两个请求**不需要 COSY 签名**，只需 `Content-Type`、`Accept` 和 `Cosy-Version`、`Cosy-ClientType` 头。

### 凭证生命周期

PAT（`pt-`）本身是长期令牌，但换来的 job token（`jt-`）只有 24 小时，refresh token（`jrt-`）48 小时。刷新按前缀路由：`jrt-` 走 `POST {openapi}/api/v1/jobToken/refresh`，失败则用 PAT 重新 exchange。**长期运行必须实现刷新，否则账号 24 小时后失效。**

### 推理请求

`POST {base}/algo/api/v2/service/pro/sse/agent_chat_generation?FetchKeys=llm_model_result&AgentId=agent_common&Encode=1`

模型列表：`GET {base}algo/api/v2/model/list?Encode=1`

请求体是 Qoder 私有格式，不是 OpenAI 或 Anthropic 格式。关键字段：`request_id`、`session_id`、`chat_task: "FREE_INPUT"`、`agent_id: "agent_common"`、`messages`、`tools`、`parameters`、`chat_context`、`model_config`、`business`。system prompt 必须作为 messages 数组首个 `role: "system"` 消息注入，顶层 `system` 字段服务端会忽略。

请求体编码经过自定义 Base64 变体：标准 base64 后，把字符串按 `a = n//3` 切成三段并重排为 `S[n-a:] + S[a:n-a] + S[:a]`，再用自定义 64 字符表替换，`=` 换成 `$`。

### COSY 签名（最大难点）

`Authorization: Bearer COSY.{payload_b64}.{sig}`

生成流程：

1. 随机 16 位 AES key，AES-128-CBC 加密 `{uid, security_oauth_token, name, aid, email}` 得到 `info_b64`
2. 用 RSA 公钥（PKCS#1 v1.5）加密 AES key，得到 `cosy_key`
3. `payload_b64 = base64({version, requestId, info, cosyVersion, ideVersion})`
4. `sig = md5(payload_b64 + "\n" + cosy_key + "\n" + timestamp + "\n" + body_bytes + "\n" + sig_path)`
5. `sig_path` 是请求路径去掉 `/algo` 前缀

配套头：`Cosy-Key`、`Cosy-User`、`Cosy-Date`、`Cosy-Version`（官方 CLI 用 `1.1.38`）、`Cosy-Machineid`、`Cosy-Machinetoken`、`Cosy-Machinetype`、`Cosy-Machineos`、`Cosy-Clienttype`（`5`）、`Cosy-Bodyhash`（body 的 md5）、`Cosy-Bodylength`、`Cosy-Sigpath`、`Cosy-Data-Policy`（`disagree`）、`X-Request-Id`。

### 已知的密码学弱点（不要"加固"）

AES key 只有 64 bit 熵且 key == IV，RSA 公钥实测 1024 位。`qoder_sign.py` 明确记录这一点并拒绝单方面修改，理由是必须与服务端解包结果逐字节一致，改动即协议不兼容。

### WorkBuddy（腾讯 CodeBuddy）

推理接口本身就是 OpenAI 兼容的，不需要协议转换：`POST {api}/v2/chat/completions`，
请求体字段与 OpenAI 一致，响应是标准 SSE（`data: {...}` + `data: [DONE]`），
最后一帧的 `usage.credit` 是本轮消耗的积分。

| 区域 | 推理 / 签到 | 积分与用量 |
|---|---|---|
| 国内版 | `https://copilot.tencent.com` | `https://www.codebuddy.cn` |
| 国际版 | `https://www.workbuddy.ai` | `https://www.workbuddy.ai` |

真实差异只有三处，都在 `src/workbuddy.py` 里处理：

1. **客户端标识**。上游按请求头判断调用方，缺了返回 11128「Illegal API invocation from
   an unapproved channel」。需要 `X-Product-Code: codebuddy` 加 `X-Domain`（导出文件里的
   domain），`X-Machine-Id` 按 uid 做 uuid5 派生并保持稳定，其余 IDE 版本号上游不校验。
   国际版另有一道渠道校验：`X-Conversation-ID` / `X-Conversation-Request-ID` /
   `X-Conversation-Message-ID` 三个头必须同时出现。
2. **类型约束**。上游是 Go 服务：`role=developer` 会整条请求被拦，要先转成 `system`；
   object 形式的 `tool_choice` 反序列化失败，要换成等价的函数名字符串。
3. **客户端自称**。上游按「system 消息开头那段」匹配客户端模板，命中就整条请求
   返回 11128。实测规律：只在开头匹配（前面多一个空格、空行或别的句子就不命中），
   放在 user 消息里从不命中，与模型和 tools 都无关。已确认会被拦的写法包括
   `You are Claude Code, Anthropic's official CLI` 和
   `You are an AI agent powered by DeepSeek Harness`（后者是 dsh 的模板）。
   处理方式是只改写开头这段为等价的角色描述，正文一字不动；正文里出现同样的
   句子不会被拦，所以也不改，避免动到用户的 prompt。

凭证是官方客户端导出的文件，两种形状都认：`workbuddy-<uid>.json` 是
`{account, auth}` 对象，`cockpit-<uid>.json` 是这种对象组成的数组。区域由 `auth.domain`
决定，不看界面上的选择。续期走 `POST {api}/v2/plugin/auth/token/refresh`，带
`X-Refresh-Token` 与 `X-Auth-Refresh-Source: plugin`，返回 `expiresIn`（秒）。

思考档位：上游认 `reasoning_effort`（`none` / `low` / `medium` / `high`），
**不传就完全不思考**——同一道题实测不传时 0 个思考字符，传任意档位都有。
所以网关必须透传，不能像 Qoder 那样按模型白名单裁剪。客户端的
`thinking` / `reasoning` 对象也要原样带上，`{"type": "enabled"}` 本身就是
一个档位信息（映射成 medium）。

积分：`POST {web}/billing/meter/get-user-resource-summary`，返回 `TotalCredits` 与
`CreditGroups`（`plan_base` / `plan_bonus` / `addon` / `benefit`）。归成三块上报：
套餐 = plan_base + plan_bonus，赠送 = addon + benefit，个人号没有共享池。

签到：国内版 `POST {api}/v2/billing/meter/daily-checkin`（先查
`checkin-activity-status`，已签就不重复提交），国际版没有这个活动，返回「签到活动未开放」。

模型目录：上游没有公开的列表接口（官方插件读的是下发到客户端的产品配置），所以
`CATALOG` 是一张人工维护的表——2026-10-25 用真实账号逐个发过对话确认可用的集合。
账号套餐不同，可用集合会有出入，缺模型时按需增删。

## 风控边界（必须写进文档告知使用者）

参考项目 `docs/risk-control-playbook.md` §0 的三条硬约束，本项目沿用同一判断：

1. **队列是模型级的**，多账号不增加队列容量
2. **数据中心 IP 无法用协议头伪装成住宅 IP**。若上游按 ASN 或地理做风控，唯一解是换出口，不是换头
3. **设备身份是派生的"稳定假设备"**，能避免被判爬虫，但不等于官方客户端

因此风险排序是：出口 > 身份 > 调度 > 请求整形。本项目的服务器是腾讯云机房 IP，比家用宽带暴露面大。

## 功能边界

做：

- 多厂商账号模型：账号带 provider 列，调度不会跨厂商
- PAT 导入多个 Qoder 账号（国际版 / 国内版分别管理）
- 账号文件导入多个 WorkBuddy 账号（workbuddy / cockpit 两种导出文件）
- 账号池轮询：`pick_account` 在同一个事务里选中并把 `last_used_at` 推到当前
  时刻，并发请求各拿各的账号；时间戳必须在 `BEGIN IMMEDIATE` 之后取，
  否则并发写入的时间几乎相同，排序退化成按 id
- 故障冷却换号、token 自动刷新
- SSE 流一旦开写就不能再补发完整 HTTP 响应（`_error` 会检查
  `_stream_started`，改发一条 SSE 错误帧）；否则 `[DONE]` 后面会粘上
  `HTTP/1.1 500 ...` 报文，客户端把它当数据解析
- 上游 chunk 的 `finish_reason` 空字符串要归一成 `null`：OpenAI 语义是
  中间帧为 null、末帧才有值，按 finish_reason 判断流结束的客户端
  （DeepSeek Harness）遇到 `""` 会判定流未结束并重试整轮
- 冷却只针对账号自身的问题（凭证失效、限流、额度耗尽）；请求内容被上游
  拒绝（如 11128 渠道校验）不冷却账号，也不重试换号——换账号重发一样被拒
- 生成 API Key 分发给多人
- **按成员限制成本额度**（参考项目缺失）：日额度 + 周额度，单位元，口径同 opencode Go
- **按 Key 限制 IP 来源**（参考项目缺失）
- **按 Key 限制可用模型**（参考项目缺失，它的 `key_allows_model` 是全局的）
- 模型对使用者只有一个名字，后面有几家厂商是网关内部的事
- OpenAI 兼容 `/v1/chat/completions` 和 `/v1/models`
- 管理界面：白底金边 / 黑底金边双主题，Serif 标题配等宽数字

### 公开数据看板的口径

`/data` 是给外人看的用量看板，所有数字都不带可识别信息。几条容易踩的：

- **市场份额按模型作者（lab）分组，不按上游厂商。** 按厂商等于公开
  「我们买了哪几家额度」，而作者是模型本身的公开信息。识别表在
  `src/labs.py`，认不出来的落到「其他」。早先的分类看板里还有一节
  「厂商排名」，直接泄露了账号池的构成，已经整节删掉，接口也不再返回
  `vendors` 字段。
- **排行榜有两个窗口**：`tokens_week`（最近 7 天）与 `tokens_day`（最后一天），
  页面上的「每周 / 每日」切的是同一批模型的这两个值。`change_pct` 用前后两个
  7 天窗口比；上周为 0 时不给百分比、只标「新」——0 做分母算不出涨幅。
- **两个单价口径，不要混。** `cost_per_mtok` 是牌价加权（输入 0.9 : 输出 0.1），
  只用来看「这个模型官方定多贵」；`effective_mtok` 是实付总额 ÷ 总 token，
  把缓存命中也算进去了。看板上的「Token 成本」用后者，否则会得出与实际
  相反的结论：DeepSeek-V4.1-Flash 牌价加权 ¥1.30 看起来比 GLM-5.3-Flash 的
  ¥1.00 贵，但它 96% 的输入命中缓存（¥0.02/M），实付只有 ¥0.092/M。
- **Token 成本的排行按请求数过滤，不按 token 量。** 单价是比值，请求太少
  时一个特例就能把均价推到任意值（Auto 四个请求算出 ¥7.81）。按 token
  量筛会错杀真实在用的模型：GLM-5.3-Flash 有 133 个请求但单次会话很短，
  总共才 8 万 token。
- **会话是近似值**：同一成员同一模型相隔 30 分钟内的请求算同一次会话。
  没有会话 id 可用，这是唯一能做的切分。

### 计费口径

每个模型一张 per 1M tokens 的四列价目表（输入 / 输出 / 缓存读 / 缓存写），
一次请求的成本 = 各类 token × 各自单价 / 1,000,000。`prompt_tokens` 已含命中
缓存的部分，计价时先扣掉，否则缓存 token 会被按原价再收一遍。成员额度也用
同一币种表示，百分比 = 已花 / 额度，到 100% 拒绝；额度按模型各算一份，见下。

**币种是人民币。** 单价取自各家官方中文定价页，而不是拿美元价乘汇率：
DeepSeek（空闲时段价，高峰两倍）、智谱 GLM、Moonshot Kimi、MiniMax、
阿里百炼（华北 2）、小米 MiMo（国内价）、腾讯混元（TokenHub 广州）、
阶跃星辰，都直接公布「元/百万 tokens」。官方按输入长度分档的取最低档。
没有人民币官方价的境外模型（Grok / Claude / GPT）按 7 元/美元折算，
只作为估算口径。

只有两个窗口：日额度（本地 0 点重置）与周额度（每周一 0 点重置，自然周）。
opencode 原生的三层窗口（滚动 5 小时 / 周 / 月）压成两层。

**额度按模型算，按份额累加。** 每个模型有自己的日/周额度，默认就是档位那一份
数（Go 每日 ¥5、Plus ¥15……），存在 `member_model_limits` 里的只有管理员手改过
的那些行。一次调用按 `本次金额 / 该模型额度` 折算成占了多少份额，各模型份额相加
就是池子用掉多少：

> a 额度 ¥100、b ¥100、c ¥100，d ¥10。a 花 ¥50 → 占 50%，再花 d ¥4 → 占 40%，
> 池子共 90%，只剩 10%：还能花 ¥10 在 a 上，或 ¥1 在 d 上。

档位那个数（¥100）是对外口径，真正卡住能花多少的是这个和。因此
`guard.quota_status` 里的 `pct_today` / `pct_total` 是各模型百分比之和而非
`已花 / 档位金额`，`used_*` 才是实花金额。整个 plan 通道看这个和是否到 100%，
单个模型看它自己那份是否到 100%。

为什么默认给每个模型整份额度而不是均分：均分之后花掉 1/N 就已经到 100%，
一用就锁死。全取整份时 `Σ(模型已花/模型额度)` 恰好等于 `总花费/档位额度`，
与引入按模型额度之前的行为完全一致，管理员把某个模型改小才是新增的节流能力。

额度设成 0 的模型没有分母，不计入这个和——花多少都不推进池子，等于免费。

档位对外的「每日 / 每周」就是那两个数字，周那个落在 `limit_total` 这一列上
（列名是积分时代的旧名），实现上按自然周回滚。

单价分两层：`src/pricing.py` 的内置表是默认值，管理端「定价」页改过的值存
`settings.model_prices` 覆盖它。查价顺序是上游 key → 对外 id → 显示名，
Qoder 的内部 key 在 `pricing.ALIASES` 里对到具体模型。上游自己报的积分照记，
但只用于对账。

### 两条计费通道

密钥有 `kind`：`plan` 与 `api`，两条通道的钱来自不同地方，互不相通。

- **plan**：按上面的档位百分比扣。每次请求的 `usage.cost` 计入该成员在该模型
  上的消耗，与 `api_credits` 无关。
- **api**：按预存余额扣。`api_credits` 是纯追加账本（入账正数、扣费负数），
  余额 = SUM(amount)；同一笔花费不再计入档位百分比。

`usage.channel` 记下这次请求走的哪条通道，而不是查询时 join `api_keys` 反推：
密钥删掉或事后改 `kind` 时，历史行就无法归属了。空值一律当 `plan`，与引入
这一列之前的行为一致。

因此统计也要按通道分开：`store.channel_stats()` 给两套小计，
`cost_since` / `cost_total` 传 `channel='plan'` 才算档位额度。

### 模型与厂商的两层关系

一个模型对外只有一个名字（`DeepSeek-V4.1-Flash`），背后可能有几家上游
（Qoder 的 `dfmodel`、WorkBuddy 的 `deepseek-v4.1-flash`）。两层判断：

1. **可用模型是最终依据**。成员/密钥的白名单里写的是**来源**，不是模型：
   `qoder:dfmodel` 与 `workbuddy:deepseek-v4.1-flash` 是两个独立选项。
   没勾到的来源一律不能用，即使优先账号属于那家厂商。
2. **优先账号决定顺序**。成员指定了优先账号且该厂商在可用范围里，
   就先试它的账号；它排队、额度不够或连不上时，换到下一家厂商重连
   同款模型，对客户端仍然是一个模型名、一条流。

白名单兼容四种写法，都有同一个“允许这个模型的某一家来源”的含义：

| 写法 | 含义 |
|---|---|
| `qoder:dfmodel` | 只允许 Qoder 这一家 |
| `dfmodel` | 旧数据，按上游 key 认（只命中这一家）|
| `DeepSeek-V4.1-Flash` | 不关心厂商，哪家都行 |
| `deepseek-v4.1-flash` | 合并后的内部 id，同上 |

管理端不展示厂商级的“模型列表”，而是每个模型下面列出各家来源供逐条勾选；
"全选"按模型算，会把该模型的每一家来源都勾上。

`/v1/models` 只列这个密钥真能跑到的来源；各家一个都不剩的模型不出现。

### 密钥的归属边界

密钥属于成员，管理员的权限到「看前缀 + 启停」为止。这不是 UI 层面的约束，
接口那一层就得拦住：

- `POST /api/keys`（代建）、`PATCH /api/keys/<id>/reset`（重置）、
  `DELETE /api/keys/<id>`（代删）对管理员一律 403 `admin_key_immutable`。
- `PATCH /api/keys/<id>` 只认 `enabled` 一个字段。白名单、归属、`kind`
  都改不了。
- 创建与删除只走成员自己的 `/api/me/keys`。

原因是明文：代建和重置都会在响应里返回一把可用的密钥。只要留任一条，
「库里只存 sha256」就变成安慰——拿到管理员会话的人能直接换出一把成员正在
用的密钥。停用不会产出明文，所以它是管理员唯一的硬手段；要换新密钥，
由成员自己删掉重建，效果和重置一样。

`Store.reset_key()` 留在 store 层但没有 HTTP 路由接它，别再给接上。

### 成员侧不暴露路由

厂商、上游 key（`dfmodel`）、白名单来源 spec（`workbuddy:deepseek-v4.1-flash`）
都是网关自己的路由信息。模型对成员只有一个名字，所以凡是成员/匿名可访问的
接口都只给那个名字：

- `/api/me` 的 `by_model` 与 `recent[].model` 先按 `public_model_names()` 翻成
  显示名再返回，同一个模型的两家上游合并成一行。`recent` 只回时间、token、
  成本、状态，不带 `account_id` / `credits` / `ip`。
- `/api/me/pricing` 按**模型**一行，不返回 `provider` / `upstream_key` / `spec`。
  同一个模型各来源价目不一致时给 `price_range` 区间，不拿其中一份冒充全部。
  管理端的 `/api/pricing` 仍按来源逐条列，勾选与对账靠它。
- `/api/public/plans`（套餐页、文档页，不需要登录）的模型行不带 `provider`。
- 错误响应不带上游原文：`upstream_status` / `upstream_error` / `upstream_rejected`
  只回固定文案，503 的 `no_account` 不写厂商名、区域与冷却剩余时间。
  原文进服务端日志（前缀 `[upstream]`）和账号的冷却原因。

不做：

- 活动中心的成长任务、抽奖、兑换（只做每日签到）
- Anthropic Messages 协议桥接
- Responses API
- 多区域自动选区（按账号所属区域路由即可）
- 组织级用量审计

## 技术选型

Python 3.9+，零第三方依赖（COSY 签名所需的 AES、RSA 用手写实现或移植 `qoder_sign.py`）。Web 层用标准库 `http.server` 还是引入轻量框架待定，需评估并发与 SSE 流的支持度。

数据存储用 SQLite（`sqlite3` 标准库），存账号、API Key、用量记录、限额配置。

## 界面风格

参考 Anthropic 官网设计：温暖的米白背景（`#faf9f5` 一类）、深色正文、克制的强调色。正文和标题都用衬线体（参考 Anthropic 用的 Copernicus / Tiempos 类风格，网页可用 Georgia、Palatino、`Songti SC` 兜底）。留白充足，不用重描边和阴影，靠字号与间距建立层次。
