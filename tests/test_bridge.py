"""Synthetic integration coverage for provider routing and browser leases."""

import base64
import io
import sys
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from PIL import Image

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from engine import Settings
from server import create_app


@pytest.fixture
def image_bytes():
    output = io.BytesIO()
    Image.new("RGB", (256, 192), "#a3b883").save(output, format="PNG")
    return output.getvalue()


@pytest.fixture
def bridge_client(tmp_path):
    settings = Settings(tmp_path / "state", tmp_path / "images", timeout=3)
    with TestClient(create_app(settings)) as client:
        key = client.post("/api/keys", json={"name": "test-project"}).json()["key"]
        client.headers["Authorization"] = "Bearer " + key
        yield client, settings


def pair(client):
    if not any(
        p["id"] == "demo-site" for p in client.get("/api/providers").json()["data"]
    ):
        assert (
            client.put(
                "/api/providers/demo-site",
                json={
                    "id": "demo-site",
                    "name": "Demo Site",
                    "url": "https://example.org/chat",
                    "prompt_selector": "textarea",
                    "send_selector": "button.send",
                    "response_selector": ".reply",
                    "image_selector": "img",
                },
            ).status_code
            == 200
        )
    token = client.post("/api/bridge/pair").json()["token"]
    headers = {"Authorization": "Bearer " + token}
    result = client.post(
        "/bridge/v1/heartbeat",
        json={
            "session_id": "synthetic_session_1",
            "tabs": [{"provider": "demo-site", "status": "ready"}],
        },
        headers=headers,
    )
    assert result.status_code == 200
    return headers


def claim(client, headers):
    deadline = time.monotonic() + 2
    while time.monotonic() < deadline:
        response = client.post(
            "/bridge/v1/claim",
            json={"session_id": "synthetic_session_1"},
            headers=headers,
        )
        assert response.status_code == 200, response.text
        task = response.json()["task"]
        if task:
            return task
        time.sleep(0.01)
    pytest.fail("No bridge task offered")


def lease(task):
    return {"session_id": "synthetic_session_1", "lease": task["lease"]}


def finish(client, headers, task, **result):
    return client.post(
        f"/bridge/v1/tasks/{task['id']}/result",
        json={**lease(task), **result},
        headers=headers,
    )


def wait_job(client, job_id):
    deadline = time.monotonic() + 5
    while time.monotonic() < deadline:
        job = client.get("/api/jobs/" + job_id).json()
        if job["status"] not in {"queued", "running"}:
            return job
        time.sleep(0.01)
    pytest.fail("Job did not finish")


def test_provider_configuration_is_separate_from_credentials(bridge_client):
    client, settings = bridge_client
    data = client.get("/api/providers").json()
    assert [item["id"] for item in data["data"]] == [
        "gemini",
        "bing",
        "imagefx-web",
        "recraft-web",
        "leonardo-web",
        "doubao-web",
        "jimeng-web",
        "tongyi-web",
        "hailuo-web",
        "puter",
        "pollinations",
    ]
    headers = pair(client)
    client.post(
        "/bridge/v1/heartbeat",
        json={"session_id": "synthetic_session_1", "tabs": []},
        headers=headers,
    )
    assert not data["paired"]
    assert (
        client.get("/v1/models?provider=demo-site").json()["data"][0]["id"] == "default"
    )
    response = client.post(
        "/v1/jobs", json={"provider": "demo-site", "prompt": "draw a cup"}
    )
    assert response.status_code == 503
    assert response.json()["error"]["code"] == "browser_not_ready"
    assert not (settings.state_dir / "credentials.json").exists()


def test_bridge_auth_and_origin_guards(bridge_client):
    client, _ = bridge_client
    headers = pair(client)
    assert client.get("/bridge/v1/providers").status_code == 401
    assert (
        client.get(
            "/api/providers", headers={"Origin": "chrome-extension://synthetic"}
        ).status_code
        == 403
    )
    assert (
        client.get(
            "/bridge/v1/providers",
            headers={**headers, "Origin": "https://demo-site.com"},
        ).status_code
        == 403
    )
    response = client.get(
        "/bridge/v1/providers",
        headers={**headers, "Origin": "chrome-extension://synthetic"},
    )
    assert response.status_code == 200
    assert (
        response.headers["access-control-allow-origin"]
        == "chrome-extension://synthetic"
    )
    assert (
        client.options(
            "/bridge/v1/claim", headers={"Origin": "chrome-extension://synthetic"}
        ).status_code
        == 200
    )
    token = headers["Authorization"].split()[1]
    assert token not in client.get("/health").text
    assert token not in client.get("/api/providers").text
    assert token not in client.get("/api/jobs").text


def test_demo_site_image_without_gemini_uses_single_lease_and_real_image(
    bridge_client, image_bytes
):
    client, settings = bridge_client
    headers = pair(client)
    request = {"provider": "demo-site", "prompt": "a cup", "aspect_ratio": "4:3"}
    job = client.post(
        "/v1/jobs", json=request, headers={"Idempotency-Key": "provider-image"}
    ).json()
    assert job["request"]["model"] == "default"
    task = claim(client, headers)
    assert task["provider"] == "demo-site" and task["kind"] == "image"
    assert "4:3" in task["prompt"]
    assert "paths" not in task and "owner_id" not in task
    assert (
        client.post(
            "/bridge/v1/claim",
            json={"session_id": "synthetic_session_1"},
            headers=headers,
        ).json()["task"]
        is None
    )
    bad_lease = client.post(
        f"/bridge/v1/tasks/{task['id']}/result",
        json={
            **lease(task),
            "lease": "incorrect-lease-value-long",
            "images": [base64.b64encode(image_bytes).decode()],
        },
        headers=headers,
    )
    assert bad_lease.status_code == 409
    encoded = base64.b64encode(image_bytes).decode()
    assert finish(client, headers, task, images=[encoded]).status_code == 200
    completed = wait_job(client, job["id"])
    assert completed["status"] == "succeeded"
    assert client.get("/images/" + completed["images"][0]).content == image_bytes
    assert len(list(settings.images_dir.glob("*/*"))) == 1
    assert finish(client, headers, task, images=[encoded]).status_code == 409
    repeated = client.post(
        "/v1/jobs", json=request, headers={"Idempotency-Key": "provider-image"}
    )
    assert repeated.json()["id"] == job["id"]
    assert (
        client.post(
            "/v1/jobs",
            json={**request, "provider": "gemini"},
            headers={"Idempotency-Key": "provider-image"},
        ).status_code
        == 409
    )


def test_references_require_lease_and_remain_project_scoped(bridge_client, image_bytes):
    client, _ = bridge_client
    headers = pair(client)
    reference = client.post(
        "/v1/references", files={"files": ("ref.png", image_bytes, "image/png")}
    ).json()["data"][0]["id"]
    job = client.post(
        "/v1/jobs",
        json={
            "provider": "demo-site",
            "prompt": "edit",
            "reference_images": [reference],
        },
    ).json()
    task = claim(client, headers)
    url = f"/bridge/v1/tasks/{task['id']}/references/0"
    assert task["reference_count"] == 1
    assert client.post(url, json=lease(task), headers=headers).content == image_bytes
    assert (
        client.post(
            url,
            json={**lease(task), "session_id": "other_session_123"},
            headers=headers,
        ).status_code
        == 409
    )
    assert (
        client.post(
            "/api/jobs",
            json={
                "provider": "demo-site",
                "prompt": "edit",
                "reference_images": [reference],
            },
        ).status_code
        == 404
    )
    assert finish(client, headers, task, error="upload_failed").status_code == 200
    assert wait_job(client, job["id"])["error"]["code"] == "upload_failed"
    assert client.post(url, json=lease(task), headers=headers).status_code == 409


def test_chat_completion_context_and_key_isolation(bridge_client):
    client, _ = bridge_client
    headers = pair(client)
    payload = {
        "provider": "demo-site",
        "messages": [
            {"role": "system", "content": "中文简洁回答"},
            {"role": "user", "content": "你好"},
        ],
    }
    with ThreadPoolExecutor() as pool:
        future = pool.submit(
            client.post,
            "/v1/chat/completions",
            json=payload,
            headers={"Idempotency-Key": "chat-context"},
        )
        task = claim(client, headers)
        assert task["kind"] == "chat" and "中文简洁回答" in task["prompt"]
        assert finish(client, headers, task, text="你好！").status_code == 200
        response = future.result(timeout=5)
    assert response.status_code == 200
    result = response.json()
    assert result["object"] == "chat.completion"
    assert result["choices"][0]["message"]["content"] == "你好！"
    assert "usage" not in result
    other = client.post("/api/keys", json={"name": "other"}).json()["key"]
    assert (
        client.get(
            "/v1/jobs/" + result["job_id"], headers={"Authorization": "Bearer " + other}
        ).status_code
        == 404
    )
    assert (
        client.post(
            "/v1/chat/completions", json={**payload, "stream": True}
        ).status_code
        == 422
    )
    assert (
        client.post(
            "/v1/chat/completions",
            json=payload,
            headers={"Idempotency-Key": "chat-context"},
        ).json()["job_id"]
        == result["job_id"]
    )


def test_invalid_images_not_saved_and_no_hidden_retry(bridge_client):
    client, settings = bridge_client
    headers = pair(client)
    job = client.post(
        "/v1/jobs", json={"provider": "demo-site", "prompt": "draw"}
    ).json()
    task = claim(client, headers)
    for value in ("not base64", base64.b64encode(b"<html>login</html>").decode()):
        assert finish(client, headers, task, images=[value]).status_code == 415
    assert list(settings.images_dir.glob("*/*")) == []
    assert finish(client, headers, task, error="download_failed").status_code == 200
    result = wait_job(client, job["id"])
    assert result["status"] == "failed"
    assert (
        client.post(
            "/bridge/v1/claim",
            json={"session_id": "synthetic_session_1"},
            headers=headers,
        ).json()["task"]
        is None
    )


def test_browser_timeout_does_not_requeue_or_retry(bridge_client):
    client, _ = bridge_client
    headers = pair(client)
    job = client.post(
        "/v1/jobs", json={"provider": "demo-site", "prompt": "draw"}
    ).json()
    task = claim(client, headers)
    result = wait_job(client, job["id"])
    assert (
        result["status"] == "failed" and result["error"]["code"] == "upstream_timeout"
    )
    assert finish(client, headers, task, error="upstream_error").status_code == 409
    assert client.get("/health").json()["queue_size"] == 0


def test_pair_rotation_revokes_old_bridge_token(bridge_client):
    client, settings = bridge_client
    old = pair(client)
    client.post("/api/bridge/pair")
    assert client.get("/bridge/v1/providers", headers=old).status_code == 401
    assert (settings.state_dir / "bridge-secret.json").stat().st_mode & 0o777 == 0o600
    assert client.get("/api/providers").json()["data"][1]["state"] == "disconnected"


def test_custom_provider_validation_capabilities_and_persistence(bridge_client):
    client, settings = bridge_client
    pair(client)
    item = {
        "id": "custom-site",
        "name": "Custom",
        "url": "https://example.com/chat",
        "prompt_selector": "textarea",
        "send_selector": "button.send",
        "response_selector": ".reply",
        "images": False,
        "text": True,
        "references": False,
    }
    assert client.put("/api/providers/custom-site", json=item).status_code == 200
    assert (
        client.put(
            "/api/providers/custom-site",
            json={**item, "url": "https://user:secret@example.com/"},
        ).status_code
        == 422
    )
    assert (
        client.put("/api/providers/chatgpt", json={**item, "id": "chatgpt"}).status_code
        == 422
    )
    assert (
        client.post(
            "/v1/jobs", json={"provider": "custom-site", "prompt": "draw"}
        ).json()["error"]["code"]
        == "unsupported_capability"
    )
    assert (
        client.post(
            "/v1/jobs",
            json={"provider": "demo-site", "model": "made-up", "prompt": "draw"},
        ).status_code
        == 422
    )
    with TestClient(create_app(settings)) as reopened:
        assert any(
            item["id"] == "custom-site"
            for item in reopened.get("/api/providers").json()["data"]
        )


def test_batch_routes_each_item_and_preserves_partial_images(
    bridge_client, image_bytes
):
    client, _ = bridge_client
    headers = pair(client)
    payload = {
        "items": [
            {"custom_id": "first", "provider": "demo-site", "prompt": "one", "n": 2},
            {"custom_id": "second", "provider": "demo-site", "prompt": "two"},
        ]
    }
    batch = client.post(
        "/v1/batches", json=payload, headers={"Idempotency-Key": "bridge-batch"}
    ).json()
    assert batch["counts"]["total"] == 2
    first = claim(client, headers)
    assert client.post("/api/bridge/pair").status_code == 409
    assert (
        finish(
            client, headers, first, images=[base64.b64encode(image_bytes).decode()]
        ).status_code
        == 200
    )
    second = claim(client, headers)
    assert (
        finish(client, headers, second, error="no_generated_image").status_code == 200
    )
    third = claim(client, headers)
    assert (
        finish(
            client, headers, third, images=[base64.b64encode(image_bytes).decode()]
        ).status_code
        == 200
    )
    deadline = time.monotonic() + 3
    while time.monotonic() < deadline:
        final = client.get("/v1/batches/" + batch["id"]).json()
        if final["status"] != "in_progress":
            break
        time.sleep(0.01)
    assert final["counts"]["failed"] == 1 and final["counts"]["succeeded"] == 1
    assert len(final["items"][0]["data"]) == 1
    assert (
        client.post(
            "/v1/batches", json=payload, headers={"Idempotency-Key": "bridge-batch"}
        ).json()["id"]
        == batch["id"]
    )
