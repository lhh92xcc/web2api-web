"""Direct Jimeng web-account image/video adapter.

This adapter uses the user's own first-party web session and web-credit
entitlements. It never calls a paid developer API, automates login, solves
CAPTCHA/WAF challenges, or retries a generation submission.
"""

import asyncio
import hashlib
import json
import mimetypes
import re
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid
from io import BytesIO
from pathlib import Path

from PIL import Image, UnidentifiedImageError

from storage import GatewayError

JIMENG_BASE_URL = "https://jimeng.jianying.com"
JIMENG_IMAGE_UPLOAD_URL = "https://imagex.bytedanceapi.com/"
JIMENG_ASSISTANT_ID = "513695"
JIMENG_WEB_VERSION = "7.5.0"
JIMENG_PLATFORM = "7"
DRAFT_VERSION = "3.2.9"
MAX_IMAGE_BYTES = 32 * 1024 * 1024
MAX_VIDEO_BYTES = 1024 * 1024 * 1024
POLL_INTERVAL = 2

IMAGE_MODELS = {
    "jimeng-seedream-5-flash": {
        "name": "Seedream 5.0 Flash · 即梦网页",
        "model_req_key": "high_aes_general_v50_flash",
    },
    "jimeng-seedream-5-lite": {
        "name": "Seedream 5.0 Lite · 即梦网页",
        "model_req_key": "high_aes_general_v50",
    },
    "jimeng-seedream-4.7": {
        "name": "Seedream 4.7 · 即梦网页",
        "model_req_key": "high_aes_general_v43",
    },
    "jimeng-seedream-4.0": {
        "name": "Seedream 4.0 · 即梦网页",
        "model_req_key": "high_aes_general_v40",
    },
    "jimeng-seedream-3.1": {
        "name": "Seedream 3.1 · 即梦网页",
        "model_req_key": "high_aes_general_v30l_art_fangzhou:general_v3.0_18b",
    },
}
DEFAULT_IMAGE_MODEL = "jimeng-seedream-5-flash"

VIDEO_MODELS = {
    "jimeng-seedance-2.5-draft": {
        "name": "Seedance 2.5 样片 · 低成本预览",
        "model_req_key": "dreamina_seedance_45_pro_draft",
        "benefit_type": "seedance_25_draft_480p_output",
    },
    "jimeng-seedance-2.5": {
        "name": "Seedance 2.5",
        "model_req_key": "dreamina_seedance_45_pro",
        "benefit_type": "seedance_25_720p_output",
    },
    "jimeng-seedance-2.0-mini": {
        "name": "Seedance 2.0 Mini",
        "model_req_key": "dreamina_seedance_40_mini",
        "benefit_type": "seedance_20_mini_720p_output",
    },
    "jimeng-seedance-2.0-fast-vip": {
        "name": "Seedance 2.0 Fast VIP · 会员权限",
        "model_req_key": "dreamina_seedance_40_vision",
        "benefit_type": "seedance_20_fast_720p_output",
    },
    "jimeng-seedance-2.0-vip": {
        "name": "Seedance 2.0 VIP · 会员权限",
        "model_req_key": "dreamina_seedance_40_pro_vision",
        "benefit_type": "seedance_20_pro_720p_output",
    },
    "jimeng-seedance-1.0": {
        "name": "Seedance 1.0",
        "model_req_key": "dreamina_ic_generate_video_model_vgfm_3.0_pro",
        "benefit_type": "basic_video_operation_vgfm_v_three_pro",
    },
    "jimeng-seedance-1.0-fast": {
        "name": "Seedance 1.0 Fast",
        "model_req_key": "dreamina_ic_generate_video_model_vgfm_3.0_fast",
        "benefit_type": "basic_video_operation_vgfm_v_three",
    },
    "jimeng-happyhorse-1.1": {
        "name": "HappyHorse 1.1",
        "model_req_key": "dreamina_happyhorse_v1_1",
        "benefit_type": "happyhorse_11_720p_output",
    },
    "jimeng-wan-3.0": {
        "name": "Wan 3.0",
        "model_req_key": "dreamina_wan_30",
        "benefit_type": "wan_30_720p_output",
    },
}
DEFAULT_VIDEO_MODEL = "jimeng-seedance-2.5-draft"

_SESSION_RE = re.compile(r"^[A-Za-z0-9._%-]{1,512}$")


class JimengWebBackend:
    """Use a saved sessionid against Jimeng's first-party web endpoints."""

    def __init__(self, config: dict, timeout: int = 450):
        self.config = config
        self.timeout = max(20, min(int(timeout), 600))
        self.token = str(config.get("credential") or "")
        if not _SESSION_RE.fullmatch(self.token):
            raise GatewayError(
                "jimeng_authentication_failed",
                "即梦 sessionid 格式不正确，请从已登录的即梦网页重新复制。",
                401,
            )
        self.device_id = str(config.get("device_id") or uuid.uuid4())
        self.web_id = str(config.get("uuid") or uuid.uuid4())
        self.allow_paid = bool(config.get("allow_paid", False))

    @staticmethod
    def _url(path: str, params: dict | None = None) -> str:
        if not path.startswith("/") or "://" in path:
            raise GatewayError("jimeng_invalid_request", "即梦接口路径无效。", 500)
        query = urllib.parse.urlencode(params or {})
        return JIMENG_BASE_URL + path + ("?" + query if query else "")

    def _request_sync(
        self, method: str, path: str, payload: dict | None = None, params=None
    ) -> dict:
        timestamp = str(int(time.time()))
        sign_source = (
            f"9e2c|{path[-7:]}|{JIMENG_PLATFORM}|{JIMENG_WEB_VERSION}|"
            f"{timestamp}|{self.device_id}|11ac"
        )
        signature = hashlib.md5(sign_source.encode()).hexdigest()
        query = {
            "aid": JIMENG_ASSISTANT_ID,
            "device_platform": "web",
            "region": "CN",
            "web_id": self.web_id,
            **(params or {}),
        }
        body = (
            None
            if payload is None
            else json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode()
        )
        headers = {
            "Accept": "application/json, text/plain, */*",
            "Origin": JIMENG_BASE_URL,
            "Referer": JIMENG_BASE_URL + "/ai-tool/image/generate",
            "Cookie": f"sessionid={self.token}; sessionid_ss={self.token}",
            "Appid": JIMENG_ASSISTANT_ID,
            "Appvr": JIMENG_WEB_VERSION,
            "Pf": JIMENG_PLATFORM,
            "Tdid": self.device_id,
            "Device-Time": timestamp,
            "Sign": signature,
            "Sign-Ver": "1",
        }
        if body is not None:
            headers["Content-Type"] = "application/json"
        request = urllib.request.Request(
            self._url(path, query), data=body, headers=headers, method=method.upper()
        )
        try:
            with urllib.request.urlopen(request, timeout=self.timeout) as response:
                raw = response.read(16 * 1024 * 1024 + 1)
                status = response.status
        except urllib.error.HTTPError as exc:
            if exc.code in {401, 403}:
                raise GatewayError(
                    "jimeng_authentication_failed",
                    "即梦网页登录信息无效或需要网页验证，请更新 sessionid 并在网页完成验证。",
                    401,
                ) from None
            if exc.code == 429:
                raise GatewayError(
                    "jimeng_rate_limited",
                    "即梦网页请求过于频繁，请稍后再试。",
                    429,
                ) from None
            raise GatewayError(
                "jimeng_upstream_error",
                f"即梦网页接口返回 HTTP {exc.code}。",
                502,
            ) from None
        except (urllib.error.URLError, TimeoutError, OSError):
            raise GatewayError(
                "jimeng_network_error", "即梦网页暂时无法连接。", 502
            ) from None
        if len(raw) > 16 * 1024 * 1024:
            raise GatewayError(
                "jimeng_response_too_large", "即梦网页返回内容过大。", 502
            )
        try:
            result = json.loads(raw)
        except (ValueError, UnicodeDecodeError):
            raise GatewayError(
                "jimeng_invalid_response",
                "即梦网页返回了无法读取的响应；可能需要重新登录或完成人机验证。",
                502,
            ) from None
        if not isinstance(result, dict):
            raise GatewayError(
                "jimeng_invalid_response", "即梦网页返回了无效响应。", 502
            )
        ret = result.get("ret")
        if ret not in (None, 0, "0"):
            error_text = str(result.get("errmsg") or "").lower()
            if ret in (5000, "5000") or any(
                phrase in error_text
                for phrase in ("credit", "quota", "积分", "额度", "余额")
            ):
                raise GatewayError(
                    "jimeng_free_quota_empty",
                    "即梦网页免费积分不足，任务已停止；未切换到其他付费 API。",
                    429,
                )
            if any(
                phrase in error_text
                for phrase in ("login", "auth", "登录", "认证", "验证")
            ):
                raise GatewayError(
                    "jimeng_authentication_failed",
                    "即梦登录信息失效或需要网页验证，请更新 sessionid。",
                    401,
                )
            raise GatewayError(
                "jimeng_upstream_error",
                "即梦网页接口拒绝了请求；请检查模型权限和账号状态。",
                502,
            )
        if status >= 400:
            raise GatewayError(
                "jimeng_upstream_error", "即梦网页请求失败。", 502
            )
        data = result.get("data")
        return data if isinstance(data, dict) else result

    async def _request(self, method, path, payload=None, params=None):
        return await asyncio.to_thread(
            self._request_sync, method, path, payload, params
        )

    @staticmethod
    def _numeric(value):
        try:
            return max(0, int(value or 0))
        except (TypeError, ValueError):
            return 0

    async def credits(self) -> dict:
        data = await self._request(
            "POST", "/commerce/v1/benefits/user_credit", {}
        )
        balance = data.get("credit", data)
        if not isinstance(balance, dict):
            raise GatewayError(
                "jimeng_invalid_response",
                "即梦没有返回积分余额，请重新测试 sessionid。",
                502,
            )
        gift = self._numeric(balance.get("gift_credit"))
        purchased = self._numeric(balance.get("purchase_credit"))
        vip = self._numeric(balance.get("vip_credit"))
        return {
            "provider": "jimeng-web",
            "credits": {
                "gift_credit": gift,
                "purchase_credit": purchased,
                "vip_credit": vip,
                "total_available": gift + purchased + vip,
            },
        }

    async def claim_daily_credit(self) -> dict:
        try:
            await self._request(
                "POST",
                "/commerce/v1/benefits/credit_receive",
                {"time_zone": "Asia/Shanghai"},
            )
        except GatewayError as exc:
            # The upstream may report that today's free award was already
            # claimed. Refresh the balance below and surface that result.
            if exc.code not in {"jimeng_upstream_error", "jimeng_free_quota_empty"}:
                raise
        return await self.credits()

    async def _require_free_credit(self):
        current = await self.credits()
        if current["credits"]["gift_credit"] <= 0:
            current = await self.claim_daily_credit()
        credits = current["credits"]
        if credits["gift_credit"] <= 0:
            raise GatewayError(
                "jimeng_free_quota_empty",
                "即梦账号当前没有可用的网页赠送积分；任务已停止，没有调用付费 API。",
                429,
            )
        # ``gift_credit`` is the only balance this adapter is allowed to use
        # by default.  An account may legitimately have free daily points and
        # purchased/VIP points at the same time; their presence must not block
        # the free request.  If the gift bucket is empty, fail before submit so
        # the paid buckets can never be consumed accidentally.
        if not self.allow_paid and credits["gift_credit"] <= 0:
            raise GatewayError(
                "jimeng_free_quota_empty",
                "即梦账号当前没有可用的网页赠送积分；任务已停止，没有调用付费 API。",
                429,
            )
        return credits

    def _call_upload_proof(self, file_path: Path, scene: str):
        path = Path(file_path)
        data = path.read_bytes()
        if not data or len(data) > MAX_IMAGE_BYTES:
            raise GatewayError(
                "jimeng_reference_too_large",
                "即梦参考图必须非空且不能超过 32 MB。",
                413,
            )
        content_type = mimetypes.guess_type(path.name)[0] or "image/png"
        proof = self._request_sync(
            "POST",
            "/mweb/v1/get_upload_image_proof",
            {
                "scene": scene,
                "file_name": path.name,
                "file_size": len(data),
            },
        ).get("proof_info")
        if not isinstance(proof, dict):
            raise GatewayError(
                "jimeng_upload_failed", "即梦没有返回参考图上传凭证。", 502
            )
        query = proof.get("query_params")
        if not isinstance(query, dict):
            query = {}
        boundary = "----Web2API" + uuid.uuid4().hex
        filename = path.name.replace('"', "_").replace("\r", "_").replace("\n", "_")
        body = (
            f"--{boundary}\r\n"
            f'Content-Disposition: form-data; name="file"; filename="{filename}"\r\n'
            f"Content-Type: {content_type}\r\n\r\n"
        ).encode() + data + f"\r\n--{boundary}--\r\n".encode()
        headers = {
            str(key): str(value)
            for key, value in (proof.get("headers") or {}).items()
            if str(key).lower() not in {"host", "content-length", "content-type"}
        }
        headers["Content-Type"] = f"multipart/form-data; boundary={boundary}"
        request = urllib.request.Request(
            JIMENG_IMAGE_UPLOAD_URL + ("?" + urllib.parse.urlencode(query) if query else ""),
            data=body,
            headers=headers,
            method="POST",
        )
        try:
            with urllib.request.urlopen(request, timeout=self.timeout) as response:
                response.read(1024 * 1024)
                if response.status != 200:
                    raise GatewayError(
                        "jimeng_upload_failed", "即梦参考图上传失败。", 502
                    )
        except GatewayError:
            raise
        except (urllib.error.URLError, TimeoutError, OSError):
            raise GatewayError(
                "jimeng_upload_failed", "即梦参考图上传失败。", 502
            ) from None
        image_uri = proof.get("image_uri")
        if not isinstance(image_uri, str) or not image_uri:
            raise GatewayError(
                "jimeng_upload_failed", "即梦上传凭证没有图片 ID。", 502
            )
        return image_uri

    @staticmethod
    def _dimensions(request):
        ratio = request.composition() or "1:1"
        try:
            ratio_w, ratio_h = (int(value) for value in ratio.split(":", 1))
        except (TypeError, ValueError):
            ratio_w, ratio_h = 1, 1
        size = request.size
        if "x" in size:
            width, height = (int(value) for value in size.split("x", 1))
            return width, height
        edge = {"1K": 1024, "2K": 2048, "4K": 4096}.get(size.upper(), 2048)
        if ratio_w >= ratio_h:
            width = edge
            height = max(64, round(edge * ratio_h / ratio_w / 64) * 64)
        else:
            height = edge
            width = max(64, round(edge * ratio_w / ratio_h / 64) * 64)
        return width, height

    async def generate_image(self, request, folder: Path, reference_paths):
        if request.model not in {"default", *IMAGE_MODELS}:
            raise GatewayError(
                "jimeng_invalid_model", "所选模型不在即梦网页模型列表中。", 422
            )
        model_id = DEFAULT_IMAGE_MODEL if request.model == "default" else request.model
        model_key = IMAGE_MODELS[model_id]["model_req_key"]
        await self._require_free_credit()
        uploaded = []
        for path in reference_paths:
            uploaded.append(
                await asyncio.to_thread(
                    self._call_upload_proof, Path(path), "aigc_image"
                )
            )
        width, height = self._dimensions(request)
        component_id = str(uuid.uuid4())
        component = {
            "type": "image_base_component",
            "id": component_id,
            "min_version": DRAFT_VERSION,
            "generate_type": "blend" if uploaded else "generate",
            "aigc_mode": "workbench",
            "abilities": {
                "type": "",
                "id": str(uuid.uuid4()),
            },
        }
        core_param = {
            "type": "",
            "id": str(uuid.uuid4()),
            "model": model_key,
            "prompt": request.render_prompt(has_references=bool(uploaded)),
            "negative_prompt": request.negative_prompt,
            "seed": int(uuid.uuid4().int % 100_000_000) + 2_500_000_000,
            "sample_strength": 0.5,
            "image_ratio": 1,
            "large_image_info": {
                "type": "",
                "id": str(uuid.uuid4()),
                "height": height,
                "width": width,
                "resolution_type": request.size.lower()
                if request.size.upper() in {"1K", "2K", "4K"}
                else "2k",
            },
        }
        if uploaded:
            component["abilities"]["blend"] = {
                "type": "",
                "id": str(uuid.uuid4()),
                "core_param": core_param,
                "ability_list": [
                    {
                        "type": "",
                        "id": str(uuid.uuid4()),
                        "name": "byte_edit",
                        "image_uri_list": [image_uri],
                        "image_list": [
                            {
                                "type": "image",
                                "id": str(uuid.uuid4()),
                                "source_from": "upload",
                                "platform_type": 1,
                                "name": "",
                                "image_uri": image_uri,
                                "width": 0,
                                "height": 0,
                                "format": "",
                                "uri": image_uri,
                            }
                        ],
                        "strength": 0.5,
                    }
                    for image_uri in uploaded
                ],
                "prompt_placeholder_info_list": [
                    {
                        "type": "",
                        "id": str(uuid.uuid4()),
                        "ability_index": index,
                    }
                    for index, _image_uri in enumerate(uploaded)
                ],
                "postedit_param": {
                    "type": "",
                    "id": str(uuid.uuid4()),
                    "generate_type": 0,
                },
            }
        else:
            component["abilities"]["generate"] = {
                "type": "",
                "id": str(uuid.uuid4()),
                "core_param": core_param,
                "history_option": {"type": "", "id": str(uuid.uuid4())},
            }
        result = await self._request(
            "POST",
            "/mweb/v1/aigc_draft/generate",
            {
                "extend": {"root_model": model_key, "template_id": ""},
                "submit_id": str(uuid.uuid4()),
                "metrics_extra": json.dumps(
                    {"promptSource": "custom", "generateCount": 1},
                    separators=(",", ":"),
                ),
                "draft_content": json.dumps(
                    {
                        "type": "draft",
                        "id": str(uuid.uuid4()),
                        "min_version": DRAFT_VERSION,
                        "is_from_tsn": True,
                        "version": DRAFT_VERSION,
                        "main_component_id": component_id,
                        "component_list": [component],
                    },
                    ensure_ascii=False,
                    separators=(",", ":"),
                ),
                "http_common_info": {"aid": int(JIMENG_ASSISTANT_ID)},
            },
            params={
                "babi_param": urllib.parse.quote(
                    json.dumps(
                        {
                            "scenario": "image_video_generation",
                            "feature_key": "aigc_to_image",
                            "feature_entrance": "to_image",
                            "feature_entrance_detail": "to_image-" + model_key,
                        },
                        separators=(",", ":"),
                    )
                )
            },
        )
        aigc_data = result.get("aigc_data", result)
        history_id = aigc_data.get("history_record_id") or aigc_data.get("history_id")
        if not history_id:
            raise GatewayError(
                "jimeng_invalid_response", "即梦没有返回图片任务 ID。", 502
            )
        record = await self._poll_history(str(history_id), "image")
        item_list = record.get("item_list") or []
        urls = [self._image_url(item) for item in item_list]
        urls = [url for url in urls if url]
        if not urls:
            raise GatewayError(
                "jimeng_invalid_response", "即梦任务完成，但没有返回图片。", 502
            )
        return [
            await asyncio.to_thread(
                self._download_media,
                url,
                Path(folder) / f"jimeng-{uuid.uuid4().hex}.webp",
                MAX_IMAGE_BYTES,
                "image",
            )
            for url in urls[:1]
        ]

    async def generate(self, request, folder: Path, reference_paths):
        if request.model not in {"default", *VIDEO_MODELS}:
            raise GatewayError(
                "jimeng_invalid_model", "所选模型不在即梦网页视频模型列表中。", 422
            )
        model_id = DEFAULT_VIDEO_MODEL if request.model == "default" else request.model
        model = VIDEO_MODELS[model_id]
        await self._require_free_credit()
        if len(reference_paths) > 2:
            raise GatewayError(
                "jimeng_reference_count",
                "即梦视频当前通过此接口支持首帧和尾帧，最多 2 张参考图。",
                422,
            )
        uploaded = []
        for path in reference_paths:
            uploaded.append(
                await asyncio.to_thread(
                    self._call_upload_proof, Path(path), "video_cover"
                )
            )
        width = 1280
        height = 720
        first_frame = (
            self._frame(uploaded[0], reference_paths[0], width, height)
            if uploaded
            else None
        )
        end_frame = (
            self._frame(uploaded[1], reference_paths[1], width, height)
            if len(uploaded) > 1
            else None
        )
        component_id = str(uuid.uuid4())
        video_input = {
            "duration_ms": request.duration * 1000,
            "first_frame_image": first_frame,
            "end_frame_image": end_frame,
            "fps": 24,
            "id": str(uuid.uuid4()),
            "min_version": "3.0.5",
            "prompt": request.prompt,
            "resolution": request.resolution.lower(),
            "type": "",
            "video_mode": 2,
        }
        commerce = {
            "benefit_type": model["benefit_type"],
            "resource_id": "generate_video",
            "resource_id_type": "str",
            "resource_sub_type": "aigc",
        }
        result = await self._request(
            "POST",
            "/mweb/v1/aigc_draft/generate",
            {
                "extend": {
                    "root_model": model["model_req_key"],
                    "m_video_commerce_info": commerce,
                    "m_video_commerce_info_list": [commerce],
                },
                "submit_id": str(uuid.uuid4()),
                "metrics_extra": json.dumps(
                    {
                        "enterFrom": "click",
                        "isDefaultSeed": 1,
                        "promptSource": "custom",
                        "isRegenerate": False,
                        "originSubmitId": str(uuid.uuid4()),
                    },
                    separators=(",", ":"),
                ),
                "draft_content": json.dumps(
                    {
                        "type": "draft",
                        "id": str(uuid.uuid4()),
                        "min_version": "3.0.5",
                        "is_from_tsn": True,
                        "version": DRAFT_VERSION,
                        "main_component_id": component_id,
                        "component_list": [
                            {
                                "type": "video_base_component",
                                "id": component_id,
                                "min_version": "1.0.0",
                                "metadata": {
                                    "type": "",
                                    "id": str(uuid.uuid4()),
                                    "created_platform": 3,
                                    "created_platform_version": "",
                                    "created_time_in_ms": int(time.time() * 1000),
                                    "created_did": "",
                                },
                                "generate_type": "gen_video",
                                "aigc_mode": "workbench",
                                "abilities": {
                                    "type": "",
                                    "id": str(uuid.uuid4()),
                                    "gen_video": {
                                        "id": str(uuid.uuid4()),
                                        "type": "",
                                        "text_to_video_params": {
                                            "type": "",
                                            "id": str(uuid.uuid4()),
                                            "model_req_key": model["model_req_key"],
                                            "priority": 0,
                                            "seed": int(uuid.uuid4().int % 100_000_000)
                                            + 2_500_000_000,
                                            "video_aspect_ratio": request.aspect_ratio,
                                            "video_gen_inputs": [video_input],
                                        },
                                        "video_task_extra": "{}",
                                    },
                                },
                            }
                        ],
                    },
                    ensure_ascii=False,
                    separators=(",", ":"),
                ),
                "http_common_info": {"aid": int(JIMENG_ASSISTANT_ID)},
            },
            params={"web_version": JIMENG_WEB_VERSION, "da_version": DRAFT_VERSION},
        )
        aigc_data = result.get("aigc_data", result)
        history_id = aigc_data.get("history_record_id") or aigc_data.get("history_id")
        if not history_id:
            raise GatewayError(
                "jimeng_invalid_response", "即梦没有返回视频任务 ID。", 502
            )
        record = await self._poll_history(str(history_id), "video")
        video_url = self._video_url(record.get("item_list") or [])
        if not video_url:
            raise GatewayError(
                "jimeng_invalid_response", "即梦任务完成，但没有返回视频。", 502
            )
        return await asyncio.to_thread(
            self._download_media,
            video_url,
            Path(folder) / f"jimeng-{uuid.uuid4().hex}.mp4",
            MAX_VIDEO_BYTES,
            "video",
        )

    @staticmethod
    def _frame(image_uri, path, width, height):
        try:
            with Image.open(path) as image:
                image_width, image_height = image.size
        except (OSError, UnidentifiedImageError):
            image_width, image_height = width, height
        return {
            "format": "",
            "height": image_height,
            "id": str(uuid.uuid4()),
            "image_uri": image_uri,
            "name": "",
            "platform_type": 1,
            "source_from": "upload",
            "type": "image",
            "uri": image_uri,
            "width": image_width,
        }

    async def _poll_history(self, history_id: str, kind: str):
        deadline = time.monotonic() + self.timeout
        while time.monotonic() < deadline:
            result = await self._request(
                "POST",
                "/mweb/v1/get_history_by_ids",
                {
                    "history_ids": [history_id],
                    "image_info": {
                        "width": 2048,
                        "height": 2048,
                        "format": "webp",
                    },
                },
            )
            record = self._find_record(result, history_id)
            if record is not None:
                try:
                    status = int(record.get("status") or 0)
                except (TypeError, ValueError):
                    status = 0
                item_list = record.get("item_list") or []
                if item_list and status not in {20, 30}:
                    return record
                if status == 30:
                    fail_code = str(record.get("fail_code") or "")
                    if fail_code == "2038":
                        raise GatewayError(
                            "jimeng_content_filtered",
                            "即梦内容审核未通过。",
                            422,
                        )
                    raise GatewayError(
                        "jimeng_generation_failed",
                        "即梦生成失败；请检查模型权限、提示词和网页任务记录。",
                        502,
                    )
                if status == 10 and not item_list:
                    raise GatewayError(
                        "jimeng_generation_failed",
                        "即梦任务已结束，但没有生成结果。",
                        502,
                    )
            await asyncio.sleep(min(POLL_INTERVAL, max(0.05, deadline - time.monotonic())))
        raise GatewayError(
            "upstream_timeout",
            "等待即梦网页生成超时；请先检查即梦网页任务记录，再决定是否重新提交。",
            504,
        )

    @staticmethod
    def _find_record(result, history_id):
        if not isinstance(result, dict):
            return None
        direct = result.get(history_id)
        if isinstance(direct, dict):
            return direct
        for key in ("history_list", "history_records", "records", "data"):
            value = result.get(key)
            if isinstance(value, list):
                for item in value:
                    if isinstance(item, dict) and str(
                        item.get("history_id")
                        or item.get("history_record_id")
                        or item.get("id")
                        or ""
                    ) == history_id:
                        return item
                    if isinstance(item, dict) and "item_list" in item:
                        return item
            elif isinstance(value, dict):
                if history_id in value and isinstance(value[history_id], dict):
                    return value[history_id]
                found = JimengWebBackend._find_record(value, history_id)
                if found:
                    return found
        return None

    @staticmethod
    def _image_url(item):
        if not isinstance(item, dict):
            return None
        image = item.get("image") or {}
        large = image.get("large_images") or []
        if large and isinstance(large[0], dict):
            return large[0].get("image_url") or large[0].get("url")
        return (item.get("common_attr") or {}).get("cover_url")

    @staticmethod
    def _video_url(items):
        for item in items:
            if not isinstance(item, dict):
                continue
            video = item.get("video") or {}
            transcoded = video.get("transcoded_video") or {}
            origin = transcoded.get("origin") or {}
            url = (
                origin.get("video_url")
                or video.get("play_url")
                or video.get("download_url")
                or video.get("url")
            )
            if url:
                return url
        return None

    @staticmethod
    def _download_media(url, path: Path, limit: int, kind: str):
        parsed = urllib.parse.urlsplit(str(url))
        if parsed.scheme != "https" or not parsed.hostname or parsed.username:
            raise GatewayError(
                "jimeng_invalid_media_url", "即梦返回了无效下载地址。", 502
            )
        request = urllib.request.Request(
            url, headers={"Accept": "image/*" if kind == "image" else "video/*"}
        )
        try:
            with urllib.request.urlopen(request, timeout=120) as response:
                data = response.read(limit + 1)
                if len(data) > limit:
                    raise GatewayError(
                        "jimeng_media_too_large",
                        "即梦返回的媒体文件超过本地保存上限。",
                        413,
                    )
                mime = response.headers.get_content_type()
        except GatewayError:
            raise
        except (urllib.error.URLError, TimeoutError, OSError):
            raise GatewayError(
                "jimeng_download_failed", "即梦生成结果下载失败。", 502
            ) from None
        if not data:
            raise GatewayError(
                "jimeng_download_failed", "即梦返回了空媒体文件。", 502
            )
        if kind == "image":
            try:
                with Image.open(BytesIO(data)) as image:
                    image.verify()
                suffix = {
                    "PNG": ".png",
                    "JPEG": ".jpg",
                    "WEBP": ".webp",
                }.get(Image.open(BytesIO(data)).format)
            except (OSError, UnidentifiedImageError, SyntaxError):
                suffix = None
            if suffix is None:
                raise GatewayError(
                    "jimeng_invalid_image", "即梦返回的图片文件无效。", 502
                )
            path = path.with_suffix(suffix)
        elif not (len(data) >= 12 and data[4:8] == b"ftyp"):
            raise GatewayError(
                "jimeng_invalid_video", "即梦返回的文件不是有效 MP4 视频。", 502
            )
        path.write_bytes(data)
        return path
