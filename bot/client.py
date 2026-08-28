import asyncio
import re
import time

import botpy
from botpy import logging

from . import prompts
from .knowledge import KnowledgeBase
from .llm import call_llm, generate_reply

import botpy.connection as _conn


def _parse_group_message_create(self, payload):
    _message = _conn.GroupMessage(self.api, payload.get("id", None), payload.get("d", {}))
    self._dispatch("group_message_create", _message)


_conn.ConnectionState.parse_group_message_create = _parse_group_message_create

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
    def __init__(self, config, kb, **kwargs):
        super().__init__(**kwargs)
        self.config = config
        self.kb = kb
        bot_cfg = config.get("bot", {})
        self.memory_hours = bot_cfg.get("memory_hours", 12)
        self.review_enabled = bot_cfg.get("review_enabled", True)
        self.max_mem_turns = bot_cfg.get("max_memory_turns", 20)
        self.memory = {}

    async def on_ready(self):
        _log.info("robot %s on_ready!", getattr(self.robot, "name", ""))
        try:
            asyncio.create_task(self._memory_sweeper())
        except Exception as exc:  # noqa: BLE001
            _log.error("sweeper start error: %s", exc)

    async def on_group_at_message_create(self, message):
        await self.handle(message)

    async def on_at_message_create(self, message):
        await self.handle(message)

    async def on_c2c_message_create(self, message):
        await self.handle(message)

    async def on_group_message_create(self, message):
        if not self.config.get("reply_all_messages"):
            return
        raw = getattr(message, "content", "") or ""
        if MENTION_RE.search(raw):
            return
        await self.handle(message)

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
            _log.error("review error: %s", exc)
            return text

    async def handle(self, message):
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
                await self.safe_reply(message, "喵呜～知识库重新啃完啦，共 %d 个 txt 文件捏～" % n)
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
                answer = "呜呜，猫猫刚才走神了，再问一遍喵？"

            if self.review_enabled:
                answer = await self._review(answer)

            self._memory_append(key, "bot", answer)
            await self.safe_reply(message, answer)
        except Exception as exc:  # noqa: BLE001
            _log.error("handle error: %s", exc)
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
            await self._active_send(message, primary)

        for chunk in rest:
            await self._active_send(message, chunk)
            await asyncio.sleep(0.6)

        _ = sent

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
        except Exception as exc:  # noqa: BLE001
            _log.error("active send failed: %s", exc)
