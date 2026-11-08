# 学校 SSO 登录

from __future__ import annotations

import asyncio
import json
import time
import uuid
from datetime import datetime
from typing import Any
from urllib.parse import parse_qs, urljoin, urlsplit

import httpx

from .browser import BrowserProfile
from .config import Settings


class AuthError(RuntimeError):
    pass


USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/156.0.0.0 Safari/537.36"
)  # 实际出站头由 BrowserProfile 统一生成


def _json(resp: httpx.Response) -> Any:
    try:
        return resp.json()
    except Exception:
        return None


def _dig(node: Any, path: str) -> Any:
    cur = node
    for part in path.split("."):
        if isinstance(cur, dict):
            cur = cur.get(part)
        elif isinstance(cur, list):
            try:
                cur = cur[int(part)]
            except (ValueError, IndexError):
                return None
        else:
            return None
        if cur is None:
            return None
    return cur


class Session:
    # 一次有效的上游会话。

    def __init__(self, token: str, user: dict[str, Any] | None = None,
                 user_id: str = "", account_id: str = "",
                 obtained_at: str = "", source: str = "") -> None:
        self.token = token
        self.user = user or {}
        self.user_id = user_id or str(self.user.get("StaffStuId") or "")
        self.account_id = account_id or str(self.user.get("AccountId") or "")
        self.obtained_at = obtained_at or datetime.now().isoformat(timespec="seconds")
        self.source = source

    @property
    def display_name(self) -> str:
        return str(self.user.get("Name") or self.user.get("AccountName") or "")

    def to_dict(self) -> dict[str, Any]:
        return {
            "token": self.token,
            "user": self.user,
            "user_id": self.user_id,
            "account_id": self.account_id,
            "obtained_at": self.obtained_at,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any], source: str = "") -> "Session":
        return cls(
            token=str(data.get("token") or ""),
            user=data.get("user") or {},
            user_id=str(data.get("user_id") or ""),
            account_id=str(data.get("account_id") or ""),
            obtained_at=str(data.get("obtained_at") or ""),
            source=source,
        )


class AuthManager:
    def __init__(self, cfg: Settings) -> None:
        self.cfg = cfg
        self._session: Session | None = None
        self._lock = asyncio.Lock()
        self._last_error = ""
        self._last_validated = 0.0
        self.profile = cfg.browser_profile()
        idp_origin = str(cfg.idp.get("origin") or "https://idp.cityu-dg.edu.cn")
        self.idp_profile = BrowserProfile(
            {**cfg.browser, "referer_path": "/frontend/login/"}, idp_origin
        )

    # ---------- 缓存 ----------
    def _read_cache(self) -> Session | None:
        path = self.cfg.token_file
        if not path.is_file():
            return None
        try:
            data = json.loads(path.read_text("utf-8"))
        except (OSError, json.JSONDecodeError):
            return None
        session = Session.from_dict(data, source="cache")
        if not session.token:
            return None
        if not session.user_id:
            session.user_id = self.cfg.user_id
        if not session.account_id:
            session.account_id = self.cfg.account_id
        return session

    def _write_cache(self, session: Session) -> None:
        path = self.cfg.token_file
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(json.dumps(session.to_dict(), ensure_ascii=False, indent=2), "utf-8")
            path.chmod(0o600)
        except OSError:
            pass

    # ---------- 对外 ----------
    @property
    def current(self) -> Session | None:
        return self._session

    def invalidate(self) -> None:
        self._session = None

    async def ensure(self, force: bool = False) -> Session:
        # 拿一个可用会话；force=True 强制重新登录。
        async with self._lock:
            if self._session and not force:
                return self._session

            mode = self.cfg.auth_mode

            if not force:
                cached = self._read_cache()
                if cached and mode != "password":
                    if await self.validate(cached.token) or mode == "token":
                        self._session = cached
                        return cached

            if mode in ("auto", "token"):
                static = self.cfg.static_token
                if static and mode == "token":
                    self._session = Session(
                        token=static,
                        user_id=self.cfg.user_id,
                        account_id=self.cfg.account_id,
                        source="env",
                    )
                    return self._session
                if static and mode == "auto" and not self.cfg.password:
                    self._session = Session(
                        token=static,
                        user_id=self.cfg.user_id,
                        account_id=self.cfg.account_id,
                        source="env",
                    )
                    return self._session

            if mode in ("auto", "password") and self.cfg.account and self.cfg.password:
                if mode == "password":
                    session = await self._login_password()
                else:
                    # auto：密码登录失败退到缓存 / 静态 token
                    try:
                        session = await self._login_password()
                    except AuthError as exc:
                        session = None
                        self._last_error = str(exc)
                if session is not None:
                    self._session = session
                    self._write_cache(session)
                    return session

            # 密码不可用时，退到最后一份缓存 / 静态 token，由调用方处理 401
            fallback = self._read_cache()
            if fallback:
                self._session = fallback
                return fallback
            static = self.cfg.static_token
            if static:
                self._session = Session(
                    token=static,
                    user_id=self.cfg.user_id,
                    account_id=self.cfg.account_id,
                    source="env",
                )
                return self._session

            hint = f"（上一次自动登录失败：{self._last_error}）" if self._last_error else ""
            raise AuthError(
                "没有可用的凭据：请在 .env 里填 CITYU_ACCOUNT / CITYU_PASSWORD，"
                f"或填 GPT_TOKEN（auth.mode 设为 token）。{hint}"
            )

    @property
    def last_error(self) -> str:
        return self._last_error

    async def validate(self, token: str) -> bool:
        # 用 /api/user/menus 探 token 是否有效（不消耗对话额度）。
        try:
            async with httpx.AsyncClient(
                timeout=20, follow_redirects=True, http2=self.profile.http2
            ) as client:
                resp = await client.get(
                    self.cfg.base_url + "/api/user/menus",
                    headers=self.profile.json_get({"Authorization": f"Bearer {token}"}),
                )
        except httpx.HTTPError:
            return False
        if resp.status_code == 401:
            return False
        body = _json(resp)
        if not isinstance(body, dict):
            return resp.status_code == 200
        code = body.get("code")
        if code in (401, 4001):
            return False
        return resp.status_code == 200

    async def relogin(self, stale_token: str | None = None) -> Session:
        # 重新登录。
        async with self._lock:
            current = self._session
            if stale_token and current and current.token and current.token != stale_token:
                return current

            if self.cfg.auth_mode == "token":
                raise AuthError("当前是纯 token 模式（auth.mode=token），不会自动重登。")

            if not (self.cfg.account and self.cfg.password):
                raise AuthError(
                    "登录已过期，但没有配置学号密码，无法自动重登。"
                    "请在 .env 填 CITYU_ACCOUNT / CITYU_PASSWORD。"
                )

            session = await self._login_password()
            self._session = session
            self._last_validated = time.time()
            self._write_cache(session)
            return session

    async def check_and_refresh(self) -> bool:
        # 后台巡检：token 还有效就不动，失效了立刻换一个。
        try:
            session = await self.ensure()
        except AuthError as exc:
            self._last_error = str(exc)
            return False

        if await self.validate(session.token):
            self._last_validated = time.time()
            return True

        try:
            await self.relogin(stale_token=session.token)
        except AuthError as exc:
            self._last_error = str(exc)
            return False
        return True

    # ---------- 密码登录 ----------
    async def _login_password(self) -> Session:
        account, secret = self.cfg.account, self.cfg.password
        idp_headers = self.idp_profile.json_post()

        async with httpx.AsyncClient(
            timeout=30, follow_redirects=True, http2=self.idp_profile.http2
        ) as client:
            # 1) IdP 的 clientId
            try:
                resp = await client.get(self.cfg.idp_url("login_data"), headers=idp_headers)
                client_id = _dig(_json(resp), "data.clientId")
            except httpx.HTTPError as exc:
                raise AuthError(f"取 IdP clientId 失败：{exc}") from exc
            if not client_id:
                raise AuthError("IdP 没有返回 clientId，接口可能变了。")

            # 2) sm2_code
            sm2_code = ""
            try:
                resp = await client.get(self.cfg.idp_url("init_code"), headers=idp_headers)
                sm2_code = _dig(_json(resp), "data.code") or ""
            except httpx.HTTPError:
                pass

            # 3) password grant
            payload = {
                "client_id": client_id,
                "grant_type": "password",
                "scope": "read",
                "username": account,
                "password": secret,
                "_enterprise_id": "",
                "sm2_code": sm2_code,
                "auto_login": False,
            }
            try:
                resp = await client.post(self.cfg.idp_url("token"), json=payload, headers=idp_headers)
                body = _json(resp) or {}
            except httpx.HTTPError as exc:
                raise AuthError(f"IdP 密码登录请求失败：{exc}") from exc

            access = _dig(body, "data.access_token") or _dig(body, "data.accessToken") or _dig(body, "access_token")
            if not access:
                reason = (
                    _dig(body, "message")
                    or _dig(body, "error_description")
                    or _dig(body, "error")
                    or resp.text[:200]
                )
                raise AuthError(f"学号密码登录被拒绝：{reason}")
            access = str(access)

            # 4) 换 GPT 的 code
            cookies = {c.name: c.value for c in client.cookies.jar}
            code = await self._fetch_code(client, cookies, access)

        # 5) code → token
        return await self._exchange_code(code, source="password")

    async def _fetch_code(self, client: httpx.AsyncClient, cookies: dict[str, str], access_token: str) -> str:
        try:
            info = await client.get(
                self.cfg.sso_url("info"),
                headers=self.profile.json_get(),
            )
            info_body = _json(info) or {}
        except httpx.HTTPError as exc:
            raise AuthError(f"取 SSO 配置失败：{exc}") from exc

        authorize = _dig(info_body, "data.authorizeUrl")
        client_id = _dig(info_body, "data.clientId")
        redirect_uri = _dig(info_body, "data.redirectUri")
        if not (authorize and client_id and redirect_uri):
            raise AuthError(f"getSsoInfo 返回不符合预期：{info.text[:300]}")

        state = str(uuid.uuid4())
        from urllib.parse import urlencode

        query = urlencode({
            "response_type": "code",
            "scope": "read",
            "client_id": client_id,
            "redirect_uri": redirect_uri,
            "state": state,
        })
        url = f"{authorize}?{query}"

        for attempt in ("cookie", "bearer"):
            code, last = await self._walk(client, url, cookies, access_token, attempt)
            if code:
                return code
        raise AuthError(
            "没拿到授权 code：IdP 会话既没带 cookie 也没认 Bearer。"
            f"最后停在 {last}。若出现二次认证页，请手动过一次登录。"
        )

    async def _walk(
        self,
        client: httpx.AsyncClient,
        url: str,
        cookies: dict[str, str],
        access_token: str,
        mode: str,
    ) -> tuple[str, str]:
        headers = self.idp_profile.headers(
            accept="text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,*/*;q=0.8",
            post=False,
        )
        if mode == "bearer":
            headers["authorization"] = f"Bearer {access_token}"

        current = url
        last = current
        for _ in range(8):
            try:
                resp = await client.get(
                    current,
                    headers=headers,
                    cookies=cookies,
                    follow_redirects=False,
                )
            except httpx.HTTPError as exc:
                raise AuthError(f"授权跳转失败：{exc}") from exc

            location = resp.headers.get("location")
            if not location:
                break
            current = urljoin(current, location)
            last = current
            params = parse_qs(urlsplit(current).query)
            if params.get("code"):
                return params["code"][0], current
        return "", last

    async def _exchange_code(self, code: str, source: str = "sso") -> Session:
        async with httpx.AsyncClient(
            timeout=30, follow_redirects=True, http2=self.profile.http2
        ) as client:
            try:
                resp = await client.post(
                    self.cfg.sso_url("login"),
                    json={"code": code},
                    headers=self.profile.json_post(),
                )
            except httpx.HTTPError as exc:
                raise AuthError(f"用 code 换 token 失败：{exc}") from exc

        body = _json(resp)
        if not isinstance(body, dict):
            raise AuthError(f"loginSSO 返回不是 JSON：{resp.text[:300]}")
        if body.get("code") not in (0, None):
            raise AuthError(f"loginSSO 拒绝：{body.get('msg') or body}")

        data = body.get("data") or {}
        token = data.get("token")
        if not token:
            raise AuthError(f"loginSSO 未返回 token：{resp.text[:300]}")

        return Session(
            token=str(token),
            user=data.get("user") or {},
            user_id=self.cfg.user_id,
            account_id=self.cfg.account_id,
            source=source,
        )

    # ---------- 工具 ----------
    async def list_models(self) -> list[dict[str, Any]]:
        session = await self.ensure()
        async with httpx.AsyncClient(
            timeout=30, follow_redirects=True, http2=self.profile.http2
        ) as client:
            resp = await client.get(
                self.cfg.base_url + "/api/model/getModelSimpleList",
                params={"modelType": "1"},
                headers=self.profile.json_get({"Authorization": f"Bearer {session.token}"}),
            )
        body = _json(resp) or {}
        data = body.get("data")
        return data if isinstance(data, list) else []
