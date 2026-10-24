# oh-pmt-api

把订阅制 AI 账号的额度封装成 OpenAI 兼容 API，通过 API Key 分发给多人使用，并支持按 Key 限制积分用量、IP 来源和模型。

账号来源按厂商建模（`src/providers.py`）。Qoder 是当前唯一实现完整协议的厂商，
文档里这部分协议细节仍然有效；WorkBuddy / Cline / CommandCode 注册为待接入，
新增一家只需要在注册表里补一个适配器。

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
- 账号池轮询、故障冷却换号、token 自动刷新
- 生成 API Key 分发给多人
- **按 Key 限制 token 用量**（参考项目缺失）
- **按 Key 限制 IP 来源**（参考项目缺失）
- **按 Key 限制可用模型**（参考项目缺失，它的 `key_allows_model` 是全局的）
- OpenAI 兼容 `/v1/chat/completions` 和 `/v1/models`
- 管理界面：Anthropic 设计风格，衬线体

不做：

- 签到、活动领取
- Anthropic Messages 协议桥接
- Responses API
- 多区域自动选区（按账号所属区域路由即可）
- 组织级用量审计

## 技术选型

Python 3.9+，零第三方依赖（COSY 签名所需的 AES、RSA 用手写实现或移植 `qoder_sign.py`）。Web 层用标准库 `http.server` 还是引入轻量框架待定，需评估并发与 SSE 流的支持度。

数据存储用 SQLite（`sqlite3` 标准库），存账号、API Key、用量记录、限额配置。

## 界面风格

参考 Anthropic 官网设计：温暖的米白背景（`#faf9f5` 一类）、深色正文、克制的强调色。正文和标题都用衬线体（参考 Anthropic 用的 Copernicus / Tiempos 类风格，网页可用 Georgia、Palatino、`Songti SC` 兜底）。留白充足，不用重描边和阴影，靠字号与间距建立层次。
