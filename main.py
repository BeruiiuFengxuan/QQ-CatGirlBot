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

    client = CatBot(config=config, kb=kb, intents=intents)
    client.run(appid=config["qq"]["appid"], secret=config["qq"]["secret"])


if __name__ == "__main__":
    main()
