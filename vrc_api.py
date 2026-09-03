"""VRChat API 异步客户端封装（基于 aiohttp）。

职责：
- 邮箱/用户名 + 密码登录
- 两步验证（邮箱验证码 / TOTP）处理
- 登录会话持久化（auth cookie 存盘复用，避免掉线后重复登录）
- 玩家 / 地图 / 模型 / 群组信息查询
"""

import asyncio
import base64
import json
import os
import time
import urllib.parse

import aiohttp

# 优先使用 AstrBot 的日志体系（保证在平台日志中可见）；
# 独立运行/测试时回退到标准 logging 并自接控制台 handler。
try:
    from astrbot.api import logger  # noqa: PLC0415
except Exception:  # pragma: no cover - 非 AstrBot 环境
    import logging as _logging

    logger = _logging.getLogger("astrbot_plugin_vrc_tool")
    logger.setLevel(_logging.INFO)
    if not logger.handlers:
        _console_handler = _logging.StreamHandler()
        _console_handler.setFormatter(
            _logging.Formatter("[%(name)s][%(levelname)s] %(message)s")
        )
        logger.addHandler(_console_handler)
        logger.propagate = False

VERSION = "1.0.0"
BASE_URL = "https://api.vrchat.cloud/api/1"
AUTH_FILE = "auth.json"
CACHE_TTL = 3600  # 秒


class VRCLoginRequired(Exception):
    """登录凭据失效，需要重新登录"""


class VRCAuthFailed(Exception):
    """账号或密码错误等登录失败"""


class VRC2FARequired(Exception):
    """需要两步验证：method 为 'email'（邮箱验证码）或 'totp'"""

    def __init__(self, method: str):
        self.method = method
        super().__init__(method)


class VRCApi:
    """VRChat API 客户端。"""

    #: VRChat WAF 会拦截带占位联系信息的 User-Agent（如 example.com 邮箱）
    PLACEHOLDER_CONTACT_MARKERS = (
        "example.com",
        "example.org",
        "example.net",
        "yourdomain",
        "yourdomain.com",
        "@test",
        "test.com",
        "admin@example",
    )

    def __init__(
        self,
        data_dir: str,
        app_name: str = "astrbot-plugin-vrc-tool",
        app_contact: str = "support@baidu.com",
        request_interval: float = 1.0,
        fallback_username: str = "",
        fallback_password: str = "",
    ):
        self.data_dir = data_dir
        os.makedirs(data_dir, exist_ok=True)
        self._auth_path = os.path.join(data_dir, AUTH_FILE)
        # 懒加载：aiohttp.ClientSession 必须在事件循环内创建
        self._session: aiohttp.ClientSession | None = None
        self._app_name = (app_name or "astrbot-plugin-vrc-tool").strip()
        self._contact = (app_contact or "").strip()
        self._ua = self._build_ua()
        self._request_interval = max(0.0, float(request_interval or 1.0))
        self._last_request_ts = 0.0

        # 登录态
        self._auth_token = ""
        self._two_factor_cookie = ""  # 两步验证挑战时下发的 cookie
        self._email = fallback_username or ""
        self._password = fallback_password or ""
        self._need_2fa = False
        self._2fa_method = ""
        self._login_state = "none"  # none | ok | 2fa
        self._token_verified = False
        self._login_lock = asyncio.Lock()

        # 简单缓存，减少对 VRChat 的重复请求
        self._avatar_cache: dict[str, tuple[str, float]] = {}
        self._world_cache: dict[str, tuple[str, float]] = {}
        self._group_cache: dict[str, tuple[str, float]] = {}

        self._load_auth()

    def _build_ua(self) -> str:
        """构造 VRChat 要求的 User-Agent：应用名/版本 联系信息。"""
        ua = f"{self._app_name}/{VERSION}"
        if self._contact:
            ua += f" {self._contact}"
        return ua

    def ua_contact_invalid(self) -> bool:
        """联系信息是否为占位内容（VRChat WAF 会拒绝）。"""
        contact = self._contact.lower()
        if not contact:
            return True
        return any(marker in contact for marker in self.PLACEHOLDER_CONTACT_MARKERS)

    @staticmethod
    def _ua_blocked_message() -> str:
        return (
            "VRChat 要求 User-Agent 携带真实联系信息。请在插件配置中把 "
            "`app_contact` 改为你的真实联系邮箱或网址（例如 yourname@qq.com），"
            "占位邮箱如 admin@example.com 会被 VRChat 拒绝（HTTP 403）。"
        )

    # ------------------------------------------------------------------ #
    # 登录与会话持久化
    # ------------------------------------------------------------------ #
    @staticmethod
    def _basic_auth(email: str, password: str) -> str:
        """构造 VRChat 要求的 Basic 认证串：base64(urlencode(账号):urlencode(密码))"""
        raw = f"{urllib.parse.quote(email, safe='')}:{urllib.parse.quote(password, safe='')}"
        return base64.b64encode(raw.encode("utf-8")).decode("utf-8")

    def _load_auth(self):
        try:
            if os.path.exists(self._auth_path):
                with open(self._auth_path, encoding="utf-8") as f:
                    data = json.load(f)
                self._auth_token = str(data.get("auth_token") or "")
                self._email = str(data.get("email") or "") or self._email
                self._password = str(data.get("password") or "") or self._password
                self._need_2fa = bool(data.get("need_2fa"))
                self._2fa_method = str(data.get("2fa_method") or "")
                self._login_state = "2fa" if self._need_2fa else ("ok" if self._auth_token else "none")
                self._token_verified = False
        except Exception as e:
            logger.error(f"读取登录信息失败: {e}")

    def _save_auth(self):
        try:
            data = {
                "auth_token": self._auth_token,
                "email": self._email,
                "password": self._password,
                "need_2fa": self._need_2fa,
                "2fa_method": self._2fa_method,
                "login_time": int(time.time()),
            }
            with open(self._auth_path, "w", encoding="utf-8") as f:
                json.dump(data, f, ensure_ascii=False, indent=2)
        except Exception as e:
            logger.error(f"保存登录信息失败: {e}")

    async def login(self, email: str, password: str) -> str:
        """登录。成功返回 'ok'；需要两步验证时抛出 VRC2FARequired。"""
        async with self._login_lock:
            # 前置检查：User-Agent 联系信息必须是真实内容，否则 VRChat WAF 直接 403
            if self.ua_contact_invalid():
                raise VRCAuthFailed(self._ua_blocked_message())

            self._email = email.strip()
            self._password = password
            self._auth_token = ""
            self._two_factor_cookie = ""
            self._need_2fa = False
            self._2fa_method = ""
            self._token_verified = False
            basic = self._basic_auth(self._email, self._password)
            status, data = await self._request("GET", "/auth/user", basic_auth=basic, use_cookie=False)
            logger.debug(f"[VRC工具] 登录请求完成: HTTP {status}, body={data}, auth_token={'有' if self._auth_token else '无'}")

            msg = ""
            if isinstance(data, dict):
                err = data.get("error") or {}
                msg = str(err.get("message") or "")
                # 403：User-Agent 被 WAF 拦截
                if status == 403 and "user-agent" in msg.lower():
                    raise VRCAuthFailed(self._ua_blocked_message())
                req = data.get("requiresTwoFactorAuth")
                if req:
                    logger.info(
                        f"[VRC工具] 账号 {self._email} 需要两步验证（{req}），已发送验证码邮件，"
                        f"挑战cookie={'已捕获' if self._auth_token else '未捕获'}"
                    )
                    self._raise_2fa(req)
                if "2 factor authentication" in msg.lower() or "2fa" in msg.lower():
                    method = "email" if "email" in msg.lower() else "totp"
                    logger.info(
                        f"[VRC工具] 账号 {self._email} 需要两步验证（{method}），已发送验证码邮件，"
                        f"挑战cookie={'已捕获' if self._auth_token else '未捕获'}"
                    )
                    self._raise_2fa([method])
                if status == 200 and data.get("id"):
                    self._login_state = "ok"
                    self._token_verified = True
                    self._save_auth()
                    return "ok"
            raise VRCAuthFailed(f"登录失败（HTTP {status}）：{msg or '未知错误'}")

    def _raise_2fa(self, req):
        """根据 requiresTwoFactorAuth 字段设置 2FA 状态并抛出异常"""
        method = "email" if "emailOtp" in req else "totp"
        self._need_2fa = True
        self._2fa_method = method
        self._login_state = "2fa"
        self._save_auth()
        raise VRC2FARequired(method)

    async def verify_2fa(self, code: str) -> bool:
        """提交两步验证码。成功返回 True。"""
        async with self._login_lock:
            if self._login_state == "ok" and not self._need_2fa:
                return True
            if not self._need_2fa:
                raise VRCLoginRequired("当前没有待验证的两步验证，请先执行 /vrc登录")
            if not (self._email and self._password):
                raise VRCLoginRequired("请先执行 /vrc登录 <邮箱> <密码>")
            method = self._2fa_method or "email"
            path = "/auth/twofactorauth/emailotp/verify" if method == "email" else "/auth/twofactorauth/totp/verify"
            basic = self._basic_auth(self._email, self._password)
            # 校验接口是非认证端点：只携带登录挑战下发的 cookie，
            # 绝不能带 Basic Authorization（会被 VRChat WAF 以 403 拒绝）。
            # suppress_401：401 = 验证码错误/挑战失效，保留挑战 cookie 以便重试。
            status, data = await self._request(
                "POST", path, json_body={"code": str(code).strip()},
                use_cookie=True, suppress_401=True,
            )
            logger.info(
                f"[VRC工具] 验证码校验结果: HTTP {status}, body={data}, "
                f"携带cookie={'有' if self._auth_token or self._two_factor_cookie else '无'}"
            )
            if status == 200 and isinstance(data, dict) and data.get("verified"):
                # 重新拉取用户信息（/auth/user 是认证端点，可带 Basic），确保拿到 auth cookie
                try:
                    await self._request("GET", "/auth/user", basic_auth=basic, use_cookie=True)
                except VRCLoginRequired:
                    pass
                self._need_2fa = False
                self._2fa_method = ""
                self._two_factor_cookie = ""
                self._login_state = "ok"
                self._token_verified = True
                self._save_auth()
                return True
            return False

    async def ensure_login(self) -> str:
        """确保已登录。返回：'ok' | '2fa' | 'no_creds' | 'failed'"""
        if self._login_state == "ok" and self._auth_token:
            if self._token_verified:
                return "ok"
            # 进程内首次使用 token 时校验一次，避免凭据失效后的意外报错
            try:
                status, _ = await self._request("GET", "/auth/user", use_cookie=True)
                if status == 200:
                    self._token_verified = True
                    return "ok"
            except VRCLoginRequired:
                pass
            except Exception:
                # 网络波动不阻断，视为已登录
                return "ok"
            self._login_state = "none"
            self._token_verified = False
        if self._need_2fa:
            return "2fa"
        if not (self._email and self._password):
            return "no_creds"
        try:
            await self.login(self._email, self._password)
            return "ok"
        except VRC2FARequired:
            return "2fa"
        except VRCAuthFailed as e:
            logger.error(f"自动登录失败: {e}")
            return "failed"
        except Exception as e:
            logger.error(f"自动登录异常: {e}")
            return "failed"

    async def close(self):
        if self._session is not None:
            try:
                await self._session.close()
            except Exception:
                pass
            self._session = None

    def _get_session(self) -> aiohttp.ClientSession:
        """懒创建 aiohttp 会话（必须在事件循环内调用）。"""
        if self._session is None:
            self._session = aiohttp.ClientSession(cookie_jar=aiohttp.DummyCookieJar())
        return self._session

    # ------------------------------------------------------------------ #
    # 底层请求
    # ------------------------------------------------------------------ #
    async def _request(self, method, path, params=None, json_body=None, basic_auth=None, use_cookie=True, suppress_401=False):
        """底层请求。

        suppress_401=True 时，401 不抛 VRCLoginRequired、不清空会话 cookie，
        原样返回 (401, data)，供两步验证校验等"401=业务失败"的场景使用。
        """
        url = BASE_URL + path
        headers = {"User-Agent": self._ua, "Accept": "application/json"}
        if use_cookie:
            parts = []
            if self._auth_token:
                parts.append(f"auth={self._auth_token}")
            if self._two_factor_cookie:
                parts.append(f"twoFactorAuth={self._two_factor_cookie}")
            if parts:
                headers["Cookie"] = "; ".join(parts)
        if basic_auth:
            headers["Authorization"] = f"Basic {basic_auth}"

        wait = self._last_request_ts + self._request_interval - time.monotonic()
        if wait > 0:
            await asyncio.sleep(wait)
        self._last_request_ts = time.monotonic()

        async with self._get_session().request(
            method, url, params=params, json=json_body, headers=headers
        ) as resp:
            try:
                data = await resp.json(content_type=None)
            except Exception:
                data = {}
            # 捕获登录挑战 / 验证成功后下发的 auth、twoFactorAuth cookie
            # （两步验证校验时需要携带登录挑战时下发的 cookie）
            try:
                c_auth = resp.cookies.get("auth")
                if c_auth is not None and c_auth.value:
                    self._auth_token = c_auth.value
                c_tfa = resp.cookies.get("twoFactorAuth")
                if c_tfa is not None and c_tfa.value:
                    self._two_factor_cookie = c_tfa.value
            except Exception:
                pass

            if resp.status == 200 and use_cookie and self._auth_token:
                self._login_state = "ok"
            if resp.status == 401:
                if use_cookie and not basic_auth and not suppress_401:
                    self._auth_token = ""
                    self._login_state = "none"
                    self._token_verified = False
                    raise VRCLoginRequired("VRChat 登录已过期，请重新使用 /vrc登录 登录")
            if resp.status == 429:
                raise RuntimeError("VRChat API 请求过于频繁（429），请稍后再试")
            return resp.status, data

    # ------------------------------------------------------------------ #
    # 查询接口
    # ------------------------------------------------------------------ #
    async def query_user(self, query: str, strict_match: bool = False) -> dict:
        """按玩家昵称或玩家ID查询玩家完整信息。

        strict_match=True 时必须精准匹配昵称，否则返回带 _not_found 标记的
        占位 info（玩家ID 显示为"无法精准匹配查询到此人，请管理员手动审核"），
        供入群审核使用。
        会话过期时自动重新登录并重试一次。
        """
        try:
            return await self._query_user_inner(query, strict_match)
        except VRCLoginRequired:
            if await self.ensure_login() == "ok":
                return await self._query_user_inner(query, strict_match)
            raise

    async def _query_user_inner(self, query: str, strict_match: bool = False) -> dict:
        query = query.strip()
        if not query:
            raise ValueError("查询内容为空")

        def _not_found_info():
            return {
                "displayName": query,
                "id": "无法精准匹配查询到此人，请管理员手动审核",
                "_not_found": True,
            }

        if query.startswith("usr_"):
            uid = query
            status, user = await self._request("GET", f"/users/{uid}")
            if status != 200 or not isinstance(user, dict) or not user.get("id"):
                if strict_match:
                    return _not_found_info()
                raise ValueError(f"未找到玩家 {query}")
            return await self._build_user_info(user)

        status, data = await self._request("GET", "/users", params={"search": query, "n": 10})
        users = data if isinstance(data, list) else []
        picked = self._pick_exact(users, query, strict=strict_match)
        if not picked:
            # 严格模式精准匹配失败（或搜索结果为空）
            if strict_match:
                return _not_found_info()
            raise ValueError(f"未找到昵称为 {query} 的玩家")
        uid = picked.get("id", "")
        status, user = await self._request("GET", f"/users/{uid}")
        if status != 200 or not isinstance(user, dict) or not user.get("id"):
            if strict_match:
                return _not_found_info()
            raise ValueError(f"未找到玩家 {query}")
        return await self._build_user_info(user)

    async def _build_user_info(self, user: dict) -> dict:
        uid = user.get("id", "")
        info = {
            "displayName": user.get("displayName", "") or "",
            "id": uid,
            # 玩家头像（VRChat User.userIcon 为玩家自定义头像 URL，无则为空串）
            "iconUrl": user.get("userIcon", "") or "",
            # 在线状态：state 为 offline/online；status 为 join me/active/ask me/busy
            "state": user.get("state", "") or "",
            "status": user.get("status", "") or "",
            "statusDescription": user.get("statusDescription", "") or "",
            # 玩家简介
            "bio": user.get("bio", "") or "",
            # 账号创建日期（ISO 8601 字符串）
            "date_joined": user.get("date_joined", "") or "",
            # 信誉状态（trustLevel）：VRChat API 无直接字段，从 tags 推断
            "trustLevel": self._trust_level_from_tags(user.get("tags", [])),
        }
        # 正在使用的模型
        avatar_id = user.get("currentAvatar") or ""
        if avatar_id:
            try:
                name = await self._get_avatar_name(avatar_id)
                info["avatar"] = name or avatar_id
            except Exception:
                info["avatar"] = avatar_id
        # 正在展示的群组（represented group）
        try:
            status, gdata = await self._request("GET", f"/users/{uid}/groups/represented")
            if status == 200 and isinstance(gdata, dict) and gdata.get("id"):
                info["group"] = gdata.get("name") or gdata.get("shortCode") or gdata.get("id")
                info["group_id"] = gdata.get("id")
        except Exception:
            pass
        if not info.get("group"):
            gid = user.get("profilePicOverride") or ""
            if gid.startswith("grp_"):
                try:
                    name = await self._get_group_name(gid)
                    info["group"] = name or gid
                    info["group_id"] = gid
                except Exception:
                    pass
        # 当前位置 + 房间ID（不在线则不展示）
        location = user.get("location") or ""
        if location and str(location).strip() not in ("", "offline"):
            world_id = user.get("worldId") or ""
            instance_id = str(location).split(":", 1)[1] if ":" in str(location) else str(location)
            try:
                world_name = await self._get_world_name(world_id) if world_id else ""
            except Exception:
                world_name = ""
            info["world"] = world_name or world_id
            info["world_id"] = world_id
            info["instance_id"] = instance_id
        return info

    async def query_world(self, query: str) -> dict:
        """按地图昵称或地图ID查询地图信息。会话过期时自动重新登录并重试一次。"""
        try:
            return await self._query_world_inner(query)
        except VRCLoginRequired:
            if await self.ensure_login() == "ok":
                return await self._query_world_inner(query)
            raise

    async def _query_world_inner(self, query: str) -> dict:
        query = query.strip()
        if not query:
            raise ValueError("查询内容为空")
        if query.startswith("wrld_"):
            status, world = await self._request("GET", f"/worlds/{query}")
            if status != 200 or not isinstance(world, dict) or not world.get("id"):
                raise ValueError(f"未找到地图 {query}")
        else:
            status, data = await self._request("GET", "/worlds", params={"search": query, "n": 10})
            worlds = data if isinstance(data, list) else []
            picked = self._pick_exact(worlds, query)
            if not picked:
                raise ValueError(f"未找到名称为 {query} 的地图")
            status, world = await self._request("GET", f"/worlds/{picked.get('id', '')}")
            if status != 200 or not isinstance(world, dict) or not world.get("id"):
                raise ValueError(f"未找到地图 {query}")
        name = world.get("name") or ""
        self._world_cache[world.get("id", "")] = (name, time.monotonic())
        return {
            "name": name,
            "id": world.get("id", ""),
            "thumbnailImageUrl": world.get("thumbnailImageUrl") or "",
            "authorName": world.get("authorName") or "",
        }

    # ------------------------------------------------------------------ #
    # 缓存辅助
    # ------------------------------------------------------------------ #
    @staticmethod
    def _pick_exact(objs, query, strict: bool = False):
        """从搜索结果里精准匹配昵称/名称。

        strict=True 时必须存在完全相等的项，否则返回 None；
        strict=False（默认）时兜底返回第一项以保持旧有模糊匹配行为。
        """
        q = str(query).strip().lower()
        for o in objs:
            if isinstance(o, dict):
                name = str(o.get("displayName") or o.get("name") or "").strip().lower()
                if name == q:
                    return o
        if strict:
            return None
        return objs[0] if objs else None

    @staticmethod
    def _trust_level_from_tags(tags) -> str:
        """从 User.tags 推断 VRChat 信誉级别（trustLevel）。

        VRChat API 的 User 对象没有直接的 trustLevel 字段，需从 tags 推断。
        注意：信誉标签比实际等级低一级（legacy 命名）。
        - system_trust_veteran -> 可信玩家（紫色 Trusted User）
        - system_trust_trusted -> 知名玩家（橙色 Known User）
        - system_trust_known   -> 玩家（绿色 User）
        - system_trust_basic   -> 新玩家（蓝色 New User）
        - 无任何 trust 标签    -> 游客（灰色 Visitor）
        - system_troll         -> 劣迹玩家
        """
        if not isinstance(tags, list) or not tags:
            return "游客"
        tag_set = set(tags)
        if "system_troll" in tag_set:
            return "劣迹玩家"
        if "system_trust_veteran" in tag_set:
            return "可信玩家"
        if "system_trust_trusted" in tag_set:
            return "知名玩家"
        if "system_trust_known" in tag_set:
            return "玩家"
        if "system_trust_basic" in tag_set:
            return "新玩家"
        return "游客"

    async def _get_avatar_name(self, avatar_id: str) -> str:
        now = time.monotonic()
        if avatar_id in self._avatar_cache and now - self._avatar_cache[avatar_id][1] < CACHE_TTL:
            return self._avatar_cache[avatar_id][0]
        name = ""
        try:
            status, data = await self._request("GET", f"/avatars/{avatar_id}")
            if status == 200 and isinstance(data, dict):
                name = data.get("name") or ""
        except Exception:
            pass
        self._avatar_cache[avatar_id] = (name, now)
        return name

    async def _get_world_name(self, world_id: str) -> str:
        now = time.monotonic()
        if world_id in self._world_cache and now - self._world_cache[world_id][1] < CACHE_TTL:
            return self._world_cache[world_id][0]
        name = ""
        try:
            status, data = await self._request("GET", f"/worlds/{world_id}")
            if status == 200 and isinstance(data, dict):
                name = data.get("name") or ""
        except Exception:
            pass
        self._world_cache[world_id] = (name, now)
        return name

    async def _get_group_name(self, group_id: str) -> str:
        now = time.monotonic()
        if group_id in self._group_cache and now - self._group_cache[group_id][1] < CACHE_TTL:
            return self._group_cache[group_id][0]
        name = ""
        try:
            status, data = await self._request("GET", f"/groups/{group_id}")
            if status == 200 and isinstance(data, dict):
                name = data.get("name") or ""
        except Exception:
            pass
        self._group_cache[group_id] = (name, now)
        return name
