#!/usr/bin/env bash
# 幂等矩阵验收(SPEC-api §5 中「可用 HTTP 验证」的部分)。
#
#   BRAIN_URL=http://127.0.0.1:8790 BRAIN_TOKEN=bk_xxx ./tools/test-idempotency.sh
#
# 退出码:0 全部通过;1 有失败;2 环境不满足(服务没起 / 缺 python3)。
#
# 注意:id 必须满足 ^m_[0-9a-hjkmnp-tv-z]{10}$。上一版用 `date +%s|tail -c 6` 生成的是 9 位,
# 服务端按 SPEC 会直接 400 —— 脚本永远不可能 PASS。这里改成 Crockford 字母表随机生成。
set -uo pipefail

BASE="${BRAIN_URL:-http://127.0.0.1:8790}"
TOKEN="${BRAIN_TOKEN:-}"
PASS=0; FAIL=0

say() { printf '%s\n' "$*"; }
ok()  { PASS=$((PASS+1)); say "  PASS  $1"; }
bad() { FAIL=$((FAIL+1)); say "  FAIL  $1"; }

new_id() { python3 -c 'import secrets; a="0123456789abcdefghjkmnpqrstvwxyz"; print("m_"+"".join(secrets.choice(a) for _ in range(10)))'; }
errcode() { python3 -c 'import json,sys;print(json.load(sys.stdin).get("error",{}).get("code",""))' < /tmp/.brain_body 2>/dev/null; }
code() {
  local m="$1" p="$2" d="${3:-}"
  if [ -n "$d" ]; then
    curl -s -o /tmp/.brain_body -w '%{http_code}' -X "$m" \
      -H "Authorization: Bearer $TOKEN" -H 'Content-Type: application/json' -d "$d" "$BASE$p"
  else
    curl -s -o /tmp/.brain_body -w '%{http_code}' -X "$m" \
      -H "Authorization: Bearer $TOKEN" "$BASE$p"
  fi
}
search_count() {
  curl -s -H "Authorization: Bearer $TOKEN" "$BASE/v1/search?q=$1" \
    | python3 -c 'import json,sys;print(len(json.load(sys.stdin).get("hits",[])))' 2>/dev/null
}

command -v python3 >/dev/null || { say "缺 python3"; exit 2; }
curl -sf "$BASE/v1/health" >/dev/null || { say "服务不可达:$BASE"; exit 2; }

ID=$(new_id); SAY="幂等测试$$"
say "== 1 同 id 同内容,重放三次 → 只存一条"
c1=$(code POST /v1/remember "{\"id\":\"$ID\",\"kind\":\"fact\",\"body\":\"$SAY\",\"tags\":[\"test\"]}")
c2=$(code POST /v1/remember "{\"id\":\"$ID\",\"kind\":\"fact\",\"body\":\"$SAY\",\"tags\":[\"test\"]}")
c3=$(code POST /v1/remember "{\"id\":\"$ID\",\"kind\":\"fact\",\"body\":\"$SAY\",\"tags\":[\"test\"]}")
[ "$c1" = 201 ] && ok "首次 201" || bad "首次期望 201,得到 $c1"
{ [ "$c2" = 200 ] && [ "$c3" = 200 ]; } && ok "重放 200 ×2" || bad "重放期望 200,得到 $c2/$c3"
n=$(search_count "$SAY")
[ "$n" = 1 ] && ok "库中只有 1 条" || bad "期望 1 条,实际 $n(重复写入了)"

say "== 2 同 id 不同内容 → 409 conflict_id,且不覆盖"
c4=$(code POST /v1/remember "{\"id\":\"$ID\",\"kind\":\"fact\",\"body\":\"篡改$$并且另加内容\",\"tags\":[\"test\"]}")
[ "$c4" = 409 ] && ok "409" || bad "期望 409,得到 $c4"
v=$(errcode)
[ "$v" = "conflict_id" ] && ok "错误码 conflict_id" || bad "错误码为 '$v'"
n=$(search_count "篡改$$")
[ "$n" = 0 ] && ok "篡改内容未落库" || bad "篡改内容被写入了"

say "== 3 不同 id 同内容(两端各写一次)→ 必须保留两条"
ID2=$(new_id); ID3=$(new_id); SAY2="同一事实$$"
code POST /v1/remember "{\"id\":\"$ID2\",\"kind\":\"fact\",\"body\":\"$SAY2\",\"tags\":[\"test\"]}" >/dev/null
code POST /v1/remember "{\"id\":\"$ID3\",\"kind\":\"fact\",\"body\":\"$SAY2\",\"tags\":[\"test\"]}" >/dev/null
n=$(search_count "$SAY2")
[ "$n" = 2 ] && ok "两条都保留($n)" || bad "期望 2 条,实际 $n(疑似被 hash 判重误杀)"

say "== 4 forget 追加语义 + 恢复 + 错误分支"
c5=$(code POST /v1/forget "{\"id\":\"$ID\",\"status\":\"deprecated\",\"reason\":\"测试\"}")
[ "$c5" = 201 ] && ok "忘记得 201" || bad "期望 201,得到 $c5"
c6=$(code POST /v1/forget "{\"id\":\"$ID\",\"status\":\"deprecated\",\"reason\":\"测试\"}")
[ "$c6" = 200 ] && ok "重复 forget 200(noop)" || bad "期望 200,得到 $c6"
n=$(search_count "$SAY")
[ "$n" = 0 ] && ok "已遗忘的检索不到" || bad "已遗忘的仍能检索到($n)"
c7=$(code POST /v1/forget '{"id":"m_zzzzzzzzzz","status":"deprecated"}')
[ "$c7" = 404 ] && ok "未知 id 404" || bad "期望 404,得到 $c7"
c8=$(code POST /v1/forget "{\"id\":\"$ID\",\"status\":\"retracted\"}")
[ "$c8" = 400 ] && ok "retracted 缺 replaced_by 被拒" || bad "期望 400,得到 $c8"
c9=$(code POST /v1/forget "{\"id\":\"$ID\",\"status\":\"retracted\",\"replaced_by\":\"m_zzzzzzzzzz\"}")
[ "$c9" = 400 ] && ok "replaced_by 指向不存在被拒" || bad "期望 400,得到 $c9"
c10=$(code POST /v1/forget "{\"id\":\"$ID\",\"status\":\"active\",\"reason\":\"恢复\"}")
[ "$c10" = 201 ] && ok "恢复 201" || bad "期望 201,得到 $c10"
n=$(search_count "$SAY")
[ "$n" = 1 ] && ok "恢复后可检索到(证明 restore 生效)" || bad "恢复后仍检索不到($n)—— 典型的「出现过就剔除」bug"

say "== 5 鉴权"
c11=$(curl -s -o /dev/null -w '%{http_code}' "$BASE/v1/brief")
[ "$c11" = 401 ] && ok "无 token 401" || bad "期望 401,得到 $c11"

rm -f /tmp/.brain_body
say ""
say "结果:PASS=$PASS FAIL=$FAIL"
[ "$FAIL" -eq 0 ] || exit 1
say "注:「提交时进程崩溃」属进程级故障注入,由服务端单元测试覆盖,本脚本不假装测过。"
