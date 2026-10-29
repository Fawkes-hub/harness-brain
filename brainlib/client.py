"""端侧 HTTP 客户端 + 离线队列。

优先级:环境变量 > ~/.config/brain/config.toml > 未配置(走本地文件模式)。
离线:POST 失败时追加到 ~/.local/state/brain/outbox.jsonl(0600),下次命令成功时按序重放。
"""
from __future__ import annotations

import json
import os
import urllib.error
import urllib.request
from pathlib import Path

CONFIG = Path(os.environ.get("BRAIN_CONFIG", Path.home() / ".config" / "brain" / "config.toml"))
OUTBOX = Path(os.environ.get("BRAIN_OUTBOX", Path.home() / ".local" / "state" / "brain" / "outbox.jsonl"))


def _parse_toml(text: str) -> dict:
    out: dict[str, str] = {}
    for line in text.splitlines():
        line = line.split("#", 1)[0].strip()
        if "=" in line:
            k, v = line.split("=", 1)
            out[k.strip()] = v.strip().strip('"').strip("'")
    return out


def load_config() -> dict:
    cfg: dict[str, str] = {}
    if CONFIG.exists():
        cfg.update(_parse_toml(CONFIG.read_text(encoding="utf-8")))
    for env, key in (("BRAIN_URL", "url"), ("BRAIN_TOKEN", "token"), ("BRAIN_ROLE", "role")):
        if os.environ.get(env):
            cfg[key] = os.environ[env]
    return cfg


def request(cfg: dict, method: str, path: str, payload: dict | None = None,
            timeout: float = 30.0) -> tuple[int, object]:
    url = cfg["url"].rstrip("/") + path
    data = None
    headers = {"Authorization": f"Bearer {cfg.get('token', '')}", "X-Brain-Client": "2.1.0"}
    if payload is not None:
        data = json.dumps(payload, ensure_ascii=False).encode()
        headers["Content-Type"] = "application/json"
    req = urllib.request.Request(url, data=data, headers=headers, method=method)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            raw = r.read().decode()
            ctype = r.headers.get("Content-Type", "")
            return r.status, (raw if "application/json" not in ctype else json.loads(raw))
    except urllib.error.HTTPError as e:
        raw = e.read().decode(errors="replace")
        try:
            return e.code, json.loads(raw)
        except json.JSONDecodeError:
            return e.code, {"error": {"code": "internal", "message": raw[:200]}}


def append_outbox(rec: dict) -> None:
    OUTBOX.parent.mkdir(parents=True, exist_ok=True)
    with open(OUTBOX, "a", encoding="utf-8") as fh:
        fh.write(json.dumps(rec, ensure_ascii=False) + "\n")
        fh.flush()
        os.fsync(fh.fileno())
    try:
        os.chmod(OUTBOX, 0o600)
    except OSError:
        pass


def replay(cfg: dict) -> int:
    """按序重放;失败即停(保序)。成功后才从队列移除。"""
    if not OUTBOX.exists():
        return 0
    lines = [l for l in OUTBOX.read_text(encoding="utf-8").splitlines() if l.strip()]
    done = 0
    for line in lines:
        try:
            rec = json.loads(line)
        except json.JSONDecodeError:
            done += 1
            continue
        try:
            code, _ = request(cfg, "POST", "/v1/remember", rec)
        except (urllib.error.URLError, TimeoutError, OSError):
            break
        if code in (200, 201, 409):
            done += 1
        else:
            break
    if done:
        rest = lines[done:]
        OUTBOX.write_text("".join(l + "\n" for l in rest), encoding="utf-8")
    return done
