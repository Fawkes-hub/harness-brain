# 分期路线图与验收标准

> 每一期的"完成"都必须能用**命令证明**,不靠"感觉好了"。
> 上一期未达验收标准,不进入下一期 —— 这套系统最大的风险不是做不出来,是半成品被当成完成品用起来。

---

## 外部依赖

- **age CLI**:v2.2 的加密与轮换用它(`apt install age` / `brew install age`),当前服务器 **未安装**。
  Python 侧只通过 `subprocess` 调用,不引 PyPI 包 —— 所以「零依赖」只对 v2.0/v2.1 成立,部署文档要写清。
- 其余全部 Python stdlib。

## 通用规则

- 每个验收命令必须能指回本期的**产出**;引用了不存在的命令/文件,等于门禁不存在。
- 期次之间不跳步。v2.0 的"迁移无损"没证明,后面所有结论都不可信。

---

## v2.0 数据层地基

**产出**(每项都要落到文件):

| 产出 | 说明 |
| --- | --- |
| `atoms/<yyyymm>/<id>.md` + `views/` 重建器 | 含 §4.3 的 staging→replace→revision 流程 |
| `tombstones.jsonl` | 追加式,`seq` 单调 |
| `profiles/<role>.md` + `default.md` | 只建文件,**此时不注入** persona(渲染在 v2.2) |
| `tools/migrate-v1.py` | `--dry-run` / `--apply` / `--verify` |
| `bin/brain` 新命令 | `whoami`、`verify`、`remember --id`、`reindex --force` |
| `deploy/brain.service` | v2.1 才启用,但 v2.0 就要提交这个文件 |
| `bin/brain forget` / `restore` | 追加 tombstone(§4.5);v2.0 只操作本地文件 |

**`brain verify` 的五项**(必须实现在 `verify` 里,而不是写在文档里):

1. 每个 atom 都能被解析(frontmatter 完整、`id` 匹配 `^m_[0-9a-hjkmnp-tv-z]{10}$`);
2. 每个 atom 的 `hash` 与重算值一致(`hash_algo: sha256-v1`);
3. `supersedes` 指向的 id 存在(或已登记为断链);
4. `tombstones.jsonl` 每行可解析、`seq` 严格递增;
5. `views/.revision` 与"当前 atoms ⊕ tombstones"的重算值一致(不一致则提示 `reindex`)。

**验收**:

```bash
# 1 迁移无损(dry-run 先给基线;实测 90 条,逐行覆盖 100%)
./tools/migrate-v1.py --dry-run            # 输出:文件 → atom 数 → 字节数 的对照表
./tools/migrate-v1.py --apply
./tools/migrate-v1.py --verify             # 四条断言全过(见 DESIGN §12)
# 2 视图可重建且幂等
test -f views/rules.md || ./bin/brain reindex --force    # 干净检出时 views/ 还不存在
before=$(sha256sum views/rules.md views/INDEX.md)
./bin/brain reindex --force && after=$(sha256sum views/rules.md views/INDEX.md)
[ "$before" = "$after" ]
# 3 视图损坏可恢复
echo broken > views/rules.md && ./bin/brain reindex --force && grep -q "规范" views/rules.md
# 4 verify 五项
./bin/brain verify                          # 期望 OK,且删掉一条 atom 后会报错
# 5 遗忘不改 atom
printf '%s' '{"id":"m_test000001","status":"deprecated","reason":"t"}' >/dev/null
./bin/brain forget m_test000001 && git status --short atoms/ | wc -l   # 期望 0
```

**退出标准**:1–5 全绿;`--dry-run` 逐行覆盖 100%(脚本强制),逐文件对照表人工抽查。

---

## v2.1 serve(核心能力)

**产出**:`brain serve`、令牌签发/吊销(`--init`/`issue`/`revoke`/`list`)、
`/v1/{health,whoami,remember,brief,search,forget,stale,conflicts}`(恢复 = forget + `status: active`)、客户端离线 outbox、
`tools/test-idempotency.sh`、启用 `deploy/brain.service`。

**验收**:

```bash
# 1 服务存活且开机自启
systemctl is-enabled brain && systemctl is-active brain
curl -sf localhost:8790/v1/health | jq -e '.ok and .api==1'
# 2 身份来自令牌,不来自请求体
curl -s -H "Authorization: Bearer $T" localhost:8790/v1/whoami | jq -e '.host=="mac" and .role_source=="token"'
# 3 无令牌被拒
curl -s -o /dev/null -w "%{http_code}\n" localhost:8790/v1/brief   # 期望 401
# 4 幂等(SPEC §5 全部场景)
bash tools/test-idempotency.sh                                      # 期望 ALL PASS
# 5 笔记本不挂载也能用(注意:此时 brief 还没有角色段,persona 在 v2.2)
ssh mac 'brain brief | head -3'                                     # 期望出现"规范"段
# 6 离线队列
systemctl stop brain; ssh mac 'brain remember "离线测试" -k fact'; systemctl start brain
sleep 2; ssh mac 'brain search 离线测试' | grep -q 离线测试
# 7 budget 硬下限
curl -s -o /dev/null -w "%{http_code}\n" -H "Authorization: Bearer $T" \
  "localhost:8790/v1/brief?budget=200"                              # 期望 413
```

**退出标准**:1–7 全绿;`journalctl -u brain` 无未处理异常;连续运行 72 小时无内存增长。

---

## v2.2 密钥与角色

**产出**:`brain secret add/exec/list/reencrypt`、age 多收件人、`recipients.txt` 管理、
**persona 渲染进 `brief` 首段**、`secrets/README.md`(轮换记录)。

**验收**:

```bash
# 1 密文入库,明文不在(用固定假口令,避免把真口令拼进 argv)
printf 'FIXED_DUMMY_9f3a' | brain secret add qb
git -C . grep -c "FIXED_DUMMY" -- secrets/            # 期望 0
# 2 exec 不泄漏(grep 的是字面量,不做命令替换)
brain secret exec -s qb -- sh -c 'test -n "$QB_PASS" && echo ok'   # 期望 ok
journalctl -u brain | grep -c FIXED_DUMMY                          # 期望 0
# 3 吊销的真实语义:旧密文仍可解,重加密后不可解
age -d -i old.key secrets/qb.age >/dev/null && echo "旧密文仍可解(符合预期)"
brain secret reencrypt --to new-recipients  && (age -d -i old.key secrets/qb.age || echo "新密文旧钥匙解不开 ✓")
# 4 get 默认被拒
brain secret get -s qb; echo $?                                      # 期望非 0
# 5 persona 进 brief
brain brief | head -3                                                # 期望首段是角色
```

**退出标准**:1–5 全绿;`git log -p` 全历史中搜不到任何明文口令。

---

## v2.3 检索与 MCP

**产出**:FTS5 + bigram、`/v1/recall`、`search --include-deprecated`、MCP server 包装(可选)。

**验收**:

```bash
# 1 中文检索命中(注意 v2 的路径是 views/topics,不是 v1 的 facts/)
brain search 代理 | head -3        # 期望命中 views/topics/network.md
brain search 代理地址 | head -3     # 期望同样命中(bigram 生效)
# 2 检索速度
time brain search 端口              # 期望 < 100ms(1000 条规模)
# 3 已废弃条目不出现
brain search --include-deprecated 旧条目 | wc -l   # > 默认结果数
```

**退出标准**:1–3 全绿;`EXPLAIN QUERY PLAN` 确认走 FTS 索引。

---

## 独立项:路由安全基线(已完成)

见 `hosts/edge-gateway.md` 与 DESIGN §10.5。

**验收**:口令登录被拒、38368 只监听 LAN 地址、ttyd 从 mesh 不可达、LAN 回滚通道可用。**已全部通过。**

---

## 不承诺的部分(明确排除)

- 不做 Web UI 的多租户/富文本编辑;v2.2 已提供服务端渲染的管理台(见 README「可视界面」),只做浏览、复核与低风险管理动作。
- 不做多租户与权限系统(单个家庭局域网)。
- 不做会话 transcript 全量同步(只同步结论)。
- 不做自动冲突合并(见 ADR-0005)。
- 不做公网暴露(需要时再加边缘 + TLS,见 DESIGN §10.4)。
