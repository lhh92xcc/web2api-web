"""Video provider adapters and the local video job route use mocked upstreams."""

import asyncio
import base64
import hashlib
import json
import sys
import time
import urllib.parse
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import engine as engine_module
import video
from engine import Settings
from schemas import ImageRequest, VideoRequest
from server import create_app

PNG = base64.b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAwMCAO+jfS8AAAAASUVORK5CYII="
)


async def _async_noop():
    return None


async def _async_result(value):
    return value


def reference(tmp_path, name="reference.png"):
    path = tmp_path / name
    path.write_bytes(PNG)
    return path


def mocked_task_transport(monkeypatch, backend, responses):
    calls = []

    def request(method, url, payload=None, headers=None):
        calls.append((method, url, payload, headers or {}))
        response = responses[len(calls) - 1]
        return 200, json.dumps(response).encode()

    monkeypatch.setattr(backend, "_request", request)
    monkeypatch.setattr(
        backend,
        "_download",
        lambda url, path: path.write_bytes(b"fake-mp4"),
    )
    monkeypatch.setattr(video, "POLL_INTERVAL", 0)
    return calls


@pytest.mark.asyncio
async def test_minimax_uses_multimodal_reference_roles(monkeypatch, tmp_path):
    backend = video.MiniMaxVideoBackend(
        {**video.VIDEO_PROVIDERS["minimax"], "api_key": "mm-secret", "base_url": "https://mm.test"},
        3,
    )
    calls = mocked_task_transport(
        monkeypatch,
        backend,
        [{"task_id": "task-1"}, {"task": {"status": "succeeded", "content": {"url": "https://cdn.test/out.mp4"}}}],
    )
    request = VideoRequest(prompt="人物从左向右走", provider="minimax", duration=5, reference_images=[])
    path = await backend.generate(request, tmp_path, [reference(tmp_path, "a.png"), reference(tmp_path, "b.png")])

    assert path.read_bytes() == b"fake-mp4"
    payload = calls[0][2]
    assert payload["model"] == "MiniMax-H3"
    assert [item["role"] for item in payload["content"][1:]] == ["first_frame", "last_frame"]
    assert calls[0][0] == "POST" and calls[1][0] == "GET"


@pytest.mark.asyncio
async def test_vidu_selects_reference_endpoint_by_image_count(monkeypatch, tmp_path):
    for count, endpoint in ((1, "/ent/v2/img2video"), (2, "/ent/v2/start-end2video"), (3, "/ent/v2/reference2video")):
        backend = video.ViduVideoBackend(
            {**video.VIDEO_PROVIDERS["vidu"], "api_key": "vidu-secret", "base_url": "https://vidu.test"},
            3,
        )
        calls = mocked_task_transport(
            monkeypatch,
            backend,
            [{"task_id": f"task-{count}"}, {"state": "success", "creations": [{"url": "https://cdn.test/out.mp4"}]}],
        )
        refs = [reference(tmp_path, f"{count}-{index}.png") for index in range(count)]
        await backend.generate(VideoRequest(prompt="动作", provider="vidu"), tmp_path, refs)
        assert calls[0][1].endswith(endpoint)
        assert calls[0][2]["model"] == "viduq3-turbo"
        if count == 1:
            assert isinstance(calls[0][2]["images"], str)
        elif count == 2:
            assert isinstance(calls[0][2]["images"], list)


@pytest.mark.asyncio
async def test_vidu_q2_uses_q2_model_and_image_array(monkeypatch, tmp_path):
    backend = video.ViduVideoBackend(
        {**video.VIDEO_PROVIDERS["vidu"], "api_key": "vidu-secret", "base_url": "https://vidu.test"},
        3,
    )
    calls = mocked_task_transport(
        monkeypatch,
        backend,
        [{"task_id": "q2-task"}, {"state": "success", "creations": [{"url": "https://cdn.test/q2.mp4"}]}],
    )
    await backend.generate(
        VideoRequest(prompt="动作", provider="vidu", model="viduq2-turbo", duration=2),
        tmp_path,
        [reference(tmp_path, "q2.png")],
    )
    assert calls[0][1].endswith("/ent/v2/img2video")
    assert calls[0][2]["model"] == "viduq2-turbo"
    assert isinstance(calls[0][2]["images"], list)


@pytest.mark.asyncio
async def test_seedance_marks_first_and_last_frames(monkeypatch, tmp_path):
    backend = video.SeedanceVideoBackend(
        {**video.VIDEO_PROVIDERS["seedance"], "api_key": "seedance-secret", "base_url": "https://ark.test"},
        3,
    )
    calls = mocked_task_transport(
        monkeypatch,
        backend,
        [{"id": "task-1"}, {"status": "succeeded", "video_url": "https://cdn.test/out.mp4"}],
    )
    refs = [reference(tmp_path, "first.png"), reference(tmp_path, "last.png")]
    await backend.generate(VideoRequest(prompt="转场", provider="seedance"), tmp_path, refs)
    content = calls[0][2]["content"]
    assert [item["role"] for item in content[1:]] == ["first_frame", "last_frame"]
    assert calls[0][3]["Authorization"] == "Bearer seedance-secret"


@pytest.mark.asyncio
async def test_bailian_uses_resolution_parameter(monkeypatch, tmp_path):
    backend = video.DashScopeVideoBackend(
        {**video.VIDEO_PROVIDERS["bailian"], "api_key": "dash-secret", "base_url": "https://dash.test"},
        3,
    )
    calls = mocked_task_transport(
        monkeypatch,
        backend,
        [{"output": {"task_id": "task-1"}}, {"output": {"task_status": "SUCCEEDED", "video_url": "https://cdn.test/out.mp4"}}],
    )
    await backend.generate(VideoRequest(prompt="镜头推进", provider="bailian", resolution="1080p"), tmp_path, [])
    assert calls[0][2]["parameters"]["resolution"] == "1080P"
    assert calls[0][3]["Authorization"] == "Bearer dash-secret"


@pytest.mark.asyncio
async def test_hailuo_web_signs_requests_and_polls_first_party_feed(monkeypatch, tmp_path):
    config = {
        **video.VIDEO_PROVIDERS["hailuo-web"],
        "credential": "web-token-secret",
        "uuid": "uuid-1",
        "device_id": "device-1",
    }
    backend = video.HailuoWebVideoBackend(config, 3)
    responses = [
        {"statusInfo": {"code": 0}, "data": {"id": "feed-1"}},
        {
            "statusInfo": {"code": 0},
            "data": {
                "commonInfo": {"status": 2},
                "metaInfo": {
                    "videoMetaInfo": {
                        "mediaInfo": {
                            "downloadURL": {
                                "withoutWatermarkURL": "https://cdn.test/hailuo.mp4"
                            }
                        }
                    }
                },
            },
        },
    ]
    calls = []

    class FakeResponse:
        status = 200

        def __init__(self, body):
            self.body = body

        def read(self, _limit):
            return self.body

        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return False

    def urlopen(request, timeout):
        calls.append(request)
        return FakeResponse(json.dumps(responses.pop(0)).encode())

    monkeypatch.setattr(video.urllib.request, "urlopen", urlopen)
    monkeypatch.setattr(
        backend, "_download", lambda url, path: path.write_bytes(b"fake-mp4")
    )
    monkeypatch.setattr(backend, "_require_free_credit", lambda: _async_noop())
    monkeypatch.setattr(video, "POLL_INTERVAL", 0)
    path = await backend.generate(
        VideoRequest(prompt="人物向镜头走来", provider="hailuo-web"), tmp_path, []
    )

    assert path.read_bytes() == b"fake-mp4"
    assert calls[0].method == "POST"
    assert urllib.parse.urlsplit(calls[0].full_url).path == "/v2/api/multimodal/generate/video"
    assert urllib.parse.parse_qs(urllib.parse.urlsplit(calls[1].full_url).query)["id"] == ["feed-1"]
    assert calls[0].headers["Token"] == "web-token-secret"
    assert calls[0].headers["Yy"]
    body = json.loads(calls[0].data)
    assert body["parameter"]["modelID"] == "hailuo3.0-t2v"
    assert body["parameter"]["resolution"] == "768"

    timestamp = 1700000000000
    signed_path, signature, body = backend._signature(
        "/api/feed/detail",
        {"id": "feed-1"},
        "GET",
        None,
        timestamp,
        config,
    )
    assert body == "{}"
    expected = hashlib.md5(
        (
            urllib.parse.quote(signed_path, safe="-_.!~*'()")
            + "_{}"
            + hashlib.md5(str(timestamp).encode()).hexdigest()
            + "ooui"
        ).encode()
    ).hexdigest()
    assert signature == expected


@pytest.mark.asyncio
async def test_hailuo_web_image_endpoint_uses_free_guard_and_saves_assets(monkeypatch, tmp_path):
    config = {
        **video.VIDEO_PROVIDERS["hailuo-web"],
        "credential": "web-token-secret",
        "uuid": "uuid-1",
        "device_id": "device-1",
    }
    backend = video.HailuoWebVideoBackend(config, 3)
    responses = [
        {"statusInfo": {"code": 0}, "data": {"id": "image-feed-1"}},
        {
            "statusInfo": {"code": 0},
            "data": {
                "commonInfo": {"status": 2},
                "metaInfo": {
                    "imageMetaInfo": {
                        "images": [
                            {"url": "https://cdn.test/one.png"},
                            {"url": "https://cdn.test/two.png"},
                        ]
                    }
                },
            },
        },
    ]
    calls = []

    def request(method, path, payload=None, headers=None, params=None):
        calls.append((method, path, payload, params))
        return 200, json.dumps(responses.pop(0)).encode()

    monkeypatch.setattr(backend, "_request", request)
    monkeypatch.setattr(backend, "_require_free_credit", lambda: _async_noop())
    monkeypatch.setattr(
        backend,
        "_download_image",
        lambda url, path: path.write_bytes(PNG),
    )
    result = await backend.generate_image(
        ImageRequest(prompt="一只猫", provider="hailuo-web", n=2),
        tmp_path,
        2,
        [],
    )

    assert len(result) == 2
    assert all(path.read_bytes() == PNG for path in result)
    assert calls[0][1] == "/v2/api/multimodal/generate/image"
    assert calls[0][2]["parameter"]["modelID"] == "gpt-image-2"
    assert calls[1][3] == {"id": "image-feed-1"}


@pytest.mark.asyncio
async def test_hailuo_credits_split_free_and_purchased_buckets(monkeypatch):
    backend = video.HailuoWebVideoBackend(
        {**video.VIDEO_PROVIDERS["hailuo-web"], "credential": "private-token"}, 3
    )

    monkeypatch.setattr(
        backend,
        "_request",
        lambda method, path, payload=None, headers=None, params=None: (
            200,
            json.dumps(
                {
                    "statusInfo": {"code": 0},
                    "data": {
                        "total_credit": 117,
                        "sub_credits": [
                            {"credit_type": 1, "credit": 99, "record": ["private"]},
                            {"credit_type": 2, "credit": 12},
                            {"credit_type": 3, "credit": 6},
                        ],
                        "nickname": "private profile",
                    },
                }
            ).encode(),
        ),
    )

    result = await backend.credits()
    assert result == {
        "provider": "hailuo-web",
        "credits": {
            "total_available": 117,
            "user_buy": 99,
            "subscribe_give": 12,
            "login_give": 6,
            "free_available": 18,
            "free_buckets_known": True,
        },
    }
    assert "private profile" not in json.dumps(result)
    assert "record" not in json.dumps(result)


@pytest.mark.asyncio
async def test_hailuo_free_guard_does_not_use_purchased_credits(monkeypatch, tmp_path):
    backend = video.HailuoWebVideoBackend(
        {**video.VIDEO_PROVIDERS["hailuo-web"], "credential": "private-token"}, 3
    )
    monkeypatch.setattr(
        backend,
        "credits",
        lambda: _async_result(
            {
                "provider": "hailuo-web",
                "credits": {
                    "total_available": 99,
                    "user_buy": 99,
                    "subscribe_give": 0,
                    "login_give": 0,
                    "free_available": 0,
                    "free_buckets_known": True,
                },
            }
        ),
    )
    with pytest.raises(video.GatewayError) as error:
        await backend.generate(
            VideoRequest(prompt="人物走路", provider="hailuo-web"), tmp_path, []
        )
    assert error.value.code == "video_quota_exceeded"


@pytest.mark.asyncio
async def test_hailuo_web_reference_frames_are_uploaded_before_submit(monkeypatch, tmp_path):
    config = {
        **video.VIDEO_PROVIDERS["hailuo-web"],
        "credential": "token",
        "uuid": "uuid-1",
        "device_id": "device-1",
    }
    backend = video.HailuoWebVideoBackend(config, 3)
    calls = []

    async def fake_upload(path, frame_type):
        calls.append((path.name, frame_type))
        return {
            "id": f"file-{frame_type}",
            "url": f"https://oss.test/file-{frame_type}.png",
            "frameType": frame_type,
            "assetFileType": "image",
        }

    monkeypatch.setattr(backend, "_upload_reference", fake_upload)
    monkeypatch.setattr(backend, "_require_free_credit", lambda: _async_noop())
    submitted = []

    def request(method, path, payload=None, headers=None, params=None):
        submitted.append((method, path, payload))
        if path == "/v2/api/multimodal/generate/video":
            body = {"statusInfo": {"code": 0}, "data": {"id": "feed-2"}}
        else:
            body = {
                "statusInfo": {"code": 0},
                "data": {
                    "commonInfo": {"status": 2},
                    "metaInfo": {
                        "videoMetaInfo": {
                            "mediaInfo": {"url": "https://cdn.test/out.mp4"}
                        }
                    },
                },
            }
        return 200, json.dumps(body).encode()

    monkeypatch.setattr(backend, "_request", request)
    monkeypatch.setattr(
        backend, "_download", lambda url, path: path.write_bytes(b"fake-mp4")
    )
    first = reference(tmp_path, "first.png")
    last = reference(tmp_path, "last.png")
    path = await backend.generate(
        VideoRequest(prompt="转场", provider="hailuo-web", duration=5),
        tmp_path,
        [first, last],
    )
    assert calls == [("first.png", 0), ("last.png", 1)]
    assert path.read_bytes() == b"fake-mp4"
    file_list = submitted[0][2]["parameter"]["fileList"]
    assert [item["frameType"] for item in file_list] == [0, 1]
    assert submitted[0][2]["parameter"]["referenceMode"] == "start-end-frames"


def test_hailuo_web_token_is_saved_without_public_secret(tmp_path):
    store = video.VideoConfigStore(tmp_path)
    saved = store.save("hailuo-web", "private-web-token")
    assert saved["auth_type"] == "web_token"
    assert saved["configured"] is True
    assert "private-web-token" not in json.dumps(store.public())
    assert store.get("hailuo-web")["base_url"] == "https://hailuoai.video"


def test_pixverse_web_token_is_private_and_uses_fixed_first_party_host(tmp_path):
    store = video.VideoConfigStore(tmp_path)
    saved = store.save("pixverse-web", "private-pixverse-token")

    assert saved["auth_type"] == "web_token"
    assert saved["supports_credits"] is True
    assert saved["configured"] is True
    assert "private-pixverse-token" not in json.dumps(store.public())
    assert store.get("pixverse-web")["base_url"] == (
        "https://cn-app-api.pixverseai.cn/creative_platform"
    )
    assert store.get("pixverse-web")["device_id"]


def test_kling_web_cookie_is_private_and_uses_fixed_first_party_host(tmp_path):
    store = video.VideoConfigStore(tmp_path)
    cookie = "session=private-kling-cookie"
    saved = store.save("kling-web", cookie, auth_type="web_cookie")

    assert saved["auth_type"] == "web_cookie"
    assert saved["supports_credits"] is True
    assert saved["configured"] is True
    assert cookie not in json.dumps(store.public())
    assert store.get("kling-web")["base_url"] == "https://klingai.com"


@pytest.mark.asyncio
async def test_kling_web_submit_polls_and_downloads_video(monkeypatch, tmp_path):
    backend = video.KlingWebVideoBackend(
        {**video.VIDEO_PROVIDERS["kling-web"], "credential": "session=secret"}, 3
    )
    calls = []

    def web_request(method, path, payload=None, params=None):
        calls.append((method, path, payload, params))
        if path == "/api/task/submit":
            return {"task": {"id": "kling-task-1"}}
        return {
            "taskInfos": [
                {
                    "id": "kling-task-1",
                    "status": 99,
                    "videoUrl": "https://cdn.test/kling.mp4",
                }
            ]
        }

    monkeypatch.setattr(backend, "_web_request", web_request)
    monkeypatch.setattr(
        backend, "_download", lambda url, path: path.write_bytes(b"fake-mp4")
    )
    monkeypatch.setattr(video, "POLL_INTERVAL", 0)

    result = await backend.generate(
        VideoRequest(
            prompt="人物自然走向镜头",
            provider="kling-web",
            model="2.1",
            duration=5,
            aspect_ratio="16:9",
        ),
        tmp_path,
        [],
    )

    assert result.read_bytes() == b"fake-mp4"
    submitted = calls[0][2]
    assert submitted["type"] == "m2v_txt2video"
    assert {item["name"]: item["value"] for item in submitted["arguments"]}["kling_version"] == "2.1"
    assert calls[1][1] == "/api/task/status/batch"


@pytest.mark.asyncio
async def test_kling_web_credits_only_return_numeric_balance_fields(monkeypatch):
    backend = video.KlingWebVideoBackend(
        {**video.VIDEO_PROVIDERS["kling-web"], "credential": "session=secret"}, 3
    )

    monkeypatch.setattr(
        backend,
        "_web_request",
        lambda method, path, payload=None, params=None: {
            "data": {
                "userPoints": 20,
                "userTickets": 3,
                "freePoints": 5,
                "nickname": "private profile",
            }
        },
    )
    result = await backend.credits()

    assert result == {
        "provider": "kling-web",
        "credits": {
            "points": 20,
            "tickets": 3,
            "free_points": 5,
            "total_available": None,
        },
    }
    assert "private profile" not in json.dumps(result)


@pytest.mark.asyncio
async def test_pixverse_credits_use_web_token_and_return_only_balance_fields(monkeypatch):
    backend = video.PixverseWebVideoBackend(
        {
            **video.VIDEO_PROVIDERS["pixverse-web"],
            "credential": "private-pixverse-token",
            "device_id": "device-123",
        },
        3,
    )
    captured = []

    class FakeResponse:
        status = 200

        def read(self, _limit):
            return json.dumps(
                {
                    "ErrCode": 0,
                    "Resp": {
                        "credit_daily": 12,
                        "credit_monthly": 34,
                        "credit_package": 56,
                        "nickname": "private profile data",
                    },
                }
            ).encode()

        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return False

    def urlopen(request, timeout):
        captured.append(request)
        return FakeResponse()

    monkeypatch.setattr(video.urllib.request, "urlopen", urlopen)
    result = await backend.credits()

    assert result == {
        "provider": "pixverse-web",
        "credits": {
            "credit_daily": 12,
            "credit_monthly": 34,
            "credit_package": 56,
            "free_available": 46,
            "total_available": 102,
            "free_buckets_known": True,
        },
    }
    headers = {key.lower(): value for key, value in captured[0].header_items()}
    assert headers["token"] == "private-pixverse-token"
    assert headers["x-platform"] == "Web"
    assert headers["x-device-id"] == "device-123"
    assert headers["workspace-id"] == "0"
    assert "private-pixverse-token" not in json.dumps(result)


@pytest.mark.asyncio
async def test_pixverse_reference_upload_uses_temporary_sts_then_registers_media(
    monkeypatch, tmp_path
):
    backend = video.PixverseWebVideoBackend(
        {**video.VIDEO_PROVIDERS["pixverse-web"], "credential": "token"}, 3
    )
    image = reference(tmp_path)
    calls = []

    def pix_request(method, path, payload=None, refresh_credit=False):
        calls.append((method, path, payload))
        if path == "/getUploadToken":
            return {"Ak": "temporary-ak", "Sk": "temporary-sk", "Token": "temporary-sts"}
        return {"result": [{"path": "registered/reference/path"}]}

    uploaded = []
    monkeypatch.setattr(backend, "_pix_request", pix_request)
    monkeypatch.setattr(
        backend,
        "_upload_to_oss",
        lambda path, key, credentials: uploaded.append((path, key, credentials)),
    )

    result = await backend._register_reference(image)

    assert result == "registered/reference/path"
    assert calls[0] == ("POST", "/getUploadToken", {})
    assert calls[1][0:2] == ("POST", "/media/batch_upload_media")
    image_info = calls[1][2]["images"][0]
    assert image_info["path"].startswith("upload/")
    assert image_info["file_name"] == image_info["name"]
    assert uploaded[0][0] == image
    assert uploaded[0][1] == image_info["path"]
    assert uploaded[0][2] == ("temporary-ak", "temporary-sk", "temporary-sts")


@pytest.mark.asyncio
async def test_pixverse_upload_registers_reference_then_uses_i2v(monkeypatch, tmp_path):
    backend = video.PixverseWebVideoBackend(
        {
            **video.VIDEO_PROVIDERS["pixverse-web"],
            "credential": "token",
            "device_id": "device",
        },
        3,
    )
    image = reference(tmp_path)
    calls = []

    async def fake_register(path):
        assert path == image
        return "registered/reference/path"

    responses = [
        {"video_id": "pix-video-1"},
        {"status": "completed", "video_url": "https://cdn.test/result.mp4"},
    ]

    def pix_request(method, path, payload=None, refresh_credit=False):
        calls.append((method, path, payload, refresh_credit))
        return responses.pop(0)

    monkeypatch.setattr(backend, "_register_reference", fake_register)
    monkeypatch.setattr(backend, "_require_free_credit", lambda: _async_noop())
    monkeypatch.setattr(backend, "_pix_request", pix_request)
    monkeypatch.setattr(backend, "_download", lambda url, path: path.write_bytes(b"fake-mp4"))
    monkeypatch.setattr(video, "POLL_INTERVAL", 0)

    result = await backend.generate(
        VideoRequest(
            prompt="人物沿街走来",
            provider="pixverse-web",
            model="v6",
            resolution="720p",
            duration=6,
        ),
        tmp_path,
        [image],
    )

    assert result.read_bytes() == b"fake-mp4"
    method, endpoint, payload, refresh = calls[0]
    assert (method, endpoint, refresh) == ("POST", "/video/i2v", True)
    assert payload["model"] == "v6"
    assert payload["customer_img_paths"] == ["registered/reference/path"]
    assert payload["duration"] == 6 and payload["create_count"] == 1
    assert calls[1] == ("POST", "/video/list/detail", {"video_id": "pix-video-1"}, False)


def test_video_public_metadata_never_contains_api_key(tmp_path):
    store = video.VideoConfigStore(tmp_path)
    store.save("minimax", "private-video-key")
    payload = store.public()
    assert all("api_key" not in item for item in payload)
    assert next(item for item in payload if item["id"] == "minimax")["configured"] is True
    assert "private-video-key" not in json.dumps(payload)


@pytest.mark.asyncio
async def test_puter_driver_backend_uses_documented_video_envelope(monkeypatch, tmp_path):
    backend = video.PuterVideoBackend(
        {**video.VIDEO_PROVIDERS[video.PUTER_ID], "credential": "puter-auth-token"},
        3,
    )
    calls = []

    def fake_call(interface, driver, args, *, media):
        calls.append((interface, driver, args, media))
        return "video/mp4", b"\x00\x00\x00\x18ftypmp42\x00\x00\x00\x00"

    monkeypatch.setattr(backend, "_call", fake_call)
    path = await backend.generate(
        VideoRequest(
            provider=video.PUTER_ID,
            model="vidu/vidu-q3-turbo",
            prompt="一只猫走向镜头",
            duration=5,
        ),
        tmp_path,
        [],
    )

    assert path.suffix == ".mp4"
    interface, driver, args, media = calls[0]
    assert (interface, driver, media) == (
        "puter-video-generation",
        "ai-video",
        "video",
    )
    assert args["model"] == "vidu/vidu-q3-turbo"
    assert args["seconds"] == 5
    assert args["size"] == "720p"


@pytest.mark.asyncio
async def test_puter_image_backend_preserves_webp_extension(monkeypatch, tmp_path):
    backend = video.PuterVideoBackend(
        {**video.VIDEO_PROVIDERS[video.PUTER_ID], "credential": "puter-auth-token"},
        3,
    )
    webp = b"RIFF" + b"\x00\x00\x00\x00" + b"WEBP" + b"VP8 "
    monkeypatch.setattr(
        backend,
        "_call",
        lambda interface, driver, args, *, media: ("image/webp", webp),
    )
    paths = await backend.generate_image(
        ImageRequest(
            provider=video.PUTER_ID,
            model="gpt-image-2",
            prompt="一只猫",
        ),
        tmp_path,
        [],
    )
    assert paths[0].suffix == ".webp"


def test_pollinations_is_public_and_does_not_require_a_secret(tmp_path):
    store = video.VideoConfigStore(tmp_path)
    item = next(row for row in store.public() if row["id"] == video.POLLINATIONS_ID)
    assert item["configured"] is True
    assert store.get(video.POLLINATIONS_ID)["auth_type"] == "public"


def test_pixverse_credit_query_route_never_returns_saved_token(monkeypatch, tmp_path):
    async def fake_credits(self):
        return {
            "provider": "pixverse-web",
            "credits": {"credit_daily": 3, "credit_monthly": 4, "credit_package": 5, "total_available": 12},
        }

    monkeypatch.setattr(video.PixverseWebVideoBackend, "credits", fake_credits)
    settings = Settings(tmp_path / "state", tmp_path / "images", timeout=3)
    with TestClient(create_app(settings)) as client:
        token = "pixverse-token-must-not-leak"
        saved = client.put(
            "/api/video-providers/pixverse-web",
            json={"credential": token, "auth_type": "web_token"},
        )
        assert saved.status_code == 200
        response = client.get("/api/video-providers/pixverse-web/credits")
        assert response.status_code == 200
        assert response.json()["credits"]["total_available"] == 12
        assert token not in response.text


def test_vidu_credit_query_route_returns_sanitized_api_balance(monkeypatch, tmp_path):
    async def fake_credits(self):
        return {
            "provider": "vidu",
            "credits": {
                "remains": [{"type": "test", "credit_remain": 42}],
                "packages": [{"id": "package-1", "credit_remain": 42}],
            },
        }

    monkeypatch.setattr(video.ViduVideoBackend, "credits", fake_credits)
    settings = Settings(tmp_path / "state", tmp_path / "images", timeout=3)
    with TestClient(create_app(settings)) as client:
        token = "vidu-api-token-must-not-leak"
        saved = client.put("/api/video-providers/vidu", json={"api_key": token})
        assert saved.status_code == 200
        response = client.get("/api/video-providers/vidu/credits")
        assert response.status_code == 200
        assert response.json()["credits"]["remains"][0]["credit_remain"] == 42
        assert token not in response.text


def test_admin_model_discovery_exposes_vidu_q2_and_q3(tmp_path):
    settings = Settings(tmp_path / "state", tmp_path / "images", timeout=3)
    with TestClient(create_app(settings)) as client:
        assert client.put("/api/video-providers/vidu", json={"api_key": "vidu-key"}).status_code == 200
        models = client.get("/api/models?provider=vidu")
        assert models.status_code == 200
        ids = {item["id"] for item in models.json()["data"]}
        assert {"viduq3-turbo", "viduq2", "viduq2-pro-fast"}.issubset(ids)


def test_admin_video_job_saves_and_serves_mp4(monkeypatch, tmp_path):
    class FakeVideoBackend:
        async def generate(self, request, folder, references):
            path = folder / "fake-video.mp4"
            path.write_bytes(b"\x00\x00\x00\x18ftypmp42\x00\x00\x00\x00")
            return path

    monkeypatch.setattr(engine_module, "video_backend_for", lambda *args: FakeVideoBackend())
    settings = Settings(tmp_path / "state", tmp_path / "images", timeout=3)
    with TestClient(create_app(settings)) as client:
        saved = client.put("/api/video-providers/minimax", json={"api_key": "secret"})
        assert saved.status_code == 200 and "secret" not in saved.text
        submitted = client.post(
            "/api/video/jobs",
            json={"provider": "minimax", "prompt": "一只猫走向镜头", "duration": 5},
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
        assert job["videos"]
        assert client.get("/videos/" + job["videos"][0]).content.startswith(b"\x00\x00\x00\x18ftyp")


def test_video_batch_routes_return_correlated_mp4_results_and_are_idempotent(
    monkeypatch, tmp_path
):
    class FakeVideoBackend:
        async def generate(self, request, folder, references):
            path = folder / f"{request.prompt}.mp4"
            path.write_bytes(b"\x00\x00\x00\x18ftypmp42\x00\x00\x00\x00")
            return path

    monkeypatch.setattr(engine_module, "video_backend_for", lambda *args: FakeVideoBackend())
    settings = Settings(tmp_path / "state", tmp_path / "images", timeout=3)
    with TestClient(create_app(settings)) as client:
        assert client.put("/api/video-providers/minimax", json={"api_key": "secret"}).status_code == 200
        token = client.post("/api/keys", json={"name": "video-batch"}).json()["key"]
        headers = {
            "Authorization": "Bearer " + token,
            "Idempotency-Key": "video-batch-once",
        }
        payload = {
            "items": [
                {"custom_id": "opening", "provider": "minimax", "prompt": "opening"},
                {"custom_id": "ending", "provider": "minimax", "prompt": "ending"},
            ]
        }
        submitted = client.post("/v1/video/batches", json=payload, headers=headers)
        assert submitted.status_code == 202, submitted.text
        batch = submitted.json()
        assert batch["object"] == "video.batch"
        assert batch["counts"]["total"] == 2
        assert [item["custom_id"] for item in batch["items"]] == ["opening", "ending"]

        duplicate = client.post("/v1/video/batches", json=payload, headers=headers)
        assert duplicate.status_code == 202
        assert duplicate.json()["id"] == batch["id"]

        deadline = time.monotonic() + 3
        while time.monotonic() < deadline:
            result = client.get(
                f"/v1/video/batches/{batch['id']}",
                headers={"Authorization": "Bearer " + token},
            ).json()
            if result["status"] == "completed":
                break
            time.sleep(0.01)
        assert result["status"] == "completed", result
        assert result["counts"]["succeeded"] == 2
        assert all(item["data"][0]["url"].endswith(".mp4") for item in result["items"])
        assert all(item["data"][0]["url"].startswith("http://testserver/videos/") for item in result["items"])

        changed = {
            "items": [{"custom_id": "opening", "provider": "minimax", "prompt": "changed"}]
        }
        conflict = client.post("/v1/video/batches", json=changed, headers=headers)
        assert conflict.status_code == 409, conflict.text
