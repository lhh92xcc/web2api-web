"""Jimeng web-session adapter tests use synthetic first-party responses only."""

import asyncio
import json
import sys
from pathlib import Path

import pytest
import time
from fastapi.testclient import TestClient

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import engine as engine_module
import video
from engine import Settings
from jimeng import JimengWebBackend
from schemas import ImageRequest
from server import create_app


def test_jimeng_session_is_private_and_fixed_to_first_party_host(tmp_path):
    store = video.VideoConfigStore(tmp_path)
    saved = store.save("jimeng-web", "sessionid-private")

    assert saved["auth_type"] == "web_session"
    assert saved["configured"] is True
    assert saved["images"] is True
    assert saved["image_models"]
    assert "sessionid-private" not in json.dumps(store.public())
    config = store.get("jimeng-web")
    assert config["credential"] == "sessionid-private"
    assert config["base_url"] == "https://jimeng.jianying.com"
    assert config["allow_paid"] is False


def test_jimeng_free_bucket_is_allowed_even_with_paid_balance(monkeypatch):
    backend = JimengWebBackend({"credential": "sessionid-private"}, timeout=3)

    async def credits():
        return {
            "provider": "jimeng-web",
            "credits": {
                "gift_credit": 10,
                "purchase_credit": 99,
                "vip_credit": 50,
                "total_available": 159,
            },
        }

    monkeypatch.setattr(backend, "credits", credits)
    result = asyncio.run(backend._require_free_credit())
    assert result["gift_credit"] == 10


def test_jimeng_never_uses_paid_bucket_when_free_bucket_is_empty(monkeypatch):
    backend = JimengWebBackend({"credential": "sessionid-private"}, timeout=3)

    async def credits():
        return {
            "provider": "jimeng-web",
            "credits": {
                "gift_credit": 0,
                "purchase_credit": 99,
                "vip_credit": 50,
                "total_available": 149,
            },
        }

    async def claim_daily_credit():
        return await credits()

    monkeypatch.setattr(backend, "credits", credits)
    monkeypatch.setattr(backend, "claim_daily_credit", claim_daily_credit)
    with pytest.raises(Exception) as error:
        asyncio.run(backend._require_free_credit())
    assert error.value.code == "jimeng_free_quota_empty"


def test_jimeng_image_request_uses_web_session_and_reference_path(monkeypatch, tmp_path):
    backend = JimengWebBackend({"credential": "sessionid-private"}, timeout=3)
    calls = []
    reference = tmp_path / "reference.png"
    reference.write_bytes(b"synthetic-reference")

    async def request(method, path, payload=None, params=None):
        calls.append((method, path, payload, params))
        if path == "/commerce/v1/benefits/user_credit":
            return {"credit": {"gift_credit": 8, "purchase_credit": 0, "vip_credit": 0}}
        if path == "/mweb/v1/aigc_draft/generate":
            return {"aigc_data": {"history_record_id": "history-1"}}
        if path == "/mweb/v1/get_history_by_ids":
            return {
                "history_list": [
                    {
                        "history_id": "history-1",
                        "status": 10,
                        "item_list": [
                            {
                                "image": {
                                    "large_images": [
                                        {"image_url": "https://cdn.test/image.webp"}
                                    ]
                                }
                            }
                        ],
                    }
                ]
            }
        raise AssertionError(path)

    monkeypatch.setattr(backend, "_request", request)
    monkeypatch.setattr(
        backend,
        "_call_upload_proof",
        lambda path, scene: "uploaded-reference-uri",
    )

    def download(url, path, limit, kind):
        path.write_bytes(b"RIFFxxxxWEBP")
        return path

    monkeypatch.setattr(backend, "_download_media", download)
    request_body = ImageRequest(
        provider="jimeng-web",
        model="jimeng-seedream-5-flash",
        prompt="把参考图改成白底产品摄影",
        reference_images=[],
    )
    folder = tmp_path / "output"
    folder.mkdir()
    paths = asyncio.run(backend.generate_image(request_body, folder, [reference]))

    assert len(paths) == 1 and paths[0].is_file()
    generate_call = next(call for call in calls if call[1] == "/mweb/v1/aigc_draft/generate")
    payload = generate_call[2]
    draft = json.loads(payload["draft_content"])
    component = draft["component_list"][0]
    assert component["generate_type"] == "blend"
    ability = component["abilities"]["blend"]["ability_list"][0]
    assert ability["image_uri_list"] == ["uploaded-reference-uri"]
    assert ability["image_list"][0]["image_uri"] == "uploaded-reference-uri"
    assert payload["extend"]["root_model"] == "high_aes_general_v50_flash"


def test_jimeng_image_provider_uses_native_backend_without_browser_bridge(
    monkeypatch, tmp_path
):
    class FakeBackend:
        async def generate_image(self, request, folder, reference_paths):
            path = folder / "jimeng-native.png"
            path.write_bytes(b"\x89PNG\r\n\x1a\nsynthetic")
            return [path]

    monkeypatch.setattr(engine_module, "video_backend_for", lambda *args: FakeBackend())
    settings = Settings(tmp_path / "state", tmp_path / "images", timeout=3)
    with TestClient(create_app(settings)) as client:
        saved = client.put(
            "/api/video-providers/jimeng-web",
            json={"credential": "sessionid-private", "auth_type": "web_session"},
        )
        assert saved.status_code == 200, saved.text
        providers = client.get("/api/providers").json()["data"]
        jimeng = next(item for item in providers if item["id"] == "jimeng-web")
        assert jimeng["mode"] == "native"
        assert jimeng["images"] is True
        assert jimeng["state"] == "ready"

        submitted = client.post(
            "/api/jobs",
            json={
                "provider": "jimeng-web",
                "model": "jimeng-seedream-5-flash",
                "prompt": "生成白底产品图",
            },
        )
        assert submitted.status_code == 202, submitted.text
        job_id = submitted.json()["id"]
        deadline = time.monotonic() + 3
        while time.monotonic() < deadline:
            job = client.get(f"/api/jobs/{job_id}").json()
            if job["status"] in {"succeeded", "failed"}:
                break
            time.sleep(0.01)
        assert job["status"] == "succeeded", job
        assert job["images"]
