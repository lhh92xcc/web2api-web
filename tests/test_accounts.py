"""Account concurrency, quota failover, persistence and redaction."""

import asyncio
import sys
import time
from collections import defaultdict
from pathlib import Path
from typing import ClassVar

import pytest
from fastapi.testclient import TestClient

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import engine as engine_module
from engine import Settings
from server import create_app
from storage import GatewayError
from test_gateway import PNG


@pytest.fixture
def harness(tmp_path):
    class Backend:
        calls: ClassVar[list] = []
        active: ClassVar[dict] = defaultdict(int)
        maximum: ClassVar[dict] = defaultdict(int)
        total_max = 0
        errors: ClassVar[dict] = defaultdict(list)
        delay_by_token: ClassVar[dict] = {}
        delay = 0.06

        def __init__(self, credentials, settings):
            self.token = credentials.secure_1psid
            self.ready = False

        async def connect(self):
            self.ready = True
            return [{"id": "gemini-pro", "available": True}]

        async def generate(
            self, request, folder, remaining, reference_paths=None, on_text=None
        ):
            cls = type(self)
            cls.calls.append(
                (
                    self.token,
                    request.prompt,
                    [p.read_bytes() for p in reference_paths or []],
                )
            )
            cls.active[self.token] += 1
            cls.maximum[self.token] = max(
                cls.maximum[self.token], cls.active[self.token]
            )
            cls.total_max = max(cls.total_max, sum(cls.active.values()))
            try:
                await asyncio.sleep(cls.delay_by_token.get(self.token, cls.delay))
                if cls.errors[self.token]:
                    error = cls.errors[self.token].pop(0)
                    if error:
                        raise error
                path = folder / (self.token + str(len(cls.calls)) + ".png")
                path.write_bytes(PNG)
                return [path]
            finally:
                cls.active[self.token] -= 1

        async def chat(self, request, reference_paths=None):
            cls = type(self)
            cls.calls.append((self.token, "chat", []))
            if cls.errors[self.token]:
                raise cls.errors[self.token].pop(0)
            return "synthetic answer"

        async def close(self):
            self.ready = False

    settings = Settings(tmp_path / "state", tmp_path / "images", retry_cooldown=0.1)
    with TestClient(create_app(settings, Backend)) as client:
        key = client.post("/api/keys", json={"name": "pool-tests"}).json()["key"]
        client.headers["Authorization"] = "Bearer " + key
        yield client, Backend, settings


def add(client, token, name=None):
    response = client.post(
        "/api/accounts", json={"name": name or token, "secure_1psid": token}
    )
    assert response.status_code == 201, response.text
    return response.json()["id"]


def wait(client, job_id, state=None):
    deadline = time.monotonic() + 5
    while time.monotonic() < deadline:
        job = client.get("/api/jobs/" + job_id).json()
        if job["status"] in (
            {state} if state else {"succeeded", "failed", "cancelled"}
        ):
            return job
        time.sleep(0.01)
    pytest.fail("task did not reach expected state")


def test_two_accounts_parallel_but_each_account_exclusive(harness):
    c, b, _ = harness
    add(c, "first")
    add(c, "second")
    jobs = [c.post("/v1/jobs", json={"prompt": str(i)}).json() for i in range(6)]
    for j in jobs:
        assert wait(c, j["id"])["status"] == "succeeded"
    assert b.total_max == 2
    assert dict(b.maximum) == {"first": 1, "second": 1}
    assert {row[0] for row in b.calls} == {"first", "second"}


def test_health_exposes_two_worker_gemini_first_policy(harness):
    c, _, _ = harness

    health = c.get("/health").json()
    assert health["max_concurrency"] == 2
    assert health["routing"] == {
        "gemini_first": True,
        "gemini_probe_each_request": True,
        "seedream_fallback_only_after_all_gemini_quota": True,
    }
    policy = c.get("/api/accounts").json()["policy"]
    assert policy["parallel_accounts"] == 2
    assert policy["max_concurrency"] == 2
    assert policy["probe_gemini_on_each_request"] is True


def test_quota_switches_same_job_and_idempotency_does_not_resubmit(harness):
    c, b, _ = harness
    first = add(c, "first")
    second = add(c, "second")
    b.errors["first"] = [GatewayError("quota_exceeded", "synthetic quota", 429)]
    headers = {"Idempotency-Key": "single-business-operation"}
    r = c.post(
        "/v1/images/generations", json={"prompt": "cup", "n": 2}, headers=headers
    )
    assert r.status_code == 200, r.text
    j = c.get("/v1/jobs/" + r.json()["job_id"]).json()
    assert [a["account_id"] for a in j["attempts"]] == [first, second, first]
    assert len(j["images"]) == 2
    assert (
        c.post(
            "/v1/images/generations", json={"prompt": "cup", "n": 2}, headers=headers
        ).json()["job_id"]
        == j["id"]
    )
    assert len(b.calls) == 3
    accounts = c.get("/api/accounts").json()["data"]
    assert accounts[0]["state"] == "ready"
    assert accounts[0]["cooldown_until"] == 0


def test_quota_failover_waits_for_busy_alternate_account(harness):
    c, b, _ = harness
    first_id = add(c, "first")
    second_id = add(c, "second")
    b.delay_by_token = {"first": 0.2, "second": 0.7}
    b.errors["first"] = [GatewayError("quota_exceeded", "synthetic quota", 429)]
    first_job = c.post("/v1/jobs", json={"prompt": "quota then failover"}).json()
    second_job = c.post("/v1/jobs", json={"prompt": "already using alternate"}).json()

    deadline = time.monotonic() + 3
    while time.monotonic() < deadline:
        states = {
            account["id"]: account["state"]
            for account in c.get("/api/accounts").json()["data"]
        }
        if states[first_id] == "ready" and states[second_id] == "busy":
            break
        time.sleep(0.01)
    assert states[first_id] == "ready" and states[second_id] == "busy"
    assert c.get("/api/jobs/" + first_job["id"]).json()["status"] == "queued"

    first_result = wait(c, first_job["id"])
    second_result = wait(c, second_job["id"])

    assert first_result["status"] == "succeeded"
    assert [attempt["account_name"] for attempt in first_result["attempts"]] == [
        "first",
        "second",
    ]
    assert second_result["status"] == "succeeded"


def test_both_quotas_fail_fast_after_trying_each_account(harness):
    c, b, _ = harness
    add(c, "first")
    add(c, "second")
    for name in ["first", "second"]:
        b.errors[name] = [GatewayError("quota_exceeded", "quota", 429)]
    response = c.post(
        "/v1/images/generations",
        json={"prompt": "cup"},
        headers={"Idempotency-Key": "both-quota"},
    )
    assert response.status_code == 429
    assert response.json()["error"]["code"] == "seedream_not_configured"
    job = c.get("/v1/jobs/" + response.json()["error"]["job_id"]).json()
    assert job["status"] == "failed"
    assert [attempt["status"] for attempt in job["attempts"]] == ["failed", "failed"]
    assert len(b.calls) == 2

    for name in ["first", "second"]:
        b.errors[name] = [GatewayError("quota_exceeded", "quota", 429)]
    second_request = c.post(
        "/v1/images/generations",
        json={"prompt": "cup again"},
        headers={"Idempotency-Key": "both-quota-repeat"},
    )
    assert second_request.status_code == 429
    assert second_request.json()["error"]["code"] == "seedream_not_configured"
    assert len(b.calls) == 4


def test_both_quotas_fail_fast_for_text_requests(harness):
    c, b, _ = harness
    add(c, "first")
    add(c, "second")
    for name in ["first", "second"]:
        b.errors[name] = [GatewayError("quota_exceeded", "quota", 429)]
    response = c.post(
        "/v1/chat/completions",
        json={"messages": [{"role": "user", "content": "hello"}]},
    )
    assert response.status_code == 429
    assert response.json()["error"]["code"] == "all_accounts_quota_exhausted"
    assert len(b.calls) == 2


def test_seedream_runs_only_after_both_gemini_quotas(harness, monkeypatch):
    c, backend, settings = harness

    class FakeSeedream:
        calls: ClassVar[list] = []

        def __init__(self, api_key, timeout):
            self.api_key = api_key

        async def generate(
            self, request, folder, remaining, reference_paths, on_billed_images=None
        ):
            type(self).calls.append((self.api_key, request.prompt, remaining))
            path = folder / "seedream-test.png"
            path.write_bytes(PNG)
            if on_billed_images:
                on_billed_images(1)
            return [path]

    monkeypatch.setattr(engine_module, "SeedreamBackend", FakeSeedream)
    add(c, "first")
    add(c, "second")
    for name in ["first", "second"]:
        backend.errors[name] = [GatewayError("quota_exceeded", "quota", 429)]
    secret = "ark-test-key-never-return-this"
    saved = c.put("/api/seedream", json={"api_key": secret})
    assert saved.status_code == 200
    assert secret not in saved.text
    assert secret not in c.get("/health").text
    assert (settings.state_dir / "seedream.json").stat().st_mode & 0o777 == 0o600

    response = c.post("/v1/jobs", json={"prompt": "fallback image"})
    job = wait(c, response.json()["id"])
    assert job["status"] == "succeeded"
    assert job["fallback_provider"] == "seedream"
    assert job["billing"]["generated_images"] == 1
    assert job["billing"]["estimated_amount_cny"] == 0.12
    assert [attempt.get("provider", "gemini") for attempt in job["attempts"]] == [
        "gemini",
        "gemini",
        "seedream",
    ]
    assert [row[0] for row in backend.calls] == ["first", "second"]
    assert FakeSeedream.calls == [(secret, "fallback image", 1)]


def test_new_request_retries_gemini_before_reusing_seedream_fallback(
    harness, monkeypatch
):
    c, backend, _ = harness

    class FakeSeedream:
        calls = 0

        def __init__(self, api_key, timeout):
            pass

        async def generate(
            self, request, folder, remaining, reference_paths, on_billed_images=None
        ):
            type(self).calls += 1
            path = folder / "seedream-recovery-test.png"
            path.write_bytes(PNG)
            if on_billed_images:
                on_billed_images(1)
            return [path]

    monkeypatch.setattr(engine_module, "SeedreamBackend", FakeSeedream)
    first_id = add(c, "first")
    add(c, "second")
    backend.errors["first"] = [GatewayError("quota_exceeded", "quota", 429), None]
    backend.errors["second"] = [GatewayError("quota_exceeded", "quota", 429)]
    c.put("/api/seedream", json={"api_key": "ark-test-key"})

    fallback_job = c.post("/v1/jobs", json={"prompt": "first request"}).json()
    fallback_result = wait(c, fallback_job["id"])
    assert [attempt.get("provider", "gemini") for attempt in fallback_result["attempts"]] == [
        "gemini",
        "gemini",
        "seedream",
    ]
    assert fallback_result["fallback_provider"] == "seedream"
    assert FakeSeedream.calls == 1

    recovered_job = c.post("/v1/jobs", json={"prompt": "next request"}).json()
    recovered_result = wait(c, recovered_job["id"])

    assert recovered_result["status"] == "succeeded"
    assert [attempt["account_id"] for attempt in recovered_result["attempts"]] == [
        first_id
    ]
    assert "fallback_provider" not in recovered_result
    assert "billing" not in recovered_result
    assert FakeSeedream.calls == 1
    assert [(call[0], call[1]) for call in backend.calls] == [
        ("first", "first request"),
        ("second", "first request"),
        ("first", "next request"),
    ]


def test_each_new_request_probes_quota_accounts_before_seedream(harness, monkeypatch):
    c, backend, _ = harness

    class FakeSeedream:
        calls = 0

        def __init__(self, api_key, timeout):
            pass

        async def generate(
            self, request, folder, remaining, reference_paths, on_billed_images=None
        ):
            type(self).calls += 1
            path = folder / "fresh-probe-seedream.png"
            path.write_bytes(PNG)
            if on_billed_images:
                on_billed_images(1)
            return [path]

    monkeypatch.setattr(engine_module, "SeedreamBackend", FakeSeedream)
    add(c, "first")
    add(c, "second")
    c.put("/api/seedream", json={"api_key": "ark-test-key"})

    # Both accounts are exhausted for the first request.  On the next request
    # account 1 is still exhausted but account 2 has recovered.  Persisted
    # quota errors must not short-circuit the fresh Gemini probe.
    backend.errors["first"] = [
        GatewayError("quota_exceeded", "quota", 429),
        GatewayError("quota_exceeded", "quota", 429),
    ]
    backend.errors["second"] = [
        GatewayError("quota_exceeded", "quota", 429),
        None,
    ]

    first = c.post("/v1/jobs", json={"prompt": "exhausted"}).json()
    assert wait(c, first["id"])["fallback_provider"] == "seedream"

    second = c.post("/v1/jobs", json={"prompt": "recovered"}).json()
    result = wait(c, second["id"])
    assert result["status"] == "succeeded"
    assert result["attempts"][0]["account_name"] == "first"
    assert result["attempts"][0]["error"]["code"] == "quota_exceeded"
    assert result["attempts"][1]["account_name"] == "second"
    assert "fallback_provider" not in result
    assert FakeSeedream.calls == 1


def test_legacy_seedream_job_estimates_cost_from_saved_seedream_images():
    service = engine_module.JobService.__new__(engine_module.JobService)
    job = {
        "fallback_provider": "seedream",
        "images": ["img_1/gemini-image.png", "img_1/seedream-a.png", "img_1/seedream-b.png"],
    }

    service._restore_seedream_billing(job)

    assert job["billing"]["generated_images"] == 2
    assert job["billing"]["estimated_amount_cny"] == 0.24
    assert job["billing"]["estimate_source"] == "legacy_saved_outputs"


def test_seedream_does_not_run_for_non_quota_gemini_errors(harness, monkeypatch):
    c, backend, _ = harness

    class FakeSeedream:
        calls = 0

        def __init__(self, api_key, timeout):
            pass

        async def generate(self, request, folder, remaining, reference_paths):
            type(self).calls += 1
            path = folder / "unexpected-seedream.png"
            path.write_bytes(PNG)
            return [path]

    monkeypatch.setattr(engine_module, "SeedreamBackend", FakeSeedream)
    add(c, "first")
    add(c, "second")
    c.put("/api/seedream", json={"api_key": "ark-test-key"})
    backend.errors["first"] = [GatewayError("quota_exceeded", "quota", 429)]
    backend.errors["second"] = [
        GatewayError("authentication_failed", "login required", 401)
    ]

    response = c.post("/v1/jobs", json={"prompt": "do not spend"})
    job = wait(c, response.json()["id"])
    assert job["status"] == "failed"
    assert job["error"]["code"] == "authentication_failed"
    assert FakeSeedream.calls == 0


def test_partial_seedream_result_is_kept_when_followup_fails(harness, monkeypatch):
    c, backend, _ = harness

    class PartialSeedream:
        def __init__(self, api_key, timeout):
            pass

        async def generate(
            self, request, folder, remaining, reference_paths, on_billed_images=None
        ):
            path = folder / "partial-seedream.png"
            path.write_bytes(PNG)
            if on_billed_images:
                on_billed_images(1)
            error = GatewayError("seedream_rate_limited", "rate limited", 502)
            error.partial_paths = [path]
            raise error

    monkeypatch.setattr(engine_module, "SeedreamBackend", PartialSeedream)
    add(c, "first")
    add(c, "second")
    for name in ["first", "second"]:
        backend.errors[name] = [GatewayError("quota_exceeded", "quota", 429)]
    c.put("/api/seedream", json={"api_key": "ark-test-key"})

    submitted = c.post("/v1/jobs", json={"prompt": "two images", "n": 2}).json()
    result = wait(c, submitted["id"])

    assert result["status"] == "failed"
    assert result["error"]["code"] == "seedream_rate_limited"
    assert len(result["images"]) == 1
    assert result["billing"]["generated_images"] == 1
    assert result["billing"]["estimated_amount_cny"] == 0.12
    image = c.get("/images/" + result["images"][0])
    assert image.status_code == 200
    assert image.content == PNG


def test_seedream_fallback_keeps_service_concurrency_at_two(harness, monkeypatch):
    c, backend, _ = harness

    class FakeSeedream:
        active = 0
        maximum = 0

        def __init__(self, api_key, timeout):
            pass

        async def generate(
            self, request, folder, remaining, reference_paths, on_billed_images=None
        ):
            cls = type(self)
            cls.active += 1
            cls.maximum = max(cls.maximum, cls.active)
            try:
                await asyncio.sleep(0.08)
                path = folder / "seedream-concurrency.png"
                path.write_bytes(PNG)
                if on_billed_images:
                    on_billed_images(1)
                return [path]
            finally:
                cls.active -= 1

    monkeypatch.setattr(engine_module, "SeedreamBackend", FakeSeedream)
    add(c, "first")
    add(c, "second")
    for name in ["first", "second"]:
        backend.errors[name] = [
            GatewayError("quota_exceeded", "quota", 429) for _ in range(8)
        ]
    c.put("/api/seedream", json={"api_key": "ark-test-key"})

    warmup = c.post("/v1/jobs", json={"prompt": "warm up quota state"}).json()
    assert wait(c, warmup["id"])["status"] == "succeeded"
    jobs = [
        c.post("/v1/jobs", json={"prompt": f"fallback {index}"}).json()
        for index in range(2)
    ]
    results = [wait(c, job["id"]) for job in jobs]

    assert all(job["status"] == "succeeded" for job in results), [
        (
            job["status"],
            job.get("error"),
            [
                (
                    attempt.get("account_name"),
                    attempt.get("status"),
                    attempt.get("error"),
                )
                for attempt in job["attempts"]
            ],
        )
        for job in results
    ]
    assert FakeSeedream.maximum == 2


def test_single_account_quota_fails_without_wait_or_duplicate_attempt(harness):
    c, b, _ = harness
    add(c, "first")
    b.errors["first"] = [GatewayError("quota_exceeded", "quota", 429)]
    j = c.post("/v1/jobs", json={"prompt": "cup"}).json()
    result = wait(c, j["id"])
    assert result["status"] == "failed"
    assert result["error"]["code"] == "quota_exceeded"
    assert len(b.calls) == 1
    account = c.get("/api/accounts").json()["data"][0]
    assert account["cooldown_until"] == 0


def test_waiter_does_not_block_account_it_cannot_use(harness):
    c, b, _ = harness
    first = add(c, "first")
    add(c, "second")
    b.errors["first"] = [GatewayError("invalid_model", "model unavailable", 400)]
    b.errors["second"] = [GatewayError("quota_exceeded", "quota", 429)]
    older = c.post(
        "/v1/jobs", json={"prompt": "needs a special model", "model": "special-model"}
    ).json()
    result = wait(c, older["id"])
    assert result["status"] == "failed"
    assert result["error"]["code"] == "accounts_unavailable"
    assert [attempt["account_id"] for attempt in result["attempts"]] == [
        first,
        next(
            account["id"]
            for account in c.get("/api/accounts").json()["data"]
            if account["id"] != first
        ),
    ]


def test_quota_exhaustion_fails_without_cooldown_wait(harness):
    c, b, _ = harness
    add(c, "first")
    b.errors["first"] = [GatewayError("quota_exceeded", "quota", 429)]
    result = c.post(
        "/v1/batches", json={"items": [{"custom_id": "one", "prompt": "cup"}]}
    ).json()
    job_id = result["items"][0]["job_id"]
    job = wait(c, job_id)
    batch = c.get("/v1/batches/" + result["id"]).json()
    assert job["status"] == "failed"
    assert job["error"]["code"] == "quota_exceeded"
    assert batch["status"] == "failed" and batch["counts"]["failed"] == 1


def test_auth_and_network_errors_fail_over_and_preserve_model(harness):
    c, b, _ = harness
    add(c, "first")
    add(c, "second")
    b.errors["first"] = [GatewayError("authentication_failed", "expired", 401)]
    j = c.post("/v1/jobs", json={"prompt": "cup", "model": "gemini-pro"}).json()
    assert wait(c, j["id"])["status"] == "succeeded"
    assert c.get("/api/accounts").json()["data"][0]["state"] == "login_required"
    first = c.get("/api/accounts").json()["data"][0]["id"]
    c.post("/api/accounts/" + first + "/retry")
    b.errors["first"] = [RuntimeError("private-upstream-text")]
    j = c.post("/v1/jobs", json={"prompt": "another"}).json()
    assert wait(c, j["id"])["status"] == "succeeded"
    assert "private-upstream-text" not in c.get("/api/jobs").text


def test_partial_results_reused_on_quota_and_references_forwarded(harness):
    c, b, _ = harness
    add(c, "first")
    add(c, "second")
    b.errors["first"] = [None, GatewayError("quota_exceeded", "quota", 429)]
    # Both accounts get the same project-owned reference bytes.
    import io

    from PIL import Image

    data = io.BytesIO()
    Image.new("RGB", (64, 64), "white").save(data, format="PNG")
    reference = data.getvalue()
    response = c.post(
        "/v1/references", files={"files": ("ref.png", reference, "image/png")}
    )
    assert response.status_code == 201, response.text
    ref = response.json()["data"][0]["id"]
    r = c.post(
        "/v1/images/generations",
        json={"prompt": "cup", "n": 3, "reference_images": [ref]},
    )
    assert r.status_code == 200, r.text
    assert len(r.json()["data"]) == 3
    assert all(row[2] == [reference] for row in b.calls)


def test_accounts_persist_private_and_legacy_migrates(harness):
    c, b, settings = harness
    assert (
        c.post("/api/config", json={"secure_1psid": "legacy-cookie"}).status_code == 200
    )
    add(c, "second-secret", "Pro 2")
    assert len(c.get("/api/accounts").json()["data"]) == 2
    for path in ["/health", "/api/accounts", "/api/providers"]:
        assert "second-secret" not in c.get(path).text
        assert "legacy-cookie" not in c.get(path).text
    assert settings.state_dir.joinpath("accounts.json").stat().st_mode & 0o777 == 0o600
    from accounts import AccountPool

    pool = AccountPool(settings, b)
    assert len(pool.items) == 2
    assert pool.items["primary"].credentials.secure_1psid == "legacy-cookie"


def test_duplicate_credentials_and_removed_chatgpt_are_rejected(harness):
    c, _, _ = harness
    add(c, "first")
    assert (
        c.post(
            "/api/accounts", json={"name": "duplicate", "secure_1psid": "first"}
        ).status_code
        == 409
    )
    assert "chatgpt" not in [p["id"] for p in c.get("/api/providers").json()["data"]]
    assert (
        c.post("/v1/jobs", json={"provider": "chatgpt", "prompt": "draw"}).status_code
        == 404
    )
    r = c.post("/api/accounts", json={"name": "bad", "secure_1psid": "secret;invalid"})
    assert r.status_code == 422 and "secret;invalid" not in r.text


def test_chat_uses_pool_failover(harness):
    c, b, _ = harness
    add(c, "first")
    add(c, "second")
    b.errors["first"] = [GatewayError("quota_exceeded", "quota", 429)]
    r = c.post(
        "/v1/chat/completions",
        json={"messages": [{"role": "user", "content": "hello"}]},
    )
    assert (
        r.status_code == 200
        and r.json()["choices"][0]["message"]["content"] == "synthetic answer"
    )
    assert len(b.calls) == 2


def test_legacy_file_migration_and_cooldowns_survive_restart(harness, tmp_path):
    import json

    _c, backend, _ = harness
    settings = Settings(tmp_path / "migration", tmp_path / "migration-images")
    settings.state_dir.mkdir()
    (settings.state_dir / "credentials.json").write_text(
        json.dumps({"secure_1psid": "old-login"})
    )
    with TestClient(create_app(settings, backend)) as http:
        accounts = http.get("/api/accounts").json()["data"]
        assert accounts[0]["id"] == "primary"
        assert http.get("/health").json()["configured"] is True
        pool = http.app.state.service.accounts
        pool.items["primary"].cooldown_until = time.time() + 60
        pool.items["primary"].error = GatewayError(
            "temporarily_blocked", "temporary limit", 429
        ).as_dict()
        pool.persist()
        job = http.post("/api/jobs", json={"prompt": "later"}).json()
        wait(http, job["id"], "waiting")
    before = len(backend.calls)
    with TestClient(create_app(settings, backend)) as reopened:
        assert reopened.get("/api/accounts").json()["data"][0]["state"] == "cooldown"
        assert wait(reopened, job["id"], "waiting")["finished_at"] is None
        assert len(backend.calls) == before
        reopened.post("/api/accounts/primary/retry")
        assert wait(reopened, job["id"])["status"] == "succeeded"


def test_edit_does_not_touch_other_account_or_reveal_proxy(harness):
    c, _, _ = harness
    first = c.post(
        "/api/accounts",
        json={
            "name": "Pro 1",
            "secure_1psid": "first",
            "proxy": "http://user:proxy-secret@127.0.0.1:7890",
        },
    ).json()["id"]
    second = add(c, "second", "Pro 2")
    pool = c.app.state.service.accounts
    assert pool.items[second].credentials.proxy == pool.items[first].credentials.proxy
    assert (
        c.put(
            "/api/accounts/" + second, json={"name": "Pro 2 renamed", "enabled": False}
        ).status_code
        == 200
    )
    assert pool.items[second].credentials.secure_1psid == "second"
    assert pool.items[first].enabled
    assert "proxy-secret" not in c.get("/api/accounts").text


def test_transient_retries_are_bounded_and_download_does_not_regenerate(harness):
    c, b, settings = harness
    add(c, "first")
    add(c, "second")
    settings.retry_cooldown = 0.01
    for token in ["first", "second"]:
        b.errors[token] = [RuntimeError("private-network-error")] * 5
    job = c.post("/v1/jobs", json={"prompt": "network"}).json()
    result = wait(c, job["id"])
    assert result["status"] == "failed" and len(b.calls) == 4
    assert "private-network-error" not in str(result)
    for token in ["first", "second"]:
        b.errors[token] = [GatewayError("download_failed", "download failed")] * 5
    before = len(b.calls)
    job = c.post("/v1/jobs", json={"prompt": "download"}).json()
    assert wait(c, job["id"])["error"]["code"] == "download_failed"
    assert len(b.calls) == before + 1


def test_uncertain_timeout_does_not_resubmit_to_same_account_without_alternative(
    harness,
):
    c, b, _ = harness
    add(c, "first")
    second = add(c, "second")
    pool = c.app.state.service.accounts
    pool.items[second].error = {
        "code": "authentication_failed",
        "message": "login required",
        "status": 401,
    }
    pool.persist()
    b.errors["first"] = [TimeoutError("private-timeout")]

    submitted = c.post("/v1/jobs", json={"prompt": "maybe already generated"}).json()
    result = wait(c, submitted["id"])

    assert result["status"] == "failed"
    assert result["error"]["code"] == "upstream_timeout"
    assert result["attempts"][0]["result_uncertain"] is True
    assert len(result["attempts"]) == 1
    assert len(b.calls) == 1


def test_uncertain_timeout_switches_to_another_account(harness):
    c, b, settings = harness
    first = add(c, "first")
    second = add(c, "second")
    settings.retry_cooldown = 0.01
    b.errors["first"] = [TimeoutError("private-timeout")]

    submitted = c.post("/v1/jobs", json={"prompt": "maybe already generated"}).json()
    result = wait(c, submitted["id"])

    assert result["status"] == "succeeded"
    assert [attempt["account_id"] for attempt in result["attempts"]] == [first, second]
    assert result["attempts"][0]["result_uncertain"] is True
    assert [row[0] for row in b.calls] == ["first", "second"]
