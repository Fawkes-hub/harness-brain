"""备份子系统 —— 让部署者在管理台里配,不用登服务器。

设计要点（每一条都对应一次真实踩坑,见 docs/BACKUP.md）：

1. **出网的一律是密文**。快照 = tar.gz → age 加密 → 才交给目的地。没有「明文备份」这个选项。
2. **私钥绝不进快照**。`state/age.key` 在打包时被排除——否则钥匙跟着密文一起跑,
   等于把保险箱和钥匙绑一块寄出去。
3. **凭据不回显**。配置里可以存 WebDAV 密码,但 `public()` 只回 `password_set: true`。
4. **每个目的地独立成败**。一家挂了不影响另一家,报告里逐项写明。
5. **成功判据是「读回校验」**。写完必须 GET 回来比对 sha256,命令返回 0 不算数。
6. **hash chain**。每份 manifest 的 `prev_cipher_sha256` 指向上一份密文,回滚/替换能被发现。
7. **不依赖 git / zstd 二进制**。tarfile + gzip 都在标准库;age 是唯一的软依赖。
"""
from __future__ import annotations

import base64
import copy
import datetime as dt
import hashlib
import json
import os
import re
import shutil
import subprocess
import tarfile
import threading
import urllib.error
import urllib.request
from pathlib import Path
from urllib.parse import quote

from . import __version__ as SERVER_VERSION  # 单一版本来源: brainlib/__init__.py

AGE_RECIPIENT_RE = re.compile(r"age1[0-9a-z]{20,80}")

SEQ_WIDTH = 6
MAX_INDEX = 200

# 快照排除项:派生的、临时的、以及**绝不能带上路的私钥**
EXCLUDE_DIRS = {"views", "__pycache__", ".rebuild"}
EXCLUDE_FILES = {"state/age.key", "state/.rebuild.lock", "state/backup.lock"}
EXCLUDE_SUFFIX = (".tmp", ".pyc")

DEFAULTS: dict = {
    "version": 1,
    "enabled": True,
    "cipher": {"kind": "age", "recipient": "", "cmd": "age"},
    "schedule": {"mode": "off", "at": "03:40", "jitter_min": 30,
                 "every_hours": 6, "weekday": 0},
    "retention": {"daily": 30, "keep_monthly": True},
    "destinations": [],
}

DEST_TYPES = {
    "local": "本地目录",
    "webdav": "WebDAV",
}

DAY_NAMES = ["周一", "周二", "周三", "周四", "周五", "周六", "周日"]


# ----------------------------------------------------------------- 工具

def _now() -> dt.datetime:
    return dt.datetime.now().astimezone()


def _iso(t: dt.datetime | None = None) -> str:
    return (t or _now()).isoformat(timespec="seconds")


def _deep_merge(base: dict, over: dict) -> dict:
    out = copy.deepcopy(base)
    for k, v in (over or {}).items():
        if isinstance(v, dict) and isinstance(out.get(k), dict):
            out[k] = _deep_merge(out[k], v)
        else:
            out[k] = v
    return out


def sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def sha256_file(path: Path | str) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def atomic_write(path: Path, data: str, mode: int = 0o600) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    with open(tmp, "w", encoding="utf-8") as fh:
        fh.write(data)
        fh.flush()
        os.fsync(fh.fileno())
    os.chmod(tmp, mode)
    os.replace(tmp, path)


# ----------------------------------------------------------------- 目的地

class DestinationError(RuntimeError):
    pass


class Destination:
    """一个备份目标。所有方法只处理「密文字节」,不碰明文。"""

    type = "base"
    label = ""

    def __init__(self, cfg: dict):
        self.cfg = cfg

    @property
    def id(self) -> str:
        return self.cfg.get("id", "")

    @property
    def enabled(self) -> bool:
        return bool(self.cfg.get("enabled", True))

    def describe(self) -> str:
        return self.label

    # --- 子类实现 ---
    def put(self, name: str, data: bytes) -> None:
        raise NotImplementedError

    def get(self, name: str) -> bytes:
        raise NotImplementedError

    def listing(self) -> list[str]:
        raise NotImplementedError

    def delete(self, name: str) -> None:
        raise NotImplementedError

    # --- 通用:写入 + 读回校验 ---
    def put_verified(self, name: str, data: bytes) -> None:
        self.put(name, data)
        back = self.get(name)
        if sha256_bytes(back) != sha256_bytes(data):
            raise DestinationError("读回校验不一致(内容被截断或改写)")

    def test(self) -> str:
        probe = b"brain-backup-probe\n"
        name = "brain-probe.txt"
        try:
            self.put(name, probe)
            back = self.get(name)
            if back != probe:
                raise DestinationError("探针读回内容不一致")
        finally:
            try:
                self.delete(name)
            except Exception:
                pass
        return "连通"


class LocalDest(Destination):
    type = "local"
    label = "本地目录"

    @property
    def path(self) -> Path:
        return Path(os.path.expanduser(self.cfg.get("path") or "")).resolve()

    def describe(self) -> str:
        return str(self.path)

    def put(self, name: str, data: bytes) -> None:
        p = self.path / name
        p.parent.mkdir(parents=True, exist_ok=True)
        tmp = p.with_suffix(p.suffix + ".tmp")
        with open(tmp, "wb") as fh:
            fh.write(data)
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(tmp, p)

    def get(self, name: str) -> bytes:
        p = self.path / name
        if not p.exists():
            raise DestinationError(f"文件不存在: {p}")
        return p.read_bytes()

    def listing(self) -> list[str]:
        if not self.path.exists():
            return []
        return sorted(p.name for p in self.path.iterdir() if p.is_file())

    def delete(self, name: str) -> None:
        (self.path / name).unlink(missing_ok=True)


class WebDavDest(Destination):
    type = "webdav"
    label = "WebDAV"

    def _base(self) -> str:
        url = (self.cfg.get("url") or "").strip()
        if not url:
            raise DestinationError("WebDAV 地址为空")
        if not url.endswith("/"):
            url += "/"
        return url

    def describe(self) -> str:
        return self._base()

    def _auth_header(self) -> dict:
        user = self.cfg.get("username") or ""
        pwd = self.cfg.get("password") or ""
        headers = {}
        if user:
            raw = f"{user}:{pwd}".encode()
            headers["Authorization"] = "Basic " + base64.b64encode(raw).decode()
        return headers

    def _url(self, name: str) -> str:
        return self._base() + quote(name)

    def _req(self, method: str, url: str, data: bytes | None = None,
             headers: dict | None = None, timeout: float = 30) -> tuple[int, bytes]:
        h = {"User-Agent": f"brain/{SERVER_VERSION} backup"}
        h.update(self._auth_header())
        h.update(headers or {})
        req = urllib.request.Request(url, data=data, method=method, headers=h)
        try:
            with urllib.request.urlopen(req, timeout=timeout) as r:
                return r.status, r.read()
        except urllib.error.HTTPError as e:
            body = e.read() if hasattr(e, "read") else b""
            return e.code, body or str(e).encode()
        except urllib.error.URLError as e:
            raise DestinationError(f"连接失败: {e.reason}") from e

    def _ensure_collection(self) -> None:
        code, _ = self._req("MKCOL", self._base())
        if code not in (200, 201, 204, 301, 405):   # 405 = 已存在
            raise DestinationError(f"创建目录失败(HTTP {code})")

    def put(self, name: str, data: bytes) -> None:
        self._ensure_collection()
        code, body = self._req("PUT", self._url(name), data,
                               {"Content-Type": "application/octet-stream"})
        if code not in (200, 201, 204):
            raise DestinationError(f"上传失败(HTTP {code} {body[:120].decode('utf-8', 'replace')})")

    def get(self, name: str) -> bytes:
        code, body = self._req("GET", self._url(name))
        if code != 200:
            raise DestinationError(f"下载失败(HTTP {code})")
        return body

    def listing(self) -> list[str]:
        body = (b'<?xml version="1.0"?><d:propfind xmlns:d="DAV:">'
                b"<d:prop><d:getcontentlength/></d:prop></d:propfind>")
        code, raw = self._req("PROPFIND", self._base(), body,
                              {"Depth": "1", "Content-Type": "application/xml"})
        if code not in (200, 207):
            raise DestinationError(f"列目录失败(HTTP {code})")
        names = []
        for href in re.findall(r"<[^>]*href[^>]*>([^<]+)</[^>]*href>", raw.decode("utf-8", "replace")):
            name = href.rstrip("/").rsplit("/", 1)[-1]
            if name:
                names.append(urllib.request.unquote(name))
        return sorted(set(names))

    def delete(self, name: str) -> None:
        code, _ = self._req("DELETE", self._url(name))
        if code not in (200, 204, 404):
            raise DestinationError(f"删除失败(HTTP {code})")


DEST_CLASSES = {"local": LocalDest, "webdav": WebDavDest}


def make_destination(cfg: dict) -> Destination:
    cls = DEST_CLASSES.get(cfg.get("type", ""))
    if cls is None:
        raise DestinationError(f"未知目的地类型: {cfg.get('type')}")
    return cls(cfg)


# ----------------------------------------------------------------- 调度计算

def _jitter_seconds(seed: str, jitter_min: int) -> int:
    if jitter_min <= 0:
        return 0
    digest = hashlib.sha256(seed.encode()).hexdigest()
    return int(digest[:8], 16) % (jitter_min * 60 + 1)


def compute_next_run(sched: dict, now: dt.datetime | None = None,
                     seed: str = "") -> str:
    """返回下一次该跑的时间(ISO),mode=off 时返回空串。

    抖动是**确定性**的:同一天同一库算出同一个偏移,页面上显示的就是真会跑的时间,
    而不是「大概三点半左右」。
    """
    now = now or _now()
    mode = (sched.get("mode") or "off").lower()
    if mode == "off":
        return ""
    try:
        hh, mm = [int(x) for x in str(sched.get("at") or "03:40").split(":")[:2]]
    except ValueError:
        hh, mm = 3, 40
    jitter = int(sched.get("jitter_min") or 0)

    if mode == "every":
        hours = max(1, min(168, int(sched.get("every_hours") or 6)))
        nxt = now.replace(second=0, microsecond=0) + dt.timedelta(hours=hours)
        return _iso(nxt)

    if mode == "weekly":
        weekday = int(sched.get("weekday") or 0) % 7        # 0=周一
        base = now.replace(hour=hh, minute=mm, second=0, microsecond=0)
        ahead = (weekday - base.weekday()) % 7
        cand = base + dt.timedelta(days=ahead)
        if cand <= now:
            cand += dt.timedelta(days=7)
        cand += dt.timedelta(seconds=_jitter_seconds(f"{seed}:{cand.date()}:w", jitter))
        return _iso(cand)

    # daily(默认)
    base = now.replace(hour=hh, minute=mm, second=0, microsecond=0)
    cand = base + dt.timedelta(seconds=_jitter_seconds(f"{seed}:{base.date()}:d", jitter))
    if cand <= now:
        base = base + dt.timedelta(days=1)
        cand = base + dt.timedelta(seconds=_jitter_seconds(f"{seed}:{base.date()}:d", jitter))
    return _iso(cand)


# ----------------------------------------------------------------- 主体

class Backup:
    """一个库的备份门面:配置 / 快照 / 加密 / 分发 / 保留 / 状态。"""

    def __init__(self, root: Path | str):
        self.root = Path(root)
        self.state_dir = self.root / "state"
        self.cfg_path = self.state_dir / "backup.json"
        self.state_path = self.state_dir / "backup-state.json"
        self.index_path = self.state_dir / "backup-index.json"
        self.key_path = self.state_dir / "age.key"
        self._run_lock = threading.Lock()

    # ---------- 配置 ----------
    def load(self) -> dict:
        cfg = copy.deepcopy(DEFAULTS)
        if self.cfg_path.exists():
            try:
                cfg = _deep_merge(cfg, json.loads(self.cfg_path.read_text(encoding="utf-8")))
            except (OSError, json.JSONDecodeError):
                pass
        return cfg

    def save(self, cfg: dict) -> dict:
        cfg = _deep_merge(DEFAULTS, cfg)
        atomic_write(self.cfg_path, json.dumps(cfg, ensure_ascii=False, indent=2))
        return cfg

    def public(self, cfg: dict | None = None) -> dict:
        """给 UI / API 的版本:**凭据一律摘掉**,只留「配没配」。"""
        cfg = copy.deepcopy(cfg or self.load())
        for d in cfg.get("destinations", []):
            if d.get("password"):
                d["password"] = ""
                d["password_set"] = True
            else:
                d["password_set"] = False
        cfg["cipher"]["has_key"] = self.key_path.exists()
        if not cfg["cipher"].get("recipient"):
            cfg["cipher"]["recipient"] = self.recipient_from_key() or ""
        cfg["age_available"] = self.age_bin() is not None
        return cfg

    # ---------- 状态 ----------
    def state(self) -> dict:
        if self.state_path.exists():
            try:
                return json.loads(self.state_path.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError):
                pass
        return {"last_run": "", "last_ok": None, "last_seq": 0, "next_run": "", "last_report": {}}

    def save_state(self, st: dict) -> None:
        atomic_write(self.state_path, json.dumps(st, ensure_ascii=False, indent=2))

    def index(self) -> dict:
        if self.index_path.exists():
            try:
                idx = json.loads(self.index_path.read_text(encoding="utf-8"))
                if isinstance(idx, dict) and isinstance(idx.get("snapshots"), list):
                    return idx
            except (OSError, json.JSONDecodeError):
                pass
        return {"seq": 0, "snapshots": []}

    def save_index(self, idx: dict) -> None:
        idx["snapshots"] = idx.get("snapshots", [])[-MAX_INDEX:]
        atomic_write(self.index_path, json.dumps(idx, ensure_ascii=False, indent=2))

    # ---------- 密钥 ----------
    def age_bin(self) -> str | None:
        cmd = (self.load().get("cipher") or {}).get("cmd") or "age"
        found = shutil.which(cmd)
        if found:
            return found
        p = Path(cmd)
        return str(p) if p.is_file() and os.access(p, os.X_OK) else None

    def keygen_bin(self) -> str | None:
        age = self.age_bin()
        if not age:
            return None
        cand = Path(age).with_name("age-keygen")
        if cand.is_file() and os.access(cand, os.X_OK):
            return str(cand)
        return shutil.which("age-keygen")

    def gen_key(self) -> tuple[str, str]:
        """生成新的 age 身份。私钥写 `state/age.key`(0600),公钥写回配置。

        返回 `(公钥, 私钥全文)`;私钥全文只在这一刻给管理员看一次,之后只有文件里有。
        为什么要把私钥写进库目录:这是"能恢复"的最低要求;同时快照打包会**排除**它
        (见 EXCLUDE_NAMES),免得密钥跟着密文一起走。
        """
        kg = self.keygen_bin()
        if not kg:
            raise DestinationError("找不到 age-keygen,请先安装 age")
        self.key_path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.key_path.with_name(self.key_path.name + ".tmp")
        try:
            proc = subprocess.run([kg, "-o", str(tmp)], capture_output=True, text=True, timeout=30)
            if proc.returncode != 0 or not tmp.exists():
                raise DestinationError(f"age-keygen 失败: {(proc.stderr or proc.stdout).strip()[:200]}")
            os.chmod(tmp, 0o600)
            text = tmp.read_text(encoding="utf-8")
            pub = ""
            for line in text.splitlines():
                if line.startswith("# public key:"):
                    pub = line.split(":", 1)[1].strip()
                    break
            if not AGE_RECIPIENT_RE.fullmatch(pub):
                raise DestinationError("age-keygen 的输出里找不到合法公钥")
            os.replace(tmp, self.key_path)
        finally:
            if tmp.exists():
                try:
                    tmp.unlink()
                except OSError:
                    pass
        cfg = self.load()
        cfg["cipher"]["recipient"] = pub
        self.save(cfg)
        self.recompute_next()
        return pub, text

    def recipient_from_key(self) -> str:
        if not self.key_path.exists():
            return ""
        try:
            for line in self.key_path.read_text(encoding="utf-8").splitlines():
                if line.lower().startswith("# public key:"):
                    return line.split(":", 1)[1].strip()
        except OSError:
            return ""
        return ""

    def ensure_key(self) -> tuple[str, str]:
        """生成密钥对;返回 (公钥, 私钥)。**私钥只在生成这一刻返回给调用方。**"""
        keygen = self.keygen_bin()
        if not keygen:
            raise DestinationError("服务器上找不到 age-keygen,无法生成密钥")
        r = subprocess.run([keygen, "-o", str(self.key_path)],
                           capture_output=True, text=True, timeout=60)
        if r.returncode != 0:
            raise DestinationError(f"生成密钥失败: {(r.stderr or '').strip()[:200]}")
        try:
            os.chmod(self.key_path, 0o600)
        except OSError:
            pass
        priv = self.key_path.read_text(encoding="utf-8").strip()
        pub = self.recipient_from_key() or (r.stderr.split(":", 1)[-1].strip())
        return pub, priv

    def import_recipient(self, recipient: str) -> str:
        recipient = recipient.strip()
        if not AGE_RECIPIENT_RE.fullmatch(recipient):
            raise DestinationError("公钥格式不对(应形如 age1…)")
        cfg = self.load()
        cfg["cipher"]["recipient"] = recipient
        self.save(cfg)
        return recipient

    # ---------- 快照 ----------
    def make_snapshot(self, dest_path: Path) -> dict:
        """把整个库目录打成 tar.gz。返回统计信息。"""
        stats = {"atoms": 0, "tombstones": 0, "files": 0}
        root = self.root
        with tarfile.open(dest_path, "w:gz", compresslevel=6) as tar:

            def add(p: Path, arcname: str) -> None:
                if arcname in EXCLUDE_FILES or p.name.endswith(EXCLUDE_SUFFIX):
                    return
                if p.is_dir():
                    if p.name in EXCLUDE_DIRS:
                        return
                    for child in sorted(p.iterdir(), key=lambda x: x.name):
                        add(child, f"{arcname}/{child.name}" if arcname else child.name)
                    return
                if not p.is_file():
                    return
                tar.add(p, arcname=arcname, recursive=False)
                stats["files"] += 1
                if arcname.startswith("atoms/") and arcname.endswith(".md"):
                    stats["atoms"] += 1
                elif arcname == "tombstones.jsonl":
                    try:
                        stats["tombstones"] = sum(
                            1 for line in p.read_text(encoding="utf-8").splitlines() if line.strip())
                    except OSError:
                        pass

            for child in sorted(root.iterdir(), key=lambda x: x.name):
                add(child, child.name)
        return stats

    def encrypt(self, src: Path, out: Path) -> None:
        cfg = self.load()
        age = self.age_bin()
        if not age:
            raise DestinationError("服务器上找不到 age,无法加密(不会降级成明文)")
        recipient = cfg["cipher"].get("recipient") or self.recipient_from_key()
        if not recipient:
            raise DestinationError("还没有收件人公钥,请先生成或导入密钥")
        r = subprocess.run([age, "-r", recipient, "-o", str(out), str(src)],
                           capture_output=True, text=True, timeout=600)
        if r.returncode != 0 or not out.exists():
            raise DestinationError(f"加密失败: {(r.stderr or '').strip()[:200]}")

    # ---------- 保留 ----------
    def prune(self, dest: Destination, idx: dict) -> list[str]:
        """删掉超出保留份数的旧快照。**月档(1 号那份)不动**;删不动只警告。"""
        cfg = self.load()
        keep_daily = max(1, int(cfg["retention"].get("daily") or 30))
        keep_monthly = bool(cfg["retention"].get("keep_monthly", True))
        try:
            names = [n for n in dest.listing() if re.fullmatch(r"brain-\d{6}\.age", n)]
        except Exception:
            return []
        seqs = sorted(int(re.findall(r"\d{6}", n)[0]) for n in names)
        created = {int(s.get("seq", 0)): str(s.get("created_at", "")) for s in idx.get("snapshots", [])}
        keep = set(seqs[-keep_daily:])
        if keep_monthly:
            for s in seqs:
                if created.get(s, "")[8:10] == "01":
                    keep.add(s)
        removed = []
        for s in seqs:
            if s in keep:
                continue
            name = f"brain-{s:0{SEQ_WIDTH}d}.age"
            try:
                dest.delete(name)
                try:
                    dest.delete(f"brain-{s:0{SEQ_WIDTH}d}.json")
                except Exception:
                    pass
                removed.append(name)
            except Exception:
                pass
        return removed

    # ---------- 执行 ----------
    def run(self, trigger: str = "manual") -> dict:
        cfg = self.load()
        started = _now()
        if not cfg.get("enabled", True):
            return {"ok": False, "error": "备份已停用", "trigger": trigger}
        dests = [make_destination(d) for d in cfg.get("destinations", []) if d.get("enabled", True)]
        if not dests:
            return {"ok": False, "error": "没有启用的目的地", "trigger": trigger}
        if not self._run_lock.acquire(blocking=False):
            return {"ok": False, "error": "已有一份备份正在跑", "trigger": trigger}
        try:
            idx = self.index()
            seq = int(idx.get("seq") or 0) + 1
            prev_sha = (idx.get("snapshots") or [{}])[-1].get("cipher_sha256", "")
            tmpdir = Path(self.state_dir / ".snap")
            if tmpdir.exists():
                shutil.rmtree(tmpdir, ignore_errors=True)
            tmpdir.mkdir(parents=True, exist_ok=True)
            try:
                plain = tmpdir / "brain.tar.gz"
                stats = self.make_snapshot(plain)
                cipher = tmpdir / "brain.age"
                self.encrypt(plain, cipher)
                data = cipher.read_bytes()
                csha = sha256_bytes(data)
                name = f"brain-{seq:0{SEQ_WIDTH}d}.age"
                manifest = {
                    "seq": seq,
                    "created_at": _iso(started),
                    "server_version": SERVER_VERSION,
                    "atoms": stats["atoms"],
                    "tombstones": stats["tombstones"],
                    "files": stats["files"],
                    "cipher_size": len(data),
                    "cipher_sha256": csha,
                    "prev_cipher_sha256": prev_sha,
                    "trigger": trigger,
                }
                results: dict[str, dict] = {}
                for d in dests:
                    t0 = _now()
                    entry: dict = {"type": d.type, "label": d.describe()}
                    try:
                        d.put_verified(name, data)
                        d.put(name.replace(".age", ".json"),
                              json.dumps(manifest, ensure_ascii=False, indent=2).encode())
                        entry["ok"] = True
                        entry["pruned"] = self.prune(d, idx)
                    except Exception as e:      # 一家挂了不影响别家
                        entry["ok"] = False
                        entry["error"] = str(e)[:300]
                    entry["ms"] = int((_now() - t0).total_seconds() * 1000)
                    results[d.id or d.describe()] = entry
                ok = bool(results) and all(r.get("ok") for r in results.values())
                idx["seq"] = seq
                idx.setdefault("snapshots", []).append(manifest)
                self.save_index(idx)
                report = {
                    "ok": ok, "seq": seq, "trigger": trigger,
                    "atoms": stats["atoms"], "tombstones": stats["tombstones"],
                    "files": stats["files"],
                    "cipher_size": len(data), "cipher_sha256": csha,
                    "prev_cipher_sha256": prev_sha,
                    "started_at": _iso(started), "finished_at": _iso(),
                    "destinations": results,
                }
                st = self.state()
                st.update({"last_run": _iso(started), "last_ok": ok,
                           "last_seq": seq, "last_report": report})
                st["next_run"] = compute_next_run(cfg["schedule"], seed=self.recipient_from_key() or "brain")
                self.save_state(st)
                return report
            finally:
                shutil.rmtree(tmpdir, ignore_errors=True)
        finally:
            self._run_lock.release()

    @property
    def running(self) -> bool:
        """是否正在跑备份(手动/调度共用一把锁,页面用它显示"进行中")。"""
        return self._run_lock.locked()

    def recompute_next(self) -> str:
        """改完频率后立刻刷新 next_run(页面/接口改配置必须调)。"""
        cfg = self.load()
        st = self.state()
        st["next_run"] = compute_next_run(cfg["schedule"],
                                         seed=self.recipient_from_key() or "brain")
        self.save_state(st)
        return st["next_run"]

    def status(self) -> dict:
        cfg = self.load()
        st = self.state()
        idx = self.index()
        sched = cfg.get("schedule") or {}
        seed = self.recipient_from_key() or "brain"
        if not st.get("next_run") and sched.get("mode") != "off":
            st["next_run"] = compute_next_run(sched, seed=seed)
        snaps = list(reversed(idx.get("snapshots", [])))[:12]
        return {
            "enabled": bool(cfg.get("enabled", True)),
            "running": self._run_lock.locked(),
            "key_path": str(self.key_path),
            "last": st.get("last_report") or {},
            "age_available": self.age_bin() is not None,
            "has_key": self.key_path.exists(),
            "recipient": cfg["cipher"].get("recipient") or self.recipient_from_key(),
            "schedule": sched,
            "next_run": st.get("next_run", ""),
            "last_run": st.get("last_run", ""),
            "last_ok": st.get("last_ok"),
            "last_seq": st.get("last_seq", 0),
            "last_report": st.get("last_report", {}),
            "destinations": [
                {"id": d.get("id", ""), "type": d.get("type", ""),
                 "label": d.get("label") or DEST_TYPES.get(d.get("type", ""), d.get("type", "")),
                 "enabled": bool(d.get("enabled", True))}
                for d in cfg.get("destinations", [])
            ],
            "snapshots": snaps,
            "seq": idx.get("seq", 0),
        }


# ----------------------------------------------------------------- 调度线程

class Scheduler(threading.Thread):
    """进程内单线程调度器。**不依赖 cron / systemd**,页面改完就生效。

    - 每 `tick` 秒醒一次,只有到点才真跑;
    - 库重启后按 `state/backup-state.json` 里的 next_run 判断要不要补跑(错过就立刻补);
    - 任何异常都吞掉并记进 state,绝不让线程死掉把服务带走。
    """

    def __init__(self, backup: Backup, tick: float = 20.0):
        super().__init__(name="brain-backup-scheduler", daemon=True)
        self.backup = backup
        self.tick = tick
        self._stop = threading.Event()

    def stop(self) -> None:
        self._stop.set()

    def due(self, now: dt.datetime | None = None) -> bool:
        cfg = self.backup.load()
        if not cfg.get("enabled", True) or cfg["schedule"].get("mode") == "off":
            return False
        st = self.backup.state()
        nxt = st.get("next_run") or compute_next_run(
            cfg["schedule"], seed=self.backup.recipient_from_key() or "brain")
        if not nxt:
            return False
        try:
            target = dt.datetime.fromisoformat(nxt)
        except ValueError:
            return False
        return (now or _now()) >= target

    def tick_once(self) -> dict | None:
        if not self.due():
            return None
        cfg = self.backup.load()
        try:
            report = self.backup.run("schedule")
        except Exception as e:
            report = {"ok": False, "error": str(e)[:300], "trigger": "schedule"}
        st = self.backup.state()
        st["next_run"] = compute_next_run(cfg["schedule"],
                                         seed=self.backup.recipient_from_key() or "brain")
        self.backup.save_state(st)
        return report

    def run(self) -> None:      # pragma: no cover - 线程循环
        while not self._stop.wait(self.tick):
            try:
                self.tick_once()
            except Exception:
                pass
