# 果娘酱 · QQ AI 机器人

一只以「果娘酱」猫娘人设活动的 QQ 机器人，基于腾讯官方 Python SDK `qq-botpy` 实现，支持群聊 @ 消息、频道 @ 消息、单聊（C2C）：

- **有人 @ 它提问** → 机器人去 `knowledge/` 里的 txt 文件检索相关资料，交给配置的**自定义大模型**（默认 Agnes AI）理解后回答。
- **资料里找不到答案** → 用猫猫语气人性化地说「不知道 / 不太清楚」，绝不编造。
- **普通聊天** → 所有回复都带果娘酱的猫娘口吻（喵呜～、贴贴～、呜呜、捏…）。
- **记忆** → 按「群 + 用户」记住近期对话，**每 12 小时自动清空**，对话更连贯。
- **发送前自审** → 每条回复先过一遍内容审核，把违规 / 敏感内容自动改写成安全、委婉的猫娘话术，降低封号风险。
- **违禁内容检测** → 群成员发言命中 `moderation/rules.txt` 里的类型 / 关键词时，自动撤回（需机器人为群管理员）并回复警告，同时把「哪个成员、哪句话、什么类型」写入 `moderation/logs/`。
- **每个群不同规则** → 支持按群 OpenID 配置专属关键词文件，互不干扰；没有专属规则的群继续用全局规则，也可以选择「群规则 + 全局规则」合并生效。
- **入群申请自动审核（默认关闭）** → 机器人是群管理员且群开启「管理员审核问答 / 验证消息」时，可读取申请人的入群问答，按自定义标准答案（支持每个群单独配置）和大模型评分，并可选自动通过；模型认为不适合入群时默认转人工处理，不自动拒绝。
- **识图 / 视频** → 能识别成员发的图片（图像理解，图片会转 base64 喂给模型）；视频会识别到并接话（真正看懂视频内容需接入视频模型）。

---

## 1. 在 q.qq.com 创建机器人

1. 打开 <https://q.qq.com> → 机器人/应用管理 → 创建机器人。
2. 开发 → 开发设置：复制 **AppID**，并生成 **AppSecret**。
3. 功能配置里**开启「群聊 @ 消息」**（以及你想要的 C2C / 频道）。
4. 如需「普通聊天也回复」（不只在 @ 时回复），要在平台开启**「接收所有消息」**，并把 `config.json` 里的 `bot.reply_all_messages` 改为 `true`。设为 `false` 时只回复 @机器人 的消息，普通消息仅做违禁词检测。
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
    "model": "agnes-2.0-flash",        // 本项目默认 Agnes AI；也可换 agnes-image-2.1-flash（识图）等
    "temperature": 0.8,
    "max_tokens": 800,
    "timeout": 60
  },
  "bot": {
    "knowledge_dir": "knowledge",   // txt 知识库目录
    "knowledge_top_k": 3,           // 一次最多取几个最相关文件
    "max_context_chars": 4000,      // 喂给模型的资料总字数上限
    "reply_all_messages": false     // true=普通聊天也回复（需平台开启接收所有消息）
  },
  "moderation": {
    "enabled": true,                // 是否启用违禁内容检测
    "rules_file": "moderation/rules.txt", // 违禁词规则 txt
    "log_dir": "moderation/logs",   // 警告记录保存目录
    "recall": true,                 // 是否尝试撤回违规消息（群管理员才可撤回他人消息）
    "warn_cooldown_seconds": 30,    // 同一成员同一类型的警告冷却时间
    "max_log_chars": 500,           // 每条日志最多保存多少字原消息
    "min_keyword_length": 2,        // 关键词最少字符数，防止单字误判
    "mention_member": true,         // 警告时是否 @ 违规成员
    "enabled_categories": [],       // 只启用某些类型，如 ["广告","辱骂"]；空数组=全部
    "warning_template": "喵呜～{mention}的消息含有【{category}】内容（触发词：{keyword}），猫猫已经记录警告啦！请遵守群规，再犯可能会被管理员请出群哦～",
    "group_rules_mode": "replace",  // replace=群专属规则替换全局；merge=群规则+全局
    "group_rules_dir": "moderation/groups", // 群专属规则目录，文件名=群OpenID.txt
    "group_rules": {},              // 显式指定：{"群OpenID": "规则文件路径"}
    "join_review": {                // 入群申请审核（默认关闭）
      "enabled": false,
      "request_member_intent": true, // 是否申请群成员事件权限；连接失败时可尝试改为 false
      "mode": "suggest",            // suggest=只记录建议；approve_only=只自动通过合格者；auto=自动通过合格者，不适合的默认转人工
      "approve_score": 70,          // 自动通过所需的最低分（0-100）
      "auto_decline": false,        // 是否允许自动拒绝；默认 false，不适合时只记录并交给管理员
      "standard_answers": [         // 全局入群问答标准答案，支持对象或数组格式
        {"question": "为什么想加群？", "answer": "喜欢 Minecraft，想和大家一起玩"} // 示例，请替换
      ],
      "group_standard_answers_mode": "replace", // replace=群配置替换全局；merge=群配置+全局
      "group_standard_answers": {   // 群专属标准答案，示例占位，请替换成真实群 OpenID 或删除
        "请替换成真实群OpenID": [
          {"question": "为什么想加群？", "answer": "喜欢 Minecraft，想一起玩生存/建筑，会遵守群规"}
        ]
      },
      "criteria": "1. 回答是否认真、完整，是否明显敷衍、复制粘贴或答非所问。\n2. 是否出现广告、辱骂、涉黄、引战、开盒、诈骗、拉人等风险内容。\n3. 入群意图是否明确、友善，是否符合本群的主题和交流氛围。\n4. 如果回答信息不足，应倾向于「人工复核」，不要武断通过。", // 可直接修改；留空回退内置标准
      "reject_reason": "抱歉喵，入群验证没有通过，请认真回答管理员的问题后重新申请～",
      "decline_blacklist": false,   // auto_decline=true 时，自动拒绝是否同时拉黑
      "notify_group_openid": "",    // 可选：发到指定群；填 "same" 发到申请所在群；留空不发
      "max_answer_chars": 2000,     // 每个回答最多多少字送入模型
      "max_log_chars": 1000,        // 日志里问答文本最多保留多少字
      "temperature": 0.2,
      "max_tokens": 500
    }
  }
}
```

`config.json` 里已经预填了一套示例内容：`join_review.criteria` 的默认审核标准、一条全局示例标准答案，以及一个 `请替换成真实群OpenID` 的群专属占位条目。这些示例不会自动适配你的群，启用入群审核前请替换成真实问题和答案；如果暂时不需要群专属配置，把占位条目删掉或改成 `{}` 即可。`criteria` 删成空字符串会回退到代码内置的同一份标准。入群审核整体仍然默认关闭。

`llm.base_url` 支持任意 OpenAI 兼容接口，例如：

- DeepSeek：`https://api.deepseek.com/v1`
- 混元 / 腾讯云：按官方文档填
- 本地 Ollama：`http://127.0.0.1:11434/v1`（模型名如 `qwen2.5`）

## 4. 准备知识库

把 `.txt` 文件丢进 `knowledge/` 目录，内容就是纯文本。机器人被 @ 时按关键词匹配最相关的几个文件，
连同问题一起交给大模型，由模型总结成自然回答。改完文件后 @ 机器人 发 `/reload` 即可热刷新。

## 5. 违禁内容检测与警告

规则写在 `moderation/rules.txt`，格式如下：

```text
# 以 # 开头的是注释
[广告]
加群
加微信|私聊我
代练

[辱骂]
傻逼
你妈

# 也可以一行定义一个类型
广告=刷单,返利

# 需要正则时用 re: 开头（不区分大小写）
re:(?:https?://|www\.)
```

- `[类型名]` 开始一个类型，下面的关键词都属于它；警告文案里的 `{category}` 就是这里的类型名。
- 一行可以写一个关键词，也可以用 `|`、逗号、顿号分隔多个。
- `类型名=词1,词2` 是简写方式。
- `re:正则表达式` 支持正则匹配，适合检测链接、手机号等；正则必须单独占一行（因为正则里可能包含 `|`）。
- 规则按文件顺序匹配，越靠前越优先；关键词默认至少 2 个字符，可在 `moderation.min_keyword_length` 调整。
- 改完规则后，在群里 @机器人 发送 `/reload` 即可热更新，不需要重启。

检测到违规内容后：

1. 如果 `moderation.recall` 为 `true`，机器人会尝试撤回该成员的消息。**QQ 官方接口要求机器人是群管理员才能撤回普通成员的消息**；即使机器人是管理员，也**不能撤回群主或其他管理员**的消息（接口会返回 `40062003 无操作权限`）。如果机器人只是普通成员，只能撤回自己发送的消息。撤回失败时警告和日志仍会正常执行。
2. 机器人回复一条警告（默认会 @ 违规成员），文案可在 `moderation.warning_template` 中修改。可用占位符：`{mention}`、`{user_id}`、`{category}`、`{keyword}`、`{count}`。群聊的纯文本消息不支持 `<@openid>`，直接发会显示成一串 OpenID；所以群聊警告会用 markdown 消息里的 `<qqbot-at-user id="成员OpenID" />` 来 @ 成员，显示为正常的 @ 标签。如果机器人没有 markdown 权限、发送失败，会自动退回 `@昵称` 纯文本。
3. 记录写入 `moderation/logs/`，每天生成两个文件：
   - `warnings-YYYY-MM-DD.log`：方便直接打开阅读；
   - `warnings-YYYY-MM-DD.jsonl`：结构化数据，方便统计。
   
   记录包含时间、群 OpenID、成员 OpenID、消息 ID、违规类型、触发关键词、原消息内容、撤回是否成功、警告是否发送等。群消息事件有 `author.username` 时会记录昵称；拿不到昵称时警告里显示「这位朋友」，不会把 OpenID 当作 @ 文案发到群里。
4. 同一成员、同一类型在 `warn_cooldown_seconds` 秒内重复违规时，消息仍会被撤回并记录，但不会重复刷屏警告。

> 要检测群里**所有**成员的聊天，需要**群主**在手机 QQ 的群机器人权限设置里开启「接收全量消息」（添加机器人时的「设置权限」里也可以设置）。这不是开放平台的普通接口权限；代码里的 `public_messages=True` 已经申请了 `GROUP_AND_C2C_EVENT (1 << 25)` intent。未开启时机器人只能收到 @ 它的 `GROUP_AT_MESSAGE_CREATE` 事件，也就只能检测这些消息。`bot.reply_all_messages` 只控制机器人是否陪聊，不影响检测本身。

### 5.1 给每个群配置不同关键词

1. 在目标群里 @机器人 发送 `/groupid`，复制返回的群 OpenID。
2. 在 `moderation/groups/` 下新建一个 txt 文件，文件名就是「群 OpenID.txt」，例如：
   `moderation/groups/30584554AA2BF4E72BD3B8F27A70339D.txt`
3. 文件格式和 `moderation/rules.txt` 完全一样：

   ```text
   [广告]
   加群
   加微信|私聊我

   [辱骂]
   傻逼

   re:(?:https?://|www\.)
   ```

4. 改完后在群里 @机器人 发送 `/reload` 热更新。

规则优先级由 `moderation.group_rules_mode` 控制：

- `replace`（默认）：有专属规则文件的群**只用**专属规则，不再匹配全局规则。
- `merge`：先匹配群专属规则，没命中再匹配全局规则。
- 没有专属规则文件的群，始终使用全局 `moderation/rules.txt`。
- 也可以在 `moderation.group_rules` 里显式指定：
  ```jsonc
  "group_rules": {
    "30584554AA2BF4E72BD3B8F27A70339D": "moderation/groups/这个群.txt"
  }
  ```
  显式配置优先于目录扫描；如果配置的文件不存在，会回退到全局规则，避免因为路径写错而完全失去检测。

> `/reload` 会重新读取 `moderation/groups/` 下的文件和已有映射对应的文件；如果修改了 `group_rules_mode`、`group_rules` 映射或 `config.json` 里的其他配置，需要重启机器人。

### 5.2 新成员入群申请自动审核（默认关闭）

QQ 官方 API 提供 `GROUP_JOIN_REQUEST` 事件：当**机器人是群管理员**，且群使用「管理员审核问答」或「验证消息」时，申请人填写的验证消息 / 问答会随事件下发。机器人可以：

1. 读取管理员设置的入群问题和申请人的回答；
2. 先用本群的关键词 / 正则规则做一次本地风险筛查；
3. 再把问答交给大模型，按 `standard_answers` 里的标准答案和 `criteria` 里的补充标准评分；
4. 根据 `mode` 自动通过合格申请，或者只写日志留给管理员判断。**默认不会自动拒绝**，模型认为不适合入群时也只是记录并转人工。

开启前提：

1. 把 `moderation.join_review.enabled` 改为 `true`；
2. 机器人必须是目标群的**管理员**；
3. 群设置里开启「管理员审核问答」或「验证消息」；
4. 开放平台允许群成员事件（`GROUP_MEMBER_EVENT`，`1 << 24`），否则平台可能不下发入群申请事件。`join_review.request_member_intent` 默认 `true` 会自动申请这个 intent；如果你的机器人没有该权限、开启后反而连接失败，可以改成 `false` 再试（部分平台版本会把入群申请放在 `public_messages` 里下发）；
5. 重启机器人。`/reload` 只刷新关键词规则，不会重新读取 `config.json`。

三种模式：

| mode | 行为 |
| --- | --- |
| `suggest` | 只审核、写日志，可选发群通知，不调用审批接口。默认，最安全。 |
| `approve_only` | 评分达标且没有风险的申请自动通过；其他留人工复核。 |
| `auto` | 达标的申请自动通过；模型认为不适合时默认也转人工，只有 `auto_decline=true` 才会自动拒绝。 |

#### 标准答案

QQ 群设置里的「管理员审核问答」通常只能设置**一道问题**，所以一般只需要配置一条标准答案：

```jsonc
"standard_answers": [
  {"question": "为什么想加群？", "answer": "喜欢 Minecraft，想和大家一起玩"}
]
```

也可以写成对象：

```jsonc
"standard_answers": {
  "为什么想加群？": "喜欢 Minecraft，想和大家一起玩"
}
```

官方 `GROUP_JOIN_REQUEST` 事件里的 `review_qa_list` 本身是数组，所以代码兼容 0~N 道问题；如果你的群只有一道题，就只写一条。多条标准答案主要用于：不同群使用不同入群问题、以后平台下发多道题，或者同一个机器人服务多个群。

- 最多支持 20 条标准答案；问题文本会做全半角、空格、标点归一化后匹配，措辞略有差异时也会尝试包含匹配。
- 对应问题的标准答案会一起发给模型，让模型按语义判断，不要求申请人逐字复制；同义表达、合理补充都算正确。
- 标准答案只用于评分，会随问答一起发送给你配置的大模型服务商；不会发给申请人。普通审核日志只记录「匹配到几条标准答案」，不会单独保存标准答案配置；模型生成的理由中可能提及标准答案内容。
- 如果配置了标准答案，但本次申请的问题一个都匹配不上，会自动阻止自动通过，转人工复核。
- 验证消息、额外审核要求等可以继续写在 `criteria` 里。

##### 按群配置标准答案

如果不同群的入群问题或标准答案不一样，先在目标群发 `/groupid` 拿到群 OpenID，然后这样配置：

```jsonc
"group_standard_answers_mode": "replace",
"group_standard_answers": {
  "群A的OpenID": [
    {"question": "为什么想加群？", "answer": "喜欢 Minecraft，想一起玩"}
  ],
  "群B的OpenID": {
    "群B的问题": "群B的标准答案"
  }
}
```

- `replace`（默认）：群专属配置存在时，**只用该群的标准答案**，全局 `standard_answers` 不参与。
- `merge`：群专属配置优先；同一个问题以群配置为准，其他问题继续使用全局标准答案。
- 没有配置群专属标准答案的群，继续使用全局 `standard_answers`。
- 把某个群显式配置成 `[]` 或 `{}`，可以让该群不使用标准答案（即使全局有配置）。
- `config.json` 默认带一个 `请替换成真实群OpenID` 的占位条目，请替换成真实群 OpenID 或删除；未替换的占位条目不会匹配任何真实群。
- 修改 `config.json` 后需要重启机器人，`/reload` 不会重新读取这里的配置。

#### 关于自动拒绝

`auto_decline` 默认 `false`：`auto` 模式下模型给出 `decline`（不建议通过）时，机器人**不会调用拒绝接口**，只会写日志 / 发通知，把申请留给群管理员处理。只有显式改成 `true` 才会自动拒绝；`reject_reason` 和 `decline_blacklist` 也只在自动拒绝时生效。

如果希望彻底不操作审批接口，用 `suggest`；如果只希望自动通过、不希望任何自动拒绝，用 `approve_only` 或保持 `auto` + `auto_decline=false`。

自动通过需要同时满足：大模型结论为 `approve`、评分不低于 `approve_score`、没有命中本地群规、没有平台安全提示（`risk_tips`）、申请人不是机器人账号。以下情况**不会**自动通过：

- 本地关键词 / 正则命中（只会转为人工复核，不会仅凭关键词自动拒绝）；
- 平台返回安全提示；
- 申请人被标记为机器人；
- 没有验证消息也没有问答，信息不足；
- 配置了标准答案，但本次问答没有匹配到任何标准答案；
- 大模型调用失败或返回格式异常。

审核结果写入 `moderation/logs/`，每天生成：

- `join-reviews-YYYY-MM-DD.log`：方便直接阅读；
- `join-reviews-YYYY-MM-DD.jsonl`：结构化数据。

记录包含申请人 OpenID / 昵称、申请 ID、申请来源、验证方式、验证消息或问答、评分、结论、理由、本地群规命中情况、标准答案匹配数量、是否自动审批成功等。`notify_group_openid` 非空时，还会把审核结果发到指定群：可以填一个管理员群的 OpenID，也可以填 `"same"` 发到申请所在群（注意该群成员都能看到审核结果）。

> 审核发生在「申请入群」阶段，在申请人真正进群之前，所以比进群后再踢人更安全。QQ 官方 API 目前没有提供让机器人对已进群成员强制答题的接口；如果需要进群后的流程，请使用 QQ 群自身的入群问答设置。
>
> 开启后，申请人填写的验证消息 / 问答以及管理员配置的标准答案会发送给你在 `llm.base_url` 配置的大模型服务商；入群审核日志也会包含申请内容，属于个人信息。请确认你能接受对应的数据处理方式，并妥善保管日志，不要随意公开。

## 6. 运行

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
│   ├── join_review.py   # 入群申请问答审核、自动审批与独立日志
│   ├── llm.py           # 通用 OpenAI 兼容大模型调用
│   ├── knowledge.py     # txt 知识库加载与关键词检索
│   ├── moderation.py    # 违禁词规则加载、匹配、警告与日志
│   └── prompts.py       # 果娘酱人设 & 知识库回答规则
├── knowledge/           # 你的 txt 知识库
│   └── example_faq.txt
└── moderation/          # 违禁内容检测 / 入群审核
    ├── rules.txt        # 全局规则：类型 + 关键词 / 正则
    ├── groups/          # 群专属规则：<群OpenID>.txt
    └── logs/            # 每天的警告 / 入群审核记录（自动生成）
```

## 指令

- `/reload` 重新读取 `knowledge/` 里的所有 txt，并重新加载 `moderation/rules.txt` 和 `moderation/groups/` 下的群专属规则。
- `/groupid` 查看当前群的 OpenID（配置群专属关键词规则 / 入群标准答案时用）。
- `/help` 显示帮助。

## 说明 / 限制

- 群聊文本消息单条上限约 2000 字，过长回复会自动分段发送。
- 被动回复（@ 触发）需在事件窗口内完成；若大模型较慢导致超时，会自动退回「主动消息」方式补发（受主动消息频次限制）。
- 开启全量消息后，如果只想检测违规而不陪聊，把 `reply_all_messages` 设为 `false`；普通消息仍然会检测、警告和撤回，但不会调用大模型回复，**@机器人 的消息仍然会正常回复**。设为 `true` 会回复群里每一条消息，注意平台消息频次、token 消耗以及大模型 429 限流。
- 开启全量消息后，QQ 可能把 @机器人 的消息也通过 `GROUP_MESSAGE_CREATE`（而不是 `GROUP_AT_MESSAGE_CREATE`）推送；代码会根据 `mentions[].is_you` / 机器人 OpenID 判断是否被 @，并对重复事件去重。
- **记忆**为内存存储，机器人重启即丢失；按 12 小时 TTL 自动过期，并有后台任务定期清理。
- **发送前自审**会额外调用一次大模型，使每次回复的耗时与 token 消耗大约翻倍（可在 `config.json` 的 `bot.review_enabled` 关闭）。如果服务商返回 `429 Too Many Requests`，机器人会暂停自审 60 秒并直接发送原回复，避免每条消息都重复报错；频繁 429 时请检查服务商限额或关闭自审。
- **违禁内容检测**使用本地关键词 / 正则匹配，不调用大模型，不会额外消耗 token；但关键词列表需要你自己维护，建议先用较保守的词表，观察日志后再调整。
- **撤回群成员消息**需要机器人在群里拥有管理员权限，且消息发送不超过 2 分钟；权限不足时不会撤回成功，但警告和日志不受影响。
- **入群申请审核**需要机器人是群管理员，并且开放平台已授予群成员事件权限；平台不下发 `GROUP_JOIN_REQUEST` 时机器人无法审核。审核会额外调用一次大模型（仅在收到申请时），默认关闭。
- **入群审核只在申请阶段生效**，不会对已经进群的成员强制答题；自动通过/拒绝还会受平台审批接口权限限制，失败时会记录错误并留待人工处理。
- **警告 / 入群审核日志包含成员 OpenID、原消息或申请问答内容**，属于群内数据和个人信息，请妥善保管，不要随意公开。
- **识图**：图片会先下载成 base64 再发给模型，因此对模型服务器的出网可达性无依赖；若图片下载失败会退回图片 URL 让模型自行拉取。
- **视频**目前仅做到「识别到并接话」；要真正理解视频内容，需把视频接入 Agnes 的视频模型（如 `agnes-video-2.5-flash`）。
- 换电脑运行需把新机器的**公网 IP** 加入 QQ 开放平台的 IP 白名单，否则机器人无法连接；`start.bat` 会自动检测/安装 Python 3.11 并安装依赖。
- `config.json` 含 AppID / AppSecret 与 LLM API Key，分享给他人时会一并交出凭证，请勿公开发布。
