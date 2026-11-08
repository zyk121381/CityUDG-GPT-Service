# 配置加载：.env + config.yaml，支持 ${ENV} 展开。

from __future__ import annotations

import os
import re
from pathlib import Path
from typing import Any

import yaml

ROOT = Path(__file__).resolve().parents[2]
_PLACEHOLDER = re.compile(r"\$\{([A-Za-z_][A-Za-z0-9_]*)\}")


def load_dotenv(path: Path) -> None:
    if not path.is_file():
        return
    for raw in path.read_text("utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        if line.lower().startswith("export "):
            line = line[7:].strip()
        key, _, value = line.partition("=")
        key = key.strip()
        value = value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
            value = value[1:-1]
        if key:
            os.environ.setdefault(key, value)


def _expand(node: Any) -> Any:
    if isinstance(node, str):
        return _PLACEHOLDER.sub(lambda m: os.environ.get(m.group(1), ""), node).strip()
    if isinstance(node, dict):
        return {k: _expand(v) for k, v in node.items()}
    if isinstance(node, list):
        return [_expand(v) for v in node]
    return node


def _truthy(value: Any) -> bool:
    if isinstance(value, bool):
        return value
    return str(value).strip().lower() in ("1", "true", "yes", "on")


def _number(value: Any, default: float) -> float:
    if value is None or value == "":
        return default
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


class Settings:
    def __init__(self, raw: dict[str, Any]):
        self.raw = raw or {}
        self.server = self.raw.get("server") or {}
        self.upstream = self.raw.get("upstream") or {}
        self.auth = self.raw.get("auth") or {}
        self.openai = self.raw.get("openai") or {}
        self.proxy = self.raw.get("proxy") or {}
        self.browser = self.raw.get("browser") or {}
        self.conversations = self.raw.get("conversations") or {}

        self.idp = self.auth.get("idp") or {}
        self.sso = self.auth.get("sso") or {}

    # ---- server ----
    @property
    def host(self) -> str:
        return str(self.server.get("host") or "127.0.0.1")

    @property
    def port(self) -> int:
        return int(self.server.get("port") or 8787)

    @property
    def expose_docs(self) -> bool:
        return _truthy(self.server.get("expose_docs", False))

    @property
    def debug_headers(self) -> bool:
        return _truthy(self.server.get("debug_headers", False))

    @property
    def log_requests(self) -> bool:
        return _truthy(self.server.get("log_requests", False))

    # ---- upstream ----
    @property
    def base_url(self) -> str:
        return str(self.upstream.get("base") or "https://gpt.cityu-dg.edu.cn").rstrip("/")

    @property
    def timeout(self) -> float:
        return float(self.upstream.get("timeout") or 600)

    # ---- auth ----
    @property
    def auth_mode(self) -> str:
        return str(self.auth.get("mode") or "auto").strip().lower()

    @property
    def token_file(self) -> Path:
        p = Path(str(self.auth.get("token_file") or "data/token.json"))
        return p if p.is_absolute() else ROOT / p

    @property
    def static_token(self) -> str:
        return str(self.auth.get("token") or "").strip()

    @property
    def account(self) -> str:
        return str(self.auth.get("account") or "").strip()

    @property
    def password(self) -> str:
        return str(self.auth.get("password") or "").strip()

    @property
    def user_id(self) -> str:
        return str(self.auth.get("user_id") or "").strip()

    @property
    def account_id(self) -> str:
        return str(self.auth.get("account_id") or "").strip()

    @property
    def keepalive_interval(self) -> float:
        # 后台探一次 token时间。0 = 关闭。
        try:
            return float(self.auth.get("keepalive_interval", 600))
        except (TypeError, ValueError):
            return 600.0

    def idp_url(self, key: str) -> str:
        origin = str(self.idp.get("origin") or "https://idp.cityu-dg.edu.cn").rstrip("/")
        path = str(self.idp.get(key) or "")
        return origin + path if path.startswith("/") else origin + "/" + path

    # ---- openai ----
    @property
    def default_model(self) -> str:
        return str(self.openai.get("default_model") or "gpt-4o")

    @property
    def default_search(self) -> bool:
        return _truthy(self.openai.get("search_enabled", False))

    @property
    def default_library(self) -> bool:
        return _truthy(self.openai.get("library_enabled", False))

    @property
    def default_thinking(self) -> bool:
        return _truthy(self.openai.get("thinking_enabled", False))

    @property
    def owned_by(self) -> str:
        return str(self.openai.get("owned_by") or "system")

    # ---- proxy ----
    @property
    def proxy_enabled(self) -> bool:
        return _truthy(self.proxy.get("enabled", True))

    @property
    def proxy_password(self) -> str:
        return str(self.proxy.get("password") or "").strip()

    @property
    def api_key(self) -> str:
        return str(self.proxy.get("api_key") or "").strip()

    # ---- conversations ----
    @property
    def conversations_persist(self) -> bool:
        return _truthy(self.conversations.get("persist", True))

    @property
    def auto_chain(self) -> bool:
        # 按消息内容自动串联上游会话。只在单租户场景安全，默认关闭。
        return _truthy(self.conversations.get("auto_chain", False))

    @property
    def conversations_file(self) -> Path:
        p = Path(str(self.conversations.get("store_file") or "data/conversations.json"))
        return p if p.is_absolute() else ROOT / p

    @property
    def _auto_delete(self) -> dict[str, Any]:
        return self.conversations.get("auto_delete") or {}

    @property
    def auto_delete_enabled(self) -> bool:
        return _truthy(self._auto_delete.get("enabled", False))

    @property
    def delete_idle_seconds(self) -> float:
        # 空闲多久后删除，0 表示不按空闲删除。
        return _number(self._auto_delete.get("idle_seconds"), 3600.0)

    @property
    def delete_max_age_seconds(self) -> float:
        # 创建后多久强制删除，0 表示不限。
        return _number(self._auto_delete.get("max_age_seconds"), 0.0)

    @property
    def delete_sweep_seconds(self) -> float:
        return max(10.0, _number(self._auto_delete.get("sweep_seconds"), 300.0))

    def sso_url(self, key: str) -> str:
        path = str(self.sso.get(key) or "")
        return self.base_url + path

    def browser_profile(self):  # noqa: ANN201 - 避免循环导入
        from .browser import BrowserProfile

        return BrowserProfile(self.browser, self.base_url)


def load(config_path: Path | None = None) -> Settings:
    load_dotenv(ROOT / ".env")

    path = config_path or (ROOT / "config.yaml")
    if not path.is_file():
        example = ROOT / "config.example.yaml"
        if example.is_file():
            path = example
    raw: dict[str, Any] = {}
    if path.is_file():
        raw = yaml.safe_load(path.read_text("utf-8")) or {}
    return Settings(_expand(raw))
