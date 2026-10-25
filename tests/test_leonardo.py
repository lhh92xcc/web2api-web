"""Leonardo web-session connector tests; no real Leonardo request is made."""

import base64
import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import leonardo
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


def token():
    payload = base64.urlsafe_b64encode(
        json.dumps({"sub": "leonardo-sub"}).encode()
    ).decode().rstrip("=")
    return f"header.{payload}.signature"


def details(tokens=4, plan="FREE"):
    return {
        "data": {
            "users": [
                {
                    "id": "user-123",
                    "username": "tester",
                    "user_details": {
                        "plan": plan,
                        "subscriptionTokens": tokens,
                        "paidTokens": 999,
                        "apiCredit": 999,
                    },
                }
            ]
        }
    }


@pytest.mark.asyncio
async def test_leonardo_checks_free_tokens_polls_and_downloads(tmp_path, monkeypatch):
    responses = [
        FakeResponse(json.dumps(details()).encode()),
        FakeResponse(json.dumps({"data": {"sdGenerationJob": {"generationId": "gen-123"}}}).encode()),
        FakeResponse(json.dumps({"data": {"generations": [{"id": "gen-123", "status": "PENDING", "generated_images": []}]}}).encode()),
        FakeResponse(json.dumps({"data": {"generations": [{"id": "gen-123", "status": "COMPLETE", "generated_images": [{"id": "image-123", "url": "https://cdn.leonardo.ai/image.png"}]}]}}).encode()),
        FakeResponse(PNG, content_type="image/png"),
    ]
    seen = []

    def fake_urlopen(request, timeout):
        seen.append((request.full_url, dict(request.headers), request.data))
        return responses.pop(0)

    monkeypatch.setattr(leonardo.urllib.request, "urlopen", fake_urlopen)
    monkeypatch.setattr(leonardo.time, "sleep", lambda _seconds: None)
    request = ImageRequest(prompt="white ceramic cup", provider=leonardo.LEONARDO_ID, n=1)
    folder = tmp_path / "images"
    folder.mkdir()
    paths = await leonardo.LeonardoBackend(token(), timeout=7).generate(request, folder, 1)

    assert len(paths) == 1
    assert paths[0].read_bytes() == PNG
    operations = [json.loads(item[2])["operationName"] for item in seen[:4]]
    assert operations == [
        "GetUserDetails",
        "CreateSDGenerationJob",
        "GetAIGenerationFeed",
        "GetAIGenerationFeed",
    ]
    assert "Bearer " + token() in seen[0][1]["Authorization"]
    payload = json.loads(seen[1][2])
    assert payload["variables"]["arg1"]["num_images"] == 1
    assert payload["variables"]["arg1"]["modelId"] == "b820ea11-02bf-4652-97ae-9ac0cc00593d"


def test_leonardo_store_redacts_token(tmp_path):
    store = leonardo.LeonardoConfigStore(tmp_path)
    public = store.save("Bearer " + token())
    assert public["configured"] is True
    assert token() not in json.dumps(public)
    assert store.path.stat().st_mode & 0o777 == 0o600


@pytest.mark.asyncio
async def test_leonardo_refuses_paid_plan_before_generation(tmp_path, monkeypatch):
    called = []

    def fake_urlopen(request, timeout):
        called.append(request.full_url)
        return FakeResponse(json.dumps(details(plan="PRO", tokens=100)).encode())

    monkeypatch.setattr(leonardo.urllib.request, "urlopen", fake_urlopen)
    request = ImageRequest(prompt="cat", provider=leonardo.LEONARDO_ID)
    with pytest.raises(GatewayError) as error:
        await leonardo.LeonardoBackend(token(), timeout=7).generate(request, tmp_path, 1)
    assert error.value.code == "leonardo_free_plan_required"
    assert len(called) == 1


@pytest.mark.asyncio
async def test_leonardo_refuses_when_free_tokens_are_unknown(tmp_path, monkeypatch):
    def fake_urlopen(request, timeout):
        return FakeResponse(json.dumps(details(tokens=None)).encode())

    monkeypatch.setattr(leonardo.urllib.request, "urlopen", fake_urlopen)
    request = ImageRequest(prompt="cat", provider=leonardo.LEONARDO_ID)
    with pytest.raises(GatewayError) as error:
        await leonardo.LeonardoBackend(token(), timeout=7).generate(request, tmp_path, 1)
    assert error.value.code == "leonardo_quota_unknown"
