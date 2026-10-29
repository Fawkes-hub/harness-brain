---
name: brain
description: 跨主机 / 跨 harness 的共享长期记忆库。当需要用户长期信息(画像、偏好、环境、机器、项目来龙去脉)、当用户说「你忘了」「上次说过」「记住」、或当出现可复用的新事实/踩坑经验时使用。包含检索、写入、合并的完整协议。
---

# brain —— 共享记忆库

一个纯 Markdown 的知识库,DSH / Octop / Pi / Claude Code / Codex 共用同一份。
**任何 agent 只要会读文件或跑 shell,就能读写它。**

## 位置

- 库根:由 `BRAIN_ROOT` 决定,未设置时按命令行位置推断。
- 服务端部署在服务器上的某个目录(如 `/srv/brain`);其他机器通过 HTTP 客户端访问,
  或用 SMB/NFS 挂载后把 `BRAIN_ROOT` 指向挂载点(如 `/Volumes/<share>/brain`)。
- 统一入口是 `bin/brain`(在 PATH 里时直接 `brain ...`)。

## 会话开始时(必做一次)

```bash
brain brief
```

一次调用就能拿到:规范(rules.md)+ 事实索引 + 主机笔记 + 尚未合并的新记忆。
如果 `brief` 内容与当前任务无关,不要复述,只把它当背景。**不要在没读的情况下声称"我记得"。**

## 需要具体信息时

```bash
brain search 代理            # 正则检索全库,带 文件:行号
brain show <库内文件>      # 读全文
```

规则:**先 search,再回答**。库里有就以库里为准;库里没有就说不知道并去查证。
库里内容有过期迹象(日期旧、与现场冲突)时,**以现场实测为准,并立刻修正库**。

## 什么时候写(重要)

满足任一条就写:

1. 用户给出**新的稳定事实**:账号归属、路径、端口、硬件、网络、偏好、称呼、禁忌。
2. 用户**纠正**了之前的说法 → 先 `search` 找到旧条目,再写一条 `-k fact` 说明"更正:原 X 现为 Y"。
3. 你**踩坑并解决**了一个非显而易见的问题 → `-k lesson`,写清症状 + 原因 + 解法。
4. 某个 **harness / 版本的差异**:同样的操作在 DSH、Octop、Pi 上表现不同 → `-k host`。
5. 项目状态发生实质变化(完成了什么、卡在哪、下一步) → `-k project -t <项目名>`。

**不写**:会话流水、临时中间结果、能从代码/文档直接查到的东西、凭据明文。

## 怎么写

```bash
brain remember "内容,一句话说清结论" -k fact -t network -t proxy
```

- `-k` 选类型:`fact` `rule` `pref` `lesson` `host` `project` `idea` `secret-ref`
- `-t` 是标签,**第一个标签决定归档主题**(v1 落在 `facts/<标签>.md`;v2 落在 `views/topics/<标签>.md`)
- 写成**可独立阅读的结论句**,不要写"见上文"。
- 一条只讲一件事;多条就多次调用。
- 敏感值:不写密码/token 明文,写「凭据存放位置」和「怎么取」。

写入由 CLI 落到服务端(在线:一次 POST;离线:本地 outbox 排队后重放),**你不需要关心落盘细节**。
v1 的实现在服务端 inbox 里追加;v2 起改为 `POST /v1/remember`,命令不变。

## 维护(只在 NAS 上做)

```bash
brain consolidate            # dry-run 看会合并到哪
brain consolidate --apply    # 真正合并 + 归档 + 重建索引
brain sync -m "..." --push   # git 提交并推送备份
```

建议每天定时跑一次(见 `tools/cron.example`)。

## 边界

- 库是**共享的**:写进去的内容所有主机所有 harness 都能看到。不写私人闲聊、不写别人的隐私。
- 冲突时不要删别人的条目,追加一条更正并标注日期,让历史可见。
- `inbox/archive/` 是只读历史,不要改。
