"""ImageFX web-session connector and provider registration tests."""

import base64
import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import imagefx
from engine import Settings
from schemas import ImageRequest
from server import create_app
from storage import GatewayError
from test_gateway import PNG
from fastapi.testclient import TestClient


class FakeResponse:
    def __init__(self, body, status=200):
        self.body = body
        self.status = status

    def read(self, _limit=-1):
        return self.body

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return False


def test_imagefx_store_redacts_credential(tmp_path):
    store = imagefx.ImageFxConfigStore(tmp_path)
    public = store.save("__Secure-1PSID=secret; SID=private")
    assert public["configured"] is True
    assert "secret" not in json.dumps(public)
    assert "private" not in json.dumps(public)
    assert store.path.stat().st_mode & 0o777 == 0o600


@pytest.mark.asyncio
async def test_imagefx_refreshes_cookie_session_and_saves_images(tmp_path, monkeypatch):
    responses = [
        FakeResponse(json.dumps({"access_token": "short-lived-token"}).encode()),
        FakeResponse(
            json.dumps(
                {
                    "imagePanels": [
                        {
                            "generatedImages": [
                                {"encodedImage": base64.b64encode(PNG).decode()}
                            ]
                        }
                    ]
                }
            ).encode()
        ),
    ]
    seen = []

    def fake_urlopen(request, timeout):
        seen.append((request.full_url, dict(request.headers), request.data))
        return responses.pop(0)

    monkeypatch.setattr(imagefx.urllib.request, "urlopen", fake_urlopen)
    backend = imagefx.ImageFxBackend("__Secure-1PSID=secret; SID=private", timeout=7)
    request = ImageRequest(prompt="white ceramic cup", model="default", n=1)
    folder = tmp_path / "images"
    folder.mkdir()
    paths = await backend.generate(request, folder, 1)

    assert len(paths) == 1
    assert paths[0].read_bytes() == PNG
    assert seen[0][0] == imagefx.IMAGEFX_SESSION_URL
    assert seen[1][0] == imagefx.IMAGEFX_GENERATE_URL
    assert "private" in seen[0][1].get("Cookie", "")
    payload = json.loads(seen[1][2])
    assert payload["modelInput"]["modelNameType"] == "IMAGEN_3_5"
    assert payload["userInput"]["candidatesCount"] == 1


@pytest.mark.asyncio
async def test_imagefx_rejects_reference_images(tmp_path):
    backend = imagefx.ImageFxBackend("Bearer token")
    request = ImageRequest(
        prompt="edit",
        model="default",
        n=1,
        reference_images=["ref_" + "a" * 32],
    )
    with pytest.raises(GatewayError) as error:
        await backend.generate(request, tmp_path, 1, [tmp_path / "reference.png"])
    assert error.value.code == "imagefx_references_unsupported"


def test_web_image_provider_routes_are_local_and_redacted(tmp_path):
    settings = Settings(tmp_path / "state", tmp_path / "images")

    class Backend:
        def __init__(self, *_args):
            self.ready = False

        async def connect(self):
            return []

        async def close(self):
            return None

    with TestClient(create_app(settings, Backend)) as client:
        key = client.post("/api/keys", json={"name": "imagefx-tests"}).json()["key"]
        client.headers["Authorization"] = "Bearer " + key
        providers = client.get("/api/web-image-providers")
        assert providers.status_code == 200
        assert {row["id"] for row in providers.json()["data"]} == {
            "bing",
            "imagefx-web",
            "recraft-web",
            "leonardo-web",
        }
        secret = "__Secure-1PSID=do-not-return"
        saved = client.put(
            "/api/web-image-providers/imagefx-web",
            json={"credential": secret},
        )
        assert saved.status_code == 200
        assert secret not in saved.text
        assert secret not in client.get("/api/providers").text
        cleared = client.delete("/api/web-image-providers/imagefx-web")
        assert cleared.status_code == 200
        assert cleared.json()["configured"] is False
