import asyncio
import json
import logging
import os
import re
import time
import unicodedata
from dataclasses import dataclass, field

from . import prompts
from .llm import call_llm
from .moderation import DEFAULT_LOG_DIR, DailyLogger

_log = logging.getLogger(__name__)

DEFAULT_MODE = "suggest"
VALID_MODES = ("suggest", "approve_only", "auto")
DEFAULT_REJECT_REASON = "抱歉喵，入群验证没有通过，请认真回答管理员的问题后重新申请～"
DEFAULT_APPROVE_SCORE = 70
PROCESSED_TTL = 600
MAX_QA_ITEMS = 10
MAX_STANDARD_ANSWERS = 20

_JSON_FENCE_RE = re.compile(r"^```[a-zA-Z0-9_-]*\s*|\s*```$")
_NOTIFY_MENTION_RE = re.compile(r"<@!?[^>\r\n]{1,64}>")
_QUESTION_NOISE_RE = re.compile(
    r"[\s\u200b-\u200f\u202a-\u202e\ufeff?？!！:：,，.。;；、\"'“”‘’()（）\[\]【】<>《》/\\|]+"
)
_PLACEHOLDER_GROUP_RE = re.compile(
    r"(请替换|替换成|占位|示例群|真实群|your[_-]?group|example[_-]?group)",
    re.IGNORECASE,
)


def _normalize_question(text):
    """把问题文本归一化，便于匹配管理员配置的标准答案。"""
    text = unicodedata.normalize("NFKC", str(text or "")).lower()
    return _QUESTION_NOISE_RE.sub("", text)


def _to_int(value, default, minimum=0, maximum=100):
    try:
        value = int(float(value))
    except (TypeError, ValueError):
        match = re.search(r"-?\d+", str(value or ""))
        if not match:
            return default
        value = int(match.group(0))
    return max(minimum, min(maximum, value))


def _to_bool(value, default=False):
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return bool(value)
    if isinstance(value, str):
        text = value.strip().lower()
        if text in ("1", "true", "yes", "on", "是", "开启"):
            return True
        if text in ("0", "false", "no", "off", "否", "关闭", ""):
            return False
    return default


def _clip(value, limit):
    text = str(value or "").strip()
    if len(text) > limit:
        return text[:limit] + "..."
    return text


def _string_list(value, limit=6, item_limit=120):
    if not isinstance(value, list):
        return []
    items = []
    for item in value:
        text = _clip(item, item_limit)
        if text:
            items.append(text)
        if len(items) >= limit:
            break
    return items


def _extract_json_object(text):
    text = (text or "").strip()
    if not text:
        raise ValueError("审核模型返回了空内容")
    if text.startswith("```"):
        text = _JSON_FENCE_RE.sub("", text).strip()
    try:
        return json.loads(text)
    except Exception:  # noqa: BLE001
        pass
    start = text.find("{")
    end = text.rfind("}")
    if start == -1 or end <= start:
        raise ValueError("审核模型没有返回 JSON 对象")
    return json.loads(text[start:end + 1])


class GroupMemberEvent:
    """GROUP_MEMBER_ADD / GROUP_MEMBER_REMOVE 的轻量事件对象。"""

    __slots__ = (
        "_api",
        "event_id",
        "timestamp",
        "group_openid",
        "member_openid",
        "user_openid",
    )

    def __init__(self, api, event_id, data):
        data = data or {}
        self._api = api
        self.event_id = event_id
        self.timestamp = data.get("timestamp")
        self.group_openid = data.get("group_openid") or ""
        self.member_openid = data.get("member_openid") or ""
        self.user_openid = data.get("user_openid") or ""

    def __repr__(self):
        return str({
            "event_id": self.event_id,
            "timestamp": self.timestamp,
            "group_openid": self.group_openid,
            "member_openid": self.member_openid,
            "user_openid": self.user_openid,
        })


class GroupJoinRequestEvent:
    """GROUP_JOIN_REQUEST 的轻量事件对象。

    官方事件里可能带两种验证方式：
    - verify_message：申请人填写的验证消息；
    - admin_review_qa：管理员设置的入群问答，位于 verify_info.review_qa_list。
    """

    __slots__ = (
        "_api",
        "event_id",
        "timestamp",
        "group_openid",
        "join_request_id",
        "member_openid",
        "union_openid",
        "username",
        "apply_at",
        "apply_source",
        "invited_by",
        "bot",
        "method",
        "verify_message",
        "qa_list",
        "risk_tips",
        "auto_approved",
        "raw",
    )

    def __init__(self, api, event_id, data):
        data = data or {}
        verify = data.get("verify_info") or {}
        if not isinstance(verify, dict):
            verify = {}
        qa_list = verify.get("review_qa_list") or []
        if not isinstance(qa_list, list):
            qa_list = []
        auto_approved = data.get("auto_approved") or {}
        if not isinstance(auto_approved, dict):
            auto_approved = {}

        self._api = api
        self.event_id = event_id
        self.timestamp = data.get("timestamp")
        self.group_openid = data.get("group_openid") or ""
        self.join_request_id = data.get("join_request_id") or ""
        self.member_openid = data.get("member_openid") or ""
        self.union_openid = data.get("union_openid") or ""
        self.username = data.get("username") or ""
        self.apply_at = data.get("apply_at") or ""
        self.apply_source = data.get("apply_source") or ""
        self.invited_by = data.get("invited_by") or ""
        self.bot = bool(data.get("bot", False))
        self.method = verify.get("method") or ""
        self.verify_message = verify.get("verify_message") or ""
        self.qa_list = qa_list
        self.risk_tips = data.get("risk_tips") or ""
        self.auto_approved = auto_approved
        self.raw = data

    def __repr__(self):
        return str({
            "event_id": self.event_id,
            "group_openid": self.group_openid,
            "join_request_id": self.join_request_id,
            "member_openid": self.member_openid,
            "username": self.username,
            "apply_source": self.apply_source,
            "method": self.method,
            "qa_count": len(self.qa_list),
        })


@dataclass
class JoinReviewResult:
    decision: str = "review"
    llm_decision: str = ""
    score: int = 0
    summary: str = ""
    reasons: list = field(default_factory=list)
    risk: list = field(default_factory=list)
    local_match: object = None
    error: str = ""
    action: str = "manual"
    action_ok: bool = False
    action_error: str = ""
    notified: bool = False
    notify_error: str = ""


class JoinReviewLogger(DailyLogger):
    """入群审核记录，每天生成 join-reviews-YYYY-MM-DD.log / .jsonl。"""

    prefix = "join-reviews"

    @staticmethod
    def _format_human(rec):
        member_name = str(rec.get("username") or "").replace("\r", " ").replace("\n", " ")
        member_text = rec.get("member_id") or ""
        if member_name:
            member_text = "%s(%s)" % (member_text, member_name)

        decision = rec.get("decision") or "review"
        decision_text = {
            "approve": "通过",
            "review": "人工复核",
            "decline": "不建议通过",
        }.get(decision, decision)

        action = rec.get("action") or "manual"
        if action == "platform":
            action_text = "平台已自动通过"
        elif rec.get("action_ok"):
            action_text = {
                "approve": "自动通过成功",
                "decline": "自动拒绝成功",
            }.get(action, action + "成功")
        elif rec.get("action_error"):
            error = str(rec.get("action_error") or "").replace("\r", " ").replace("\n", " ")
            action_text = {
                "approve": "自动通过失败",
                "decline": "自动拒绝失败",
            }.get(action, action + "失败") + ((": " + error) if error else "")
        else:
            action_text = "未操作（人工复核）"

        qa_text = str(rec.get("qa_text") or "").replace("\r", " ").replace("\n", " / ")
        summary = str(rec.get("summary") or "").replace("\r", " ").replace("\n", " ")
        standard_text = ""
        if rec.get("standard_answers_configured"):
            standard_text = " | 标准答案=%s/%s(%s)" % (
                rec.get("standard_answers_matched", 0),
                rec.get("standard_answers_configured", 0),
                rec.get("standard_answers_source", "global"),
            )
        return (
            "%s | 群=%s | 申请人=%s | 方式=%s | 评分=%s | 结论=%s | 处理=%s | 摘要=%s | 问答=%s%s"
            % (
                rec.get("time", ""),
                rec.get("group_id", ""),
                member_text,
                rec.get("verify_method", "") or "无",
                rec.get("score", 0),
                decision_text,
                action_text,
                summary,
                qa_text,
                standard_text,
            )
        )


class JoinReview:
    """入群申请自动审核：读取官方入群问答，调用大模型评估并可选自动审批。

    默认关闭（moderation.join_review.enabled=false）。开启后需要机器人是群管理员，
    且群使用「管理员审核问答」或「验证消息」；平台才会下发 GROUP_JOIN_REQUEST。
    """

    def __init__(self, config, base_dir, moderation=None):
        mod_cfg = config.get("moderation") or config.get("bot", {}).get("moderation") or {}
        cfg = mod_cfg.get("join_review") or {}
        self.config = config
        self.base_dir = base_dir or os.getcwd()
        self.moderation = moderation
        self.enabled = _to_bool(cfg.get("enabled", False), False)

        mode = str(cfg.get("mode") or DEFAULT_MODE).strip().lower()
        if mode not in VALID_MODES:
            _log.warning(
                "moderation.join_review.mode 只能是 suggest / approve_only / auto，已改用 suggest"
            )
            mode = DEFAULT_MODE
        self.mode = mode

        self.approve_score = _to_int(
            cfg.get("approve_score", DEFAULT_APPROVE_SCORE), DEFAULT_APPROVE_SCORE, 0, 100
        )
        self.criteria = str(cfg.get("criteria") or "").strip() or prompts.JOIN_AUDIT_DEFAULT_CRITERIA
        self.reject_reason = str(cfg.get("reject_reason") or DEFAULT_REJECT_REASON)
        self.decline_blacklist = _to_bool(cfg.get("decline_blacklist", False), False)
        # 默认不自动拒绝：模型认为不适合时只记录并留给管理员处理。
        self.auto_decline = _to_bool(cfg.get("auto_decline", False), False)
        self.notify_group_openid = str(cfg.get("notify_group_openid") or "").strip()
        self.max_answer_chars = _to_int(
            cfg.get("max_answer_chars", 2000), 2000, 100, 20000
        )
        self.standard_answers = self._parse_standard_answers(cfg.get("standard_answers"))
        self.standard_map = self._build_standard_map(
            self.standard_answers, "standard_answers"
        )
        self._standard_sorted = self._sorted_standard_items(self.standard_map)

        group_mode = str(
            cfg.get("group_standard_answers_mode") or "replace"
        ).strip().lower()
        if group_mode not in ("replace", "merge"):
            _log.warning(
                "moderation.join_review.group_standard_answers_mode 只能是 replace 或 merge，已改用 replace"
            )
            group_mode = "replace"
        self.group_standard_answers_mode = group_mode
        self.group_standard_answers = self._parse_group_standard_answers(
            cfg.get("group_standard_answers")
        )
        self.placeholder_group_standard_ids = [
            group_id
            for group_id in self.group_standard_answers
            if self._is_placeholder_group_id(group_id)
        ]
        self.active_group_standard_count = len(self.group_standard_answers) - len(
            self.placeholder_group_standard_ids
        )
        if self.enabled and self.placeholder_group_standard_ids:
            _log.warning(
                "moderation.join_review.group_standard_answers 里还有未替换的占位群: %s；"
                "这些条目不会匹配真实群，请替换成真实群 OpenID 或删除",
                "、".join(self.placeholder_group_standard_ids),
            )
        self.group_standard_maps = {}
        self._group_standard_contexts = {}
        for group_id, items in self.group_standard_answers.items():
            group_map = self._build_standard_map(
                items, "group_standard_answers[%s]" % group_id
            )
            self.group_standard_maps[group_id] = group_map
            # 显式空配置（[] / {}）表示该群不使用标准答案，即使 merge 模式也不继承全局。
            if group_mode == "merge" and items:
                merged = dict(self.standard_map)
                merged.update(group_map)
                self._group_standard_contexts[group_id] = (
                    len(merged),
                    merged,
                    self._sorted_standard_items(merged),
                )
            else:
                self._group_standard_contexts[group_id] = (
                    len(group_map),
                    group_map,
                    self._sorted_standard_items(group_map),
                )
        self._default_standard_context = (
            len(self.standard_map),
            self.standard_map,
            self._standard_sorted,
        )
        self.llm_temperature = max(0.0, min(1.0, self._to_float(cfg.get("temperature", 0.2), 0.2)))
        self.llm_max_tokens = _to_int(cfg.get("max_tokens", 500), 500, 100, 4000)

        raw_log_dir = cfg.get("log_dir") or mod_cfg.get("log_dir") or DEFAULT_LOG_DIR
        self.logger = JoinReviewLogger(
            self._resolve(raw_log_dir), cfg.get("max_log_chars", 1000)
        )

        self._processed = {}
        self._processed_ttl = PROCESSED_TTL

    @staticmethod
    def _to_float(value, default):
        try:
            return float(value)
        except (TypeError, ValueError):
            return float(default)

    def _resolve(self, path):
        path = str(path)
        if os.path.isabs(path):
            return path
        return os.path.abspath(os.path.join(self.base_dir, path))

    @staticmethod
    def _is_placeholder_group_id(group_id):
        return bool(_PLACEHOLDER_GROUP_RE.search(str(group_id or "")))

    @staticmethod
    def _parse_standard_answers(raw):
        """支持两种写法：

        - {"问题": "标准答案", ...}
        - [{"question": "问题", "answer": "标准答案"}, ...]
        """
        if raw in (None, "", [], {}):
            return []

        if isinstance(raw, dict):
            pairs = list(raw.items())
        elif isinstance(raw, list):
            pairs = []
            for entry in raw:
                if not isinstance(entry, dict):
                    _log.warning(
                        "moderation.join_review.standard_answers 的列表项必须是对象，已忽略: %r",
                        entry,
                    )
                    continue
                pairs.append(
                    (
                        entry.get("question", entry.get("q")),
                        entry.get("answer", entry.get("a")),
                    )
                )
        else:
            _log.warning(
                "moderation.join_review.standard_answers 必须是对象或数组，已忽略"
            )
            return []

        items = []
        for question, answer in pairs:
            if not isinstance(question, str) or not isinstance(answer, str):
                _log.warning(
                    "moderation.join_review.standard_answers 的问题和答案都必须是字符串，已忽略: %r",
                    (question, answer),
                )
                continue
            question = _clip(question, 300)
            answer = _clip(answer, 1000)
            if not question or not answer:
                continue
            items.append({"question": question, "answer": answer})
            if len(items) >= MAX_STANDARD_ANSWERS:
                if len(pairs) > MAX_STANDARD_ANSWERS:
                    _log.warning(
                        "moderation.join_review.standard_answers 最多支持 %d 条，多余部分已忽略",
                        MAX_STANDARD_ANSWERS,
                    )
                break
        return items

    @staticmethod
    def _build_standard_map(items, label):
        mapping = {}
        for item in items:
            key = _normalize_question(item["question"])
            if not key:
                continue
            if key in mapping:
                _log.warning(
                    "moderation.join_review.%s 里有重复问题，已保留先出现的一条: %s",
                    label,
                    item["question"],
                )
                continue
            mapping[key] = item["answer"]
        return mapping

    @staticmethod
    def _sorted_standard_items(mapping):
        return sorted(mapping.items(), key=lambda pair: len(pair[0]), reverse=True)

    def _parse_group_standard_answers(self, raw):
        """解析 {群 OpenID: 标准答案}；群配置默认替换全局，也可设为 merge。"""
        if raw in (None, "", [], {}):
            return {}
        if not isinstance(raw, dict):
            _log.warning(
                "moderation.join_review.group_standard_answers 必须是 "
                "{群OpenID: 标准答案} 形式的对象，已忽略"
            )
            return {}

        result = {}
        for group_id, value in raw.items():
            if not isinstance(group_id, str) or not group_id.strip():
                _log.warning(
                    "moderation.join_review.group_standard_answers 的键必须是群 OpenID，已忽略: %r",
                    group_id,
                )
                continue
            group_id = group_id.strip()
            if not isinstance(value, (dict, list)):
                _log.warning(
                    "moderation.join_review.group_standard_answers[%s] 必须是对象或数组，已忽略",
                    group_id,
                )
                continue
            items = self._parse_standard_answers(value)
            if items:
                result[group_id] = items
            elif value in ({}, []):
                # 显式空配置：这个群不使用标准答案，即使全局配置了也不继承。
                result[group_id] = []
            else:
                _log.warning(
                    "moderation.join_review.group_standard_answers[%s] 没有有效条目，"
                    "已回退全局标准答案",
                    group_id,
                )
        return result

    def _standard_context(self, group_id=None):
        """返回 (标准答案数量, 归一化问题映射, 按问题长度排序的条目)。"""
        if group_id and group_id in self._group_standard_contexts:
            return self._group_standard_contexts[group_id]
        return self._default_standard_context

    def _standard_for(self, question, group_id=None):
        """按问题文本查找该群的标准答案；找不到返回空字符串。"""
        key = _normalize_question(question)
        if not key:
            return ""

        _count, mapping, sorted_items = self._standard_context(group_id)
        answer = mapping.get(key)
        if answer:
            return answer

        # 问题措辞略有差异时做一次包含匹配，避免因为多一个「呢」「呀」就匹配不上。
        if len(key) < 4:
            return ""
        for std_key, std_answer in sorted_items:
            if len(std_key) >= 4 and (std_key in key or key in std_key):
                return std_answer
        return ""

    def _standard_count(self, group_id=None):
        return self._standard_context(group_id)[0]

    def _matched_standard_count(self, qa_list, group_id=None):
        return sum(
            1
            for item in qa_list
            if self._standard_for(item.get("question"), group_id)
        )

    def _standard_source(self, group_id=None):
        if group_id and group_id in self._group_standard_contexts:
            return (
                "group+global"
                if self.group_standard_answers_mode == "merge"
                else "group"
            )
        return "global" if self.standard_map else "none"

    def _is_duplicate(self, key):
        if not key:
            return False
        now = time.time()
        for old_key, ts in list(self._processed.items()):
            if now - ts > self._processed_ttl:
                del self._processed[old_key]
        if key in self._processed:
            return True
        self._processed[key] = now
        return False

    async def handle(self, event, api=None):
        """审核一条入群申请；返回写入日志的记录（关闭或重复时返回 None）。"""
        if not self.enabled:
            return None

        req = self._coerce_event(event)
        if not req.group_openid or not req.member_openid:
            _log.warning("join review event missing group/member openid: %r", event)
            return None

        dedupe_key = req.join_request_id or "%s|%s|%s" % (
            req.group_openid,
            req.member_openid,
            req.apply_at,
        )
        if self._is_duplicate(dedupe_key):
            _log.debug("join review duplicate ignored: %s", dedupe_key)
            return None

        if req.auto_approved:
            result = JoinReviewResult(
                decision="approve",
                llm_decision="approve",
                score=100,
                summary="平台自动审批策略已通过",
                reasons=["该申请由 QQ 平台的自动审批策略直接通过，机器人未重复审批"],
                action="platform",
                action_ok=True,
            )
            result.notified, result.notify_error = await self._notify(req, result, api)
            record = self._record(req, result)
            self.logger.write(record)
            return record

        try:
            result = await asyncio.get_running_loop().run_in_executor(None, self._evaluate, req)
        except Exception as exc:  # noqa: BLE001
            _log.error("join review evaluate error: %s", exc)
            result = JoinReviewResult(
                decision="review",
                score=0,
                summary="自动审核失败，请人工复核",
                reasons=[_clip(exc, 160)],
                error=str(exc),
            )

        result.action = self._decide_action(req, result)
        if result.action in ("approve", "decline"):
            await self._apply_action(req, result, api)

        result.notified, result.notify_error = await self._notify(req, result, api)
        record = self._record(req, result)
        self.logger.write(record)
        return record

    def _coerce_event(self, event):
        if isinstance(event, GroupJoinRequestEvent):
            return event
        data = getattr(event, "raw", None)
        if not isinstance(data, dict):
            data = event if isinstance(event, dict) else {}

        # 兼容较新版本 SDK 自己定义的事件对象：没有 raw 字典时从属性里取。
        if not data and not isinstance(event, dict):
            data = {}
            for field in (
                "group_openid",
                "join_request_id",
                "member_openid",
                "union_openid",
                "username",
                "apply_at",
                "apply_source",
                "invited_by",
                "bot",
                "timestamp",
                "risk_tips",
            ):
                value = getattr(event, field, None)
                if value is not None:
                    data[field] = value

            verify = getattr(event, "verify_info", None)
            if isinstance(verify, dict):
                data["verify_info"] = verify
            elif verify is not None:
                data["verify_info"] = {
                    "method": getattr(verify, "method", None) or "",
                    "verify_message": getattr(verify, "verify_message", None) or "",
                    "review_qa_list": (
                        getattr(verify, "review_qa_list", None)
                        or getattr(verify, "qa_list", None)
                        or []
                    ),
                }

        return GroupJoinRequestEvent(None, getattr(event, "event_id", None), data)

    def _clean_qa(self, qa_list):
        cleaned = []
        for item in qa_list or []:
            if not isinstance(item, dict):
                continue
            question = _clip(item.get("question"), 300)
            answer = _clip(item.get("answer"), self.max_answer_chars)
            if not question and not answer:
                continue
            cleaned.append({"question": question, "answer": answer})
            if len(cleaned) >= MAX_QA_ITEMS:
                break
        return cleaned

    def _collect_text(self, req):
        qa_list = self._clean_qa(req.qa_list)
        parts = []
        scan_parts = []
        qa_parts = []
        if req.verify_message:
            message = _clip(req.verify_message, self.max_answer_chars)
            parts.append("验证消息：\n" + message)
            scan_parts.append(message)
            qa_parts.append("验证消息：" + message)
        for item in qa_list:
            question = item["question"] or "（未提供问题）"
            answer = item["answer"] or ""
            parts.append("问题：%s\n回答：%s" % (question, answer or "（未填写）"))
            scan_parts.append(answer)
            qa_parts.append("%s => %s" % (question, answer or "（未填写）"))
        return "\n\n".join(parts), "\n".join(scan_parts), " | ".join(qa_parts), qa_list

    def _evaluate(self, req):
        _text, scan_text, _qa_text, qa_list = self._collect_text(req)
        if not scan_text.strip():
            return JoinReviewResult(
                decision="review",
                score=0,
                summary="没有可审核的验证内容，建议人工复核",
                reasons=["申请未填写验证消息，也没有入群问答"],
            )

        standard_count = self._standard_count(req.group_openid)
        standard_matched = self._matched_standard_count(qa_list, req.group_openid)
        local_match = None
        if self.moderation is not None:
            try:
                # 只扫描申请人填写的内容，不扫描管理员设置的问题，
                # 避免问题本身含有关键词时误伤正常申请。
                local_match = self.moderation.find_match(
                    scan_text, group_id=req.group_openid
                )
            except Exception as exc:  # noqa: BLE001
                _log.warning("join review local rule check failed: %s", exc)

        result = None
        error = ""
        try:
            raw = self._call_llm(req, qa_list, local_match)
            result = self._parse_result(raw)
        except Exception as exc:  # noqa: BLE001
            error = str(exc)
            _log.warning("join review llm failed: %s", exc)

        if result is None:
            result = JoinReviewResult(
                decision="review",
                score=0,
                summary="自动审核失败，请人工复核",
                reasons=[_clip(error, 160)],
                error=error,
            )

        if local_match is not None:
            result.local_match = local_match
            result.risk.append(
                "命中群规【%s】：%s" % (local_match.category, local_match.keyword)
            )
            if result.decision == "approve":
                result.decision = "review"
                result.reasons.append("本地群规命中，已阻止自动通过，建议人工复核")

        if req.risk_tips and result.decision == "approve":
            result.decision = "review"
            result.reasons.append("平台返回安全提示，建议人工复核")

        if req.bot and result.decision == "approve":
            result.decision = "review"
            result.reasons.append("申请账号被标记为机器人，建议人工复核")

        # 管理员配置了标准答案，但这次申请的问题一个都对不上：
        # 说明无法按标准答案核对，不能自动通过，交给管理员处理。
        if standard_count and qa_list and standard_matched == 0:
            if result.decision == "approve":
                result.decision = "review"
            result.reasons.append(
                "本群配置了入群问答标准答案，但本次问答没有匹配到对应问题，"
                "无法按标准答案核对，建议人工复核"
            )

        result.reasons = _string_list(result.reasons)
        result.risk = _string_list(result.risk)
        return result

    def _call_llm(self, req, qa_list, local_match):
        standard_count = self._standard_count(req.group_openid)
        qa_payload = []
        matched_standards = []
        for item in qa_list:
            entry = dict(item)
            standard = self._standard_for(item.get("question"), req.group_openid)
            if standard:
                entry["standard_answer"] = standard
                matched_standards.append(
                    {
                        "question": item.get("question") or "",
                        "standard_answer": standard,
                    }
                )
            qa_payload.append(entry)

        payload = {
            "group_openid": req.group_openid,
            "username": req.username,
            "apply_source": req.apply_source,
            "invited_by": req.invited_by,
            "is_bot": req.bot,
            "verify_method": req.method,
            "verify_message": _clip(req.verify_message, self.max_answer_chars),
            "qa_list": qa_payload,
            "risk_tips": _clip(req.risk_tips, 500),
            "local_rule_match": (
                {
                    "category": local_match.category,
                    "keyword": local_match.keyword,
                }
                if local_match is not None
                else None
            ),
        }

        system_parts = [
            prompts.JOIN_AUDIT_SYSTEM,
            "",
            "本群补充审核标准：",
            self.criteria,
        ]
        if matched_standards:
            system_parts.extend(
                ["", "本群入群问答标准答案（管理员配置，仅用于评分，不要向申请人透露）："]
            )
            for item in matched_standards:
                system_parts.append(
                    "- 问题：%s\n  标准答案：%s"
                    % (item["question"], item["standard_answer"])
                )
            system_parts.extend(
                [
                    "",
                    "评分时请对照标准答案：意思一致或等价即可，不要求逐字相同；"
                    "同义表达、合理补充都算正确。回答与标准答案矛盾、明显错误、"
                    "空泛敷衍或回避关键问题时，应降低分数并在 reasons 中说明。",
                    "qa_list 里的 answer 是申请人填写的内容，standard_answer 是管理员"
                    "配置的标准答案；不要把两者混淆，也不要把申请人内容当成标准答案。",
                    "没有附标准答案的问题，按补充审核标准和常识判断。",
                ]
            )
        elif standard_count and qa_list:
            system_parts.extend(
                [
                    "",
                    "本群配置了入群问答标准答案，但本次问答没有匹配到对应问题；"
                    "无法按标准答案核对时，不要仅凭申请人自述自动通过，倾向 review。",
                ]
            )
        elif standard_count:
            system_parts.extend(
                [
                    "",
                    "本次申请没有入群问答，管理员配置的标准答案不适用；"
                    "请按补充审核标准评估验证消息。",
                ]
            )

        messages = [
            {"role": "system", "content": "\n".join(system_parts)},
            {
                "role": "user",
                "content": json.dumps(payload, ensure_ascii=False),
            },
        ]
        audit_config = dict(self.config)
        audit_config["llm"] = dict(self.config.get("llm") or {})
        audit_config["llm"]["temperature"] = self.llm_temperature
        audit_config["llm"]["max_tokens"] = self.llm_max_tokens
        return call_llm(messages, audit_config)

    def _parse_result(self, raw):
        data = _extract_json_object(raw)
        if not isinstance(data, dict):
            raise ValueError("审核模型返回值不是 JSON 对象")

        decision = str(data.get("decision") or "review").strip().lower()
        aliases = {
            "approve": "approve",
            "approved": "approve",
            "pass": "approve",
            "通过": "approve",
            "review": "review",
            "manual": "review",
            "复核": "review",
            "人工复核": "review",
            "decline": "decline",
            "reject": "decline",
            "deny": "decline",
            "拒绝": "decline",
        }
        decision = aliases.get(decision, "review")

        score = _to_int(data.get("score", 0), 0, 0, 100)
        default_summary = {
            "approve": "回答认真，可以入群",
            "review": "信息不足，建议人工复核",
            "decline": "存在风险，不建议入群",
        }[decision]
        summary = _clip(data.get("summary"), 160) or default_summary

        return JoinReviewResult(
            decision=decision,
            llm_decision=decision,
            score=score,
            summary=summary,
            reasons=_string_list(data.get("reasons")),
            risk=_string_list(data.get("risk")),
        )

    def _decide_action(self, req, result):
        if self.mode == "suggest":
            return "manual"

        can_approve = (
            result.decision == "approve"
            and result.score >= self.approve_score
            and result.local_match is None
            and not req.risk_tips
            and not req.bot
        )
        if self.mode == "approve_only":
            return "approve" if can_approve else "manual"
        # 默认 auto_decline=false：模型认为不适合时也只记录，不自动拒绝，
        # 留给群管理员人工处理。只有显式开启 auto_decline 才会自动拒绝。
        if result.decision == "decline" and self.auto_decline:
            return "decline"
        if can_approve:
            return "approve"
        return "manual"

    async def _apply_action(self, req, result, api):
        if api is None or not hasattr(api, "approval_group_join_request"):
            result.action_error = "当前 qq-botpy 版本缺少入群审批接口"
            _log.error(result.action_error)
            return

        kwargs = {}
        if req.join_request_id:
            kwargs["join_request_id"] = req.join_request_id
        if result.action == "decline":
            kwargs["reject_reason"] = self.reject_reason
            kwargs["add_to_member_blacklist"] = self.decline_blacklist

        try:
            await api.approval_group_join_request(
                req.group_openid,
                req.member_openid,
                op=result.action,
                **kwargs
            )
            result.action_ok = True
            _log.info(
                "join review %s: group=%s member=%s",
                result.action,
                req.group_openid,
                req.member_openid,
            )
        except Exception as exc:  # noqa: BLE001
            result.action_error = _clip(exc, 500)
            _log.warning(
                "join review %s failed: group=%s member=%s error=%s",
                result.action,
                req.group_openid,
                req.member_openid,
                exc,
            )

    async def _notify(self, req, result, api):
        target = self.notify_group_openid
        if not target:
            return False, ""
        if target.lower() == "same":
            target = req.group_openid
        if api is None or not hasattr(api, "post_group_message"):
            return False, "当前 qq-botpy 版本缺少群消息发送接口"
        try:
            await api.post_group_message(
                group_openid=target,
                msg_type=0,
                content=self._notify_text(req, result),
            )
            return True, ""
        except Exception as exc:  # noqa: BLE001
            _log.warning(
                "join review notify failed: group=%s error=%s",
                target,
                exc,
            )
            return False, _clip(exc, 300)

    def _notify_text(self, req, result):
        decision_text = {
            "approve": "通过",
            "review": "人工复核",
            "decline": "不建议通过",
        }.get(result.decision, result.decision)

        if result.action == "platform":
            action_text = "平台已自动通过"
        elif result.action_ok:
            action_text = {
                "approve": "已自动通过",
                "decline": "已自动拒绝",
            }.get(result.action, "已处理")
        elif result.action_error:
            action_text = {
                "approve": "自动通过失败",
                "decline": "自动拒绝失败",
            }.get(result.action, "自动处理失败")
        else:
            action_text = "未操作，等待管理员人工复核"

        reasons = "；".join(result.reasons[:3]) or "无"
        text = (
            "【入群审核】%s\n"
            "结论：%s（%d 分）\n"
            "处理：%s\n"
            "摘要：%s\n"
            "理由：%s"
            % (
                req.username or req.member_openid,
                decision_text,
                result.score,
                action_text,
                result.summary or "无",
                reasons,
            )
        )
        # 防止申请人昵称或模型输出里夹带 @ 标签，在群里误 @ 其他人
        return _clip(_NOTIFY_MENTION_RE.sub("", text), 1800)

    def _record(self, req, result):
        _text, _scan_text, qa_text, qa_list = self._collect_text(req)
        local_rule = None
        if result.local_match is not None:
            local_rule = {
                "category": result.local_match.category,
                "keyword": result.local_match.keyword,
            }
        return {
            "group_id": req.group_openid,
            "member_id": req.member_openid,
            "union_openid": req.union_openid,
            "username": req.username,
            "join_request_id": req.join_request_id,
            "apply_at": req.apply_at,
            "apply_source": req.apply_source,
            "invited_by": req.invited_by,
            "is_bot": req.bot,
            "verify_method": req.method,
            "verify_message": req.verify_message,
            "qa_text": qa_text,
            "qa_list": qa_list,
            "risk_tips": _clip(req.risk_tips, 500),
            "mode": self.mode,
            "auto_decline": self.auto_decline,
            "standard_answers_configured": self._standard_count(req.group_openid),
            "standard_answers_matched": self._matched_standard_count(
                qa_list, req.group_openid
            ),
            "standard_answers_source": self._standard_source(req.group_openid),
            "group_standard_answers_mode": self.group_standard_answers_mode,
            "decision": result.decision,
            "llm_decision": result.llm_decision,
            "score": result.score,
            "summary": result.summary,
            "reasons": result.reasons,
            "risk": result.risk,
            "local_rule": local_rule,
            "error": result.error,
            "action": result.action,
            "action_ok": result.action_ok,
            "action_error": result.action_error,
            "notified": result.notified,
            "notify_error": result.notify_error,
        }
