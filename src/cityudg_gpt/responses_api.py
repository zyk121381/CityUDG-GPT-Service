# OpenAI Responses API（/v1/responses）兼容层。

from __future__ import annotations

import json
import logging
import time
import uuid
from collections.abc import AsyncIterator
from typing import Any

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse, StreamingResponse

from . import errors as err
from .config import Settings
from .openai_api import (
    ConversationStore,
    ModelRegistry,
    _read_bool,
    _resolve_keys,
    sanitized_error,
)
from .upstream import DifyClient, UpstreamError, build_payload, compose, estimate_tokens

router = APIRouter()
log = logging.getLogger("cityudg")


# ---------------- 输入解析 ----------------
def _content_text(content: Any) -> str:
    if content is None:
        return ""
    if isinstance(content, str):
        return content.strip()
    if isinstance(content, list):
        parts: list[str] = []
        for item in content:
            if isinstance(item, str):
                parts.append(item)
            elif isinstance(item, dict):
                text = item.get("text")
                if text:
                    parts.append(str(text))
        return "".join(parts).strip()
    return str(content).strip()


def _collect(body: dict[str, Any]) -> tuple[str, list[dict[str, str]]]:
    # 解析 instructions + input，得到 (系统文本, 轮次列表)。
    instructions = body.get("instructions")
    if isinstance(instructions, list):
        instructions = _content_text(instructions)
    instructions = str(instructions or "").strip()

    raw = body.get("input")
    turns: list[dict[str, str]] = []

    if isinstance(raw, str):
        if raw.strip():
            turns.append({"role": "user", "text": raw.strip()})
        return instructions, turns
    if not isinstance(raw, list):
        return instructions, turns

    for item in raw:
        if isinstance(item, str):
            if item.strip():
                turns.append({"role": "user", "text": item.strip()})
            continue
        if not isinstance(item, dict):
            continue

        itype = str(item.get("type") or "message")
        if itype in ("message", "input_message"):
            role = str(item.get("role") or "user")
            if role == "developer":
                role = "system"
            text = _content_text(item.get("content"))
            if not text:
                continue
            if role == "system":
                instructions = (instructions + "\n\n" + text).strip() if instructions else text
            else:
                turns.append({"role": role, "text": text})
        elif itype == "function_call_output":
            text = _content_text(item.get("output"))
            if text:
                turns.append({"role": "tool", "text": text})
        # function_call / reasoning 之类直接忽略
    return instructions, turns


# ---------------- 响应对象 ----------------
def _usage(prompt_text: str, completion_text: str) -> dict[str, Any]:
    pin = estimate_tokens(prompt_text)
    pout = estimate_tokens(completion_text)
    return {
        "input_tokens": pin,
        "input_tokens_details": {"cached_tokens": 0, "cache_write_tokens": 0},
        "output_tokens": pout,
        "output_tokens_details": {"reasoning_tokens": 0},
        "total_tokens": pin + pout,
    }


def _message_item(item_id: str, text: str, status: str = "completed") -> dict[str, Any]:
    content: list[dict[str, Any]] = []
    if status == "completed":
        content = [{"type": "output_text", "text": text, "annotations": [], "logprobs": []}]
    return {
        "id": item_id,
        "type": "message",
        "status": status,
        "role": "assistant",
        "content": content,
    }


def _response_obj(
    response_id: str,
    created: float,
    model: str,
    status: str,
    *,
    output: list[dict[str, Any]] | None = None,
    usage: dict[str, Any] | None = None,
    instructions: str = "",
    previous_response_id: str | None = None,
    error: dict[str, Any] | None = None,
) -> dict[str, Any]:
    return {
        "id": response_id,
        "object": "response",
        "created_at": created,
        "status": status,
        "model": model,
        "output": output or [],
        "parallel_tool_calls": True,
        "tool_choice": "auto",
        "tools": [],
        "usage": usage,
        "error": error,
        "incomplete_details": None,
        "instructions": instructions or None,
        "metadata": {},
        "temperature": 1.0,
        "top_p": 1.0,
        "max_output_tokens": None,
        "previous_response_id": previous_response_id,
        "truncation": "disabled",
        "user": None,
    }


def _event(name: str, payload: dict[str, Any]) -> str:
    return f"event: {name}\ndata: " + json.dumps(payload, ensure_ascii=False) + "\n\n"


# ---------------- 路由 ----------------
@router.post("/v1/responses")
async def create_response(request: Request) -> Any:
    try:
        body = await request.json()
    except Exception:
        return JSONResponse(
            {"error": {"message": "请求体不是合法 JSON", "type": "invalid_request_error"}},
            status_code=400,
        )
    if not isinstance(body, dict):
        return JSONResponse(
            {"error": {"message": "请求体必须是 JSON 对象", "type": "invalid_request_error"}},
            status_code=400,
        )

    instructions, turns = _collect(body)
    if not turns:
        return JSONResponse(
            {"error": {"message": "input 里没有可用文本内容", "type": "invalid_request_error"}},
            status_code=400,
        )

    cfg: Settings = request.app.state.settings
    registry: ModelRegistry = request.app.state.registry
    dify: DifyClient = request.app.state.dify
    store: ConversationStore = request.app.state.conversations
    responses = request.app.state.responses

    model = await registry.resolve(body.get("model"))
    previous_id = body.get("previous_response_id")
    previous_id = str(previous_id) if previous_id else ""

    user_texts = [t["text"] for t in turns if t["role"] == "user"]

    lookup_key, store_key, chain_mode = _resolve_keys(request, body, user_texts, cfg.auto_chain)

    chained = None
    if previous_id:
        chained = responses.get(previous_id)
        if chained is not None and not chained.get("conversationId"):
            chained = None
    if chained is None and lookup_key:
        chained = store.get(lookup_key)

    if chained:
        query = turns[-1]["text"]
        conversation_id = str(chained["conversationId"])
        parent_id = str(chained.get("chatHistId") or "1")
    else:
        query = compose(instructions, turns)
        conversation_id = ""
        parent_id = "1"

    session = await request.app.state.auth.ensure()
    payload = build_payload(
        query=query,
        model=model,
        user_id=session.user_id or cfg.user_id,
        search=_read_bool(body, "search_enabled", cfg.default_search),
        library=_read_bool(body, "library_enabled", cfg.default_library),
        thinking=_read_bool(body, "thinking_enabled", cfg.default_thinking),
        parent_chat_history_id=parent_id,
        conversation_id=conversation_id,
    )

    started = chained is None
    response_id = "resp_" + uuid.uuid4().hex
    item_id = "msg_" + uuid.uuid4().hex
    created = time.time()

    log.info(
        "[responses] turns=%d users=%d mode=%s chained=%s conv=%s qlen=%d",
        len(turns), len(user_texts), chain_mode,
        not started, conversation_id or "-", len(query),
    )

    meta: dict[str, str] = {}
    if cfg.debug_headers:
        meta = {
            "X-CGPT-Chained": "1" if chained else "0",
            "X-CGPT-Conversation": conversation_id or "",
            "X-CGPT-Query-Len": str(len(query)),
        }

    if not _read_bool(body, "stream", False):
        return await _run_complete(
            dify, payload, response_id, item_id, created, model, query,
            instructions, previous_id, store, store_key, started, responses, meta,
        )

    return StreamingResponse(
        _run_stream(
            dify, payload, response_id, item_id, created, model, query,
            instructions, previous_id, store, store_key, started, responses,
        ),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
            "X-Accel-Buffering": "no",
            **meta,
        },
    )


@router.get("/v1/responses/{response_id}")
async def get_response(response_id: str, request: Request) -> Any:
    responses = request.app.state.responses
    stored = responses.get_full(response_id)
    if not stored:
        return JSONResponse(
            {"error": {"message": f"没有这个 response：{response_id}", "type": "invalid_request_error"}},
            status_code=404,
        )
    return JSONResponse(stored)


@router.delete("/v1/responses/{response_id}")
async def delete_response(response_id: str, request: Request) -> Any:
    request.app.state.responses.drop(response_id)
    return {"id": response_id, "object": "response.deleted", "deleted": True}


# ---------------- 执行 ----------------
async def _run_stream(
    dify: DifyClient,
    payload: dict[str, Any],
    response_id: str,
    item_id: str,
    created: float,
    model: str,
    query: str,
    instructions: str,
    previous_id: str,
    store: ConversationStore,
    store_key: str,
    started: bool,
    responses: Any,
) -> AsyncIterator[str]:
    seq = 0

    def nxt() -> int:
        nonlocal seq
        seq += 1
        return seq

    created_response = _response_obj(
        response_id, created, model, "in_progress",
        instructions=instructions, previous_response_id=previous_id or None,
    )
    yield _event("response.created", {
        "type": "response.created", "response": created_response, "sequence_number": nxt(),
    })
    yield _event("response.in_progress", {
        "type": "response.in_progress", "response": created_response, "sequence_number": nxt(),
    })
    yield _event("response.output_item.added", {
        "type": "response.output_item.added",
        "output_index": 0,
        "item": _message_item(item_id, "", "in_progress"),
        "sequence_number": nxt(),
    })
    yield _event("response.content_part.added", {
        "type": "response.content_part.added",
        "item_id": item_id,
        "output_index": 0,
        "content_index": 0,
        "part": {"type": "output_text", "text": "", "annotations": [], "logprobs": []},
        "sequence_number": nxt(),
    })

    parts: list[str] = []
    conversation_id = ""
    chat_hist_id = ""
    last_emit = time.monotonic()
    failure: UpstreamError | None = None

    try:
        async for event in dify.stream_chat(payload):
            if event.get("conversationId"):
                conversation_id = str(event["conversationId"])
            if event.get("chatHistId"):
                chat_hist_id = str(event["chatHistId"])

            if event.get("event") != "message":
                if time.monotonic() - last_emit > 8:
                    yield ": keepalive\n\n"
                    last_emit = time.monotonic()
                continue
            answer = event.get("answer")
            if not answer:
                continue
            chunk = str(answer)
            parts.append(chunk)
            yield _event("response.output_text.delta", {
                "type": "response.output_text.delta",
                "item_id": item_id,
                "output_index": 0,
                "content_index": 0,
                "delta": chunk,
                "logprobs": [],
                "sequence_number": nxt(),
            })
            last_emit = time.monotonic()
    except UpstreamError as exc:
        failure = exc

    if failure is not None:
        request_id = err.new_id()
        status = err.normalize_status(failure.status)
        err.record(request_id, status, failure)
        failed = _response_obj(
            response_id, created, model, "failed",
            instructions=instructions, previous_response_id=previous_id or None,
            error={
                "code": "upstream_error",
                "message": f"{err.public_message(status)}（编号 {request_id}）",
            },
        )
        yield _event("response.failed", {
            "type": "response.failed", "response": failed, "sequence_number": nxt(),
        })
        return

    text = "".join(parts)
    usage = _usage(query, text)

    if store is not None and store_key:
        store.put(store_key, conversation_id, chat_hist_id)
    if store is not None:
        if started:
            store.register(conversation_id)
        else:
            store.touch(conversation_id)
    responses.put(response_id, conversation_id, chat_hist_id)

    yield _event("response.output_text.done", {
        "type": "response.output_text.done",
        "item_id": item_id,
        "output_index": 0,
        "content_index": 0,
        "text": text,
        "logprobs": [],
        "sequence_number": nxt(),
    })
    yield _event("response.content_part.done", {
        "type": "response.content_part.done",
        "item_id": item_id,
        "output_index": 0,
        "content_index": 0,
        "part": {"type": "output_text", "text": text, "annotations": [], "logprobs": []},
        "sequence_number": nxt(),
    })
    final_item = _message_item(item_id, text)
    yield _event("response.output_item.done", {
        "type": "response.output_item.done",
        "output_index": 0,
        "item": final_item,
        "sequence_number": nxt(),
    })

    done = _response_obj(
        response_id, created, model, "completed",
        output=[final_item], usage=usage, instructions=instructions,
        previous_response_id=previous_id or None,
    )
    responses.put_full(response_id, done)
    yield _event("response.completed", {
        "type": "response.completed", "response": done, "sequence_number": nxt(),
    })


async def _run_complete(
    dify: DifyClient,
    payload: dict[str, Any],
    response_id: str,
    item_id: str,
    created: float,
    model: str,
    query: str,
    instructions: str,
    previous_id: str,
    store: ConversationStore,
    store_key: str,
    started: bool,
    responses: Any,
    extra_headers: dict[str, str],
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
    item = _message_item(item_id, text)
    done = _response_obj(
        response_id, created, model, "completed",
        output=[item], usage=_usage(query, text), instructions=instructions,
        previous_response_id=previous_id or None,
    )
    responses.put(response_id, conversation_id, chat_hist_id)
    responses.put_full(response_id, done)
    headers = {**extra_headers, "X-CGPT-Conversation": conversation_id or ""}
    return JSONResponse(done, headers=headers)
