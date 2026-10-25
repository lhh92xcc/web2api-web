"""Recraft web-session connector tests; no real Recraft request is made."""

import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import recraft
from schemas import ImageRequest
from storage import GatewayError
from test_gateway import PNG


class FakeResponse:
    def __init__(self, body, status=200, content_type="application/json"):
        self.body = body
        self.status = status
        self.headers = {"Content-Type": content_type}

    def read(self, _limit=-1):
        return self.body

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return False


def test_recraft_store_redacts_token(tmp_path):
    store = recraft.RecraftConfigStore(tmp_path)
    public = store.save("Bearer recraft-secret-token")
    assert public["configured"] is True
    assert "recraft-secret-token" not in json.dumps(public)
    assert store.path.stat().st_mode & 0o777 == 0o600


@pytest.mark.asyncio
async def test_recraft_checks_free_credits_polls_and_downloads(tmp_path, monkeypatch):
    operation = "operation-123"
    responses = [
        FakeResponse(json.dumps({"plan": {"is_free": True}, "subscription_credits": 4}).encode()),
        FakeResponse(json.dumps({"operation_id": operation}).encode()),
        FakeResponse(json.dumps({"status": "completed", "images": [{"image_id": "image-123"}]}).encode()),
        FakeResponse(PNG, content_type="image/png"),
    ]
    seen = []

    def fake_urlopen(request, timeout):
        seen.append((request.full_url, dict(request.headers), request.data))
        return responses.pop(0)

    monkeypatch.setattr(recraft.urllib.request, "urlopen", fake_urlopen)
    request = ImageRequest(prompt="white ceramic cup", provider=recraft.RECRAFT_ID, n=1)
    folder = tmp_path / "images"
    folder.mkdir()
    paths = await recraft.RecraftBackend("recraft-secret-token", timeout=7).generate(
        request, folder, 1
    )

    assert len(paths) == 1
    assert paths[0].read_bytes() == PNG
    assert "/users/me" in seen[0][0]
    assert "/queue_recraft/prompt_to_image" in seen[1][0]
    assert "/poll_recraft?" in seen[2][0]
    assert "/image/image-123" in seen[3][0]
    assert "Bearer recraft-secret-token" in seen[0][1]["Authorization"]
    payload = json.loads(seen[1][2])
    assert payload["transform_model"] == recraft.RECRAFT_DEFAULT_MODEL
    assert payload["num_images_per_prompt"] == 1


@pytest.mark.asyncio
async def test_recraft_refuses_paid_plan_before_generation(tmp_path, monkeypatch):
    called = []

    def fake_urlopen(request, timeout):
        called.append(request.full_url)
        return FakeResponse(json.dumps({"plan": {"is_free": False}, "subscription_credits": 100}).encode())

    monkeypatch.setattr(recraft.urllib.request, "urlopen", fake_urlopen)
    request = ImageRequest(prompt="cat", provider=recraft.RECRAFT_ID)
    with pytest.raises(GatewayError) as error:
        await recraft.RecraftBackend("token", timeout=7).generate(request, tmp_path, 1)
    assert error.value.code == "recraft_free_plan_required"
    assert len(called) == 1


@pytest.mark.asyncio
async def test_recraft_refuses_when_only_unknown_or_missing_free_credits(tmp_path, monkeypatch):
    def fake_urlopen(request, timeout):
        return FakeResponse(json.dumps({"plan": {"is_free": True}, "credits": 100}).encode())

    monkeypatch.setattr(recraft.urllib.request, "urlopen", fake_urlopen)
    request = ImageRequest(prompt="cat", provider=recraft.RECRAFT_ID)
    with pytest.raises(GatewayError) as error:
        await recraft.RecraftBackend("token", timeout=7).generate(request, tmp_path, 1)
    assert error.value.code == "recraft_quota_unknown"
