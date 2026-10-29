"""令牌:签发、校验、吊销。

- 明文只打印一次,盘上只存 `sha256(salt || token)`(必须加盐,否则短 token 可查表反推);
- 身份(host/harness/role)与 scopes 存在令牌记录里,**服务端以它为准**,不信请求体自报。
"""
from __future__ import annotations

import hashlib
import hmac
import json
import secrets
from pathlib import Path

from .model import CROCKFORD, now_iso

SCOPES = {"read", "write", "secret", "admin"}


def _rand(n: int = 24) -> str:
    return "".join(secrets.choice(CROCKFORD) for _ in range(n))


class TokenStore:
    def __init__(self, path: Path):
        self.path = Path(path)

    def _load(self) -> dict:
        if not self.path.exists():
            return {"salt": "", "tokens": []}
        return json.loads(self.path.read_text(encoding="utf-8"))

    def _save(self, data: dict) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_suffix(".tmp")
        tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
        tmp.replace(self.path)
        try:
            import os
            os.chmod(self.path, 0o600)
        except OSError:
            pass

    def init(self) -> None:
        data = self._load()
        if not data.get("salt"):
            data["salt"] = _rand(32)
            self._save(data)
        else:
            try:
                import os
                os.chmod(self.path, 0o600)
            except OSError:
                pass

    def issue(self, host: str, harness: str, role: str,
              scopes: list[str] | None = None, ttl_days: int = 365) -> tuple[str, dict]:
        data = self._load()
        if not data.get("salt"):
            data["salt"] = _rand(32)
        scopes = [s for s in (scopes or ["read", "write"]) if s in SCOPES] or ["read", "write"]
        token = "bk_" + _rand(24)
        rec = {
            "id": _rand(8), "prefix": token[:10],
            "digest": hashlib.sha256((data["salt"] + token).encode()).hexdigest(),
            "author": {"host": host, "harness": harness, "role": role},
            "scopes": scopes, "created": now_iso(),
            "expires": None if ttl_days <= 0 else
            (__import__("datetime").datetime.now().astimezone()
             + __import__("datetime").timedelta(days=ttl_days)).isoformat(timespec="seconds"),
            "revoked": False,
        }
        data["tokens"].append(rec)
        self._save(data)
        return token, rec

    def revoke(self, id_or_prefix: str) -> bool:
        data = self._load()
        hit = False
        for t in data["tokens"]:
            if t["id"] == id_or_prefix or t["prefix"] == id_or_prefix:
                t["revoked"] = True
                hit = True
        if hit:
            self._save(data)
        return hit

    def list(self) -> list[dict]:
        return [{k: v for k, v in t.items() if k != "digest"} for t in self._load()["tokens"]]

    def verify(self, token: str) -> tuple[dict | None, str | None]:
        """返回 (record, error_code)。"""
        if not token or not token.startswith("bk_"):
            return None, "unauthorized"
        data = self._load()
        cand = hashlib.sha256((data.get("salt", "") + token).encode()).hexdigest()
        for t in data["tokens"]:
            if t.get("revoked"):
                continue
            if hmac.compare_digest(t["digest"], cand):
                exp = t.get("expires")
                if exp:
                    import datetime as dt
                    try:
                        if dt.datetime.fromisoformat(exp) < dt.datetime.now().astimezone():
                            return None, "token_expired"
                    except ValueError:
                        pass
                return t, None
        return None, "unauthorized"
