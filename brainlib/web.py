"""人读界面:服务端渲染,零依赖,不需要前端构建。

设计取舍:
  * 不引 React/Vue —— 这套系统的前提是"任何一台机器 clone 下来就能跑";
  * 所有用户内容先 html.escape,正文只做极有限的排版增强(列表/行内代码/链接),
    插入的标签全部由本模块生成,不解析任意 HTML;
  * 认证复用同一套令牌,登录后写 HttpOnly + SameSite=Strict cookie;
  * 浅色/深色、桌面/移动端共用一个 CSS,主题切换只用 20 行原生 JS。
"""
from __future__ import annotations

import html
import re
from dataclasses import dataclass, field
from datetime import date, datetime
from urllib.parse import quote, urlencode

import os
from pathlib import Path

from .model import Atom

BRAIN_ROOT = Path(os.environ.get("BRAIN_ROOT")
                  or Path(__file__).resolve().parent.parent).resolve()

# ---------------------------------------------------------------- 常量

KIND_LABEL = {
    "fact": "事实", "rule": "规范", "pref": "偏好", "lesson": "踩坑",
    "host": "主机", "project": "项目", "idea": "想法", "secret-ref": "密钥引用",
    "journal": "日记",
}
KIND_CLASS = {
    "fact": "k-fact", "rule": "k-rule", "pref": "k-pref", "lesson": "k-lesson",
    "host": "k-host", "project": "k-project", "idea": "k-idea",
    "secret-ref": "k-secret", "journal": "k-journal",
}
CONF_LABEL = {"high": "高", "medium": "中", "low": "低", "guess": "猜测"}
STATUS_LABEL = {"active": "正常", "deprecated": "已过期", "retracted": "已撤回"}
STATUS_CLASS = {"active": "st-ok", "deprecated": "st-warn", "retracted": "st-danger"}
ACTION_LABEL = {
    "remember": "写入记忆", "forget": "变更状态", "renew": "续期",
    "reindex": "重建视图", "token_issue": "签发令牌", "token_revoke": "吊销令牌",
    "backup_run": "手动备份", "backup_config": "改备份配置",
    "backup_dest": "改备份目的地", "backup_key": "变更备份密钥",
}

MSG = {
    "created": ("已写入记忆库", "ok"),
    "deprecated": ("已标记为过期,默认列表不再显示", "ok"),
    "restored": ("已恢复为正常状态", "ok"),
    "retracted": ("已撤回,并记录了替代记录", "ok"),
    "renewed": ("已续期,并生成一条取代旧记录的新记忆", "ok"),
    "revoked": ("令牌已吊销", "ok"),
    "reindexed": ("视图已重建", "ok"),
    "backup_started": ("备份已开始,跑完刷新页面看结果", "ok"),
    "backup_saved": ("备份配置已保存", "ok"),
    "dest_saved": ("目的地已保存", "ok"),
    "dest_deleted": ("目的地已删除", "ok"),
    "dest_ok": ("目的地连通", "ok"),
    "key_imported": ("公钥已导入", "ok"),
    "logged_out": ("已退出登录", "ok"),
}
ERRS = {
    "bad_body": ("正文不能为空", "err"),
    "bad_id": ("记录 id 格式不正确", "err"),
    "not_found": ("找不到这条记录", "err"),
    "bad_status": ("不支持的状态变更", "err"),
    "need_replaced_by": ("撤回必须指定一条已存在的替代记录", "err"),
    "supersedes_missing": ("取代的旧记录不存在", "err"),
    "bad_kind": ("记忆类型不合法", "err"),
    "bad_ttl": ("有效期格式应为 YYYY-MM-DD", "err"),
    "forbidden": ("当前令牌没有执行该操作的权限", "err"),
    "bad_token_form": ("令牌参数不合法:身份只能用字母数字与 - _ .", "err"),
    "backup_busy": ("已有一份备份在跑,等它结束再来", "err"),
    "age_missing": ("服务器上找不到 age / age-keygen,无法加密备份", "err"),
    "bad_dest": ("目的地参数不完整:本地目录要填路径,WebDAV 要填地址和用户名", "err"),
    "bad_schedule": ("频率或保留参数不合法", "err"),
    "key_bad": ("公钥格式不对,应形如 age1…", "err"),
    "backup_failed": ("备份没有成功,原因见页面上的最近报告", "err"),
    "csrf": ("请求来源校验失败,请刷新页面后重试", "err"),
}

# ---------------------------------------------------------------- 图标

ICONS = {
    "home": '<path d="M3.5 10.5 12 3l8.5 7.5"/><path d="M5.5 9.5V21h13V9.5"/><path d="M9.5 21v-6h5v6"/>',
    "database": '<ellipse cx="12" cy="5" rx="8" ry="3"/><path d="M4 5v6c0 1.7 3.6 3 8 3s8-1.3 8-3V5"/><path d="M4 11v6c0 1.7 3.6 3 8 3s8-1.3 8-3v-6"/>',
    "tag": '<path d="M20.6 13.4 12 22l-9-9V4h9l8.6 8.6a1.4 1.4 0 0 1 0 2Z"/><circle cx="7.5" cy="7.5" r="1.3"/>',
    "book": '<path d="M4 19.5A2.5 2.5 0 0 1 6.5 17H20"/><path d="M6.5 2H20v20H6.5A2.5 2.5 0 0 1 4 19.5v-15A2.5 2.5 0 0 1 6.5 2Z"/>',
    "clock": '<circle cx="12" cy="12" r="9"/><path d="M12 7v5l3 2"/>',
    "alert": '<path d="M12 3 2.5 20h19L12 3Z"/><path d="M12 9v5"/><circle cx="12" cy="17" r=".7"/>',
    "check": '<path d="m5 12 4 4L19 6"/>',
    "key": '<circle cx="8" cy="12" r="4"/><path d="M12 12h9m-3 0v3m-3-3v2"/>',
    "activity": '<path d="M3 12h4l2-6 4 12 2-6h6"/>',
    "server": '<rect x="3" y="4" width="18" height="7" rx="2"/><rect x="3" y="13" width="18" height="7" rx="2"/><path d="M7 7.5h.01M7 16.5h.01"/>',
    "search": '<circle cx="11" cy="11" r="7"/><path d="m20 20-3.5-3.5"/>',
    "plus": '<path d="M12 5v14M5 12h14"/>',
    "menu": '<path d="M4 6h16M4 12h16M4 18h16"/>',
    "x": '<path d="m6 6 12 12M18 6 6 18"/>',
    "sun": '<circle cx="12" cy="12" r="4"/><path d="M12 2v2m0 16v2M2 12h2m16 0h2M4.9 4.9l1.4 1.4m11.4 11.4 1.4 1.4M19.1 4.9l-1.4 1.4M6.3 17.7l-1.4 1.4"/>',
    "moon": '<path d="M20 14.5A8 8 0 0 1 9.5 4 8 8 0 1 0 20 14.5Z"/>',
    "arrow": '<path d="M5 12h14m-6-6 6 6-6 6"/>',
    "copy": '<rect x="9" y="9" width="11" height="11" rx="2"/><path d="M5 15V6a2 2 0 0 1 2-2h9"/>',
    "link": '<path d="M10 13a5 5 0 0 0 7 0l2-2a5 5 0 0 0-7-7l-1 1"/><path d="M14 11a5 5 0 0 0-7 0l-2 2a5 5 0 0 0 7 7l1-1"/>',
    "logout": '<path d="M9 21H5a2 2 0 0 1-2-2V5a2 2 0 0 1 2-2h4"/><path d="m16 17 5-5-5-5"/><path d="M21 12H9"/>',
    "refresh": '<path d="M21 12a9 9 0 1 1-2.6-6.4"/><path d="M21 3v6h-6"/>',
    "user": '<circle cx="12" cy="8" r="4"/><path d="M4 21a8 8 0 0 1 16 0"/>',
    "file": '<path d="M14 3H6a2 2 0 0 0-2 2v14a2 2 0 0 0 2 2h12a2 2 0 0 0 2-2V9l-6-6Z"/><path d="M14 3v6h6"/>',
    "filter": '<path d="M3 5h18l-7 8v6l-4 2v-8L3 5Z"/>',
    "chevron": '<path d="m9 6 6 6-6 6"/>',
    "shield": '<path d="M12 3 5 6v6c0 4.5 3 7.5 7 9 4-1.5 7-4.5 7-9V6l-7-3Z"/><path d="m9 12 2 2 4-4"/>',
    "network": '<circle cx="12" cy="5" r="2.2"/><circle cx="5" cy="19" r="2.2"/><circle cx="19" cy="19" r="2.2"/><path d="M10.5 6.6 6.5 17M13.5 6.6l4 10.4M7.2 19h9.6"/>',
    "inbox": '<path d="M3 13h5l2 3h4l2-3h5"/><path d="M5.5 4h13l2.5 9v6a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2v-6l2.5-9Z"/>',
    "edit": '<path d="M12 20h9"/><path d="M16.5 3.5a2.1 2.1 0 0 1 3 3L7 19l-4 1 1-4Z"/>',
}


def icon(name: str, size: int = 18, cls: str = "ico") -> str:
    body = ICONS.get(name, ICONS["file"])
    return (f'<svg class="{cls}" width="{size}" height="{size}" viewBox="0 0 24 24" '
            f'fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" '
            f'stroke-linejoin="round" aria-hidden="true">{body}</svg>')


# ---------------------------------------------------------------- 上下文

@dataclass
class Ctx:
    rec: dict
    counts: dict = field(default_factory=dict)
    path: str = "/"
    q: str = ""
    msg: str = ""
    err: str = ""

    @property
    def scopes(self) -> list[str]:
        return list(self.rec.get("scopes") or [])

    def can(self, scope: str) -> bool:
        return scope in self.scopes or "admin" in self.scopes

    @property
    def author(self) -> str:
        a = self.rec.get("author") or {}
        return f"{a.get('host', '?')}/{a.get('harness', '?')}"


# ---------------------------------------------------------------- 基础工具

def esc(value) -> str:
    return html.escape(str(value), quote=True)


def qs(path: str, **params) -> str:
    clean = {k: v for k, v in params.items() if v not in (None, "")}
    return path + ("?" + urlencode(clean) if clean else "")


def when(iso: str, full: bool = False) -> str:
    if not iso:
        return "—"
    try:
        d = datetime.fromisoformat(iso)
    except ValueError:
        return iso[:16]
    if d.tzinfo is None:
        d = d.replace(tzinfo=datetime.now().astimezone().tzinfo)
    now = datetime.now(d.tzinfo)
    if full:
        return d.strftime("%Y-%m-%d %H:%M")
    delta = (now.date() - d.date()).days
    if delta == 0:
        return f"今天 {d:%H:%M}"
    if delta == 1:
        return f"昨天 {d:%H:%M}"
    if 1 < delta < 7:
        return f"{delta} 天前"
    if d.year == now.year:
        return f"{d:%m-%d}"
    return f"{d:%Y-%m-%d}"


def plain(text: str) -> str:
    """预览用:去掉行内 markdown 记号,详情页仍显示原排版。"""
    text = re.sub(r"`([^`\n]*)`", r"\1", text or "")
    text = re.sub(r"\*\*([^*\n]+)\*\*", r"\1", text)
    return text


def first_line(text: str, width: int = 120) -> str:
    line = ""
    for raw in (text or "").splitlines():
        line = plain(raw).strip().lstrip("-*").strip()
        if line.startswith(">"):
            line = line[1:].strip()
        if line:
            break
    if len(line) > width:
        line = line[: width - 1] + "…"
    return line or "(空)"


def highlight(text: str, terms: list[str]) -> str:
    """转义后高亮;只在纯文本上查找,插入的标签都是本模块生成的。"""
    if not terms:
        return esc(text)
    pattern = re.compile("|".join(re.escape(t) for t in terms if t), re.I)
    out, pos = [], 0
    for m in pattern.finditer(text):
        out.append(esc(text[pos:m.start()]))
        out.append("<mark>" + esc(m.group(0)) + "</mark>")
        pos = m.end()
    out.append(esc(text[pos:]))
    return "".join(out)


_URL_RE = re.compile(r"https?://[^\s<>\"']+")


def _linkify(m: re.Match) -> str:
    # 入参已经过 html.escape,这里不能再转义一次(否则 & 会变成 &amp;amp;)
    url = m.group(0)
    tail = ""
    while url and url[-1] in ".,;:!?、。)）】":
        tail = url[-1] + tail
        url = url[:-1]
    if not url:
        return m.group(0)
    return (f'<a href="{url}" target="_blank" rel="noopener noreferrer">{url}</a>'
            + tail)


def prose(text: str) -> str:
    """极简、安全的正文排版:先转义,再识别行内代码/粗体/链接/列表/代码块。"""
    safe = esc(text)
    safe = re.sub(r"`([^`\n]+)`", r"<code>\1</code>", safe)
    safe = re.sub(r"\*\*([^*\n]+)\*\*", r"<strong>\1</strong>", safe)
    safe = _URL_RE.sub(_linkify, safe)

    out: list[str] = []
    in_ul = False
    in_code = False
    for line in safe.split("\n"):
        stripped = line.strip()
        if stripped.startswith("```"):
            if in_code:
                out.append("</code></pre>")
                in_code = False
            else:
                if in_ul:
                    out.append("</ul>")
                    in_ul = False
                out.append('<pre class="code"><code>')
                in_code = True
            continue
        if in_code:
            out.append(line)
            continue
        if not stripped:
            if in_ul:
                out.append("</ul>")
                in_ul = False
            continue
        if stripped.startswith(("- ", "* ")):
            if not in_ul:
                out.append("<ul>")
                in_ul = True
            out.append(f"<li>{stripped[2:]}</li>")
            continue
        if in_ul:
            out.append("</ul>")
            in_ul = False
        if stripped.startswith("&gt;"):
            out.append(f"<blockquote>{stripped[4:].lstrip()}</blockquote>")
            continue
        if stripped.startswith("### "):
            out.append(f"<h4>{stripped[4:]}</h4>")
        elif stripped.startswith("## "):
            out.append(f"<h3>{stripped[3:]}</h3>")
        elif stripped.startswith("# "):
            out.append(f"<h2>{stripped[2:]}</h2>")
        else:
            out.append(f"<p>{stripped}</p>")
    if in_ul:
        out.append("</ul>")
    if in_code:
        out.append("</code></pre>")
    return "".join(out)


def kind_dot(kind: str) -> str:
    return f'<span class="kind-dot {KIND_CLASS.get(kind, "k-fact")}" aria-hidden="true"></span>'


def kind_pill(kind: str) -> str:
    return (f'<span class="kind-pill {KIND_CLASS.get(kind, "k-fact")}">'
            f'{esc(KIND_LABEL.get(kind, kind))}</span>')


def status_pill(status: str) -> str:
    if not status or status == "active":
        return '<span class="status st-ok">正常</span>'
    return (f'<span class="status {STATUS_CLASS.get(status, "st-warn")}">'
            f'{esc(STATUS_LABEL.get(status, status))}</span>')


def tag_chip(tag: str, count: int | None = None, active: bool = False) -> str:
    n = f'<b>{count}</b>' if count is not None else ""
    cls = "chip active" if active else "chip"
    return f'<a class="{cls}" href="/topic/{quote(tag, safe="")}">#{esc(tag)} {n}</a>'


def flash(ctx: Ctx) -> str:
    if ctx.msg and ctx.msg in MSG:
        text, tone = MSG[ctx.msg]
        ic = "check" if tone == "ok" else "alert"
        return f'<div class="flash {tone}" role="status">{icon(ic, 17)}<span>{esc(text)}</span></div>'
    if ctx.err and ctx.err in ERRS:
        text, tone = ERRS[ctx.err]
        return f'<div class="flash err" role="alert">{icon("alert", 17)}<span>{esc(text)}</span></div>'
    return ""


def page_head(title: str, desc: str = "", actions: str = "", eyebrow: str = "") -> str:
    eb = f'<p class="eyebrow">{esc(eyebrow)}</p>' if eyebrow else ""
    return (f'<div class="page-head"><div class="page-head-text">{eb}'
            f'<h2 class="page-title">{esc(title)}</h2>'
            f'<p class="page-desc">{desc}</p></div>'
            f'<div class="head-actions">{actions}</div></div>')


def empty(icon_name: str, title: str, text: str, action_html: str = "") -> str:
    return (f'<div class="empty">{icon(icon_name, 26)}'
            f'<p class="empty-title">{esc(title)}</p>'
            f'<p class="empty-text">{esc(text)}</p>{action_html}</div>')


def opt(value: str, label: str, selected: bool) -> str:
    sel = " selected" if selected else ""
    return f'<option value="{esc(value)}"{sel}>{esc(label)}</option>'


def select(name: str, options: list[tuple[str, str]], value: str, label: str = "") -> str:
    opts = "".join(opt(v, text, v == value) for v, text in options)
    lab = f'<label for="f-{name}">{esc(label)}</label>' if label else ""
    return f'<div class="field">{lab}<select id="f-{name}" name="{esc(name)}">{opts}</select></div>'


def search_terms(q: str) -> list[str]:
    return [t for t in (q or "").split() if t][:8]


def snippet(body: str, q: str, width: int = 180) -> str:
    text = plain((body or "").replace("\r", " ").replace("\n", " "))
    if q:
        terms = search_terms(q)
        pos = -1
        for t in terms:
            pos = text.lower().find(t.lower())
            if pos >= 0:
                break
        if pos > width // 3:
            text = "…" + text[max(0, pos - width // 3):]
    if len(text) > width:
        text = text[: width - 1] + "…"
    return highlight(text, search_terms(q))


# ---------------------------------------------------------------- 布局

NAV = [
    ("工作台", [
        ("home", "概览", "/", None),
        ("database", "记忆库", "/memories", "atoms"),
        ("tag", "主题", "/topics", "topics"),
    ]),
    ("治理", [
        ("clock", "待复核", "/review", "review"),
        ("alert", "冲突", "/conflicts", "conflicts"),
        ("book", "规范", "/rules", None),
    ]),
    ("系统", [
        ("key", "访问令牌", "/tokens", None),
        ("activity", "活动审计", "/activity", None),
        ("server", "系统状态", "/system", None),
        ("shield", "备份", "/backup", None),
    ]),
]


def _active(path: str, href: str) -> bool:
    if href == "/":
        return path == "/"
    if href == "/memories":
        return path == "/memories" or path.startswith("/atom/") or path == "/new"
    if href == "/topics":
        return path == "/topics" or path.startswith("/topic/")
    return path == href or path.startswith(href + "/")


def _nav(ctx: Ctx) -> str:
    out = []
    for group, items in NAV:
        links = []
        for icon_name, label, href, count_key in items:
            if href in ("/tokens", "/activity", "/backup") and not ctx.can("admin"):
                continue
            active = _active(ctx.path, href)
            badge = ""
            if count_key:
                n = ctx.counts.get(count_key) or 0
                if n:
                    tone = "danger" if count_key == "conflicts" else "warn"
                    badge = f'<span class="nav-badge {tone}">{n}</span>'
            links.append(
                f'<a class="nav-item{" active" if active else ""}" href="{href}"'
                f'{" aria-current=page" if active else ""}>{icon(icon_name, 18)}'
                f'<span>{esc(label)}</span>{badge}</a>')
        if links:
            out.append(f'<div class="nav-group">{esc(group)}</div>' + "".join(links))
    return "".join(out)


def layout(ctx: Ctx, title: str, body: str) -> str:
    initial = esc((ctx.rec.get("author") or {}).get("host", "?")[:1].upper())
    scopes = " · ".join(ctx.scopes)
    return f"""<!doctype html><html lang="zh-CN"><head>
<meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<meta name="color-scheme" content="light dark">
<title>{esc(title)} · 共享记忆库</title>
<script>{EARLY_JS}</script>
<style>{CSS}</style>
</head><body>
<a class="skip" href="#main">跳到主要内容</a>
<div class="app">
<aside class="sidebar" id="sidebar">
  <a class="brand" href="/">
    <span class="brand-mark">{icon("network", 20)}</span>
    <span class="brand-text"><span class="brand-name">共享记忆库</span>
    <span class="brand-sub">BRAIN · 记忆库</span></span>
  </a>
  <nav class="nav" aria-label="主导航">{_nav(ctx)}</nav>
  <div class="side-foot">
    <div class="user">
      <span class="avatar">{initial}</span>
      <span class="user-meta"><b>{esc(ctx.author)}</b><span>{esc(scopes)} · {esc((ctx.rec.get("author") or {}).get("role", ""))}</span></span>
      <form method="post" action="/logout" class="logout-form"><button class="icon-btn" type="submit" title="退出登录" aria-label="退出登录">{icon("logout", 17)}</button></form>
    </div>
  </div>
</aside>
<div class="scrim" data-nav-close></div>
<div class="main">
  <header class="topbar"><div class="topbar-inner">
    <button class="icon-btn nav-toggle" data-nav-toggle aria-label="打开导航">{icon("menu", 20)}</button>
    <a class="crumb" href="/"><span class="crumb-root">共享记忆库</span></a>
    <form class="searchbox" action="/search" method="get" role="search">
      {icon("search", 17)}
      <input type="search" name="q" value="{esc(ctx.q)}" placeholder="搜索记忆、标签、作者…" aria-label="搜索记忆">
      <kbd>/</kbd>
    </form>
    <div class="top-actions">
      <button class="icon-btn" data-theme-toggle aria-label="切换深色/浅色">{icon("sun", 18, "ico icon-sun")}{icon("moon", 18, "ico icon-moon")}</button>
      <a class="btn primary" href="/new">{icon("plus", 16)}<span>记一条</span></a>
    </div>
  </div></header>
  <main class="content" id="main">
    {flash(ctx)}
    {body}
  </main>
  <footer class="foot">数据目录 <code>{esc(str(BRAIN_ROOT))}</code> · atom 不可变,视图可重建 ·
    <a href="/v1/health" target="_blank" rel="noopener">health</a></footer>
</div>
</div>
<script>{MAIN_JS}</script>
</body></html>"""


# ---------------------------------------------------------------- 组件

def stat(value, label: str, hint: str = "", icon_name: str = "database", tone: str = "") -> str:
    return f"""<div class="stat {tone}">
<div class="stat-top"><div class="stat-icon">{icon(icon_name, 18)}</div></div>
<b>{esc(value)}</b><span class="stat-label">{esc(label)}</span>
<span class="stat-hint">{esc(hint)}</span></div>"""


def recent_row(a: Atom) -> str:
    tags = "".join(f'<span class="mini-tag">#{esc(t)}</span>' for t in a.tags[:2])
    return f"""<a class="recent" href="/atom/{esc(a.id)}">
{kind_dot(a.kind)}
<span class="recent-main"><span class="recent-title">{esc(first_line(a.body, 96))}</span>
<span class="recent-meta">{esc(KIND_LABEL.get(a.kind, a.kind))} · {esc(a.author.get('host', '?'))}/{esc(a.author.get('harness', '?'))} {tags}</span></span>
<time class="recent-time" title="{esc(a.occurred_at)}">{esc(when(a.occurred_at))}</time></a>"""


def mem_table(atoms: list[Atom], status: dict[str, dict], q: str = "") -> str:
    if not atoms:
        return empty("inbox", "没有匹配的记忆", "换个关键词或清除筛选条件再试。",
                     '<a class="btn" href="/memories">清除筛选</a>')
    rows = []
    for a in atoms:
        st = (status.get(a.id) or {}).get("status", "active")
        tags = "".join(f'<span class="mini-tag">#{esc(t)}</span>' for t in a.tags[:3])
        more = f'<span class="mini-tag">+{len(a.tags) - 3}</span>' if len(a.tags) > 3 else ""
        rows.append(f"""<tr>
<td class="cell-main"><div class="mem-main">{kind_dot(a.kind)}<div class="mem-text">
<a class="mem-title" href="/atom/{esc(a.id)}">{highlight(first_line(a.body, 130), search_terms(q))}</a>
<div class="mem-sub">{tags}{more}{('<span class="suspect" title="客户端时钟可疑,时间仅供排序参考">' + icon("clock", 11) + '</span>') if a.time_suspect else ''}</div></div></div></td>
<td data-label="类型">{kind_pill(a.kind)}</td>
<td data-label="作者" class="mono-sm">{esc(a.author.get('host', '?'))}/{esc(a.author.get('harness', '?'))}</td>
<td data-label="时间" class="nowrap"><time title="{esc(a.occurred_at)}">{esc(when(a.occurred_at))}</time></td>
<td data-label="状态">{status_pill(st) if st != "active" else '<span class="muted">—</span>'}</td></tr>""")
    return f"""<div class="table-wrap"><table class="tbl tbl-mem">
<thead><tr><th>记忆</th><th>类型</th><th>作者</th><th>时间</th><th>状态</th></tr></thead>
<tbody>{''.join(rows)}</tbody></table></div>"""


def pager(base: dict, page: int, pages: int) -> str:
    if pages <= 1:
        return ""
    items = []
    if page > 1:
        items.append(f'<a class="page-btn" href="{qs("/memories", **base, page=page - 1)}">上一页</a>')
    start = max(1, page - 2)
    end = min(pages, page + 2)
    if start > 1:
        items.append(f'<a class="page-btn" href="{qs("/memories", **base, page=1)}">1</a>')
        if start > 2:
            items.append('<span class="page-gap">…</span>')
    for n in range(start, end + 1):
        cls = "page-btn current" if n == page else "page-btn"
        items.append(f'<a class="{cls}" href="{qs("/memories", **base, page=n)}">{n}</a>')
    if end < pages:
        if end < pages - 1:
            items.append('<span class="page-gap">…</span>')
        items.append(f'<a class="page-btn" href="{qs("/memories", **base, page=pages)}">{pages}</a>')
    if page < pages:
        items.append(f'<a class="page-btn" href="{qs("/memories", **base, page=page + 1)}">下一页</a>')
    return f'<nav class="pager" aria-label="分页">{"".join(items)}</nav>'


# ---------------------------------------------------------------- 页面

def dashboard(ctx: Ctx, health: dict, recent: list[Atom], review: list[dict],
              conflicts: list[dict], tags: list[tuple[str, int]], week_new: int,
              last_rebuild: str) -> str:
    shown = health.get("atoms", 0)
    review_n = len(review)
    conflict_n = len(conflicts)
    if review_n or conflict_n:
        bits = []
        if review_n:
            bits.append(f'<a href="/review">{review_n} 条记忆已过有效期</a>')
        if conflict_n:
            bits.append(f'<a href="/conflicts">{conflict_n} 处取代链异常</a>')
        alert = (f'<div class="alert warn">{icon("alert", 18)}<div><b>有需要处理的事项</b>'
                 f'<span>{" · ".join(bits)},建议逐条复核。</span></div>'
                 f'<a class="btn sm" href="/review">去处理</a></div>')
    else:
        alert = (f'<div class="alert ok">{icon("shield", 18)}<div><b>记忆库状态良好</b>'
                 f'<span>没有过期待复核项,也没有取代链冲突。最近重建 {esc(when(last_rebuild))}。</span></div></div>')

    stats = (f'<div class="stats">'
             f'{stat(shown, "条记忆", f"本周新增 {week_new} 条", "database")}'
             f'{stat(len(tags), "个主题", "按标签聚合", "tag")}'
             f'{stat(review_n, "条待复核", "超过有效期", "clock", "warn" if review_n else "")}'
             f'{stat(conflict_n, "处冲突", "取代链异常", "alert", "danger" if conflict_n else "")}'
             f'</div>')

    recent_html = "".join(recent_row(a) for a in recent) or empty(
        "inbox", "还没有记忆", "从右上角「记一条」开始,或让任意 harness 写入第一条。")
    recent_card = f"""<section class="card">
<div class="card-head"><h3 class="card-title">最近记录</h3><a class="link-more" href="/memories">全部记忆 {icon("arrow", 14)}</a></div>
<div class="card-body flush"><div class="recent-list">{recent_html}</div></div></section>"""

    def task_rows(items: list[dict], kind: str) -> str:
        out = []
        for it in items[:3]:
            a = it.get("atom")
            preview = first_line(a.body, 64) if a else it.get("id", "")
            href = f'/atom/{esc(it.get("id", ""))}'
            if kind == "review":
                meta = f'#{esc(it.get("topic", ""))} · 已过期 {it.get("days_overdue", 0)} 天'
            else:
                meta = it.get("problem", "")
            out.append(f'<a class="task" href="{href}">{kind_dot(a.kind if a else "fact")}'
                       f'<span><span class="task-title">{esc(preview)}</span>'
                       f'<span class="task-meta">{esc(meta)}</span></span></a>')
        return "".join(out)

    if review_n or conflict_n:
        review_block = ""
        if review_n:
            review_block += ('<div class="task-label">待复核</div>' + task_rows(review, "review"))
        if conflict_n:
            review_block += ('<div class="task-label">冲突</div>' + task_rows(conflicts, "conflict"))
        todo_card = f"""<section class="card"><div class="card-head">
<h3 class="card-title">需要处理</h3><span class="count">{review_n + conflict_n}</span></div>
<div class="card-body flush"><div class="task-list">{review_block}</div></div></section>"""
    else:
        todo_card = f"""<section class="card"><div class="card-head"><h3 class="card-title">需要处理</h3></div>
<div class="card-body">{empty("check", "暂时没有待办", "过期复核与取代链检查都通过了。")}</div></section>"""

    tag_html = "".join(tag_chip(t, n) for t, n in tags[:14]) or '<p class="muted">还没有标签。</p>'
    topics_card = f"""<section class="card"><div class="card-head">
<h3 class="card-title">热门主题</h3><a class="link-more" href="/topics">全部 {icon("arrow", 14)}</a></div>
<div class="card-body"><div class="chips">{tag_html}</div></div></section>"""

    compose = f"""<section class="card" id="compose"><div class="card-head">
<h3 class="card-title">快速记录</h3><a class="link-more" href="/new">完整表单 {icon("arrow", 14)}</a></div>
<div class="card-body"><form method="post" action="/compose">
<div class="field"><textarea name="body" rows="3" required data-submit
 placeholder="一条只讲一件事,写成可独立阅读的结论句。例如:出网代理 http://proxy.example:7890 可用。"></textarea></div>
<div class="quick-row">
<select name="kind" aria-label="类型">{''.join(opt(k, v, k == 'fact') for k, v in KIND_LABEL.items() if k != 'journal')}</select>
<input type="text" name="tags" placeholder="标签,逗号分隔" aria-label="标签">
<button class="btn primary" type="submit">写入</button>
</div><p class="hint">⌘/Ctrl + Enter 提交 · 写入后 atom 不可修改,更正请用「写更正」</p>
</form></div></section>"""

    return f"""{page_head("概览", f"共 {shown} 条记忆 · {len(tags)} 个主题 · 数据在服务器本地", eyebrow=when(last_rebuild) + " 最近重建")}
{alert}{stats}
<div class="grid grid-2">
<div class="stack">{recent_card}</div>
<div class="stack">{todo_card}{topics_card}{compose}</div>
</div>"""


def memories(ctx: Ctx, atoms: list[Atom], total: int, page: int, pages: int,
             filters: dict, options: dict, status: dict[str, dict],
             kind_counts: dict[str, int] | None = None) -> str:
    kind_opts = [("", "全部类型")] + [(k, v) for k, v in KIND_LABEL.items() if k != "journal"]
    tag_opts = [("", "全部主题")] + [(t, f"#{t}") for t in options.get("tags", [])]
    host_opts = [("", "全部作者")] + [(h, h) for h in options.get("hosts", [])]
    status_opts = [("active", "正常"), ("all", "含已过期/已撤回"), ("deprecated", "已过期"), ("retracted", "已撤回")]
    sort_opts = [("new", "按时间:新 → 旧"), ("old", "按时间:旧 → 新"), ("kind", "按类型")]
    actions = ""
    cur = filters
    summary = f'共 <b>{total}</b> 条'
    if cur.get("q"):
        summary += f' · 关键词「{esc(cur["q"])}」'
    if cur.get("tag"):
        summary += f' · #{esc(cur["tag"])}'
    if cur.get("kind"):
        summary += f' · {esc(KIND_LABEL.get(cur["kind"], cur["kind"]))}'
    filters_html = f"""<form class="filterbar" method="get" action="/memories">
<div class="filter-search">{icon("search", 16)}<input type="search" name="q" value="{esc(cur.get('q', ''))}" placeholder="搜索正文、标签、作者…"></div>
{select("kind", kind_opts, cur.get("kind", ""))}
{select("tag", tag_opts, cur.get("tag", ""))}
{select("author", host_opts, cur.get("author", ""))}
{select("status", status_opts, cur.get("status", "active"))}
{select("sort", sort_opts, cur.get("sort", "new"))}
<button class="btn" type="submit">{icon("filter", 15)}筛选</button>
<a class="btn ghost" href="/memories">重置</a>
</form>"""
    chips = []
    all_n = sum((kind_counts or {}).values())
    base = {k: v for k, v in cur.items() if k not in ("kind", "page")}
    for value, label in [("", "全部")] + [(k, v) for k, v in KIND_LABEL.items() if k != "journal"]:
        n = all_n if value == "" else (kind_counts or {}).get(value, 0)
        if value and not n:
            continue
        cls = "kchip active" if cur.get("kind", "") == value else "kchip"
        chips.append(f'<a class="{cls}" href="{qs("/memories", **base, kind=value)}">{esc(label)} <b>{n}</b></a>')
    body = f"""{page_head("记忆库", summary, actions)}
<section class="card">{filters_html}<div class="kindfilter">{"".join(chips)}</div>
{mem_table(atoms, status, cur.get("q", ""))}</section>
{pager({k: v for k, v in cur.items() if k}, page, pages)}"""
    return body


def search(ctx: Ctx, q: str, results: list[Atom], total: int,
           options: dict, kind: str = "", tag: str = "") -> str:
    kind_opts = [("", "全部类型")] + [(k, v) for k, v in KIND_LABEL.items() if k != "journal"]
    tag_opts = [("", "全部主题")] + [(t, f"#{t}") for t in options.get("tags", [])]
    form = f"""<form class="search-hero" method="get" action="/search">
<div class="search-hero-input">{icon("search", 20)}
<input type="search" name="q" value="{esc(q)}" placeholder="搜索正文、标签、作者…" autofocus aria-label="搜索关键词">
<button class="btn primary" type="submit">检索</button></div>
<div class="search-hero-filters">
{select("kind", kind_opts, kind)}
{select("tag", tag_opts, tag)}
<span class="hint">多个关键词用空格分隔,全部命中才显示</span>
</div></form>"""
    if not q:
        examples = "".join(f'<a class="chip" href="/search?q={quote(t)}">{esc(t)}</a>'
                           for t in ("network", "deploy", "backup", "proxy", "检索"))
        body = f"""{page_head("检索", "按正文、标签、作者查找记忆")}
{form}
<section class="card"><div class="card-body">
{empty("search", "输入关键词开始检索", "例如 network、deploy、backup;也可以直接点下面的示例。",
       f'<div class="chips" style="justify-content:center">{examples}</div>')}
</div></section>"""
        return body
    if not results:
        body = f"""{page_head("检索", f"关键词「{esc(q)}」 · 0 条结果")}
{form}
<section class="card"><div class="card-body">
{empty("search", "没有命中", "试试更短的关键词,或到记忆库里按类型和主题浏览。",
       '<a class="btn" href="/memories">浏览记忆库</a>')}
</div></section>"""
        return body
    items = []
    for a in results:
        tags = "".join(f'<span class="mini-tag">#{esc(t)}</span>' for t in a.tags[:3])
        items.append(f"""<a class="result" href="/atom/{esc(a.id)}">
<div class="result-top">{kind_pill(a.kind)}<time>{esc(when(a.occurred_at))}</time></div>
<p class="result-body">{snippet(a.body, q)}</p>
<div class="result-meta"><span>{esc(a.author.get('host', '?'))}/{esc(a.author.get('harness', '?'))}</span>{tags}</div></a>""")
    return f"""{page_head("检索", f"关键词「{esc(q)}」 · {total} 条结果")}
{form}<div class="results">{''.join(items)}</div>"""


def atom_page(ctx: Ctx, a: Atom, status: str, supersedes: Atom | None,
              children: list[Atom], tombstones: list[dict], status_map: dict[str, dict]) -> str:
    tags = "".join(tag_chip(t) for t in a.tags) or '<span class="muted">无标签</span>'
    suspect = ('<div class="flash warn">' + icon("alert", 16) +
               '<span>写入时检测到客户端与服务端时钟差超过 5 分钟,时间可能不可靠。</span></div>') if a.time_suspect else ""
    status_html = status_pill(status)

    action_btns = []
    if ctx.can("write"):
        action_btns.append(f'<a class="btn" href="/new?supersedes={esc(a.id)}">{icon("edit", 16)}写更正</a>')
    action_btns.append(f'<button class="btn" type="button" data-copy-target="#atom-body">{icon("copy", 16)}复制正文</button>')
    if ctx.can("write"):
        if status in ("deprecated", "retracted"):
            action_btns.append(f"""<form method="post" action="/forget" data-confirm="确定把这条记录恢复为正常状态?">
<input type="hidden" name="id" value="{esc(a.id)}"><input type="hidden" name="status" value="active">
<input type="hidden" name="next" value="/atom/{esc(a.id)}">
<button class="btn" type="submit">{icon("refresh", 16)}恢复为正常</button></form>""")
        else:
            action_btns.append(f"""<form method="post" action="/forget" data-confirm="标记过期后,这条记忆会从默认列表和 brief 中隐藏。确定继续?">
<input type="hidden" name="id" value="{esc(a.id)}"><input type="hidden" name="status" value="deprecated">
<input type="hidden" name="reason" value="web 标记过期"><input type="hidden" name="next" value="/atom/{esc(a.id)}">
<button class="btn danger" type="submit">{icon("clock", 16)}标记为过期</button></form>""")
        if a.ttl:
            action_btns.append(f"""<form method="post" action="/renew" data-confirm="将生成一条续期 180 天的新记忆,并取代当前这条。确定继续?">
<input type="hidden" name="id" value="{esc(a.id)}"><input type="hidden" name="days" value="180">
<input type="hidden" name="next" value="/atom/{esc(a.id)}">
<button class="btn" type="submit">{icon("refresh", 16)}续期 180 天</button></form>""")
        action_btns.append(f"""<details class="danger-zone"><summary>撤回这条记忆…</summary>
<form method="post" action="/forget" data-confirm="撤回是不可逆的状态变更,必须指定替代记录。确定继续?">
<input type="hidden" name="id" value="{esc(a.id)}"><input type="hidden" name="status" value="retracted">
<input type="hidden" name="next" value="/atom/{esc(a.id)}">
<label>替代记录 id(必须已存在)<input type="text" name="replaced_by" required pattern="[mj]_[0-9a-hjkmnp-tv-z]{{10}}" placeholder="m_xxxxxxxxxx"></label>
<label>原因<input type="text" name="reason" placeholder="例如:结论有误,已重新实测"></label>
<button class="btn danger" type="submit">确认撤回</button></form></details>""")

    meta_rows = [
        ("记录 ID", f'<span class="mono">{esc(a.id)}</span>'),
        ("类型", kind_pill(a.kind)),
        ("发生时间", f'<span title="{esc(a.occurred_at)}">{esc(a.occurred_at or "—")}</span>'),
        ("入库时间", esc(a.received_at or "—")),
        ("作者", f'{esc(a.author.get("host", "?"))}/{esc(a.author.get("harness", "?"))}/{esc(a.author.get("role", "?"))}'),
        ("置信度", esc(CONF_LABEL.get(a.confidence, a.confidence))),
        ("来源", esc(a.source or "—")),
        ("有效期", esc(a.ttl or "—")),
        ("内容哈希", f'<span class="mono-sm">{esc(a.hash or "—")}</span>'),
    ]
    if a.section:
        meta_rows.insert(2, ("小节", esc(a.section)))
    meta = "".join(f'<div class="meta-row"><dt>{esc(k)}</dt><dd>{v}</dd></div>' for k, v in meta_rows)

    chain = ""
    if supersedes:
        chain += (f'<div class="chain-row"><span class="chain-label">取代</span>'
                  f'<a href="/atom/{esc(supersedes.id)}">{esc(first_line(supersedes.body, 42))}</a></div>')
    for c in children:
        cst = (status_map.get(c.id) or {}).get("status", "active")
        chain += (f'<div class="chain-row"><span class="chain-label">被取代</span>'
                  f'<a href="/atom/{esc(c.id)}">{esc(first_line(c.body, 40))}</a>'
                  f'{status_pill(cst)}</div>')
    if not chain:
        chain = '<p class="muted">这条记忆没有取代链。'
        if ctx.can("write"):
            chain += "更正时用「写更正」会自动建立关系。"
        chain += "</p>"

    if tombstones:
        hist = "".join(f"""<div class="history-row"><div class="history-top">
{status_pill(t.get("status", ""))}<span class="history-time">{esc(when(t.get("at", "")))}</span>
<span class="history-seq">#{t.get("seq", "")}</span></div>
<p>{esc(t.get("reason") or "未填写原因")}</p>
<span class="history-author">{esc(t.get("author", ""))}</span></div>""" for t in reversed(tombstones))
    else:
        hist = '<p class="muted">还没有状态变更记录。</p>'

    chain_note = ""
    if a.supersedes and supersedes is None:
        chain_note = ('<div class="flash err">' + icon("alert", 16) +
                      f'<span>取代目标 {esc(a.supersedes)} 不存在,需要写一条更正记录。</span></div>')

    return f"""
<nav class="breadcrumb"><a href="/memories">记忆库</a><span>/</span><span class="mono">{esc(a.id)}</span></nav>
<div class="detail-grid">
<article class="card detail-main">
  <div class="detail-top">{kind_pill(a.kind)}{status_html}<span class="detail-time">{esc(when(a.occurred_at))}</span></div>
  <div class="detail-tags">{tags}</div>
  {suspect}
  <div class="prose" id="atom-body">{prose(a.body)}</div>
  {chain_note}
</article>
<aside class="stack">
  <section class="card"><div class="card-head"><h3 class="card-title">操作</h3></div>
  <div class="card-body action-stack">{''.join(action_btns)}</div></section>
  <section class="card"><div class="card-head"><h3 class="card-title">元数据</h3></div>
  <div class="card-body"><dl class="meta-list">{meta}</dl></div></section>
  <section class="card"><div class="card-head"><h3 class="card-title">取代链</h3></div>
  <div class="card-body"><div class="chain">{chain}</div></div></section>
  <section class="card"><div class="card-head"><h3 class="card-title">状态历史</h3></div>
  <div class="card-body"><div class="history">{hist}</div></div></section>
</aside>
</div>"""


def new_page(ctx: Ctx, supersedes: str = "", kind: str = "fact") -> str:
    kind_opts = "".join(opt(k, v, k == kind) for k, v in KIND_LABEL.items() if k != "journal")
    conf_opts = "".join(opt(k, v, k == "medium") for k, v in CONF_LABEL.items())
    prefill = ""
    if supersedes:
        prefill = (f'<div class="flash warn">{icon("link", 16)}<span>这条新记忆会取代 '
                   f'<span class="mono">{esc(supersedes)}</span>,旧记录保留但默认不再显示。</span></div>')
    return f"""{page_head("记一条", "一个结论一条记录 · 写清楚、可独立阅读、不写密码明文")}
<div class="new-grid">
<form class="card form-card" method="post" action="/compose">
<div class="card-body form-body">
{prefill}
<div class="field"><label for="body">正文 <span class="req">*</span></label>
<textarea id="body" name="body" rows="9" required data-submit autofocus
 placeholder="例:出网代理 http://proxy.example:7890 可用,HTTP 与 SOCKS5 都通。"></textarea>
<div class="hint"><span id="char-count">0</span> 字 · 一条只讲一件事,不写「见上文」</div></div>
<div class="form-grid">
<div class="field"><label for="kind">类型</label><select id="kind" name="kind">{kind_opts}</select></div>
<div class="field"><label for="tags">标签</label><input id="tags" type="text" name="tags" placeholder="network, proxy" value=""></div>
<div class="field"><label for="confidence">置信度</label><select id="confidence" name="confidence">{conf_opts}</select></div>
<div class="field"><label for="source">来源</label><input id="source" type="text" name="source" value="web" placeholder="例如 curl 实测"></div>
<div class="field"><label for="ttl">有效期(可选)</label><input id="ttl" type="date" name="ttl"></div>
<div class="field"><label for="supersedes">取代记录(可选)</label><input id="supersedes" type="text" name="supersedes" value="{esc(supersedes)}" pattern="[mj]_[0-9a-hjkmnp-tv-z]{{10}}" placeholder="m_xxxxxxxxxx"></div>
</div>
<div class="form-actions"><button class="btn primary" type="submit">{icon("check", 16)}写入记忆库</button>
<a class="btn ghost" href="/memories">取消</a></div>
</div></form>
<aside class="stack">
<section class="card"><div class="card-head"><h3 class="card-title">写入约定</h3></div>
<div class="card-body prose prose-small">
<ul><li>一个结论一条记录,能独立阅读,不写「见上文」。</li>
<li>密码 / token / 密钥<b>不进库</b>,只记「存在哪、怎么取」。</li>
<li>atom 不可修改;更正用「写更正」建立取代链。</li>
<li>过期事实写有效期,系统会在待复核里提醒。</li></ul></div></section>
<section class="card"><div class="card-head"><h3 class="card-title">快捷方式</h3></div>
<div class="card-body muted">在任意页面按 <kbd>/</kbd> 聚焦搜索;正文里按 <kbd>⌘</kbd>/<kbd>Ctrl</kbd> + <kbd>Enter</kbd> 提交。</div></section>
</aside></div>"""


def rules_page(ctx: Ctx, groups: list[tuple[str, list[Atom]]]) -> str:
    if not groups:
        return page_head("规范与偏好", "所有 harness 共用的行为规范") + empty(
            "book", "还没有规范", "用「记一条」写入 kind=rule 或 pref 的记录。")
    total = sum(len(items) for _, items in groups)
    add = '<a class="btn" href="/new?kind=rule">新增规范</a>' if ctx.can("write") else ""
    parts = [page_head("规范与偏好", f"共 {total} 条 · 派生视图,修改请追加新记录", add)]
    for section, items in groups:
        rows = []
        for a in items:
            rows.append(f"""<div class="rule-item">
<div class="rule-body">{prose(a.body)}</div>
<div class="rule-meta"><a href="/atom/{esc(a.id)}" class="mono-sm">{esc(a.id)}</a>
<span>{esc(a.author.get('host', '?'))}/{esc(a.author.get('harness', '?'))}</span>
<span>{esc(when(a.occurred_at))}</span>{kind_pill(a.kind)}</div></div>""")
        parts.append(f"""<section class="card rules-card"><div class="card-head">
<h3 class="card-title">{esc(section or "通用")}</h3><span class="count">{len(items)}</span></div>
<div class="card-body flush"><div class="rules-list">{''.join(rows)}</div></div></section>""")
    return "".join(parts)


def topics_page(ctx: Ctx, topics: list[dict]) -> str:
    if not topics:
        return page_head("主题", "按标签聚合的记忆") + empty(
            "tag", "还没有主题", "记忆写入时带上标签,这里会自动聚合。")
    cards = []
    for t in topics:
        kinds = "".join(f'<span class="kind-dot {KIND_CLASS.get(k, "k-fact")}" title="{esc(KIND_LABEL.get(k, k))}: {n}"></span>'
                        for k, n in t["kinds"])
        cards.append(f"""<a class="topic-card" href="/topic/{quote(t['tag'], safe='')}">
<div class="topic-top"><span class="topic-hash">#</span><b>{esc(t['tag'])}</b><span class="topic-count">{t['count']}</span></div>
<p class="topic-preview">{esc(first_line(t['latest'], 90) if t.get('latest') else '')}</p>
<div class="topic-kinds">{kinds}</div></a>""")
    return page_head("主题", f"{len(topics)} 个标签 · 点击进入主题视图") + f'<div class="topic-grid">{"".join(cards)}</div>'


def topic_page(ctx: Ctx, tag: str, atoms: list[Atom], status: dict[str, dict]) -> str:
    actions = f'<a class="btn ghost" href="/topics">全部主题</a>'
    return (page_head(f"#{tag}", f"{len(atoms)} 条记忆", actions)
            + f'<section class="card">{mem_table(atoms, status)}</section>')


def review_page(ctx: Ctx, items: list[dict]) -> str:
    if not items:
        return page_head("待复核", "超过有效期的事实") + empty(
            "check", "没有待复核项", "所有带有效期的事实都在有效期内,不需要处理。")
    rows = []
    for it in items:
        a = it["atom"]
        actions = f'<a class="btn sm" href="/atom/{esc(a.id)}">打开</a>'
        if ctx.can("write"):
            actions += f"""<form method="post" action="/renew" data-confirm="续期 180 天会生成一条取代旧记录的新记忆。继续?">
<input type="hidden" name="id" value="{esc(a.id)}"><input type="hidden" name="days" value="180">
<input type="hidden" name="next" value="/review"><button class="btn sm" type="submit">续期 180 天</button></form>
<form method="post" action="/forget" data-confirm="标记过期后不再出现在默认列表。继续?">
<input type="hidden" name="id" value="{esc(a.id)}"><input type="hidden" name="status" value="deprecated">
<input type="hidden" name="reason" value="复核后标记过期"><input type="hidden" name="next" value="/review">
<button class="btn sm danger" type="submit">标记过期</button></form>"""
        rows.append(f"""<tr><td class="cell-main"><div class="mem-main">{kind_dot(a.kind)}<div class="mem-text">
<a class="mem-title" href="/atom/{esc(a.id)}">{esc(first_line(a.body, 110))}</a>
<div class="mem-sub"><span class="mini-tag">#{esc(it.get('topic', ''))}</span></div></div></div></td>
<td data-label="有效期" class="nowrap">{esc(it.get('ttl', ''))}</td>
<td data-label="已过期" class="nowrap danger-text">{it.get('days_overdue', 0)} 天</td>
<td data-label="操作"><div class="row-actions">{actions}</div></td></tr>""")
    return page_head("待复核", f"{len(items)} 条记忆超过有效期,建议复核后更新或遗忘") + f"""<section class="card">
<div class="table-wrap"><table class="tbl tbl-mem"><thead><tr><th>记忆</th><th>有效期</th><th>已过期</th><th>操作</th></tr></thead>
<tbody>{''.join(rows)}</tbody></table></div></section>"""


def conflicts_page(ctx: Ctx, items: list[dict]) -> str:
    if not items:
        return page_head("冲突", "取代链检查") + empty(
            "check", "没有冲突", "没有断链、环或多尾冲突。写更正时会自动维持取代链。")
    rows = []
    for it in items:
        a = it.get("atom")
        rows.append(f"""<tr><td class="cell-main"><div class="mem-main">{kind_dot(a.kind if a else 'fact')}<div class="mem-text">
<a class="mem-title" href="/atom/{esc(it['id'])}">{esc(first_line(a.body, 100) if a else it['id'])}</a>
<div class="mem-sub mono-sm">{esc(it['id'])}</div></div></div></td>
<td data-label="问题"><span class="warn-text">{esc(it['problem'])}</span></td>
<td data-label="操作"><div class="row-actions"><a class="btn sm" href="/atom/{esc(it['id'])}">打开</a>
{(f'<a class="btn sm" href="/new?supersedes={esc(it["id"])}">写更正</a>') if ctx.can("write") else ""}</div></td></tr>""")
    return page_head("冲突", f"{len(items)} 处取代链异常需要人工裁决") + f"""<section class="card">
<div class="table-wrap"><table class="tbl tbl-mem"><thead><tr><th>记忆</th><th>问题</th><th>操作</th></tr></thead>
<tbody>{''.join(rows)}</tbody></table></div></section>"""


def tokens_page(ctx: Ctx, tokens: list[dict]) -> str:
    rows = []
    for t in tokens:
        author = t.get("author") or {}
        badges = "".join(f'<span class="chip mini">{esc(s)}</span>' for s in t.get("scopes", []))
        cur = (t.get("id") == ctx.rec.get("id"))
        status = "已吊销" if t.get("revoked") else "有效"
        cls = "st-danger" if t.get("revoked") else "st-ok"
        if not t.get("revoked") and t.get("expires"):
            try:
                exp = datetime.fromisoformat(t["expires"])
                if exp < datetime.now().astimezone():
                    status, cls = "已过期", "st-warn"
            except ValueError:
                pass
        revoke = ""
        if not t.get("revoked") and ctx.can("admin"):
            revoke = f"""<form method="post" action="/token-revoke" data-confirm="吊销后该端立即无法访问,确定继续?">
<input type="hidden" name="id" value="{esc(t.get('id', ''))}"><button class="btn sm danger" type="submit">吊销</button></form>"""
        rows.append(f"""<tr><td class="mono-sm">{esc(t.get('prefix', ''))}…</td>
<td>{esc(author.get('host', '?'))}/{esc(author.get('harness', '?'))}<div class="muted small">{esc(author.get('role', ''))}</div></td>
<td><div class="chips">{badges}</div></td>
<td data-label="创建" class="mono-sm nowrap">{esc(when(t.get('created', '')))}</td>
<td data-label="到期" class="mono-sm nowrap">{esc(when(t.get('expires') or '', full=False) if t.get('expires') else '永不过期')}</td>
<td data-label="状态"><span class="status {cls}">{status}</span>{' <span class="chip mini">当前</span>' if cur else ''}</td>
<td data-label="操作">{revoke}</td></tr>""")
    scope_boxes = "".join(
        f'<label class="check"><input type="checkbox" name="scopes" value="{s}"'
        f'{" checked" if s in ("read", "write") else ""}><span><b>{s}</b> '
        f'{ {"read": "读取", "write": "写入", "secret": "密钥", "admin": "管理"}[s] }</span></label>'
        for s in ("read", "write", "secret", "admin"))
    return f"""{page_head("访问令牌", "每个主机 / harness 一把令牌 · 明文只在签发时显示一次")}
<div class="alert warn">{icon("shield", 18)}<div><b>令牌即身份</b><span>浏览器里存的是 HttpOnly + SameSite=Strict cookie;吊销后该端立即失效。不要在聊天里粘贴令牌明文。</span></div></div>
<section class="card"><div class="card-head"><h3 class="card-title">签发新令牌</h3></div>
<div class="card-body"><form method="post" action="/token-issue" class="form-body">
<div class="form-grid">
<div class="field"><label for="thost">主机</label><input id="thost" name="host" required pattern="[A-Za-z0-9._-]{{1,24}}" placeholder="mac / nas"></div>
<div class="field"><label for="tharness">harness</label><input id="tharness" name="harness" required pattern="[A-Za-z0-9._-]{{1,24}}" placeholder="pi / dsh"></div>
<div class="field"><label for="trole">角色</label><input id="trole" name="role" required pattern="[A-Za-z0-9._-]{{1,24}}" value="engineer"></div>
<div class="field"><label for="ttl">有效期(天,0 = 永不过期)</label><input id="ttl" name="ttl" type="number" min="0" max="3650" value="365"></div>
</div>
<div class="field"><label>权限</label><div class="checks">{scope_boxes}</div></div>
<div class="form-actions"><button class="btn primary" type="submit">{icon("key", 16)}签发令牌</button></div>
</form></div></section>
<section class="card"><div class="card-head"><h3 class="card-title">已有令牌</h3><span class="count">{len(tokens)}</span></div>
<div class="table-wrap"><table class="tbl"><thead><tr><th>前缀</th><th>身份</th><th>权限</th><th>创建</th><th>到期</th><th>状态</th><th>操作</th></tr></thead>
<tbody>{''.join(rows)}</tbody></table></div></section>"""


def token_issued(ctx: Ctx, token: str, rec: dict) -> str:
    author = rec.get("author") or {}
    return f"""{page_head("令牌已签发", "只显示这一次,离开本页后无法再次查看")}
<div class="alert err">{icon("alert", 18)}<div><b>现在保存它</b><span>关闭本页后将无法恢复明文;丢了只能吊销重发。</span></div></div>
<section class="card"><div class="card-body">
<p class="muted">身份 <b>{esc(author.get('host', '?'))}/{esc(author.get('harness', '?'))}/{esc(author.get('role', '?'))}</b> ·
权限 {esc(', '.join(rec.get('scopes', [])))} · 到期 {esc(rec.get('expires') or '永不过期')}</p>
<div class="token-box"><code id="issued-token">{esc(token)}</code>
<button class="btn" type="button" data-copy-target="#issued-token">{icon("copy", 16)}复制</button></div>
<p class="hint">写到端侧 <code>~/.config/brain/config.toml</code>(0600),不要贴进聊天或提交到 git。</p>
<div class="form-actions"><a class="btn primary" href="/tokens">我已保存,返回令牌列表</a></div>
</div></section>"""


def activity_page(ctx: Ctx, entries: list[dict]) -> str:
    if not entries:
        return page_head("活动审计", "本机 UI 与 API 的管理动作") + empty(
            "activity", "还没有审计记录", "写入、遗忘、续期、令牌操作会记录在这里。")
    rows = []
    for e in entries:
        action = ACTION_LABEL.get(e.get("action", ""), e.get("action", ""))
        rows.append(f"""<tr><td class="mono-sm nowrap" title="{esc(e.get('at', ''))}">{esc(when(e.get('at', '')))}</td>
<td><span class="action-pill">{esc(action)}</span></td>
<td class="mono-sm">{esc(e.get('actor', ''))}</td>
<td class="mono-sm">{esc(e.get('target', ''))}</td>
<td class="muted">{esc(e.get('detail', ''))}</td></tr>""")
    return page_head("活动审计", f"最近 {len(entries)} 条管理动作 · 只追加,不修改") + f"""<section class="card">
<div class="table-wrap"><table class="tbl"><thead><tr><th>时间</th><th>动作</th><th>操作者</th><th>对象</th><th>详情</th></tr></thead>
<tbody>{''.join(rows)}</tbody></table></div></section>"""


def system_page(ctx: Ctx, health: dict, problems: list[str], disk: dict,
                last_rebuild: str, audit: list[dict]) -> str:
    ok = not problems
    verify_html = (empty("check", "五项自检全部通过", "atom 可解析、hash 一致、取代链完整、tombstone 单调、视图版本同步。")
                   if ok else f'<ul class="problem-list">{"".join(f"<li>{esc(p)}</li>" for p in problems)}</ul>')
    recent = "".join(f'<div class="recent"><span class="recent-main"><span class="recent-title">'
                     f'{esc(ACTION_LABEL.get(e.get("action", ""), e.get("action", "")))} · {esc(e.get("target", ""))}</span>'
                     f'<span class="recent-meta">{esc(e.get("actor", ""))} · {esc(e.get("detail", ""))}</span></span>'
                     f'<time class="recent-time">{esc(when(e.get("at", "")))}</time></div>' for e in audit[:8])
    reindex = (f'<form method="post" action="/reindex" data-confirm="重建视图会重写 views/ 下的派生文件,不影响 atoms。继续?">'
               f'<button class="btn" type="submit">{icon("refresh", 16)}重建视图</button></form>') if ctx.can("admin") else ""
    return f"""{page_head("系统状态", "服务、数据与自检", reindex)}
<div class="stats">
{stat(health.get('server', '—'), "服务版本", f"API v{health.get('api', '?')}", "server")}
{stat(health.get('atoms', 0), "条记忆", f"{health.get('tombstones', 0)} 条状态变更", "database")}
{stat(len(problems), "项自检问题", "0 表示全部通过" if ok else "需要处理", "shield", "" if ok else "danger")}
{stat(f"{disk.get('free_gb', 0)} GB", "磁盘可用", f"总 {disk.get('total_gb', 0)} GB", "server")}
</div>
<div class="grid grid-2">
<section class="card"><div class="card-head"><h3 class="card-title">自检</h3>
<span class="status {'st-ok' if ok else 'st-danger'}">{'通过' if ok else '异常'}</span></div>
<div class="card-body">{verify_html}</div></section>
<div class="stack">
<section class="card"><div class="card-head"><h3 class="card-title">运行信息</h3></div>
<div class="card-body"><dl class="meta-list">
<div class="meta-row"><dt>数据目录</dt><dd class="mono-sm">{esc(str(BRAIN_ROOT))}</dd></div>
<div class="meta-row"><dt>视图版本</dt><dd class="mono-sm">{esc(health.get('views_rev', ''))}</dd></div>
<div class="meta-row"><dt>最近重建</dt><dd>{esc(when(last_rebuild, full=True))}</dd></div>
<div class="meta-row"><dt>功能</dt><dd>{esc(', '.join(health.get('features', [])))}</dd></div>
</dl></div></section>
<section class="card"><div class="card-head"><h3 class="card-title">最近活动</h3><a class="link-more" href="/activity">全部</a></div>
<div class="card-body flush"><div class="recent-list">{recent or '<p class="muted" style="padding:16px 18px">暂无记录</p>'}</div></div></section>
</div></div>"""


def error_page(ctx: Ctx, code: int, title: str, message: str) -> str:
    return f"""<div class="empty error-page">{icon("alert", 34)}
<p class="error-code">{code}</p><p class="empty-title">{esc(title)}</p>
<p class="empty-text">{esc(message)}</p>
<a class="btn" href="/">回到概览</a></div>"""


def login(error: str = "", next_path: str = "") -> str:
    err = (f'<div class="flash err" role="alert">{icon("alert", 17)}<span>{esc(error)}</span></div>'
           if error else "")
    nxt = f'<input type="hidden" name="next" value="{esc(next_path)}">' if next_path else ""
    return f"""<!doctype html><html lang="zh-CN"><head>
<meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<meta name="color-scheme" content="light dark">
<title>登录 · 共享记忆库</title>
<script>{EARLY_JS}</script><style>{CSS}</style></head>
<body class="login-body">
<button class="icon-btn login-theme" data-theme-toggle aria-label="切换深色/浅色">{icon("sun", 18, "ico icon-sun")}{icon("moon", 18, "ico icon-moon")}</button>
<div class="login-wrap">
  <section class="login-brand">
    <div class="brand login-logo"><span class="brand-mark">{icon("network", 22)}</span>
      <span class="brand-text"><span class="brand-name">共享记忆库</span><span class="brand-sub">BRAIN · 服务器</span></span></div>
    <h1>一套记忆,多台主机,<br>多个 harness 共用。</h1>
    <p>不可变的 atom、可重建的视图、可追溯的取代链。浏览器只是一个管理入口,真相始终在服务器上的纯文本里。</p>
    <ul class="login-points">
      <li>{icon("database", 17)}<span>事实、规范与踩坑,集中在一处</span></li>
      <li>{icon("shield", 17)}<span>令牌分权,吊销即失效,审计只追加</span></li>
      <li>{icon("network", 17)}<span>任何 harness 都能通过 CLI 写入与读取</span></li>
    </ul>
  </section>
  <section class="login-panel">
    <h2>用令牌登录</h2>
    <p class="muted">令牌由服务器侧签发(<code>brain token issue …</code>),登录后写入 HttpOnly cookie,有效期 7 天。</p>
    {err}
    <form method="post" action="/login" class="login-form">
      {nxt}
      <div class="field"><label for="token">访问令牌</label>
      <input id="token" type="password" name="token" placeholder="bk_…" autocomplete="off" spellcheck="false" autofocus required></div>
      <button class="btn primary" type="submit">登录 {icon("arrow", 16)}</button>
    </form>
    <p class="hint">没有令牌?在服务器上执行 <code>brain token issue --host laptop --harness pi --role engineer</code>。</p>
  </section>
</div>
<script>{MAIN_JS}</script>
</body></html>"""


# ---------------------------------------------------------------- 样式
CSS = """
:root{
--font-sans:-apple-system,BlinkMacSystemFont,"Segoe UI","PingFang SC","Hiragino Sans GB","Noto Sans CJK SC","Microsoft YaHei",sans-serif;
--font-mono:ui-monospace,SFMono-Regular,"SF Mono",Menlo,Consolas,"Liberation Mono",monospace;
--bg:#f5f5f1;--surface:#ffffff;--surface-2:#f0f0ea;--surface-3:#e9e9e2;
--text:#171a1f;--text-2:#4a515b;--muted:#767d87;
--border:#e3e2da;--border-strong:#cfcec4;
--accent:#0f6b5a;--accent-strong:#0a5749;--accent-soft:#e5f2ee;--accent-contrast:#ffffff;
--ok:#167a5a;--ok-soft:#e6f4ee;--warn:#9a6206;--warn-soft:#fbf3e2;--warn-line:#edd9ad;
--danger:#b42318;--danger-soft:#fdecea;--danger-line:#f2c7c2;--info:#1d4ed8;--info-soft:#e9effc;
--k-fact:#2563eb;--k-rule:#b42318;--k-pref:#7c3aed;--k-lesson:#b45309;
--k-host:#0e7490;--k-project:#047857;--k-idea:#9333ea;--k-secret:#9f1239;--k-journal:#64748b;
--shadow-sm:0 1px 2px rgba(20,24,31,.045);--shadow-md:0 14px 34px -18px rgba(20,24,31,.28);
--radius:12px;--radius-sm:8px;--sidebar:248px;
}
[data-theme="dark"]{
--bg:#0d0f12;--surface:#14171c;--surface-2:#1a1e24;--surface-3:#222831;
--text:#e9ebee;--text-2:#c3c8cf;--muted:#8b929d;
--border:#262b33;--border-strong:#39404a;
--accent:#63cbae;--accent-strong:#8adcc3;--accent-soft:#12332c;--accent-contrast:#06211a;
--ok:#6fd3a9;--ok-soft:#10281f;--warn:#e6b567;--warn-soft:#2d2516;--warn-line:#5d4a24;
--danger:#f08a80;--danger-soft:#321a18;--danger-line:#6a302b;--info:#8fb4f7;--info-soft:#17233a;
--k-fact:#7aa7ff;--k-rule:#f2938b;--k-pref:#c4a5ff;--k-lesson:#e3b56a;
--k-host:#6cc7dd;--k-project:#6fd3a9;--k-idea:#d1a3ff;--k-secret:#f28ca8;--k-journal:#94a3b8;
--shadow-sm:0 1px 2px rgba(0,0,0,.35);--shadow-md:0 16px 40px -20px rgba(0,0,0,.7);
}
*{box-sizing:border-box}
html{-webkit-text-size-adjust:100%;scroll-behavior:smooth}
body{margin:0;background:var(--bg);color:var(--text);font:14px/1.6 var(--font-sans);-webkit-font-smoothing:antialiased;text-rendering:optimizeLegibility}
a{color:var(--accent);text-decoration:none}
a:hover{text-decoration:underline}
button,input,select,textarea{font:inherit;color:inherit}
code,.mono{font-family:var(--font-mono);font-size:.92em}
.mono-sm{font-family:var(--font-mono);font-size:12px;color:var(--muted)}
.muted{color:var(--muted)}
.small{font-size:12px}
.nowrap{white-space:nowrap}
.danger-text{color:var(--danger);font-weight:600}
.warn-text{color:var(--warn);font-weight:600}
.skip{position:absolute;left:12px;top:-48px;z-index:100;background:var(--surface);padding:8px 14px;border-radius:8px;border:1px solid var(--border)}
.skip:focus{top:12px}
:focus-visible{outline:2px solid var(--accent);outline-offset:2px;border-radius:4px}
::selection{background:var(--accent-soft);color:var(--text)}
.app{display:grid;grid-template-columns:var(--sidebar) minmax(0,1fr);min-height:100vh}
.main{min-width:0;display:flex;flex-direction:column}
.ico{flex:none;display:block}
[data-theme-toggle] .icon-sun{display:block}
[data-theme-toggle] .icon-moon{display:none}
[data-theme="dark"] [data-theme-toggle] .icon-sun{display:none}
[data-theme="dark"] [data-theme-toggle] .icon-moon{display:block}
"""

CSS += """
/* ---------- 侧边栏 ---------- */
.sidebar{position:sticky;top:0;height:100vh;display:flex;flex-direction:column;background:var(--surface);border-right:1px solid var(--border);z-index:30}
.brand{display:flex;align-items:center;gap:11px;padding:20px 18px 14px;color:var(--text)}
.brand:hover{text-decoration:none}
.brand-mark{width:36px;height:36px;border-radius:10px;background:var(--accent);color:var(--accent-contrast);display:grid;place-items:center;flex:none;box-shadow:inset 0 -6px 12px rgba(0,0,0,.12)}
.brand-text{display:flex;flex-direction:column;line-height:1.25;min-width:0}
.brand-name{font-weight:700;font-size:15px;letter-spacing:.2px}
.brand-sub{font-size:9.5px;letter-spacing:.16em;color:var(--muted);font-weight:600;margin-top:2px}
.nav{padding:6px 12px 12px;overflow-y:auto;flex:1}
.nav-group{padding:0 10px;margin:18px 0 6px;font-size:10.5px;font-weight:700;letter-spacing:.14em;color:var(--muted)}
.nav-item{display:flex;align-items:center;gap:10px;padding:8px 10px;margin:1px 0;border-radius:8px;color:var(--text-2);font-size:13.5px;font-weight:500}
.nav-item:hover{background:var(--surface-2);color:var(--text);text-decoration:none}
.nav-item.active{background:var(--accent-soft);color:var(--accent-strong);font-weight:650}
.nav-item .ico{opacity:.72}
.nav-item.active .ico{opacity:1}
.nav-badge{margin-left:auto;min-width:19px;height:19px;padding:0 5px;border-radius:999px;display:inline-grid;place-items:center;font-size:11px;font-weight:700;line-height:1}
.nav-badge.warn{background:var(--warn-soft);color:var(--warn)}
.nav-badge.danger{background:var(--danger-soft);color:var(--danger)}
.side-foot{padding:12px;border-top:1px solid var(--border)}
.logout-form{margin:0;display:inline-flex}
.user{display:flex;align-items:center;gap:10px;padding:6px}
.avatar{width:32px;height:32px;border-radius:9px;background:var(--surface-2);border:1px solid var(--border);display:grid;place-items:center;font-weight:700;font-size:13px;color:var(--text-2);flex:none}
.user-meta{display:flex;flex-direction:column;line-height:1.3;min-width:0;flex:1}
.user-meta b{font-size:12.5px;font-weight:650;white-space:nowrap;overflow:hidden;text-overflow:ellipsis}
.user-meta span{font-size:10.5px;color:var(--muted);white-space:nowrap;overflow:hidden;text-overflow:ellipsis}
.scrim{display:none}
/* ---------- 顶栏 ---------- */
.topbar{position:sticky;top:0;z-index:20;background:var(--bg);border-bottom:1px solid var(--border)}
@supports (backdrop-filter:blur(1px)){.topbar{background:color-mix(in srgb,var(--bg) 82%,transparent);backdrop-filter:blur(14px) saturate(1.1)}}
.topbar-inner{display:flex;align-items:center;gap:14px;height:62px;padding:0 28px}
.crumb{display:flex;align-items:center;gap:8px;font-size:12.5px;color:var(--muted);white-space:nowrap;min-width:0}
a.crumb:hover{text-decoration:none;color:var(--accent)}
.crumb>span:last-child{color:var(--text-2);font-weight:600;overflow:hidden;text-overflow:ellipsis}
.crumb-root{color:var(--muted)}
.crumb-sep{opacity:.5}
.nav-toggle{display:none}
.searchbox{position:relative;display:flex;align-items:center;margin-left:auto;max-width:380px;flex:1}
.searchbox .ico{position:absolute;left:11px;color:var(--muted);pointer-events:none}
.searchbox input[type="search"]{width:100%;height:36px;padding:0 40px 0 40px;border:1px solid var(--border);border-radius:9px;background:var(--surface);color:var(--text);font-size:13px;transition:border-color .15s,box-shadow .15s}
.searchbox input[type="search"]:focus{outline:none;border-color:var(--accent);box-shadow:0 0 0 3px color-mix(in srgb,var(--accent) 14%,transparent)}
.searchbox kbd{position:absolute;right:9px;font-family:var(--font-mono);font-size:10.5px;color:var(--muted);border:1px solid var(--border);border-radius:4px;padding:1px 5px;background:var(--surface-2)}
.top-actions{display:flex;align-items:center;gap:8px}
.icon-btn{width:34px;height:34px;display:inline-grid;place-items:center;border:1px solid transparent;border-radius:8px;background:transparent;color:var(--text-2);cursor:pointer}
.icon-btn:hover{background:var(--surface-2);border-color:var(--border);color:var(--text)}
/* ---------- 内容 ---------- */
.content{width:100%;max-width:1240px;margin:0 auto;padding:26px 28px 64px;flex:1}
.page-head{display:flex;align-items:flex-end;justify-content:space-between;gap:18px;margin:4px 0 20px}
.page-head-text{min-width:0}
.eyebrow{font-size:11px;letter-spacing:.1em;text-transform:uppercase;color:var(--muted);margin:0 0 6px;font-weight:650}
.page-title{font-size:23px;line-height:1.25;margin:0;font-weight:700;letter-spacing:-.015em;word-break:break-word}
.page-desc{color:var(--muted);font-size:13px;margin:5px 0 0;line-height:1.5}
.head-actions{display:flex;gap:8px;flex-wrap:wrap;flex:none}
.breadcrumb{display:flex;gap:8px;align-items:center;font-size:12px;color:var(--muted);margin-bottom:6px}
.breadcrumb a{color:var(--muted)}
.breadcrumb a:hover{color:var(--accent)}
/* ---------- 卡片 ---------- */
.card{background:var(--surface);border:1px solid var(--border);border-radius:var(--radius);box-shadow:var(--shadow-sm);min-width:0}
.card-head{display:flex;align-items:center;justify-content:space-between;gap:12px;padding:14px 18px;border-bottom:1px solid var(--border);min-height:50px}
.card-title{font-size:13.5px;font-weight:650;margin:0;letter-spacing:.01em;display:flex;align-items:center;gap:8px}
.card-body{padding:16px 18px}
.card-body.flush{padding:0}
.count{font-size:11.5px;color:var(--muted);background:var(--surface-2);border:1px solid var(--border);border-radius:999px;padding:1px 8px;font-weight:600}
.link-more{font-size:12px;color:var(--muted);display:inline-flex;align-items:center;gap:4px;white-space:nowrap}
.link-more:hover{color:var(--accent);text-decoration:none}
.stack{display:flex;flex-direction:column;gap:16px;min-width:0}
.grid{display:grid;gap:16px}
.grid-2{grid-template-columns:minmax(0,1.72fr) minmax(290px,1fr)}
/* ---------- 统计 ---------- */
.stats{display:grid;grid-template-columns:repeat(4,minmax(0,1fr));gap:14px;margin-bottom:18px}
.stat{background:var(--surface);border:1px solid var(--border);border-radius:var(--radius);padding:16px 17px 15px;box-shadow:var(--shadow-sm);min-width:0}
.stat-top{display:flex;justify-content:space-between;align-items:center;margin-bottom:10px}
.stat-icon{width:32px;height:32px;border-radius:9px;background:var(--surface-2);color:var(--text-2);display:grid;place-items:center}
.stat.warn .stat-icon{background:var(--warn-soft);color:var(--warn)}
.stat.danger .stat-icon{background:var(--danger-soft);color:var(--danger)}
.stat b{display:block;font-size:27px;line-height:1.1;font-weight:700;letter-spacing:-.02em;font-variant-numeric:tabular-nums}
.stat-label{display:block;font-size:12.5px;color:var(--text-2);margin-top:3px;font-weight:550}
.stat-hint{display:block;font-size:11.5px;color:var(--muted);margin-top:6px;white-space:nowrap;overflow:hidden;text-overflow:ellipsis}
/* ---------- 提醒 ---------- */
.alert{display:flex;align-items:flex-start;gap:11px;padding:12px 14px;border-radius:var(--radius-sm);border:1px solid var(--border);background:var(--surface);margin:0 0 18px;font-size:13px}
.alert>div{display:flex;flex-direction:column;gap:2px;flex:1;min-width:0}
.alert b{font-weight:650;font-size:13px}
.alert span{color:var(--text-2);font-size:12.5px;line-height:1.5}
.alert.ok{background:var(--ok-soft);border-color:color-mix(in srgb,var(--ok) 22%,var(--border));color:var(--ok)}
.alert.warn{background:var(--warn-soft);border-color:var(--warn-line);color:var(--warn)}
.alert.err{background:var(--danger-soft);border-color:var(--danger-line);color:var(--danger)}
.alert .btn{margin-left:auto;flex:none;align-self:center}
/* ---------- 按钮 ---------- */
.btn{display:inline-flex;align-items:center;justify-content:center;gap:6px;height:36px;padding:0 13px;border:1px solid var(--border);border-radius:8px;background:var(--surface);color:var(--text-2);font-size:13px;font-weight:550;cursor:pointer;white-space:nowrap;transition:background .14s,border-color .14s,color .14s}
.btn:hover{background:var(--surface-2);border-color:var(--border-strong);color:var(--text);text-decoration:none}
.btn.primary{background:var(--accent);border-color:var(--accent);color:var(--accent-contrast)}
.btn.primary:hover{background:var(--accent-strong);border-color:var(--accent-strong);color:var(--accent-contrast)}
.btn.ghost{background:transparent;border-color:transparent;color:var(--muted)}
.btn.ghost:hover{background:var(--surface-2);color:var(--text)}
.btn.danger{color:var(--danger);border-color:var(--danger-line);background:var(--danger-soft)}
.btn.danger:hover{background:color-mix(in srgb,var(--danger) 14%,var(--danger-soft))}
.btn.sm{height:29px;padding:0 9px;font-size:12px;border-radius:7px;gap:5px}
.btn:disabled{opacity:.5;cursor:not-allowed}
/* ---------- 表单 ---------- */
input[type="text"],input[type="password"],input[type="search"],input[type="number"],input[type="date"],select,textarea{width:100%;padding:8px 10px;border:1px solid var(--border);border-radius:8px;background:var(--surface);color:var(--text);font-size:13px;line-height:1.45;transition:border-color .14s,box-shadow .14s}
input::placeholder,textarea::placeholder{color:var(--muted);opacity:.85}
input:focus,select:focus,textarea:focus{outline:none;border-color:var(--accent);box-shadow:0 0 0 3px color-mix(in srgb,var(--accent) 13%,transparent)}
textarea{resize:vertical;min-height:84px}
select{appearance:none;background-image:linear-gradient(45deg,transparent 50%,var(--muted) 50%),linear-gradient(135deg,var(--muted) 50%,transparent 50%);background-position:calc(100% - 15px) 52%,calc(100% - 10px) 52%;background-size:5px 5px,5px 5px;background-repeat:no-repeat;padding-right:30px}
.field{display:flex;flex-direction:column;gap:5px;min-width:0}
.field label{font-size:12px;font-weight:650;color:var(--text-2);letter-spacing:.01em}
.field .req{color:var(--danger)}
.hint{font-size:11.5px;color:var(--muted);margin:6px 0 0;line-height:1.5}
.form-grid{display:grid;grid-template-columns:repeat(2,minmax(0,1fr));gap:13px}
.form-body{display:flex;flex-direction:column;gap:14px}
.form-actions{display:flex;gap:9px;align-items:center;flex-wrap:wrap;margin-top:2px}
.quick-row{display:flex;gap:8px;margin-top:9px;align-items:center}
.quick-row select{width:112px;flex:none}
.quick-row input{flex:1;min-width:0}
.quick-row .btn{flex:none}
.checks{display:flex;gap:8px;flex-wrap:wrap}
.check{display:inline-flex;align-items:center;gap:7px;padding:7px 10px;border:1px solid var(--border);border-radius:8px;background:var(--surface);font-size:12.5px;cursor:pointer}
.check input{width:14px;height:14px;margin:0;accent-color:var(--accent)}
.check span{color:var(--text-2)}
.check span b{font-family:var(--font-mono);font-size:11px;color:var(--text)}
/* ---------- 筛选 ---------- */
.filterbar{display:flex;gap:8px;align-items:flex-end;flex-wrap:wrap;padding:13px 16px;border-bottom:1px solid var(--border);background:var(--surface);border-radius:var(--radius) var(--radius) 0 0}
.filterbar .field{width:auto;flex:0 1 auto}
.filterbar select{width:auto;min-width:110px}
.filterbar .btn{flex:none}
.filter-search{position:relative;flex:1 1 190px;min-width:160px}
.filter-search .ico{position:absolute;left:9px;top:50%;transform:translateY(-50%);color:var(--muted)}
.filter-search input{padding-left:31px;height:36px}
.search-hero{background:var(--surface);border:1px solid var(--border);border-radius:var(--radius);padding:16px;margin-bottom:18px;box-shadow:var(--shadow-sm)}
.search-hero-input{display:flex;gap:9px;align-items:center;position:relative}
.search-hero-input>.ico{position:absolute;left:13px;color:var(--muted)}
.search-hero-input input{height:46px;padding-left:42px;font-size:15px;border-radius:10px}
.search-hero-input .btn{height:46px;padding:0 20px;flex:none}
.search-hero-filters{display:flex;gap:8px;align-items:flex-end;flex-wrap:wrap;margin-top:12px}
.search-hero-filters .field{width:auto}
.search-hero-filters select{width:auto;min-width:120px}
/* ---------- 表格 ---------- */
.table-wrap{overflow-x:auto}
.tbl{width:100%;border-collapse:collapse;font-size:13px}
.tbl th{text-align:left;padding:9px 16px;font-size:11px;font-weight:700;letter-spacing:.06em;color:var(--muted);border-bottom:1px solid var(--border);white-space:nowrap;background:var(--surface)}
.tbl td{padding:11px 16px;border-bottom:1px solid var(--border);vertical-align:middle;color:var(--text-2)}
.tbl tbody tr:last-child td{border-bottom:0}
.tbl tbody tr:hover{background:var(--surface-2)}
.tbl td.cell-main{min-width:260px;max-width:560px}
.mem-main{display:flex;gap:9px;align-items:flex-start;min-width:0}
.kind-dot{width:7px;height:7px;border-radius:50%;flex:none;margin-top:7px;background:var(--k-fact);display:inline-block}
.k-fact{--kc:var(--k-fact)}.k-rule{--kc:var(--k-rule)}.k-pref{--kc:var(--k-pref)}.k-lesson{--kc:var(--k-lesson)}
.k-host{--kc:var(--k-host)}.k-project{--kc:var(--k-project)}.k-idea{--kc:var(--k-idea)}
.k-secret{--kc:var(--k-secret)}.k-journal{--kc:var(--k-journal)}
.kind-dot.k-fact{background:var(--k-fact)}.kind-dot.k-rule{background:var(--k-rule)}.kind-dot.k-pref{background:var(--k-pref)}
.kind-dot.k-lesson{background:var(--k-lesson)}.kind-dot.k-host{background:var(--k-host)}.kind-dot.k-project{background:var(--k-project)}
.kind-dot.k-idea{background:var(--k-idea)}.kind-dot.k-secret{background:var(--k-secret)}.kind-dot.k-journal{background:var(--k-journal)}
.mem-text{min-width:0}
.mem-title{display:block;color:var(--text);font-weight:500;font-size:13px;line-height:1.45;overflow:hidden;text-overflow:ellipsis;display:-webkit-box;-webkit-line-clamp:2;-webkit-box-orient:vertical}
.mem-title:hover{color:var(--accent);text-decoration:none}
.mem-sub{display:flex;gap:5px;flex-wrap:wrap;margin-top:5px}
.mini-tag{font-size:11px;color:var(--muted);background:var(--surface-2);border-radius:5px;padding:0 5px;line-height:1.55}
.suspect{display:inline-flex;align-items:center;gap:3px;font-size:10.5px;color:var(--warn);white-space:nowrap}
.tbl td .kind-pill{white-space:nowrap}
.kind-pill{display:inline-flex;align-items:center;gap:5px;font-size:11.5px;font-weight:600;padding:1px 8px;border-radius:999px;color:var(--kc,var(--text-2));background:color-mix(in srgb,var(--kc,var(--muted)) 11%,transparent);white-space:nowrap}
.status{display:inline-flex;align-items:center;gap:4px;font-size:11.5px;font-weight:600;padding:1px 8px;border-radius:999px;white-space:nowrap}
.status::before{content:"";width:5px;height:5px;border-radius:50%;background:currentColor;opacity:.85}
.st-ok{color:var(--ok);background:var(--ok-soft)}
.st-warn{color:var(--warn);background:var(--warn-soft)}
.st-danger{color:var(--danger);background:var(--danger-soft)}
.row-actions{display:flex;gap:6px;flex-wrap:wrap;align-items:center}
.row-actions form{display:inline-flex;margin:0}
.pager{display:flex;gap:5px;justify-content:center;align-items:center;margin-top:18px;flex-wrap:wrap}
.page-btn{min-width:32px;height:32px;padding:0 9px;display:inline-flex;align-items:center;justify-content:center;border:1px solid var(--border);border-radius:7px;background:var(--surface);color:var(--text-2);font-size:12.5px}
.page-btn:hover{background:var(--surface-2);text-decoration:none;color:var(--text)}
.page-btn.current{background:var(--accent);border-color:var(--accent);color:var(--accent-contrast);font-weight:650}
.page-gap{color:var(--muted);padding:0 3px}
/* ---------- 最近/任务/主题 ---------- */
.recent-list{display:flex;flex-direction:column}
.recent{display:flex;align-items:flex-start;gap:10px;padding:11px 18px;border-bottom:1px solid var(--border);color:inherit}
.recent:last-child{border-bottom:0}
.recent:hover{background:var(--surface-2);text-decoration:none}
.recent-main{display:flex;flex-direction:column;gap:2px;min-width:0;flex:1}
.recent-title{font-size:13px;color:var(--text);line-height:1.45;overflow:hidden;text-overflow:ellipsis;display:-webkit-box;-webkit-line-clamp:1;-webkit-box-orient:vertical}
.recent-meta{font-size:11.5px;color:var(--muted);display:flex;gap:5px;align-items:center;flex-wrap:wrap}
.recent-time{font-size:11.5px;color:var(--muted);white-space:nowrap;margin-top:2px;font-variant-numeric:tabular-nums}
.task-list{display:flex;flex-direction:column}
.task{display:flex;align-items:flex-start;gap:9px;padding:10px 18px;border-bottom:1px solid var(--border);color:inherit}
.task:last-child{border-bottom:0}
.task:hover{background:var(--surface-2);text-decoration:none}
.task>span:last-child{display:flex;flex-direction:column;gap:1px;min-width:0}
.task-title{font-size:12.5px;color:var(--text);overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
.task-meta{font-size:11px;color:var(--warn)}
.task-label{padding:8px 18px 4px;font-size:10.5px;font-weight:700;letter-spacing:.1em;color:var(--muted);text-transform:uppercase}
.chips{display:flex;gap:6px;flex-wrap:wrap;align-items:center}
.kindfilter{display:flex;gap:6px;flex-wrap:wrap;padding:9px 16px;border-bottom:1px solid var(--border);background:var(--surface)}
.kchip{display:inline-flex;align-items:center;gap:5px;padding:3px 9px;border-radius:999px;border:1px solid transparent;background:var(--surface-2);color:var(--text-2);font-size:11.5px;white-space:nowrap}
.kchip b{font-size:10.5px;color:var(--muted);font-weight:600}
.kchip:hover{border-color:var(--border-strong);text-decoration:none;color:var(--text)}
.kchip.active{background:var(--accent-soft);color:var(--accent-strong);border-color:color-mix(in srgb,var(--accent) 24%,transparent)}
.kchip.active b{color:var(--accent-strong)}
.chip{display:inline-flex;align-items:center;gap:4px;background:var(--surface-2);border:1px solid transparent;border-radius:999px;padding:2px 9px;font-size:11.5px;color:var(--text-2);white-space:nowrap}
.chip b{font-size:10.5px;color:var(--muted);font-weight:600}
.chip:hover{border-color:var(--border-strong);color:var(--text);text-decoration:none}
.chip.active{background:var(--accent-soft);color:var(--accent-strong);border-color:color-mix(in srgb,var(--accent) 25%,transparent)}
.chip.mini{font-family:var(--font-mono);font-size:10.5px;padding:1px 6px}
/* ---------- 结果 ---------- */
.results{display:flex;flex-direction:column;gap:10px}
.result{display:block;background:var(--surface);border:1px solid var(--border);border-radius:var(--radius-sm);padding:14px 16px;color:inherit;box-shadow:var(--shadow-sm)}
.result:hover{border-color:var(--border-strong);text-decoration:none;box-shadow:var(--shadow-md)}
.result-top{display:flex;align-items:center;justify-content:space-between;gap:10px;margin-bottom:6px}
.result-top time{font-size:11.5px;color:var(--muted)}
.result-body{margin:0;font-size:13.5px;line-height:1.6;color:var(--text)}
.result-meta{display:flex;gap:6px;flex-wrap:wrap;margin-top:7px;font-size:11.5px;color:var(--muted);align-items:center}
mark{background:color-mix(in srgb,var(--warn) 26%,transparent);color:inherit;border-radius:3px;padding:0 1px}
/* ---------- 详情 ---------- */
.detail-grid{display:grid;grid-template-columns:minmax(0,1.75fr) minmax(280px,1fr);gap:16px;align-items:start}
.detail-main{min-width:0}
.detail-top{display:flex;align-items:center;gap:9px;flex-wrap:wrap;padding:14px 20px;border-bottom:1px solid var(--border)}
.detail-time{font-size:11.5px;color:var(--muted);margin-left:auto}
.detail-tags{display:flex;gap:6px;flex-wrap:wrap;padding:12px 20px 0}
.prose{padding:16px 20px 22px;font-size:14px;line-height:1.75;color:var(--text);word-break:break-word}
.prose p{margin:0 0 10px}
.prose p:last-child{margin-bottom:0}
.prose ul{margin:4px 0 12px;padding-left:20px}
.prose li{margin:3px 0}
.prose h2,.prose h3,.prose h4{margin:14px 0 6px;font-size:14.5px}
.prose blockquote{margin:6px 0 10px;padding:3px 12px;border-left:3px solid var(--border-strong);color:var(--text-2);font-size:13px}
.prose code{background:var(--surface-2);border:1px solid var(--border);border-radius:4px;padding:1px 4px;font-size:12.5px;word-break:break-all}
.prose a{word-break:break-all}
.prose pre.code{background:var(--surface-2);border:1px solid var(--border);border-radius:8px;padding:12px;overflow-x:auto;font-size:12.5px;line-height:1.6;margin:8px 0}
.prose pre.code code{background:none;border:0;padding:0;white-space:pre}
.prose-small{font-size:12.5px;line-height:1.65;padding:14px 18px}
.prose-small ul{padding-left:18px}
.meta-list{margin:0;display:flex;flex-direction:column}
.meta-row{display:grid;grid-template-columns:86px minmax(0,1fr);gap:10px;padding:6px 0;border-bottom:1px dashed var(--border);font-size:12px}
.meta-row:last-child{border-bottom:0}
.meta-row dt{color:var(--muted);font-size:11.5px}
.meta-row dd{margin:0;color:var(--text-2);word-break:break-word}
.chain{display:flex;flex-direction:column;gap:8px;font-size:12.5px}
.chain-row{display:flex;gap:8px;align-items:center;flex-wrap:wrap}
.chain-label{font-size:10.5px;font-weight:700;letter-spacing:.06em;color:var(--muted);background:var(--surface-2);border-radius:4px;padding:1px 6px;flex:none}
.chain-row a{min-width:0;overflow:hidden;text-overflow:ellipsis;white-space:nowrap;flex:1}
.history{display:flex;flex-direction:column;gap:10px}
.history-row{border-left:2px solid var(--border-strong);padding-left:10px}
.history-top{display:flex;align-items:center;gap:7px;flex-wrap:wrap}
.history-time{font-size:11px;color:var(--muted)}
.history-seq{font-size:10.5px;color:var(--muted);font-family:var(--font-mono)}
.history-row p{margin:4px 0 2px;font-size:12px;color:var(--text-2)}
.history-author{font-size:10.5px;color:var(--muted)}
.action-stack{display:flex;flex-direction:column;gap:8px}
.action-stack form{margin:0}
.action-stack .btn{width:100%;justify-content:flex-start}
.danger-zone{margin-top:6px;border-top:1px dashed var(--border);padding-top:10px}
.danger-zone summary{cursor:pointer;color:var(--danger);font-size:12px;font-weight:600}
.danger-zone form{display:flex;flex-direction:column;gap:8px;margin-top:9px}
.danger-zone label{display:flex;flex-direction:column;gap:4px;font-size:11.5px;color:var(--muted)}
.token-box{display:flex;align-items:center;gap:10px;background:var(--surface-2);border:1px solid var(--border);border-radius:9px;padding:12px 14px;margin:12px 0;flex-wrap:wrap}
.token-box code{flex:1;min-width:220px;word-break:break-all;font-size:13px;color:var(--text)}
/* ---------- 新记忆/主题 ---------- */
.new-grid{display:grid;grid-template-columns:minmax(0,1.7fr) minmax(270px,1fr);gap:16px;align-items:start}
.form-card .card-body{padding:20px}
.topic-grid{display:grid;grid-template-columns:repeat(auto-fill,minmax(240px,1fr));gap:12px}
.topic-card{display:flex;flex-direction:column;gap:8px;padding:16px;background:var(--surface);border:1px solid var(--border);border-radius:var(--radius);color:inherit;box-shadow:var(--shadow-sm);min-width:0}
.topic-card:hover{border-color:var(--border-strong);text-decoration:none;box-shadow:var(--shadow-md)}
.topic-top{display:flex;align-items:baseline;gap:3px}
.topic-hash{color:var(--muted);font-size:14px}
.topic-top b{font-size:14px;font-weight:650;overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
.topic-count{margin-left:auto;font-size:11px;color:var(--muted);background:var(--surface-2);border-radius:999px;padding:0 7px;font-weight:600}
.topic-preview{margin:0;font-size:12px;color:var(--muted);line-height:1.5;height:2.9em;overflow:hidden}
.topic-kinds{display:flex;gap:4px}
/* ---------- 规范 ---------- */
.rules-card{margin-bottom:14px}
.rules-list{display:flex;flex-direction:column}
.rule-item{display:flex;flex-direction:column;gap:5px;padding:14px 18px;border-bottom:1px solid var(--border)}
.rule-item:last-child{border-bottom:0}
.rule-body .prose,.rule-body p{margin:0;font-size:13.5px;line-height:1.65;padding:0}
.rule-body ul{margin:0;padding-left:18px}
.rule-body blockquote{margin:0 0 4px;padding:1px 0 1px 10px;border-left:2px solid var(--border-strong);color:var(--text-2)}
.rule-meta{display:flex;gap:9px;align-items:center;font-size:11px;color:var(--muted);flex-wrap:wrap}
.rule-meta .kind-pill{font-size:10.5px}
/* ---------- 空态 ---------- */
.empty{display:flex;flex-direction:column;align-items:center;text-align:center;gap:7px;padding:42px 22px;color:var(--muted)}
.empty>.ico{color:var(--border-strong);width:34px;height:34px}
.empty-title{margin:4px 0 0;font-size:14px;font-weight:650;color:var(--text-2)}
.empty-text{margin:0;font-size:12.5px;max-width:380px;line-height:1.6}
.empty .btn{margin-top:8px}
.empty .chips{margin-top:8px}
.problem-list{margin:0;padding-left:18px;color:var(--danger);font-size:12.5px;line-height:1.7}
.error-page{padding:80px 20px}
.error-code{font-family:var(--font-mono);font-size:13px;color:var(--muted);margin:0;letter-spacing:.1em}
/* ---------- 提示/页脚 ---------- */
.flash{display:flex;align-items:flex-start;gap:9px;padding:10px 14px;border:1px solid var(--border);border-radius:9px;background:var(--surface);font-size:12.5px;margin:0 0 16px;color:var(--text-2)}
.flash.ok{background:var(--ok-soft);border-color:color-mix(in srgb,var(--ok) 25%,var(--border));color:var(--ok)}
.flash.warn{background:var(--warn-soft);border-color:var(--warn-line);color:var(--warn)}
.flash.err{background:var(--danger-soft);border-color:var(--danger-line);color:var(--danger)}
.foot{border-top:1px solid var(--border);padding:16px 28px 30px;font-size:11px;color:var(--muted);text-align:center}
.foot code{background:var(--surface-2);border:1px solid var(--border);border-radius:4px;padding:1px 4px;font-size:10.5px}
.action-pill{display:inline-block;font-size:11px;font-weight:600;background:var(--surface-2);border-radius:5px;padding:1px 7px;color:var(--text-2);white-space:nowrap}
kbd{font-family:var(--font-mono);font-size:10.5px;border:1px solid var(--border);border-bottom-width:2px;border-radius:4px;padding:1px 5px;background:var(--surface);color:var(--text-2)}
/* ---------- 登录 ---------- */
.login-body{background:var(--bg);min-height:100vh;display:flex;align-items:center;justify-content:center;padding:28px}
.login-theme{position:fixed;top:18px;right:18px;z-index:10;background:var(--surface);border:1px solid var(--border)}
.login-wrap{display:grid;grid-template-columns:minmax(0,1.06fr) minmax(0,.94fr);width:min(980px,100%);background:var(--surface);border:1px solid var(--border);border-radius:18px;overflow:hidden;box-shadow:var(--shadow-md)}
.login-brand{position:relative;padding:42px 42px 36px;background:var(--accent-soft);color:var(--text);overflow:hidden}
.login-brand::after{content:"";position:absolute;right:-90px;bottom:-120px;width:320px;height:320px;border-radius:50%;background:radial-gradient(circle,color-mix(in srgb,var(--accent) 22%,transparent),transparent 68%);pointer-events:none}
.login-logo{padding:0;margin-bottom:34px}
.login-brand h1{font-size:27px;line-height:1.35;margin:0 0 14px;letter-spacing:-.02em;font-weight:700}
.login-brand p{font-size:13px;color:var(--text-2);line-height:1.7;margin:0 0 26px;max-width:34em}
.login-points{list-style:none;margin:0;padding:0;display:flex;flex-direction:column;gap:11px;font-size:12.5px;color:var(--text-2)}
.login-points li{display:flex;gap:9px;align-items:center}
.login-points .ico{color:var(--accent)}
.login-panel{padding:48px 44px;display:flex;flex-direction:column;justify-content:center}
.login-panel h2{font-size:21px;margin:0 0 8px;letter-spacing:-.01em}
.login-panel>p{font-size:12.5px;line-height:1.65;margin:0 0 20px}
.login-form{display:flex;flex-direction:column;gap:14px;margin-bottom:18px}
.login-form .btn{height:42px;font-size:14px}
.login-panel .hint{line-height:1.7}
/* ---------- 动画/响应式 ---------- */
@media (prefers-reduced-motion:reduce){*,*::before,*::after{transition:none!important;animation:none!important;scroll-behavior:auto!important}}
@media (max-width:1120px){
 .grid-2{grid-template-columns:1fr}
 .detail-grid{grid-template-columns:1fr}
 .new-grid{grid-template-columns:1fr}
 .stats{grid-template-columns:repeat(2,minmax(0,1fr))}
}
@media (max-width:960px){
 .app{display:block}
 .sidebar{position:fixed;inset:0 auto 0 0;width:286px;height:100vh;transform:translateX(-102%);transition:transform .22s ease;box-shadow:var(--shadow-md)}
 body.nav-open .sidebar{transform:none}
 .scrim{display:block;position:fixed;inset:0;background:rgba(8,10,14,.45);z-index:25;opacity:0;pointer-events:none;transition:opacity .2s}
 body.nav-open .scrim{opacity:1;pointer-events:auto}
 body.nav-open{overflow:hidden}
 .nav-toggle{display:inline-grid}
 .topbar-inner{flex-wrap:wrap;height:auto;padding:9px 16px;gap:9px}
 .crumb{order:2;font-size:12px}
 .searchbox{order:5;flex-basis:100%;max-width:none;margin-left:0}
 .searchbox kbd{display:none}
 .top-actions{order:3;margin-left:auto}
 .content{padding:20px 16px 56px}
 .page-title{font-size:21px}
 .page-head{align-items:flex-start}
 .head-actions{justify-content:flex-end}
 .form-grid{grid-template-columns:1fr}
 .foot{padding:14px 16px 28px}
}
@media (max-width:760px){
 .stats{grid-template-columns:repeat(2,minmax(0,1fr));gap:10px}
 .stat{padding:13px 14px}
 .stat b{font-size:23px}
 .stat-hint{display:none}
 .filterbar{padding:11px 12px}
 .filterbar .field,.filterbar select{width:100%;flex:1 1 44%}
 .filterbar .filter-search{flex-basis:100%}
 .filterbar .btn{flex:1}
 .tbl thead{display:none}
 .tbl,.tbl tbody,.tbl tr,.tbl td{display:block;width:100%}
 .tbl tr{border-bottom:1px solid var(--border);padding:10px 12px}
 .tbl tbody tr:last-child{border-bottom:0}
 .tbl tbody tr:hover{background:transparent}
 .tbl td{border:0;padding:3px 0;display:flex;gap:10px;justify-content:space-between;align-items:center;font-size:12px}
 .tbl td[data-label]::before{content:attr(data-label);color:var(--muted);font-size:11px;font-weight:600;flex:none}
 .tbl td.cell-main{display:block;padding-bottom:7px}
 .tbl td.cell-main .mem-title{font-size:13.5px}
 .tbl td[data-label="状态"]{justify-content:flex-start;gap:7px}
 .tbl td[data-label="操作"]{justify-content:flex-start;padding-top:7px}
 .tbl td[data-label="操作"]::before{display:none}
 .row-actions{width:100%}
 .row-actions .btn{flex:1}
 .pager{margin-top:14px}
 .page-head{flex-direction:column;align-items:stretch}
 .head-actions{justify-content:flex-start}
 .recent{padding:10px 14px}
 .recent-time{display:none}
 .card-head{padding:12px 14px}
 .card-body{padding:14px}
 .detail-top{padding:12px 15px}
 .prose{padding:14px 15px 18px;font-size:13.5px}
 .detail-tags{padding:10px 15px 0}
 .login-wrap{grid-template-columns:1fr;border-radius:14px}
 .login-brand{padding:28px 26px 22px}
 .login-brand h1{font-size:22px}
 .login-points{display:none}
 .login-panel{padding:30px 26px 34px}
 .login-body{padding:16px;align-items:flex-start}
 .login-wrap{margin-top:44px}
}
@media (max-width:420px){
 .stats{grid-template-columns:1fr 1fr}
 .head-actions .btn{flex:1}
 .quick-row{flex-wrap:wrap}
 .quick-row select{width:100%}
 .quick-row .btn{width:100%}
}
@media print{.sidebar,.topbar,.scrim,.head-actions,.action-stack,.foot{display:none!important}.app{display:block}.content{max-width:none;padding:0}}
"""

# ---------------------------------------------------------------- 脚本

EARLY_JS = """(function(){try{var t=localStorage.getItem('brain-theme');if(!t&&window.matchMedia&&window.matchMedia('(prefers-color-scheme: dark)').matches)t='dark';if(!t)t='light';document.documentElement.dataset.theme=t;}catch(e){}})();"""

MAIN_JS = """(function(){
  var root=document.documentElement;
  function setTheme(t){root.dataset.theme=t;try{localStorage.setItem('brain-theme',t)}catch(e){}}
  document.querySelectorAll('[data-theme-toggle]').forEach(function(b){b.addEventListener('click',function(){
    setTheme(root.dataset.theme==='dark'?'light':'dark');
  });});
  var body=document.body;
  document.querySelectorAll('[data-nav-toggle]').forEach(function(b){b.addEventListener('click',function(){
    body.classList.toggle('nav-open');
  });});
  document.querySelectorAll('[data-nav-close]').forEach(function(b){b.addEventListener('click',function(){
    body.classList.remove('nav-open');
  });});
  document.querySelectorAll('[data-copy-target]').forEach(function(btn){btn.addEventListener('click',function(){
    var el=document.querySelector(btn.getAttribute('data-copy-target'));
    if(!el)return;
    var text=el.innerText||el.textContent||'';
    function done(){var old=btn.querySelector('span');if(old){old.textContent='已复制';setTimeout(function(){old.textContent='复制'},1600);}else{var t=btn.textContent;btn.textContent='已复制';setTimeout(function(){btn.textContent=t},1600);}}
    if(navigator.clipboard&&navigator.clipboard.writeText){navigator.clipboard.writeText(text).then(done,done);}
    else{var ta=document.createElement('textarea');ta.value=text;document.body.appendChild(ta);ta.select();try{document.execCommand('copy')}catch(e){}document.body.removeChild(ta);done();}
  });});
  document.querySelectorAll('form[data-confirm]').forEach(function(f){f.addEventListener('submit',function(e){
    if(!window.confirm(f.getAttribute('data-confirm'))){e.preventDefault();}
  });});
  document.querySelectorAll('textarea[data-submit]').forEach(function(t){t.addEventListener('keydown',function(e){
    if((e.metaKey||e.ctrlKey)&&e.key==='Enter'&&t.form){e.preventDefault();t.form.submit();}
  });});
  var counter=document.getElementById('char-count');
  var bodyField=document.getElementById('body');
  if(counter&&bodyField){var upd=function(){counter.textContent=bodyField.value.length};bodyField.addEventListener('input',upd);upd();}
  var searchInput=document.querySelector('.searchbox input');
  if(searchInput){document.addEventListener('keydown',function(e){
    if(e.key==='/'&&document.activeElement&&document.activeElement.tagName!=='INPUT'&&document.activeElement.tagName!=='TEXTAREA'&&!e.metaKey&&!e.ctrlKey){e.preventDefault();searchInput.focus();searchInput.select();}
  });}
  document.querySelectorAll('.sidebar a.nav-item').forEach(function(a){a.addEventListener('click',function(){body.classList.remove('nav-open')});});
})();"""

# ---------------------------------------------------------------- 备份

def _size(n) -> str:
    try:
        n = float(n)
    except (TypeError, ValueError):
        return "—"
    for unit in ("B", "KB", "MB", "GB"):
        if n < 1024 or unit == "GB":
            return f"{n:.0f} {unit}" if unit == "B" else f"{n:.1f} {unit}"
        n /= 1024
    return f"{n:.1f} GB"


def _dest_where(d: dict) -> str:
    if d.get("type") == "webdav":
        return d.get("url") or "(未填地址)"
    return d.get("path") or "(未填路径)"


def _dest_badge(d: dict) -> str:
    label = {"local": "本地目录", "webdav": "WebDAV"}.get(d.get("type", ""), d.get("type", "?"))
    return f'<span class="chip mini">{esc(label)}</span>'


def backup_page(ctx: Ctx, st: dict, cfg: dict, edit_id: str = "") -> str:
    sched = cfg.get("schedule") or {}
    ret = cfg.get("retention") or {}
    dests = cfg.get("destinations") or []
    last = st.get("last") or {}
    reports = last.get("destinations") or {}
    editing = next((d for d in dests if d.get("id") == edit_id), None)

    alerts = []
    if not st.get("age_available"):
        alerts.append(
            '<div class="alert err">' + icon("alert", 18) +
            '<div><b>服务器上找不到 age</b><span>备份必须先加密,没有 age 就不会产出任何密文文件。'
            '安装 <code>apt install age</code>(或把官方静态二进制放进 PATH)后,重新生成密钥。</span></div></div>')
    if not st.get("recipient"):
        alerts.append(
            '<div class="alert warn">' + icon("shield", 18) +
            '<div><b>还没有配置收件人公钥</b><span>先在「加密密钥」里生成或导入一把 age 公钥;'
            '私钥只显示一次,请存到离线介质。</span></div></div>')
    if not cfg.get("enabled", True):
        alerts.append(
            '<div class="alert warn">' + icon("alert", 18) +
            '<div><b>备份已停用</b><span>手动备份也跑不起来;要恢复自动备份请勾回「启用备份」。</span></div></div>')
    if st.get("running"):
        alerts.append(
            '<div class="alert warn">' + icon("refresh", 18) +
            '<div><b>正在备份…</b><span>跑完刷新本页看结果。<a class="btn sm" href="/backup">刷新</a></span></div></div>')
    if last and not last.get("ok"):
        alerts.append(
            '<div class="alert err">' + icon("alert", 18) +
            '<div><b>上次备份失败</b><span>' +
            esc(last.get("error") or "有目的地失败,详见下方报告") + '</span></div></div>')

    stats = "".join([
        stat(esc(when(last.get("created_at") or st.get("last_run") or "")
                 if (last.get("created_at") or st.get("last_run")) else "还没有"),
             "上次备份",
             esc(("成功" if last.get("ok") else "失败") + " · " + str(last.get("trigger") or ""))
             if last else "还没有跑过", "clock",
             "ok" if last.get("ok") else ""),
        stat(esc(when(st["next_run"]) if st.get("next_run") else "—"), "下次计划",
             "按下面的频率自动执行" if st.get("next_run") else "在「自动备份」里选一个频率", "refresh", ""),
        stat(str(len(st.get("snapshots") or [])), "快照记录",
             "seq 到 " + str(st.get("last_seq") or 0), "database", ""),
        stat(_size(last.get("cipher_size")), "最近体积",
             (str(last.get("atoms", 0)) + " 条记忆") if last else "—", "file", ""),
    ])

    rows = []
    for d in dests:
        key = d.get("id", "")
        rep = reports.get(key) or {}
        if rep:
            tone, word = ("st-ok", "成功") if rep.get("ok") else ("st-danger", "失败")
            state = '<span class="status %s">%s</span>' % (tone, word)
            if not rep.get("ok"):
                state += '<div class="muted small">' + esc(rep.get("error", "")) + '</div>'
        else:
            state = '<span class="status">未跑过</span>'
        rows.append(
            '<tr><td>' + _dest_badge(d) +
            '<div class="muted small">' + esc(d.get("label") or "") + '</div></td>'
            '<td class="mono-sm" style="word-break:break-all">' + esc(_dest_where(d)) + '</td>'
            '<td>' + ('<span class="status st-ok">启用</span>' if d.get("enabled", True)
                      else '<span class="status">停用</span>') + '</td>'
            '<td data-label="最近">' + state + '</td>'
            '<td data-label="操作" class="nowrap">'
            '<a class="btn sm" href="/backup?edit=' + esc(key) + '">编辑</a>'
            '<form method="post" action="/backup/dest" class="inline">'
            '<input type="hidden" name="id" value="' + esc(key) + '">'
            '<input type="hidden" name="action" value="test">'
            '<button class="btn sm" type="submit">测试</button></form>'
            '<form method="post" action="/backup/dest" class="inline" '
            'data-confirm="删除这个目的地?已上传的快照不会被清理。">'
            '<input type="hidden" name="id" value="' + esc(key) + '">'
            '<input type="hidden" name="action" value="delete">'
            '<button class="btn sm danger" type="submit">删除</button></form>'
            '</td></tr>')
    if not rows:
        rows.append('<tr><td colspan="5" class="muted">还没有目的地。至少加一个,否则备份无处可去。</td></tr>')

    e = editing or {}
    pw_hint = "已保存,留空表示不修改" if e.get("password_set") else "可选"
    cancel = '<a class="btn" href="/backup">取消编辑</a>' if editing else ""
    dest_form = (
        '<form method="post" action="/backup/dest" class="form-body">'
        '<input type="hidden" name="action" value="save">'
        '<input type="hidden" name="id" value="' + esc(e.get("id", "")) + '">'
        '<div class="form-grid">'
        '<div class="field"><label for="dtype">类型</label>' +
        select("type", [("local", "本地目录"), ("webdav", "WebDAV")], e.get("type", "local")) + '</div>'
        '<div class="field"><label for="dlabel">名称</label>'
        '<input id="dlabel" name="label" maxlength="40" value="' + esc(e.get("label", "")) +
        '" placeholder="例如 外置硬盘 / 网盘"></div>'
        '<div class="field"><label for="dpath">本地路径(本地目录填)</label>'
        '<input id="dpath" name="path" value="' + esc(e.get("path", "")) +
        '" placeholder="/backup/brain"></div>'
        '<div class="field"><label for="durl">WebDAV 目录地址(WebDAV 填)</label>'
        '<input id="durl" name="url" value="' + esc(e.get("url", "")) +
        '" placeholder="https://dav.example.com/brain/"></div>'
        '<div class="field"><label for="duser">WebDAV 用户名</label>'
        '<input id="duser" name="username" value="' + esc(e.get("username", "")) +
        '" autocomplete="off"></div>'
        '<div class="field"><label for="dpw">WebDAV 密码</label>'
        '<input id="dpw" name="password" type="password" value="" autocomplete="new-password" '
        'placeholder="' + esc(pw_hint) + '"></div>'
        '</div>'
        '<label class="check"><input type="checkbox" name="enabled" value="1"' +
        (' checked' if e.get("enabled", True) else "") + '><span>启用这个目的地</span></label>'
        '<div class="form-actions"><button class="btn primary" type="submit">' + icon("check", 16) +
        ('保存修改' if editing else '添加目的地') + '</button>' + cancel + '</div>'
        '<p class="hint">密码保存在 <code>state/backup.json</code>(0600);接口与页面只回「已保存」,永不回显明文。</p>'
        '</form>')

    mode_opts = [("off", "关闭(只手动)"), ("daily", "每天"), ("weekly", "每周"), ("every", "每隔 N 小时")]
    weekday_opts = [(str(i), n) for i, n in
                    enumerate(["周一", "周二", "周三", "周四", "周五", "周六", "周日"])]
    schedule_form = (
        '<form method="post" action="/backup/schedule" class="form-body">'
        '<div class="form-grid">'
        '<div class="field"><label for="smode">频率</label>' +
        select("mode", mode_opts, sched.get("mode", "off")) + '</div>'
        '<div class="field"><label for="sat">时间</label>'
        '<input id="sat" name="at" type="time" value="' + esc(sched.get("at", "03:40")) + '"></div>'
        '<div class="field"><label for="sday">星期几(每周)</label>' +
        select("weekday", weekday_opts, str(sched.get("weekday", 0))) + '</div>'
        '<div class="field"><label for="severy">间隔小时(每隔)</label>'
        '<input id="severy" name="every_hours" type="number" min="1" max="168" value="' +
        esc(sched.get("every_hours", 6)) + '"></div>'
        '<div class="field"><label for="sjit">随机抖动(分钟)</label>'
        '<input id="sjit" name="jitter_min" type="number" min="0" max="360" value="' +
        esc(sched.get("jitter_min", 30)) + '"></div>'
        '<div class="field"><label for="rkeep">保留份数</label>'
        '<input id="rkeep" name="daily" type="number" min="1" max="365" value="' +
        esc(ret.get("daily", 30)) + '"></div>'
        '</div>'
        '<label class="check"><input type="checkbox" name="keep_monthly" value="1"' +
        (' checked' if ret.get("keep_monthly", True) else "") +
        '><span>每月第一天的快照永久保留</span></label>'
        '<label class="check"><input type="checkbox" name="enabled" value="1"' +
        (' checked' if cfg.get("enabled", True) else "") +
        '><span>启用备份(含手动)</span></label>'
        '<div class="form-actions"><button class="btn primary" type="submit">' + icon("check", 16) +
        '保存</button></div>'
        '<p class="hint">抖动是给这份库固定的偏移量,避免多台机器同一分钟一起上传;'
        '页面显示的下次时间已含抖动。</p></form>')

    if st.get("recipient"):
        key_body = (
            '<p class="muted">当前收件人公钥(加密用,可公开):</p>'
            '<div class="token-box"><code id="rcpt">' + esc(st["recipient"]) + '</code>'
            '<button class="btn" type="button" data-copy-target="#rcpt">' + icon("copy", 16) +
            '复制</button></div>'
            '<p class="hint">私钥在 <code>' + esc(st.get("key_path", "state/age.key")) +
            '</code>(0600),<b>不在任何快照里</b> —— 请另行离线保存一份,'
            '否则硬盘坏了密文也打不开。</p>'
            '<form method="post" action="/backup/key"><input type="hidden" name="action" value="generate">'
            '<button class="btn danger" type="submit" '
            'data-confirm="重新生成会让旧密文无法用新私钥解开,确定?">' + icon("key", 16) +
            '重新生成密钥</button></form>')
    else:
        key_body = (
            '<p class="muted">还没有密钥。生成一把(推荐),或粘贴一把已有的 age 公钥。</p>'
            '<form method="post" action="/backup/key" class="inline">'
            '<input type="hidden" name="action" value="generate">'
            '<button class="btn primary" type="submit">' + icon("key", 16) + '生成密钥</button></form>'
            '<form method="post" action="/backup/key" class="form-body">'
            '<input type="hidden" name="action" value="import">'
            '<div class="field"><label for="rcptin">已有 age 公钥</label>'
            '<input id="rcptin" name="recipient" placeholder="age1..."></div>'
            '<div class="form-actions"><button class="btn" type="submit">导入公钥</button></div></form>')

    snap_rows = []
    for sn in (st.get("snapshots") or []):
        dest_cell = " ".join(
            '<span class="status %s">%s</span>' % ("st-ok" if v.get("ok") else "st-danger", esc(k))
            for k, v in (sn.get("destinations") or {}).items()) or '<span class="muted">—</span>'
        snap_rows.append(
            '<tr><td class="mono-sm">#' + esc(sn.get("seq")) + '</td>'
            '<td class="nowrap">' + esc(when(sn.get("created_at", ""))) + '</td>'
            '<td class="mono-sm">' + esc(sn.get("atoms", "—")) + '</td>'
            '<td class="mono-sm">' + _size(sn.get("cipher_size")) + '</td>'
            '<td class="mono-sm" title="' + esc(sn.get("cipher_sha256", "")) + '">' +
            esc((sn.get("cipher_sha256") or "")[:12]) + '…</td>'
            '<td>' + dest_cell + '</td></tr>')
    if not snap_rows:
        snap_rows.append('<tr><td colspan="6" class="muted">还没有快照记录。</td></tr>')

    actions = ('<form method="post" action="/backup/run" class="inline" '
               'data-confirm="现在立刻做一次备份?">'
               '<button class="btn primary" type="submit">' + icon("shield", 16) +
               '立即备份</button></form>')

    return (
        page_head("备份", "加密快照 · 目的地、频率、密钥都在这里配,不用登服务器", actions=actions) +
        "".join(alerts) +
        '<div class="stat-grid">' + stats + '</div>'
        '<section class="card"><div class="card-head"><h3 class="card-title">目的地</h3>'
        '<span class="count">' + str(len(dests)) + '</span></div>'
        '<div class="table-wrap"><table class="tbl"><thead><tr><th>类型</th><th>位置</th>'
        '<th>状态</th><th>最近结果</th><th>操作</th></tr></thead><tbody>' +
        "".join(rows) + '</tbody></table></div></section>'
        '<section class="card"><div class="card-head"><h3 class="card-title">' +
        ('编辑目的地' if editing else '添加目的地') + '</h3></div>'
        '<div class="card-body">' + dest_form + '</div></section>'
        '<section class="card"><div class="card-head"><h3 class="card-title">自动备份与保留</h3></div>'
        '<div class="card-body">' + schedule_form + '</div></section>'
        '<section class="card"><div class="card-head"><h3 class="card-title">加密密钥</h3></div>'
        '<div class="card-body">' + key_body + '</div></section>'
        '<section class="card"><div class="card-head"><h3 class="card-title">快照记录</h3>'
        '<span class="count">' + str(len(st.get("snapshots") or [])) + '</span></div>'
        '<div class="table-wrap"><table class="tbl"><thead><tr><th>seq</th><th>时间</th>'
        '<th>记忆数</th><th>体积</th><th>sha256</th><th>目的地</th></tr></thead><tbody>' +
        "".join(snap_rows) + '</tbody></table></div></section>'
        '<p class="hint">恢复步骤见 <code>docs/BACKUP.md</code>:解密 → 解包 → 起服务 → '
        '<code>brain verify</code> 五项全过。快照里不含 <code>views/</code>(可重建)'
        '与 age 私钥(必须离线另存)。</p>')


def backup_key_page(ctx: Ctx, pub: str, priv: str, key_path: str) -> str:
    return (
        page_head("私钥已生成", "只显示这一次,离开本页后无法再次查看") +
        '<div class="alert err">' + icon("alert", 18) +
        '<div><b>现在抄走它</b><span>私钥不会写进任何快照,也不会再显示。'
        '丢了它 = 所有密文变废纸,没有找回途径。</span></div></div>'
        '<section class="card"><div class="card-body">'
        '<p class="muted">收件人公钥(加密用,可公开)</p>'
        '<div class="token-box"><code>' + esc(pub) + '</code></div>'
        '<p class="muted" style="margin-top:16px">私钥(解密用,存到离线介质后从这里删掉)</p>'
        '<div class="token-box"><code id="priv-key">' + esc(priv) + '</code>'
        '<button class="btn" type="button" data-copy-target="#priv-key">' + icon("copy", 16) +
        '复制</button></div>'
        '<p class="hint">服务器上已保存一份在 <code>' + esc(key_path) + '</code>(0600) 供自动备份使用;'
        '但它是单点 —— 请用 U 盘 / 密码管理器 / 打印纸另存一份。</p>'
        '<div class="form-actions"><a class="btn primary" href="/backup">我已妥善保存,返回备份页</a></div>'
        '</div></section>')
