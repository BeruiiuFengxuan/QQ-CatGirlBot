import json
import os

import botpy

from bot.client import CatBot
from bot.knowledge import KnowledgeBase


def load_config(path="config.json"):
    with open(path, encoding="utf-8") as fh:
        return json.load(fh)


def main():
    root = os.path.dirname(os.path.abspath(__file__))
    config_path = os.path.join(root, "config.json")

    config = load_config(config_path)
    bot_cfg = config.get("bot", {})

    kb_dir = bot_cfg.get("knowledge_dir", "knowledge")
    if not os.path.isabs(kb_dir):
        kb_dir = os.path.join(root, kb_dir)

    kb = KnowledgeBase(
        kb_dir,
        top_k=bot_cfg.get("knowledge_top_k", 3),
        max_chars=bot_cfg.get("max_context_chars", 4000),
    )
    print("[果娘酱] 知识库已加载 %d 个 txt 文件，目录: %s" % (len(kb.docs), kb_dir))

    intents = botpy.Intents(public_messages=True)
    # GROUP_MEMBER_EVENT (1 << 24)：群成员加入/退出、入群申请事件。
    # 当前 qq-botpy 版本的 Intents 还没有这个开关，直接补上对应的位。
    # 默认关闭入群审核时不申请这个权限，避免没开通事件权限的机器人连接失败。
    mod_cfg = config.get("moderation") or config.get("bot", {}).get("moderation") or {}
    join_review_cfg = mod_cfg.get("join_review") or {}
    if join_review_cfg.get("enabled", False) and join_review_cfg.get(
        "request_member_intent", True
    ):
        intents.value |= 1 << 24

    client = CatBot(config=config, kb=kb, base_dir=root, intents=intents)
    moderation = client.moderation
    print(
        "[果娘酱] 违禁词规则已加载 %d 条，规则文件: %s"
        % (len(moderation.rules.rules), moderation.rules.path)
    )
    group_rule_count = moderation.group_rule_count()
    if group_rule_count:
        print(
            "[果娘酱] 群专属规则已加载 %d 个群，目录: %s"
            % (group_rule_count, moderation.group_rules_dir)
        )
    print("[果娘酱] 警告日志目录: %s" % moderation.logger.directory)

    join_review = client.join_review
    print(
        "[果娘酱] 入群申请审核: %s（模式: %s）"
        % ("开启" if join_review.enabled else "关闭", join_review.mode)
    )
    if join_review.enabled:
        print("[果娘酱] 入群审核日志目录: %s" % join_review.logger.directory)
        print(
            "[果娘酱] 入群审核标准答案: 全局 %d 条，群专属 %d 个群；自动拒绝: %s"
            % (
                len(join_review.standard_answers),
                join_review.active_group_standard_count,
                "开启" if join_review.auto_decline else "关闭（不适合时转人工）",
            )
        )
        if join_review.placeholder_group_standard_ids:
            print(
                "[果娘酱] 注意: group_standard_answers 里还有 %d 个未替换的占位群（%s），"
                "请替换成真实群 OpenID 或删除"
                % (
                    len(join_review.placeholder_group_standard_ids),
                    "、".join(join_review.placeholder_group_standard_ids),
                )
            )
        print("[果娘酱] 提示：入群审核需要机器人是群管理员，且开放平台已开通群成员事件权限")

    client.run(appid=config["qq"]["appid"], secret=config["qq"]["secret"])


if __name__ == "__main__":
    main()
