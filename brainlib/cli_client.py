"""远端客户端 CLI:给 Mac/手机用,不需要本地数据目录。"""
from __future__ import annotations

import argparse
import json
import sys

from .client import append_outbox, load_config, replay, request
from .model import new_id, now_iso


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(prog="brain", description="共享记忆库远端客户端")
    p.add_argument("--url", help="覆盖配置里的地址")
    p.add_argument("--token", help="覆盖配置里的令牌")
    sub = p.add_subparsers(dest="cmd", required=True)

    b = sub.add_parser("brief", help="会话开头摘要")
    b.add_argument("--max-chars", type=int, default=6000)

    s = sub.add_parser("search", help="检索")
    s.add_argument("query", nargs="+")
    s.add_argument("-n", "--limit", type=int, default=20)

    r = sub.add_parser("remember", help="记一条")
    r.add_argument("text", nargs="+")
    r.add_argument("-k", "--kind", default="fact")
    r.add_argument("-t", "--tag", action="append")
    r.add_argument("--supersedes")
    r.add_argument("--ttl")

    f = sub.add_parser("forget", help="遗忘")
    f.add_argument("id")
    f.add_argument("--status", default="deprecated", choices=["deprecated", "retracted", "active"])
    f.add_argument("--reason", default="")
    f.add_argument("--replaced-by")

    sub.add_parser("whoami", help="我是谁")
    sub.add_parser("health", help="服务健康")

    a = p.parse_args(argv)
    cfg = load_config()
    if getattr(a, "url", None):
        cfg["url"] = a.url
    if getattr(a, "token", None):
        cfg["token"] = a.token
    if not cfg.get("url"):
        print("未配置:在 ~/.config/brain/config.toml 写 url/token(见 deploy/config.toml.example)", file=sys.stderr)
        return 2

    if a.cmd == "health":
        code, data = request(cfg, "GET", "/v1/health")
        print(json.dumps(data, ensure_ascii=False, indent=2))
        return 0 if code == 200 else 1

    replay(cfg)

    if a.cmd == "brief":
        code, data = request(cfg, "GET", f"/v1/brief?budget={a.max_chars}")
        if code == 200:
            print(data if isinstance(data, str) else json.dumps(data, ensure_ascii=False))
            return 0
        print(f"{code} {data}", file=sys.stderr)
        return 3 if code == 413 else 1

    if a.cmd == "search":
        import urllib.parse
        q = " ".join(a.query)
        code, data = request(cfg, "GET", f"/v1/search?q={urllib.parse.quote(q)}&limit={a.limit}")
        if code != 200:
            print(f"{code} {data}", file=sys.stderr)
            return 1
        for h in data.get("hits", []):
            print(f"{h['id']}  {h['occurred_at'][:10]}  {h['author']}  {h['snippet']}")
        return 0 if data.get("hits") else 1

    if a.cmd == "remember":
        body = " ".join(a.text).strip()
        rec = {"id": new_id(), "kind": a.kind, "body": body,
               "tags": [t.lstrip("#") for t in (a.tag or [])],
               "supersedes": a.supersedes, "ttl": a.ttl, "occurred_at": now_iso()}
        try:
            code, data = request(cfg, "POST", "/v1/remember", rec)
        except OSError:
            append_outbox(rec)
            print(f"服务不可达,已离线暂存 {rec['id']}")
            return 0
        print(("已记录 " if code == 201 else f"{code} ") + str(data.get("id", data)))
        return 0 if code in (200, 201) else 4

    if a.cmd == "forget":
        code, data = request(cfg, "POST", "/v1/forget",
                             {"id": a.id, "status": a.status, "reason": a.reason,
                              "replaced_by": a.replaced_by})
        print(f"{code} {json.dumps(data, ensure_ascii=False)}")
        return 0 if code in (200, 201) else 2

    if a.cmd == "whoami":
        code, data = request(cfg, "GET", "/v1/whoami")
        print(json.dumps(data, ensure_ascii=False, indent=2))
        return 0 if code == 200 else 1
    return 2
