"""Seedream request formatting and response handling use mocked transport only."""

import base64
import io
import json
import sys
from pathlib import Path

import pytest
from PIL import Image

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import seedream
from schemas import ImageRequest

PNG = base64.b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAwMCAO+jfS8AAAAASUVORK5CYII="
)


def test_seedream_public_settings_include_current_output_price_without_key(tmp_path):
    settings = seedream.SeedreamConfigStore(tmp_path).public()

    assert settings["output_price_cny"] == 0.12
    assert settings["configured"] is False
    assert "api_key" not in settings


def test_seedream_payload_uses_fixed_model_and_reference_data_uri(tmp_path):
    reference = tmp_path / "reference.png"
    reference.write_bytes(PNG)
    request = ImageRequest(prompt="edit this image", size="2K", aspect_ratio="16:9")
    payload = seedream.SeedreamBackend("secret")._payload(request, [reference])

    assert payload["model"] == "doubao-seedream-5-0-flash-260915"
    assert payload["size"] == "2K"
    assert payload["response_format"] == "b64_json"
    assert payload["image"] == "data:image/png;base64," + base64.b64encode(PNG).decode()
    assert "16:9" in payload["prompt"]


def test_seedream_maps_unsupported_4k_to_2k_and_rejects_invalid_custom_size():
    backend = seedream.SeedreamBackend("secret")
    payload = backend._payload(ImageRequest(prompt="a landscape", size="4K"), [])
    assert payload["size"] == "2K"
    assert "目标分辨率为 2K" in payload["prompt"]
    assert "目标分辨率为 4K" not in payload["prompt"]

    with pytest.raises(seedream.GatewayError) as error:
        backend._payload(ImageRequest(prompt="a landscape", size="64x64"), [])
    assert error.value.code == "seedream_size_unsupported"


def test_oversized_reference_is_resized_to_webp(tmp_path):
    reference = tmp_path / "wide.png"
    Image.new("RGB", (seedream.MAX_REFERENCE_EDGE + 50, 100), "white").save(reference)

    mime_type, contents = seedream.SeedreamBackend._reference_data(reference)

    assert mime_type == "image/webp"
    assert len(contents) <= seedream.MAX_REFERENCE_BYTES
    with Image.open(io.BytesIO(contents)) as image:
        assert max(image.size) <= seedream.MAX_REFERENCE_EDGE


@pytest.mark.asyncio
async def test_seedream_posts_to_ark_and_saves_all_requested_images(
    monkeypatch, tmp_path
):
    requests = []

    class Response(io.BytesIO):
        def __enter__(self):
            return self

        def __exit__(self, *args):
            self.close()

    def fake_urlopen(request, timeout):
        requests.append((request, timeout, json.loads(request.data)))
        body = json.dumps(
            {
                "data": [{"b64_json": base64.b64encode(PNG).decode()}],
                "usage": {"generated_images": 1},
            }
        )
        return Response(body.encode())

    monkeypatch.setattr(seedream.urllib.request, "urlopen", fake_urlopen)
    backend = seedream.SeedreamBackend("ark-secret", timeout=17)
    folder = tmp_path / "images"
    folder.mkdir()
    billed_images = []
    paths = await backend.generate(
        ImageRequest(prompt="a red cup", n=2, prompt_mode="raw"),
        folder,
        2,
        on_billed_images=billed_images.append,
    )

    assert len(paths) == len(requests) == 2
    assert billed_images == [1, 1]
    assert all(path.read_bytes() == PNG for path in paths)
    request, timeout, payload = requests[0]
    assert request.full_url == (
        "https://ark.cn-beijing.volces.com/api/v3/images/generations"
    )
    assert request.get_header("Authorization") == "Bearer ark-secret"
    assert timeout == 17
    assert payload["model"] == seedream.SEEDREAM_MODEL
    assert payload["prompt"] == "a red cup"
    assert payload["response_format"] == "b64_json"


@pytest.mark.asyncio
async def test_seedream_keeps_partial_images_when_a_later_request_fails(
    monkeypatch, tmp_path
):
    calls = 0

    class Response(io.BytesIO):
        def __enter__(self):
            return self

        def __exit__(self, *args):
            self.close()

    def fake_urlopen(request, timeout):
        nonlocal calls
        calls += 1
        if calls == 2:
            raise seedream.urllib.error.HTTPError(
                request.full_url, 429, "rate limited", {}, io.BytesIO()
            )
        body = json.dumps(
            {
                "data": [{"b64_json": base64.b64encode(PNG).decode()}],
                "usage": {"generated_images": 1},
            }
        )
        return Response(body.encode())

    monkeypatch.setattr(seedream.urllib.request, "urlopen", fake_urlopen)
    folder = tmp_path / "images"
    folder.mkdir()
    billed_images = []

    with pytest.raises(seedream.GatewayError) as error:
        await seedream.SeedreamBackend("ark-secret").generate(
            ImageRequest(prompt="a red cup", n=2),
            folder,
            2,
            on_billed_images=billed_images.append,
        )

    assert error.value.code == "seedream_rate_limited"
    assert billed_images == [1]
    assert len(error.value.partial_paths) == 1
    assert error.value.partial_paths[0].read_bytes() == PNG
