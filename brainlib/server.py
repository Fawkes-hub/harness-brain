"""brain serve —— 标准库 HTTP 服务,单写者。

设计要点:
  * 身份由令牌决定,请求体里的 author 一律忽略;
  * 写操作串行(全局锁)+ store 自己的 flock,视图重建在锁内完成;
  * 错误体统一 {"error":{"code","message"}},code 与 SPEC 一致;
  * 人读界面与服务端渲染共用一个进程,cookie 认证 + 表单 POST + Origin 校验。
"""
from __future__ import annotations

import datetime as dt
import fcntl
import json
import os
import re
import shutil
import threading
import traceback
from collections import Counter
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, quote, unquote, urlencode, urlparse, urlsplit, urlunsplit

from . import auth as authmod
from . import backup as backupmod
from . import web
from .brief import render_brief
from .model import ID_RE, KINDS, Atom, content_hash, new_id, now_iso, parse_iso
from .store import Store

from . import __version__ as SERVER_VERSION  # 单一版本来源: brainlib/__init__.py
MIN_CLIENT = "2.0.0"
MAX_BODY = 64 * 1024
PAGE_SIZE = 20

SCOPE_FOR = {
    ("GET", "/v1/whoami"): "read",
    ("GET", "/v1/brief"): "read",
    ("GET", "/v1/search"): "read",
    ("GET", "/v1/recall"): "read",
    ("GET", "/v1/stale"): "read",
    ("GET", "/v1/conflicts"): "read",
    ("POST", "/v1/remember"): "write",
    ("POST", "/v1/journal"): "write",
    ("POST", "/v1/forget"): "write",
    ("POST", "/v1/reindex"): "admin",
    ("GET", "/v1/backup/status"): "admin",
    ("POST", "/v1/backup/run"): "admin",
}

KIND_ORDER = list(KINDS)


def _who(author: dict) -> str:
    return f"{author.get('host', '?')}/{author.get('harness', '?')}/{author.get('role', '?')}"


def _terms(q: str) -> list[str]:
    return [t for t in (q or "").split() if t][:8]


def _matches(a: Atom, terms: list[str]) -> bool:
    hay = " ".join([
        a.body or "", " ".join(a.tags or []), a.kind or "",
        a.author.get("host", ""), a.author.get("harness", ""), a.id,
    ]).lower()
    return all(t.lower() in hay for t in terms)


def _sanitize_next(value: str, default: str = "/") -> str:
    value = (value or "").strip()
    if not value.startswith("/") or value.startswith("//"):
        return default
    return value


class Brain:
    def __init__(self, root: Path):
        self.root = Path(root)
        self.store = Store(self.root)
        self.tokens = authmod.TokenStore(self.root / "state" / "tokens.json")
        self.tokens.init()
        self.backup = backupmod.Backup(self.root)
        self.lock = threading.Lock()

    # ---------- 审计 ----------
    def audit(self, action: str, actor: str, target: str = "", detail: str = "") -> dict:
        rec = {"at": now_iso(), "action": action, "actor": actor,
               "target": target, "detail": detail, "via": "web"}
        path = self.root / "state" / "audit.jsonl"
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            with open(path, "a", encoding="utf-8") as fh:
                try:
                    os.chmod(path, 0o600)
                except OSError:
                    pass
                fcntl.flock(fh, fcntl.LOCK_EX)
                fh.write(json.dumps(rec, ensure_ascii=False) + "\n")
                fh.flush()
                os.fsync(fh.fileno())
                fcntl.flock(fh, fcntl.LOCK_UN)
        except OSError:
            pass
        return rec

    def read_audit(self, limit: int = 200) -> list[dict]:
        path = self.root / "state" / "audit.jsonl"
        if not path.exists():
            return []
        out = []
        try:
            lines = path.read_text(encoding="utf-8").splitlines()[-limit:]
        except OSError:
            return []
        for line in reversed(lines):
            try:
                rec = json.loads(line)
            except json.JSONDecodeError:
                continue
            if isinstance(rec, dict):
                out.append(rec)
        return out

    # ---------- 业务 ----------
    def health(self) -> dict:
        rev_file = self.store.views_dir / ".revision"
        last_rebuild = ""
        if rev_file.exists():
            last_rebuild = dt.datetime.fromtimestamp(rev_file.stat().st_mtime).astimezone() \
                .isoformat(timespec="seconds")
        return {
            "ok": True, "api": 1, "server": SERVER_VERSION, "min_client": MIN_CLIENT,
            "features": ["brief", "search", "recall", "forget", "stale", "conflicts", "ui",
                         "tokens", "audit", "backup"],
            "atoms": len(self.store.load_atoms()),
            "views_rev": self.store.current_revision(),
            "root": str(self.root),
            "tombstones": len(self.store.load_tombstones()),
            "last_rebuild": last_rebuild,
        }

    def remember(self, rec: dict, author: dict) -> tuple[int, dict]:
        if not isinstance(rec, dict):
            return 400, {"error": {"code": "bad_request", "message": "JSON 必须是对象"}}
        ident = rec.get("id")
        if not isinstance(ident, str) or not ID_RE.fullmatch(ident):
            return 400, {"error": {"code": "bad_request", "message": "id 不合规"}}
        kind = rec.get("kind") or "fact"
        if not isinstance(kind, str) or kind not in KINDS:
            return 400, {"error": {"code": "bad_request", "message": "kind 非法"}}
        raw_body = rec.get("body")
        if not isinstance(raw_body, str) or not raw_body.strip():
            return 400, {"error": {"code": "bad_request", "message": "body 必填"}}
        body = raw_body.strip()
        if len(body) > 60000:
            return 400, {"error": {"code": "bad_request", "message": "body 过长"}}
        raw_tags = rec.get("tags") or []
        if not isinstance(raw_tags, list):
            return 400, {"error": {"code": "bad_request", "message": "tags 必须是数组"}}
        tags = [str(t).strip() for t in raw_tags if str(t).strip()][:16]

        occurred_raw = rec.get("occurred_at")
        if occurred_raw in (None, ""):
            occurred_raw = now_iso()
        if not isinstance(occurred_raw, str):
            return 400, {"error": {"code": "bad_request", "message": "occurred_at 非法"}}
        occ = parse_iso(occurred_raw)
        if occ is None:
            return 400, {"error": {"code": "bad_request", "message": "occurred_at 必须是 ISO 时间"}}
        tz = dt.datetime.now().astimezone().tzinfo
        if occ.tzinfo is None:
            occ = occ.replace(tzinfo=tz)
        if occ > dt.datetime.now(tz) + dt.timedelta(minutes=5):
            return 400, {"error": {"code": "bad_request", "message": "occurred_at 不能晚于当前时间 5 分钟"}}

        supersedes = rec.get("supersedes")
        if supersedes not in (None, ""):
            if not isinstance(supersedes, str) or not ID_RE.fullmatch(supersedes):
                return 400, {"error": {"code": "bad_request", "message": "supersedes 不合规"}}
            if supersedes == ident:
                return 400, {"error": {"code": "bad_request", "message": "不能取代自己"}}
            atoms = self.store.load_atoms()
            if supersedes not in atoms:
                return 400, {"error": {"code": "bad_request", "message": "supersedes 目标不存在"}}
            seen: set[str] = set()
            cur = supersedes
            while cur and cur not in seen and len(seen) < 100:
                if cur == ident:
                    return 400, {"error": {"code": "bad_request", "message": "会形成取代环"}}
                seen.add(cur)
                nxt = atoms.get(cur)
                cur = nxt.supersedes if nxt else None
        else:
            supersedes = None

        ttl = rec.get("ttl")
        if ttl not in (None, ""):
            if not isinstance(ttl, str):
                return 400, {"error": {"code": "bad_request", "message": "ttl 非法"}}
            try:
                dt.date.fromisoformat(ttl)
            except ValueError:
                return 400, {"error": {"code": "bad_request", "message": "ttl 必须是 YYYY-MM-DD"}}
        else:
            ttl = None

        confidence = rec.get("confidence") or "medium"
        if confidence not in ("high", "medium", "low", "guess"):
            confidence = "medium"
        source = rec.get("source") or "api"
        if not isinstance(source, str):
            source = "api"
        at = Atom(
            id=ident, kind=kind, body=body,
            occurred_at=occurred_raw, received_at=now_iso(),
            author=author, tags=tags, confidence=confidence,
            source=source[:200], supersedes=supersedes, ttl=ttl,
        )
        at.hash = content_hash(at.kind, at.tags, at.ttl, at.supersedes, at.body)
        with self.lock:
            try:
                state = self.store.put_atom(at)
            except FileExistsError as e:
                return 409, {"error": {"code": "conflict_id", "message": str(e)}}
            except ValueError as e:
                return 400, {"error": {"code": "bad_request", "message": str(e)}}
            self.store.rebuild()
        if state == "replay":
            return 200, {"id": at.id, "stored": False, "reason": "idempotent_replay",
                         "views_rev": self.store.current_revision()}
        self.audit("remember", _who(author), at.id,
                   f"{at.kind} tags={','.join(tags)}")
        return 201, {"id": at.id, "stored": True, "occurred_at": at.occurred_at,
                     "received_at": at.received_at, "views_rev": self.store.current_revision()}

    def forget(self, rec: dict, author: dict) -> tuple[int, dict]:
        if not isinstance(rec, dict):
            return 400, {"error": {"code": "bad_request", "message": "JSON 必须是对象"}}
        ident = rec.get("id")
        if not isinstance(ident, str) or not ident:
            return 400, {"error": {"code": "bad_request", "message": "id 必填"}}
        status = rec.get("status") or "deprecated"
        if status not in ("deprecated", "retracted", "active"):
            return 400, {"error": {"code": "bad_request", "message": "status 非法"}}
        replaced_by = rec.get("replaced_by")
        if replaced_by not in (None, ""):
            if not isinstance(replaced_by, str) or not ID_RE.fullmatch(replaced_by):
                return 400, {"error": {"code": "bad_request", "message": "replaced_by 不合规"}}
        else:
            replaced_by = None
        with self.lock:
            atoms = self.store.load_atoms()
            if ident not in atoms:
                return 404, {"error": {"code": "not_found", "message": ident}}
            if status == "retracted":
                if not replaced_by:
                    return 400, {"error": {"code": "bad_request", "message": "retracted 必须带 replaced_by"}}
                if replaced_by not in atoms:
                    return 400, {"error": {"code": "bad_request", "message": "replaced_by 目标不存在"}}
                if replaced_by == ident:
                    return 400, {"error": {"code": "bad_request", "message": "不能自我替代"}}
            cur = self.store.status_map().get(ident, {}).get("status")
            rec_out = self.store.append_tombstone(ident, status, (rec.get("reason") or "")[:300],
                                                  _who(author), replaced_by)
            self.store.rebuild()
        self.audit("forget", _who(author), ident, f"status={status}")
        if cur == status:
            return 200, {"seq": rec_out["seq"], "status": status, "noop": True}
        return 201, {"seq": rec_out["seq"], "status": status}

    def search(self, q: str, limit: int, include_deprecated: bool) -> dict:
        terms = _terms(q)
        if not terms:
            return {"query": q, "hits": []}
        if include_deprecated:
            pool = list(self.store.load_atoms().values())
        else:
            pool = self.store.visible()[0]
        hits = []
        for a in pool:
            if not _matches(a, terms):
                continue
            hits.append({"id": a.id, "occurred_at": a.occurred_at,
                         "received_at": a.received_at, "score": 1.0,
                         "snippet": (a.body.splitlines()[0] if a.body else "")[:160],
                         "author": f"{a.author.get('host', '?')}/{a.author.get('harness', '?')}"})
        hits.sort(key=lambda h: h["occurred_at"] or "", reverse=True)
        return {"query": q, "hits": hits[:limit]}

    @staticmethod
    def _stale_items(atoms: list[Atom]) -> list[dict]:
        today = dt.date.today()
        items = []
        for a in atoms:
            if not a.ttl:
                continue
            try:
                due = dt.date.fromisoformat(a.ttl)
            except ValueError:
                continue
            overdue = (today - due).days
            if overdue > 0:
                items.append({"id": a.id, "ttl": a.ttl,
                              "topic": (a.tags[0] if a.tags else ""),
                              "days_overdue": overdue})
        items.sort(key=lambda x: -x["days_overdue"])
        return items

    def stale(self, within_days: int) -> dict:
        return {"items": self._stale_items(self.store.visible()[0])}

    def conflicts(self) -> dict:
        _, notes = self.store.visible()
        return {"items": [{"id": k, "problem": v.strip()} for k, v in notes.items() if v.strip()]}

    def recall(self, since_days: int, limit: int) -> dict:
        cutoff = (dt.datetime.now().astimezone() - dt.timedelta(days=since_days)).isoformat()
        entries = [a for a in self.store.visible()[0]
                   if a.kind == "journal" and (a.occurred_at or "") >= cutoff]
        entries.sort(key=lambda a: a.occurred_at or "", reverse=True)
        return {"since": f"{since_days}d",
                "entries": [{"occurred_at": a.occurred_at,
                             "author": f"{a.author.get('host', '?')}/{a.author.get('harness', '?')}",
                             "text": a.body} for a in entries[:limit]]}

    # ---------- 界面数据 ----------
    def snapshot(self) -> dict:
        atoms = self.store.load_atoms()
        shown, notes = self.store.visible()
        status = self.store.status_map()
        tags = Counter(t for a in shown for t in a.tags)
        conflicts = [{"id": k, "problem": v.strip()} for k, v in notes.items() if v.strip()]
        cutoff = (dt.datetime.now().astimezone() - dt.timedelta(days=7)).isoformat()
        week_new = sum(1 for a in atoms.values() if (a.occurred_at or "") >= cutoff)
        return {"atoms": atoms, "shown": shown, "notes": notes, "status": status,
                "tags": tags, "stale": self._stale_items(shown),
                "conflicts": conflicts, "week_new": week_new}

    def _ui_ctx(self, rec: dict, path: str, q: dict):
        snap = self.snapshot()
        counts = {"atoms": len(snap["atoms"]), "topics": len(snap["tags"]),
                  "review": len(snap["stale"]), "conflicts": len(snap["conflicts"])}
        ctx = web.Ctx(rec=rec, counts=counts, path=path,
                      q=(q.get("q") or [""])[0][:120],
                      msg=(q.get("msg") or [""])[0],
                      err=(q.get("err") or [""])[0])
        return ctx, snap

    def filter_atoms(self, snap: dict, *, q: str = "", kind: str = "", tag: str = "",
                     author: str = "", status: str = "active", sort: str = "new") -> list[Atom]:
        if status == "deprecated" or status == "retracted":
            pool = [a for a in snap["atoms"].values()
                    if (snap["status"].get(a.id) or {}).get("status") == status]
        elif status in ("", "all"):
            pool = list(snap["atoms"].values())
        else:
            pool = list(snap["shown"])
        if kind:
            pool = [a for a in pool if a.kind == kind]
        if tag:
            pool = [a for a in pool if tag in a.tags]
        if author:
            pool = [a for a in pool if a.author.get("host") == author]
        terms = _terms(q)
        if terms:
            pool = [a for a in pool if _matches(a, terms)]
        if sort == "old":
            pool.sort(key=lambda a: (a.occurred_at or "", a.id))
        elif sort == "kind":
            pool.sort(key=lambda a: (KIND_ORDER.index(a.kind) if a.kind in KIND_ORDER else 99,
                                     a.occurred_at or ""), reverse=False)
        else:
            pool.sort(key=lambda a: (a.occurred_at or "", a.id), reverse=True)
        return pool


def make_handler(brain: Brain):
    class Handler(BaseHTTPRequestHandler):
        server_version = "brain/" + SERVER_VERSION
        protocol_version = "HTTP/1.1"

        def log_message(self, fmt, *args):  # 只记路径与码,不记 body/令牌
            print("%s %s" % (self.address_string(), fmt % args), flush=True)

        # ---------- 工具 ----------
        def _send(self, code: int, payload, ctype="application/json"):
            if isinstance(payload, (dict, list)):
                raw = json.dumps(payload, ensure_ascii=False).encode()
            else:
                raw = str(payload).encode()
            self.send_response(code)
            self.send_header("Content-Type", f"{ctype}; charset=utf-8")
            self.send_header("Content-Length", str(len(raw)))
            self.send_header("Cache-Control", "no-store")
            self.send_header("X-Brain-Version", SERVER_VERSION)
            self.end_headers()
            self.wfile.write(raw)

        def _err(self, code: int, err: str, msg: str = ""):
            self._send(code, {"error": {"code": err, "message": msg}})

        def _auth(self, scope: str):
            hdr = self.headers.get("Authorization", "")
            tok = hdr[7:].strip() if hdr.startswith("Bearer ") else ""
            rec, err = brain.tokens.verify(tok)
            if err:
                self._err(401, err)
                return None
            if scope and scope not in rec["scopes"] and "admin" not in rec["scopes"]:
                self._err(403, "forbidden_scope", f"需要 {scope}")
                return None
            return rec

        def _body(self) -> dict | None:
            n = int(self.headers.get("Content-Length") or 0)
            if n > MAX_BODY:
                self._err(413, "too_large")
                return None
            try:
                parsed = json.loads(self.rfile.read(n) or b"{}")
            except json.JSONDecodeError:
                self._err(400, "bad_request", "JSON 解析失败")
                return None
            if not isinstance(parsed, dict):
                self._err(400, "bad_request", "JSON 必须是对象")
                return None
            return parsed

        def _query_int(self, q: dict, name: str, default: int,
                       lo: int | None = None, hi: int | None = None) -> int | None:
            try:
                value = int((q.get(name) or [str(default)])[0])
            except (TypeError, ValueError):
                self._err(400, "bad_request", f"{name} 必须是整数")
                return None
            if lo is not None and value < lo:
                self._err(400, "bad_request", f"{name} 不能小于 {lo}")
                return None
            if hi is not None and value > hi:
                self._err(400, "bad_request", f"{name} 不能大于 {hi}")
                return None
            return value

        # ---------- HTML 界面 ----------
        def _cookie_token(self) -> str:
            raw = self.headers.get("Cookie", "")
            for part in raw.split(";"):
                k, _, v = part.strip().partition("=")
                if k == "brain_token":
                    return v
            return ""

        def _html_auth(self):
            hdr = self.headers.get("Authorization", "")
            tok = hdr[7:].strip() if hdr.startswith("Bearer ") else self._cookie_token()
            rec, _ = brain.tokens.verify(tok)
            return rec

        def _redirect(self, to: str, cookie: str = ""):
            self.send_response(303)
            self.send_header("Location", to)
            if cookie:
                self.send_header("Set-Cookie", cookie)
            self.send_header("Cache-Control", "no-store")
            self.send_header("Content-Length", "0")
            self.end_headers()

        def _html(self, text: str, code: int = 200):
            raw = text.encode()
            self.send_response(code)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(raw)))
            self.send_header("Cache-Control", "no-store")
            self.send_header("X-Content-Type-Options", "nosniff")
            self.send_header("Referrer-Policy", "same-origin")
            self.send_header("X-Frame-Options", "DENY")
            self.send_header("Content-Security-Policy",
                             "default-src 'none'; style-src 'unsafe-inline'; "
                             "script-src 'unsafe-inline'; img-src data:; form-action 'self'; "
                             "base-uri 'none'; frame-ancestors 'none'")
            self.end_headers()
            self.wfile.write(raw)

        def _form(self) -> dict:
            n = int(self.headers.get("Content-Length") or 0)
            if n > MAX_BODY:
                return {}
            raw = self.rfile.read(n).decode(errors="replace")
            return {k: v[0] for k, v in parse_qs(raw, keep_blank_values=True).items()}

        def _form_lists(self) -> dict:
            n = int(self.headers.get("Content-Length") or 0)
            if n > MAX_BODY:
                return {}
            raw = self.rfile.read(n).decode(errors="replace")
            return parse_qs(raw, keep_blank_values=True)

        def _same_origin(self) -> bool:
            site = (self.headers.get("Sec-Fetch-Site") or "").lower()
            if site and site not in ("same-origin", "none"):
                return False
            origin = self.headers.get("Origin")
            if origin:
                host = self.headers.get("Host") or ""
                try:
                    if urlparse(origin).netloc != host:
                        return False
                except ValueError:
                    return False
            return True

        def _forbidden(self, ctx, message: str = "当前令牌没有访问该页面的权限。"):
            return self._html(web.error_page(ctx, 403, "权限不足", message), 403)

        def _redirect_back(self, form: dict, default: str, *, msg: str = "", err: str = ""):
            target = _sanitize_next(form.get("next", ""), default)
            parts = urlsplit(target)
            query = parse_qs(parts.query, keep_blank_values=True)
            query.pop("msg", None)
            query.pop("err", None)
            if msg:
                query["msg"] = [msg]
            if err:
                query["err"] = [err]
            location = urlunsplit(("", "", parts.path or default,
                                   urlencode(query, doseq=True), ""))
            self._redirect(location)

        # ---------- GET ----------
        def html_get(self, u, q):
            if u.path == "/login":
                if self._html_auth() is not None:
                    return self._redirect("/")
                nxt = _sanitize_next((q.get("next") or [""])[0], "")
                return self._html(web.login(next_path=nxt))
            rec = self._html_auth()
            if rec is None:
                back = u.path + (("?" + u.query) if u.query else "")
                return self._redirect("/login?next=" + quote(back, safe="/"))
            if "read" not in rec["scopes"] and "admin" not in rec["scopes"]:
                return self._html(web.login("该令牌没有读取权限"), 403)
            ctx, snap = brain._ui_ctx(rec, u.path, q)
            try:
                return self._dispatch_get(u, q, ctx, snap)
            except Exception:
                traceback.print_exc()
                return self._html(web.error_page(ctx, 500, "服务端错误",
                                                 "页面渲染失败,详情已写入服务日志。"), 500)

        def _dispatch_get(self, u, q, ctx, snap):
            path = u.path
            if path == "/":
                recent = sorted(snap["shown"], key=lambda a: a.occurred_at or "", reverse=True)[:10]
                review = [dict(it, atom=snap["atoms"].get(it["id"])) for it in snap["stale"]]
                conflicts = [dict(it, atom=snap["atoms"].get(it["id"])) for it in snap["conflicts"]]
                health = brain.health()
                body = web.dashboard(ctx, health, recent, review, conflicts,
                                     snap["tags"].most_common(40), snap["week_new"],
                                     health.get("last_rebuild", ""))
                return self._html(web.layout(ctx, "概览", body))
            if path == "/memories":
                filters = {
                    "q": (q.get("q") or [""])[0][:120],
                    "kind": (q.get("kind") or [""])[0],
                    "tag": (q.get("tag") or [""])[0],
                    "author": (q.get("author") or [""])[0],
                    "status": (q.get("status") or ["active"])[0],
                    "sort": (q.get("sort") or ["new"])[0],
                }
                pool = brain.filter_atoms(snap, **{**filters, "kind": ""})
                total = len(pool)
                pages = max(1, (total + PAGE_SIZE - 1) // PAGE_SIZE)
                try:
                    page = int((q.get("page") or ["1"])[0] or 1)
                except ValueError:
                    page = 1
                page = max(1, min(pages, page))
                if filters.get("kind"):
                    pool = [a for a in pool if a.kind == filters["kind"]]
                    total = len(pool)
                    pages = max(1, (total + PAGE_SIZE - 1) // PAGE_SIZE)
                    page = min(page, pages)
                chunk = pool[(page - 1) * PAGE_SIZE: page * PAGE_SIZE]
                options = {"tags": sorted(snap["tags"], key=lambda t: (-snap["tags"][t], t)),
                           "hosts": sorted({a.author.get("host", "") for a in snap["atoms"].values()
                                            if a.author.get("host")})}
                kind_counts = Counter(a.kind for a in brain.filter_atoms(
                    snap, **{**filters, "kind": ""}))
                body = web.memories(ctx, chunk, total, page, pages, filters, options,
                                    snap["status"], kind_counts)
                return self._html(web.layout(ctx, "记忆库", body))
            if path == "/search":
                qq = (q.get("q") or [""])[0][:120].strip()
                kind = (q.get("kind") or [""])[0]
                tag = (q.get("tag") or [""])[0]
                atoms = brain.filter_atoms(snap, q=qq, kind=kind, tag=tag,
                                           status="active", sort="new") if qq else []
                options = {"tags": sorted(snap["tags"], key=lambda t: (-snap["tags"][t], t))}
                body = web.search(ctx, qq, atoms[:100], len(atoms), options, kind, tag)
                return self._html(web.layout(ctx, "检索", body))
            if path.startswith("/atom/"):
                ident = unquote(path.split("/atom/", 1)[1])
                a = snap["atoms"].get(ident)
                if a is None:
                    return self._html(web.layout(ctx, "找不到记录", web.error_page(
                        ctx, 404, "找不到这条记录", f"没有 id 为 {ident} 的 atom,它可能从未写入。")), 404)
                status = (snap["status"].get(ident) or {}).get("status", "active")
                kids = [x for x in snap["atoms"].values() if x.supersedes == ident]
                kids.sort(key=lambda x: x.occurred_at or "")
                tombs = [t for t in brain.store.load_tombstones() if t.get("id") == ident]
                parent = snap["atoms"].get(a.supersedes) if a.supersedes else None
                body = web.atom_page(ctx, a, status, parent, kids, tombs, snap["status"])
                return self._html(web.layout(ctx, "记忆详情", body))
            if path == "/topics":
                by_tag: dict[str, list[Atom]] = {}
                for a in snap["shown"]:
                    for t in a.tags:
                        by_tag.setdefault(t, []).append(a)
                topics = []
                for tag, items in by_tag.items():
                    items.sort(key=lambda a: a.occurred_at or "", reverse=True)
                    topics.append({"tag": tag, "count": len(items),
                                   "latest": items[0].body if items else "",
                                   "kinds": Counter(a.kind for a in items).most_common()})
                topics.sort(key=lambda t: (-t["count"], t["tag"]))
                return self._html(web.layout(ctx, "主题", web.topics_page(ctx, topics)))
            if path.startswith("/topic/"):
                tag = unquote(path.split("/topic/", 1)[1])
                items = [a for a in snap["shown"] if tag in a.tags]
                items.sort(key=lambda a: a.occurred_at or "", reverse=True)
                body = web.topic_page(ctx, tag, items, snap["status"])
                return self._html(web.layout(ctx, f"#{tag}", body))
            if path == "/rules":
                items = [a for a in snap["shown"] if a.kind in ("rule", "pref")]
                items.sort(key=lambda a: a.occurred_at or "")
                groups: list[tuple[str, list[Atom]]] = []
                index: dict[str, int] = {}
                for a in items:
                    section = a.section or "通用"
                    if section not in index:
                        index[section] = len(groups)
                        groups.append((section, []))
                    groups[index[section]][1].append(a)
                return self._html(web.layout(ctx, "规范与偏好",
                                             web.rules_page(ctx, groups)))
            if path in ("/review", "/stale"):
                items = [dict(it, atom=snap["atoms"].get(it["id"])) for it in snap["stale"]]
                items = [it for it in items if it["atom"] is not None]
                return self._html(web.layout(ctx, "待复核", web.review_page(ctx, items)))
            if path == "/conflicts":
                items = [dict(it, atom=snap["atoms"].get(it["id"])) for it in snap["conflicts"]]
                return self._html(web.layout(ctx, "冲突", web.conflicts_page(ctx, items)))
            if path == "/new":
                if not ctx.can("write"):
                    return self._forbidden(ctx, "当前令牌没有写入权限,不能新增记忆。")
                sup = (q.get("supersedes") or [""])[0].strip()
                if sup and not ID_RE.fullmatch(sup):
                    sup = ""
                kind = (q.get("kind") or ["fact"])[0]
                if kind not in web.KIND_LABEL or kind == "journal":
                    kind = "fact"
                return self._html(web.layout(ctx, "记一条", web.new_page(ctx, sup, kind)))
            if path == "/tokens":
                if not ctx.can("admin"):
                    return self._forbidden(ctx, "令牌管理需要 admin scope。")
                tokens = list(reversed(brain.tokens.list()))
                return self._html(web.layout(ctx, "访问令牌", web.tokens_page(ctx, tokens)))
            if path == "/activity":
                if not ctx.can("admin"):
                    return self._forbidden(ctx, "审计日志需要 admin scope。")
                return self._html(web.layout(ctx, "活动审计",
                                             web.activity_page(ctx, brain.read_audit(200))))
            if path == "/system":
                health = brain.health()
                problems = brain.store.verify()
                du = shutil.disk_usage(str(brain.root))
                disk = {"free_gb": round(du.free / 1e9, 1), "total_gb": round(du.total / 1e9, 1)}
                body = web.system_page(ctx, health, problems, disk,
                                       health.get("last_rebuild", ""), brain.read_audit(10))
                return self._html(web.layout(ctx, "系统状态", body))
            if path == "/backup":
                if not ctx.can("admin"):
                    return self._forbidden(ctx, "备份配置需要 admin scope。")
                edit = (q.get("edit") or [""])[0].strip()
                body = web.backup_page(ctx, brain.backup.status(), brain.backup.public(), edit)
                return self._html(web.layout(ctx, "备份", body))
            if path == "/logout":
                return self._redirect("/login", "brain_token=; Path=/; Max-Age=0; HttpOnly; SameSite=Strict")
            return self._html(web.layout(ctx, "404", web.error_page(
                ctx, 404, "页面不存在", f"没有 {path} 这个页面。")), 404)

        # ---------- POST ----------
        def html_post(self, u):
            if not self._same_origin():
                return self._html(web.login("请求来源校验失败,请刷新页面后重试"), 403)
            if u.path == "/logout":
                return self._redirect("/login", "brain_token=; Path=/; Max-Age=0; HttpOnly; SameSite=Strict")
            if u.path == "/login":
                form = self._form()
                rec, _ = brain.tokens.verify(form.get("token", ""))
                if rec is None:
                    return self._html(web.login("令牌无效或已过期"), 401)
                cookie = (f"brain_token={form['token']}; Path=/; HttpOnly; SameSite=Strict; "
                          f"Max-Age=604800")
                nxt = _sanitize_next(form.get("next", ""), "/")
                if nxt == "/login":
                    nxt = "/"
                return self._redirect(nxt, cookie)
            rec = self._html_auth()
            if rec is None:
                return self._redirect("/login")
            ctx, snap = brain._ui_ctx(rec, u.path, {})
            if "read" not in rec["scopes"] and "admin" not in rec["scopes"]:
                return self._forbidden(ctx, "该令牌没有读取权限。")
            try:
                return self._dispatch_post(u, ctx, snap, rec)
            except Exception:
                traceback.print_exc()
                return self._html(web.error_page(ctx, 500, "服务端错误",
                                                 "操作失败,详情已写入服务日志。"), 500)

        def _dispatch_post(self, u, ctx, snap, rec):
            if u.path == "/compose":
                if not ctx.can("write"):
                    return self._forbidden(ctx, "当前令牌没有写入权限。")
                form = self._form()
                body = (form.get("body") or "").strip()
                if not body:
                    return self._redirect("/new?err=bad_body")
                kind = form.get("kind") or "fact"
                if kind not in KINDS or kind == "journal":
                    return self._redirect("/new?err=bad_kind")
                tags = []
                for raw in re.split(r"[,，、\s]+", form.get("tags") or ""):
                    raw = raw.strip().lstrip("#")
                    if raw and raw not in tags:
                        tags.append(raw[:40])
                ttl = (form.get("ttl") or "").strip() or None
                if ttl:
                    try:
                        dt.date.fromisoformat(ttl)
                    except ValueError:
                        return self._redirect("/new?err=bad_ttl")
                supersedes = (form.get("supersedes") or "").strip() or None
                if supersedes:
                    if not ID_RE.match(supersedes):
                        return self._redirect("/new?err=bad_id")
                    if supersedes not in snap["atoms"]:
                        return self._redirect("/new?err=supersedes_missing")
                confidence = form.get("confidence") or "medium"
                if confidence not in ("high", "medium", "low", "guess"):
                    confidence = "medium"
                payload = {"id": new_id(), "kind": kind, "body": body, "tags": tags[:16],
                           "confidence": confidence, "source": (form.get("source") or "web")[:200],
                           "supersedes": supersedes, "ttl": ttl}
                code, data = brain.remember(payload, rec["author"])
                if code not in (200, 201):
                    return self._redirect("/new?err=bad_body")
                return self._redirect(f"/atom/{data.get('id', '')}?msg=created")
            if u.path == "/forget":
                if not ctx.can("write"):
                    return self._forbidden(ctx, "当前令牌没有写入权限。")
                form = self._form()
                ident = (form.get("id") or "").strip()
                status = form.get("status") or "deprecated"
                if ident not in snap["atoms"]:
                    return self._redirect_back(form, "/memories", err="not_found")
                if status not in ("deprecated", "retracted", "active"):
                    return self._redirect_back(form, "/memories", err="bad_status")
                replaced_by = (form.get("replaced_by") or "").strip()
                if status == "retracted":
                    if not replaced_by or replaced_by not in snap["atoms"]:
                        return self._redirect_back(form, f"/atom/{ident}", err="need_replaced_by")
                brain.forget({"id": ident, "status": status,
                              "reason": (form.get("reason") or "")[:300],
                              "replaced_by": replaced_by or None}, rec["author"])
                msg = {"deprecated": "deprecated", "retracted": "retracted",
                       "active": "restored"}.get(status, "deprecated")
                return self._redirect_back(form, f"/atom/{ident}", msg=msg)
            if u.path == "/renew":
                if not ctx.can("write"):
                    return self._forbidden(ctx, "当前令牌没有写入权限。")
                form = self._form()
                ident = (form.get("id") or "").strip()
                a = snap["atoms"].get(ident)
                if a is None:
                    return self._redirect_back(form, "/review", err="not_found")
                try:
                    days = max(1, min(3650, int(form.get("days") or 180)))
                except ValueError:
                    days = 180
                ttl = (dt.date.today() + dt.timedelta(days=days)).isoformat()
                payload = {"id": new_id(), "kind": a.kind, "body": a.body, "tags": list(a.tags),
                           "confidence": a.confidence, "source": f"web:renew+{days}d",
                           "supersedes": a.id, "ttl": ttl}
                code, data = brain.remember(payload, rec["author"])
                if code not in (200, 201):
                    return self._redirect_back(form, f"/atom/{ident}", err="bad_body")
                brain.audit("renew", _who(rec["author"]), ident, f"days={days} new={data.get('id')}")
                return self._redirect(f"/atom/{data.get('id')}?msg=renewed")
            if u.path == "/token-issue":
                if not ctx.can("admin"):
                    return self._forbidden(ctx, "签发令牌需要 admin scope。")
                vals = self._form_lists()
                host = (vals.get("host") or [""])[0].strip()
                harness = (vals.get("harness") or [""])[0].strip()
                role = (vals.get("role") or [""])[0].strip() or "engineer"
                if not all(re.fullmatch(r"[A-Za-z0-9._-]{1,24}", v) for v in (host, harness, role)):
                    return self._redirect("/tokens?err=bad_token_form")
                scopes = [s for s in vals.get("scopes", []) if s in authmod.SCOPES]
                if not scopes:
                    scopes = ["read"]
                try:
                    ttl_days = max(0, min(3650, int((vals.get("ttl") or ["365"])[0])))
                except ValueError:
                    ttl_days = 365
                with brain.lock:
                    token, trec = brain.tokens.issue(host, harness, role, scopes, ttl_days)
                brain.audit("token_issue", _who(rec["author"]), trec["id"],
                            f"{host}/{harness}/{role} scopes={','.join(scopes)}")
                return self._html(web.token_issued(ctx, token, trec))
            if u.path == "/token-revoke":
                if not ctx.can("admin"):
                    return self._forbidden(ctx, "吊销令牌需要 admin scope。")
                form = self._form()
                ident = (form.get("id") or "").strip()
                with brain.lock:
                    ok = brain.tokens.revoke(ident)
                if ok:
                    brain.audit("token_revoke", _who(rec["author"]), ident, "")
                return self._redirect("/tokens?msg=revoked" if ok else "/tokens?err=not_found")
            if u.path == "/reindex":
                if not ctx.can("admin"):
                    return self._forbidden(ctx, "重建视图需要 admin scope。")
                with brain.lock:
                    brain.store.rebuild(force=True)
                brain.audit("reindex", _who(rec["author"]), "", "")
                return self._redirect("/system?msg=reindexed")
            if u.path == "/backup/run":
                if not ctx.can("admin"):
                    return self._forbidden(ctx, "备份需要 admin scope。")
                if brain.backup.running:
                    return self._redirect("/backup?err=backup_busy")
                threading.Thread(target=brain.backup.run, args=("manual",), daemon=True).start()
                brain.audit("backup_run", _who(rec["author"]), "", "manual")
                return self._redirect("/backup?msg=backup_started")
            if u.path == "/backup/dest":
                if not ctx.can("admin"):
                    return self._forbidden(ctx, "备份配置需要 admin scope。")
                form = self._form()
                action = (form.get("action") or "save").strip()
                cfg = brain.backup.load()
                ident = (form.get("id") or "").strip()
                if action == "delete":
                    before = len(cfg["destinations"])
                    cfg["destinations"] = [d for d in cfg["destinations"] if d.get("id") != ident]
                    if len(cfg["destinations"]) == before:
                        return self._redirect("/backup?err=not_found")
                    brain.backup.save(cfg)
                    brain.backup.recompute_next()
                    brain.audit("backup_dest", _who(rec["author"]), ident, "delete")
                    return self._redirect("/backup?msg=dest_deleted")
                dtype = (form.get("type") or "local").strip()
                label = (form.get("label") or "").strip()[:40]
                path = (form.get("path") or "").strip()
                url = (form.get("url") or "").strip()
                username = (form.get("username") or "").strip()[:80]
                password = form.get("password") or ""
                enabled = bool(form.get("enabled"))
                if dtype not in ("local", "webdav"):
                    return self._redirect("/backup?err=bad_dest")
                if dtype == "local" and not path:
                    return self._redirect("/backup?err=bad_dest")
                if dtype == "webdav" and not (url and username):
                    return self._redirect("/backup?err=bad_dest")
                if dtype == "webdav" and not url.startswith(("http://", "https://")):
                    return self._redirect("/backup?err=bad_dest")
                new_dest = {"id": ident or ("d_" + os.urandom(4).hex()), "type": dtype,
                            "label": label, "enabled": enabled, "path": path,
                            "url": url, "username": username}
                old_dest = next((d for d in cfg["destinations"] if d.get("id") == ident), None)
                if old_dest and not password:
                    new_dest["password"] = old_dest.get("password", "")
                else:
                    new_dest["password"] = password
                if action == "test" and old_dest is not None:
                    try:
                        dest = brain.backup.make_destination(old_dest)
                        msg = dest.test()
                    except Exception as e:  # noqa: BLE001 - 网络/凭据错误都要展示给人看
                        return self._redirect("/backup?err=bad_dest&detail=" + quote(str(e)[:120]))
                    brain.audit("backup_dest", _who(rec["author"]), ident, "test ok " + msg)
                    return self._redirect("/backup?msg=dest_ok")
                if old_dest is not None:
                    cfg["destinations"] = [new_dest if d.get("id") == ident else d
                                           for d in cfg["destinations"]]
                else:
                    cfg["destinations"].append(new_dest)
                brain.backup.save(cfg)
                brain.backup.recompute_next()
                brain.audit("backup_dest", _who(rec["author"]), new_dest["id"],
                            f"{dtype} {label or path or url} enabled={enabled}")
                return self._redirect("/backup?msg=dest_saved")
            if u.path == "/backup/schedule":
                if not ctx.can("admin"):
                    return self._forbidden(ctx, "备份配置需要 admin scope。")
                form = self._form()
                cfg = brain.backup.load()
                mode = (form.get("mode") or "off").strip()
                at = (form.get("at") or "03:40").strip()
                if mode not in ("off", "daily", "weekly", "every"):
                    return self._redirect("/backup?err=bad_schedule")
                if not re.fullmatch(r"\d{1,2}:\d{2}", at):
                    return self._redirect("/backup?err=bad_schedule")
                hh, mm = (int(x) for x in at.split(":"))
                if not (0 <= hh < 24 and 0 <= mm < 60):
                    return self._redirect("/backup?err=bad_schedule")

                def _int(name, default, lo, hi):
                    try:
                        return max(lo, min(hi, int((form.get(name) or "").strip() or default)))
                    except ValueError:
                        return default

                cfg["enabled"] = bool(form.get("enabled"))
                cfg["schedule"] = {"mode": mode, "at": f"{hh:02d}:{mm:02d}",
                                   "weekday": _int("weekday", 0, 0, 6),
                                   "every_hours": _int("every_hours", 6, 1, 168),
                                   "jitter_min": _int("jitter_min", 30, 0, 360)}
                cfg["retention"] = {"daily": _int("daily", 30, 1, 365),
                                    "keep_monthly": bool(form.get("keep_monthly"))}
                brain.backup.save(cfg)
                nxt = brain.backup.recompute_next()
                brain.audit("backup_config", _who(rec["author"]), "",
                            f"mode={mode} at={at} enabled={cfg['enabled']} next={nxt}")
                return self._redirect("/backup?msg=backup_saved")
            if u.path == "/backup/key":
                if not ctx.can("admin"):
                    return self._forbidden(ctx, "变更备份密钥需要 admin scope。")
                form = self._form()
                action = (form.get("action") or "").strip()
                if action == "generate":
                    try:
                        pub, priv = brain.backup.gen_key()
                    except Exception as e:  # noqa: BLE001
                        print(f"backup gen_key failed: {e}")
                        return self._redirect("/backup?err=age_missing")
                    brain.audit("backup_key", _who(rec["author"]), "", "generate")
                    return self._html(web.layout(ctx, "私钥",
                                                 web.backup_key_page(ctx, pub, priv,
                                                                     str(brain.backup.key_path))))
                rcpt = (form.get("recipient") or "").strip()
                try:
                    pub = brain.backup.import_recipient(rcpt)
                except Exception:  # noqa: BLE001
                    return self._redirect("/backup?err=key_bad")
                brain.audit("backup_key", _who(rec["author"]), "", "import")
                return self._redirect("/backup?msg=key_imported")
            return self._html(web.layout(ctx, "404", web.error_page(
                ctx, 404, "页面不存在", f"没有 {u.path} 这个页面。")), 404)

        # ---------- 路由 ----------
        def do_GET(self):
            u = urlparse(self.path)
            if not u.path.startswith("/v1"):
                return self.html_get(u, parse_qs(u.query, keep_blank_values=True))
            q = parse_qs(u.query)
            if u.path == "/v1/health":
                return self._send(200, brain.health())
            if u.path == "/v1/client":
                p = brain.root / "bundle" / "brain.pyz"
                if not p.exists():
                    return self._err(404, "not_found", "bundle 未构建")
                raw = p.read_bytes()
                self.send_response(200)
                self.send_header("Content-Type", "application/zip")
                self.send_header("Content-Length", str(len(raw)))
                self.send_header("Content-Disposition", 'attachment; filename="brain.pyz"')
                self.send_header("Cache-Control", "no-store")
                self.end_headers()
                self.wfile.write(raw)
                return
            scope = SCOPE_FOR.get(("GET", u.path))
            if scope is None:
                return self._err(404, "not_found")
            rec = self._auth(scope)
            if rec is None:
                return
            if u.path == "/v1/whoami":
                return self._send(200, {"host": rec["author"]["host"], "harness": rec["author"]["harness"],
                                        "role": rec["author"]["role"], "role_source": "token",
                                        "scopes": rec["scopes"], "server_version": SERVER_VERSION})
            if u.path == "/v1/brief":
                budget = self._query_int(q, "budget", 6000, 1, 1_000_000)
                if budget is None:
                    return
                text, dropped, err = render_brief(brain.root, brain.store, budget)
                if err:
                    return self._err(413, err, f"预算 {budget} 太小")
                return self._send(200, text, ctype="text/plain")
            if u.path == "/v1/search":
                qs = (q.get("q") or [""])[0]
                if not qs:
                    return self._err(400, "bad_request", "q 必填")
                if len(qs) > 200:
                    return self._err(400, "bad_request", "q 过长")
                limit = self._query_int(q, "limit", 20, 1, 200)
                if limit is None:
                    return
                return self._send(200, brain.search(qs, limit,
                                                    (q.get("include_deprecated") or ["false"])[0] == "true"))
            if u.path == "/v1/stale":
                try:
                    within = int((q.get("within") or ["30"])[0].rstrip("d"))
                except ValueError:
                    return self._err(400, "bad_request", "within 必须是整数")
                return self._send(200, brain.stale(max(1, within)))
            if u.path == "/v1/conflicts":
                return self._send(200, brain.conflicts())
            if u.path == "/v1/backup/status":
                return self._send(200, brain.backup.status())
            if u.path == "/v1/recall":
                try:
                    since = int((q.get("since") or ["7d"])[0].rstrip("d"))
                except ValueError:
                    return self._err(400, "bad_request", "since 必须是整数")
                limit = self._query_int(q, "limit", 20, 1, 200)
                if limit is None:
                    return
                return self._send(200, brain.recall(max(1, since), limit))
            return self._err(404, "not_found")

        def do_POST(self):
            u = urlparse(self.path)
            if not u.path.startswith("/v1"):
                return self.html_post(u)
            scope = SCOPE_FOR.get(("POST", u.path))
            if scope is None:
                return self._err(404, "not_found")
            rec = self._auth(scope)
            if rec is None:
                return
            body = self._body()
            if body is None:
                return
            if u.path == "/v1/remember":
                code, payload = brain.remember(body, rec["author"])
                return self._send(code, payload)
            if u.path == "/v1/forget":
                code, payload = brain.forget(body, rec["author"])
                return self._send(code, payload)
            if u.path == "/v1/journal":
                body.setdefault("kind", "journal")
                body.setdefault("tags", ["journal"])
                code, payload = brain.remember(body, rec["author"])
                return self._send(code, payload)
            if u.path == "/v1/reindex":
                with brain.lock:
                    rev = brain.store.rebuild(force=True)
                brain.audit("reindex", _who(rec["author"]), "", "api")
                return self._send(200, {"views_rev": rev})
            if u.path == "/v1/backup/run":
                if brain.backup.running:
                    return self._err(409, "backup_busy")
                threading.Thread(target=brain.backup.run, args=("api",), daemon=True).start()
                brain.audit("backup_run", _who(rec["author"]), "", "api")
                return self._send(202, {"started": True})
            return self._err(404, "not_found")

    return Handler


def serve(root: Path, bind: str, port: int) -> None:
    brain = Brain(root)
    # 进程内调度:页面改完频率就生效,不依赖 cron / systemd
    backupmod.Scheduler(brain.backup).start()
    httpd = ThreadingHTTPServer((bind, port), make_handler(brain))
    print(f"brain serve on http://{bind}:{port}  root={root}  atoms={len(brain.store.load_atoms())}", flush=True)
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        httpd.server_close()
