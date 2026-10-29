# harness-brain

**Git-native shared memory for every AI coding agent.**
One append-only Markdown store, one HTTP service, any number of agents — on machines you own.

[中文说明](#中文说明) · [Design](docs/DESIGN.md) · [API spec](docs/SPEC-api.md) · [Roadmap](docs/ROADMAP.md) · [Changelog](CHANGELOG.md)

---

## The problem

Every agent you use keeps its own memory:

- your CLI agent knows last week's decisions, your IDE agent does not;
- your laptop agent and your server agent tell you different things about the same machine;
- a new agent starts from zero, and you re-explain the same context for the tenth time;
- whatever it learned is trapped in a vendor-specific database you cannot `git diff`.

## What this is

A small, boring, self-hosted service that holds **one shared memory** for all of them.

```
   agents on any machine                     your server
 ┌───────────────────────┐              ┌──────────────────────────┐
 │ CLI agent   ─┐        │   HTTP/JSON  │  brain serve             │
 │ IDE agent   ─┼────────┼─────────────▶│   ├─ atoms/       (truth)│
 │ CI job      ─┤        │  one file    │   ├─ tombstones.jsonl    │
 │ scripts     ─┘        │  client      │   ├─ views/     (derived)│
 └───────────────────────┘              │   └─ git history         │
                                        └──────────────────────────┘
```

Design commitments, in priority order:

1. **The truth is Markdown in a git repo.** `git clone` gives you a readable tree. No database to
   export, no format to reverse-engineer, no lock-in. You can read your memory with `cat`.
2. **Atoms are immutable.** A correction is a new atom that supersedes the old one; deletions are
   appended to `tombstones.jsonl`. Nothing is ever rewritten in place.
3. **Single writer.** Only the server process writes. Clients queue offline in a local outbox and
   replay idempotently when they reconnect.
4. **Derived things are disposable.** `views/`, `INDEX.md`, search indexes are pure functions of
   `atoms ⊕ tombstones`. Delete them, rebuild them, byte-for-byte identical every time.
5. **Zero dependencies.** Python 3.11+ standard library only. No pip install, no build step, no
   supply chain. The whole service is a few thousand lines you can read in an afternoon.
6. **Your data does not leave your network.** No SaaS, no telemetry, no account.

## What it is *not*

- Not a vector database and not RAG. It stores short, human-written conclusions, not embeddings.
- Not a chat transcript store. Transcripts are noisy; conclusions are durable.
- Not multi-tenant (yet). v2 is single-owner with scoped tokens; namespaces are on the roadmap.
- Not a replacement for your agent's context window. It is what the agent reads *at session start*.

## Quick start

### Docker (recommended)

```bash
docker run -d --name brain \
  -p 127.0.0.1:8790:8790 \
  -v "$PWD/brain-data:/data" \
  --restart unless-stopped \
  --memory 256m \
  ghcr.io/Fawkes-hub/harness-brain:2.2.3
```

### docker compose

```bash
git clone https://github.com/Fawkes-hub/harness-brain.git && cd harness-brain
docker compose up -d          # data lands in ./data
```

### Bare metal (systemd, no Docker)

```bash
git clone https://github.com/Fawkes-hub/harness-brain.git /opt/harness-brain
sudo useradd -r -s /usr/sbin/nologin brain
sudo mkdir -p /srv/brain && sudo chown brain: /srv/brain
sudo -u brain BRAIN_ROOT=/srv/brain python3 /opt/harness-brain/bin/brain init
sudo -u brain BRAIN_ROOT=/srv/brain python3 /opt/harness-brain/bin/brain serve --bind 0.0.0.0 --port 8790
```

Keep it alive with a unit (`/etc/systemd/system/brain.service`):

```ini
[Unit]
Description=harness-brain shared memory service
After=network-online.target

[Service]
User=brain
Environment=BRAIN_ROOT=/srv/brain
ExecStart=/usr/bin/python3 /opt/harness-brain/bin/brain serve --bind 0.0.0.0 --port 8790
Restart=on-failure
NoNewPrivileges=yes
ProtectSystem=strict
ReadWritePaths=/srv/brain

[Install]
WantedBy=multi-user.target
```

## Five-minute walkthrough

```bash
# 0. one-time: create the store layout (idempotent; the container does this on first start)
BRAIN_ROOT=/srv/brain ./bin/brain init

# 1. issue a scoped token for one machine (plaintext is printed exactly once)
BRAIN_ROOT=/srv/brain ./bin/brain token issue \
    --host laptop --harness cli --role engineer --scope read,write --ttl 365

# 2. on the client machine, write it to a 0600 file — never into a repo
mkdir -p ~/.config/brain && cat > ~/.config/brain/config.toml <<'TOML'
url   = "http://192.0.2.10:8790"
token = "bk_..."
role  = "engineer"
TOML
chmod 600 ~/.config/brain/config.toml

# 3. remember something
brain remember "Staging DB is read-only; writes go through the migration job" -k fact -t deploy

# 4. at the start of every session, load the brief
brain brief --budget 6000

# 5. find things again
brain search "staging"
brain show atoms/202610/m_01j9zk4m2x.md
```

`brain brief` is the important one: it renders a token-budgeted digest of rules, recent decisions,
open issues and topic indexes, so an agent starts each session already knowing.

## Data model

```markdown
---
id: m_01j9zk4m2x           # ^m_[0-9a-hjkmnp-tv-z]{10}$ — frozen; clients generate it
kind: fact                 # fact | rule | pref | lesson | host | project | idea | journal
tags: [deploy, staging]
occurred_at: 2026-10-29T09:12:00+08:00
recorded_at: 2026-10-29T09:13:04+08:00
author: { host: laptop, harness: cli, role: engineer }
ttl: 2027-04-29            # optional: after this, `brain stale` flags it
supersedes: [m_01j8aa11bc] # optional: this atom corrects older ones
hash: sha256:9a0fdfd24833230c
---

Staging DB is read-only; writes go through the migration job.
```

- `id` is the only idempotency key: replaying the same id is a no-op, never a duplicate.
- `hash` is a content fingerprint used **only** for "possible duplicate" hints, never for dedup.
- Roles (`engineer`, `assistant`, …) change tone and duty only. **Facts are written neutrally.**

## Wiring an agent

Any agent that can run a shell or read a file can use it. Drop the protocol file into your harness's
skill/instruction directory — see [`examples/SKILL.md`](examples/SKILL.md) — and tell it:

> At session start run `brain brief`. When you learn something durable, `brain remember "…"`.
> Never rewrite old memories; supersede them.

## Deployment topologies

| # | Topology | When |
|---|---|---|
| N0 | same host, unix socket / loopback | single machine |
| N1 | plain LAN over HTTP | trusted home/office network |
| N2 | SSH tunnel (`ssh -L 8790:localhost:8790`) | no VPN, occasional remote use |
| N3 | mesh VPN, **no public port** | recommended for remote agents |
| N4 | reverse proxy + TLS + rate limit + IP allowlist | public or semi-public exposure |

Rules of thumb: **never expose 8790 directly to the internet**; if you put a proxy in front, set
`BRAIN_TRUST_PROXY=1` *only* when the proxy is really there (otherwise clients can spoof
`X-Forwarded-For`); give every machine its own token so revocation is surgical.

## Backups (do this on day one)

The store is a git repo, so backups are git plus one encrypted archive:

```bash
# L1 every 30 min: commit what changed
BRAIN_ROOT=/srv/brain ./bin/brain sync --message "auto: $(date -Is)"
# L2 push to a PRIVATE remote
git -C /srv/brain push --quiet origin main
# L3 nightly: bundle everything, then encrypt
git -C /srv/brain bundle create /backup/brain-$(date +%F).bundle --all
age -r <recipient> -o /backup/brain-$(date +%F).bundle.age /backup/brain-$(date +%F).bundle
```

Restore drill monthly: unpack a bundle into a scratch dir, run `brain verify`, expect five green checks.

## Configuration

| Variable | Default | Meaning |
|---|---|---|
| `BRAIN_ROOT` | script's parent dir | where `atoms/`, `state/`, `views/` live |
| `BRAIN_HOST` | `127.0.0.1` | bind address |
| `BRAIN_PORT` | `8790` | listen port |
| `BRAIN_TZ` | system | timezone for rendered timestamps |
| `BRAIN_URL` / `BRAIN_TOKEN` | — | client-side override (beats `config.toml`) |

## Status

`v2.2.3`. Implemented: atom store, tombstones, derived views, `verify`, HTTP API, scoped tokens,
admin UI, brief/search/recall, offline outbox. Not yet: encrypted secrets (`brain secret`),
SQLite+FTS5 retrieval, MCP transport, namespaces/multi-user. See [ROADMAP](docs/ROADMAP.md).

Version policy — data-format changes are always a MAJOR bump — is in [docs/VERSIONING.md](docs/VERSIONING.md).
API details in [docs/SPEC-api.md](docs/SPEC-api.md). Architecture decisions in [docs/ADR.md](docs/ADR.md).

## Contributing

Issues and PRs welcome — read [CONTRIBUTING.md](CONTRIBUTING.md) first. Security reports: see
[SECURITY.md](SECURITY.md). License: [MIT](LICENSE).

---

## 中文说明

**给所有 AI 编码 agent 的共享记忆库。** 一个只追加的 Markdown 库、一个 HTTP 服务、任意多台机器上的任意多个 agent —— 全跑在你自己的机器上。

### 它解决什么

每个 agent 各记各的:命令行 agent 记得上周的决定,IDE 里的不知道;笔记本和服务器的说法不一致;换个新 agent 就从零开始,同一套背景你讲第十遍。而这些"记忆"通常还锁在厂商私有数据库里,`git diff` 都看不到。

### 它是什么

一台服务器上跑 `brain serve`,库就是一堆 Markdown 文件加一个 git 历史。任何能跑 shell 的 agent 用同一个单文件客户端读写它。

六个设计承诺:

1. **真相是 git 里的 Markdown** —— `git clone` 就得到一棵可读的树,`cat` 就能看,不锁死任何格式。
2. **atom 不可变** —— 更正靠"新 atom 取代旧 atom",删除追加到 `tombstones.jsonl`,永不原地改写。
3. **单写者** —— 只有服务端进程写库;客户端离线进本地 outbox,恢复后按 id 幂等重放。
4. **派生品皆可弃** —— `views/`、索引都是 `atoms ⊕ tombstones` 的纯函数,删掉重建字节一致。
5. **零依赖** —— 只用 Python 3.11+ 标准库,没有 pip、没有构建、没有供应链。
6. **数据不出内网** —— 没有 SaaS、没有遥测、没有账号。

### 不是什么

不是向量库、不是 RAG、不存聊天记录、不是多租户(v3 才有 namespace)、不替代上下文窗口 —— 它是 agent **开场时读的那一份**。

### 三步上手

```bash
# 1) 起服务(容器,数据在 ./data)
docker run -d --name brain -p 127.0.0.1:8790:8790 -v "$PWD/brain-data:/data" \
  --restart unless-stopped ghcr.io/Fawkes-hub/harness-brain:2.2.3

# 2) 给每台机器签一把自己的令牌(明文只显示一次)
BRAIN_ROOT=./brain-data ./bin/brain token issue --host laptop --harness cli --role engineer

# 3) 客户端写下地址与令牌(0600,绝不进仓库),然后就能用了
brain remember "预发库是只读的,写入只走迁移任务" -k fact -t deploy
brain brief --budget 6000      # 会话开头加载
brain search "预发"             # 之后再找回来
```

### 给 agent 接线

把 [`examples/SKILL.md`](examples/SKILL.md) 放进你的 harness 的技能/指令目录,并告诉它:**开场先 `brain brief`;学到稳定结论就 `brain remember`;更正旧记忆要用取代,不要改写。**

### 部署与安全

五档拓扑(N0 同机 → N4 反代+TLS+限流)。经验法则:**永远不要把 8790 直接暴露到公网**;前面真有反代时才设 `BRAIN_TRUST_PROXY=1`;每台机器一把独立令牌,便于定点吊销。

### 备份

库本身就是 git 仓库,备份 = git 推送私有远端 + 每天 `git bundle` 后用 `age` 加密归档。**每月做一次恢复演练**:把 bundle 解开到临时目录,跑 `brain verify`,五项全绿才算数。

### 状态

`v2.2.3`。已实现:atom 存储、tombstone、派生视图、`verify`、HTTP API、令牌、管理台、brief/search/recall、离线 outbox。未实现:加密密钥(`brain secret`)、SQLite+FTS5 检索、MCP 传输、namespace 多用户。见 [ROADMAP](docs/ROADMAP.md);版本策略见 [docs/VERSIONING.md](docs/VERSIONING.md);接口见 [docs/SPEC-api.md](docs/SPEC-api.md)。
