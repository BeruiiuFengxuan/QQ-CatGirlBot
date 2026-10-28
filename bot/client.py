import asyncio
import os
import re
import time

import botpy
from botpy import logging
from botpy.api import BotAPI
from botpy.http import Route

from . import prompts
from .join_review import GroupJoinRequestEvent, GroupMemberEvent, JoinReview
from .knowledge import KnowledgeBase
from .llm import call_llm, generate_reply
from .moderation import Moderation

import botpy.connection as _conn


def _parse_group_message_create(self, payload):
    _message = _conn.GroupMessage(self.api, payload.get("id", None), payload.get("d", {}))
    self._dispatch("group_message_create", _message)


_conn.ConnectionState.parse_group_message_create = _parse_group_message_create


# qq-botpy 1.2.x 的 GroupMessage._User 只保留了 member_openid，
# 这里把事件里的昵称、群角色等字段补上，方便日志排查和判断能否撤回。
def _patch_group_message_user():
    group_message = getattr(_conn, "GroupMessage", None)
    user_cls = getattr(group_message, "_User", None)
    if user_cls is None or getattr(user_cls, "_qqcatgirl_patched", False):
        return

    original_init = user_cls.__init__

    def _init(self, data):
        original_init(self, data)
        if not isinstance(data, dict):
            return
        for key in (
            "id",
            "username",
            "bot",
            "is_you",
            "member_role",
            "union_openid",
            "user_openid",
        ):
            if hasattr(self, key):
                continue
            try:
                setattr(self, key, data.get(key))
            except AttributeError:
                pass

    user_cls.__init__ = _init
    user_cls._qqcatgirl_patched = True


_patch_group_message_user()


# 当前 qq-botpy 版本还没有封装群成员加入 / 入群申请事件，这里补齐。
def _parse_group_member_add(self, payload):
    _event = GroupMemberEvent(self.api, payload.get("id", None), payload.get("d", {}))
    self._dispatch("group_member_add", _event)


def _parse_group_member_remove(self, payload):
    _event = GroupMemberEvent(self.api, payload.get("id", None), payload.get("d", {}))
    self._dispatch("group_member_remove", _event)


def _parse_group_join_request(self, payload):
    _event = GroupJoinRequestEvent(self.api, payload.get("id", None), payload.get("d", {}))
    self._dispatch("group_join_request", _event)


# 只在当前 SDK 还没有对应解析器时补丁，避免覆盖新版本 SDK 自己的事件对象。
for _event_name, _parser in (
    ("parse_group_member_add", _parse_group_member_add),
    ("parse_group_member_remove", _parse_group_member_remove),
    ("parse_group_join_request", _parse_group_join_request),
):
    if not hasattr(_conn.ConnectionState, _event_name):
        setattr(_conn.ConnectionState, _event_name, _parser)


# 当前 botpy 版本还没有封装「撤回群消息」，这里补一个。
# 机器人是群管理员时可以撤回普通群成员的消息，否则只能撤回自己的消息。
if not hasattr(BotAPI, "recall_group_message"):
    async def _recall_group_message(self, group_openid, message_id):
        route = Route(
            "DELETE",
            "/v2/groups/{group_openid}/messages/{message_id}",
            group_openid=group_openid,
            message_id=message_id,
        )
        return await self._http.request(route)

    BotAPI.recall_group_message = _recall_group_message


# 入群申请审批：机器人需为群管理员。
if not hasattr(BotAPI, "approval_group_join_request"):
    async def _approval_group_join_request(
        self,
        group_openid,
        member_openid,
        op,
        join_request_id=None,
        reject_reason=None,
        add_to_member_blacklist=False,
    ):
        route = Route(
            "POST",
            "/v2/groups/{group_openid}/approval_join_request/{member_openid}",
            group_openid=group_openid,
            member_openid=member_openid,
        )
        payload = {"op": op}
        if join_request_id:
            payload["join_request_id"] = join_request_id
        if op == "decline":
            if reject_reason:
                payload["reject_reason"] = reject_reason
            if add_to_member_blacklist:
                payload["add_to_member_blacklist"] = True
        return await self._http.request(route, json=payload)

    BotAPI.approval_group_join_request = _approval_group_join_request

_log = logging.get_logger()

MENTION_RE = re.compile(r"<@!?[0-9a-zA-Z_]+>")
MAX_CHUNK = 1900
MEMORY_TTL = 12 * 3600


def split_text(text):
    text = text.strip()
    if not text:
        return [""]
    if len(text) <= MAX_CHUNK:
        return [text]
    chunks = []
    for i in range(0, len(text), MAX_CHUNK):
        chunks.append(text[i:i + MAX_CHUNK])
    return chunks


class CatBot(botpy.Client):
    def __init__(self, config, kb, base_dir=None, **kwargs):
        super().__init__(**kwargs)
        self.config = config
        self.kb = kb
        bot_cfg = config.get("bot", {})
        self.memory_hours = bot_cfg.get("memory_hours", 12)
        self.review_enabled = bot_cfg.get("review_enabled", True)
        self.max_mem_turns = bot_cfg.get("max_memory_turns", 20)
        self.memory = {}
        self.moderation = Moderation(config, base_dir or os.getcwd())
        self.join_review = JoinReview(
            config, base_dir or os.getcwd(), moderation=self.moderation
        )
        # 同一条消息可能同时触发「群 @ 消息」和「群全量消息」两个事件，避免重复撤回/警告
        self._moderated_message_ids = {}
        # 全量消息模式下 @消息可能只走 GROUP_MESSAGE_CREATE，避免两个事件重复回复
        self._chat_message_ids = {}
        self._bot_member_openid = ""
        # 第一次收到全量群消息时记一条日志，方便确认群主已开启「接收全量消息」
        self._full_message_seen = False
        # 大模型审核被限流后短暂跳过，避免每条回复都重复触发 429
        self._review_cooldown_until = 0.0
        # 群警告 markdown（<qqbot-at-user>）发送失败后暂时退回纯文本
        self._warning_markdown_disabled_until = 0.0

    async def on_ready(self):
        _log.info("robot %s on_ready!", getattr(self.robot, "name", ""))
        try:
            asyncio.create_task(self._memory_sweeper())
        except Exception as exc:  # noqa: BLE001
            _log.error("sweeper start error: %s", exc)

    async def on_group_at_message_create(self, message):
        if await self.moderate(message):
            return
        if self._chat_already_handled(message):
            return
        await self.handle(message)

    async def on_at_message_create(self, message):
        await self.handle(message)

    async def on_c2c_message_create(self, message):
        await self.handle(message)

    async def on_group_message_create(self, message):
        if not self._full_message_seen:
            self._full_message_seen = True
            _log.info(
                "group full-message mode active: group=%s",
                getattr(message, "group_openid", ""),
            )

        author = getattr(message, "author", None)
        if author is not None and getattr(author, "bot", False):
            return

        if await self.moderate(message):
            return

        # 开启全量消息后，QQ 可能把 @机器人 的消息也放进
        # GROUP_MESSAGE_CREATE，而不是 GROUP_AT_MESSAGE_CREATE。
        # 所以不能只在 reply_all_messages=true 时处理。
        mentioned = self._is_bot_mentioned(message)
        if not mentioned and not self.config.get("bot", {}).get(
            "reply_all_messages"
        ):
            return
        if self._chat_already_handled(message):
            return
        # 非 @ 的全量消息不是对机器人的定向提问，模型失败时保持安静；
        # @消息则允许发送兜底提示。
        await self.handle(message, allow_fallback=mentioned)

    async def on_group_join_request(self, event):
        """收到入群申请：按配置审核问答，可选自动通过/拒绝。"""
        try:
            await self.join_review.handle(event, self.api)
        except Exception as exc:  # noqa: BLE001
            _log.error("join review handle error: %s", exc)

    async def on_group_member_add(self, event):
        _log.info(
            "group member add: group=%s member=%s",
            getattr(event, "group_openid", ""),
            getattr(event, "member_openid", ""),
        )

    async def on_group_member_remove(self, event):
        _log.info(
            "group member remove: group=%s member=%s",
            getattr(event, "group_openid", ""),
            getattr(event, "member_openid", ""),
        )

    def _conv_key(self, message):
        chat = (
            getattr(message, "group_openid", None)
            or getattr(message, "channel_id", None)
            or getattr(message, "user_openid", None)
            or "global"
        )
        user = ""
        author = getattr(message, "author", None)
        if author is not None:
            user = (
                getattr(author, "member_openid", None)
                or getattr(author, "id", None)
                or getattr(author, "user_openid", None)
                or ""
            )
        return "%s|%s" % (chat, user)

    def _memory_append(self, key, role, text):
        if not text:
            return
        bucket = self.memory.setdefault(key, [])
        bucket.append({"role": role, "text": text, "ts": time.time()})
        limit = self.max_mem_turns * 2
        if len(bucket) > limit:
            self.memory[key] = bucket[-limit:]

    def _memory_context(self, key):
        now = time.time()
        ttl = self.memory_hours * 3600
        lines = []
        for e in self.memory.get(key, []):
            if now - e["ts"] > ttl:
                continue
            who = "对方" if e["role"] == "user" else "果娘酱"
            lines.append("%s：%s" % (who, e["text"]))
        return "\n".join(lines)

    async def _memory_sweeper(self):
        while True:
            await asyncio.sleep(600)
            now = time.time()
            ttl = self.memory_hours * 3600
            for k in list(self.memory.keys()):
                self.memory[k] = [e for e in self.memory[k] if now - e["ts"] <= ttl]
                if not self.memory[k]:
                    del self.memory[k]

    def _clean(self, message):
        return MENTION_RE.sub("", getattr(message, "content", "") or "").strip()

    def _chat_already_handled(self, message):
        """同一 msg_id 可能同时触发 @ 和全量事件，只处理一次。"""
        message_id = getattr(message, "id", None)
        if not message_id:
            return False

        now = time.time()
        for old_id, ts in list(self._chat_message_ids.items()):
            if now - ts > 300:
                del self._chat_message_ids[old_id]
        if message_id in self._chat_message_ids:
            return True
        self._chat_message_ids[message_id] = now
        return False

    def _is_bot_mentioned(self, message):
        """判断全量消息是否 @ 了机器人。

        开启「接收全量消息」后，QQ 会把 @机器人 的消息也放进
        GROUP_MESSAGE_CREATE，实际 payload 里的 mentions 形如：
        {"id": "机器人OpenID", "is_you": true}。
        部分版本没有 is_you，则用机器人 OpenID 缓存 / content 兜底。
        """
        mentions = getattr(message, "mentions", None) or []
        content = getattr(message, "content", "") or ""
        tokens = MENTION_RE.findall(content)

        known_ids = set()
        bot_mentions = []
        has_is_you = False
        for user in mentions:
            user_ids = [
                getattr(user, "member_openid", None),
                getattr(user, "id", None),
                getattr(user, "user_openid", None),
                getattr(user, "union_openid", None),
            ]
            known_ids.update(str(value) for value in user_ids if value)

            is_you = getattr(user, "is_you", None)
            if is_you is not None:
                has_is_you = True
            if is_you is True:
                self._cache_bot_openid(user)
                return True
            if getattr(user, "bot", False):
                bot_mentions.append(user)

        # 没有 is_you 字段的版本：mentions 里唯一的机器人就是自己。
        if not has_is_you and len(bot_mentions) == 1:
            self._cache_bot_openid(bot_mentions[0])
            return True

        # 有些版本不会把机器人自己放进 mentions，用缓存过的 OpenID 兜底。
        if self._bot_member_openid and any(
            self._bot_member_openid in token for token in tokens
        ):
            return True

        # 再兜底：mentions 里没有机器人时，content 里多出来的那个 @ 就是机器人。
        # 只接受唯一一个未知 @，避免把 @全体成员 或漏报的普通成员误判成机器人。
        unknown = []
        for token in tokens:
            inner = token[2:-1].lstrip("!")
            if inner.lower() == "all":
                continue
            if inner not in known_ids and inner not in unknown:
                unknown.append(inner)
        if len(unknown) == 1:
            self._bot_member_openid = unknown[0]
            return True
        return False

    def _cache_bot_openid(self, user):
        bot_id = (
            getattr(user, "member_openid", None)
            or getattr(user, "id", None)
            or getattr(user, "user_openid", None)
        )
        if bot_id:
            self._bot_member_openid = str(bot_id)

    async def moderate(self, message):
        """检测一条群消息；命中规则时撤回、警告并记录，返回 True 表示已处理。"""
        if not self.moderation.enabled:
            return False

        author = getattr(message, "author", None)
        if author is not None and getattr(author, "bot", False):
            return False

        text = self._clean(message)
        if not text:
            return False
        # 只放行机器人自己的指令，避免指令参数里出现关键词时误伤
        if text.lower() in ("/reload", "/help", "/groupid"):
            return False

        group_id = getattr(message, "group_openid", None) or ""
        try:
            match = self.moderation.check(text, group_id=group_id)
        except Exception as exc:  # noqa: BLE001
            _log.error("moderation check error: %s", exc)
            return False
        if match is None:
            return False

        message_id = getattr(message, "id", None)
        if message_id:
            now = time.time()
            for old_id, ts in list(self._moderated_message_ids.items()):
                if now - ts > 300:
                    del self._moderated_message_ids[old_id]
            if message_id in self._moderated_message_ids:
                return True
            self._moderated_message_ids[message_id] = now

        try:
            await self._handle_violation(message, text, match)
        except Exception as exc:  # noqa: BLE001
            _log.error("moderation handle error: %s", exc)
        return True

    async def _handle_violation(self, message, text, match):
        author = getattr(message, "author", None)
        group_id = getattr(message, "group_openid", None) or ""
        channel_id = getattr(message, "channel_id", None) or ""
        member_id = (
            getattr(author, "member_openid", None)
            or getattr(author, "id", None)
            or getattr(author, "user_openid", None)
            or ""
        )
        member_name = (
            getattr(author, "username", None)
            or getattr(author, "nick", None)
            or ""
        )
        member_role = getattr(author, "member_role", None) or ""
        message_id = getattr(message, "id", None) or ""
        scope = "group" if group_id else ("channel" if channel_id else "unknown")
        match_type = "regex" if match.is_regex else "keyword"

        recall_attempted = False
        recalled = False
        recall_error = ""
        recall_skipped_reason = ""
        if self.moderation.recall_enabled and message_id:
            if member_role in ("admin", "owner"):
                # 群主 / 管理员的消息，机器人即使也是管理员也撤不了。
                role_text = "群主" if member_role == "owner" else "管理员"
                recall_skipped_reason = (
                    "发送者是%s，机器人只能撤回普通群成员的消息" % role_text
                )
                _log.info(
                    "moderation recall skipped (%s): %s",
                    match.category,
                    recall_skipped_reason,
                )
            else:
                recall_attempted = True
                try:
                    if group_id:
                        await self.api.recall_group_message(group_id, message_id)
                    elif channel_id:
                        await self.api.recall_message(channel_id, message_id)
                    else:
                        recall_attempted = False
                    recalled = recall_attempted
                except Exception as exc:  # noqa: BLE001
                    recall_error = str(exc)
                    if "40062003" in recall_error:
                        recall_error += (
                            "（请确认机器人已被群主设为群管理员，"
                            "且发送者是普通群成员）"
                        )
                    _log.warning(
                        "moderation recall failed (%s): %s", match.category, exc
                    )

        warning_sent = False
        warning_suppressed = False
        warning_count = 0
        if self.moderation.should_warn(group_id, member_id, match.category):
            warning_count = self.moderation.next_count(group_id, member_id)
            try:
                warning_sent = await self._send_moderation_warning(
                    message,
                    group_id,
                    channel_id,
                    member_id,
                    member_name,
                    match,
                    warning_count,
                )
            except Exception as exc:  # noqa: BLE001
                _log.error("moderation warning send failed: %s", exc)
        else:
            warning_suppressed = True

        self.moderation.write_log(
            group_id=group_id,
            channel_id=channel_id,
            member_id=member_id,
            member_name=member_name,
            member_role=member_role,
            message_id=message_id,
            scope=scope,
            category=match.category,
            keyword=match.keyword,
            match_type=match_type,
            content=text,
            recall_attempted=recall_attempted,
            recalled=recalled,
            recall_error=recall_error,
            recall_skipped_reason=recall_skipped_reason,
            warning_sent=warning_sent,
            warning_suppressed=warning_suppressed,
            warning_count=warning_count,
        )

    @staticmethod
    def _replace_raw_group_mentions(text, member_name):
        """群聊纯文本不会渲染 <@openid>，把残留标签换成可读的 @昵称。"""
        replacement = "@%s" % (member_name or "这位朋友")
        return re.sub(r"<@!?[0-9a-zA-Z_]+>", replacement, text)

    async def _send_moderation_warning(
        self,
        message,
        group_id,
        channel_id,
        member_id,
        member_name,
        match,
        warning_count,
    ):
        """发送违规警告。

        群聊纯文本消息不支持 ``<@openid>``，直接发会显示成一串 OpenID；
        真正的 @ 需要 markdown 消息 + ``<qqbot-at-user id="member_openid" />``。
        如果 markdown 发送失败，再退回可读的 ``@昵称`` 纯文本。
        """
        if (
            group_id
            and member_id
            and self.moderation.mention_member
            and time.time() >= self._warning_markdown_disabled_until
        ):
            mention_tag = '<qqbot-at-user id="%s" />' % member_id
            warning = self.moderation.warning_text(
                match,
                member_id,
                warning_count,
                member_name=member_name,
                mention=mention_tag,
            )
            warning = self._replace_raw_group_mentions(warning, member_name)
            try:
                await self.api.post_group_message(
                    group_openid=group_id,
                    msg_id=getattr(message, "id", None),
                    msg_type=2,
                    markdown={"content": warning},
                )
                return True
            except Exception as exc:  # noqa: BLE001
                # 机器人可能没有 markdown 权限，短时间内直接用纯文本，避免每次违规都失败一次
                self._warning_markdown_disabled_until = time.time() + 300
                _log.warning(
                    "markdown moderation warning failed, fallback to text for 300s: %s",
                    exc,
                )

        mention = None
        if channel_id and member_id and self.moderation.mention_member:
            # 文字子频道的纯文本支持 <@!user_id>。
            mention = "<@!%s>" % member_id
        warning = self.moderation.warning_text(
            match,
            member_id,
            warning_count,
            member_name=member_name,
            mention=mention,
        )
        if group_id and member_id:
            warning = self._replace_raw_group_mentions(warning, member_name)
        return await self.safe_reply(message, warning)

    def _build_system(self, snippets, key):
        parts = [prompts.PERSONA_SYSTEM]
        mem = self._memory_context(key)
        if mem:
            parts.append("以下是你与这位朋友的近期聊天记忆（仅作上下文，不要逐字复述）：\n" + mem)
        if snippets:
            parts.append(prompts.KNOWLEDGE_WITH_MATERIALS + "\n\n" + snippets)
        else:
            parts.append(prompts.KNOWLEDGE_NO_MATERIALS)
        return "\n\n".join(parts)

    def _build_user_parts(self, text, attachments):
        parts = []
        if text:
            parts.append({"type": "text", "text": text})
        for att in attachments:
            ct = (getattr(att, "content_type", None) or "").lower()
            url = getattr(att, "url", None)
            if not url:
                continue
            if ct.startswith("image"):
                parts.append({"type": "image_url", "image_url": {"url": url}})
            elif ct.startswith("video"):
                parts.append({"type": "text", "text": "[对方发来一段视频，地址：" + url + "]"})
        if not parts:
            parts.append({"type": "text", "text": ""})
        return parts

    async def _review(self, text):
        if time.time() < self._review_cooldown_until:
            return text
        try:
            messages = [
                {"role": "system", "content": prompts.REVIEW_SYSTEM},
                {"role": "user", "content": text},
            ]
            reviewed = await asyncio.get_running_loop().run_in_executor(
                None, call_llm, messages, self.config
            )
            return reviewed.strip() or text
        except Exception as exc:  # noqa: BLE001
            error_text = str(exc)
            if "429" in error_text or "Too Many Requests" in error_text:
                self._review_cooldown_until = time.time() + 60
                _log.warning(
                    "review rate limited, skip review for 60s: %s", exc
                )
            else:
                _log.warning("review skipped, use original reply: %s", exc)
            return text

    async def handle(self, message, allow_fallback=True):
        try:
            author = getattr(message, "author", None)
            if author is not None and getattr(author, "bot", False):
                return

            text = self._clean(message)
            attachments = getattr(message, "attachments", None) or []
            if not text and not attachments:
                return

            if text.startswith("/reload"):
                n = self.kb.reload()
                rule_count = self.moderation.reload()
                group_count = self.moderation.group_rule_count()
                await self.safe_reply(
                    message,
                    "喵呜～知识库重新啃完啦，共 %d 个 txt 文件；"
                    "全局违禁词规则 %d 条，群专属规则 %d 个群捏～"
                    % (n, rule_count, group_count),
                )
                return
            if text.startswith("/groupid"):
                group_openid = getattr(message, "group_openid", None)
                if group_openid:
                    await self.safe_reply(
                        message,
                        "本群 OpenID 是：\n%s\n\n"
                        "把它作为文件名放在 moderation/groups/ 下，"
                        "就能给本群单独配置关键词规则；\n"
                        "也可以把它填进 config.json 的 "
                        "moderation.join_review.group_standard_answers，"
                        "给本群单独配置入群标准答案啦～" % group_openid,
                    )
                else:
                    await self.safe_reply(
                        message, "喵？这个指令要在群里用才行哦～"
                    )
                return
            if text.startswith("/help"):
                await self.safe_reply(message, prompts.HELP_TEXT)
                return

            key = self._conv_key(message)
            self._memory_append(key, "user", text or "[发来图片/视频]")

            snippets = ""
            if text:
                snippets = self.kb.search(
                    text,
                    top_k=self.config.get("bot", {}).get("knowledge_top_k", 3),
                    max_chars=self.config.get("bot", {}).get("max_context_chars", 4000),
                )

            system_prompt = self._build_system(snippets, key)
            user_parts = self._build_user_parts(text, attachments)

            answer = await asyncio.get_running_loop().run_in_executor(
                None, generate_reply, self.config, system_prompt, user_parts
            )
            if not answer:
                # 普通全量消息（allow_fallback=False）模型返回空时保持安静，
                # 避免群里每个人说话都收到「刚才走神了」。
                if not allow_fallback:
                    return
                answer = "呜呜，猫猫刚才走神了，再问一遍喵？"

            if self.review_enabled:
                answer = await self._review(answer)

            self._memory_append(key, "bot", answer)
            await self.safe_reply(message, answer)
        except Exception as exc:  # noqa: BLE001
            error_text = str(exc)
            if "429" in error_text or "Too Many Requests" in error_text:
                # 大模型限流时不要刷屏回复，等额度恢复即可。
                _log.warning("handle rate limited, skip reply: %s", exc)
                return
            _log.error("handle error: %s", exc)
            if not allow_fallback:
                return
            try:
                await self.safe_reply(message, "呜呜，猫猫刚才走神了，再问一遍喵？")
            except Exception:
                pass

    async def safe_reply(self, message, text):
        chunks = split_text(text)
        primary = chunks[0]
        rest = chunks[1:]

        sent = False
        try:
            await message.reply(content=primary)
            sent = True
        except Exception as exc:  # noqa: BLE001
            _log.warning("passive reply failed: %s, fallback to active", exc)
            sent = await self._active_send(message, primary)

        for chunk in rest:
            await self._active_send(message, chunk)
            await asyncio.sleep(0.6)

        return sent

    async def _active_send(self, message, text):
        api = self.api
        try:
            group_openid = getattr(message, "group_openid", None)
            channel_id = getattr(message, "channel_id", None)
            user_openid = getattr(message, "user_openid", None)
            guild_id = getattr(message, "guild_id", None)
            if group_openid:
                await api.post_group_message(group_openid=group_openid, msg_type=0, content=text)
            elif channel_id:
                await api.post_message(channel_id=channel_id, content=text)
            elif user_openid:
                await api.post_c2c_message(openid=user_openid, content=text)
            elif guild_id:
                await api.post_dms_message(guild_id=guild_id, content=text)
            else:
                return False
            return True
        except Exception as exc:  # noqa: BLE001
            _log.error("active send failed: %s", exc)
            return False
