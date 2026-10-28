这个文件夹用来放「单个群聊专属」的违禁词规则。

用法：
1. 在目标群里 @机器人 发送 /groupid，复制返回的群 OpenID。
2. 在本文件夹新建一个 txt 文件，文件名就是「群 OpenID.txt」。
   例如：30584554AA2BF4E72BD3B8F27A70339D.txt
3. 文件格式和 moderation/rules.txt 完全一样：
       [广告]
       加群
       加微信|私聊我

       [辱骂]
       傻逼

       re:(?:https?://|www\.)

4. 改完后在群里 @机器人 发送 /reload 热更新。

规则优先级：
- 群专属规则文件存在时，默认「替换」全局规则（group_rules_mode=replace）；
  只有该群文件里的规则生效。
- 如果希望「群规则 + 全局规则」同时生效，把 config.json 里的
  moderation.group_rules_mode 改成 "merge"。
- 没有专属规则文件的群，继续使用 moderation/rules.txt 的全局规则。
- 也可以在 config.json 的 moderation.group_rules 里显式指定：
      "group_rules": {
        "群OpenID": "moderation/groups/另一个文件名.txt"
      }

注意：
- 文件名必须严格等于群 OpenID，区分大小写。
- 这个 README 不会被当成规则加载；但请不要把其他说明文件命名为 *.txt，
  除非你确实想让文件名成为群 OpenID。
