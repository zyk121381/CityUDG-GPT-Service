#!/usr/bin/env python
"""
命令行入口。
    python run.py serve            起服务（默认）
    python run.py login            强制重登，打印结果
    python run.py check            校验当前 token 还能不能用
    python run.py token            打印当前 token
    python run.py models           列出上游模型
"""

from __future__ import annotations

import asyncio
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT / "src"))

from cityudg_gpt.auth import AuthError, AuthManager  # noqa: E402
from cityudg_gpt.config import load  # noqa: E402


def _serve() -> None:
    import logging

    import uvicorn

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)-7s %(name)s: %(message)s",
    )
    cfg = load()
    uvicorn.run(
        "cityudg_gpt.server:app",
        host=cfg.host,
        port=cfg.port,
        log_level="info",
        reload=False,
        server_header=False,
        proxy_headers=True,
    )


async def _login() -> int:
    cfg = load()
    if not (cfg.account and cfg.password):
        print("先在这两项填好再登录：.env → CITYU_ACCOUNT / CITYU_PASSWORD")
        return 2
    manager = AuthManager(cfg)
    try:
        session = await manager._login_password()
        manager._write_cache(session)
    except AuthError as exc:
        print(f"登录失败：{exc}")
        return 1
    ok = await manager.validate(session.token)
    print(json.dumps({
        "ok": ok,
        "user": session.display_name,
        "account": session.user.get("AccountName"),
        "user_id": session.user_id,
        "account_id": session.account_id,
        "token_prefix": session.token[:8] + "...",
        "token_length": len(session.token),
        "obtained_at": session.obtained_at,
        "saved_to": str(cfg.token_file),
    }, ensure_ascii=False, indent=2))
    return 0 if ok else 2


async def _check() -> int:
    cfg = load()
    manager = AuthManager(cfg)
    try:
        session = await manager.ensure()
    except AuthError as exc:
        print(f"没有可用会话：{exc}")
        return 1
    ok = await manager.validate(session.token)
    print(json.dumps({
        "ok": ok,
        "user": session.display_name,
        "source": session.source,
        "obtained_at": session.obtained_at,
    }, ensure_ascii=False, indent=2))
    return 0 if ok else 2


async def _token() -> int:
    cfg = load()
    manager = AuthManager(cfg)
    session = await manager.ensure(force=True)
    print(session.token)
    return 0


async def _models() -> int:
    cfg = load()
    manager = AuthManager(cfg)
    session = await manager.ensure()
    models = await manager.list_models()
    if not models:
        print(f"没拿到模型列表（token 可能失效：{session.token[:8]}...）")
        return 1
    for item in models:
        print(f"{item.get('modelValue'):<16} {item.get('modelName')}")
    return 0


def main() -> int:
    command = sys.argv[1] if len(sys.argv) > 1 else "serve"
    table = {
        "serve": None,
        "login": _login,
        "check": _check,
        "token": _token,
        "models": _models,
    }
    if command not in table:
        print(__doc__)
        return 2
    if command == "serve":
        _serve()
        return 0
    return asyncio.run(table[command]())


if __name__ == "__main__":
    raise SystemExit(main())
