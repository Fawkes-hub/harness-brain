# 版本管理策略

> 目标:任何一端看到的版本号只有一处真相,任何一次发版都能被机器复现。

## 1. 单一版本来源

| 位置 | 作用 | 是否手改 |
|---|---|---|
| `brainlib/__init__.py` 的 `__version__` | **唯一版本来源** | ✅ 只有这一处 |
| `brainlib/server.py` 的 `SERVER_VERSION` | 由 `from . import __version__` 派生 | ❌ 不要手改 |
| `bin/brain --version` | 打印 `brain <__version__>` | ❌ |
| `GET /v1/health` 的 `server` 字段 | 服务端回报同一字符串 | ❌ |
| `CHANGELOG.md` 最新版本节标题 | 必须等于 `__version__` | ✅ 发版时同步 |

验收口径(可脚本化,已进 `tools/docs-check.py`):

- `brain --version` 的输出 `== /v1/health.server`
- 仓内除 `brainlib/__init__.py` 与 `CHANGELOG.md` 外,不出现形如 `"2.2.1"` 的版本字面量
- `CHANGELOG.md` 最上面的版本节 `== __version__`

## 2. 版本号语义

`MAJOR.MINOR.PATCH`,但这里的「兼容」比框架库更严,因为**库里有用户十年的记忆**。

| 位 | 什么时候加 | 例子 |
|---|---|---|
| MAJOR | 数据格式或语义不向后兼容:atom 字段增删改名、`id` 规则变更、hash 口径变更、tombstone 语义变更、`/v1` 端点删除或语义反转 | 日志式存储替代 atoms(已否决,若发生即 3.0) |
| MINOR | 纯增量:新端点、新子命令、新视图、新可选 frontmatter 字段、新 Transport | `secret`、SQLite 检索、MCP |
| PATCH | 无 API/数据面变更:bug 修复、性能、文档、仓库卫生、内部重构 | 2.2.1 |

补充规则:

- **只加不减**:`/v1` 上已发布的端点不得删除,只能标记 deprecated 并在下一个 MAJOR 移除。
- **hash 口径变更一律 MAJOR**,且必须带迁移脚本 + `verify` 全过,否则全网客户端的幂等键会集体漂移。
- 客户端与服务端用 `min_client` 双向协商:服务端声明「最低可连客户端版本」,低于它直接回 426。
- 同一 MINOR 内的 PATCH 不得要求客户端升级。

## 3. 提交信息

`<type>(<scope>): <一句话>`,`type ∈ {feat,fix,refactor,docs,test,chore,perf,build,ci}`。
正文写「为什么」,不写「改了哪些行」——diff 自己会说。

需要进 CHANGELOG 的类型:`feat`、`fix`(含**数据/行为**层面的 fix),以及任何 `BREAKING CHANGE`。
`chore/docs/test/ci` 一般不进 CHANGELOG,除非影响使用者。

## 4. 发版流程

```bash
# 1) 改版本号 + 写 CHANGELOG(把 [Unreleased] 内容整段搬到新版本节)
#    brainlib/__init__.py: __version__ = "2.3.0"
# 2) 门禁自检(不写任何东西)
python3 tools/release.py
# 3) 打 tag(annotated,带发版说明)
python3 tools/release.py --apply
# 4) 推送(有远端之后)
git push origin main && git push origin v2.3.0
```

`tools/release.py` 会依次验证:在 `main` 上 → 工作区干净 → 版本号与 CHANGELOG 一致 →
单测 → `docs-check` → `brain verify` → tag 是否已存在且是否指向 HEAD。
**任一项不过就不打 tag**;`--apply` 只是最后一步。

若同名 tag 已指向 HEAD:说明该版本已发过,脚本以 0 退出并提示「无待发内容」——
这是防止「改了代码忘了提版本号又打一次同名 tag」的护栏。

## 5. 分支与 tag

- 长期分支只有 `main`,保持随时可部署。
- 发版用 tag,不开 `release/*` 分支;需要热修就在 `main` 上 PATCH 一次。
- tag 一律 annotated(`git tag -a`),message 头行写 `vX.Y.Z —— <一句话定性>`,正文写影响面与升级指引。
- tag 只增不改:**已推送的 tag 绝不 force-move**。发错了就 PATCH 一个修回去。

## 6. 版本与数据的关系

版本号管**代码**,不管**库里的内容**。库内容另有两条独立水位:

- `views/.revision`(`sha256`)—— 派生视图的一致性水位,`reindex --force` 刷新。
- `tombstones.jsonl` 的 `seq` —— 单调写水位,`brief --since` / outbox 重放 / 后续联邦都靠它。

升级前后必跑 `brain verify`(五项),它校验的是数据自洽,与代码版本无关;
但**跨 MAJOR 升级必须跑迁移脚本,并在迁移前后各做一次快照**。
