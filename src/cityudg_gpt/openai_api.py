# OpenAI 兼容层：/v1/models、/v1/chat/completions。

from __future__ import annotations

import hashlib
import json
import logging
import time
import uuid
from collections import OrderedDict
from pathlib import Path
from collections.abc import AsyncIterator
from typing import Any

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse, StreamingResponse

from . import errors as err
from .config import Settings
from .upstream import (
    DifyClient,
    UpstreamError,
    build_payload,
    compose,
    estimate_tokens,
    split,
)

router = APIRouter()
log = logging.getLogger("cityudg")


def _error(message: str, status: int, kind: str = "upstream_error") -> JSONResponse:
    return JSONResponse(
        status_code=status,
        content={"error": {"message": message, "type": kind, "code": status}},
    )


def sanitized_error(exc: Exception, status: int, kind: str = "upstream_error") -> JSONResponse:
    # 内部异常真实原因只写日志。
    request_id = err.new_id()
    status = err.normalize_status(status)
    err.record(request_id, status, exc)
    return _error(f"{err.public_message(status)}（编号 {request_id}）", status, kind)


class ModelRegistry:
    # 缓存上游模型表，把 modelValue / modelName 都映射到 modelValue。
    def __init__(self, client: DifyClient, cfg: Settings) -> None:
        self.client = client
        self.cfg = cfg
        self._aliases: dict[str, str] = {}
        self._items: list[dict[str, Any]] = []
        self._loaded_at = 0.0
        self._ttl = 600.0

    async def _ensure(self) -> None:
        if self._items and time.time() - self._loaded_at < self._ttl:
            return
        try:
            items = await self.client.models()
        except Exception:
            items = []
        if not items:
            return
        aliases: dict[str, str] = {}
        for item in items:
            value = str(item.get("modelValue") or "").strip()
            if not value:
                continue
            aliases[value.lower()] = value
            name = str(item.get("modelName") or "").strip()
            if name:
                aliases.setdefault(name.lower(), value)
        self._items = items
        self._aliases = aliases
        self._loaded_at = time.time()

    async def resolve(self, requested: str | None) -> str:
        await self._ensure()
        name = (requested or self.cfg.default_model or "").strip()
        if not name:
            return self.cfg.default_model
        hit = self._aliases.get(name.lower())
        if hit:
            return hit
        return name

    async def list(self) -> list[dict[str, Any]]:
        await self._ensure()
        if self._items:
            return self._items
        return [{"modelValue": self.cfg.default_model, "modelName": self.cfg.default_model}]


@router.get("/v1/models")
@router.get("/v1", include_in_schema=False)
async def list_models(request: Request) -> JSONResponse:
    registry: ModelRegistry = request.app.state.registry
    cfg: Settings = request.app.state.settings
    created = int(time.time())
    data = []
    for item in await registry.list():
        value = str(item.get("modelValue") or "").strip()
        if not value:
            continue
        data.append({
            "id": value,
            "object": "model",
            "created": created,
            "owned_by": cfg.owned_by,
        })
    return JSONResponse(content={"object": "list", "data": data})


@router.get("/v1/models/{model_id}")
async def get_model(model_id: str, request: Request) -> JSONResponse:
    registry: ModelRegistry = request.app.state.registry
    cfg: Settings = request.app.state.settings
    resolved = await registry.resolve(model_id)
    return JSONResponse(content={
        "id": resolved,
        "object": "model",
        "created": int(time.time()),
        "owned_by": cfg.owned_by,
    })


def _read_bool(body: dict[str, Any], key: str, default: bool) -> bool:
    if key not in body or body[key] is None:
        return default
    value = body[key]
    if isinstance(value, bool):
        return value
    return str(value).strip().lower() in ("1", "true", "yes", "on")


class ConversationStore:
    """
    把「某段对话」映射到上游的 conversationId / 上一条 chatHistId。

    OpenAI 客户端是无状态的，每次都重发全量历史；上游却是有状态会话。
    这里用「用户消息序列」的指纹做键：同一串追问，就认作同一条会话，
    于是后续轮次只发新问题，由上游自己记住上下文。

    指纹只算 user 消息——assistant 文本会被客户端改写，system 文本会被 agent
    框架每轮重建，两者放进键里都会让命中率崩掉。
    """

    def __init__(self, maxsize: int = 1000, ttl: float = 12 * 3600,
                 path: Path | None = None) -> None:
        self._data: "OrderedDict[str, dict[str, Any]]" = OrderedDict()
        # 由本网关新建的会话，用来做定时清理。
        self._convs: dict[str, dict[str, float]] = {}
        self._maxsize = maxsize
        self._ttl = ttl
        self._path = path
        self._load()

    # ---------- 落盘 ----------
    def _load(self) -> None:
        if self._path is None:
            return
        try:
            raw = json.loads(self._path.read_text("utf-8"))
        except (OSError, json.JSONDecodeError):
            return
        if not isinstance(raw, dict):
            return
        for key, value in (raw.get("chains") or {}).items():
            if isinstance(value, dict) and value.get("conversationId"):
                self._data[key] = value
        for cid, value in (raw.get("conversations") or {}).items():
            if isinstance(value, dict):
                self._convs[cid] = {
                    "created": float(value.get("created") or 0),
                    "used": float(value.get("used") or 0),
                }

    def _save(self) -> None:
        if self._path is None:
            return
        payload = {
            "chains": dict(self._data),
            "conversations": self._convs,
        }
        try:
            self._path.parent.mkdir(parents=True, exist_ok=True)
            tmp = self._path.with_suffix(self._path.suffix + ".tmp")
            tmp.write_text(json.dumps(payload, ensure_ascii=False), "utf-8")
            tmp.replace(self._path)
        except OSError:
            pass

    def get(self, key: str) -> dict[str, Any] | None:
        item = self._local_get(key)
        if item is None and self._path is not None:
            self._load()
            item = self._local_get(key)
        return item

    def _local_get(self, key: str) -> dict[str, Any] | None:
        item = self._data.get(key)
        if not item:
            return None
        if time.time() - item.get("at", 0) > self._ttl:
            self._data.pop(key, None)
            return None
        self._data.move_to_end(key)
        return item

    def put(self, key: str, conversation_id: str, chat_hist_id: str) -> None:
        if not conversation_id:
            return
        self._data[key] = {
            "conversationId": conversation_id,
            "chatHistId": chat_hist_id,
            "at": time.time(),
        }
        self._data.move_to_end(key)
        while len(self._data) > self._maxsize:
            self._data.popitem(last=False)
        self._save()

    # ---------- 自动清理 ----------
    def register(self, conversation_id: str) -> None:
        if not conversation_id:
            return
        now = time.time()
        item = self._convs.get(conversation_id)
        if item is None:
            self._convs[conversation_id] = {"created": now, "used": now}
        else:
            item["used"] = now
        self._save()

    def touch(self, conversation_id: str) -> None:
        item = self._convs.get(conversation_id)
        if item is not None:
            item["used"] = time.time()

    def due(self, idle_seconds: float, max_age_seconds: float) -> list[str]:
        now = time.time()
        expired: list[str] = []
        for conversation_id, item in list(self._convs.items()):
            if max_age_seconds > 0 and now - item["created"] >= max_age_seconds:
                expired.append(conversation_id)
            elif idle_seconds > 0 and now - item["used"] >= idle_seconds:
                expired.append(conversation_id)
        return expired

    def forget(self, conversation_id: str) -> None:
        self._convs.pop(conversation_id, None)
        stale = [k for k, v in self._data.items() if v.get("conversationId") == conversation_id]
        for key in stale:
            self._data.pop(key, None)
        self._save()

    @property
    def tracked(self) -> int:
        return len(self._convs)

    @property
    def size(self) -> int:
        return len(self._data)


def _chain_key(user_texts: list[str], namespace: str = "") -> str:
    return hashlib.sha256((namespace + "\x1d" + "\x1f".join(user_texts)).encode("utf-8")).hexdigest()


def _session_key(request: Request, body: dict[str, Any]) -> str:
    """
    客户端显式给的会话标识，优先级最高。
    头 `X-Conversation-Id` 或请求体里的 `conversation_id`。
    由调用方自己给一个稳定的会话 id。
    """
    value = request.headers.get("x-conversation-id") or body.get("conversation_id")
    text = str(value or "").strip()
    return f"sid:{text}" if text else ""


def _resolve_keys(
    request: Request,
    body: dict[str, Any],
    user_texts: list[str],
    auto_chain: bool,
) -> tuple[str, str, str]:
    """
    sid  —— 调用方给了会话 id，按 id 串，绝对隔离
    auto —— 按消息指纹串，只在单租户、且带了 end-user 命名空间时安全
    off  —— 不串，每次都是新会话，历史仍然整段发过去
    """
    sid = _session_key(request, body)
    if sid:
        return sid, sid, "sid"

    if not auto_chain or len(user_texts) < 2:
        return "", "", "off"

    end_user = str(body.get("user") or "").strip()
    namespace = f"u:{end_user}" if end_user else "u:"
    return _chain_key(user_texts[:-1], namespace), _chain_key(user_texts, namespace), "auto"


class ResponseStore:
    # Responses API 的 response_id → 上游会话，以及完整响应对象的缓存。
    def __init__(self, maxsize: int = 500, ttl: float = 24 * 3600) -> None:
        self._data: "OrderedDict[str, dict[str, Any]]" = OrderedDict()
        self._maxsize = maxsize
        self._ttl = ttl

    def _live(self, key: str) -> dict[str, Any] | None:
        item = self._data.get(key)
        if not item:
            return None
        if time.time() - item["at"] > self._ttl:
            self._data.pop(key, None)
            return None
        self._data.move_to_end(key)
        return item

    def _touch(self, key: str) -> dict[str, Any]:
        item = self._data.get(key)
        if item is None:
            item = {"conversationId": "", "chatHistId": "", "full": None, "at": time.time()}
            self._data[key] = item
        item["at"] = time.time()
        self._data.move_to_end(key)
        while len(self._data) > self._maxsize:
            self._data.popitem(last=False)
        return item

    def get(self, response_id: str) -> dict[str, Any] | None:
        return self._live(response_id)

    def put(self, response_id: str, conversation_id: str, chat_hist_id: str) -> None:
        if not response_id:
            return
        item = self._touch(response_id)
        item["conversationId"] = conversation_id
        item["chatHistId"] = chat_hist_id

    def put_full(self, response_id: str, payload: dict[str, Any]) -> None:
        self._touch(response_id)["full"] = payload

    def get_full(self, response_id: str) -> dict[str, Any] | None:
        item = self._live(response_id)
        return item.get("full") if item else None

    def drop(self, response_id: str) -> None:
        self._data.pop(response_id, None)

    @property
    def size(self) -> int:
        return len(self._data)


def _request_fingerprint(request: Request) -> dict[str, Any]:
    headers = request.headers
    cookie_names = sorted({
        part.split("=")[0].strip()
        for part in (headers.get("cookie") or "").split(";")
        if "=" in part
    })
    extra = sorted(
        key for key in headers.keys()
        if key.lower().startswith("x-") and "api-key" not in key.lower() and "auth" not in key.lower()
    )
    return {
        "xff": (headers.get("x-forwarded-for") or "")[:60],
        "real_ip": (headers.get("x-real-ip") or "")[:40],
        "origin": (headers.get("origin") or "")[:60],
        "referer": (headers.get("referer") or "")[:80],
        "ua": (headers.get("user-agent") or "")[:40],
        "cookie_names": cookie_names,
        "x_headers": extra,
        "peer": request.client.host if request.client else "",
    }


@router.post("/v1/chat/completions")
async def chat_completions(request: Request) -> Any:
    try:
        body = await request.json()
    except Exception:
        return _error("请求体不是合法 JSON", 400, "invalid_request_error")
    if not isinstance(body, dict):
        return _error("请求体必须是 JSON 对象", 400, "invalid_request_error")

    messages = body.get("messages")
    if not isinstance(messages, list) or not messages:
        return _error("messages 不能为空", 400, "invalid_request_error")

    cfg: Settings = request.app.state.settings
    registry: ModelRegistry = request.app.state.registry
    dify: DifyClient = request.app.state.dify
    store: ConversationStore = request.app.state.conversations

    system_text, turns = split(messages)
    if not turns:
        return _error("messages 里没有可用文本内容", 400, "invalid_request_error")

    model = await registry.resolve(body.get("model"))
    user_texts = [t["text"] for t in turns if t["role"] == "user"]

    lookup_key, store_key, chain_mode = _resolve_keys(request, body, user_texts, cfg.auto_chain)
    chained = store.get(lookup_key) if lookup_key else None

    if chained:
        query = turns[-1]["text"]
        conversation_id = str(chained["conversationId"])
        parent_id = str(chained.get("chatHistId") or "1")
    else:
        # 拿不到原生会话，退回拼接历史，保证答案正确
        query = compose(system_text, turns)
        conversation_id = str(body.get("conversationId") or "")
        parent_id = str(body.get("parentChatHistoryId") or "1")

    session = await request.app.state.auth.ensure()
    user_id = session.user_id or cfg.user_id

    payload = build_payload(
        query=query,
        model=model,
        user_id=user_id,
        search=_read_bool(body, "search_enabled", cfg.default_search),
        library=_read_bool(body, "library_enabled", cfg.default_library),
        thinking=_read_bool(body, "thinking_enabled", cfg.default_thinking),
        parent_chat_history_id=parent_id,
        conversation_id=conversation_id,
    )

    started = chained is None
    completion_id = "chatcmpl-" + uuid.uuid4().hex
    created = int(time.time())

    log.info(
        "[chat] msgs=%d users=%d mode=%s chained=%s conv=%s qlen=%d",
        len(messages), len(user_texts), chain_mode,
        not started, conversation_id or "-", len(query),
    )
    if cfg.log_requests:
        log.info("[req] %s body_keys=%s user=%r", _request_fingerprint(request), sorted(body.keys()),
                 str(body.get("user") or "")[:24])

    # 诊断头默认关闭：它会暴露上游会话 id 和"拼接/串联"这种内部状态
    meta: dict[str, str] = {}
    if cfg.debug_headers:
        meta = {
            "X-CGPT-Chained": "1" if chained else "0",
            "X-CGPT-Conversation": conversation_id or "",
            "X-CGPT-Query-Len": str(len(query)),
        }

    if not _read_bool(body, "stream", False):
        return await _complete(
            dify, payload, completion_id, created, model, query,
            store, store_key, started, meta,
        )

    stream_options = body.get("stream_options")
    include_usage = isinstance(stream_options, dict) and _read_bool(stream_options, "include_usage", False)

    headers = {
        "Cache-Control": "no-cache",
        "Connection": "keep-alive",
        "X-Accel-Buffering": "no",
        **meta,
    }
    return StreamingResponse(
        _stream(
            dify, payload, completion_id, created, model, include_usage, query,
            store, store_key, started,
        ),
        media_type="text/event-stream",
        headers=headers,
    )


def _chunk(completion_id: str, created: int, model: str, delta: dict[str, Any],
           finish: str | None = None) -> str:
    payload = {
        "id": completion_id,
        "object": "chat.completion.chunk",
        "created": created,
        "model": model,
        "choices": [{"index": 0, "delta": delta, "finish_reason": finish}],
    }
    return "data: " + json.dumps(payload, ensure_ascii=False) + "\n\n"


async def _stream(
    dify: DifyClient,
    payload: dict[str, Any],
    completion_id: str,
    created: int,
    model: str,
    include_usage: bool = False,
    prompt_text: str = "",
    store: ConversationStore | None = None,
    store_key: str = "",
    started: bool = True,
) -> AsyncIterator[str]:
    yield _chunk(completion_id, created, model, {"role": "assistant", "content": ""})
    last_emit = time.monotonic()
    parts: list[str] = []
    conversation_id = ""
    chat_hist_id = ""
    try:
        async for event in dify.stream_chat(payload):
            if event.get("conversationId"):
                conversation_id = str(event["conversationId"])
            if event.get("chatHistId"):
                chat_hist_id = str(event["chatHistId"])

            if event.get("event") != "message":
                # 上游思考/编排阶段没有正文，隔久了发个注释保活
                if time.monotonic() - last_emit > 8:
                    yield ": keepalive\n\n"
                    last_emit = time.monotonic()
                continue
            answer = event.get("answer")
            if not answer:
                continue
            parts.append(str(answer))
            yield _chunk(completion_id, created, model, {"content": str(answer)})
            last_emit = time.monotonic()
    except UpstreamError as exc:
        request_id = err.new_id()
        status = err.normalize_status(exc.status)
        err.record(request_id, status, exc)
        yield "data: " + json.dumps({
            "error": {
                "message": f"{err.public_message(status)}（编号 {request_id}）",
                "type": "upstream_error",
                "code": status,
            },
        }, ensure_ascii=False) + "\n\n"
        yield "data: [DONE]\n\n"
        return

    if store is not None and store_key:
        store.put(store_key, conversation_id, chat_hist_id)
    if store is not None:
        if started:
            store.register(conversation_id)
        else:
            store.touch(conversation_id)

    yield _chunk(completion_id, created, model, {}, finish="stop")
    if include_usage:
        text = "".join(parts)
        yield "data: " + json.dumps({
            "id": completion_id,
            "object": "chat.completion.chunk",
            "created": created,
            "model": model,
            "choices": [],
            "usage": {
                "prompt_tokens": estimate_tokens(prompt_text),
                "completion_tokens": estimate_tokens(text),
                "total_tokens": estimate_tokens(prompt_text) + estimate_tokens(text),
            },
        }, ensure_ascii=False) + "\n\n"
    yield "data: [DONE]\n\n"


async def _complete(
    dify: DifyClient,
    payload: dict[str, Any],
    completion_id: str,
    created: int,
    model: str,
    query: str,
    store: ConversationStore | None = None,
    store_key: str = "",
    started: bool = True,
    extra_headers: dict[str, str] | None = None,
) -> Any:
    parts: list[str] = []
    conversation_id = ""
    chat_hist_id = ""
    try:
        async for event in dify.stream_chat(payload):
            if event.get("conversationId"):
                conversation_id = str(event["conversationId"])
            if event.get("chatHistId"):
                chat_hist_id = str(event["chatHistId"])
            if event.get("event") != "message":
                continue
            answer = event.get("answer")
            if answer:
                parts.append(str(answer))
    except UpstreamError as exc:
        return sanitized_error(exc, exc.status)

    if store is not None and store_key:
        store.put(store_key, conversation_id, chat_hist_id)
    if store is not None:
        if started:
            store.register(conversation_id)
        else:
            store.touch(conversation_id)

    text = "".join(parts)
    # 非流式能等到上游返回
    if extra_headers is not None:
        extra_headers = {**extra_headers, "X-CGPT-Conversation": conversation_id or ""}
    return JSONResponse(content={
        "id": completion_id,
        "object": "chat.completion",
        "created": created,
        "model": model,
        "choices": [{
            "index": 0,
            "message": {"role": "assistant", "content": text},
            "finish_reason": "stop",
        }],
        "usage": {
            "prompt_tokens": estimate_tokens(query),
            "completion_tokens": estimate_tokens(text),
            "total_tokens": estimate_tokens(query) + estimate_tokens(text),
        },
    }, headers=extra_headers)
