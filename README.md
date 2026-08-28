# 果娘酱 · QQ AI 机器人

一只以「果娘酱」猫娘人设活动的 QQ 机器人，基于腾讯官方 Python SDK `qq-botpy` 实现，支持群聊 @ 消息、频道 @ 消息、单聊（C2C）：

- **有人 @ 它提问** → 机器人去 `knowledge/` 里的 txt 文件检索相关资料，交给配置的**自定义大模型**（默认 Agnes AI）理解后回答。
- **资料里找不到答案** → 用猫猫语气人性化地说「不知道 / 不太清楚」，绝不编造。
- **普通聊天** → 所有回复都带果娘酱的猫娘口吻（喵呜～、贴贴～、呜呜、捏…）。
- **记忆** → 按「群 + 用户」记住近期对话，**每 12 小时自动清空**，对话更连贯。
- **发送前自审** → 每条回复先过一遍内容审核，把违规 / 敏感内容自动改写成安全、委婉的猫娘话术，降低封号风险。
- **识图 / 视频** → 能识别成员发的图片（图像理解，图片会转 base64 喂给模型）；视频会识别到并接话（真正看懂视频内容需接入视频模型）。

---

## 1. 在 q.qq.com 创建机器人

1. 打开 <https://q.qq.com> → 机器人/应用管理 → 创建机器人。
2. 开发 → 开发设置：复制 **AppID**，并生成 **AppSecret**。
3. 功能配置里**开启「群聊 @ 消息」**（以及你想要的 C2C / 频道）。
4. 如需「普通聊天也回复」（不只在 @ 时回复），要在平台开启**「接收所有消息」**，并把 `config.json` 里的 `bot.reply_all_messages` 改为 `true`。
5. 开发设置里填写你的**服务器公网 IP 白名单**（本地开发可用 `curl ipconfig.io` 查公网 IP）。

## 2. 安装依赖（可选）

> 直接**双击 `start.bat`** 即可：它会自动检测 / 安装 Python 3.11 并执行 `pip install -r requirements.txt`，一般无需手动操作。下面仅当你想自己用 `python main.py` 运行时才需要。

```bash
cd qq-cat-bot
pip install -r requirements.txt
```

> 中文分词用 `jieba`（可选，装不上也能跑，会退化为字级 2-gram 匹配）。

## 3. 配置

编辑 `config.json`：

```jsonc
{
  "qq": { "appid": "你的AppID", "secret": "你的AppSecret" },
  "llm": {
    "base_url": "https://api.openai.com/v1",     // 可换成 DeepSeek / 混元 / 本地 Ollama 等任意 OpenAI 兼容地址
    "api_key": "你的APIKey",
    "model": "agnes-2.0-flash",        // 本项目默认 Agnes AI
    "temperature": 0.8,
    "max_tokens": 800,
    "timeout": 60
  },
  "bot": {
    "knowledge_dir": "knowledge",   // txt 知识库目录
    "knowledge_top_k": 3,           // 一次最多取几个最相关文件
    "max_context_chars": 4000,      // 喂给模型的资料总字数上限
    "reply_all_messages": false     // true=普通聊天也回复（需平台开启接收所有消息）
  }
}
```

`llm.base_url` 支持任意 OpenAI 兼容接口，例如：

- DeepSeek：`https://api.deepseek.com/v1`
- 混元 / 腾讯云：按官方文档填
- 本地 Ollama：`http://127.0.0.1:11434/v1`（模型名如 `qwen2.5`）

## 4. 准备知识库

把 `.txt` 文件丢进 `knowledge/` 目录，内容就是纯文本。机器人被 @ 时按关键词匹配最相关的几个文件，
连同问题一起交给大模型，由模型总结成自然回答。改完文件后 @ 机器人 发 `/reload` 即可热刷新。

## 5. 运行

**方式一（推荐）：双击 `start.bat`** —— 自动装好 Python 与依赖并启动，窗口常驻便于看日志；关掉窗口即停止。

**方式二：手动运行**（需先装好依赖，见第 2 节）：

```bash
python main.py
```

看到 `robot xxx on_ready!` 即连接成功。去群里 @ 它试试：「@果娘酱 群规是什么呀」。

---

## 目录结构

```
qq-cat-bot/
├── main.py              # 入口
├── config.json          # 配置（appid/secret/大模型/知识库）
├── requirements.txt
├── bot/
│   ├── client.py        # QQ 机器人客户端（事件处理、回复、分段发送）
│   ├── llm.py           # 通用 OpenAI 兼容大模型调用
│   ├── knowledge.py     # txt 知识库加载与关键词检索
│   └── prompts.py       # 果娘酱人设 & 知识库回答规则
└── knowledge/           # 你的 txt 知识库
    └── example_faq.txt
```

## 指令

- `/reload` 重新读取 `knowledge/` 里的所有 txt。
- `/help` 显示帮助。

## 说明 / 限制

- 群聊文本消息单条上限约 2000 字，过长回复会自动分段发送。
- 被动回复（@ 触发）需在事件窗口内完成；若大模型较慢导致超时，会自动退回「主动消息」方式补发（受主动消息频次限制）。
- `reply_all_messages=true` 会回复群里每一条消息，注意平台主动消息频次与 token 消耗。
- **记忆**为内存存储，机器人重启即丢失；按 12 小时 TTL 自动过期，并有后台任务定期清理。
- **发送前自审**会额外调用一次大模型，使每次回复的耗时与 token 消耗大约翻倍（可在 `config.json` 的 `bot.review_enabled` 关闭）。
- **识图**：图片会先下载成 base64 再发给模型，因此对模型服务器的出网可达性无依赖；若图片下载失败会退回图片 URL 让模型自行拉取。
- **视频**目前仅做到「识别到并接话」；要真正理解视频内容，需把视频接入 Agnes 的视频模型。
- 换电脑运行需把新机器的**公网 IP** 加入 QQ 开放平台的 IP 白名单，否则机器人无法连接；`start.bat` 会自动检测/安装 Python 3.11 并安装依赖。
- `config.json` 含 AppID / AppSecret 与 LLM API Key，分享给他人时会一并交出凭证，请勿公开发布。
