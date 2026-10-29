# 变更记录

本文件遵循 [Keep a Changelog](https://keepachangelog.com/zh-CN/1.1.0/)，
版本号采用 [语义化版本](https://semver.org/lang/zh-CN/)。
数据格式与 API 的兼容边界见 `docs/SPEC-api.md` 与 `docs/DESIGN-v2.md`。

## [Unreleased]

### 修复

- `tools/release.py` 新增 `--check` 模式(只校验版本号与 CHANGELOG 一致、版本字面量唯一),
  CI 改用它。此前 CI 直接跑完整门禁,而门禁要求「在 main 上且 tag 不存在或不指向 HEAD」——
  于是**发版当天对 tag 的构建必然失败**(detached HEAD),之后的日常提交也必然失败。
- 门禁本身现在识别「发版验证模式」:detached HEAD 且 tag 指向当前提交时,跳过分支/工作区检查。

### 计划中

- v2.2 补齐：`secret add/exec`（age 多收件人）、persona 渲染进 `brief` 首段
- v2.3：SQLite + FTS5（中文 bigram）检索、`scan` 增量重建、`/v1/stats`
- v3.0：多库（namespace）、MCP Transport、`Store`/`Index` 接口化

## [2.2.1] - 2026-10-29

### 修复

- `atoms/` 中两条历史记录（`m_kz712vak38`、`m_rfnxwpa6da`）的 `content_hash` 与当前口径不符：
  hash 口径为 `kind|空格连接的排序 tags|ttl|supersedes|规范化 body`，这两条写于旧版客户端本地直写模式，
  误用逗号连接 tags。已用 `model.content_hash` 重算并断言正文/标签/关系未变，`brain verify` 五项恢复全过。
- `views/.revision` 与数据不一致时 `verify` 报错，已 `reindex --force` 修复。

### 变更

- **派生视图移出版本库**：`views/` 与 `INDEX.md` 不再跟踪，改为提交确定性生成的 `views.manifest`
  （每文件 sha256）。视图是 `atoms ⊕ tombstones` 的纯函数产物，可随时重建；此前每次 rebuild
  会改动数十至数百个文件，淹没真相层 diff，并使仓库体积随重建次数膨胀。
- `.gitignore` 重写：补 `__pycache__/`、`*.pyc`，按类别分区注释。

### 新增

- **单一版本来源**：版本号收敛到 `brainlib/__init__.py` 的 `__version__`，`server.py` 改为引用它；
  新增 `brain --version`。
- `.gitattributes`：统一换行策略，标记二进制类型。
- `CHANGELOG.md`（本文件）与 `docs/VERSIONING.md`（版本策略：语义边界、提交规范、发版流程）。
- `tools/release.py` 发版门禁：分支 / 工作区 / 版本一致 / 单测 / `brain verify` / tag 状态，
  全过才允许 `--apply` 打 annotated tag。
- git 钩子入库到 `.githooks/`（`core.hooksPath` 指向它）：此前钩子只在 `.git/hooks/` 里，clone 即丢失，等于「门禁只对我这台机器有效」。

### 运维

- 补提交 2026-10-18 之后 10 天未入库的记忆（`atoms/` 67 条、`tombstones.jsonl` 首条记录、docs 修订）。
- git 仓库配置 `gc.auto=0`、`maintenance.auto=false`，避免 auto-gc 撞在写路径上；主分支 `master` → `main`。

### 已验证

- `brain verify` 五项全过；`reindex --force` 连跑两次输出字节一致（渲染确定性成立）。
- 57 项单元测试通过。

## [2.2.0] - 2026-10-18

### 新增

- 服务端渲染的管理台（零依赖、零构建）：`/`、`/memories`、`/search`、`/atom/<id>`、`/topics`、
  `/topic/<tag>`、`/review`、`/conflicts`、`/rules`、`/new`、`/tokens`、`/activity`、`/system`、`/login`。
- 写动作按 token scope 限制；带 Origin 校验的 cookie 表单；审计落 `state/audit.jsonl`。
- 深浅色主题、`/` 聚焦搜索、危险操作二次确认。

## [2.1.0] - 2026-10-18

### 新增

- `brain serve`：stdlib `ThreadingHTTPServer`，`/v1/{health,whoami,remember,brief,search,recall,forget,stale,conflicts,client,reindex}`。
- 令牌签发/吊销（加盐哈希，明文只显示一次）；身份取自令牌而非请求体。
- 单文件客户端 `bundle/brain.pyz`；端侧离线 outbox 与自动重放（按 id 幂等）。
- systemd 部署单元与一键接入脚本。

## [2.0.0] - 2026-10-18

### 新增

- v2 数据层：不可变 `atoms/<yyyymm>/<id>.md`、派生 `views/`、追加式 `tombstones.jsonl`（单调 `seq`）。
- `brain verify` 五项自检；`reindex --force`；`forget` / `restore` 走 tombstone，atom 零改动。
- v1 → v2 迁移脚本（`tools/migrate-v1.py`，90 条逐行覆盖 100%）。

### 变更

- `id` 规则冻结为 `^m_[0-9a-hjkmnp-tv-z]{10}$`（Crockford base32）。
- 幂等键只认客户端生成的 `id`，`content_hash` 降级为「疑似重复」提示（ADR-0004）。
