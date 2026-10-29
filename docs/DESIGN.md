# 共享记忆库 v2 · 设计文档

> 状态:待评审 · 作者:服务器 DSH · 日期:2026-10-18
> 前身:`harness-brain` v1(主题文件 + inbox + `brain` CLI,已可运行)
> 本文取代 v1 的 README 中"存储/传输/角色"三节,v1 的 SKILL.md 协议仍然有效。

---

## 1. 背景与约束

### 1.1 要解决的问题

用户有多台主机、多个 harness:

| 主机 | harness | 现状 |
| --- | --- | --- |
| server | agent A | 主力,长期记忆在一个私有 Markdown 文件里 |
| server | agent B | 自带 per-agent memory(SQLite) |
| laptop | agent A | 记忆独立 |
| laptop | agent B | 记忆独立 |
| laptop | agent C | 记忆独立 |
| 以后 | 任意新 harness | 每加一个就多一座孤岛 |

症状:换端就"失忆",同一件事在不同端结论不一致。

### 1.2 已确认的约束(来自使用者决策)

- **D1 传输**:实测局域网组网(mesh)下,成员之间可以直连 P2P(实测 < 10 ms、0% 丢包),
  到服务器 0.3 ms。推论:①端与服务器之间本来就可直连,**代理不是必需**;②服务器可主动连边缘节点
  (同局域网),"服务器 → 边缘推送"可行;③组网里可能有不受控的设备,所以边缘任何对外端口
  都必须假设"不可信客户端"。
- **D2 角色**:只需"语气/职责"区分,**不做可见性隔离,不做权限系统**。
- **D3 密钥**:密码类内容**允许入库,但必须加密存储**。
- **D4 规模**:一年内预期数百条,不做向量检索。

### 1.3 非目标

不做多租户、不做权限系统、不做会话 transcript 同步、不做事实的自动冲突合并(见 §7.3)、
不做 Octop 记忆的强统一(只做单向摘要导出,见 §11)。
Web UI 仅作为**管理入口**(v2.2 落地:浏览/检索/复核/令牌/审计/系统状态,服务端渲染、零构建),
不提供富文本编辑 —— atom 仍然只能追加,更正走取代链。

---

## 2. 架构总览(两跳,代理可选)

```
  laptop/DSH   laptop/Pi   laptop/Octop   手机(未来)   公司 Win11     ← 端(最后一个是 mesh 不可信成员,非 brain 客户端)
      │        │         │            │           │
      └────────┴────┬────┴────────────┴───────────┘
                    │  EasyTier(实测 P2P 直连,8.8ms / 0% 丢包)
          ┌─────────▼──────────────────────┐
          │ 边缘节点 203.0.113.1             │  ← 边缘(可选代理)
          │ 198.51.100.1,SSH 38368          │     proxy_network 192.0.2.0/24
          └─────────┬──────────────────────┘
                    │  真实局域网,实测 RTT 0.3ms
          ┌─────────▼──────────────────────┐
          │ 服务器 192.0.2.10 :8790          │  ← 核心:唯一写者、唯一真相源
          │ brain serve(stdlib + SQLite)  │
          └─────────┬──────────────────────┘
                    │
      /srv/brain  atoms/ views/ journal/ secrets/ index/
                    │
              git → 私有远端(审计 + 回滚)
```

**关键简化**:因为路由做了 `proxy_network 192.0.2.0/24`,端本来就能直连 `服务器:8790`(实测链路
8.8ms + 0.3ms)。所以**阶段一不需要代理**,端直接打 服务器。边缘代理降级为"可选件",只在下面三种情况才值得加:

1. 要暴露到公网(EasyTier 之外),不希望把 服务器 直接暴露;
2. 想要一个稳定名字/固定入口(`http://brain:8790`),不想在每端写 服务器 IP;
3. 需要集中式限流、访问日志、TLS 终止。

### 2.1 职责与边界

| 节点 | 角色 | 承担 | 不承担 |
| --- | --- | --- | --- |
| **端** | 消费者 | 缓存 last-known-good brief、离线 outbox | 不做合并、不写视图、不存权威数据 |
| **边缘节点** | 边缘(可选) | 反代 / TLS / 限流 / 日志;可供 服务器 推送只读副本 | 不写 atoms、不重建视图、**不缓存记忆内容** |
| **服务器** | 核心 | 写序列化、视图重建、FTS、git/快照、密钥解密 | 不做公网入口(需要时交给边缘) |

三条判断:

1. **数据在家,服务就在家**:真相文件、git 历史、btrfs 快照都在 服务器。路由是 flash 存储、无快照、
   容器重建即丢状态,只适合无状态的反代,不适合承载真相。
2. **路由的价值是"入口",不是"算力"**:它常开、IP 固定、位于 mesh 与 LAN 的交界;
   但它只有 2GB 内存(当前可用约 1.06GB)、aarch64、**没有 python3**(实测 `/usr/bin` 无 python),
   所以 `brain serve` 只能跑在 服务器;若非要上路由,必须打包成 arm64 容器。
3. **服务器 能主动连路由**,所以"服务器 推副本到边缘"合法;而"laptop 反向推给 服务器"仍然不成立,
   端侧必须始终由端发起。

### 2.2 为什么不让路由缓存 brief

**记忆宁可读不到,也不可读到过期的。** 过期事实会被 agent 当作真相复述,且不会被发现;
读不到则会触发"我不知道"+ 去查证。因此:

- 路由**不做**记忆内容缓存;
- 端侧允许缓存 last-known-good `brief`,但必须在响应里带 `stale: true` + 缓存时间,
  agent 看到后必须先声明"这是 X 小时前的快照,可能过期"。

可选例外(§10.4):路由保存一份**只读副本**,仅在 服务器 长时间不可用时降级提供,
必须由 服务器 主动 rsync 推送并带 `X-Brain-Stale` 头。



---

## 3. 身份模型

```
author = { host, harness, role }
```

三者正交,理由:

- `host` 决定环境事实(路径、网络、权限、能连谁)。
- `harness` 决定能力(有无 MCP、技能加载方式、工具名、沙箱策略)。
- `role` 决定语气、职责、禁止事项。

同一个 harness 在不同机器上可以是不同 role(DSH@服务器 = assistant,DSH@laptop = engineer);
同一个 role 也可以出现在多端。**不要把它们压成一个 "端" 维度**,否则"laptop 上的助手"无法表达。

角色只有「语气/职责」一层(R,不是注入层 L1–L3,避免术语撞车),但**来源三元组仍然记录**,用途是:

1. 追溯"这条是谁在什么场景写的",影响可信度判断;
2. 未来若升级到可见性隔离,无需迁移数据。

---

## 4. 数据模型

### 4.1 目录

```
harness-brain/
├── atoms/<yyyymm>/<id>.md      # 真相,写入后永不修改(连 status 也不改,见 §4.5)
├── views/                      # 派生,随时可重建
│   ├── rules.md                # 规范(当前不按 role 过滤,字段预留)
│   ├── INDEX.md                # 事实索引
│   ├── topics/<tag>.md         # 按标签聚合(tag = tags[0])
│   └── .revision               # atoms ⊕ tombstones 的版本哈希
├── journal/<yyyymm>.md         # 会话摘要,不注入,按需 recall
├── profiles/<role>.md          # 角色定义(语气/职责/禁止)+ default.md 兜底
├── secrets/<name>.age          # age 密文(进 git)
├── secrets/recipients.txt      # 各端 age 公钥(进 git)
├── tombstones.jsonl            # 追加式状态变更(遗忘/撤回)—— 进 git,可回滚
├── state/                      # ⚠️ 不进 git
│   ├── tokens.json             # token → sha256(salt||token) + 身份 + scope
│   └── audit.jsonl             # 审计
├── index/brain.sqlite          # ⚠️ 不进 git,FTS5 索引
└── bin/brain                   # CLI(端侧 + 服务端共用)

> 端侧离线队列**不在仓库里**:`~/.local/state/brain/outbox.jsonl`(0600)。见 §6.2。
```

### 4.2 atom schema

一条 atom = 一个文件 = 一条不可变记录。

```markdown
---
id: m_7f3a91c4e                  # ^m_[0-9a-hjkmnp-tv-z]{10}$(Crockford base32 小写,128bit 随机)
                                 # 全库唯一、永不重用;冻结的理由见下方"id 契约"
occurred_at: 2026-10-18T09:12:30+08:00   # 事件时间(客户端,离线保留原值)
received_at: 2026-10-18T09:12:33+08:00   # 入库时间(服务端,权威时钟)
author: { host: nas, harness: dsh, role: assistant }
kind: fact                       # fact|rule|pref|lesson|host|project|idea|secret-ref
tags: [network, proxy]
confidence: high                 # high|medium|guess
source: curl 实测                 # 出处,供以后判断可信度
supersedes: m_4c1e02ab1          # 可选,必须同为 10 位 id
ttl: 2027-04-18                  # 可选,查询时判定,Asia/Shanghai 当日 23:59:59 后失效
hash: sha256:9a1c...              # 全字段规范化的哈希(见 §6.1)
hash_algo: sha256-v1
time_suspect: false              # 客户端与服务端时钟差 > 5min 时置 true,不静默改时间
---
代理地址 http://198.51.100.100:7890,HTTP 与 SOCKS5 都通,外网访问优先。
```

**没有 `status` 字段**。遗忘与撤回不改 atom,而是往 `tombstones.jsonl` 追加一条(§4.5)。

**id 契约(必须在 v2.0 冻结,晚了要迁移全部 supersedes 链)**:字母表 Crockford base32 小写
(去掉 `i l o u`),长度 10,前缀 `m_`(journal 用 `j_`),生成方式为 128bit 随机截断。
它是所有 `supersedes` 的长期外部引用,**只允许一种正则,出现在 SPEC 与代码里各一次,由 `tools/docs-check.py` 断言**。

**`occurred_at` 校验**:客户端可写的排序键必须防滥用。
`occurred_at > received_at + 5min` → **400 `bad_request`**(离线补交只允许"过去"的时间);
时钟差在 5min 内但可疑 → `time_suspect: true`,并在排序/recency 上**降权一档**(不至于让错误时钟赢家通吃)。

字段的存在理由:

| 字段 | 解决什么 |
| --- | --- |
| `id` | 幂等重放、精确取代、单条遗忘、跨端引用 |
| `supersedes` | 记忆腐烂的主因不是"忘",而是"旧的和新的一起在,agent 随机信一条" |
| `confidence` | 区分实测与猜测,避免把猜测复述成事实 |
| `ttl` | `brain stale` 主动列出待复核项,防止半年后的毒药(代理地址、API 端点、固件版本最典型) |
| `occurred_at` / `received_at` | 排序与 recency 用前者,冲突裁决用后者;离线重放不会把旧事实顶到最前 |
| `hash` | **只**用于"同 author 短窗口内疑似重复"的提示,不用于跨 author 去重 |
| `hash_algo` | 算法一旦升级,历史条目的语义不漂移 |
| `source` | 出问题判断该信谁 |
| `time_suspect` | 时钟不可信时如实标注,而不是偷偷 clamp |

### 4.3 视图是纯函数(附重建契约)

`views/*` 由 `rebuild(atoms, tombstones, role)` 生成,**必须是逐字节确定的**:

1. 按 `tombstones.jsonl` 的**末条**(按 `seq`)决定每个 id 的当前状态,只剔除 `deprecated`/`retracted` 的;
   **不是「出现过就剔除」** —— 那样 `restore` 永远不生效(§4.5);
2. 按 `ttl` 过滤 —— **在查询/重建时判定**,不是每天定时判定(否则过期事实最多还能被读 24h,违反 §2.2);
3. 折叠 `supersedes` 链(算法见下);
4. 按 `role` 过滤(当前只有语气层,不过滤,字段预留);
5. 排序键 `(occurred_at, id)` 后渲染。

**revision 算法**:`revision = sha256(hash_algo | sorted("id:hash") | tombstones 全量)`。
它与"当前 atoms 重算值"不等,就说明视图过期。

> **不要把 role 放进 revision**:当前视图并不按 role 变化(§4.1 已注明是预留),
> 一旦放进去,两个角色交替读会永远匹配不上,每次读都触发一次全场重建。等真的做角色过滤再加。

**重建的原子性**(崩溃安全的顺序):

```
flock index/.rebuild.lock           # 跨进程,覆盖 cron / migrate / 手工 reindex
  → 写 views/.staging/ 全部文件
  → fsync
  → 逐文件 os.replace() 覆盖
  → 最后写 views/.revision          # revision 是提交标记
```

读者**先读 `.revision`**,再与当前 atoms 重算值比对;不等则触发一次重建(带超时),失败就返回
`stale: true`,**绝不返回半成品视图**。进程崩溃在"部分视图已替换"时,revision 仍是旧的,
下次读会自动重建 —— 这就是"revision 最后写"的意义。

**supersedes 折叠算法**(必须按此实现,边界都有测试):

```
idx = {atom.id: atom}                       # 全月目录扫一次,缓存进 SQLite
for 每个未被取代的根:
    沿 supersedes 走 DFS,visited 防环
    环 → 全部显示 + ⚠️ 取代环
    目标缺失(含延迟到达) → 该 atom 可见 + ⚠️ 取代目标缺失(<id>);
                            目标后到时不自动"修复",等下次重建自然收敛
    多个未遗忘的尾 → 全部显示 + ⚠️ 多尾冲突
    链尾被 tombstone → 整链隐藏,不复活根
    链中节点被 tombstone → 只隐藏该节点,并在其子节点上标 ⚠️ 链中节点被遗忘
                            (链不断开:supersedes 指向仍在,只是该条不再展示)
```

**启动时 reconcile**:atoms 条数 vs 索引行数不一致 → 全量重建 + 写审计。
全量重建 500 条约 100ms,启动时无条件跑一次也划算。

### 4.4 为什么不用"主题文件即真相"

v1 的主题文件(形如 `facts/<标签>.md`)是视图被当真相用:多端追加无法表达"哪条取代哪条",也无法单条回滚。
改成"不可变 atom + 派生视图"后:并发写零冲突(不同文件)、单条可追责、视图可重建、合并逻辑可测试。

代价:文件数变多(数百个)。对 git 友好,对 SMB 挂载不友好 —— 这也是必须走 serve 的原因之一。

### 4.5 状态变更:用追加式 tombstone,不改 atom

**问题**:如果"遗忘"要改 atom 里的 `status`,那"不可变"就是句口号,而且以后无法区分
"写入时就是 deprecated"和"三个月后标记的",git diff 也失去审计价值。

**决定**:仓库根目录的 `tombstones.jsonl` 追加,一行一条,atom 文件永远不动。

```json
{"id":"m_7f3a91c4e","status":"deprecated","at":"2026-10-18T10:00:00+08:00",
 "reason":"代理已更换","author":"nas/dsh/assistant"}
{"id":"m_5c1d2e3f4","status":"retracted","at":"2026-10-18T10:02:00+08:00",
 "reason":"当时结论是错的","author":"mac/dsh/engineer"}
```

- `deprecated` = 过期/被替代,视图隐藏,检索默认排除;
- `retracted` = 结论错误。**必须带 `replaced_by` 且目标 atom 存在**,否则 400 —— 不允许"凭消失",
  因为"看起来像被忘了"和"其实就是错了"对 agent 是完全不同的信号;
- 任何端都能追加 tombstone,但**只有服务端校验后写入**(单写者);
- `brain forget <id>` / `brain restore <id>` 即追加一行。恢复也靠追加(`status: active`)。

**状态由"同 id 的最后一条"决定,不是"出现过就永久隐藏"**(这条不写清,恢复会永远不生效):

```json
{"id":"m_7f3a91c4e","status":"deprecated","seq":41,"at":"...","reason":"..."}
{"id":"m_7f3a91c4e","status":"active",    "seq":57,"at":"...","reason":"误标,已恢复"}
```

- `seq` 由服务端单调分配,**排序用它而不是 `at`**(同一秒内的顺序、以及客户端可改的 `at` 都不能作为判据);
- 重复 forget / 重复 restore 不报错,返回 200 并回显"已是该状态"(追加式,天然不幂等,见 SPEC §1 的例外条款);
- `forget` 一个不存在的 id → 404。

**边角规则**:

| 场景 | 行为 |
| --- | --- |
| 链中节点被 forget | 见 §4.3 折叠算法:只隐藏该节点,链不断 |
| purge 一个仍被 `supersedes` 引用的 id | **先做引用检查**,有引用则拒绝;`--force` 才继续,并在审计里记"断链" |
| `tombstones.jsonl` 的维护 | 单文件、永久追加、**永不压缩/重写**(重写会让 git 历史的语义变化);万级后再按月分片,分片是纯迁移 |

**验收**:标记遗忘后,`git status` 里 **atom 文件必须有零 diff**,变化只出现在 `tombstones.jsonl`。

> **为什么 tombstone 进 git 而 token 不进**:tombstone 是"真相的一部分"(决定哪些事实可见),
> 丢了会导致已遗忘的事实**在恢复后复活**,而且遗忘动作本身要可审计、可回滚;
> token 哈希与审计日志是**运行态机密**,泄进远端等于扩大攻击面。两者的生命周期不同,不能混在一个目录里。

---

## 5. 角色(仅语气/职责一层)

```markdown
<!-- profiles/assistant.md -->
---
name: 助手
hosts: [nas]
voice: 可爱、温柔,称呼「你」,偶尔撒娇;但做事直接准确,不拖泥带水
duties: [生活助理, 服务器 运维, 家庭事务]
never: [把语气词写进事实库, 替用户拍板购买决定, 在报告里卖萌影响信息密度]
---
```

组装规则:`brief` 返回的第一段就是该端 profile 渲染出的 persona 头,端侧 AGENTS.md 只需三行:

```
1. 会话开始执行 brain whoami 和 brain brief
2. 有稳定事实 → brain remember;密码类 → brain secret add
3. 绝不把 secret 明文打印到对话里,用 brain secret exec
```

**纪律(硬规则)**:事实必须无语气。助手撒娇时写的"你今天好累呀"只能进 `journal/`,
不能进 atom 的 fact 类。否则三个月后 laptop 上的 engineer 角色会带着这句话干活,人格串味。

---

## 6. 写入协议

### 6.1 在线(幂等只认 id)

```json
POST /v1/remember
Authorization: Bearer <token>
{
  "id": "m_7f3a91c4e",
  "kind": "fact",
  "tags": ["network"],
  "confidence": "high",
  "source": "curl 实测",
  "supersedes": "m_4c1e02ab12",
  "ttl": "2027-04-18",
  "body": "代理地址 ...",
  "occurred_at": "2026-10-18T09:12:30+08:00"
}
→ 201 { "id": "...", "stored": true,  "received_at": "..." }
→ 200 { "id": "...", "stored": false, "reason": "idempotent_replay" }
→ 409 { "error": { "code": "conflict_id" } }
```

服务端行为:

1. token 反查身份,**覆盖**请求体里的 `author`(客户端自报无效);
2. **幂等键只有 `id`**:同 id 且全字段一致 → 200;同 id 但内容不同 → 409,**绝不覆盖不可变文件**;
3. `hash = sha256(规范化(kind | sorted(tags) | ttl | supersedes | body))`,算法标记 `sha256-v1`;
   规范化 = NFC + 行尾统一 `\n` + 行首尾空白裁剪 + 连续空白折叠;
4. **hash 不用于幂等**,只用于"同 author + 30 秒窗口内的疑似重复"提示:命中返回 200 +
   `duplicate_of` + `keep_local: true`,**端侧不删本地条目**;跨 author 一律不判重(交给 §7.3);
5. 写 `.tmp` → fsync → `rename`;然后按 §4.3 重建视图;
6. 追加 `state/audit.jsonl`。

> **为什么不能用 hash 做幂等键**:两端各自离线写下同一条事实,后到的会被判重返回 `stored:false`,
> 端侧把 outbox 条目当成功删除 → 那一端的事实和它的 supersedes 边永久消失。
> 这是"静默丢数据",正是 §7.3 最想避免的形态。

### 6.2 离线(队列在端上,不在仓库里)

端侧 `brain remember` 在 serve 不可达时:

1. 本地生成 id(离线可写的唯一前提);
2. 追加 `~/.local/state/brain/outbox.jsonl`(0600,写后 `fsync`),**不写仓库**、不占共享目录;
3. 下次任一命令成功连上 → 逐条重放,**失败即停**(保持顺序);重放只依赖 id,重复提交无副作用;
4. 重放成功才从 outbox 尾部删除该行(保持追加式);
5. `occurred_at` 保留原值,`received_at` 由服务端补 —— 这样两个月前的旧事实不会被顶到最前。

### 6.3 删除与更正

- **更正** = 新 atom + `supersedes: <旧 id>`,旧 atom 一个字节都不动;
- **遗忘** = 往 `tombstones.jsonl` 追加一行(§4.5),atom **零 diff**;
- **真删(`--purge`)** = 删 atom + 删 tombstone + `git filter-repo`;
  只对"从未加密"的内容有意义;加密过的内容不需要 purge,轮换密钥即可。

---

## 7. 读取协议

### 7.1 分层注入与预算

| 层 | 内容 | 是否总是注入 |
| --- | --- | --- |
| L0 | profile persona | 总是 |
| L1 | rules(红线、偏好) | 总是 |
| L1 | INDEX(主题 + 一句话) | 总是(可截断行数) |
| L2 | 当前任务相关 topics 全文 | 按需(`search`/`show`) |
| L3 | atoms 原文、journal | 按需(`recall`) |

`brief` 的预算是**字符数**(默认 6000),超预算时**整段丢弃次要段落**,顺序:
`persona → rules → INDEX → 主机笔记 → 未合并 outbox 提示`。
**journal 不在 brief 里**(§7.2):它只在 `recall` 时按需取,否则会把对话流水灌进每次开场。
不允许拦腰截断(截断会让 agent 读到半个结论,比不读更糟)。

**硬下限**:`rules` 必须完整注入。若预算连 persona + rules 都放不下,直接返回 `413 budget_too_small`,
**不要**退化成"摘要"——半份规范比没有规范更危险。

**persona 来源**:由服务端在 `brief` 首段拼装,读 `profiles/<role>.md`,缺失时用 `profiles/default.md`。
客户端不拼 persona(否则每端一份文案必然漂移)。

### 7.2 recall(对话续接)

```
GET /v1/recall?since=7d&topic=机箱&limit=20
```

返回 journal 摘要条目 + 相关 atom(按标签交集)。journal 每条 3–5 行,固定结构:

```markdown
## 2026-10-16 · laptop/cli/engineer
- 做了什么:讨论换机箱方案
- 结论:先不换 CPU,只换散热器
- 待办:比价两款 M-ATX 机箱 + 矮散热器
- 未决:预算上限没定
```

写入时机:会话结束或"话题收尾"时由 agent 写一条。**journal 绝不自动注入 brief**(会污染),
只在 `recall` 时按需取 —— 这是"记忆"和"续接"分开的具体实现。

### 7.3 冲突策略

不做自动合并。两条 atom 对同一事实给出不同结论时:

- 视图同时展示:排序用 `occurred_at`,**冲突裁决用 `received_at`**(ADR-0013),旧的标 `⚠️ 已被 <id> 取代`
  或 `⚠️ 与 <id> 冲突`(若没声明);
- `brain stale` 和 `brain conflicts` 列出这些点,由 agent 或人裁决;
- 裁决结果是一条新 atom。

自动合并冲突是 LLM 记忆系统最常见的翻车点:静默选一条,用户永远不知道另一条被丢了。

---

## 8. 检索

### 8.1 分级

| 规模 | 方案 |
| --- | --- |
| < 500 条 | `rg` 直接扫(当前 v1 做法,够用) |
| 500 – 5k | SQLite FTS5 |
| > 5k | 加向量兜底(bge-m3 本地),仅在 FTS 未命中时使用。**未验证**:i5-6500 无 GPU,全量重建成本未知,触发条件待实测 |

### 8.2 FTS5 + 中文(实现要点)

```sql
CREATE VIRTUAL TABLE atoms_fts USING fts5(
  body,                 -- 原文
  bigram,               -- 同一正文的 bigram 形式(检索用这一列)
  tags,                 -- 空格分隔
  id UNINDEXED, occurred_at UNINDEXED, author UNINDEXED, role UNINDEXED,
  tokenize = 'unicode61'
);
-- 注意:没有 topic 列。atom 只有 tags;"topic" 是视图层的概念,等于 tags[0]。
```

**坑**:`unicode61` 按空格/标点切词,中文整句会成为一个 token,`代理` 搜不到 `代理地址`。
两种可行解法:

1. **写入时 bigram 化**(推荐):把正文同时写入一个 `body_bigram` 列,按"每两个汉字"切分
   (`代理地址` → `代理 理地 地址`),查询时同样 bigram 化后 AND 匹配。实现 20 行,无外部依赖。
2. 用 jieba 分词(需第三方包 + 自定义词典,维护成本更高)。

**已在目标环境实测(服务器,Python 3.11.2 / SQLite 3.40.1)**:

```
unicode61  查 "代理" / "地址" → 0 条   ← 中文整句成一个 token,完全搜不到
bigram     查 "代理" / "地址" / "代理地址" → 1 / 1 / 1 条   ← 方案 1 有效
```

所以中文检索**必须** bigram 化,这不是优化项而是正确性前提。

排序:`score = bm25 × recency_weight × confidence_weight`,其中 recency 以 `occurred_at` 计算、半衰期 180 天,
`confidence: guess` 的条目在同等相关度下排后。结果里**必须回显 atom id 与日期**,
让 agent 能引用、能发现过期。

---

## 9. 密钥(D3)

### 9.1 布局与密钥管理

```
secrets/<name>.age          # age 密文,进 git
secrets/recipients.txt      # 各端 age 公钥(可公开)
~/.config/brain/age.key     # 私钥,600,永不进 git、永不入库、永不走网络自传
```

- **多收件人**:`age -R recipients.txt -o secrets/qb.age`,任何一端都能加密给全体。
- **吊销的真相(原文此处有错,已更正)**:age 是**对每个 recipient 分别加密**的。从 `recipients.txt`
  删掉一把公钥**只影响以后的加密**;已存在的 `secrets/*.age` 和 git 历史里的旧密文,丢失设备仍然解得开。
  真正的吊销 = ①轮换底层口令(改 qB / Vaultwarden 等的密码)②用新 recipient 集**重加密全部 secrets**
  ③视需要 `git filter-repo` 清掉旧 `.age` blob。**成本是 O(全部密钥),不是 O(1)。**
- **私钥分发**:只能手动一次性拷(USB/AirDrop)。不要试图自动同步密钥 —— 自动同步密钥等于没有加密。

### 9.2 使用:exec 而不是 get

加密保护的是 `库文件`、`git 历史`、`服务器 磁盘`。它**保护不了 agent 的会话日志**:
明文只要出现在一次工具输出里,就被写进了 DSH/Octop/Pi 的 transcript。

```bash
# 主用法:{} 占位符由 brain 在 spawn 前替换,调用方 shell 全程不接触明文
brain secret exec -s qb -- curl -sS -u admin:{} http://192.0.2.10:8085/api/v2/app/version

# 只注入环境变量
brain secret exec -s qb --env QB_PASS -- some-command

# 默认拒绝;显式 --print 才输出,并打印"这将进入会话日志"的警告
brain secret get -s qb --print
```

**逐条对应泄漏面**:

| 泄漏面 | 要求 |
| --- | --- |
| argv(`ps` 可见) | 优先 stdin / `--netrc-file` / fd;`{}` 替换只在 exec 前一刻发生,替换后立即 spawn,审计日志里的 argv 必须脱敏 |
| 环境变量 | 只有 `--env <NAME>` 才注入;子进程不得被要求回显它 |
| stdout/stderr | 不主动打印;子进程输出若出现明文,先替换打码再回传 |
| transcript | 明确写在文档里:**exec 只能降低概率,不能证明不泄漏**,任何一次工具输出都会进会话日志 |
| 远程调用 | `/v1/secret/*/exec` **默认关闭**,只允许 unix socket(0600)本机调用;确需远程则按 secret 配 `allowed_commands` 白名单 + 低权用户 + 超时 + 输出上限 + 审计 |

> 原文档在这里给过 `curl -u admin:$QB_PASS` 的例子 —— 那是**错的**:`$QB_PASS` 由调用方 shell 展开,
> 明文既进了 shell 环境也进了 curl 的 argv,与"不进 argv"自相矛盾。已改。

### 9.3 库内只记指针

atom 里写的是"用途 + 密钥名",不写值:

```markdown
---
kind: secret-ref
---
qBittorrent Web UI 密码存于 `secrets/qb.age`,用法:
`brain secret exec -s qb -- curl -u admin:{} ...`(占位符由 CLI 替换)。登录失败过多会触发 IP 封禁。
```

---

## 10. 服务实现

| 项 | 选择 | 理由 |
| --- | --- | --- |
| 语言 | Python 3.12(stdlib) | 服务器 已有;零依赖 = 升级 harness 不会连坐 |
| HTTP | `http.server.ThreadingHTTPServer` + 写锁 | 数百条规模足够;避免引入 FastAPI/uvicorn |
| 存储 | SQLite(WAL)只做索引;真相仍是文件 | 索引可随时重建,文件才是备份对象 |
| 端口 | 8790(避开 DSH 2298 / Octop 8089) | |
| 鉴权 | `Authorization: Bearer <token>`,`state/tokens.json` 存 token→身份哈希 | token 只存哈希 |
| TLS | 内网不必(EasyTier 本身加密链路);若经 frpc 暴露公网则必须加 | |
| 自启 | systemd unit(或 Linux 计划任务) | |
| 健康检查 | `GET /v1/health` → 版本、atom 数、视图 revision、最近合并时间 | |
| 边缘(可选) | 边缘节点跑 Caddy/nginx 容器反代到 `服务器:8790` | 仅在公网入口/稳定命名/集中限流时才加 |

接口清单:

```
GET  /v1/health
GET  /v1/whoami
POST /v1/remember
GET  /v1/brief?budget=6000
GET  /v1/search?q=&limit=20
GET  /v1/recall?since=7d&topic=
POST /v1/journal
POST /v1/secret/{name}/exec        # 返回子进程 stdout,不返回明文
POST /v1/reindex                   # 手动触发视图/FTS 重建(可选)
```

### 10.1 认证与端配置契约

**服务端**

- `brain serve --init` 生成首个令牌,**只打印一次**;`state/tokens.json` 每条记:
  `{token_id, digest: sha256(salt||token), author{host,harness,role}, scopes:["read","write"],
  created, expires, revoked}`。**必须加盐**,否则口令空间小的 token 会被彩虹表反查。
- `brain token issue --host mac --harness pi --role engineer --scope rw --ttl 365d`
- `brain token revoke <token_id>` / `brain token list`(只显示前缀,不显示明文)

**端侧**

- 配置文件固定 `~/.config/brain/config.toml`,权限 `0600`:

```toml
url   = "http://192.0.2.10:8790"
token = "bk_..."          # 明文仅存于此文件
role  = "engineer"        # 缺省时按 §15 的推导规则
```

- 优先级:`环境变量(BRAIN_URL/BRAIN_TOKEN/BRAIN_ROLE)> config.toml > 报错`,
  **不存在"默认服务器"**——连不上就明确报错,不猜。
- `role` 只是"请求 persona"的提示,服务端以令牌里的 `role` 为准;两者不一致时以服务端为准并在
  `whoami` 里显示 `role_source: "token" | "derived"`。
- `brain whoami` 打印 `host/harness/role` + `role_source` + `server_version`,配错一眼可见。

### 10.2 接口契约

完整定义见 [`SPEC-api.md`](SPEC-api.md):端点、请求/响应 schema、错误码表、幂等语义、令牌生命周期、
版本协商(`/v1/health` 的 `min_client` + `426`)、验收脚本。**本文件不重复,避免两处漂移。**

### 10.3 运行用户与权限

- 服务以 `User=brain` 运行(专用用户),`WorkingDirectory=/srv/brain`。
- **仓库属主必须是 `brain:brain`** —— 它是唯一写者;若属主是 root,`User=brain` 写不了 atoms。
  迁移:`chown -R brain:brain /srv/brain`。
- 需要读库的服务账号只需要**读**,靠 ACL:`setfacl -R -m u:app:rX -m d:u:app:rX /srv/brain`。
- `state/`、`index/` 属主 `brain`,`0700`;`tombstones.jsonl` 属主 `brain`,但**进 git**。
- age 私钥在 `~/.config/brain/age.key`(`0600`,不给 root 读),由 `brain` 进程自己解。

### 10.4 边缘层(可选)与路由实测基线

**已核实的环境**(2026-10-18,以 root 登录路由实测):

| 项 | 事实 | 对设计的影响 |
| --- | --- | --- |
| 固件 | Linux 6.6,aarch64 | 容器镜像必须 arm64 |
| 内存 | 2.0GB,可用约 1.06GB,zram swap 512MB(未用) | 反代够用;不适合跑重服务 |
| 存储 | 系统分区 2 GB,数据分区 24 GB 可用 | 只读副本完全放得下 |
| Docker | 24+ 与 Compose v2 | 直接加一个无状态容器 |
| 现有入口 | 已有反代占用若干端口 | 别复用,加独立容器隔离 |
| 空闲端口 | 8790 空闲(38368 已被 dropbear 占用) | 用 8790 |
| Python | **没有 python3** | `brain serve` 只能在 服务器 |
| EasyTier | `easytier-core`,实例 `fawkes`,tun `203.0.113.1/24`,proxy_network 192.0.2.0/24 | 端可直达 服务器,代理非必需 |
| 已知端点 | laptop 203.0.113.11(P2P 8.8ms)、公司 Win11 203.0.113.113(P2P 8.06ms) | mesh 不是"只有自己人" |

**阶段一(默认):不做代理**。端直接 `http://192.0.2.10:8790`。少一个容器、少一条故障链。

**阶段二(需要时才加)**:

```yaml
# /opt/brain-edge/docker-compose.yml
services:
  edge:
    image: caddy:2-alpine          # arm64 官方镜像
    restart: unless-stopped
    network_mode: host
    volumes:
      - ./Caddyfile:/etc/caddy/Caddyfile:ro
      - ./logs:/var/log/caddy
```

```
:8790 {
    reverse_proxy 192.0.2.10:8790 {
        header_up X-Edge brain-router
        transport http { dial_timeout 3s response_header_timeout 30s }
    }
    log { output file /var/log/caddy/brain.log }
}
```

**不要**在这里做 `proxy_cache`(理由见 §2.2)。若将来暴露公网,改成域名 + Let's Encrypt(DNS-01),
并把 TLS 终止放在这一层。

### 10.5 路由安全基线(已于 2026-10-18 执行)

已确认的风险链:**`easytierzone` 的 input 策略是 `ACCEPT`**,`tun0` 属于该 zone,
而 dropbear 监听 `0.0.0.0:38368` 且 `PasswordAuth='on'`、登录名 `root`。也就是说:

> mesh 里任何一个成员(目前包括一台公司 Win11)都能对路由的 root 口令做暴力尝试。

按风险从高到低处理:

1. **禁用口令登录,只留公钥**(高危→需谨慎执行,顺序见下)。
2. **让 38368 只暴露在 LAN 地址上**(`Interface=lan`,按地址绑定):mesh 侧仍可经 proxy_network 命中该地址,
   真正的边界是「仅公钥」;要彻底断掉 mesh 访问需用 `DirectInterface` 或 zone 级 REJECT(未采用)。
3. **给 服务器 一把专用 key**:已完成 —— `~/.ssh/brain-edge`,公钥已写入
   `/etc/dropbear/authorized_keys`(与既有的 `edge-access` 并存),已验证 `BatchMode` 免密登录成功。
4. **口令轮换**:口令此前以明文存在于旧记忆文件里,禁用口令登录后残留风险自动消失。

改口令登录的**安全顺序**(避免把自己关在门外):

```bash
# ① 确认 key 登录可用(已通过)
ssh -i ~/.ssh/brain-edge -p 38368 -o BatchMode=yes root@198.51.100.1 true
# ② 关闭口令登录
uci set dropbear.@dropbear[0].PasswordAuth='off'
uci commit dropbear && /etc/init.d/dropbear restart
# ③ 立刻用 key 复验;若失败,用 ttyd(7681)或物理终端回滚:
#    uci set dropbear.@dropbear[0].PasswordAuth='on' && uci commit dropbear && /etc/init.d/dropbear restart
ssh -i ~/.ssh/brain-edge -p 38368 -o BatchMode=yes root@198.51.100.1 'echo STILL-OK'
```

**执行结果(已验证)**:

| 动作 | 命令 | 验证 |
| --- | --- | --- |
| 关闭口令登录 | `uci set dropbear.@dropbear[0].PasswordAuth='off'` | 口令登录返回 `Permission denied (publickey)`,不再提示密码 |
| 绑定 LAN | `uci set dropbear.@dropbear[0].Interface='lan'` | 38368 只监听 `198.51.100.1` 与 br-lan 的 IPv6,不再监听 `0.0.0.0` |
| 封 ttyd 的 mesh 入口 | fw4 规则 `src=EasyTier dst_port=7681 REJECT` | 落在 `input_EasyTier` 链、位于 `jump accept_from_EasyTier` **之前** |
| 保留回滚通道 | ttyd 7681 对 LAN 仍 200 | 服务器 侧 `curl 198.51.100.1:7681` → 200 |

**关于"laptop 还能不能 SSH 进路由"**(2026-10-18 更正):

`Interface='lan'` 的语义是 **绑定到 LAN 的地址,而不是绑定到网卡** —— 这是 dropbear init 脚本
自己打印的日志原文:`Option 'Interface' binds to address(es) but not to interface`。
因此 laptop 经 `proxy_network` 访问 `198.51.100.1:38368` 时,目的地址正是被绑定的地址,**仍然可连**;
`input_EasyTier` 链里也只封了 7681,没有封 38368。从 tun0 源地址发起的连接测试也显示 TCP 可达
(认证失败退出码 1,而非不可达的 255)。

结论:**laptop 侧不需要额外开洞,只需要一把被授权的公钥**(口令登录已关闭)。
只有当将来改用 `DirectInterface='EasyTier'`(真正按网卡绑定)或给 38368 加 zone 级 REJECT 时,
才需要改成两跳(服务器 → 路由)。

**laptop 侧接入**:

```bash
# ~/.ssh/config
Host router
  HostName 198.51.100.1
  Port 38368
  User root
  IdentityFile ~/.ssh/id_ed25519
  ServerAliveInterval 30
```

自助判断:`nc -vz 198.51.100.1 38368` 通 = 网络可达;`ssh -p 38368 root@198.51.100.1`
返回 `Permission denied (publickey)` = 网络通、只缺 key。

**残余风险(已知未处理)**:

- 公司 Win11 仍是 mesh 成员,可经 `proxy_network` 访问 **服务器:8790**(靠 token 挡)和整个局域网的
  非管理端口;转发流量被 `easytierzone.masq=1` 做了 NAT,所以 LAN 侧的按源地址 ACL 看不到它。
- ttyd 7681 对 LAN 仍是 `/bin/login` + root 口令,口令我未改动(它会同时影响你的交互式登录习惯)。
- `/etc/easytier/config.toml` 里 `network_secret` 是明文;拿到路由 root 就等于拿到 mesh 入场券。

**一键回滚**:

```bash
uci set dropbear.@dropbear[0].PasswordAuth='on'
uci delete dropbear.@dropbear[0].Interface
uci delete firewall.@rule[17]     # 或按 name 定位
uci commit dropbear; uci commit firewall
/etc/init.d/dropbear restart; /etc/init.d/firewall reload
```

**部署通道已就绪**:服务器 → 路由走 key,不需 TTY、不需口令、可脚本化:

```bash
scp -i ~/.ssh/brain-edge -P 38368 -O docker-compose.yml root@198.51.100.1:/mnt/mmc2-4/brain-edge/
rsync -a --delete -e 'ssh -i ~/.ssh/brain-edge -p 38368' harness-brain/ root@198.51.100.1:/mnt/mmc2-4/brain-replica/
```

## 11. 各 harness 适配

| harness | 指令注入 | 技能 | 接入方式 |
| --- | --- | --- | --- |
| DSH | `$DSH_HOME/AGENTS.md` | `$DSH_HOME/skills/brain/` | `install.sh dsh`;有 `dsh-mcp-client`,v2.3 可加 MCP |
| Octop | agent 指令 | `<workspace>/skills/brain/` | `install.sh octop <dir>`;它的 per-agent memory 保持"内账" |
| Pi | `~/.pi/agent/AGENTS.md` | `~/.pi/agent/skills/` | `install.sh pi`;`mcp.json` 可接 MCP |
| 新 harness | 只要有 AGENTS.md 或系统提示 | 同格式则复用 | 两行配置 |

**已核实的落地路径**(2026-10-18 现场确认):

| harness | 指令文件 | 技能目录 |
| --- | --- | --- |
| DSH | `$DSH_HOME/AGENTS.md` | `$DSH_HOME/skills/<name>/` |
| Octop | 该 agent 的指令(控制台里配置) | `<workspace>/skills/<name>/SKILL.md` |
| Pi | `~/.pi/agent/AGENTS.md` | `~/.pi/agent/skills/<name>/` |

接入命令形如:

```bash
./tools/install.sh octop <harness 的 skills 目录>
```

写入前确认该目录的属主与权限;共享库本身只需给运行 agent 的账号**读**权限(ACL)。

**Octop 的关系**:它的 `memory.sqlite` 是"内账",brain 是"公共账"。不强行统一,
只加一个单向同步:定期把 Octop 内账里的稳定结论导出成 atom(`kind` 保留、`source: octop-export`),
避免双写冲突。

---

## 12. 迁移(v1 → v2)

1. `git switch -c v2` 在分支上做,保留可回滚。
2. `tools/migrate-v1.py`(v2.0 交付物,**尚未实现**)。**切分规则必须按文件结构自适应**,
   否则会把事实库搬空 —— 实测 v1 的 `facts/*` 与 `hosts/*`(除 router 那份)**没有 `##`**,只有顶层 `- `:

   | 源文件形态 | 切分方式 |
   | --- | --- |
   | 有 `##` 小节 | 每个 `##` 一条 atom |
   | 无 `##`,有顶层 `- ` | **每条 `- ` 一条 atom**(`facts/*` 26 条、其余 hosts 16 条) |
   | 表格 | **每条数据行一条**,表头信息并入 body 首行(表头只当上下文,不单独成条) |
   | 只有段落 | 每段一条 |

   **实测基线 90 条**(脚本 `--dry-run` 逐文件可见):小节标题存进 atom 的 `section` 字段,
   列表项与表格行各成一条,`##` 之前的导语(含 `>`)单独一条。

   脚本自带**逐行覆盖检查**:源文件里每个实质行都必须出现在某条 atom 的 `body` 或 `section` 里,
   缺一行就 exit 1。这比「数 atom 个数」或「断言 `##` 全覆盖」诚实得多 —— 评审实测过,
   只看条数会让表格丢失(部署清单表)而照样通过验收。
   字段:`occurred_at = 文件 mtime`(**`time_suspect: true`**,mtime 不是事件时间)、
   `received_at = 迁移时刻`、`source: v1-migration`、`confidence: medium`、`author: {nas,migrate,assistant}`。
3. v1 文件移到 `views/legacy/` 只读保留一轮,确认视图渲染无误后删除。
4. 视图重新生成后**逐条 diff**,确保没有丢内容。迁移脚本必须输出对照表并自检:

```bash
./tools/migrate-v1.py --dry-run     # 输出:文件 → atom 数 → 正文字节数
./tools/migrate-v1.py --apply
./tools/migrate-v1.py --verify      # 断言:①atom 数 == dry-run 预期 ②无重复 id
                                    # ③逐文件正文字节精确相等(==,不是 >=)
                                    # ④逐行覆盖 100%(缺一行即失败)
```

---

## 13. 运维

- **备份**:`git push` 到私有远端 + consolidate 前 `btrfs snapshot`。git 的价值是审计与回滚,
  不是同步。
- **定时**:每日 `reindex` + `git commit`;每周 `stale` 报告(过期项清单)写到 `journal/` 并在 `brief` 里提示。
- **降级**:serve 挂 → 端侧读缓存 brief、写 outbox;服务器 挂 → 各端继续写本地 outbox,
  恢复后重放(幂等)。
- **观测**:`/v1/health` 暴露 atom 数、视图 revision、outbox 积压数。
- **时钟**:`occurred_at` 用客户端时间(离线保真),`received_at` 用服务端权威时间;
  两者差 > 5min 时置 `time_suspect: true` 并审计告警,**不静默改值**。
- **边缘副本(暂缓,属过度设计)**:原计划 服务器 每 6h `rsync` 只读副本到路由。评审认为它与
  "宁可不读、不可读过期"的原则、路由 flash 无快照、以及当前规模都不匹配,**移入未来可选**;
  当前只保留端侧 last-known-good brief。要做时按 `--exclude state --exclude index --exclude others` 复制。
- **入口单点**:边缘节点挂 = 远端全断(组网进程在边缘上,mesh 随之消失),但局域网内直连服务器不受影响。
  这不可消除,只能接受;端侧 outbox 保证不阻塞工作。
- **网络命名**:阶段一直连 `http://192.0.2.10:8790`;只有当 §10.4 的边缘反代真正启用后,
  才把 `brain` 指向 `198.51.100.1`(`/etc/hosts` 或边缘 DNS)。**先有代理再改名字**,否则 `http://brain:8790`
  指向一个空端口。
- **备份内容边界**:git 收 `atoms/ views/ journal/ profiles/ secrets/*.age recipients.txt **tombstones.jsonl**`;
  `state/`(token 哈希、审计)、`index/`、端侧 outbox 一律 `--exclude`。
  `btrfs snapshot` 前先 `findmnt -T /srv/brain` 确认确实在 btrfs 上(当前 `/srv` 是 btrfs)。
- **恢复演练(必须做一次,否则备份等于没有)**:①`kill -9` serve → CLI 写 outbox → 重启 → 确认重放恰好一次;
  ②删掉 `index/` → `brain verify` 报错 → `reindex` 恢复;③断 mesh → `brief` 返回 stale 而不是旧事实。

---

## 14. 失败模式清单

| 失败 | 表现 | 处理 |
| --- | --- | --- |
| 挂载/mesh 抖动 | 端侧请求超时 | 缓存 brief + outbox 重放 |
| 双端离线写同一事实 | 两条 atom 冲突 | 视图并列 + `conflicts` 报告,人工裁决 |
| 时钟漂移 | 视图排序错误 | `occurred_at` + `received_at` 双时间戳;差 >5min 标 `time_suspect` |
| 合并写坏 | 视图错乱 | 视图是派生,重建即可;atoms 不可变 |
| 私钥丢失 | 该端无法解密 | 多收件人:用其他端重新加密给新公钥 |
| 明文泄漏进 transcript | 会话日志含密码 | 只能靠 `exec` 纪律 + 事后轮换该密钥 |
| 服务器 单点 | 全端失联 | 本地 outbox 保底,不阻塞工作 |
| 边缘节点重启 | 远端短暂全断,LAN 内正常 | 端侧重试 + 路由容器 `restart: unless-stopped` |
| 服务器 重启/升级 | 所有端写失败 | 端侧 outbox;可选边缘只读副本提供 stale 读 |
| 路由 SSH 暴露到 mesh | 管理面被扫描 | 已关闭口令登录 + 按地址绑定;mesh 仍可达但只认公钥 |
| 副本过期被当真相 | agent 复述旧事实 | 强制 `X-Brain-Stale` 头;客户端必须提示"快照过期" |
| 端 token 泄漏 | 持有 write 者可逐条 forget,把整个库「隐藏」掉 | 不提供 batch;单条 forget 记审计;必要时 retract 需 服务器 本地确认 |
| mesh 成员非可信 | 公司 Win11 等设备可达路由全部端口 | 38368 收回 LAN;口令登录改公钥;8790 也要 token |
| 路由 root 口令泄漏 | 旧记忆文件里存过明文 | 关闭 PasswordAuth **只降低了 SSH 面**的风险;LuCI(80/443)+ 同一口令仍对 mesh 开放,风险**未消除**(见 ADR-0011) |
| LuCI 被拿到口令 | mesh 成员可改启动项/加 authorized_keys,绕过 SSH 加固 | 已记录的接受风险;要消除需把 80/443 收到 br-lan 或换强口令 + 2FA |

---

## 15. 决策状态(原"开放决策")

| # | 议题 | 状态 | 结论 / 待定 |
| --- | --- | --- | --- |
| 1 | EasyTier 直连还是中继 | **已决** | P2P 直连,laptop 8.8ms / 0% loss(`easytier-cli peer` 实测) |
| 2 | 四个端的角色分配 | **默认已定,可覆盖** | `nas-dsh=assistant`、`nas-octop=ops`、`mac-dsh=engineer`、`mac-pi=engineer`;未配时按 host+harness 推导,`whoami` 显示 `role_source` |
| 3 | 公司 Win11 是否留在 mesh | **已决:保留** | 靠 8790 的 token + LuCI 口令挡;残余风险已在 §14 记录 |
| 4 | 是否收回 38368 / 关口令登录 | **已执行** | `PasswordAuth=off` + `Interface=lan`;实测口令被拒、只监听 LAN 地址 |
| 5 | 是否需要 `brain` 固定名 | **后置** | 阶段一直连 `192.0.2.10`;启用边缘反代后再改 `brain` |
| 6 | 是否需要公网入口 | **未定** | 需要时按 §10.4 加域名 + TLS;当前不做 |

> 原文档此处编号重复(1–5 后又出现 3/4/5/6)且包含已执行项,已重排。
> 角色表已给默认值 —— v2.0 不再被"角色未定"阻塞;要改口径直接改这张表 + `profiles/`。

## 15.5 实现状态(2026-10-18)

| 期 | 状态 | 证据 |
| --- | --- | --- |
| **v2.0 数据层** | ✅ **已上线** | `atoms/` 90 条(迁移自 v1,逐行覆盖 100%);`views/` 可重建且幂等;`tombstones.jsonl` + `seq`;`brain verify` 五项全过;`brainlib/` 单测 22 项通过 |
| **v2.1 serve** | ✅ **已实现** | `brain serve`(stdlib,ThreadingHTTPServer)+ 令牌签发/吊销(加盐哈希) + 11 个端点 + 客户端模式(`BRAIN_URL`/`config.toml`)+ 离线 outbox 重放;57 项单测通过(含 401/403/409/413、身份取自令牌、遗忘后不可见、路径穿越/类型校验/XSS/CSRF) |
| **部署** | ✅ | 库在 `/srv/brain`(SMB 共享 `harness-brain`);systemd `brain.service`(User=brain,`0.0.0.0:8790`,enabled+active);laptop 客户端 `bundle/brain.pyz`(123KB 单文件);令牌写入 `MAC-SETUP.txt`(600) |
| **人读界面 v2.2** | ✅ | 服务端渲染(零依赖)管理台:`/` 概览 · `/memories` · `/search` · `/atom/<id>` · `/topics` · `/topic/<tag>` · `/review` · `/conflicts` · `/rules` · `/new` · `/tokens` · `/activity` · `/system` · `/login`;写动作按 scope 限制,审计落 `state/audit.jsonl`;57 项单测通过 |
| v2.2 密钥/角色 | ⏳ | `profiles/` 三份已建,尚未注入 |
| v2.3 检索/MCP | ⏳ | |

v1 的 `facts/`、`hosts/`、`rules.md` **暂时保留**作为迁移源与审计对照,v2.1 时移到 `legacy/v1/`。

## 16. 分期

| 期 | 内容 | 可验收(命令级,详见 [`ROADMAP.md`](ROADMAP.md)) |
| --- | --- | --- |
| v2.0 | atoms/views/tombstones + role 字段 + 迁移脚本 + `whoami`/`verify` | 迁移后条数一致、视图重建幂等、`brain verify` 五项全绿 |
| v2.1 | `brain serve` + 令牌 + brief/search/recall + `/v1/stale` `/v1/conflicts` | laptop 不挂载能 `brief`;幂等测试 ALL PASS;72h 无内存增长 |
| v2.1.5(可选) | 边缘反代 | 仅公网入口/固定命名时需要;直连已够 |
| v2.2 | 密钥 add/exec + profile 渲染(persona 进 brief 首段) | 密文入库、exec 五个泄漏面各有测试命令 |
| v2.3 | FTS bigram + MCP 暴露 | 中文检索命中;`EXPLAIN QUERY PLAN` 走 FTS |
| 独立项 | 路由安全基线(已完成) | 见 §10.5 |

**每一期都必须附带"文档同步",否则下一期就是在给过期文档写代码**:

| 期 | 文档交付物 |
| --- | --- |
| v2.0 | 改写 `skills/brain/SKILL.md` 的写入协议(inbox → remember);`AGENTS.md` 加 `secret-ref`;`install.sh` 的协议文本改为从 `skills/brain/PROTOCOL.md` 生成,禁止第二份手写 |
| v2.1 | `SPEC-api.md` 中标为"已实现";`README.md` 重写为 v2 部署步骤 |
| v2.2 | `secrets/README.md`(轮换记录);§9.1 吊销流程走一遍演练 |
| v2.3 | `bin/brain --version` 与服务端版本协商写进 SPEC |

## 16.5 相关文档

| 文档 | 作用 |
| --- | --- |
| [`ADR.md`](ADR.md) | 14 条架构决策:为什么这样、否决了什么、后果是什么 |
| [`ONBOARDING.md`](ONBOARDING.md) | **交付给新 harness 的自包含接入书**(拿到就能用,不需要读本仓库) |
| [`SPEC-api.md`](SPEC-api.md) | 接口级规范:端点、错误码、幂等语义、令牌生命周期、验收脚本 |
| [`ROADMAP.md`](ROADMAP.md) | 分期路线图,每期都有可执行命令作为验收标准 |
| [`TERMS.md`](TERMS.md) | 术语表,防止「端/harness/角色」「inbox/outbox」混用 |
| [`../examples/SKILL.md`](../examples/SKILL.md) | 给 agent 的读写协议 |
| [`../README.md`](../README.md) | 安装、部署与网络拓扑 |

## 17. 附录:参考部署形态

以下形态都在真实环境跑过,尺寸取自 `free -h` / `du -sh` 的实际量级。

| 角色 | 规格量级 | 说明 |
|---|---|---|
| 服务端(主) | 2 核 / 2 GB 可用内存 / 任意 Linux + python3.12 | `brain serve` 常驻;数据在本地磁盘 |
| 客户端 | 只要能跑 shell + 读文件 | 单文件 `brain.pyz`,零第三方依赖 |
| 边缘(可选) | 512 MB 内存 / 无 python3 | 只做反向代理或只读镜像,**不写库** |
| 存储 | 起步 < 100 MB | 1 万条 atom 约 40 MB(含视图) |
| 备份 | 与数据同量级 × 2 | `git bundle` + 加密归档 |

留白纪律:文档、代码、示例里**不出现任何真实主机名、内网地址、个人路径**。
示例统一用 `server` / `laptop` / `ci` 三个占位角色,网段统一用 RFC 5737
(`192.0.2.0/24`、`198.51.100.0/24`、`203.0.113.0/24`)。这一条由
`tools/privacy-scan.py`(通用规则,随仓库公开)与 `tools/lexicon-scan.py`
(私有词表,不进仓库)双重把关。
