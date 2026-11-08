<div align="right">
  <a title="English" href="README.md"><img src="https://img.shields.io/badge/-English-A31F34?style=for-the-badge" alt="English"></a>
  <a title="简体中文" href="README_CN.md"><img src="https://img.shields.io/badge/-%E7%AE%80%E4%BD%93%E4%B8%AD%E6%96%87-545759?style=for-the-badge" alt="简体中文"></a>
</div>

<div align="center">

# 🔀 CityUDG GPT Service

**Turns the CityUHK (Dongguan) GPT service into an OpenAI-compatible API, plus a mirror of the original site.**

![WakaTime](https://wakatime.com/badge/user/a2c9dcda-13d4-4457-81cb-c15737459130/project/c78a1d4a-64a3-494a-b1d0-1d69928c5131.svg) ![License](https://img.shields.io/badge/license-Apache%202.0-green) ![Release](https://img.shields.io/github/v/release/zyk121381/CityUDG-GPT-Service?style=flat)
 ![Python](https://img.shields.io/badge/python-3.10%2B-blue) [![](https://img.shields.io/badge/Sponsor-f43f5e?logo=buymeacoffee&logoColor=white)](https://xa.2xrr.com/cashier/0e4c816c785bac0055)

</div>

## ✨ Features

### 🔐 Sign-in

- Student ID and password, no manual steps
- Chain: `login_data` → `init_code` → `rest_token` → `/oauth/authorize` → `loginSSO` → token
- Three modes: `auto`, `password`, `token`
- Re-signs in when the token dies; probes every ten minutes, and concurrent requests share one sign-in

### 🌐 OpenAI-compatible API

| Endpoint | Notes |
| --- | --- |
| `POST /v1/chat/completions` | streaming and non-streaming, honours `stream_options.include_usage` |
| `GET /v1/models` | model list |
| `POST /v1/responses` | streaming and non-streaming |
| `GET` / `DELETE /v1/responses/{id}` | read back or drop a stored response |

- Models: `gpt-5.6-terra`, `gpt-5.3codex`, `gpt-5chat`, `gpt-4o`, `gpt-4`
- Accepts `search_enabled`, `library_enabled`, `thinking_enabled`
- Supports `previous_response_id` and CORS preflight

### 🧵 Multi-turn

- Pass `X-Conversation-Id` or a `conversation_id` body field to pin a conversation; separate ids stay fully isolated
- Without one, every request is its own conversation and the gateway keeps no cross-request state, so users are isolated by construction
- `conversations.auto_chain: true` makes the gateway guess the conversation from message content; two users with the same opening line land in the same conversation, so **leave it off for multi-user setups**
- The conversation map is stored in `data/conversations.json` and survives a restart

### 🧹 Conversation cleanup

- Periodically deletes conversations this service started; conversations opened in the school web UI are left alone
- Triggered by idle time or age, configured under `conversations.auto_delete`

### 🖥️ Site mirror

- The gateway root serves the original interface
- `/api/*` is forwarded with `Authorization` injected server-side
- The page sits behind a password cookie; `/v1` uses an API key

### 🕵️ Request headers

- Outbound requests carry the browser set: UA, `sec-ch-ua`, `sec-fetch-*`, `referer`, `origin`
- HTTP/2, with `accept-encoding` limited to what the build can decode

## 🚀 Getting Started

### 1. Install

```bash
git clone <your-fork> CityUDG-GPT-Service
cd CityUDG-GPT-Service
python3 -m venv .venv           # Windows: python -m venv .venv
source .venv/bin/activate       # Windows: .venv\Scripts\activate
pip install -r requirements.txt
```

### 2. Configure

```bash
cp config.example.yaml config.yaml
cp .env.example .env
chmod 600 .env
```

`.env` holds credentials and access control:

```ini
# --- Campus account ---
CITYU_ACCOUNT=your_student_id
CITYU_PASSWORD=your_password

# --- Static token (token mode) ---
GPT_TOKEN=
GPT_USER_ID=
GPT_ACCOUNT_ID=

# --- Access control ---
PROXY_PASSWORD=
OPENAI_API_KEY=
```

`config.yaml` holds behaviour:

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
  auto_chain: false       # guess conversations from content; off for multi-user
  auto_delete:
    enabled: false
    idle_seconds: 3600      # delete after this much idle time; 0 = off
    max_age_seconds: 0      # hard delete after this age; 0 = no limit
    sweep_seconds: 300      # how often to scan
```

### 3. Verify

```bash
python run.py serve                 # in one terminal

python run.py login                 # run the password chain, write data/token.json
python run.py check                 # validate the cached token
python run.py models                # confirm the upstream model list
```

```bash
curl -s http://127.0.0.1:8787/v1/chat/completions \
  -H "Content-Type: application/json" \
  -H "Authorization: Bearer $OPENAI_API_KEY" \
  -d '{"model":"gpt-4o","messages":[{"role":"user","content":"hi"}]}'
```

## 🔒 Exposing It

1. Set `server.host` to `0.0.0.0`
2. Set `OPENAI_API_KEY` and `PROXY_PASSWORD`
3. Terminate TLS in front

```bash
python run.py serve
```

Checks:

```bash
curl -s localhost:8787/healthz
python run.py check
```

## 🖥️ Commands

| Command | Purpose |
| --- | --- |
| `python run.py serve` | Start the gateway (default) |
| `python run.py login` | Run the password chain, write `data/token.json` |
| `python run.py check` | Validate the cached token |
| `python run.py token` | Print the current token |
| `python run.py models` | List upstream models |

## 🏗️ Tech Stack

- **Runtime**: Python 3.10+
- **HTTP**: FastAPI + uvicorn, httpx over HTTP/2
- **Config**: PyYAML 6, `.env` parsed in-house
- **Encoding**: brotli, zstandard
- **Storage**: two in-memory LRUs

## 📂 Project Structure

```
CityUDG GPT Service/
├── run.py                          # Entry point: serve / login / check / token / models
├── config.yaml                     # Behaviour (gitignored)
├── config.example.yaml             # Config template
├── .env.example                    # Credentials and access control template
├── requirements.txt
├── src/cityudg_gpt/
│   ├── server.py                   # App assembly, password gate, ops endpoints, keepalive
│   ├── openai_api.py               # /v1/chat/completions, /v1/models
│   ├── responses_api.py            # /v1/responses
│   ├── webproxy.py                 # Site passthrough
│   ├── upstream.py                 # /api/dify/chat client and SSE parsing
│   ├── auth.py                     # Password chain and token cache
│   ├── browser.py                  # Browser request headers
│   ├── config.py                   # Config loading
│   └── errors.py                   # Error messages
├── data/                           # token.json (gitignored)
├── LICENSE
├── README.md                       # English documentation
└── README_CN.md                    # Chinese documentation
```


<details>
<summary>🔌 API Surface</summary>


Gateway:

| Method | Endpoint | Purpose |
| --- | --- | --- |
| `POST` | `/v1/chat/completions` | Chat Completions |
| `GET` | `/v1/models` | Model list |
| `GET` | `/v1/models/{id}` | One model |
| `POST` | `/v1/responses` | Responses |
| `GET` | `/v1/responses/{id}` | Read a stored response |
| `DELETE` | `/v1/responses/{id}` | Drop a stored response |
| `GET` | `/healthz` | Liveness, unauthenticated |
| `GET`/`POST` | `/__gate` | Password page |
| `GET` | `/__status` | Token and account state |
| `POST` | `/__relogin` | Force a sign-in |
| `*` | `/{path}` | School site passthrough |

Upstream:

| Method | Endpoint | Purpose |
| --- | --- | --- |
| `GET` | `/api/getSsoInfo` | SSO configuration |
| `POST` | `/api/loginSSO` | Exchange the code for a token |
| `POST` | `/api/loginAcc` | Account/password login |
| `GET` | `/api/user/menus` | Liveness |
| `GET` | `/api/model/getModelSimpleList?modelType=1` | Model list |
| `POST` | `/api/dify/chat` | Conversation, `text/event-stream` |
| `GET` | `/api/chatHistory/getHisList` | Conversation list |
| `GET` | `/api/chatHistory/getChatHistory` | Messages in a conversation |
| `GET` | `/api/chatHistory/removeChatHistory` | Delete a conversation |

IdP:

| Method | Endpoint | Purpose |
| --- | --- | --- |
| `GET` | `/api/public/bff/v1.2/frontend/login_data` | `clientId` |
| `GET` | `/api/public/bff/v1.2/one_time_login/init_code` | `sm2_code` |
| `POST` | `/api/public/bff/v1.2/rest_token` | OAuth2 password grant |
| `GET` | `/oauth/authorize` | Authorize redirect |

</details>

## ⚠️ Limitations

- No tool calling; `tools` and `tool_choice` are accepted but ignored
- No image or file upload
- `temperature`, `max_tokens` and other sampling parameters are ignored
- `usage` is an estimate
- `previous_response_id` lives in memory and resets on restart
- The TLS fingerprint does not match a browser

## 📫 Feedback

Welcome to talk about anything worth building. The easiest way is the feedback page below.

[Blog](https://www.040426.xyz) · [Feedback](https://feedback.valerianblog.link) · [Donate](https://xa.2xrr.com/cashier/0e4c816c785bac0055)

If any of this helped you, [buy me a coffee](https://xa.2xrr.com/cashier/0e4c816c785bac0055).

<p align="center">
    <!-- QR code: https://goqr.me/api/ -->
    <!-- Light and dark versions, so it does not glare in dark mode -->
    <a href="https://xa.2xrr.com/cashier/0e4c816c785bac0055">
        <picture>
            <source media="(prefers-color-scheme: dark)" srcset="https://api.qrserver.com/v1/create-qr-code/?size=200x200&data=https%3A%2F%2Fxa.2xrr.com%2Fcashier%2F0e4c816c785bac0055&bgcolor=0d1117&color=e6edf3"/>
            <source media="(prefers-color-scheme: light)" srcset="https://api.qrserver.com/v1/create-qr-code/?size=200x200&data=https%3A%2F%2Fxa.2xrr.com%2Fcashier%2F0e4c816c785bac0055&bgcolor=ffffff&color=000000"/>
            <img width="100" src="https://api.qrserver.com/v1/create-qr-code/?size=200x200&data=https%3A%2F%2Fxa.2xrr.com%2Fcashier%2F0e4c816c785bac0055&bgcolor=ffffff&color=000000" alt="Donation QR code" title="Donation QR code"/>
        </picture>
    </a>
</p>


## 📄 License

Released under the [Apache 2.0 License](LICENSE).

---

<div align="center">
<b>I paid tuition. I'm taking the model with me. 🔀</b>
</div>
