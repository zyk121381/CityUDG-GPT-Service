# 上游 /api/dify/chat 的客户端与 SSE 解析。

from __future__ import annotations

import json
import time
from collections.abc import AsyncIterator
from typing import Any

import httpx

from .auth import AuthManager, AuthError, Session
from .config import Settings


class UpstreamError(RuntimeError):
    def __init__(self, message: str, status: int = 502, body: Any = None) -> None:
        super().__init__(message)
        self.status = status
        self.body = body


def build_payload(
    *,
    query: str,
    model: str,
    user_id: str,
    search: bool = False,
    library: bool = False,
    thinking: bool = False,
    files: list[Any] | None = None,
    parent_chat_history_id: str = "1",
    conversation_id: str = "",
) -> dict[str, Any]:
    return {
        "query": query,
        "ai_type": model,
        "search_enabled": "true" if search else "false",
        "library_enabled": "true" if library else "false",
        "thinking_enabled": "true" if thinking else "false",
        "user_id": user_id,
        "files": files or [],
        "parentChatHistoryId": parent_chat_history_id,
        "conversationId": conversation_id,
    }


class DifyClient:
    def __init__(self, cfg: Settings, auth: AuthManager) -> None:
        self.cfg = cfg
        self.auth = auth
        self.profile = cfg.browser_profile()
        self._client: httpx.AsyncClient | None = None

    async def client(self) -> httpx.AsyncClient:
        if self._client is None or self._client.is_closed:
            self._client = httpx.AsyncClient(
                timeout=httpx.Timeout(self.cfg.timeout, connect=20.0),
                limits=httpx.Limits(max_connections=32, max_keepalive_connections=16),
                http2=self.profile.http2,
            )
        return self._client

    async def aclose(self) -> None:
        if self._client and not self._client.is_closed:
            await self._client.aclose()

    # ---------- 模型 ----------
    async def models(self) -> list[dict[str, Any]]:
        session = await self.auth.ensure()
        client = await self.client()
        resp = await client.get(
            self.cfg.base_url + "/api/model/getModelSimpleList",
            params={"modelType": "1"},
            headers=self.profile.json_get({"Authorization": f"Bearer {session.token}"}),
        )
        body = self._decode(resp)
        data = body.get("data") if isinstance(body, dict) else None
        return data if isinstance(data, list) else []

    # ---------- 会话 ----------
    async def delete_conversation(self, conversation_id: str) -> bool:
        if not conversation_id:
            return False

        session = await self.auth.ensure()
        client = await self.client()

        for attempt in (0, 1):
            try:
                resp = await client.get(
                    self.cfg.base_url + "/api/chatHistory/removeChatHistory",
                    params={"conversationId": conversation_id},
                    headers=self.profile.json_get({
                        "Authorization": f"Bearer {session.token}",
                    }),
                )
            except httpx.HTTPError:
                return False

            body = self._decode(resp)
            code = body.get("code") if isinstance(body, dict) else None
            if code in (401, 4001) and attempt == 0:
                try:
                    session = await self.auth.relogin(stale_token=session.token)
                except AuthError:
                    return False
                continue
            return code == 0

        return False

    # ---------- 对话 ----------
    async def stream_chat(self, payload: dict[str, Any]) -> AsyncIterator[dict[str, Any]]:
        session = await self.auth.ensure()
        resp = await self._post(payload, session)

        # 上游鉴权失败时返回的是 HTTP 200 + {"code":401,"msg":"Unauthorized"}。
        for attempt in (0, 1):
            failure = await self._classify(resp)
            if failure is None:
                break
            await resp.aclose()
            if failure.status in (401, 403) and attempt == 0:
                try:
                    session = await self.auth.relogin(stale_token=session.token)
                except AuthError as exc:
                    raise UpstreamError("凭据已失效且无法自动重登", status=401) from exc
                resp = await self._post(payload, session)
                continue
            raise failure

        async for event in self._iter_sse(resp):
            yield event

    async def _classify(self, resp: httpx.Response) -> "UpstreamError | None":
        if resp.status_code in (401, 403):
            return UpstreamError("上游鉴权失败", status=401)

        content_type = resp.headers.get("content-type", "").lower()
        if "text/event-stream" in content_type:
            if resp.status_code >= 400:
                return UpstreamError(f"上游返回 {resp.status_code}", status=resp.status_code)
            return None

        raw = (await resp.aread()).decode("utf-8", "replace")
        detail = raw[:300]
        try:
            body = json.loads(raw)
        except json.JSONDecodeError:
            body = None

        if isinstance(body, dict):
            code = body.get("code")
            message = body.get("msg") or body.get("message") or detail
            if code in (401, 4001):
                return UpstreamError(f"上游鉴权失败：{message}", status=401, body=message)
            if code not in (0, None):
                return UpstreamError(f"上游错误 code={code}：{message}", status=502, body=message)
            return UpstreamError("上游没有返回事件流", status=502, body=detail)

        if resp.status_code >= 400:
            return UpstreamError(f"上游返回 {resp.status_code}", status=resp.status_code, body=detail)
        return UpstreamError("上游返回了非事件流内容", status=502, body=detail)

    async def _post(self, payload: dict[str, Any], session: Session) -> httpx.Response:
        client = await self.client()
        headers = self.profile.raw_post({
            "Authorization": f"Bearer {session.token}",
            "Content-Type": "application/json",
        })
        request = client.build_request(
            "POST",
            self.cfg.base_url + "/api/dify/chat",
            json=payload,
            headers=headers,
        )
        return await client.send(request, stream=True)

    @staticmethod
    async def _iter_sse(resp: httpx.Response) -> AsyncIterator[dict[str, Any]]:
        try:
            async for line in resp.aiter_lines():
                if not line:
                    continue
                if line.startswith(":"):
                    continue
                if not line.startswith("data:"):
                    continue
                raw = line[5:].strip()
                if not raw or raw == "[DONE]":
                    continue
                try:
                    event = json.loads(raw)
                except json.JSONDecodeError:
                    continue
                if not isinstance(event, dict):
                    continue
                if event.get("event") in ("error", "workflow_error") or event.get("error"):
                    message = str(event.get("error") or event.get("message") or "上游返回错误事件")
                    raise UpstreamError(f"上游错误事件：{message[:300]}", status=502, body=message)
                yield event
        finally:
            await resp.aclose()

    @staticmethod
    def _decode(resp: httpx.Response) -> Any:
        try:
            return resp.json()
        except Exception:
            return None

    # ---------- 校验 ----------
    async def probe(self) -> dict[str, Any]:
        session = await self.auth.ensure(force=True)
        ok = await self.auth.validate(session.token)
        models = []
        if ok:
            try:
                models = await self.models()
            except Exception:
                models = []
        return {
            "ok": ok,
            "token_prefix": session.token[:8] + "..." if session.token else "",
            "token_length": len(session.token),
            "source": session.source,
            "user": session.display_name,
            "user_id": session.user_id,
            "account_id": session.account_id,
            "obtained_at": session.obtained_at,
            "models": [m.get("modelValue") for m in models],
            "checked_at": time.strftime("%Y-%m-%d %H:%M:%S"),
        }


def split(messages: list[dict[str, Any]]) -> tuple[str, list[dict[str, str]]]:
    system_parts: list[str] = []
    turns: list[dict[str, str]] = []
    for msg in messages:
        role = str(msg.get("role") or "user")
        text = _content_to_text(msg.get("content"))
        if not text:
            continue
        if role == "system":
            system_parts.append(text)
        else:
            turns.append({"role": role, "text": text})
    return "\n\n".join(system_parts), turns


def compose(system_text: str, turns: list[dict[str, str]]) -> str:
    # 把一轮或多轮拼成一段文本。
    if len(turns) == 1 and turns[0]["role"] == "user":
        body = turns[0]["text"]
    else:
        blocks = []
        for turn in turns:
            label = {"user": "用户", "assistant": "助手"}.get(turn["role"], turn["role"])
            blocks.append(f"[{label}]\n{turn['text']}")
        body = "\n\n".join(blocks)

    if system_text:
        return f"{system_text}\n\n{body}"
    return body


def flatten(messages: list[dict[str, Any]]) -> tuple[str, str]:
    system_text, turns = split(messages)
    return compose(system_text, turns), system_text


def _content_to_text(content: Any) -> str:
    if content is None:
        return ""
    if isinstance(content, str):
        return content.strip()
    if isinstance(content, list):
        parts = []
        for item in content:
            if isinstance(item, str):
                parts.append(item)
            elif isinstance(item, dict):
                if item.get("type") in (None, "text", "input_text") and item.get("text"):
                    parts.append(str(item["text"]))
        return "".join(parts).strip()
    return str(content).strip()


def estimate_tokens(text: str) -> int:
    if not text:
        return 0
    cjk = sum(1 for ch in text if "\u4e00" <= ch <= "\u9fff")
    rest = len(text) - cjk
    return max(1, cjk + rest // 4)
