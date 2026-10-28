import json
import logging
import os
import re
import threading
import time
import unicodedata
from dataclasses import dataclass
from datetime import datetime

_log = logging.getLogger(__name__)

DEFAULT_RULES_FILE = "moderation/rules.txt"
DEFAULT_LOG_DIR = "moderation/logs"
DEFAULT_GROUP_RULES_DIR = "moderation/groups"
DEFAULT_WARNING_TEMPLATE = (
    "喵呜～{mention}的消息含有【{category}】内容（触发词：{keyword}），"
    "猫猫已经记录警告啦！请遵守群规，再犯可能会被管理员请出群哦～"
)

_SPACE_RE = re.compile(r"[\s\u200b-\u200f\u202a-\u202e\ufeff]+")
_CATEGORY_RE = re.compile(r"^\[(.+?)\]$")
# 支持「类型=关键词」写法；类型名里不允许出现冒号、斜杠和等号，避免误伤 URL / 正则
_ASSIGN_RE = re.compile(r"^([^=＝:：/\\]+)[=＝](.+)$")
_SPLIT_RE = re.compile(r"[|,，、;；]+")


def normalize_text(text):
    """统一大小写、全半角，并去掉空白和零宽字符，降低简单的绕词成功率。"""
    text = unicodedata.normalize("NFKC", text or "").lower()
    return _SPACE_RE.sub("", text)


@dataclass
class RuleMatch:
    category: str
    keyword: str
    is_regex: bool = False


class Rule:
    __slots__ = ("category", "keyword", "needle", "regex")

    def __init__(self, category, keyword, needle="", regex=None):
        self.category = category
        self.keyword = keyword
        self.needle = needle
        self.regex = regex


class ModerationRules:
    r"""从 txt 文件加载「类型 -> 关键词」规则。

    文件格式示例：
        [广告]
        加群
        加微信|私聊我

        [辱骂]
        傻逼

        广告=刷单,返利
        re:(?:https?://|www\.)
    """

    def __init__(self, path, enabled_categories=None, min_keyword_length=2):
        self.path = path
        if isinstance(enabled_categories, str):
            enabled_categories = [enabled_categories]
        self.enabled_categories = {
            str(item).strip() for item in (enabled_categories or []) if str(item).strip()
        }
        try:
            self.min_keyword_length = max(1, int(min_keyword_length))
        except (TypeError, ValueError):
            self.min_keyword_length = 2
        self.rules = []
        self.loaded_at = None
        self.load()

    def load(self):
        if not self.path or not os.path.isfile(self.path):
            self.rules = []
            self.loaded_at = None
            _log.warning("违禁词规则文件不存在：%s", self.path)
            return 0

        try:
            with open(self.path, "r", encoding="utf-8-sig") as fh:
                lines = fh.readlines()
        except Exception as exc:  # noqa: BLE001
            _log.error("读取违禁词规则失败 %s: %s", self.path, exc)
            # 读取失败时保留旧规则，避免 /reload 意外把检测功能清空
            return len(self.rules)

        rules = []
        current_category = "未分类"
        for lineno, raw in enumerate(lines, 1):
            line = raw.strip()
            if not line or line.startswith("#") or line.startswith("//"):
                continue

            category_match = _CATEGORY_RE.match(line)
            if category_match:
                current_category = category_match.group(1).strip() or "未分类"
                continue

            assign_match = _ASSIGN_RE.match(line)
            if assign_match:
                current_category = assign_match.group(1).strip() or current_category
                line = assign_match.group(2).strip()
                if not line:
                    continue

            # 正则里可能含有 |，所以 re: 开头的行必须整行作为一个规则，不能按分隔符拆
            if line.lower().startswith("re:"):
                items = [line]
            else:
                items = _SPLIT_RE.split(line)

            for item in items:
                item = item.strip()
                if not item:
                    continue
                if item.lower().startswith("re:"):
                    pattern_text = item[3:].strip()
                    if not pattern_text:
                        continue
                    try:
                        compiled = re.compile(pattern_text, re.IGNORECASE)
                    except re.error as exc:
                        _log.warning(
                            "违禁词规则第 %d 行正则无效：%s (%s)", lineno, pattern_text, exc
                        )
                        continue
                    rules.append(Rule(current_category, item, regex=compiled))
                    continue

                needle = normalize_text(item)
                if not needle or len(needle) < self.min_keyword_length:
                    continue
                rules.append(Rule(current_category, item, needle=needle))

        self.rules = rules
        self.loaded_at = datetime.now()
        return len(self.rules)

    def match(self, text):
        if not text or not self.rules:
            return None

        raw = text
        normalized = normalize_text(text)
        for rule in self.rules:
            if self.enabled_categories and rule.category not in self.enabled_categories:
                continue
            if rule.regex is not None:
                if rule.regex.search(raw) or rule.regex.search(normalized):
                    return RuleMatch(rule.category, rule.keyword, True)
            elif rule.needle and rule.needle in normalized:
                return RuleMatch(rule.category, rule.keyword, False)
        return None


class DailyLogger:
    """按天写入 .log（方便阅读）和 .jsonl（方便统计）的通用日志器。"""

    prefix = "log"

    def __init__(self, directory, max_chars=500):
        self.directory = directory
        try:
            self.max_chars = max(50, int(max_chars))
        except (TypeError, ValueError):
            self.max_chars = 500
        self._lock = threading.Lock()
        try:
            os.makedirs(self.directory, exist_ok=True)
        except Exception as exc:  # noqa: BLE001
            _log.error("创建日志目录失败 %s: %s", self.directory, exc)

    def _paths(self, now):
        day = now.strftime("%Y-%m-%d")
        base = "%s-%s" % (self.prefix, day)
        return (
            os.path.join(self.directory, base + ".log"),
            os.path.join(self.directory, base + ".jsonl"),
        )

    def write(self, record):
        now = datetime.now()
        rec = dict(record)
        rec.setdefault("time", now.strftime("%Y-%m-%d %H:%M:%S"))

        # 只截断可能很长的文本字段；结构化字段（如 qa_list）保持原样
        for key in ("content", "verify_message", "qa_text"):
            if key not in rec:
                continue
            text = str(rec.get(key) or "")
            if len(text) > self.max_chars:
                rec[key] = text[: self.max_chars] + "...[已截断]"

        human_line = self._format_human(rec)
        json_line = json.dumps(rec, ensure_ascii=False, separators=(",", ":"))
        text_path, jsonl_path = self._paths(now)

        try:
            with self._lock:
                os.makedirs(self.directory, exist_ok=True)
                with open(text_path, "a", encoding="utf-8") as fh:
                    fh.write(human_line + "\n")
                with open(jsonl_path, "a", encoding="utf-8") as fh:
                    fh.write(json_line + "\n")
        except Exception as exc:  # noqa: BLE001
            _log.error("写入日志失败 %s: %s", self.directory, exc)

    def _format_human(self, rec):
        raise NotImplementedError


class WarningLogger(DailyLogger):
    """把每次检测结果同时写入易读的 .log 和结构化的 .jsonl。"""

    prefix = "warnings"

    @staticmethod
    def _format_human(rec):
        if rec.get("recalled"):
            recall_text = "成功"
        elif rec.get("recall_attempted"):
            recall_error = str(rec.get("recall_error") or "").strip()
            recall_error = recall_error.replace("\r", " ").replace("\n", " ")
            recall_text = "失败" + ((": " + recall_error) if recall_error else "")
        elif rec.get("recall_skipped_reason"):
            recall_skipped = str(rec.get("recall_skipped_reason") or "")
            recall_skipped = recall_skipped.replace("\r", " ").replace("\n", " ")
            recall_text = "跳过" + ((": " + recall_skipped) if recall_skipped else "")
        else:
            recall_text = "未尝试"

        if rec.get("warning_sent"):
            warning_text = "已发送"
        elif rec.get("warning_suppressed"):
            warning_text = "已抑制"
        else:
            warning_text = "未发送"

        content = str(rec.get("content") or "").replace("\r", "\\r").replace("\n", "\\n")
        member_name = rec.get("member_name") or ""
        member_text = rec.get("member_id") or ""
        if member_name:
            member_text = "%s(%s)" % (member_text, member_name)
        member_role = rec.get("member_role") or ""
        if member_role:
            role_text = {
                "member": "普通成员",
                "admin": "管理员",
                "owner": "群主",
            }.get(member_role, member_role)
            member_text = "%s[%s]" % (member_text, role_text)

        group_id = rec.get("group_id") or ""
        channel_id = rec.get("channel_id") or ""
        chat_label = "群" if group_id else "频道"
        chat_id = group_id or channel_id

        return (
            "%s | %s=%s | 成员=%s | 类型=%s | 触发=%s | 撤回=%s | 警告=%s | 内容=%s"
            % (
                rec.get("time", ""),
                chat_label,
                chat_id,
                member_text,
                rec.get("category", ""),
                rec.get("keyword", ""),
                recall_text,
                warning_text,
                content,
            )
        )


class Moderation:
    """违禁词检测、警告文案和冷却状态的统一入口。"""

    def __init__(self, config, base_dir):
        cfg = (config.get("moderation") or config.get("bot", {}).get("moderation") or {})
        self.base_dir = base_dir or os.getcwd()
        self.enabled = bool(cfg.get("enabled", True))
        self.recall_enabled = bool(cfg.get("recall", True))
        self.mention_member = bool(cfg.get("mention_member", True))
        self.cooldown = self._to_float(cfg.get("warn_cooldown_seconds", 30), 30)
        self.warning_template = cfg.get("warning_template") or DEFAULT_WARNING_TEMPLATE

        rules_path = self._resolve(self.base_dir, cfg.get("rules_file") or DEFAULT_RULES_FILE)
        log_dir = self._resolve(self.base_dir, cfg.get("log_dir") or DEFAULT_LOG_DIR)

        self.enabled_categories = cfg.get("enabled_categories") or []
        self.min_keyword_length = cfg.get("min_keyword_length", 2)
        self.rules = self._make_rules(rules_path)
        self.logger = WarningLogger(log_dir, cfg.get("max_log_chars", 500))

        # 每个群可以有自己的规则文件：显式配置优先，其次是 group_rules_dir 下
        # 以「群 OpenID.txt」命名的文件。没配置专属规则的群继续使用全局规则。
        self.group_rules_mode = str(cfg.get("group_rules_mode") or "replace").strip().lower()
        if self.group_rules_mode not in ("replace", "merge"):
            _log.warning("moderation.group_rules_mode 只能是 replace 或 merge，已改用 replace")
            self.group_rules_mode = "replace"
        group_rules = cfg.get("group_rules") or {}
        if not isinstance(group_rules, dict):
            _log.warning("moderation.group_rules 必须是 {群OpenID: 规则文件} 形式的对象，已忽略")
            group_rules = {}
        self.group_rules_config = group_rules
        self.group_rules_dir = self._resolve(
            self.base_dir, cfg.get("group_rules_dir") or DEFAULT_GROUP_RULES_DIR
        )
        self.group_rules = {}
        self._load_group_rules()

        self._last_warn = {}
        self._counts = {}
        self._count_date = datetime.now().strftime("%Y-%m-%d")

    @staticmethod
    def _resolve(base_dir, path):
        if os.path.isabs(path):
            return path
        return os.path.abspath(os.path.join(base_dir or os.getcwd(), path))

    @staticmethod
    def _to_float(value, default):
        try:
            return max(0.0, float(value))
        except (TypeError, ValueError):
            return float(default)

    def _make_rules(self, path):
        return ModerationRules(
            path,
            enabled_categories=self.enabled_categories,
            min_keyword_length=self.min_keyword_length,
        )

    def _load_group_rules(self):
        """重新扫描群专属规则；返回当前有效的群规则数量。"""
        rules_map = {}
        configured = set()

        for raw_group_id, raw_path in self.group_rules_config.items():
            group_id = str(raw_group_id or "").strip()
            if not group_id or not raw_path:
                continue
            configured.add(group_id)
            rules_map[group_id] = self._make_rules(self._resolve(self.base_dir, str(raw_path)))

        if os.path.isdir(self.group_rules_dir):
            try:
                names = sorted(os.listdir(self.group_rules_dir))
            except OSError as exc:
                _log.error("读取群专属规则目录失败 %s: %s", self.group_rules_dir, exc)
                names = []
            for name in names:
                if name.startswith(".") or not name.lower().endswith(".txt"):
                    continue
                if name.lower().startswith("readme"):
                    continue
                group_id = os.path.splitext(name)[0].strip()
                if not group_id or group_id in configured:
                    continue
                rules_map[group_id] = self._make_rules(
                    os.path.join(self.group_rules_dir, name)
                )

        self.group_rules = rules_map
        return self.group_rule_count()

    def group_rule_count(self):
        """返回实际存在规则文件的群专属规则数量。"""
        return sum(
            1
            for rules in self.group_rules.values()
            if rules.path and os.path.isfile(rules.path)
        )

    def _rules_for_group(self, group_id):
        if not group_id:
            return None
        rules = self.group_rules.get(str(group_id))
        if rules is None:
            return None
        # 配置了文件但文件不存在时退回全局规则，避免因为路径写错而完全失去检测
        if not rules.path or not os.path.isfile(rules.path):
            return None
        return rules

    def reload(self):
        count = self.rules.load()
        self._load_group_rules()
        return count

    def find_match(self, text, group_id=None):
        """按群规则查找命中项；不受 moderation.enabled 影响。

        群专属规则存在时按 group_rules_mode 处理：
        - replace（默认）：只用群规则，找不到就是不命中；
        - merge：先查群规则，再查全局规则。
        """
        group_rules = self._rules_for_group(group_id)
        if group_rules is not None:
            match = group_rules.match(text)
            if match is not None or self.group_rules_mode != "merge":
                return match
        return self.rules.match(text)

    def check(self, text, group_id=None):
        if not self.enabled:
            return None
        return self.find_match(text, group_id)

    def _reset_counters_if_needed(self):
        today = datetime.now().strftime("%Y-%m-%d")
        if today != self._count_date:
            self._count_date = today
            self._counts.clear()
            self._last_warn.clear()

    def should_warn(self, group_id, member_id, category):
        self._reset_counters_if_needed()
        if self.cooldown <= 0:
            return True

        key = (group_id, member_id, category)
        now = time.time()
        last = self._last_warn.get(key, 0.0)
        if now - last < self.cooldown:
            return False
        self._last_warn[key] = now
        return True

    def next_count(self, group_id, member_id):
        self._reset_counters_if_needed()
        key = (group_id, member_id)
        self._counts[key] = self._counts.get(key, 0) + 1
        return self._counts[key]

    def warning_text(self, match, member_id, count, member_name="", mention=None):
        if mention is None:
            if self.mention_member and member_id and member_name:
                # 纯文本不支持 @ 标签时展示昵称，避免把 OpenID 暴露给群成员
                mention = "@%s" % member_name
            else:
                mention = "这位朋友"
        mention = str(mention).strip() or "这位朋友"
        if mention[0] in "<@":
            mention += " "
        values = {
            "mention": mention,
            "user_id": member_id or "",
            "category": match.category,
            "keyword": match.keyword,
            "count": count,
        }
        try:
            return self.warning_template.format(**values)
        except Exception:  # noqa: BLE001
            _log.warning("警告文案模板格式有误，已使用默认模板")
            return DEFAULT_WARNING_TEMPLATE.format(**values)

    def write_log(self, **record):
        self.logger.write(record)
