# brain serve · 接口规范(v1 草案)

> 配套 [`DESIGN-v2.md`](DESIGN-v2.md) §7 / §10。目标是让实现者不需要再问一轮问题。
> 状态:草案。未实现。改动此文件需同步 `bin/brain` 的客户端实现。

---

## 1. 通用约定

| 项 | 约定 |
| --- | --- |
| 基址 | `http://<nas>:8790` |
| 编码 | JSON,`Content-Type: application/json; charset=utf-8` |
| 认证 | 每个请求带 `Authorization: Bearer <token>`(健康检查除外) |
| 时间 | ISO 8601 带时区偏移,如 `2026-10-18T09:12:33+08:00`;服务端为权威时钟 |
| 幂等 | `remember` / `journal` / `secret.exec` 幂等(见 §5);`forget` 是**追加语义,天然不幂等**(重复调用产生重复行,语义等价),唯一例外 |
| 重试 | 客户端连接超时 3s、读超时 30s;5xx 与超时可重试,4xx 不可 |
| 缓存 | 所有响应带 `Cache-Control: no-store`(记忆宁可读不到,不可读过期,见 ADR-0009) |
| 版本头 | 响应带 `X-Brain-Version: <semver>` |

**错误体统一**:

```json
{ "error": { "code": "unauthorized", "message": "token missing or invalid" } }
```

| code | HTTP | 含义 |
| --- | --- | --- |
| `unauthorized` | 401 | 无 token / token 无效或已吊销 |
| `forbidden_scope` | 403 | 令牌缺少该端点所需的 scope(见 §2.1);`forbidden_role` 仅用于角色级限制 |
| `bad_request` | 400 | 字段缺失、长度超限、kind 非法 |
| `not_found` | 404 | atom / secret / 文件不存在 |
| `conflict_id` | 409 | 同一 id 已存在且内容**不同**(见 §5) |
| `too_large` | 413 | 单条 body > 64KB |
| `budget_too_small` | 413 | `brief` 的 budget 连 persona+rules 都放不下(见 §3.4) |
| `token_expired` | 401 | 令牌已过期,需重新签发 |
| `version_skew` | 426 | 客户端版本低于 `min_client` |
| `internal` | 500 | 未预期错误(详情只进日志,不回给客户端) |

---

## 2. 身份与令牌

- 令牌在 NAS 侧签发:`brain token issue --host mac --harness pi --role engineer --scope read,write --ttl 365d`
  **scope 全集**:`read` / `write` / `secret` / `admin`(逗号分隔;`admin` 隐含 `write`,`secret` 不隐含 `read`)
  → 打印一次明文,`state/tokens.json` 存 `sha256(salt || token)` + 前缀 + 身份 + `expires`。
  **必须加盐**(无盐哈希对短 token 可查表反推),盐随 `--init` 生成并存在同一文件里。
- 服务端**从令牌反查身份**,请求体里的 `author` 一律忽略(客户端自报无效)。
- 响应带 `X-Brain-Identity: mac/pi/engineer`。
- 吊销:`brain token revoke <token_id>`;轮换:吊销 + 重新签发,旧令牌立即失效。
- **过期**:超过 `expires` → `401 token_expired`(不是 `unauthorized`,便于客户端区分"该换票"和"配错了")。
- 端侧配置**只有一个文件** `~/.config/brain/config.toml`(0600),不单独放 token 文件:

```toml
url   = "http://192.0.2.10:8790"
token = "bk_..."
role  = "engineer"
```

优先级:`环境变量 > config.toml > 报错`(不猜默认服务器)。

### 2.1 端点 → scope 映射

| 端点 | 需要的 scope |
| --- | --- |
| `GET /v1/health` | 无(免认证) |
| `GET /v1/client` | 无(免认证,只回客户端二进制,不含任何记忆数据) |
| `GET /v1/whoami`、`/v1/brief`、`/v1/search`、`/v1/recall`、`/v1/stale`、`/v1/conflicts` | `read` |
| `POST /v1/remember`、`POST /v1/journal` | `write` |
| `POST /v1/forget`(恢复也用同一个端点,`status: active`)| `write`(见下方「记忆层 DoS」) |
| `POST /v1/secret/*/exec` | `secret`(默认关闭,仅 unix socket) |
| `POST /v1/reindex` | `admin` |

> **记忆层 DoS**:任何持有 `write` 的端都能 `forget` 任意 atom。泄漏一把端 token 就能逐条隐藏整个库。
> 缓解:`retract` 必须带 `replaced_by`(不能凭消失);forget 请求体只有单 id,**不提供 batch**;
> 服务端对 forget 做速率限制(默认 60 次/分钟)并逐条记审计;DESIGN §14 已登记该风险。

---

## 3. 端点

### 3.1 `GET /v1/health`(免认证,只读)

```json
{ "ok": true,
  "version": "2.1.0",
  "min_client": "2.0.0",
  "api": 1,
  "features": ["brief", "search", "recall", "forget", "conflicts"],
  "atoms": 128,
  "views_rev": "sha256:ab12…",
  "last_write": "2026-10-18T09:12:33+08:00",
  "last_rebuild": "2026-10-18T09:12:33+08:00",
  "dirty": false }
```

版本策略(唯一口径):`X-Brain-Client` 低于 `min_client` → **写操作返回 426 `version_skew` 并拒绝**,
读操作只警告不阻断。理由:读写不兼容会写坏数据,读不兼容最多是少几个字段。

### 3.2 `GET /v1/whoami`

```json
{ "host": "mac", "harness": "pi", "role": "engineer",
  "role_source": "token",            // token | derived
  "scopes": ["read","write"],
  "server_version": "2.1.0",
  "profile_found": true }
```

`role_source` 用来一眼看出"我以为我是 engineer,但服务端按 token 认成 assistant"这类配错。

### 3.3 `POST /v1/remember`

```json
{ "id": "m_7f3a91c4e",
  "kind": "fact",
  "tags": ["network", "proxy"],
  "confidence": "high",
  "source": "curl 实测",
  "supersedes": "m_4c1e02ab12",
  "ttl": "2027-04-18",
  "body": "代理地址 …",
  "occurred_at": "2026-10-18T09:12:30+08:00" }
```

**必填**:`id`、`kind`、`body`。`id` 必须匹配 `^m_[0-9a-hjkmnp-tv-z]{10}$`(Crockford base32 小写,10 位)。

**响应**:

| 情形 | HTTP | 体 |
| --- | --- | --- |
| 新增 | 201 | `{"id": "...", "stored": true, "occurred_at": "...", "received_at": "...", "views_rev": "sha256:..."}` |
| 同 id 且全字段一致(重放) | 200 | `{"id": "...", "stored": false, "reason": "idempotent_replay"}` |
| 同 id 不同内容 | 409 | `{"error":{"code":"conflict_id"}}`,`reason` 里给出冲突字段 |
| 同 author 30s 内同 hash | 200 | `{"stored": true, "duplicate_of": "<id>", "keep_local": true}` — 只是提示,**不删数据** |

**服务端动作**:校验 → 写 `atoms/<yyyymm>/<id>.md`(临时文件 + `fsync` + `rename`,原子)
→ 按 DESIGN §4.3 重建视图 → 写审计行。

**幂等键只有 `id`。** `hash = sha256(规范化(kind|sorted(tags)|ttl|supersedes|body))`,
算法标记 `hash_algo: sha256-v1`;**hash 只用于"疑似重复"提示,不用于判重**。
跨 author 的一律不判重 —— 两端离线各写同一事实必须保留两条(见 DESIGN §6.1 的警告)。

### 3.4 `GET /v1/brief?budget=6000&index_rows=12`

返回**纯文本**(不是 JSON),因为它的用途就是直接喂给模型:

```
# 【共享记忆库 · 角色】
<profiles/<role>.md 渲染>

# 【共享记忆库 · 规范】
<views/rules.md>

# 【共享记忆库 · 事实索引】
<INDEX 前 N 行>

# 【共享记忆库 · 主机笔记】
- nas-dsh: …
```

顺序即优先级;超 `budget` 时**从尾部整段丢弃**(绝不拦腰截断,见 DESIGN §7.1)。
响应头:`X-Brain-Budget-Used`、`X-Brain-Sections-Dropped`。

- **硬下限**:连 persona + rules 都放不下 → `413 budget_too_small`(不返回残缺摘要)。
- **期次**:`角色` 段自 **v2.2** 起才返回;v2.0/v2.1 的 brief 只有规范/索引/主机笔记,
  对应验收也按"无角色段"写(见 ROADMAP),否则 v2.1 会因一个 v2.2 的段落而永远验收不过。

### 3.5 `GET /v1/search?q=&limit=20&include_deprecated=false`

```json
{ "query": "代理",
  "hits": [ { "id": "m_7f3a…", "path": "atoms/2026-10/m_7f3a….md",
              "occurred_at": "...", "received_at": "...", "score": 3.2,
              "snippet": "代理地址 http://…", "author": "nas/dsh/assistant" } ] }
```

中文检索必须 bigram 化(ADR-0010);实现细节见 DESIGN §8.2。

### 3.6 `GET /v1/recall?since=7d&topic=&limit=20`

返回 journal 摘要 + 相关 atom,**按时间倒序**。用途是"上次聊到哪",不是事实查询。

```json
{ "since": "7d",
  "entries": [ { "occurred_at": "2026-10-16T22:00:00+08:00", "author": "mac/dsh/engineer",
                 "did": "讨论换机箱", "conclusion": "只换散热器",
                 "todo": ["比价 emachines V500"], "open": ["预算上限未定"] } ] }
```

### 3.7 `POST /v1/journal`

```json
{ "id": "j_9a1c…", "did": "...", "conclusion": "...",
  "todo": ["..."], "open": ["..."], "occurred_at": "..." }
```

一条会话一次。幂等语义同 remember。

### 3.8 `POST /v1/secret/{name}/exec`

```json
{ "argv": ["curl", "-sS", "http://192.0.2.10:8085/api/v2/app/version"],
  "env_name": "QB_PASS",
  "timeout_s": 30 }
```

服务端解密 `secrets/<name>.age` → 只注入子进程**环境变量**(`env_name`,默认取 secret 名大写)
→ 执行 → 返回 `{"exit_code":0,"stdout":"...","stderr":"..."}`。

**默认关闭**:该端点在 HTTP 层面默认不注册。只有以下条件全部满足才开放,并在 `health.features` 里体现:

1. 客户端来自 **unix socket**(`/run/brain.sock`,0600,属主 `brain`)或显式配置的 loopback;
2. 该 secret 在 `secrets/<name>.meta.json` 里声明了 `allowed_commands`(前缀白名单);
3. 令牌带 `secret` scope。

**硬要求**(逐条对应 DESIGN §9.2 的五个泄漏面):

| 要求 | 实现方式 |
| --- | --- |
| 明文不进 argv | 服务端只注入环境变量;`argv` 中出现明文 → 400 拒绝 |
| 明文不出现在响应其他字段 | 响应只回 `exit_code/stdout/stderr` |
| stdout/stderr 脱敏 | 回传前把明文替换成 `***`,并记一条"发生了脱敏"的审计 |
| 不进服务端日志 | 日志只记 secret 名与 exit_code,不记 argv 与输出 |
| 超时与输出上限 | `timeout_s` 默认 30、上限 300;stdout/stderr 各截断到 64KB |

### 3.8b `POST /v1/forget`

```json
{ "id": "m_7f3a91c4e",
  "status": "deprecated",          // deprecated | retracted | active(恢复)
  "reason": "代理已更换",
  "replaced_by": "m_5c1d2e3f4"     // 仅 status=retracted 时必填,且目标必须存在
}
```

追加一条 tombstone 到仓库根的 `tombstones.jsonl`(DESIGN §4.5),**atom 文件零 diff**。
`status` 取 `deprecated` | `retracted` | `active`(恢复)。

| 情形 | HTTP | 体 |
| --- | --- | --- |
| 正常追加 | 201 | `{"seq": 42, "status": "deprecated"}` |
| 重复 forget(已是该状态) | 200 | `{"seq": 42, "status": "deprecated", "noop": true}` |
| id 不存在 | 404 | `{"error":{"code":"not_found"}}` |
| `retracted` 但没带 `replaced_by` | 400 | `{"error":{"code":"bad_request"}}` |
| `replaced_by` 指向不存在的 atom | 400 | 同上(不允许"凭消失") |

`seq` 由服务端单调分配,是所有状态判定的排序依据;恢复就是再追加一条 `status: active`。

### 3.8c `GET /v1/stale?within=30d`

```json
{ "items": [ { "id": "m_…", "ttl": "2026-10-01", "topic": "network", "days_overdue": 17 } ] }
```

供 `brain stale` 与每周报告使用。`ttl` 在查询时判定,时区固定 `Asia/Shanghai`。

### 3.8d `GET /v1/conflicts`

返回"同主题下未声明 supersedes 却结论不同"的组,以及断链/环/多尾(见 DESIGN §4.3 折叠算法)。

### 3.8e `GET /v1/client`

免认证,返回单文件客户端 `bundle/brain.pyz`(`application/zip`),用于端侧自举。
**只回代码,不含任何记忆数据**;没有 bundle 时 404。
端侧:`curl -fsSL http://<nas>:8790/v1/client -o ~/.local/bin/brain.pyz && chmod +x $_

### 3.9 `POST /v1/reindex`

重建 `views/` 与 FTS。返回 `{"views_rev": "...", "atoms": 128, "duration_ms": 42}`。运维用途,不需常规调用。

---

## 4. 视图重建与崩溃一致性

- 写入成功后**同步**重建(数百条 <50ms);重建失败不使写入失败,只置 `dirty=true` 并下次重试。
- 重建是幂等的纯函数:同样的 atoms 必须产出逐字节相同的 views。
- 进程崩溃可能出现"atom 已落盘、views 未更新":启动时检查 `views_rev` 与 atoms 哈希,不一致就全量重建。
- 全量重建 500 条约 100ms,可以放心在启动时无条件执行一次。

---

## 5. 幂等语义(实现时必须逐条测试)

| 场景 | 期望 |
| --- | --- |
| 同 id、同内容,重放 N 次 | 只存在 1 条,返回 200 `idempotent_replay` |
| 同 id、不同内容 | 409 `conflict_id`,不覆盖(调用方应换 id) |
| 不同 id、同内容(两端各写一次) | **保留两条**,返回里带 `duplicate_of` 提示,**任何一侧都不许丢** |
| 离线队列乱序重放 | 与顺序无关,结果一致;`occurred_at` 保持原值 |
| 提交时进程崩溃 | 要么完整存在,要么不存在(rename 原子性) |
| 服务端重启后重放 | 结果与首次提交一致(视图 revision 相同)|

> 前 5 行可用 `tools/test-idempotency.sh` 经 HTTP 验证;「提交时进程崩溃」属进程级故障注入,由服务端单元测试覆盖,不在 HTTP 脚本里假装测过。

> 最后一行是这套设计里唯一的"正确性"承诺,必须用测试固定住。

---

## 6. 客户端(端侧)行为

```
1. 读 ~/.config/brain/config.toml(0600;env 变量优先,见 DESIGN §10.1)
2. GET /v1/health(1 次,缓存 60s)→ 版本比对
3. 任何写操作失败(连接/5xx)→ 追加 outbox.jsonl,退出码 0 但打印"已离线暂存"
4. 下次任一命令成功 → 先重放 outbox(逐条,失败即停,保证顺序)
5. 读操作失败 → 读本地 last-good brief(带 stale 标记),否则报错,不编造
```

`brain` 与服务端的版本不一致时:只警告,不阻断;`version_skew` 才阻断写。

---

## 7. 最小验收脚本(实现完成即可跑)

```bash
# 1 健康
curl -sf localhost:8790/v1/health | jq -e '.ok'
# 2 身份
curl -sf -H "Authorization: Bearer $T" localhost:8790/v1/whoami | jq -e '.role=="engineer"'
# 3 写入幂等
for i in 1 2 3; do curl -s -o /dev/null -w "%{http_code}\n" -X POST -H "Authorization: Bearer $T" \
  -d '{"id":"m_test000001","kind":"fact","body":"幂等测试","tags":["test"]}' localhost:8790/v1/remember; done
#   期望 201 200 200
ls atoms/*/m_test000001.md | wc -l     # 期望 1
# 4 中文检索
curl -sf -H "Authorization: Bearer $T" "localhost:8790/v1/search?q=幂等" | jq -e '.hits|length>0'
# 5 brief 预算
curl -s -o /dev/null -w "%{http_code}\n" -H "Authorization: Bearer $T" \
  "localhost:8790/v1/brief?budget=200"          # 期望 413(budget_too_small)
```
