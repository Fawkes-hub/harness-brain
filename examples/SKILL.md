---
name: brain
description: 跨主机 / 跨 harness 的共享长期记忆库。当需要用户长期信息(画像、偏好、环境、机器、项目来龙去脉)、当用户说「你忘了」「上次说过」「记住」、或当出现可复用的新事实/踩坑经验时使用。包含检索、写入、更正的完整协议。
---

# brain —— 共享记忆库

一个纯 Markdown 的知识库，Claude Code / Codex / Pi / Octop 等不同 harness 共用同一份。
**任何 agent 只要会跑 shell，就能读写它** —— 不需要 SDK，不需要插件。

## 库在哪

- 库根由 `BRAIN_ROOT` 决定；没设时按 `bin/brain` 的位置推断。
- 服务端部署在服务器上的一个目录（如 `/srv/brain`），其他机器通过 HTTP 客户端访问。
- 统一入口是 `bin/brain`（在 PATH 里时直接 `brain …`）。

## 会话开始时（必做一次）

```bash
brain brief
```

一次调用拿到：规范与偏好 + 事实索引 + 主题摘要，按字符预算自动裁剪（`--budget 6000` 可调）。
**brief 的内容当背景，不要复述给用户**；但**不要在没读它的情况下声称"我记得"**。

## 需要具体信息时

```bash
brain search 代理            # 检索全库（连服务端时是相关性排序，本地模式是逐行匹配，带 文件:行号）
brain show atoms/202610/m_01j9zk4m2x.md   # 按路径读全文
```

规则：**先 search，再回答**。库里有就以库里为准；库里没有就说不知道并去查证。
库里内容有过期迹象（日期旧、与现场冲突）时，**以现场实测为准，并立刻修正库**。

## 什么时候写（重要）

满足任一条就写：

1. 用户给出**新的稳定事实**：账号归属、路径、端口、硬件、网络、偏好、称呼、禁忌。
2. 用户**纠正**了之前的说法 → 先 `search` 找到旧条目，再写一条 `-k fact` 写「更正：原 X 现为 Y」，
   并用 `--supersedes <旧 id>` 标注取代关系。
3. 你**踩坑并解决**了一个非显而易见的问题 → `-k lesson`，写清症状 + 原因 + 解法。
4. 某个 **harness / 版本的差异**：同样的操作在不同 harness 上表现不同 → `-k host`。
5. 项目状态发生实质变化（完成了什么、卡在哪、下一步）→ `-k project -t <项目名>`。

**不写**：会话流水、临时中间结果、能从代码/文档直接查到的东西、凭据明文。

## 怎么写

```bash
brain remember "内容，一句话说清结论" -k fact -t network -t proxy
```

- `-k` 选类型：`fact` `rule` `pref` `lesson` `host` `project` `idea` `secret-ref` `journal`
- `-t` 是标签，可重复；**第一个标签决定归档主题**（渲染成 `views/topics/<slug>.md`）
- 写成**可独立阅读的结论句**，不要写"见上文"。
- 一条只讲一件事；多条就多次调用。
- 可选：`--confidence high|medium|guess`、`--ttl 2027-04-29`（到期后会被 `GET /v1/stale` 列为待复核）、
  `--id <id>`（离线续写）、`--supersedes <id>`。
- 敏感值：不写密码 / token 明文，写「凭据存放位置」和「怎么取」。

落盘由服务端完成（在线一次 `POST /v1/remember`；离线先进本地 outbox，恢复后按 id 幂等重放），
**你不需要关心文件细节**。想确认写进去了，就 `brain search <关键词>` 读回来。

## 更正与遗忘（不要改写历史）

```bash
brain forget <id> --reason "结论已过期"                    # 追加一条 tombstone，atom 本身不动
brain forget <id> --reason "换方案了" --replaced-by <新id> # 标注被谁取代
brain restore <id> --reason "恢复"                        # 撤回遗忘（同样是追加）
```

atom 是不可变的：**永远不要手改 `atoms/` 下的文件**，更正一律"写新条目 + 标注取代"。

## 维护（只在服务器上做）

```bash
brain verify              # 五项自检：id、hash、supersedes、tombstone 序号、视图一致性
brain reindex --force     # 视图是派生品，坏了就重建
brain sync -m "..." --push  # git 提交（并可推送私有远端做备份）
brain doctor              # 一眼看模式 / 库根 / 身份 / 条数 / verify 结果
```

## 边界

- 库是**共享的**：写进去的内容所有主机、所有 harness 都能看到。不写私人闲聊、不写别人的隐私。
- 冲突时不要删别人的条目，追加一条更正并标注日期，让历史可见。
- 凭据只记位置，不记明文。
