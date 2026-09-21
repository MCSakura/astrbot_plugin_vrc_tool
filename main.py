"""AstrBot 插件：VRChat 工具。

功能：
1. /vrc登录 <邮箱> <密码>  登录 VRChat（含两步验证处理，会话持久化不掉线）
2. /vrc验证 <验证码>       提交邮箱/TOTP 验证码
3. /vrc玩家 <昵称或ID>     查询玩家信息（头像/昵称/ID/状态/信誉/简介/创建日期/
                          模型/展示群组/位置+房间ID，离线不显示位置）
4. /vrc地图 <昵称或ID>     查询地图信息（图标/名字/ID/上传者）
5. /vrc昵称同步 [开|关]    开关：审核通过后把入群答案同步为玩家群的群昵称
6. /vrc添加 玩家群:管理群1,管理群2  动态添加玩家群→管理群映射（立即生效、持久化）
7. 入群审核：玩家群新成员回答问题后，精准匹配查询其 VRChat 信息并发送到
   指定管理群；管理群成员引用审核消息回复「同意/拒绝」完成进群审核。
   一个玩家群可配置对应多个管理群（WebUI 配置或 /vrc添加 指令）。
"""

import json
import os
import time

from astrbot.api import AstrBotConfig, logger
from astrbot.api.event import AstrMessageEvent, filter
from astrbot.api.star import Context, Star, StarTools, register

try:
    from .vrc_api import VRC2FARequired, VRCApi, VRCAuthFailed, VRCLoginRequired
except ImportError:  # 兼容以独立文件方式加载的旧版 AstrBot
    from vrc_api import VRC2FARequired, VRCApi, VRCAuthFailed, VRCLoginRequired

try:
    from astrbot.core.platform.sources.aiocqhttp.aiocqhttp_message_event import (
        AiocqhttpMessageEvent,
    )
except Exception:  # 未启用 aiocqhttp 适配器时兜底
    AiocqhttpMessageEvent = None

PLUGIN_NAME = "astrbot_plugin_vrc_tool"
# 调用 VRChat API 的 User-Agent 联系信息（VRChat 要求真实联系方式，此处固定使用）
VRC_API_CONTACT = "support@baidu.com"
CMD_LOGIN = "vrc登录"
CMD_VERIFY = "vrc验证"
CMD_USER = "vrc玩家"
CMD_WORLD = "vrc地图"
CMD_STATUS = "vrc状态"
CMD_NICK_SYNC = "vrc昵称同步"  # 同步入群答案为群昵称的开关
CMD_ADD_GROUP = "vrc添加"      # 动态添加玩家群:管理群映射


@register(
    PLUGIN_NAME,
    "vrchat_tool",
    "VRChat 玩家/地图信息查询与玩家群入群审核",
    "1.0.3",
    "https://vrchat.community/",
)
class VrcToolPlugin(Star):
    def __init__(self, context: Context, config: AstrBotConfig):
        super().__init__(context)
        self.config = config
        self._data_dir = str(StarTools.get_data_dir(PLUGIN_NAME))
        os.makedirs(self._data_dir, exist_ok=True)
        self._data_path = os.path.join(self._data_dir, "review_data.json")
        self._pending: dict[str, str] = {}  # 审核消息ID -> 申请flag
        self._requests: dict[str, dict] = {}  # 申请flag -> 申请信息（含处理状态）
        # 防御性初始化（实际值由 _load_data 从数据文件恢复）
        self._extra_groups: list[str] = []  # 指令动态添加的玩家群:管理群映射
        self._nick_sync_enabled = False     # 审核通过后是否同步入群答案为群昵称

        self.vrc = VRCApi(
            self._data_dir,
            app_name=str(config.get("app_name", "astrbot-plugin-vrc-tool") or "astrbot-plugin-vrc-tool"),
            app_contact=VRC_API_CONTACT,
            request_interval=float(config.get("request_interval", 1.0) or 1.0),
            fallback_username=str(config.get("vrc_username", "") or ""),
            fallback_password=str(config.get("vrc_password", "") or ""),
        )

    async def initialize(self):
        logger.info("[VRC工具] 插件已加载（v1.0.3）")
        self._load_data()
        try:
            state = await self.vrc.ensure_login()
            if state == "ok":
                logger.info("[VRC工具] 已恢复 VRChat 登录会话")
            elif state == "2fa":
                logger.info("[VRC工具] VRChat 需要邮箱验证码，等待 /vrc验证")
            else:
                logger.info(f"[VRC工具] VRChat 未登录（{state}），可使用 /vrc登录 登录")
        except Exception as e:
            logger.error(f"[VRC工具] 初始化登录失败: {e}")

    async def terminate(self):
        try:
            await self.vrc.close()
        except Exception:
            pass

    # ================================================================== #
    # 指令
    # ================================================================== #
    @filter.permission_type(filter.PermissionType.ADMIN)
    @filter.command(CMD_LOGIN)
    async def vrc_login_cmd(self, event: AstrMessageEvent):
        """登录 VRChat：/vrc登录 <邮箱> <密码>（仅管理员）"""
        extra = self._strip_cmd(event, CMD_LOGIN)
        parts = (extra or "").split(None, 1)
        if len(parts) < 2:
            yield event.plain_result(f"用法：/{CMD_LOGIN} <邮箱> <密码>")
            return
        email, password = parts[0], parts[1]
        yield event.plain_result("正在登录 VRChat...")
        try:
            await self.vrc.login(email, password)
            yield event.plain_result(
                f"✅ 登录成功（账号：{email}）。会话已持久化，不会重复登录掉线。"
            )
        except VRC2FARequired as e:
            if e.method == "email":
                yield event.plain_result(
                    "📧 VRChat 已向你的账号绑定邮箱发送验证码，请查收后使用 "
                    f"/{CMD_VERIFY} <验证码> 完成登录。"
                )
            else:
                yield event.plain_result(
                    "🔐 该账号启用了 TOTP 两步验证，请输入 "
                    f"/{CMD_VERIFY} <验证码> 完成登录。"
                )
        except VRCAuthFailed as e:
            yield event.plain_result(f"❌ {e}")
        except VRCLoginRequired as e:
            yield event.plain_result(f"❌ {e}")
        except Exception as e:
            logger.error(f"登录异常: {e}")
            yield event.plain_result(f"❌ 登录异常：{e}")

    @filter.permission_type(filter.PermissionType.ADMIN)
    @filter.command(CMD_VERIFY)
    async def vrc_verify_cmd(self, event: AstrMessageEvent):
        """提交两步验证码：/vrc验证 <验证码>（仅管理员）"""
        code = self._strip_cmd(event, CMD_VERIFY).strip()
        if not code:
            yield event.plain_result(f"用法：/{CMD_VERIFY} <验证码>")
            return
        try:
            ok = await self.vrc.verify_2fa(code)
        except VRCLoginRequired as e:
            yield event.plain_result(f"❌ {e}")
            return
        except Exception as e:
            logger.error(f"验证异常: {e}")
            yield event.plain_result(f"❌ 验证异常：{e}")
            return
        if ok:
            yield event.plain_result("✅ 验证成功，VRChat 登录完成，会话已保持。")
        else:
            yield event.plain_result(
                "❌ 验证码错误或已过期。\n"
                "提示：VRChat 邮箱验证码有效期很短，且每次执行 /vrc登录 都会发送一封新的验证码邮件"
                "（旧验证码随之失效）。请重新执行 /vrc登录 <邮箱> <密码>，然后使用邮箱里【最新一封】"
                "邮件中的验证码，并尽快执行 /vrc验证 <验证码>。"
            )

    @filter.command(CMD_STATUS)
    async def vrc_status_cmd(self, event: AstrMessageEvent):
        """查看登录状态：/vrc状态"""
        state = await self.vrc.ensure_login()
        if state == "ok":
            yield event.plain_result("✅ 已登录 VRChat，会话有效。")
        elif state == "2fa":
            yield event.plain_result(
                f"⏳ 等待两步验证：请使用 /{CMD_VERIFY} <验证码> 完成登录。"
            )
        else:
            yield event.plain_result(
                f"❌ 未登录：请使用 /{CMD_LOGIN} <邮箱> <密码> 登录。"
            )

    @filter.command(CMD_USER)
    async def vrc_user_cmd(self, event: AstrMessageEvent):
        """查询玩家信息：/vrc玩家 <玩家昵称或玩家ID>"""
        query = self._strip_cmd(event, CMD_USER).strip()
        if not query:
            yield event.plain_result(f"用法：/{CMD_USER} <玩家昵称或玩家ID>")
            return
        state = await self.vrc.ensure_login()
        if state != "ok":
            yield event.plain_result(self._login_prompt(state))
            return
        try:
            info = await self.vrc.query_user(query)
        except VRCLoginRequired as e:
            yield event.plain_result(f"❌ {e}")
            return
        except Exception as e:
            logger.error(f"查询玩家失败: {e}")
            yield event.plain_result(f"❌ 查询失败：{e}")
            return
        # 有头像就先发图（iconUrl 为空则跳过）
        if info.get("iconUrl"):
            yield event.image_result(info["iconUrl"])
        yield event.plain_result(self._format_user_info(info))

    @filter.command(CMD_WORLD)
    async def vrc_world_cmd(self, event: AstrMessageEvent):
        """查询地图信息：/vrc地图 <地图昵称或地图ID>"""
        query = self._strip_cmd(event, CMD_WORLD).strip()
        if not query:
            yield event.plain_result(f"用法：/{CMD_WORLD} <地图昵称或地图ID>")
            return
        state = await self.vrc.ensure_login()
        if state != "ok":
            yield event.plain_result(self._login_prompt(state))
            return
        try:
            info = await self.vrc.query_world(query)
        except VRCLoginRequired as e:
            yield event.plain_result(f"❌ {e}")
            return
        except Exception as e:
            logger.error(f"查询地图失败: {e}")
            yield event.plain_result(f"❌ 查询失败：{e}")
            return
        if info.get("thumbnailImageUrl"):
            yield event.image_result(info["thumbnailImageUrl"])
        yield event.plain_result(
            "━━━ VRChat 地图 ━━━\n"
            f"▸ 名字　　：{info.get('name', '')}\n"
            f"▸ 地图ID　：{info.get('id', '')}\n"
            f"▸ 上传者　：{info.get('authorName', '')}"
        )

    @filter.command(CMD_NICK_SYNC)
    async def vrc_nick_sync_cmd(self, event: AstrMessageEvent):
        """切换"审核通过后把入群答案同步为群昵称"的开关：/vrc昵称同步 [开|关]

        仅在管理群内触发；不带参数时查询当前状态；带"开"开启，带"关"关闭。
        """
        # 仅允许在配置的管理群内触发
        gid = str(event.get_group_id() or "")
        if not gid or gid not in self._all_admin_groups():
            yield event.plain_result("该指令仅在管理群内可用。")
            return
        arg = self._strip_cmd(event, CMD_NICK_SYNC).strip().lower()
        if arg in ("开", "on", "1", "true", "enable"):
            self._nick_sync_enabled = True
            self._save_data()
            yield event.plain_result(
                "✅ 已开启「入群答案同步为群昵称」。\n"
                "管理员同意进群后，会把玩家填写的入群答案设为其群昵称。"
            )
            return
        if arg in ("关", "off", "0", "false", "disable"):
            self._nick_sync_enabled = False
            self._save_data()
            yield event.plain_result("✅ 已关闭「入群答案同步为群昵称」。")
            return
        if arg:
            yield event.plain_result(
                f"用法：/{CMD_NICK_SYNC} [开|关]\n当前状态："
                f"{'开启' if self._nick_sync_enabled else '关闭'}"
            )
            return
        # 不带参数：仅查询
        yield event.plain_result(
            "「入群答案同步为群昵称」当前状态："
            f"{'开启 ✅' if self._nick_sync_enabled else '关闭 ❌'}\n"
            f"使用 /{CMD_NICK_SYNC} 开 启用，/{CMD_NICK_SYNC} 关 停用。"
        )

    @filter.permission_type(filter.PermissionType.ADMIN)
    @filter.command(CMD_ADD_GROUP)
    async def vrc_add_group_cmd(self, event: AstrMessageEvent):
        """动态添加玩家群:管理群映射：/vrc添加 玩家群号:管理群号1,管理群号2（仅管理员）

        可多次调用累加；玩家群已存在则合并管理群。立即生效并持久化。
        """
        arg = self._strip_cmd(event, CMD_ADD_GROUP).strip()
        if not arg:
            yield event.plain_result(
                f"用法：/{CMD_ADD_GROUP} 玩家群号:管理群号1,管理群号2\n"
                "示例：/vrc添加 123456:111,222,333"
            )
            return
        # 校验格式：必须含冒号，玩家群为数字，管理群为逗号分隔的数字
        if ":" not in arg:
            yield event.plain_result("❌ 格式错误，需用冒号分隔，例如：玩家群号:管理群号1,管理群号2")
            return
        pg, ag = arg.split(":", 1)
        pg = pg.strip()
        admins = [a.strip() for a in ag.split(",") if a.strip()]
        if not pg or not admins:
            yield event.plain_result("❌ 玩家群号或管理群号为空。")
            return
        item = f"{pg}:{','.join(admins)}"
        # 合并到 _extra_groups（避免重复）
        merged = False
        for i, exist in enumerate(self._extra_groups):
            if not exist:
                continue
            exist = str(exist).strip()
            if not exist or ":" not in exist:
                continue
            e_pg, _ = exist.split(":", 1)
            if e_pg.strip() == pg:
                # 合并管理群
                e_admins = [a.strip() for a in exist.split(":", 1)[1].split(",") if a.strip()]
                for a in admins:
                    if a not in e_admins:
                        e_admins.append(a)
                self._extra_groups[i] = f"{pg}:{','.join(e_admins)}"
                merged = True
                break
        if not merged:
            self._extra_groups.append(item)
        self._save_data()
        # 输出当前该玩家群对应的所有管理群
        mapping = self._get_review_mapping()
        cur_admins = mapping.get(pg, [])
        yield event.plain_result(
            f"✅ 已添加玩家群 {pg} 的管理群映射。\n"
            f"当前管理群：{','.join(cur_admins) if cur_admins else '（无）'}\n"
            f"（立即生效，已持久化到 {self._data_path}）"
        )

    # ================================================================== #
    # 入群审核
    # ================================================================== #
    @filter.event_message_type(filter.EventMessageType.ALL, priority=1)
    async def on_group_request(self, event):
        """监听 QQ 群加群请求，将新成员回答的答案拿去查询 VRChat 玩家信息并转发管理群审核。"""
        if AiocqhttpMessageEvent is None or not isinstance(event, AiocqhttpMessageEvent):
            return
        raw = self._get_raw(event)
        if not isinstance(raw, dict):
            return
        if raw.get("post_type") != "request":
            return
        if raw.get("request_type") != "group":
            return

        sub_type = str(raw.get("sub_type") or "add")
        group_id = str(raw.get("group_id") or "")
        user_id = str(raw.get("user_id") or "")
        flag = str(raw.get("flag") or "")
        comment = str(raw.get("comment") or "")
        # 收到加群请求就先记录，便于排查"收不到/答案为空"等问题
        logger.info(
            f"[入群审核] 收到群加群请求: group={group_id} user={user_id} "
            f"sub_type={sub_type} comment={comment!r}"
        )

        if not self.config.get("join_review_enabled", False):
            logger.info("[入群审核] 未启用（join_review_enabled=false），忽略该请求")
            return
        if sub_type not in ("add", "invite"):
            return
        if not group_id or not flag:
            logger.warning(f"[入群审核] 缺少 group_id/flag，无法处理: raw={raw}")
            return

        mapping = self._get_review_mapping()
        admin_groups = mapping.get(group_id)
        if not admin_groups:
            logger.info(f"[入群审核] 群 {group_id} 未配置管理群映射，忽略该请求")
            return

        question = str(self.config.get("join_question", "") or "").strip()
        answer = self._extract_answer(comment, question)
        logger.info(
            f"[入群审核] 群 {group_id} 命中映射，管理群={admin_groups}，"
            f"解析答案={answer!r}"
        )

        # 拉取群名与申请人昵称（用于展示，失败则仅显示ID）
        group_name = await self._safe_get_group_name(event.bot, group_id)
        applicant_name = await self._safe_get_stranger_name(event.bot, user_id)

        # 查询 VRChat 玩家信息（精准匹配：入群审核必须严格匹配昵称，
        # 查不到时玩家ID 显示为"无法精准匹配查询到此人，请管理员手动审核"，
        # 不阻塞人工审核流程）
        vrc_text = ""
        vrc_icon = ""
        try:
            login_state = await self.vrc.ensure_login()
            if login_state == "ok":
                info = await self.vrc.query_user(answer or user_id, strict_match=True)
                vrc_icon = info.get("iconUrl", "") or ""
                vrc_text = self._format_user_info(info)
            elif login_state == "2fa":
                vrc_text = (
                    "⚠️ VRChat 需要邮箱验证码：请在管理群执行 /vrc登录 后 "
                    "再 /vrc验证 <验证码> 完成登录。"
                )
            else:
                vrc_text = "⚠️ VRChat 未登录或登录失败，请在管理群执行 /vrc登录 <邮箱> <密码>"
        except Exception as e:
            vrc_text = f"⚠️ VRChat 信息查询失败：{e}"

        lines = ["━━━ 入群审核申请 ━━━"]
        lines.append(f"▸ 玩家群　：{self._id_with_name(group_id, group_name)}")
        lines.append(f"▸ 申请人　：{self._id_with_name(user_id, applicant_name)}")
        if question:
            lines.append(f"▸ 入群问题：{question}")
        lines.append(f"▸ 申请答案：{answer or '（空）'}")
        lines.append("──── VRChat 信息 ────")
        # 有头像时在 VRChat 信息前嵌入 CQ 图片
        if vrc_icon:
            lines.append(f"[CQ:image,file={vrc_icon}]")
        lines.append(vrc_text)
        lines.append("────────────────────")
        lines.append("请引用本消息回复「同意」或「拒绝」进行审核。")
        msg = "\n".join(lines)

        req = {
            "flag": flag,
            "sub_type": sub_type,
            "group_id": group_id,
            "user_id": user_id,
            "comment": comment,
            "answer": answer,
            "ts": int(time.time()),
        }
        self._requests[flag] = req

        for ag in admin_groups:
            try:
                res = await event.bot.send_group_msg(group_id=int(ag), message=msg)
                mid = self._extract_message_id(res)
                if mid:
                    self._pending[mid] = flag
                    self._save_data()
                    logger.info(
                        f"[入群审核] 已向管理群 {ag} 发送 {user_id} 的审核消息 {mid}"
                    )
            except Exception as e:
                logger.error(f"[入群审核] 发送审核消息到管理群 {ag} 失败: {e}")

    @filter.event_message_type(filter.EventMessageType.GROUP_MESSAGE)
    async def on_review_reply(self, event):
        """管理群成员引用审核消息回复「同意/拒绝」时，执行进群审核操作。"""
        if AiocqhttpMessageEvent is None or not isinstance(event, AiocqhttpMessageEvent):
            return
        group_id = str(event.get_group_id() or "")
        if not group_id or group_id not in self._all_admin_groups():
            return
        reply_id = self._get_reply_id(event)
        if not reply_id:
            return
        flag = self._pending.get(reply_id)
        if not flag:
            return
        req = self._requests.get(flag)
        if not req:
            return

        text = (event.message_str or "").strip()
        decision = None
        if text.startswith("同意"):
            decision = True
        elif text.startswith("拒绝"):
            decision = False
        if decision is None:
            return

        expire = int(self.config.get("review_expire_seconds", 3600) or 3600)
        if int(time.time()) - int(req.get("ts", 0)) > expire:
            await self._send_group(event.bot, group_id, "该审核消息已过期，无法处理。")
            return
        if req.get("processed"):
            await self._send_group(event.bot, group_id, "该进群申请已被处理过了，无需重复操作。")
            return

        operator = str(event.get_sender_id() or "")
        try:
            await event.bot.set_group_add_request(
                flag=req["flag"],
                sub_type=req.get("sub_type", "add"),
                approve=decision,
                reason="管理群审核通过，欢迎加入" if decision else "管理群审核拒绝",
            )
            req["processed"] = {
                "result": "approved" if decision else "rejected",
                "operator": operator,
                "ts": int(time.time()),
            }
            self._save_data()
            await self._send_group(
                event.bot,
                group_id,
                f"✅ 已{'同意' if decision else '拒绝'} QQ={req.get('user_id')} 的进群申请。",
            )
            logger.info(
                f"[入群审核] 管理群 {group_id} 由 {operator} "
                f"{'同意' if decision else '拒绝'}了 {req.get('user_id')} 的申请"
            )
            # 同意进群后，若开启昵称同步，把入群答案设为玩家群的群昵称
            if decision and getattr(self, "_nick_sync_enabled", False):
                applicant_qq = str(req.get("user_id", "") or "")
                applicant_group = str(req.get("group_id", "") or "")
                nick = str(req.get("answer", "") or "").strip()
                if applicant_qq and applicant_group and nick:
                    await self._safe_set_group_card(
                        event.bot, applicant_group, applicant_qq, nick
                    )
                    await self._send_group(
                        event.bot,
                        group_id,
                        f"✏️ 已将玩家群 {applicant_group} 中 {applicant_qq} 的群昵称"
                        f"同步为入群答案：{nick}",
                    )
        except Exception as e:
            logger.error(f"[入群审核] 处理失败: {e}")
            await self._send_group(
                event.bot,
                group_id,
                f"❌ 处理失败：{e}\n（可能是申请已过期或已被处理）",
            )

    # ================================================================== #
    # 工具方法
    # ================================================================== #
    def _strip_cmd(self, event: AstrMessageEvent, cmd: str) -> str:
        """从 event.message_str 中截取指令 cmd 之后的全部参数文本。

        AstrBot 的指令参数按空格逐个分配，若用 `extra: str` 接收多参数
        只会拿到第一个 token；这里直接从完整消息解析，支持昵称/密码含空格。
        event.message_str 在唤醒阶段已剥离 `/` 前缀，此处再兜底兼容旧版本。
        """
        text = (event.message_str or "").strip()
        if text == cmd or text == "/" + cmd:
            return ""
        if text.startswith(cmd):
            return text[len(cmd):].strip()
        if text.startswith("/" + cmd):
            return text[len("/" + cmd):].strip()
        return text

    def _login_prompt(self, state: str) -> str:
        if state == "2fa":
            return (
                "⚠️ VRChat 需要邮箱验证码：请先使用 "
                f"/{CMD_LOGIN} <邮箱> <密码>，再用 /{CMD_VERIFY} <验证码> 完成登录。"
            )
        if state == "no_creds":
            return f"⚠️ 尚未登录 VRChat：请先使用 /{CMD_LOGIN} <邮箱> <密码> 登录。"
        return f"⚠️ VRChat 登录失败，请检查账号信息后使用 /{CMD_LOGIN} 重新登录。"

    def _format_user_info(self, info: dict) -> str:
        """格式化玩家信息（/vrc玩家 与入群审核共用）。

        - 头像通过 iconUrl 输出（有就输出，没有就不输出，由调用方发图）
        - 用 state 和 status 输出状态：offline=⚫；online 时
          join me=🟢 / active=🔵 / ask me=🟡 / busy=🔴
        - status 后跟 statusDescription（玩家自定义状态）
        - bio 输出简介，date_joined 输出账号创建日期
        - 在线才显示位置与房间ID
        - _not_found（精准匹配失败）只输出昵称与玩家ID提示
        """
        lines = ["━━━ VRChat 玩家 ━━━"]
        lines.append(f"▸ 昵称　　：{info.get('displayName', '')}")
        lines.append(f"▸ 玩家ID　：{info.get('id', '')}")

        # 精准匹配失败：只输出昵称与玩家ID提示，其余字段不显示
        if info.get("_not_found"):
            return "\n".join(lines)

        # 状态
        state_emoji = self._state_emoji(
            info.get("state", ""), info.get("status", "")
        )
        if state_emoji:
            desc = info.get("statusDescription", "")
            lines.append(
                f"▸ 状态　　：{state_emoji} {desc}" if desc else f"▸ 状态　　：{state_emoji}"
            )

        # 信誉状态（trustLevel，放在状态行下一行；劣迹/管理员等作为附加标注）
        if info.get("trustLevel"):
            flags = "、".join(info.get("trustFlags") or [])
            lines.append(
                f"▸ 信誉　　：{info['trustLevel']}（{flags}）"
                if flags
                else f"▸ 信誉　　：{info['trustLevel']}"
            )

        # 简介
        if info.get("bio"):
            lines.append(f"▸ 简介　　：{info['bio']}")
        # 账号创建日期
        if info.get("date_joined"):
            lines.append(f"▸ 账号创建：{info['date_joined']}")
        # 正在使用的模型
        if info.get("avatar"):
            lines.append(f"▸ 正在使用的模型：{info['avatar']}")
        # 正在展示的群组
        if info.get("group"):
            lines.append(f"▸ 正在展示的群组：{info['group']}（{info.get('group_id', '')}）")
        # 当前位置 + 房间ID（不在线则不展示）
        if info.get("world"):
            lines.append(f"▸ 当前位置：{info['world']}（{info.get('world_id', '')}）")
            lines.append(f"▸ 房间ID　：{info.get('instance_id', '')}")
        else:
            lines.append("（该玩家当前离线或未公开位置，不显示位置与房间ID）")
        return "\n".join(lines)

    @staticmethod
    def _state_emoji(state: str, status: str) -> str:
        """根据 state 和 status 返回状态 emoji。

        - state=offline -> ⚫
        - state=online 时按 status 输出：join me=🟢 / active=🔵 / ask me=🟡 / busy=🔴
        - 其余情况返回空串（不输出状态行）
        """
        if state == "offline":
            return "⚫"
        if state == "online":
            mapping = {
                "join me": "🟢",
                "active": "🔵",
                "ask me": "🟡",
                "busy": "🔴",
            }
            return mapping.get(status, "")
        return ""

    def _get_review_mapping(self) -> dict:
        """解析玩家群号 -> 管理群号列表的映射。

        来源合并：WebUI 配置 review_groups + 指令动态添加的 _extra_groups。
        条目格式：玩家群:管理群1,管理群2（无冒号时视为仅玩家群，无管理群）
        """
        mapping: dict[str, list] = {}

        def _merge(item: str):
            item = str(item).strip()
            if not item:
                return
            if ":" in item:
                pg, ag = item.split(":", 1)
                pg = pg.strip()
                admins = [a.strip() for a in ag.split(",") if a.strip()]
                mapping.setdefault(pg, [])
                for a in admins:
                    if a not in mapping[pg]:
                        mapping[pg].append(a)
            else:
                mapping.setdefault(item, [])

        # 配置项
        items = self.config.get("review_groups", []) or []
        for item in items:
            _merge(item)
        # 指令动态添加
        for item in getattr(self, "_extra_groups", []) or []:
            _merge(item)
        return mapping

    def _all_admin_groups(self) -> set:
        s = set()
        for admins in self._get_review_mapping().values():
            s.update(admins)
        return s

    @staticmethod
    def _id_with_name(uid: str, name: str) -> str:
        """ID 后带名称：123456（群名/昵称）。名称为空时只显示 ID。"""
        name = (name or "").strip()
        return f"{uid}（{name}）" if name else uid

    @staticmethod
    async def _safe_get_group_name(bot, group_id: str) -> str:
        """通过 OneBot get_group_info 获取群名。"""
        try:
            data = await bot.get_group_info(group_id=int(group_id))
            if isinstance(data, dict) and isinstance(data.get("data"), dict):
                data = data["data"]
            return str((data or {}).get("group_name", "") or "")
        except Exception as e:
            logger.error(f"[入群审核] 获取群信息失败 {group_id}: {e}")
            return ""

    @staticmethod
    async def _safe_get_stranger_name(bot, user_id: str) -> str:
        """通过 OneBot get_stranger_info 获取申请人昵称（申请人尚未入群）。"""
        try:
            data = await bot.get_stranger_info(user_id=int(user_id))
            if isinstance(data, dict) and isinstance(data.get("data"), dict):
                data = data["data"]
            name = (data or {}).get("nickname", "") or (data or {}).get("nick", "") or ""
            return str(name)
        except Exception as e:
            logger.error(f"[入群审核] 获取申请人昵称失败 {user_id}: {e}")
            return ""

    @staticmethod
    async def _safe_set_group_card(bot, group_id: str, user_id: str, card: str):
        """通过 OneBot set_group_card 设置群名片（群昵称）。失败仅记录日志。"""
        try:
            await bot.set_group_card(
                group_id=int(group_id), user_id=int(user_id), card=card
            )
            logger.info(
                f"[入群审核] 已设置群 {group_id} 中 {user_id} 的群昵称为：{card}"
            )
        except Exception as e:
            logger.error(
                f"[入群审核] 设置群昵称失败 group={group_id} user={user_id}: {e}"
            )

    @staticmethod
    def _extract_answer(comment: str, question: str = "") -> str:
        """从加群请求 comment 中提取问题答案。

        不同 OneBot 实现（NapCat/Lagrange/go-cqhttp 等）对"问题+管理员审核"
        加群请求的 comment 格式不一，常见有：
          - 答案：xxx
          - 问题：xxx 答案：yyy
          - 直接是答案 xxx
          - 以群设置的完整问题开头，后跟答案
        这里尽量兼容处理。
        """
        text = (comment or "").strip()
        if not text:
            return ""
        # comment 以配置的问题原文开头时，先去掉问题部分
        q = (question or "").strip()
        if q and text.startswith(q):
            text = text[len(q):].strip()
        # 常见答案标记
        for marker in ("答案：", "答案:", "验证答案：", "回答：", "回答:"):
            if marker in text:
                text = text.split(marker, 1)[1]
                break
        if not text.strip():
            return ""
        # 仅保留第一行（部分实现是多行文本）
        text = text.splitlines()[0].strip()
        # 去掉包裹的引号与收尾标点
        return text.strip(" \t\r\n\"'“”‘’。，,；;：:　")

    def _get_raw(self, event) -> dict:
        raw = getattr(event.message_obj, "raw_message", None)
        if isinstance(raw, dict):
            return raw
        if raw is not None:
            if hasattr(raw, "model_dump"):
                try:
                    return raw.model_dump()
                except Exception:
                    pass
            return raw.__dict__
        return {}

    def _get_reply_id(self, event) -> str:
        """从消息中提取被引用的消息ID。"""
        for comp in getattr(event.message_obj, "message", None) or []:
            name = type(comp).__name__.lower()
            if "reply" in name or "quote" in name:
                rid = getattr(comp, "id", None)
                if rid:
                    return str(rid)
        raw = self._get_raw(event)
        msg = raw.get("message")
        if isinstance(msg, list):
            for seg in msg:
                if isinstance(seg, dict) and seg.get("type") in ("reply", "reply_to", "quote"):
                    data = seg.get("data") or {}
                    rid = data.get("id")
                    if rid:
                        return str(rid)
        return None

    @staticmethod
    def _extract_message_id(res) -> str:
        if isinstance(res, dict):
            mid = res.get("message_id")
            if mid is not None:
                return str(mid)
            data = res.get("data") or {}
            mid = data.get("message_id")
            if mid is not None:
                return str(mid)
        return str(res) if res else ""

    @staticmethod
    async def _send_group(bot, group_id: str, text: str):
        try:
            await bot.send_group_msg(group_id=int(group_id), message=text)
        except Exception as e:
            logger.error(f"[入群审核] 发送消息到群 {group_id} 失败: {e}")

    # ------------------------------------------------------------------ #
    # 审核数据持久化
    # ------------------------------------------------------------------ #
    def _load_data(self):
        self._pending = {}
        self._requests = {}
        # 通过指令动态添加的玩家群:管理群映射条目（列表，元素如 "玩家群:管理1,管理2"）
        self._extra_groups: list[str] = []
        # 是否在审核通过后把入群答案同步为群昵称
        self._nick_sync_enabled = False
        try:
            if os.path.exists(self._data_path):
                with open(self._data_path, encoding="utf-8") as f:
                    data = json.load(f)
                if isinstance(data, dict):
                    self._pending = data.get("pending") or {}
                    self._requests = data.get("requests") or {}
                    self._extra_groups = list(data.get("extra_groups") or [])
                    self._nick_sync_enabled = bool(data.get("nick_sync_enabled", False))
                expire = int(self.config.get("review_expire_seconds", 3600) or 3600)
                now = int(time.time())
                for mid in list(self._pending):
                    flag = self._pending[mid]
                    req = self._requests.get(flag)
                    ts = int(req.get("ts", 0)) if isinstance(req, dict) else 0
                    if now - ts > expire:
                        del self._pending[mid]
                for flag in list(self._requests):
                    req = self._requests[flag]
                    ts = int(req.get("ts", 0)) if isinstance(req, dict) else 0
                    if now - ts > expire:
                        del self._requests[flag]
        except Exception as e:
            logger.error(f"[入群审核] 读取审核数据失败: {e}")

    def _save_data(self):
        try:
            with open(self._data_path, "w", encoding="utf-8") as f:
                json.dump(
                    {
                        "pending": self._pending,
                        "requests": self._requests,
                        "extra_groups": self._extra_groups,
                        "nick_sync_enabled": self._nick_sync_enabled,
                    },
                    f,
                    ensure_ascii=False,
                    indent=2,
                )
        except Exception as e:
            logger.error(f"[入群审核] 保存审核数据失败: {e}")
