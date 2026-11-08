<div align="right">
  <a title="English" href="README.md"><img src="https://img.shields.io/badge/-English-545759?style=for-the-badge" alt="English"></a>
  <a title="简体中文" href="README_CN.md"><img src="https://img.shields.io/badge/-%E7%AE%80%E4%BD%93%E4%B8%AD%E6%96%87-A31F34?style=for-the-badge" alt="简体中文"></a>
</div>

<div align="center">

# 🔀 香港城市大学（东莞）GPT 服务网关

**把学校的 GPT 服务包成 OpenAI 兼容接口，并镜像整站。**

![WakaTime](https://wakatime.com/badge/user/a2c9dcda-13d4-4457-81cb-c15737459130/project/c78a1d4a-64a3-494a-b1d0-1d69928c5131.svg) ![License](https://img.shields.io/badge/license-Apache%202.0-green) ![Python](https://img.shields.io/badge/python-3.10%2B-blue) [![](https://img.shields.io/badge/Sponsor-f43f5e?logo=buymeacoffee&logoColor=white)](https://xa.2xrr.com/cashier/0e4c816c785bac0055)

</div>

## ✨ 功能特性

### 🔐 登录

- 学号密码自动登录
- 链路：`login_data` → `init_code` → `rest_token` → `/oauth/authorize` → `loginSSO` → token
- 三种模式：`auto`、`password`、`token`
- token 失效自动重登；后台每 10 分钟探活，并发请求只触发一次登录

### 🌐 OpenAI 兼容接口

| 接口 | 说明 |
| --- | --- |
| `POST /v1/chat/completions` | 流式与非流式，支持 `stream_options.include_usage` |
| `GET /v1/models` | 模型列表 |
| `POST /v1/responses` | 流式与非流式 |
| `GET` / `DELETE /v1/responses/{id}` | 回读或删除已存响应 |

- 模型：`gpt-5.6-terra`、`gpt-5.3codex`、`gpt-5chat`、`gpt-4o`、`gpt-4`
- 可传 `search_enabled`、`library_enabled`、`thinking_enabled`
- 支持 `previous_response_id` 和 CORS 预检

### 🧵 多轮

- 传 `X-Conversation-Id` 头或请求体 `conversation_id` 时按该 id 串联，会话之间完全隔离
- 不传时每个请求都是独立会话，网关不保留任何跨请求状态，多用户之间天然隔离
- `conversations.auto_chain: true` 会让网关按消息内容猜会话；两个用户开场白相同时会落进同一条会话，**多用户场景不要开**
- 会话映射落盘到 `data/conversations.json`，重启后仍能接上

### 🧹 会话清理

- 定时删除由本服务发起的会话；在学校网页里自己开的会话不受影响
- 按闲置时长或创建时长触发，见 `conversations.auto_delete`

### 🖥️ 整站反代

- 根路径即原版界面
- 转发 `/api/*` 时由服务端注入 `Authorization`
- 页面用口令 cookie 保护，`/v1` 用 API key

### 🕵️ 请求头

- 出站请求带浏览器的 UA、`sec-ch-ua`、`sec-fetch-*`、`referer`、`origin`
- 走 HTTP/2，`accept-encoding` 按本机可解算法声明

## 🚀 部署与运行

### 1. 安装

```bash
git clone <your-fork> CityUDG-GPT-Service
cd CityUDG-GPT-Service
python3 -m venv .venv           # Windows: python -m venv .venv
source .venv/bin/activate       # Windows: .venv\Scripts\activate
pip install -r requirements.txt
```

### 2. 配置

```bash
cp config.example.yaml config.yaml
cp .env.example .env
chmod 600 .env
```

`.env` 放凭据和访问控制：

```ini
# --- 校园账号 ---
CITYU_ACCOUNT=你的学号
CITYU_PASSWORD=你的密码

# --- 固定 token（token 模式）---
GPT_TOKEN=
GPT_USER_ID=
GPT_ACCOUNT_ID=

# --- 访问控制 ---
PROXY_PASSWORD=
OPENAI_API_KEY=
```

`config.yaml` 放行为：

```yaml
server:
  host: "127.0.0.1"
  port: 8787
  expose_docs: false
  debug_headers: false

auth:
  mode: "auto"            # auto | password | token
  keepalive_interval: 600

openai:
  default_model: "gpt-4o"
  owned_by: "system"

proxy:
  enabled: true
  password: "${PROXY_PASSWORD}"
  api_key: "${OPENAI_API_KEY}"

browser:
  impersonate: true
  http2: true
  referer_path: "/conversation-mode"
  user_agent: ""
  sec_ch_ua: ""

conversations:
  persist: true
  store_file: "data/conversations.json"
  auto_chain: false       # 按消息内容猜会话；多用户场景不要开
  auto_delete:
    enabled: false
    idle_seconds: 3600      # 闲置多久后删除；0 = 不按闲置删
    max_age_seconds: 0      # 创建后多久强制删除；0 = 不限
    sweep_seconds: 300      # 多久扫一次
```

### 3. 验证

```bash
python run.py serve                 # 开一个终端

python run.py login                 # 跑一遍密码链，写入 data/token.json
python run.py check                 # 校验缓存 token
python run.py models                # 确认能列出上游模型
```

```bash
curl -s http://127.0.0.1:8787/v1/chat/completions \
  -H "Content-Type: application/json" \
  -H "Authorization: Bearer $OPENAI_API_KEY" \
  -d '{"model":"gpt-4o","messages":[{"role":"user","content":"你好"}]}'
```

## 🔒 对外部署

1. `server.host` 改成 `0.0.0.0`
2. 设 `OPENAI_API_KEY` 和 `PROXY_PASSWORD`
3. TLS 在网关前终止

```bash
python run.py serve
```

日常检查：

```bash
curl -s localhost:8787/healthz
python run.py check
```

## 🖥️ 命令

| 命令 | 作用 |
| --- | --- |
| `python run.py serve` | 启动网关（默认） |
| `python run.py login` | 跑密码链，写入 `data/token.json` |
| `python run.py check` | 校验缓存 token |
| `python run.py token` | 打印当前 token |
| `python run.py models` | 列出上游模型 |

## 🏗️ 技术栈

- **运行时**：Python 3.10+
- **HTTP**：FastAPI + uvicorn，httpx 走 HTTP/2
- **配置**：PyYAML 6，`.env` 自行解析
- **编码**：brotli、zstandard
- **存储**：两个内存 LRU

## 📂 项目结构

```
CityUDG GPT Service/
├── run.py                          # 入口：serve / login / check / token / models
├── config.yaml                     # 行为配置（已 gitignore）
├── config.example.yaml             # 配置模板
├── .env.example                    # 凭据与访问控制模板
├── requirements.txt
├── src/cityudg_gpt/
│   ├── server.py                   # 应用装配、口令页、运维接口、巡检
│   ├── openai_api.py               # /v1/chat/completions、/v1/models
│   ├── responses_api.py            # /v1/responses
│   ├── webproxy.py                 # 整站透传
│   ├── upstream.py                 # /api/dify/chat 客户端与 SSE 解析
│   ├── auth.py                     # 密码链与 token 缓存
│   ├── browser.py                  # 浏览器请求头
│   ├── config.py                   # 配置加载
│   └── errors.py                   # 错误文案
├── data/                           # token.json（已 gitignore）
├── LICENSE
├── README.md                       # 英文文档
└── README_CN.md                    # 中文文档
```


<details>
<summary>🔌 接口清单</summary>


网关对外：

| 方法 | 接口 | 用途 |
| --- | --- | --- |
| `POST` | `/v1/chat/completions` | Chat Completions |
| `GET` | `/v1/models` | 模型列表 |
| `GET` | `/v1/models/{id}` | 单个模型 |
| `POST` | `/v1/responses` | Responses |
| `GET` | `/v1/responses/{id}` | 回读已存响应 |
| `DELETE` | `/v1/responses/{id}` | 删除已存响应 |
| `GET` | `/healthz` | 存活检查，不鉴权 |
| `GET`/`POST` | `/__gate` | 口令页 |
| `GET` | `/__status` | token 与账号状态 |
| `POST` | `/__relogin` | 强制重登 |
| `*` | `/{path}` | 学校站点透传 |

上游业务：

| 方法 | 接口 | 用途 |
| --- | --- | --- |
| `GET` | `/api/getSsoInfo` | SSO 配置 |
| `POST` | `/api/loginSSO` | 授权码换 token |
| `POST` | `/api/loginAcc` | 账号密码登录 |
| `GET` | `/api/user/menus` | 探活 |
| `GET` | `/api/model/getModelSimpleList?modelType=1` | 模型列表 |
| `POST` | `/api/dify/chat` | 对话，`text/event-stream` |
| `GET` | `/api/chatHistory/getHisList` | 会话列表 |
| `GET` | `/api/chatHistory/getChatHistory` | 会话消息 |
| `GET` | `/api/chatHistory/removeChatHistory` | 删除会话 |

IdP：

| 方法 | 接口 | 用途 |
| --- | --- | --- |
| `GET` | `/api/public/bff/v1.2/frontend/login_data` | `clientId` |
| `GET` | `/api/public/bff/v1.2/one_time_login/init_code` | `sm2_code` |
| `POST` | `/api/public/bff/v1.2/rest_token` | OAuth2 password grant |
| `GET` | `/oauth/authorize` | 授权跳转 |

</details>

## ⚠️ 限制

- 不支持工具调用，`tools` 与 `tool_choice` 收下不生效
- 不支持图片与文件上传
- `temperature`、`max_tokens` 等采样参数被忽略
- `usage` 为估算值
- `previous_response_id` 映射只在内存，重启后失效
- TLS 指纹与浏览器不一致

## 📫 反馈

欢迎交流任何值得动手做的事。最方便的方式是下面的留言反馈页面。

[博客](https://www.040426.xyz) · [留言反馈](https://feedback.valerianblog.link) · [打赏](https://xa.2xrr.com/cashier/0e4c816c785bac0055)

如果这里的内容对你有一点帮助，可以[请我喝杯咖啡](https://xa.2xrr.com/cashier/0e4c816c785bac0055)。

<p align="center">
    <!-- 二维码：https://goqr.me/api/ -->
    <!-- 明暗双版本，避免暗色模式下二维码白得刺眼 -->
    <a href="https://xa.2xrr.com/cashier/0e4c816c785bac0055">
        <picture>
            <source media="(prefers-color-scheme: dark)" srcset="https://api.qrserver.com/v1/create-qr-code/?size=200x200&data=https%3A%2F%2Fxa.2xrr.com%2Fcashier%2F0e4c816c785bac0055&bgcolor=0d1117&color=e6edf3"/>
            <source media="(prefers-color-scheme: light)" srcset="https://api.qrserver.com/v1/create-qr-code/?size=200x200&data=https%3A%2F%2Fxa.2xrr.com%2Fcashier%2F0e4c816c785bac0055&bgcolor=ffffff&color=000000"/>
            <img width="100" src="https://api.qrserver.com/v1/create-qr-code/?size=200x200&data=https%3A%2F%2Fxa.2xrr.com%2Fcashier%2F0e4c816c785bac0055&bgcolor=ffffff&color=000000" alt="打赏二维码" title="打赏二维码"/>
        </picture>
    </a>
</p>


## 📄 开源许可证

本项目基于 [Apache 2.0 License](LICENSE) 协议发布。

---

<div align="center">
<b>学费都交了，给我用用模型怎么了。🔀</b>
</div>
