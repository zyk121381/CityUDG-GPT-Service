from __future__ import annotations

import asyncio
import hashlib
import hmac
import logging
from contextlib import asynccontextmanager
from collections.abc import AsyncIterator
from typing import Any
from urllib.parse import parse_qs
from uuid import uuid4

import httpx
from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse, Response

from . import errors as err
from .auth import AuthError, AuthManager
from .config import Settings, load
from .openai_api import ConversationStore, ModelRegistry, ResponseStore, router as openai_router
from .responses_api import router as responses_router
from .upstream import DifyClient
from .webproxy import router as proxy_router

GATE_COOKIE = "cgpt_gate"
log = logging.getLogger("cityudg")


def _gate_value(password: str) -> str:
    return hmac.new(password.encode("utf-8"), b"cityudg-gpt-gate", hashlib.sha256).hexdigest()


GATE_PAGE = """<!doctype html>
<html lang="zh-CN">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<link icon rel="icon" href="https://cdn.040426.xyz/image/brand-logo/logo.svg">
<title>Revelation | CityUDG GPT Service Gateway</title>
<style>
:root{
  color-scheme: light;
  --bg:#f3f6f2;
  --card:#ffffff;
  --ink:#17231e;
  --muted:#6c7c73;
  --line:#e2e8e3;
  --brand:#1f7a52;
  --brand-hover:#196342;
  --brand-ring:rgba(31,122,82,.14);
  --gold:#a57f49;
  --err:#b23a30;
  --shadow:0 1px 2px rgba(23,35,30,.04), 0 12px 32px -8px rgba(23,35,30,.10);
}
@media (prefers-color-scheme: dark){
  :root{
    color-scheme: dark;
    --bg:#101512;
    --card:#171d19;
    --ink:#e8efea;
    --muted:#8b9a92;
    --line:#252d28;
    --brand:#2f9e63;
    --brand-hover:#37b271;
    --brand-ring:rgba(47,158,99,.18);
    --err:#e0736a;
    --shadow:0 1px 2px rgba(0,0,0,.3), 0 18px 40px -12px rgba(0,0,0,.5);
  }
}
*{box-sizing:border-box}
html,body{height:100%}
body{
  margin:0;display:grid;place-items:center;padding:24px;
  background:
    radial-gradient(80% 60% at 50% -10%, rgba(165,127,73,.10), transparent 60%),
    radial-gradient(60% 50% at 50% 110%, rgba(31,122,82,.10), transparent 60%),
    var(--bg);
  color:var(--ink);
  font:15px/1.6 -apple-system,BlinkMacSystemFont,"Segoe UI","PingFang SC","Hiragino Sans GB","Microsoft YaHei",sans-serif;
  -webkit-font-smoothing:antialiased;
}
.card{
  width:min(372px,100%);
  background:var(--card);
  border:1px solid var(--line);
  border-radius:18px;
  padding:34px 30px 26px;
  box-shadow:var(--shadow);
  animation:rise .34s cubic-bezier(.22,.8,.3,1) both;
}
@keyframes rise{from{opacity:0;transform:translateY(8px)}to{opacity:1;transform:none}}

.logo{
  display:block;width:46px;height:46px;margin:0 auto 18px;
  -webkit-user-drag:none;user-select:none;
}
h1{
  margin:0 0 6px;text-align:center;
  font-size:19px;font-weight:600;letter-spacing:.2px;
}
.sub{
  margin:0 0 24px;text-align:center;
  font-size:13px;color:var(--muted);
}

.field{position:relative;display:block}
.field input{
  width:100%;height:44px;padding:0 12px;
  background:transparent;color:inherit;
  border:1px solid var(--line);border-radius:12px;
  font:inherit;font-size:14px;outline:none;
  transition:border-color .15s ease, box-shadow .15s ease;
}
.field input::placeholder{color:transparent}
.field span{
  position:absolute;left:12px;top:50%;transform:translateY(-50%);
  padding:0 4px;background:var(--card);
  font-size:13.5px;color:var(--muted);
  pointer-events:none;
  transition:top .15s ease, font-size .15s ease, color .15s ease;
}
.field input:focus{border-color:var(--brand);box-shadow:0 0 0 3px var(--brand-ring)}
.field input:focus + span,
.field input:not(:placeholder-shown) + span{
  top:0;font-size:11.5px;color:var(--brand);
}

button{
  width:100%;height:44px;margin-top:14px;
  border:0;border-radius:12px;
  background:var(--brand);color:#fff;
  font:inherit;font-size:14px;font-weight:500;letter-spacing:.3px;
  cursor:pointer;
  transition:background .15s ease, transform .06s ease, box-shadow .15s ease;
}
button:hover{background:var(--brand-hover);box-shadow:0 6px 16px -8px var(--brand)}
button:active{transform:translateY(1px)}

.err{
  margin:10px 0 0;
  font-size:12.5px;color:var(--err);text-align:center;
}
.err:empty{display:none}

.links{
  display:flex;justify-content:center;gap:20px;
  margin-top:18px;padding-top:14px;
  border-top:1px solid var(--line);
}
.links a{
  display:inline-flex;align-items:center;gap:6px;
  color:var(--muted);text-decoration:none;
  font-size:12.5px;line-height:1;
  transition:color .15s ease;
}
.links a:hover{color:var(--brand)}
.links svg{width:15px;height:15px;flex:none;opacity:.85}
.links a:hover svg{opacity:1}

</style>
</head>
<body>
<main class="card">
  <img class="logo" src="https://cdn.040426.xyz/image/brand-logo/logo.svg" alt="" onerror="this.style.display='none'">
  <h1>访问密钥</h1>
  <p class="sub">输入密钥以访问网关</p>
  <form method="post" action="/__gate">
    <label class="field">
      <input type="password" name="password" placeholder=" " autofocus autocomplete="current-password">
      <span>访问密钥</span>
    </label>
    <button type="submit">进入</button>
    <p class="err">__ERROR__</p>
  </form>

<nav class="links">
  <a href="https://github.com/zyk121381/CityUDG-GPT-Service" target="_blank" rel="noopener noreferrer"><svg viewBox="0 0 16 16" fill="currentColor" aria-hidden="true"><path d="M8 0C3.58 0 0 3.58 0 8c0 3.54 2.29 6.53 5.47 7.59.4.07.55-.17.55-.38 0-.19-.01-.82-.01-1.49-2.01.37-2.53-.49-2.69-.94-.09-.23-.48-.94-.82-1.13-.28-.15-.68-.52-.01-.53.63-.01 1.08.58 1.23.82.72 1.21 1.87.87 2.33.66.07-.52.28-.87.51-1.07-1.78-.2-3.64-.89-3.64-3.95 0-.87.31-1.59.82-2.15-.08-.2-.36-1.02.08-2.12 0 0 .67-.21 2.2.82.64-.18 1.32-.27 2-.27.68 0 1.36.09 2 .27 1.53-1.04 2.2-.82 2.2-.82.44 1.1.16 1.92.08 2.12.51.56.82 1.27.82 2.15 0 3.07-1.87 3.75-3.65 3.95.29.25.54.73.54 1.48 0 1.07-.01 1.93-.01 2.2 0 .21.15.46.55.38A8.01 8.01 0 0 0 16 8c0-4.42-3.58-8-8-8Z"/></svg><span>GitHub</span></a>
  <a href="https://www.040426.xyz" target="_blank" rel="noopener noreferrer"><svg viewBox="0 0 1024 1024" fill="currentColor" aria-hidden="true"><path d="M344.407934 453.627004c-29.198618-5.799725-56.39733 17.799157-56.39733 47.597746V602.019978c0 20.399034 14.199328 36.798258 33.398419 43.997917 36.398277 13.599356 62.597036 48.79769 62.597037 89.995739 0 52.997491-42.997964 95.995455-95.995456 95.995456s-95.995455-42.997964-95.995455-95.995456V240.037116c0-26.598741-21.398987-47.997728-47.997727-47.997728H48.021966c-26.598741 0-47.997728 21.398987-47.997727 47.997728v495.976518c0 178.991526 164.192227 320.384832 349.98343 281.386678 108.794849-22.798921 196.590693-110.794755 219.389614-219.389613 34.798353-165.792151-73.996497-314.385116-224.989349-344.383695zM418.00445 0.048478c-18.399129-0.999953-33.99839 13.599356-33.99839 31.998485v63.197008c0 16.999195 13.199375 30.998532 29.998579 31.798494 258.787748 13.999337 466.777901 223.989396 481.777191 482.977134 0.999953 16.799205 14.99929 29.99858 31.798495 29.99858h64.196961c18.399129 0 32.998438-15.599261 31.998485-33.99839C1006.776575 279.635241 744.388998 17.247663 418.00445 0.048478z m0.599972 191.99091c-18.599119-1.399934-34.598362 13.399366-34.598362 32.198476v64.19696c0 16.799205 12.999385 30.598551 29.598598 31.798495 153.592728 12.599403 275.986934 136.393543 289.786281 290.386252 1.599924 16.599214 15.19928 29.398608 31.798494 29.398608h64.396952c18.599119 0 33.598409-15.999243 32.198475-34.598362-16.799205-220.189575-192.990863-396.381234-413.180438-413.380429z"/></svg><span>博客</span></a>
</nav>
</main>
</body>
</html>
"""


async def _keepalive_loop(auth: AuthManager, interval: float) -> None:
    # 后台定期探 token。
    while True:
        await asyncio.sleep(interval)
        try:
            ok = await auth.check_and_refresh()
            if not ok:
                log.warning("token 巡检失败：%s", auth.last_error or "未知原因")
        except asyncio.CancelledError:
            raise
        except Exception:  # noqa: BLE001
            log.exception("token 巡检异常")


async def _reaper_loop(cfg: Settings, dify: DifyClient, store: Any) -> None:
    # 定时删掉本网关建过、且已经够久的会话。
    while True:
        await asyncio.sleep(cfg.delete_sweep_seconds)
        try:
            for conversation_id in store.due(cfg.delete_idle_seconds, cfg.delete_max_age_seconds):
                if await dify.delete_conversation(conversation_id):
                    store.forget(conversation_id)
                    log.info("已删除上游会话 %s", conversation_id)
                else:
                    store.touch(conversation_id)
        except asyncio.CancelledError:
            raise
        except Exception:  # noqa: BLE001
            log.exception("会话清理异常")


def create_app(settings: Settings | None = None) -> FastAPI:
    cfg = settings or load()
    auth = AuthManager(cfg)
    dify = DifyClient(cfg, auth)

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        app.state.proxy_client = httpx.AsyncClient(
            timeout=httpx.Timeout(300.0, connect=20.0),
            limits=httpx.Limits(max_connections=64, max_keepalive_connections=32),
        )
        try:
            await auth.check_and_refresh()
        except Exception as exc:  # noqa: BLE001
            log.warning("初始登录未完成：%s", exc)

        keepalive: asyncio.Task | None = None
        interval = cfg.keepalive_interval
        if interval > 0:
            keepalive = asyncio.create_task(_keepalive_loop(auth, interval))

        reaper: asyncio.Task | None = None
        if cfg.auto_delete_enabled:
            reaper = asyncio.create_task(_reaper_loop(cfg, dify, app.state.conversations))

        try:
            yield
        finally:
            for task in (keepalive, reaper):
                if task is not None:
                    task.cancel()
                    try:
                        await task
                    except (asyncio.CancelledError, Exception):  # noqa: B014
                        pass
            await app.state.proxy_client.aclose()
            await dify.aclose()

    app = FastAPI(
        title="LLM Gateway" if not cfg.expose_docs else "CityUDG GPT Service",
        description=None if not cfg.expose_docs else "CityUDG GPT Service Gateway",
        version="0.1.0",
        lifespan=lifespan,
        docs_url="/__docs" if cfg.expose_docs else None,
        redoc_url=None,
        openapi_url="/__openapi.json" if cfg.expose_docs else None,
    )
    app.state.settings = cfg
    app.state.auth = auth
    app.state.dify = dify
    app.state.registry = ModelRegistry(dify, cfg)
    app.state.conversations = ConversationStore(
        path=cfg.conversations_file if cfg.conversations_persist else None
    )
    app.state.responses = ResponseStore()
    app.state.dummy_token = str(uuid4())

    # ---------- 网关鉴权 ----------
    @app.middleware("http")
    async def gate(request: Request, call_next):
        path = request.url.path
        if path in ("/healthz", "/__gate"):
            return await call_next(request)

        if path == "/v1" or path.startswith("/v1/"):
            cors = {
                "Access-Control-Allow-Origin": request.headers.get("origin") or "*",
                "Access-Control-Allow-Methods": "GET, POST, OPTIONS",
                "Access-Control-Allow-Headers": "Authorization, Content-Type, X-Api-Key",
                "Access-Control-Max-Age": "86400",
            }
            if cfg.debug_headers:
                cors["Access-Control-Expose-Headers"] = (
                    "X-CGPT-Chained, X-CGPT-Conversation, X-CGPT-Query-Len"
                )
            if request.method == "OPTIONS":
                return Response(status_code=204, headers=cors)
            if cfg.api_key:
                header = request.headers.get("authorization", "")
                token = header[7:] if header.lower().startswith("bearer ") else request.headers.get("x-api-key", "")
                if not hmac.compare_digest(token.strip(), cfg.api_key):
                    return JSONResponse(
                        {"error": {"message": "API Key 不正确", "type": "auth_error", "code": 401}},
                        status_code=401,
                        headers=cors,
                    )
            response = await call_next(request)
            response.headers.update(cors)
            return response

        if cfg.proxy_password:
            cookie = request.cookies.get(GATE_COOKIE, "")
            if not hmac.compare_digest(cookie, _gate_value(cfg.proxy_password)):
                if path.startswith("/api/"):
                    return JSONResponse({"code": 401, "msg": "gate required"}, status_code=401)
                if path.startswith("/__"):
                    return JSONResponse({"error": "gate required"}, status_code=401)
                return RedirectResponse("/__gate", status_code=302)

        return await call_next(request)

    # ---------- 兜底错误处理 ----------
    @app.exception_handler(Exception)
    async def unhandled(request: Request, exc: Exception) -> JSONResponse:
        request_id = err.new_id()
        err.record(request_id, 500, exc, path=request.url.path)
        if request.url.path.startswith("/v1"):
            return JSONResponse(
                {"error": {
                    "message": f"{err.public_message(500)}（编号 {request_id}）",
                    "type": "server_error",
                    "code": 500,
                }},
                status_code=500,
            )
        return JSONResponse({"error": "服务内部错误"}, status_code=500)

    # ---------- 登录页 ----------
    @app.get("/__gate", include_in_schema=False)
    async def gate_page(request: Request) -> Response:
        if not cfg.proxy_password:
            return RedirectResponse("/", status_code=302)
        if hmac.compare_digest(request.cookies.get(GATE_COOKIE, ""), _gate_value(cfg.proxy_password)):
            return RedirectResponse("/", status_code=302)
        return HTMLResponse(GATE_PAGE.replace("__ERROR__", ""))

    @app.post("/__gate", include_in_schema=False)
    async def gate_submit(request: Request) -> Response:
        raw = (await request.body()).decode("utf-8", "replace")
        supplied = (parse_qs(raw).get("password") or [""])[0]
        if not cfg.proxy_password or not hmac.compare_digest(supplied, cfg.proxy_password):
            return HTMLResponse(GATE_PAGE.replace("__ERROR__", "密钥错误"), status_code=401)
        response = RedirectResponse("/", status_code=302)
        response.set_cookie(
            GATE_COOKIE,
            _gate_value(cfg.proxy_password),
            httponly=True,
            samesite="lax",
            max_age=60 * 60 * 24 * 30,
        )
        return response

    # ---------- 运维 ----------
    @app.get("/healthz", include_in_schema=False)
    async def healthz() -> dict[str, Any]:
        return {"ok": True}

    @app.get("/__status", include_in_schema=False)
    async def status() -> Any:
        try:
            session = await auth.ensure()
        except AuthError as exc:
            return JSONResponse({"ok": False, "error": str(exc)}, status_code=503)
        return {
            "ok": await auth.validate(session.token),
            "user": session.display_name,
            "user_id": session.user_id,
            "account_id": session.account_id,
            "account": session.user.get("AccountName"),
            "source": session.source,
            "token_tail": session.token[-6:] if session.token else "",
            "obtained_at": session.obtained_at,
            "mode": cfg.auth_mode,
            "base_url": cfg.base_url,
            "conversations": {
                "tracked": app.state.conversations.tracked,
                "auto_delete": cfg.auto_delete_enabled,
                "idle_seconds": cfg.delete_idle_seconds,
            },
        }

    @app.post("/__relogin", include_in_schema=False)
    async def relogin() -> Any:
        try:
            session = await auth.relogin()
        except AuthError as exc:
            return JSONResponse({"ok": False, "error": str(exc)}, status_code=400)
        return {"ok": True, "user": session.display_name, "obtained_at": session.obtained_at}

    # ---------- 路由 ----------
    app.include_router(openai_router)
    app.include_router(responses_router)
    if cfg.proxy_enabled:
        app.include_router(proxy_router)

    return app


app = create_app()
