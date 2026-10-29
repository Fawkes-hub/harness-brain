# 变更记录

本文件遵循 [Keep a Changelog](https://keepachangelog.com/zh-CN/1.1.0/)，
版本号采用 [语义化版本](https://semver.org/lang/zh-CN/)。
数据格式与 API 的兼容边界见 `docs/SPEC-api.md` 与 `docs/DESIGN.md`。

## [Unreleased]

### 计划中

- v2.2 补齐:`secret add/exec`(age 多收件人)、persona 渲染进 `brief` 首段
- v2.3:SQLite + FTS5(中文 bigram)检索、`scan` 增量重建、`/v1/stats`
- v3.0:多库(namespace)、MCP Transport、`Store`/`Index` 接口化

## [2.2.6] - 2026-10-29

### 修复

- **打了 tag 却没有 Release 页面**:`release.yml` 只推 GHCR 镜像,不建 GitHub Release,
  于是 v2.2.3~v2.2.5 有 tag 有镜像、点进 Releases 却看不到。现在打 tag 时自动
  从本文件抽取该版本小节建 Release(标题 = tag,正文 = 本节 + 镜像坐标),
  已存在则跳过,可安全重跑。

### 变更

- `release.yml` 增加 `contents: write`(建 Release 需要)。

## [2.2.5] - 2026-10-29

这一版是**按 README 从零走一遍**走出来的:凡是文档写了、实际跑不通的,全部修掉。

### 修复

- **`serve` 自动初始化空库后不建视图**:骨架建好了但 `views/.revision` 没写,
  `docker exec brain brain verify` 一上来就报「views/.revision 与数据不一致,需 reindex」。
  现在自动初始化后立刻 `rebuild()`,新库出生即「五项全过」。
- **服务端连不上时直接吐 traceback**:`brief` / `search` / `forget` 只捕获 `HTTPError`,
  `Connection refused` / DNS 失败这类 `URLError` 会以 Python 堆栈糊在用户脸上。
  现在统一走 `client_call()`:传输失败返回 `code=0`,由调用方决定 —— `remember` 进离线队列,
  `brief` / `search` 在本地有库时**回退本地**并提示一行,`forget` 给一句人话错误。
- **`brain brief --budget` 不认**:README 与 CLI 自己的结尾提示都写 `--budget`,
  但解析器只有 `--max-chars`,照抄文档就报 `unrecognized arguments`。
  现在两者等价(`--budget` 为别名,`dest` 不变)。

### 文档

- `examples/SKILL.md` 重写为 v2 口径:去掉 v1 遗留(`inbox/archive/`、`consolidate` 当日常维护),
  补 `forget` / `restore` / `--supersedes` 的"只追加、不改写"协议,`-k` 类型补 `journal`,
  `--ttl` 的复核入口改为实际存在的 `GET /v1/stale`(此前文档写的是并不存在的 `brain stale`)。
- 泛化残留术语:`NAS` → 服务器、`Mac` → 笔记本、`路由` → 边缘网关(含管理台 UI 文案与示例身份
  `laptop/pi/engineer`)。

### 测试

- 单测 62 → 65:`serve` 在空库上自建并 `verify` 五项全过、`brief --budget`、`brief --max-chars`
  (后两个用例顺带守住了"连不上服务端不能吐 traceback")。

## [2.2.4] - 2026-10-29

### 修复

- **按 README 起容器写不进数据**:示例用的是 `-v "$PWD/brain-data:/data"` 宿主目录绑定,
  而镜像以 `10001:10001` 运行,新建的宿主目录属 root ⇒ 首次启动直接
  `PermissionError: [Errno 13] Permission denied: '/data/atoms'`。现在 README 与
  `docker-compose.yml` 默认改用**命名卷** `brain-data`(Docker 会用镜像里 `/data` 的属主初始化,
  开箱即用),绑定宿主目录的做法保留为可选项,并写明必须先 `chown 10001:10001`。

### 新增

- 镜像内 `/usr/local/bin/brain` 指向 `/app/bin/brain`,于是
  `docker exec brain brain token issue ...` / `docker exec brain brain verify` 直接可用。
- README 增加「与容器对话」与「故障排查」两节(权限、端口占用、unhealthy 三条对照)。

### 验证

- 容器端到端跑通:空库自建 → `healthy` → `token issue` → `remember` → 同 `id` 重放返回
  `idempotent_replay` → `search` 命中 → `verify` 五项全过 → 重启后数据仍在。

## [2.2.3] - 2026-10-29

### 修复

- **空库起不来**(发布后第一次拉镜像就撞上):CLI 用「有没有 `atoms/`」当「是不是 v2 库」的判据,
  空目录于是掉进 v1 代码路径,`brain serve` 以 `AttributeError: 'Namespace' object has no attribute 'func'`
  退出。现在按子命令名分派,与目录状态无关。
- **设了 `BRAIN_ROOT` 就 `ModuleNotFoundError: brainlib`**:代码根与数据根被混成一个 `sys.path` 入口,
  容器里(代码 `/app`、数据 `/data`)连 `brain --version` 都跑不起来。现在两者分别入 `sys.path`。

### 新增

- `brain init`:幂等地把一个空目录初始化成合法库根(建 `atoms/ views/ state/ profiles/`、
  `tombstones.jsonl`、`.gitignore`,`git init`,并重建视图让 `verify` 立刻五项全过)。
  `brain serve` 遇到空库也会自动初始化 —— 容器首次启动不需要任何手工步骤。
- `tests/test_cli.py`:5 个 CLI 级回归用例(空库 `--version` / `init` / `verify` / 幂等 / 不落 v1 路径),
  专门守住上面这两个坑;单测总数 57 → 62。

## [2.2.2] - 2026-10-29

### 修复

- `tools/release.py` 新增 `--check` 模式(只校验版本号与 CHANGELOG 一致、版本字面量唯一),
  CI 改用它。此前 CI 直接跑完整门禁,而门禁要求「在 main 上且 tag 不存在或不指向 HEAD」——
  于是**发版当天对 tag 的构建必然失败**(detached HEAD),之后的日常提交也必然失败。
- 门禁本身现在识别「发版验证模式」:detached HEAD 且 tag 指向当前提交时,跳过分支/工作区检查。

### 新增

- `.github/workflows/release.yml`:打 tag 时构建 `linux/amd64` + `linux/arm64` 多架构镜像并推送到 GHCR。
  此前 `docker-compose.yml` 引用了一个从未发布过的镜像标签 —— 文档里的快捷方式必须真的能跑。

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
