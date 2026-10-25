# oh-pmt-api

把订阅制 AI 账号的额度封装成 OpenAI 兼容 API，通过 API Key 分发给多人使用。每个密钥可以独立限制积分用量、来源 IP 和可用模型。

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
- 成员体系：为每人设定每日与每周积分上限、优先使用的账号、可用模型
- **按上游真实积分计费**：直接读 `usage.credits`，不是按 token 估算
- **成员控制台**：成员用自己的密钥登录查看剩余额度、消耗曲线和可用模型
- 账号页按厂商分卡片，点进去看该厂商的账号，再点账号进入详情页（额度构成、近 14 天曲线、按模型明细、最近调用）
- 数据页：14 天柱状图、24 小时柱状图、Token 分布直方图、模型堆叠面积图、星期 × 小时热力图、按厂商与按成员排行
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

打开 `http://127.0.0.1:8791/` 进入管理界面。首次启动用 `./start.sh` 会自动生成并保存管理令牌。

数据库文件默认是 `data/oh-pmt-api.db`。从旧版 `qoder2api` 升级时，`data/qoder2api.db` 会在首次启动时自动改名接过来，然后自动迁移表结构：老的 `pat` 列变成 `credential`，所有账号的 `provider` 补成 `qoder`，唯一约束改成 `(provider, uid, region)`。两个文件同时存在时不动任何文件，以 `oh-pmt-api.db` 为准。

### 环境变量

| 变量 | 默认值 | 说明 |
|---|---|---|
| `PMT_HOST` / `Q2A_HOST` | `127.0.0.1` | 监听地址 |
| `PMT_PORT` / `Q2A_PORT` | `8791` | 监听端口 |
| `PMT_ADMIN_TOKEN` / `Q2A_ADMIN_TOKEN` | 空 | 管理接口令牌。为空时只允许本机访问管理接口 |
| `PMT_DATA` / `Q2A_DATA` | `./data` | 数据库目录 |
| `PMT_TRUST_FORWARDED` / `Q2A_TRUST_FORWARDED` | `0` | 设为 `1` 时从 `X-Forwarded-For` 读取客户端 IP。**只有前面有可信反向代理时才打开**，否则 IP 白名单可被伪造绕过 |
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

## 使用

### 接入账号

在 Qoder 的 Integrations 页面创建 Personal Access Token：

- 国际版 https://qoder.com/account/integrations
- 国内版 https://qoder.com.cn/account/integrations

令牌以 `pt-` 开头。在账号页点「接入账号」，选厂商与区域，粘贴令牌，点「校验并接入」。

PAT 本身是长期令牌，但它换来的 job token 只有 24 小时有效期，所以 PAT 会被本地保存用于自动续期。不要移除账号后到处散布 PAT。

### 分发给组员

在成员页新增成员，点模型标签选择可用模型（默认勾选 Flash 档），设定每日与每周积分上限。再在他那一行点「密钥」生成密钥。密钥只显示一次，生成后立即保存。

组员这样调用：

```bash
curl https://api.example.com/v1/chat/completions \
  -H "Authorization: Bearer sicau-PMT-..." \
  -H "Content-Type: application/json" \
  -d '{"model":"gfmodel","messages":[{"role":"user","content":"你好"}]}'
```

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

**共享账号席位本身可能违反服务条款。** 一个账号的订阅额度分给多人使用，和多人共用一个账号是同一种性质的行为。这一条不取决于本项目的实现方式。

## 与参考项目的差异

| 能力 | qoder2api-hub | 本项目 |
|---|---|---|
| 多厂商 | 单平台 | 账号模型与界面已分区，Qoder 已接入 |
| IP 白名单 | 无 | 按 Key 配置，支持 CIDR |
| 限额粒度 | 账号级 | 成员级，每日 + 每周积分 |
| 计费依据 | 账号级护栏 | 上游真实 credits |
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
src/guard.py      访问控制：IP 白名单、模型白名单、配额
src/convert.py    SSE 归一：拆 Qoder 的信封，直通 WorkBuddy 的原生 chunk
src/server.py     HTTP 服务与路由
web/assets/ui.css    设计系统：白金配色、明暗主题、动效
web/assets/charts.js 手写 SVG 图表（折线/柱状/环形/堆叠面积/直方图/热力图）
web/admin.html    管理后台
web/account.html  账号详情页
web/console.html  成员控制台
web/login.html    统一登录页
```

`CONTEXT.md` 记录了从源码提取的 Qoder 协议细节，包括端点、请求体结构、签名算法和已知的密码学弱点，改动协议相关代码前先读它。
