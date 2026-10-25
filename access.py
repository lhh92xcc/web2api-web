"""Per-project API keys; only digests are persisted (AGPL-3.0-or-later)."""

import hashlib
import hmac
import json
import secrets
import time
import uuid
from pathlib import Path

from storage import GatewayError, atomic_json


class KeyStore:
    def __init__(self, state_dir: Path):
        self.path = state_dir / "api-keys.json"
        self.keys = json.loads(self.path.read_text()) if self.path.exists() else []

    @staticmethod
    def public(key):
        return {field: value for field, value in key.items() if field != "digest"}

    def list(self):
        return [self.public(key) for key in reversed(self.keys)]

    def create(self, name: str):
        if sum(key["enabled"] for key in self.keys) >= 50:
            raise GatewayError(
                "too_many_keys", "最多保留 50 个有效 Key，请先停用不用的 Key。", 409
            )
        token = "gw_" + secrets.token_urlsafe(32)
        key = {
            "id": "key_" + uuid.uuid4().hex,
            "name": name,
            "prefix": token[:10] + "…",
            "digest": hashlib.sha256(token.encode()).hexdigest(),
            "created_at": time.time(),
            "last_used_at": None,
            "enabled": True,
        }
        self.keys.append(key)
        try:
            atomic_json(self.path, self.keys)
        except OSError:
            self.keys.pop()
            raise GatewayError(
                "storage_error", "调用 Key 保存失败，请检查磁盘与权限。", 500
            ) from None
        return {**self.public(key), "key": token}

    def authenticate(self, token: str):
        digest = hashlib.sha256(token.encode()).hexdigest()
        for key in self.keys:
            if key["enabled"] and hmac.compare_digest(key["digest"], digest):
                # Persist at most once a minute rather than for every polling request.
                if time.time() - (key["last_used_at"] or 0) >= 60:
                    key["last_used_at"] = time.time()
                    atomic_json(self.path, self.keys)
                return key["id"]
        raise GatewayError(
            "invalid_api_key", "调用 Key 无效或已停用。请在本地后台创建 Key。", 401
        )

    def revoke(self, key_id: str):
        for key in self.keys:
            if key["id"] == key_id:
                key["enabled"] = False
                atomic_json(self.path, self.keys)
                return self.public(key)
        raise GatewayError("key_not_found", "调用 Key 不存在。", 404)
