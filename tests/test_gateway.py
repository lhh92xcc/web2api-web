"""Integration tests use synthetic images and never contact Google."""

import asyncio
import base64
import json
import sys
import time
from pathlib import Path
from types import SimpleNamespace
from typing import ClassVar
from unittest.mock import AsyncMock

import pytest
from fastapi.testclient import TestClient

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from engine import GeminiBackend, Settings
from schemas import ChatRequest, CredentialsRequest, ImageRequest
from server import create_app

PNG = base64.b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAwMCAO+jfS8AAAAASUVORK5CYII="
)
CREDENTIALS = {
    "secure_1psid": "test-cookie-not-real",
    "secure_1psidts": "test-ts-not-real",
    "proxy": "",
}


@pytest.fixture
def backend_type():
    class FakeBackend:
        instances: ClassVar[list] = []
        calls = 0
        active = 0
        maximum_active = 0
        reference_calls: ClassVar[list] = []
        request_calls: ClassVar[list] = []
        fail_at = None
        error = None
        delay = 0.01
        stream_text = None
        text_hold = 0

        def __init__(self, credentials, settings):
            self.ready = False
            self.instances.append(self)

        async def connect(self):
            self.ready = True
            return [{"id": "gemini-pro", "available": True, "display_name": "Pro"}]

        async def generate(
            self, request, folder, remaining, reference_paths=None, on_text=None
        ):
            cls = type(self)
            cls.request_calls.append(request.model_dump())
            cls.reference_calls.append(
                [path.read_bytes() for path in reference_paths or []]
            )
            cls.calls += 1
            cls.active += 1
            cls.maximum_active = max(cls.maximum_active, cls.active)
            try:
                if cls.stream_text and on_text:
                    await on_text(cls.stream_text)
                await asyncio.sleep(cls.text_hold or cls.delay)
                if cls.fail_at == cls.calls or cls.error:
                    raise cls.error or RuntimeError("sensitive-upstream-cookie")
                path = folder / f"fake-{cls.calls}.png"
                path.write_bytes(PNG)
                return [path]
            finally:
                cls.active -= 1

        async def close(self):
            self.ready = False

    return FakeBackend


@pytest.fixture
def settings(tmp_path):
    return Settings(tmp_path / "private", tmp_path / "images", timeout=30)


@pytest.fixture
def client(settings, backend_type):
    with TestClient(create_app(settings, backend_type)) as http:
        key = http.post("/api/keys", json={"name": "integration-tests"}).json()
        http.headers["Authorization"] = "Bearer " + key["key"]
        yield http


def configure(client):
    assert client.post("/api/config", json=CREDENTIALS).status_code == 200


def wait_job(client, job_id):
    deadline = time.monotonic() + 5
    while time.monotonic() < deadline:
        job = client.get(f"/api/jobs/{job_id}").json()
        if job["status"] in {"succeeded", "failed", "cancelled"}:
            return job
        time.sleep(0.01)
    pytest.fail("Job did not finish")


def test_unconfigured_is_safe_and_does_not_connect(client, backend_type):
    state = client.get("/health").json()
    assert state["status"] == "ok" and not state["configured"]
    response = client.post("/v1/images/generations", json={"prompt": "cat"})
    assert response.status_code == 503
    assert response.json()["error"]["code"] == "not_configured"
    assert backend_type.instances == []
    assert client.get("/").status_code == 200


def test_config_does_not_return_or_log_secrets(client, settings, caplog):
    response = client.post("/api/config", json=CREDENTIALS)
    assert response.status_code == 200
    assert "test-cookie" not in response.text
    assert "test-cookie" not in client.get("/health").text
    assert "test-cookie" not in caplog.text
    assert settings.state_dir.stat().st_mode & 0o777 == 0o700
    assert (settings.state_dir / "credentials.json").stat().st_mode & 0o777 == 0o600


def test_invalid_credentials_are_redacted(client):
    secret = "secret-cookie; never-echo-this"
    response = client.post("/api/config", json={"secure_1psid": secret})
    assert response.status_code == 422
    assert secret not in response.text
    assert "never-echo" not in response.text
    assert (
        client.post(
            "/api/config", json={**CREDENTIALS, "proxy": "file:///etc/passwd"}
        ).status_code
        == 422
    )


def test_generation_returns_downloadable_real_image(client, backend_type):
    configure(client)
    response = client.post("/v1/images/generations", json={"prompt": "cat", "n": 2})
    assert response.status_code == 200, response.text
    result = response.json()
    assert len(result["data"]) == 2
    assert backend_type.calls == 2
    for image in result["data"]:
        download = client.get(image["url"])
        assert download.status_code == 200
        assert download.headers["content-type"] == "image/png"
        assert download.content == PNG


def test_maximum_four_image_request_returns_four_serial_results(client, backend_type):
    configure(client)
    response = client.post("/v1/images/generations", json={"prompt": "cat", "n": 4})
    assert response.status_code == 200, response.text
    assert len(response.json()["data"]) == 4
    assert backend_type.calls == 4
    assert backend_type.maximum_active == 1


def test_base64_and_idempotency_survive_restart(settings, backend_type):
    request = {"prompt": "cat", "response_format": "b64_json"}
    headers = {"Idempotency-Key": "single-operation"}
    token = None
    for attempt in range(2):
        with TestClient(create_app(settings, backend_type)) as http:
            if attempt == 0:
                configure(http)
                token = http.post(
                    "/api/keys", json={"name": "persistent-project"}
                ).json()["key"]
            http.headers["Authorization"] = "Bearer " + token
            response = http.post(
                "/v1/images/generations", json=request, headers=headers
            )
            assert response.status_code == 200
            assert base64.b64decode(response.json()["data"][0]["b64_json"]) == PNG
            conflict = http.post(
                "/v1/jobs", json={"prompt": "another"}, headers=headers
            )
            assert conflict.status_code == 409
    assert backend_type.calls == 1


def test_jobs_execute_serially_and_can_cancel_queued_work(client, backend_type):
    configure(client)
    backend_type.delay = 0.12
    first = client.post("/api/jobs", json={"prompt": "first", "n": 2}).json()
    second = client.post("/api/jobs", json={"prompt": "second"}).json()
    third = client.post("/api/jobs", json={"prompt": "third"}).json()
    cancelled = client.post(f"/api/jobs/{third['id']}/cancel")
    assert cancelled.json()["status"] == "cancelled"
    assert client.post("/api/config", json=CREDENTIALS).status_code == 409
    assert wait_job(client, first["id"])["status"] == "succeeded"
    assert wait_job(client, second["id"])["status"] == "succeeded"
    assert backend_type.calls == 3
    assert backend_type.maximum_active == 1


def test_partial_failure_is_preserved_without_retry(client, backend_type):
    configure(client)
    backend_type.fail_at = 2
    response = client.post("/v1/images/generations", json={"prompt": "cat", "n": 3})
    assert response.status_code == 502
    data = response.json()
    assert len(data["data"]) == 1
    assert "sensitive-upstream-cookie" not in response.text
    assert backend_type.calls == 2
    job = client.get(f"/api/jobs/{data['error']['job_id']}").json()
    assert job["status"] == "failed"
    assert len(job["images"]) == 1


@pytest.mark.parametrize(
    ("exception_name", "status", "code"),
    [
        ("AuthError", 401, "authentication_failed"),
        ("TimeoutError", 504, "upstream_timeout"),
    ],
)
def test_upstream_errors_are_actionable_and_redacted(
    client, backend_type, exception_name, status, code
):
    configure(client)
    backend_type.error = type(exception_name, (Exception,), {})("secret-do-not-echo")
    response = client.post("/v1/images/generations", json={"prompt": "cat"})
    assert response.status_code == status
    assert response.json()["error"]["code"] == code
    assert "secret-do-not-echo" not in response.text
    assert backend_type.calls == 1


def test_gemini_text_refusal_is_returned_with_image_job(client, backend_type):
    from storage import GatewayError

    configure(client)
    backend_type.error = GatewayError(
        "generation_refused",
        "Gemini 拒绝了这次生图请求。",
        422,
        response="身为一个语言模型，我没办法提供这方面的帮助。",
    )
    response = client.post("/v1/images/generations", json={"prompt": "edit a photo"})
    assert response.status_code == 422
    assert response.json()["text"] == "身为一个语言模型，我没办法提供这方面的帮助。"
    assert response.json()["error"]["response"] == response.json()["text"]
    job_id = response.json()["error"]["job_id"]
    job = client.get(f"/v1/jobs/{job_id}").json()
    assert job["text"] == response.json()["text"]
    assert job["status"] == "failed"


def test_failed_job_retry_reuses_parameters_references_and_project_scope(
    client, backend_type
):
    from storage import GatewayError

    configure(client)
    reference = upload_reference(client)
    backend_type.error = GatewayError(
        "no_generated_image", "Gemini returned text.", response="TEXT_ONLY"
    )
    original = client.post(
        "/v1/jobs",
        json={"prompt": "edit this image", "reference_images": [reference["id"]]},
    ).json()
    assert wait_job(client, original["id"])["status"] == "failed"

    backend_type.error = None
    headers = {"Idempotency-Key": "retry-1"}
    response = client.post(f"/v1/jobs/{original['id']}/retry", headers=headers)
    assert response.status_code == 202, response.text
    retried = response.json()
    assert retried["retry_of"] == original["id"]
    assert retried["request"] == original["request"]
    assert wait_job(client, retried["id"])["status"] == "succeeded"
    duplicate = client.post(f"/v1/jobs/{original['id']}/retry", headers=headers)
    assert duplicate.json()["id"] == retried["id"]

    other_key = client.post("/api/keys", json={"name": "other-project"}).json()["key"]
    client.headers["Authorization"] = "Bearer " + other_key
    assert client.post(f"/v1/jobs/{original['id']}/retry").status_code == 404


def test_running_image_job_surfaces_gemini_text_then_clears_it_on_success(
    client, backend_type
):
    configure(client)
    backend_type.stream_text = "Gemini 正在整理参考图。"
    backend_type.text_hold = 0.25
    response = client.post("/v1/jobs", json={"prompt": "make an image"})
    assert response.status_code == 202, response.text
    job_id = response.json()["id"]

    deadline = time.monotonic() + 2
    observed = None
    while time.monotonic() < deadline:
        observed = client.get(f"/api/jobs/{job_id}").json()
        if observed.get("text"):
            break
        time.sleep(0.01)
    assert observed["status"] == "running"
    assert observed["text"] == "Gemini 正在整理参考图。"

    finished = wait_job(client, job_id)
    assert finished["status"] == "succeeded"
    assert finished["text"] is None


def test_pending_jobs_are_not_replayed_after_restart(settings, backend_type):
    settings.state_dir.mkdir()
    (settings.state_dir / "credentials.json").write_text(json.dumps(CREDENTIALS))
    (settings.state_dir / "jobs.json").write_text(
        json.dumps(
            [
                {
                    "id": "img_interrupted",
                    "status": "running",
                    "request": {"prompt": "cat"},
                    "images": [],
                    "error": None,
                }
            ]
        )
    )
    with TestClient(create_app(settings, backend_type)) as http:
        job = http.get("/api/jobs/img_interrupted").json()
        assert job["status"] == "failed"
        assert job["error"]["code"] == "service_restarted"
    assert backend_type.instances == []


def test_cross_origin_requests_and_path_traversal_are_rejected(client, settings):
    configure(client)
    response = client.post(
        "/api/config", json=CREDENTIALS, headers={"Origin": "https://attacker.example"}
    )
    assert response.status_code == 403
    assert (
        client.get("/health", headers={"Host": "attacker.example"}).status_code == 400
    )
    response = client.get("/images/%2e%2e/private/credentials.json")
    assert response.status_code == 404
    (settings.images_dir / "unexpected.html").write_text("<script>alert(1)</script>")
    assert client.get("/images/unexpected.html").status_code == 404


@pytest.mark.parametrize(
    "invalid",
    [
        {"n": True},
        {"n": 0},
        {"n": 5},
        {"prompt": " "},
        {"size": "8K"},
        {"size": "63x1024"},
        {"size": "9000x1024"},
        {"full_size": "false"},
        {"quality": "hd"},
    ],
)
def test_unsupported_or_invalid_parameters_fail_before_generation(
    client, backend_type, invalid
):
    configure(client)
    response = client.post("/v1/images/generations", json={"prompt": "cat", **invalid})
    assert response.status_code == 422
    assert backend_type.calls == 0


@pytest.mark.asyncio
@pytest.mark.parametrize("use_reference", [False, True])
@pytest.mark.parametrize("full_size", [False, True])
@pytest.mark.parametrize("prompt_mode", ["raw", "enhanced"])
async def test_adapter_disables_library_retries_and_ignores_web_search_images(
    settings, monkeypatch, use_reference, full_size, prompt_mode
):
    from gemini_webapi.types import GeneratedImage, WebImage

    settings.images_dir.mkdir(parents=True)
    generated = GeneratedImage(
        url="https://example.com/generated", title="generated", alt=""
    )
    searched = WebImage(url="https://example.com/searched", title="searched", alt="")
    saved_path = settings.images_dir / "generated.png"

    async def save_image(self, **kwargs):
        assert kwargs["full_size"] is full_size
        saved_path.write_bytes(PNG)
        return str(saved_path)

    monkeypatch.setattr(GeneratedImage, "save", save_image)
    stream_calls = []
    stream_closed = False

    async def generate_stream(*args, **kwargs):
        nonlocal stream_closed
        stream_calls.append((args, kwargs))
        try:
            yield SimpleNamespace(images=[searched, generated], text="")
            await asyncio.sleep(30)
        finally:
            stream_closed = True

    fake = SimpleNamespace(
        generate_content_stream=generate_stream,
        close=AsyncMock(),
    )
    backend = GeminiBackend(CredentialsRequest(**CREDENTIALS), settings)
    backend.client = fake
    backend.client.resolve_model = lambda model: SimpleNamespace(is_available=True)
    backend.ready = True
    request = ImageRequest(
        prompt="  cat\nwith spaces  ",
        model="default",
        full_size=full_size,
        prompt_mode=prompt_mode,
        aspect_ratio="16:9" if prompt_mode == "enhanced" else None,
    )
    reference_paths = [settings.images_dir / "reference.png"] if use_reference else []
    paths = await backend.generate(
        request, settings.images_dir, 1, reference_paths=reference_paths
    )
    assert paths == [saved_path]
    args, kwargs = stream_calls[0]
    assert kwargs["current_retry"] == 0
    assert kwargs["files"] == (reference_paths or None)
    actual_prompt = args[0]
    if prompt_mode == "raw":
        assert actual_prompt == "  cat\nwith spaces  "
    else:
        if use_reference:
            assert "参考图" in actual_prompt
        assert "16:9" in actual_prompt
    assert len(stream_calls) == 1
    assert stream_closed


@pytest.mark.asyncio
async def test_clear_refusal_finishes_stream_early_and_returns_gemini_text(
    settings, tmp_path
):
    from engine import is_generation_refusal
    from storage import GatewayError

    assert is_generation_refusal("身为一个语言模型，我没办法提供这方面的帮助。")
    assert is_generation_refusal("我是一个语言模型，这超出了我的设计用途。")
    assert is_generation_refusal("由于程序代码的局限，我没法办到。")
    assert is_generation_refusal("我只是一个文本 AI，在这方面没法帮到你。")
    assert not is_generation_refusal("I can help create a warm studio photograph.")
    closed = False

    async def refusal_stream(*args, **kwargs):
        nonlocal closed
        try:
            yield SimpleNamespace(
                images=[], text="身为一个语言模型，我没办法提供这方面的帮助。"
            )
            await asyncio.sleep(30)
        finally:
            closed = True

    backend = GeminiBackend(CredentialsRequest(**CREDENTIALS), settings)
    backend.client = SimpleNamespace(generate_content_stream=refusal_stream)
    backend.client.resolve_model = lambda model: SimpleNamespace(is_available=True)
    backend.ready = True
    request = ImageRequest(prompt="make a harmless room photo")
    with pytest.raises(GatewayError) as result:
        await backend.generate(request, tmp_path, 1)
    assert result.value.code == "generation_refused"
    assert result.value.response == "身为一个语言模型，我没办法提供这方面的帮助。"
    assert closed


@pytest.mark.asyncio
async def test_completed_text_turn_returns_without_waiting_for_stream_close(
    settings, tmp_path
):
    from engine import GeminiBackend
    from storage import GatewayError

    response = SimpleNamespace(
        metadata=["chat-1", "reply-2"], text="I completed a text response.", images=[]
    )

    async def text_stream(*args, **kwargs):
        yield SimpleNamespace(
            metadata=["chat-1", "reply-2"], text=response.text, images=[]
        )
        await asyncio.sleep(30)

    final_turn = SimpleNamespace(role="model", model_output=response)
    history = SimpleNamespace(turns=[final_turn])
    client = SimpleNamespace(
        generate_content_stream=text_stream,
        resolve_model=lambda model: SimpleNamespace(is_available=True),
        read_chat=AsyncMock(return_value=history),
    )
    backend = GeminiBackend(CredentialsRequest(**CREDENTIALS), settings)
    backend.client, backend.ready = client, True
    with pytest.raises(GatewayError) as result:
        await backend.generate(ImageRequest(prompt="make an image"), tmp_path, 1)
    assert result.value.code == "no_generated_image"
    assert result.value.response == response.text
    client.read_chat.assert_awaited_once_with("chat-1", limit=2)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("response_text", "expected_code"),
    [
        ("Gemini finished while its stream stalled.", "no_generated_image"),
        (
            "一旦您的额度重置，我就可以创建更多图片。请在设置中查看您的使用情况。",
            "quota_exceeded",
        ),
    ],
)
async def test_completed_text_turn_is_polled_while_stream_stalls(
    settings, tmp_path, response_text, expected_code
):
    from engine import GeminiBackend
    from storage import GatewayError

    response = SimpleNamespace(
        metadata=["chat-1", "reply-2"],
        text=response_text,
        images=[],
    )

    async def text_stream(*args, **kwargs):
        yield SimpleNamespace(metadata=response.metadata, text="", images=[])
        await asyncio.sleep(30)

    final_turn = SimpleNamespace(role="model", model_output=response)
    history = SimpleNamespace(turns=[final_turn])
    client = SimpleNamespace(
        generate_content_stream=text_stream,
        resolve_model=lambda model: SimpleNamespace(is_available=True),
        read_chat=AsyncMock(side_effect=[None, history]),
    )
    backend = GeminiBackend(CredentialsRequest(**CREDENTIALS), settings)
    backend.client, backend.ready = client, True
    started = time.monotonic()

    with pytest.raises(GatewayError) as result:
        await asyncio.wait_for(
            backend.generate(ImageRequest(prompt="make an image"), tmp_path, 1),
            timeout=3,
        )

    assert result.value.code == expected_code
    assert result.value.response == response.text
    assert time.monotonic() - started < 3
    assert client.read_chat.await_count == 2


@pytest.mark.asyncio
async def test_chat_quota_reply_is_reported_as_quota_error(settings):
    from engine import GeminiBackend
    from schemas import ChatRequest
    from storage import GatewayError

    reply = "You have reached the daily usage limit. Please try again after it resets."
    client = SimpleNamespace(
        generate_content=AsyncMock(return_value=SimpleNamespace(text=reply)),
        resolve_model=lambda model: SimpleNamespace(is_available=True),
    )
    backend = GeminiBackend(CredentialsRequest(**CREDENTIALS), settings)
    backend.client, backend.ready = client, True

    with pytest.raises(GatewayError) as result:
        await backend.chat(ChatRequest(messages=[{"role": "user", "content": "hello"}]))

    assert result.value.code == "quota_exceeded"
    assert result.value.response == reply


@pytest.mark.asyncio
async def test_completed_image_is_recovered_while_stream_stalls(
    settings, tmp_path, monkeypatch
):
    from gemini_webapi.types import GeneratedImage

    image = GeneratedImage(url="https://example.com/final.png")
    saved_path = tmp_path / "final.png"

    async def save_image(self, **kwargs):
        saved_path.write_bytes(PNG)
        return str(saved_path)

    monkeypatch.setattr(GeneratedImage, "save", save_image)
    response = SimpleNamespace(metadata=["chat-1", "reply-2"], text="", images=[image])

    async def image_stream(*args, **kwargs):
        yield SimpleNamespace(metadata=response.metadata, text="", images=[])
        await asyncio.sleep(30)

    history = SimpleNamespace(
        turns=[SimpleNamespace(role="model", model_output=response)]
    )
    client = SimpleNamespace(
        generate_content_stream=image_stream,
        resolve_model=lambda model: SimpleNamespace(is_available=True),
        read_chat=AsyncMock(side_effect=[None, history]),
    )
    backend = GeminiBackend(CredentialsRequest(**CREDENTIALS), settings)
    backend.client, backend.ready = client, True

    result = await asyncio.wait_for(
        backend.generate(ImageRequest(prompt="make an image"), tmp_path, 1),
        timeout=3,
    )

    assert result == [saved_path]
    assert saved_path.read_bytes() == PNG
    assert client.read_chat.await_count == 2


def test_ten_reference_ids_are_allowed_and_eleven_are_rejected():
    from pydantic import ValidationError

    refs = ["ref_" + f"{i:032x}" for i in range(10)]
    assert (
        len(ImageRequest(prompt="edit", reference_images=refs).reference_images) == 10
    )
    with pytest.raises(ValidationError):
        ImageRequest(prompt="edit", reference_images=refs + ["ref_" + "a" * 32])


def test_generation_timeout_is_bounded_to_ten_minutes(monkeypatch, tmp_path):
    monkeypatch.setenv("WEB2API_STATE_DIR", str(tmp_path / "state"))
    monkeypatch.setenv("WEB2API_IMAGES_DIR", str(tmp_path / "images"))
    monkeypatch.setenv("WEB2API_TIMEOUT", "2700")
    assert Settings.from_environment().timeout == 600
    monkeypatch.setenv("WEB2API_TIMEOUT", "15")
    assert Settings.from_environment().timeout == 30


def test_health_reports_generation_limits_and_wait_upper_bound(client):
    health = client.get("/health").json()
    assert health["version"] == "3.4.1"
    assert health["seedream"]["configured"] is False
    assert health["generation_timeout_seconds"] == 60
    assert health["limits"] == {
        "prompt_characters": 16000,
        "negative_prompt_characters": 16000,
        "reference_images": 10,
        "reference_image_bytes": 100 * 1024 * 1024,
        "reference_total_bytes": 100 * 1024 * 1024,
        "reference_image_pixels": 25_000_000,
        "output_images": 4,
        "batch_prompts": 20,
        "pending_jobs": 20,
        "chat_message_characters": 32000,
        "chat_total_characters": 64000,
        "chat_messages": 80,
    }


def test_chat_character_and_message_limits_are_enforced():
    from pydantic import ValidationError

    assert ChatRequest(messages=[{"role": "user", "content": "x" * 32000}])
    assert ChatRequest(
        messages=[
            {"role": "system", "content": "s" * 32000},
            {"role": "user", "content": "x" * 32000},
        ]
    )
    with pytest.raises(ValidationError):
        ChatRequest(messages=[{"role": "user", "content": "x" * 32001}])
    with pytest.raises(ValidationError):
        ChatRequest(
            messages=[
                {"role": "system", "content": "s" * 32000},
                {"role": "assistant", "content": "a" * 32000},
                {"role": "user", "content": "x"},
            ]
        )
    with pytest.raises(ValidationError):
        ChatRequest(
            messages=[
                *([{"role": "assistant", "content": "a"}] * 80),
                {"role": "user", "content": "x"},
            ]
        )


def test_prompt_character_and_output_count_limits_are_enforced(client):
    assert (
        client.post(
            "/api/requests/preview",
            json={"prompt": "x" * 16000, "negative_prompt": "n" * 16000, "n": 4},
        ).status_code
        == 200
    )
    too_long = client.post("/api/requests/preview", json={"prompt": "x" * 16001})
    assert too_long.status_code == 422
    assert "prompt" in too_long.json()["error"]["message"]
    too_many = client.post("/api/requests/preview", json={"prompt": "x", "n": 5})
    assert too_many.status_code == 422
    assert "n" in too_many.json()["error"]["message"]


def test_project_keys_are_required_and_only_shown_once(client, settings):
    response = client.get("/v1/models", headers={"Authorization": ""})
    assert response.status_code == 401
    assert response.json()["error"]["code"] == "api_key_required"
    response = client.post(
        "/v1/images/generations",
        json={"prompt": "cat"},
        headers={"Authorization": "Bearer invalid"},
    )
    assert response.status_code == 401
    created = client.post("/api/keys", json={"name": "cover-project"}).json()
    assert created["key"].startswith("gw_")
    assert "digest" not in created
    assert created["key"] not in client.get("/api/keys").text
    assert created["key"] not in (settings.state_dir / "api-keys.json").read_text()
    revoked = client.post(f"/api/keys/{created['id']}/revoke").json()
    assert not revoked["enabled"]
    assert (
        client.get(
            "/v1/models", headers={"Authorization": "Bearer " + created["key"]}
        ).status_code
        == 401
    )


def test_batch_jobs_return_correlated_results_and_do_not_retry(client, backend_type):
    configure(client)
    backend_type.fail_at = 2
    payload = {
        "items": [
            {"custom_id": "cover", "prompt": "cover image"},
            {"custom_id": "banner", "prompt": "banner image"},
            {"custom_id": "footer", "prompt": "footer image"},
        ]
    }
    response = client.post(
        "/v1/batches", json=payload, headers={"Idempotency-Key": "batch-once"}
    )
    assert response.status_code == 202, response.text
    batch_id = response.json()["id"]
    for item in response.json()["items"]:
        wait_job(client, item["job_id"])
    final = client.get(f"/v1/batches/{batch_id}").json()
    assert final["status"] == "completed"
    assert final["counts"]["succeeded"] == 2
    assert final["counts"]["failed"] == 1
    assert [item["custom_id"] for item in final["items"]] == [
        "cover",
        "banner",
        "footer",
    ]
    assert len(final["items"][0]["data"]) == 1
    assert final["items"][1]["error"]["code"] == "upstream_error"
    duplicate = client.post(
        "/v1/batches", json=payload, headers={"Idempotency-Key": "batch-once"}
    )
    assert duplicate.json()["id"] == batch_id
    assert backend_type.calls == 3
    changed = {"items": [{"custom_id": "cover", "prompt": "changed"}]}
    assert (
        client.post(
            "/v1/batches", json=changed, headers={"Idempotency-Key": "batch-once"}
        ).status_code
        == 409
    )


def test_keys_isolate_job_access_and_idempotency(client, backend_type):
    configure(client)
    payload = {"prompt": "cat"}
    first = client.post(
        "/v1/jobs", json=payload, headers={"Idempotency-Key": "same-business-id"}
    ).json()
    wait_job(client, first["id"])
    token = client.post("/api/keys", json={"name": "second-project"}).json()["key"]
    second_headers = {
        "Authorization": "Bearer " + token,
        "Idempotency-Key": "same-business-id",
    }
    assert (
        client.get(f"/v1/jobs/{first['id']}", headers=second_headers).status_code == 404
    )
    second = client.post("/v1/jobs", json=payload, headers=second_headers).json()
    assert first["id"] != second["id"]
    wait_job(client, second["id"])
    assert backend_type.calls == 2


def test_batch_ownership_and_restart(settings, backend_type):
    with TestClient(create_app(settings, backend_type)) as http:
        configure(http)
        token = http.post("/api/keys", json={"name": "project"}).json()["key"]
        headers = {"Authorization": "Bearer " + token}
        batch = http.post(
            "/v1/batches",
            headers=headers,
            json={"items": [{"custom_id": "a", "prompt": "cat"}]},
        ).json()
        wait_job(http, batch["items"][0]["job_id"])
        second = http.post("/api/keys", json={"name": "other"}).json()["key"]
        assert (
            http.get(
                f"/v1/batches/{batch['id']}",
                headers={"Authorization": "Bearer " + second},
            ).status_code
            == 404
        )
    with TestClient(create_app(settings, backend_type)) as http:
        result = http.get(f"/v1/batches/{batch['id']}", headers=headers)
        assert result.status_code == 200
        assert result.json()["counts"]["succeeded"] == 1
    assert backend_type.calls == 1


def test_batch_capacity_rejection_is_atomic(client, backend_type):
    configure(client)
    client.app.state.service.settings.max_pending = 2
    backend_type.delay = 0.2
    client.post("/api/jobs", json={"prompt": "first"})
    client.post("/api/jobs", json={"prompt": "second"})
    before = len(client.get("/api/jobs").json()["data"])
    response = client.post(
        "/v1/batches",
        json={
            "items": [
                {"custom_id": "a", "prompt": "a"},
                {"custom_id": "b", "prompt": "b"},
            ]
        },
    )
    assert response.status_code == 429
    assert len(client.get("/api/jobs").json()["data"]) == before
    duplicate_id = {
        "items": [{"custom_id": "a", "prompt": "x"}, {"custom_id": "a", "prompt": "y"}]
    }
    assert client.post("/v1/batches", json=duplicate_id).status_code == 422


def test_local_project_cors_requires_key_and_does_not_expose_admin(client):
    origin = "http://localhost:5173"
    preflight = client.options(
        "/v1/batches",
        headers={
            "Origin": origin,
            "Access-Control-Request-Method": "POST",
            "Access-Control-Request-Headers": "Authorization, Content-Type",
        },
    )
    assert preflight.status_code == 200
    assert preflight.headers["access-control-allow-origin"] == origin
    denied = client.get("/v1/models", headers={"Origin": origin, "Authorization": ""})
    assert denied.status_code == 401
    assert denied.headers["access-control-allow-origin"] == origin
    assert client.get("/api/keys", headers={"Origin": origin}).status_code == 403
    assert (
        client.get(
            "/v1/models", headers={"Origin": "https://attacker.example"}
        ).status_code
        == 403
    )


def reference_bytes(fmt="PNG", color="green"):
    import io

    from PIL import Image

    stream = io.BytesIO()
    Image.new("RGB", (16, 12), color).save(stream, format=fmt)
    return stream.getvalue()


def upload_reference(client, content=None, **kwargs):
    response = client.post(
        "/v1/references",
        files={"files": ("reference.png", content or reference_bytes(), "image/png")},
        **kwargs,
    )
    assert response.status_code == 201, response.text
    return response.json()["data"][0]


def test_reference_upload_is_private_validated_and_requires_key(client, settings):
    content = reference_bytes()
    assert (
        client.post(
            "/v1/references",
            files={"files": ("a.png", content)},
            headers={"Authorization": ""},
        ).status_code
        == 401
    )
    response = client.post(
        "/v1/references", files={"files": ("../misleading.jpg", content, "text/plain")}
    )
    assert response.status_code == 201, response.text
    reference = response.json()["data"][0]
    assert reference["name"] == "misleading.jpg"
    assert reference["mime_type"] == "image/png"
    assert reference["width"] == 16 and reference["height"] == 12
    assert "owner_id" not in reference and "filename" not in reference
    downloaded = client.get(f"/v1/references/{reference['id']}/content")
    assert downloaded.content == content
    assert downloaded.headers["content-type"] == "image/png"
    assert client.get(f"/images/{reference['id']}.png").status_code == 404
    assert (settings.state_dir / "references.json").stat().st_mode & 0o777 == 0o600
    stored = list((settings.state_dir / "references").iterdir())
    assert len(stored) == 1 and stored[0].suffix == ".png"
    assert stored[0].stat().st_mode & 0o777 == 0o600


@pytest.mark.parametrize("fmt", ["PNG", "JPEG", "WEBP"])
def test_all_supported_reference_formats_upload(client, fmt):
    content = reference_bytes(fmt)
    reference = upload_reference(client, content)
    assert (
        reference["mime_type"]
        == {"PNG": "image/png", "JPEG": "image/jpeg", "WEBP": "image/webp"}[fmt]
    )
    assert client.get(f"/v1/references/{reference['id']}/content").content == content


@pytest.mark.parametrize(
    "bad", [b"<html>not an image</html>", b"\x89PNG\r\n\x1a\ntruncated"]
)
def test_invalid_reference_group_is_rejected_atomically(client, settings, bad):
    response = client.post(
        "/v1/references",
        files=[
            ("files", ("good.png", reference_bytes(), "image/png")),
            ("files", ("bad.png", bad, "image/png")),
        ],
    )
    assert response.status_code == 415
    assert list((settings.state_dir / "references").iterdir()) == []
    assert not client.app.state.service.references.items


def test_reference_limits_and_streamed_body_limit(client, settings, monkeypatch):
    content = reference_bytes()
    assert (
        client.post(
            "/v1/references",
            files=[("files", (f"{i}.png", content, "image/png")) for i in range(10)],
        ).status_code
        == 201
    )
    saved_reference_count = len(client.app.state.service.references.items)
    assert (
        client.post(
            "/v1/references",
            files=[("files", (f"{i}.png", content)) for i in range(11)],
        ).status_code
        == 422
    )
    import references
    import server

    monkeypatch.setattr(references, "MAX_REFERENCE_PIXELS", 100)
    assert (
        client.post(
            "/v1/references", files={"files": ("large.png", content)}
        ).status_code
        == 413
    )
    monkeypatch.setattr(server, "MAX_REFERENCE_BYTES", 128)
    assert (
        client.post(
            "/v1/references", files={"files": ("large.png", b"x" * 129)}
        ).status_code
        == 413
    )
    # Trigger the ASGI streaming cap, independently of Content-Length and file validation.
    monkeypatch.setattr(server, "MAX_REFERENCE_TOTAL_BYTES", 128)
    response = client.post(
        "/v1/references", files={"files": ("large.png", b"x" * (1024 * 1024 + 1024))}
    )
    assert response.status_code == 413, response.text
    assert response.json()["error"]["code"] == "reference_too_large"
    assert len(client.app.state.service.references.items) == saved_reference_count


def test_reference_upload_enforces_combined_size_limit(client, settings, monkeypatch):
    import references
    import server

    content = reference_bytes()
    total_limit = len(content) * 2 - 1
    monkeypatch.setattr(server, "MAX_REFERENCE_BYTES", len(content) + 1)
    monkeypatch.setattr(references, "MAX_REFERENCE_BYTES", len(content) + 1)
    monkeypatch.setattr(server, "MAX_REFERENCE_TOTAL_BYTES", total_limit)
    monkeypatch.setattr(references, "MAX_REFERENCE_TOTAL_BYTES", total_limit)
    response = client.post(
        "/v1/references",
        files=[
            ("files", ("first.png", content, "image/png")),
            ("files", ("second.png", content, "image/png")),
        ],
    )
    assert response.status_code == 413
    assert response.json()["error"]["code"] == "reference_too_large"
    assert "总大小" in response.json()["error"]["message"]
    assert client.app.state.service.references.items == {}
    assert list((settings.state_dir / "references").iterdir()) == []


def test_reference_generation_passes_saved_images_to_each_generation(
    client, backend_type
):
    configure(client)
    content = reference_bytes()
    reference = upload_reference(client, content)
    payload = {
        "prompt": "keep the subject, change the background",
        "n": 2,
        "reference_images": [reference["id"]],
    }
    response = client.post(
        "/v1/images/generations", json=payload, headers={"Idempotency-Key": "edit-once"}
    )
    assert response.status_code == 200, response.text
    assert backend_type.reference_calls == [[content], [content]]
    assert len(response.json()["data"]) == 2
    job_id = response.json()["job_id"]
    job = client.get(f"/v1/jobs/{job_id}").json()
    assert job["request"]["reference_images"] == [reference["id"]]
    assert (
        client.post(
            "/v1/images/generations",
            json=payload,
            headers={"Idempotency-Key": "edit-once"},
        ).json()["job_id"]
        == job_id
    )
    assert backend_type.calls == 2


def test_reference_ownership_and_invalid_paths_fail_before_queue(client, backend_type):
    configure(client)
    reference = upload_reference(client)
    token = client.post("/api/keys", json={"name": "other-project"}).json()["key"]
    headers = {"Authorization": "Bearer " + token}
    assert (
        client.get(
            f"/v1/references/{reference['id']}/content", headers=headers
        ).status_code
        == 404
    )
    assert (
        client.post(
            "/v1/jobs",
            json={"prompt": "edit", "reference_images": [reference["id"]]},
            headers=headers,
        ).status_code
        == 404
    )
    for ids in [
        ["../../credentials.json"],
        ["https://example.com/a.png"],
        [reference["id"]] * 2,
        ["ref_" + "a" * 32] * 5,
    ]:
        response = client.post(
            "/v1/jobs", json={"prompt": "edit", "reference_images": ids}
        )
        assert response.status_code == 422, response.text
    assert (
        client.post(
            "/v1/jobs", json={"prompt": "edit", "reference_images": ["ref_" + "f" * 32]}
        ).status_code
        == 404
    )
    assert backend_type.calls == 0
    assert client.get("/api/jobs").json()["data"] == []


def test_batch_references_are_per_item_and_validated_atomically(client, backend_type):
    configure(client)
    first, second = (
        upload_reference(client),
        upload_reference(client, reference_bytes(color="red")),
    )
    response = client.post(
        "/v1/batches",
        json={
            "items": [
                {
                    "custom_id": "valid",
                    "prompt": "edit",
                    "reference_images": [first["id"]],
                },
                {
                    "custom_id": "missing",
                    "prompt": "edit",
                    "reference_images": ["ref_" + "f" * 32],
                },
            ]
        },
    )
    assert response.status_code == 404
    assert client.get("/api/jobs").json()["data"] == []
    response = client.post(
        "/v1/batches",
        json={
            "items": [
                {
                    "custom_id": "edited",
                    "prompt": "combine references",
                    "reference_images": [first["id"], second["id"]],
                },
                {"custom_id": "text", "prompt": "text only"},
            ]
        },
    )
    assert response.status_code == 202, response.text
    for item in response.json()["items"]:
        assert wait_job(client, item["job_id"])["status"] == "succeeded"
    assert backend_type.reference_calls == [
        [reference_bytes(), reference_bytes(color="red")],
        [],
    ]


def test_admin_reference_flow_is_independent_of_project_keys(client, backend_type):
    configure(client)
    reference = client.post(
        "/api/references", files={"files": ("test.png", reference_bytes())}
    ).json()["data"][0]
    assert client.get(f"/api/references/{reference['id']}/content").status_code == 200
    assert client.get(f"/v1/references/{reference['id']}/content").status_code == 404
    response = client.post(
        "/api/batches",
        json={
            "items": [
                {
                    "custom_id": "one",
                    "prompt": "edit",
                    "reference_images": [reference["id"]],
                },
                {
                    "custom_id": "two",
                    "prompt": "edit again",
                    "reference_images": [reference["id"]],
                },
            ]
        },
    )
    assert response.status_code == 202
    for item in response.json()["items"]:
        assert wait_job(client, item["job_id"])["status"] == "succeeded"
    assert backend_type.reference_calls == [[reference_bytes()], [reference_bytes()]]


def test_reference_files_and_idempotency_survive_restart(settings, backend_type):
    with TestClient(create_app(settings, backend_type)) as http:
        configure(http)
        token = http.post("/api/keys", json={"name": "project"}).json()["key"]
        http.headers["Authorization"] = "Bearer " + token
        reference = upload_reference(http)
        payload = {"prompt": "edit", "reference_images": [reference["id"]]}
        result = http.post(
            "/v1/images/generations",
            json=payload,
            headers={"Idempotency-Key": "persistent-edit"},
        ).json()
        assert "job_id" in result
    with TestClient(create_app(settings, backend_type)) as http:
        http.headers["Authorization"] = "Bearer " + token
        assert (
            http.get(f"/v1/references/{reference['id']}/content").content
            == reference_bytes()
        )
        assert (
            http.post(
                "/v1/images/generations",
                json=payload,
                headers={"Idempotency-Key": "persistent-edit"},
            ).json()["job_id"]
            == result["job_id"]
        )
    assert backend_type.calls == 1


def test_standard_library_client_uploads_real_multipart(client, tmp_path, monkeypatch):
    import io

    from reference_client import upload_references

    token = client.headers["Authorization"].removeprefix("Bearer ")
    paths = []
    for fmt in ("PNG", "JPEG"):
        path = tmp_path / f"reference.{fmt.lower()}"
        path.write_bytes(reference_bytes(fmt))
        paths.append(str(path))

    def request_to_local_test_server(request, timeout):
        response = client.request(
            request.get_method(),
            request.full_url,
            content=request.data,
            headers=dict(request.header_items()),
        )
        assert response.status_code == 201, response.text
        return io.BytesIO(response.content)

    monkeypatch.setattr("urllib.request.urlopen", request_to_local_test_server)
    ids = upload_references("http://testserver", token, paths)
    assert len(ids) == 2
    for reference_id, fmt in zip(ids, ("PNG", "JPEG"), strict=True):
        assert client.get(
            f"/v1/references/{reference_id}/content"
        ).content == reference_bytes(fmt)


def test_preview_normalizes_controls_without_connecting_or_generating(
    client, backend_type
):
    payload = {
        "prompt": "  陶瓷杯  ",
        "model": "gemini-argon",
        "size": "1920×1080",
        "negative_prompt": "  文字、水印  ",
        "full_size": False,
        "response_format": "b64_json",
        "n": 2,
    }
    response = client.post("/v1/requests/preview", json=payload)
    assert response.status_code == 200, response.text
    preview = response.json()
    assert preview["parameters"]["size"] == "1920x1080"
    assert preview["parameters"]["negative_prompt"] == "文字、水印"
    assert preview["parameters"]["prompt"] == "陶瓷杯"
    assert "构图宽高比为 16:9" in preview["effective_prompt"]
    assert "目标分辨率为 1920x1080" in preview["effective_prompt"]
    assert preview["effective_prompt"].endswith("请避免以下内容：文字、水印")
    assert len(preview["warnings"]) == 2
    assert backend_type.instances == []
    assert backend_type.calls == 0
    assert client.get("/api/jobs").json()["data"] == []
    assert client.post("/api/requests/preview", json=payload).json() == preview
    assert (
        client.post(
            "/v1/requests/preview", json=payload, headers={"Authorization": ""}
        ).status_code
        == 401
    )


@pytest.mark.parametrize(
    "conflict",
    [
        {"negative_prompt": "水印"},
        {"size": "2K"},
        {"aspect_ratio": "16:9"},
    ],
)
def test_raw_conflicts_are_explicit_and_fail_before_queue(
    client, backend_type, conflict
):
    configure(client)
    for endpoint in ["/api/requests/preview", "/v1/jobs", "/v1/images/generations"]:
        response = client.post(
            endpoint, json={"prompt": "cat", "prompt_mode": "raw", **conflict}
        )
        assert response.status_code == 422
        assert "raw 模式原样传递" in response.json()["error"]["message"]
    assert backend_type.calls == 0
    assert client.get("/api/jobs").json()["data"] == []


def test_raw_mode_preserves_whitespace_in_preview_and_generation(client, backend_type):
    configure(client)
    prompt = "  Generate an image.\n\n保持这些空格  \n"
    payload = {"prompt": prompt, "prompt_mode": "raw", "full_size": False}
    preview = client.post("/v1/requests/preview", json=payload).json()
    assert preview["effective_prompt"] == prompt
    result = client.post("/v1/images/generations", json=payload).json()
    assert result["parameters"]["prompt"] == prompt
    assert result["effective_prompt"] == prompt
    assert backend_type.request_calls[0]["prompt"] == prompt
    assert result["data"][0]["width"] == result["data"][0]["height"] == 1
    assert result["data"][0]["mime_type"] == "image/png"


def test_preview_checks_project_reference_ownership_without_generating(
    client, backend_type
):
    reference = upload_reference(client)
    payload = {"prompt": "edit this", "reference_images": [reference["id"]]}
    result = client.post("/v1/requests/preview", json=payload)
    assert result.status_code == 200
    assert "参考图" in result.json()["effective_prompt"]
    assert client.post("/api/requests/preview", json=payload).status_code == 404
    token = client.post("/api/keys", json={"name": "other"}).json()["key"]
    assert (
        client.post(
            "/v1/requests/preview",
            json=payload,
            headers={"Authorization": "Bearer " + token},
        ).status_code
        == 404
    )
    assert backend_type.calls == 0


def test_all_generation_parameters_persist_and_control_idempotency(
    settings, backend_type
):
    payload = {
        "model": "gemini-argon",
        "prompt": "ceramic mug",
        "n": 2,
        "negative_prompt": "watermark",
        "size": "4K",
        "aspect_ratio": "3:4",
        "prompt_mode": "enhanced",
        "full_size": False,
        "response_format": "b64_json",
    }
    single_headers = {"Idempotency-Key": "all-parameters-single"}
    batch_headers = {"Idempotency-Key": "all-parameters-batch"}
    expected = ImageRequest(**payload).model_dump()
    batch_payload = {
        "items": [
            {**payload, "custom_id": "cover"},
            {"prompt": "  exact  ", "prompt_mode": "raw", "custom_id": "raw"},
        ]
    }
    with TestClient(create_app(settings, backend_type)) as http:
        configure(http)
        token = http.post("/api/keys", json={"name": "persistent"}).json()["key"]
        http.headers["Authorization"] = "Bearer " + token
        result = http.post(
            "/v1/images/generations", json=payload, headers=single_headers
        )
        assert result.status_code == 200, result.text
        single = result.json()
        batch = http.post(
            "/v1/batches", json=batch_payload, headers=batch_headers
        ).json()
        for item in batch["items"]:
            wait_job(http, item["job_id"])
    with TestClient(create_app(settings, backend_type)) as http:
        http.headers["Authorization"] = "Bearer " + token
        single_after = http.post(
            "/v1/images/generations", json=payload, headers=single_headers
        ).json()
        assert single_after["job_id"] == single["job_id"]
        assert single_after["parameters"] == expected
        assert "watermark" in single_after["effective_prompt"]
        batch_after = http.post(
            "/v1/batches", json=batch_payload, headers=batch_headers
        ).json()
        assert batch_after["id"] == batch["id"]
        assert batch_after["items"][0]["parameters"] == expected
        assert batch_after["items"][1]["effective_prompt"] == "  exact  "
        for field, value in [
            ("size", "2K"),
            ("full_size", True),
            ("negative_prompt", "blur"),
        ]:
            changed = {**payload, field: value}
            assert (
                http.post("/v1/jobs", json=changed, headers=single_headers).status_code
                == 409
            )
            assert (
                http.post(
                    "/v1/batches",
                    json={"items": [{**changed, "custom_id": "cover"}]},
                    headers=batch_headers,
                ).status_code
                == 409
            )
    assert backend_type.calls == 5
    assert backend_type.request_calls[:2] == [expected, expected]


def test_models_reload_uses_account_discovery(client, backend_type):
    configure(client)
    models = client.get("/api/models").json()["data"]
    assert models == client.get("/v1/models").json()["data"]
    assert models[0]["id"] == "gemini-pro"
    assert backend_type.calls == 0
