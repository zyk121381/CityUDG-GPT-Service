"""
出站请求伪装。

  GET  /api/...   accept: application/json, text/plain, */*
  POST /api/...   accept: */*（前端用的是裸 fetch）+ origin + content-type

"""

from __future__ import annotations

import importlib.util
from typing import Any

DEFAULT_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/156.0.0.0 Safari/537.36"
)
DEFAULT_SEC_CH_UA = '"Not:A-Brand";v="8", "Chromium";v="156", "Google Chrome";v="156"'
DEFAULT_ACCEPT_LANGUAGE = (
    "zh-CN,zh;q=0.9,en;q=0.8,en-GB;q=0.7,en-US;q=0.6,zh-TW;q=0.5,fr-FR;q=0.4,fr;q=0.3"
)
AJAX_ACCEPT = "application/json, text/plain, */*"
RAW_ACCEPT = "*/*"


def supported_encodings() -> str:
    # 按本机实际能解的压缩算法声明 accept-encoding。
    encodings = ["gzip", "deflate"]
    if importlib.util.find_spec("brotli") or importlib.util.find_spec("brotlicffi"):
        encodings.append("br")
    if importlib.util.find_spec("zstandard"):
        encodings.append("zstd")
    return ", ".join(encodings)


def http2_available() -> bool:
    return importlib.util.find_spec("h2") is not None


class BrowserProfile:
    # 一组可配置的浏览器指纹。
    def __init__(self, conf: dict[str, Any] | None = None, base_url: str = "") -> None:
        conf = conf or {}
        self.enabled = _flag(conf.get("impersonate", True))
        self.user_agent = str(conf.get("user_agent") or "").strip() or DEFAULT_UA
        self.sec_ch_ua = str(conf.get("sec_ch_ua") or "").strip() or DEFAULT_SEC_CH_UA
        self.accept_language = str(conf.get("accept_language") or "").strip() or DEFAULT_ACCEPT_LANGUAGE
        self.referer_path = str(conf.get("referer_path") or "/conversation-mode").strip() or "/conversation-mode"
        self.want_http2 = _flag(conf.get("http2", True))
        self.base_url = base_url.rstrip("/")

    @property
    def http2(self) -> bool:
        return self.want_http2 and http2_available()

    @property
    def referer(self) -> str:
        return self.base_url + self.referer_path

    def headers(self, *, accept: str = AJAX_ACCEPT, post: bool = False,
                extra: dict[str, str] | None = None) -> dict[str, str]:
        if not self.enabled:
            merged = {"accept": accept}
            merged.update(extra or {})
            return merged

        headers = {
            "accept": accept,
            "accept-encoding": supported_encodings(),
            "accept-language": self.accept_language,
            "priority": "u=1, i",
            "referer": self.referer,
            "sec-ch-ua": self.sec_ch_ua,
            "sec-ch-ua-mobile": "?0",
            "sec-ch-ua-platform": '"Windows"',
            "sec-fetch-dest": "empty",
            "sec-fetch-mode": "cors",
            "sec-fetch-site": "same-origin",
            "user-agent": self.user_agent,
        }
        if post:
            # 浏览器只在非 GET/HEAD 上带 origin
            headers["origin"] = self.base_url
        if extra:
            headers.update({k: v for k, v in extra.items() if v is not None})
        return headers

    def json_post(self, extra: dict[str, str] | None = None) -> dict[str, str]:
        return self.headers(accept=AJAX_ACCEPT, post=True, extra=extra)

    def raw_post(self, extra: dict[str, str] | None = None) -> dict[str, str]:
        # 前端对 /api/dify/chat 用的是裸 fetch，accept 就是 */*。
        return self.headers(accept=RAW_ACCEPT, post=True, extra=extra)

    def json_get(self, extra: dict[str, str] | None = None) -> dict[str, str]:
        return self.headers(accept=AJAX_ACCEPT, post=False, extra=extra)


def _flag(value: Any) -> bool:
    if isinstance(value, bool):
        return value
    return str(value).strip().lower() in ("1", "true", "yes", "on")


def client_kwargs(profile: BrowserProfile, timeout: Any) -> dict[str, Any]:
    # 构造一个和浏览器行为接近的 httpx.AsyncClient 参数集。
    return {
        "timeout": timeout,
        "follow_redirects": True,
        "http2": profile.http2,
        "headers": {},  # 具体头由每次请求带，避免被默认值的 User-Agent 污染
    }
