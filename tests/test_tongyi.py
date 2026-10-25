"""Tongyi Wanxiang web adapter tests use mocked first-party responses."""

import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import tongyi
import video
from schemas import ImageRequest, VideoRequest


def backend():
    return tongyi.TongyiWebBackend(
        {"credential": "session=private; XSRF-TOKEN=csrf%2Bvalue"}, timeout=3
    )


@pytest.mark.asyncio
async def test_tongyi_credits_split_free_bonus_from_paid_buckets(monkeypatch):
    adapter = backend()

    async def request(method, path, payload=None):
        assert path == "/wanx/api/common/imagineCount"
        return {
            "data": {
                "availableCount": 119,
                "amount": {"bonus": 7, "member": 12, "topUp": 100, "total": 119},
            },
            "success": True,
        }

    monkeypatch.setattr(adapter, "_request", request)
    result = await adapter.credits()
    assert result == {
        "provider": "tongyi-web",
        "credits": {
            "bonus": 7,
            "member": 12,
            "top_up": 100,
            "total": 119,
            "free_available": 7,
            "free_buckets_known": True,
        },
    }
    assert "private" not in json.dumps(result)


@pytest.mark.asyncio
async def test_tongyi_free_guard_rejects_paid_only_balance(monkeypatch):
    adapter = backend()

    async def request(method, path, payload=None):
        if path.endswith("imagineCount"):
            return {"data": {"amount": {"bonus": 0, "member": 0, "topUp": 99, "total": 99}}}
        if path.endswith("dailySignReward"):
            return {"success": True}
        raise AssertionError(path)

    monkeypatch.setattr(adapter, "_request", request)
    with pytest.raises(tongyi.GatewayError) as error:
        await adapter._require_free_credit(1)
    assert error.value.code == "tongyi_free_quota_empty"


@pytest.mark.asyncio
async def test_tongyi_image_payload_and_task_poll(monkeypatch, tmp_path):
    adapter = backend()
    calls = []

    async def request(method, path, payload=None):
        calls.append((method, path, payload))
        if path.endswith("imageGen"):
            return {"success": True, "data": {"taskId": "task-image"}}
        if path.endswith("taskResult"):
            return {
                "success": True,
                "data": {
                    "status": 2,
                    "taskResult": [{"downloadUrl": "https://cdn.test/image.png"}],
                },
            }
        raise AssertionError(path)

    monkeypatch.setattr(adapter, "_request", request)
    monkeypatch.setattr(
        adapter,
        "_require_free_credit",
        lambda cost: _result({"free_available": 10}),
    )
    monkeypatch.setattr(
        adapter,
        "_download_sync",
        lambda url, path, kind: path.write_bytes(b"\x89PNG\r\n\x1a\n"),
    )
    request_body = ImageRequest(prompt="一只猫", provider="tongyi-web", model="wan2.7-flash")
    result = await adapter.generate_image(request_body, tmp_path, [])
    assert len(result) == 1 and result[0].read_bytes().startswith(b"\x89PNG")
    payload = calls[0][2]
    assert payload["deductMode"] == "credit_mode"
    assert payload["taskType"] == "text_to_image"
    assert payload["taskInput"]["modelVersion"] == "2_7_flash"
    assert calls[1][2] == {"taskId": "task-image"}


@pytest.mark.asyncio
async def test_tongyi_image_to_image_payload_contains_reference_elements(monkeypatch):
    adapter = backend()
    request = ImageRequest(prompt="改成夜景", provider="tongyi-web", model="wan2.6")
    uploaded = [{"key": "ref-key", "url": "https://cdn.test/ref.png", "resource_id": "r1"}]
    payload = adapter._image_payload(request, uploaded)
    assert payload["taskType"] == "image_to_image"
    task = payload["taskInput"]
    assert task["reference"] == {"type": "ref-element", "refElementIds": ["ref-key"]}
    assert task["elements"]["ref-key"]["payload"]["resourceId"] == "r1"


@pytest.mark.asyncio
async def test_tongyi_video_payload_supports_headtail():
    adapter = backend()
    request = VideoRequest(
        prompt="镜头推进",
        provider="tongyi-web",
        model="wan3.0",
        duration=5,
        resolution="720p",
        aspect_ratio="16:9",
    )
    uploaded = [
        {"key": "first", "url": "https://cdn.test/first.png", "resource_id": "r1"},
        {"key": "last", "url": "https://cdn.test/last.png", "resource_id": "r2"},
    ]
    payload = adapter._video_payload(request, uploaded)
    assert payload["deductMode"] == "credit_mode"
    assert payload["taskType"] == "image_to_video"
    assert payload["taskInput"]["modelVersion"] == "3_0"
    assert payload["taskInput"]["subType"] == "headtail"
    assert payload["taskInput"]["tailImageResourceId"] == "r2"


def test_tongyi_credential_is_private_and_provider_is_native(tmp_path):
    store = video.VideoConfigStore(tmp_path)
    saved = store.save("tongyi-web", "session=private; XSRF-TOKEN=csrf")
    assert saved["auth_type"] == "web_cookie"
    assert saved["configured"] is True
    assert "private" not in json.dumps(store.public())
    assert store.get("tongyi-web")["base_url"] == tongyi.TONGYI_BASE_URL


async def _result(value):
    return value

