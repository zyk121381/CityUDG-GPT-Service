# 整站透传：把 gpt.cityu-dg.edu.cn 原样挂到本机域名下，并注入 token。

from __future__ import annotations

import json
from collections.abc import AsyncIterator, Iterable
from typing import Any

import httpx
from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse, Response, StreamingResponse

from .auth import AuthError
from .browser import supported_encodings

router = APIRouter()

AUTH_EXEMPT = ("/loginSSO", "/loginMini", "/loginAcc", "/getSsoInfo")

_DROP_REQUEST_HEADERS = {
    "host",
    "connection",
    "keep-alive",
    "proxy-authenticate",
    "proxy-authorization",
    "te",
    "trailer",
    "transfer-encoding",
    "upgrade",
    "content-length",
    "accept-encoding",
    "authorization",
    "cookie",
}

_DROP_RESPONSE_HEADERS = {
    "connection",
    "keep-alive",
    "proxy-authenticate",
    "proxy-authorization",
    "te",
    "trailer",
    "transfer-encoding",
    "upgrade",
    "content-length",
    "content-security-policy",
    "content-security-policy-report-only",
}


def _bootstrap_script(session: dict[str, Any]) -> str:
    payload = json.dumps({"token": session.get("token", ""), "user": session.get("user") or {}},
                         ensure_ascii=False).replace("</", "<\\/")
    return (
        "<script id=\"cgpt-bootstrap\">(function(){try{"
        f"var s={payload};"
        "var o=JSON.parse(localStorage.getItem('app-locale')||'{}');"
        "o.token=s.token;o.userInfo=s.user||{};"
        "if(!o.loginMethod){o.loginMethod='sso';}"
        "localStorage.setItem('app-locale',JSON.stringify(o));"
        "}catch(e){}})();</script>"
    )


def _mask_user(user: dict[str, Any] | None) -> dict[str, Any]:
    masked = dict(user or {})
    if masked.get("Email"):
        masked["Email"] = ""
    if masked.get("Mobile"):
        masked["Mobile"] = ""
    return masked


def _inject(html: str, session: dict[str, Any]) -> str:
    script = _bootstrap_script(session)
    lower = html.lower()
    idx = lower.rfind("</head>")
    if idx == -1:
        return script + html
    return html[:idx] + script + html[idx:]


def _filter(headers: Iterable[tuple[str, str]], drop: set[str]) -> dict[str, str]:
    return {k: v for k, v in headers if k.lower() not in drop}


def _json_code(raw: bytes) -> Any:
    try:
        body = json.loads(raw.decode("utf-8", "replace"))
    except (json.JSONDecodeError, UnicodeDecodeError):
        return None
    return body.get("code") if isinstance(body, dict) else None


async def _body_stream(resp: httpx.Response, client: httpx.AsyncClient) -> AsyncIterator[bytes]:
    try:
        async for chunk in resp.aiter_raw():
            yield chunk
    finally:
        await resp.aclose()


@router.api_route(
    "/{full_path:path}",
    methods=["GET", "POST", "PUT", "PATCH", "DELETE", "HEAD", "OPTIONS"],
    include_in_schema=False,
)
async def transparent_proxy(request: Request, full_path: str) -> Response:
    cfg = request.app.state.settings
    if not cfg.proxy_enabled:
        return JSONResponse({"error": "web 反代已关闭（config.yaml → proxy.enabled）"}, status_code=404)
    if full_path.startswith("__"):
        return JSONResponse({"error": "Not Found"}, status_code=404)

    auth = request.app.state.auth
    session = await auth.ensure()
    base = cfg.base_url
    url = f"{base}/{full_path}" if full_path else base + "/"

    headers = _filter(request.headers.items(), _DROP_REQUEST_HEADERS)
    headers["accept-encoding"] = supported_encodings()

    normalized = "/" + full_path.lstrip("/")
    inject_auth = not any(normalized.lower().startswith(item.lower()) for item in AUTH_EXEMPT)

    body = await request.body()
    client: httpx.AsyncClient = request.app.state.proxy_client
    params = list(request.query_params.multi_items())

    def build(token: str) -> httpx.Request:
        hdrs = dict(headers)
        if inject_auth:
            hdrs["authorization"] = f"Bearer {token}"
        return client.build_request(
            request.method,
            url,
            params=params,
            headers=hdrs,
            content=body if body else None,
        )

    buffered: bytes | None = None
    try:
        resp = await client.send(build(session.token), stream=True)

        refreshed = False
        while inject_auth and "application/json" in resp.headers.get("content-type", "").lower():
            raw = await resp.aread()
            await resp.aclose()
            code = _json_code(raw)
            if code in (401, 4001) and not refreshed:
                refreshed = True
                session = await auth.relogin(stale_token=session.token)
                resp = await client.send(build(session.token), stream=True)
                continue
            buffered = raw
            break
    except AuthError:
        return JSONResponse({"code": 401, "msg": "unauthorized"}, status_code=401)
    except httpx.HTTPError:
        return JSONResponse({"error": "服务暂时不可用"}, status_code=502)

    out_headers = _filter(resp.headers.items(), _DROP_RESPONSE_HEADERS)
    location = resp.headers.get("location")
    if location and location.startswith(base):
        out_headers["location"] = location[len(base):] or "/"

    content_type = resp.headers.get("content-type", "")
    if "text/html" in content_type.lower() and request.method != "HEAD":
        raw = await resp.aread()
        await resp.aclose()
        text = raw.decode(resp.encoding or "utf-8", "replace")
        text = _inject(text, {
            "token": request.app.state.dummy_token,
            "user": _mask_user(session.user),
        })
        out_headers.pop("content-encoding", None)
        out_headers["cache-control"] = "no-store"
        return Response(
            content=text.encode("utf-8"),
            status_code=resp.status_code,
            headers=out_headers,
            media_type="text/html; charset=utf-8",
        )

    if buffered is not None:
        out_headers.pop("content-encoding", None)
        return Response(content=buffered, status_code=resp.status_code, headers=out_headers)

    return StreamingResponse(
        _body_stream(resp, client),
        status_code=resp.status_code,
        headers=out_headers,
    )
