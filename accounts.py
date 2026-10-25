"""Private Gemini accounts, exclusive leases and persistent failover state."""

import asyncio
import json
import time
import uuid
from dataclasses import dataclass, field

from schemas import CredentialsRequest
from storage import GatewayError, atomic_json


@dataclass
class Account:
    id: str
    name: str
    credentials: CredentialsRequest = field(repr=False)
    enabled: bool = True
    cooldown_until: float = 0
    error: dict | None = None
    quota_hits: int = 0
    successes: int = 0
    failures: int = 0
    last_used: float = 0
    backend: object = field(default=None, repr=False)
    busy: bool = False
    connecting: bool = False
    models: list = field(default_factory=list)

    def public(self):
        state = (
            "disabled"
            if not self.enabled
            else "busy"
            if self.busy
            else "connecting"
            if self.connecting
            else "login_required"
            if self.error and self.error["code"] == "authentication_failed"
            else "cooldown"
            if self.cooldown_until > time.time()
            else "ready"
            if self.backend and self.backend.ready
            else "configured"
        )
        return {
            "id": self.id,
            "name": self.name,
            "enabled": self.enabled,
            "state": state,
            "cooldown_until": self.cooldown_until,
            "error": self.error,
            "successes": self.successes,
            "failures": self.failures,
            "last_used": self.last_used,
            "models": self.models,
        }


class AccountPool:
    def __init__(self, settings, factory, legacy_credentials=None):
        self.settings, self.factory = settings, factory
        self.path = settings.state_dir / "accounts.json"
        self.items = {}
        self.condition = asyncio.Condition()
        self.waiters = {}
        if self.path.exists():
            cleared_quota_cooldown = False
            for row in json.loads(self.path.read_text()):
                account = Account(
                    id=row["id"],
                    name=row["name"],
                    credentials=CredentialsRequest.model_validate(row["credentials"]),
                    **{
                        k: row[k]
                        for k in (
                            "enabled",
                            "cooldown_until",
                            "error",
                            "quota_hits",
                            "successes",
                            "failures",
                            "last_used",
                        )
                        if k in row
                    },
                )
                if (
                    account.error
                    and account.error.get("code") == "quota_exceeded"
                    and account.cooldown_until
                ):
                    account.cooldown_until = 0
                    cleared_quota_cooldown = True
                self.items[account.id] = account
            if cleared_quota_cooldown:
                self.persist()
        elif legacy_credentials:
            self.items["primary"] = Account(
                "primary", "Gemini 账号 1", legacy_credentials
            )
            self.persist()

    def persist(self):
        atomic_json(
            self.path,
            [
                {
                    "id": a.id,
                    "name": a.name,
                    "credentials": a.credentials.model_dump(),
                    "enabled": a.enabled,
                    "cooldown_until": a.cooldown_until,
                    "error": a.error,
                    "quota_hits": a.quota_hits,
                    "successes": a.successes,
                    "failures": a.failures,
                    "last_used": a.last_used,
                }
                for a in self.items.values()
            ],
        )

    def get(self, account_id):
        if account_id not in self.items:
            raise GatewayError("account_not_found", "没有此账号。", 404)
        return self.items[account_id]

    def require_configured(self):
        if not any(a.enabled for a in self.items.values()):
            raise GatewayError(
                "not_configured", "请先在 Gemini 账号页添加并启用账号。", 503
            )

    async def wake(self):
        async with self.condition:
            self.condition.notify_all()

    async def save(self, account_id, name, credentials=None, enabled=True):
        async with self.condition:
            previous = self.items.get(account_id)
            if previous and (previous.busy or previous.connecting):
                raise GatewayError("busy", "此账号正在处理任务，请完成后再修改。", 409)
            if not credentials and not previous:
                raise GatewayError(
                    "credentials_required", "新账号需要填写 Cookie。", 422
                )
            credentials = credentials or previous.credentials
            if any(
                a.id != account_id
                and a.credentials.secure_1psid == credentials.secure_1psid
                for a in self.items.values()
            ):
                raise GatewayError(
                    "duplicate_account",
                    "这份 Cookie 已用于另一个账号，请填写第二个账号的 Cookie。",
                    409,
                )
            account = Account(account_id, name, credentials, enabled=enabled)
            if previous:
                account.successes, account.failures = (
                    previous.successes,
                    previous.failures,
                )
            self.items[account_id] = account
            try:
                self.persist()
            except OSError:
                if previous:
                    self.items[account_id] = previous
                else:
                    self.items.pop(account_id)
                raise GatewayError(
                    "storage_error", "账号未能保存，请检查本机存储。", 500
                ) from None
            if previous and previous.backend:
                await previous.backend.close()
            self.condition.notify_all()
            return account

    async def configure(self, body, account_id=None):
        if account_id is None:
            if len(self.items) >= 8:
                raise GatewayError("account_limit", "最多配置 8 个账号。", 422)
            account_id = "acc_" + uuid.uuid4().hex[:16]
        else:
            self.get(account_id)
        old = self.items.get(account_id)
        credentials = None
        if body.secure_1psid:
            shared_proxy = next(
                (a.credentials.proxy for a in self.items.values() if a.enabled), ""
            )
            credentials = CredentialsRequest(
                secure_1psid=body.secure_1psid,
                secure_1psidts=body.secure_1psidts,
                proxy=body.proxy or (old.credentials.proxy if old else shared_proxy),
            )
        elif not old:
            raise GatewayError("credentials_required", "请填写新账号的 Cookie。", 422)
        elif body.secure_1psidts or body.proxy:
            credentials = CredentialsRequest(
                secure_1psid=old.credentials.secure_1psid,
                secure_1psidts=body.secure_1psidts or old.credentials.secure_1psidts,
                proxy=body.proxy or old.credentials.proxy,
            )
        return await self.save(account_id, body.name, credentials, body.enabled)

    def backend_for(self, account):
        if account.backend is None:
            account.backend = self.factory(account.credentials, self.settings)
        return account.backend

    async def connect_account(self, account_id, reset=False):
        from engine import safe_error

        account = self.get(account_id)
        async with self.condition:
            if account.busy or account.connecting:
                if account.backend and account.backend.ready:
                    return account.models
                raise GatewayError("busy", "此账号正在使用中，请稍后测试。", 409)
            account.connecting = True
        try:
            account.models = await self.backend_for(account).connect()
            if reset:
                account.cooldown_until, account.error, account.quota_hits = 0, None, 0
            self.persist()
            return account.models
        except Exception as exc:  # noqa: BLE001 - redact upstream exceptions before persistence
            error = safe_error(exc)
            account.error = error.as_dict()
            self.persist()
            raise error from None
        finally:
            account.connecting = False
            await self.wake()

    async def connect(self):
        self.require_configured()
        candidates = [a for a in self.items.values() if a.enabled]
        results = await asyncio.gather(
            *(self.connect_account(a.id) for a in candidates), return_exceptions=True
        )
        models = {}
        for result in results:
            if isinstance(result, list):
                for model in result:
                    if model.get("available", True) or model["id"] not in models:
                        models[model["id"]] = model
        if not models and all(isinstance(r, Exception) for r in results):
            raise results[0]
        return list(models.values())

    async def reset(self, account_id):
        account = self.get(account_id)
        async with self.condition:
            if account.busy or account.connecting:
                raise GatewayError("busy", "此账号正在使用中。", 409)
            account.cooldown_until, account.error, account.quota_hits = 0, None, 0
            self.persist()
            self.condition.notify_all()
        return account.public()

    def _all_enabled_accounts_quota_exhausted(self, quota_accounts):
        # This set is deliberately request-local.  ``account.error`` and
        # ``quota_hits`` are persisted for display/telemetry, but they must not
        # make a fresh request skip Gemini: a quota can recover at any time.
        enabled_ids = {
            account.id for account in self.items.values() if account.enabled
        }
        return bool(enabled_ids) and enabled_ids.issubset(quota_accounts)

    async def acquire(self, job, excluded, persist_job, quota_accounts=None):
        # Do not use ``quota_accounts or set()`` here.  The caller owns this
        # mutable set for the lifetime of one request; preserving the object
        # makes it explicit that quota exhaustion is never carried to the next
        # request.
        if quota_accounts is None:
            quota_accounts = set()
        async with self.condition:
            self.waiters[job["id"]] = frozenset(excluded)
            try:
                while True:
                    if job["status"] == "cancelled":
                        raise asyncio.CancelledError
                    now = time.time()
                    enabled = [
                        a
                        for a in self.items.values()
                        if a.enabled and a.id not in excluded
                    ]
                    if self._all_enabled_accounts_quota_exhausted(quota_accounts):
                        if sum(account.enabled for account in self.items.values()) > 1:
                            raise GatewayError(
                                "all_accounts_quota_exhausted",
                                "本次请求已依次尝试所有 Gemini 账号，额度均已耗尽。",
                                429,
                            )
                        raise GatewayError(
                            "quota_exceeded",
                            "Gemini 账号额度已耗尽。",
                            429,
                        )
                    candidates = [
                        a
                        for a in enabled
                        if not (a.error and a.error["code"] == "authentication_failed")
                    ]
                    if not candidates:
                        raise GatewayError(
                            "accounts_unavailable",
                            "没有可用账号，请更新失效账号的 Cookie 或启用其他账号。",
                            503,
                        )
                    idle = [
                        a
                        for a in candidates
                        if not a.busy and not a.connecting and a.cooldown_until <= now
                    ]
                    # Keep FIFO priority only for accounts an earlier job can use.
                    for waiter_id, blocked in self.waiters.items():
                        if waiter_id == job["id"]:
                            break
                        idle = [a for a in idle if a.id in blocked]
                    if idle:
                        # Prefer the primary account whenever it is available;
                        # the next Gemini account still handles concurrent work
                        # and quota failover without serializing the pool.
                        account = idle[0]
                        account.busy, account.last_used = True, now
                        job.update(
                            status="running",
                            started_at=job.get("started_at") or now,
                            account_id=account.id,
                            account_name=account.name,
                            retry_at=None,
                            wait_reason=None,
                        )
                        return account
                    times = [
                        a.cooldown_until for a in candidates if a.cooldown_until > now
                    ]
                    if times and all(a.cooldown_until > now for a in candidates):
                        job.update(
                            status="waiting",
                            retry_at=min(times),
                            wait_reason="账号暂时受限，恢复后自动继续",
                        )
                    else:
                        job.update(
                            status="queued", retry_at=None, wait_reason="等待空闲账号"
                        )
                    persist_job()
                    # A short bounded wake also observes a cancelled job.
                    timeout = min(5, max(0.02, min(times) - now)) if times else 5
                    try:
                        await asyncio.wait_for(self.condition.wait(), timeout)
                    except TimeoutError:
                        pass
            finally:
                self.waiters.pop(job["id"], None)
                self.condition.notify_all()

    async def execute(self, job, request, operation, persist_job):
        from engine import safe_error

        excluded, transient_attempts = set(), 0
        # Quota observations belong to this one request only.  In particular,
        # do not seed this from the persisted account error/quota counters:
        # every new request must probe Gemini account 1, then account 2, so a
        # recovered allowance is used before considering Seedream fallback.
        quota_accounts: set[str] = set()
        while True:
            account = await self.acquire(
                job, excluded, persist_job, quota_accounts=quota_accounts
            )
            attempt = {
                "account_id": account.id,
                "account_name": account.name,
                "started_at": time.time(),
                "status": "running",
            }
            job.setdefault("attempts", []).append(attempt)
            try:
                persist_job()
                backend = self.backend_for(account)
                account.models = await backend.connect()
                result = await operation(backend)
                attempt.update(status="succeeded", finished_at=time.time())
                account.successes += 1
                account.error, account.cooldown_until, account.quota_hits = None, 0, 0
                return result
            except Exception as exc:  # noqa: BLE001 - redact upstream exceptions before persistence
                error = safe_error(exc)
                attempt.update(
                    status="failed", error=error.as_dict(), finished_at=time.time()
                )
                account.error = error.as_dict()
                account.failures += 1
                alternatives = [
                    a
                    for a in self.items.values()
                    if a.enabled
                    and a.id != account.id
                    and a.id not in excluded
                    and not (a.error and a.error["code"] == "authentication_failed")
                ]
                if error.code == "quota_exceeded":
                    quota_accounts.add(account.id)
                    account.quota_hits += 1
                    # Quota is checked on every new request; do not put this
                    # account into a timed cooldown or skip it on later jobs.
                    account.cooldown_until = 0
                    excluded.add(account.id)
                elif error.code == "temporarily_blocked":
                    account.quota_hits += 1
                    account.cooldown_until = time.time() + min(
                        self.settings.retry_cooldown
                        * 2 ** min(account.quota_hits - 1, 4),
                        21600,
                    )
                elif error.code == "authentication_failed" and alternatives:
                    excluded.add(account.id)
                elif error.code == "upstream_timeout":
                    attempt["result_uncertain"] = True
                    account.cooldown_until = time.time() + self.settings.retry_cooldown
                    if alternatives and transient_attempts < 3:
                        transient_attempts += 1
                        excluded.add(account.id)
                    else:
                        raise error from None
                elif (
                    error.code == "upstream_error"
                    and alternatives
                    and transient_attempts < 3
                ):
                    transient_attempts += 1
                    account.cooldown_until = time.time() + self.settings.retry_cooldown
                elif error.code == "invalid_model" and alternatives:
                    excluded.add(account.id)
                else:
                    raise error from None
            finally:
                account.busy = False
                try:
                    self.persist()
                    persist_job()
                finally:
                    await self.wake()

    async def close(self):
        await asyncio.gather(
            *(a.backend.close() for a in self.items.values() if a.backend),
            return_exceptions=True,
        )
