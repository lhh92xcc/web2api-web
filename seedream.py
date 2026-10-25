"""Volcengine Ark Seedream image-generation fallback."""

import asyncio
import base64
import io
import json
import mimetypes
import urllib.error
import urllib.parse
import urllib.request
import uuid
from pathlib import Path

from PIL import Image, UnidentifiedImageError
from pydantic import BaseModel, ConfigDict, Field, field_validator
from storage import GatewayError, atomic_json

SEEDREAM_BASE_URL = "https://ark.cn-beijing.volces.com/api/v3"
SEEDREAM_MODEL = "doubao-seedream-5-0-flash-260915"
SEEDREAM_OUTPUT_PRICE_CNY = 0.12
MAX_IMAGE_BYTES = 100 * 1024 * 1024
MAX_REFERENCE_BYTES = 30 * 1024 * 1024
MAX_REFERENCE_EDGE = 6000


class SeedreamKeyRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)
    api_key: str | None = Field(default=None, max_length=4096)

    @field_validator("api_key")
    @classmethod
    def key_value(cls, value):
        return value.strip() if value else value


class SeedreamConfigStore:
    """Keep the Ark key local and never expose its value through the API."""

    def __init__(self, state_dir: Path):
        self.path = state_dir / "seedream.json"
        self.api_key = ""
        if self.path.exists():
            try:
                data = json.loads(self.path.read_text(encoding="utf-8"))
                self.api_key = str(data.get("api_key", "")).strip()
            except (OSError, ValueError, TypeError):
                self.api_key = ""

    @property
    def configured(self):
        return bool(self.api_key)

    def public(self):
        return {
            "configured": self.configured,
            "base_url": SEEDREAM_BASE_URL,
            "model": SEEDREAM_MODEL,
            "output_price_cny": SEEDREAM_OUTPUT_PRICE_CNY,
        }

    def save(self, api_key: str | None):
        value = (api_key or "").strip()
        if not value:
            raise GatewayError("api_key_required", "请填写火山方舟 API Key。", 422)
        try:
            atomic_json(self.path, {"api_key": value})
        except OSError:
            raise GatewayError(
                "storage_error", "Seedream API Key 保存失败。", 500
            ) from None
        self.api_key = value
        return self.public()

    def clear(self):
        try:
            self.path.unlink(missing_ok=True)
        except OSError:
            raise GatewayError(
                "storage_error", "Seedream API Key 删除失败。", 500
            ) from None
        self.api_key = ""
        return self.public()


class SeedreamBackend:
    """Call the Ark OpenAI-compatible image endpoint and save returned images."""

    def __init__(self, api_key: str, timeout: int = 450):
        self.api_key = api_key
        self.timeout = timeout

    async def generate(
        self,
        request,
        folder: Path,
        remaining: int,
        reference_paths=None,
        on_billed_images=None,
    ):
        paths = []
        for _ in range(remaining):
            try:
                payload = await asyncio.to_thread(
                    self._payload, request, reference_paths or []
                )
                raw, billed_images = await asyncio.to_thread(
                    self._generate_one, payload
                )
                if billed_images and on_billed_images:
                    on_billed_images(billed_images)
                path = folder / f"seedream-{uuid.uuid4().hex}.png"
                path.write_bytes(raw)
                paths.append(path)
            except Exception as exc:
                billed_images = getattr(exc, "billed_images", 0)
                if billed_images and on_billed_images:
                    on_billed_images(billed_images)
                if paths:
                    exc.partial_paths = paths
                raise
        return paths

    def _payload(self, request, reference_paths):
        effective_request = request
        size = request.size
        if size == "4K":
            # Seedream 5.0 Flash tops out at 2K; keep the fallback request valid.
            size = "2K"
            effective_request = request.model_copy(update={"size": size})
        elif "x" in size:
            width, height = map(int, size.split("x"))
            if (
                width > MAX_REFERENCE_EDGE
                or height > MAX_REFERENCE_EDGE
                or not 921_600 <= width * height <= 4_622_200
                or not 1 / 16 <= width / height <= 16
            ):
                raise GatewayError(
                    "seedream_size_unsupported",
                    "Seedream 5.0 Flash 自定义尺寸需满足 921600–4622200 总像素、单边不超过 6000；请改用 auto、1K 或 2K。",
                    422,
                )
        payload = {
            "model": SEEDREAM_MODEL,
            "prompt": effective_request.render_prompt(
                has_references=bool(reference_paths)
            ),
            "response_format": "b64_json",
            "watermark": False,
        }
        if size != "auto":
            payload["size"] = size
        if reference_paths:
            images = []
            for path in reference_paths:
                mime_type, contents = self._reference_data(path)
                encoded = base64.b64encode(contents).decode("ascii")
                images.append(f"data:{mime_type};base64,{encoded}")
            payload["image"] = images[0] if len(images) == 1 else images
        return payload

    @staticmethod
    def _reference_data(path: Path):
        contents = path.read_bytes()
        mime_type = mimetypes.guess_type(path.name)[0] or "image/png"
        try:
            with Image.open(io.BytesIO(contents)) as source:
                image = source.copy()
        except (OSError, UnidentifiedImageError):
            raise GatewayError(
                "seedream_invalid_reference", "Seedream 无法读取这张参考图。", 422
            ) from None
        if (
            len(contents) <= MAX_REFERENCE_BYTES
            and max(image.size) <= MAX_REFERENCE_EDGE
        ):
            return mime_type, contents

        image.thumbnail(
            (MAX_REFERENCE_EDGE, MAX_REFERENCE_EDGE), Image.Resampling.LANCZOS
        )
        image = image.convert("RGBA" if "A" in image.getbands() else "RGB")
        for _ in range(8):
            for quality in (90, 82, 74, 66):
                output = io.BytesIO()
                image.save(output, format="WEBP", quality=quality, method=6)
                compressed = output.getvalue()
                if len(compressed) <= MAX_REFERENCE_BYTES:
                    return "image/webp", compressed
            image.thumbnail(
                (max(64, int(image.width * 0.8)), max(64, int(image.height * 0.8))),
                Image.Resampling.LANCZOS,
            )
        raise GatewayError(
            "seedream_reference_too_large",
            "参考图压缩后仍超过 Seedream 5.0 Flash 的 30 MB 限制。",
            413,
        )

    def _generate_one(self, payload):
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        request = urllib.request.Request(
            f"{SEEDREAM_BASE_URL}/images/generations",
            data=body,
            headers={
                "Authorization": f"Bearer {self.api_key}",
                "Content-Type": "application/json",
                "Accept": "application/json",
            },
            method="POST",
        )
        try:
            with urllib.request.urlopen(request, timeout=self.timeout) as response:
                response_body = response.read(MAX_IMAGE_BYTES + 1)
        except urllib.error.HTTPError as exc:
            self._raise_http_error(exc.code)
        except (urllib.error.URLError, TimeoutError, OSError):
            raise GatewayError(
                "seedream_network_error", "Seedream 服务暂时无法连接，请稍后重试。", 502
            ) from None
        if len(response_body) > MAX_IMAGE_BYTES:
            raise GatewayError(
                "seedream_response_too_large", "Seedream 返回内容过大。", 502
            )
        try:
            result = json.loads(response_body)
            data = result.get("data") or []
            usage = result.get("usage") or {}
            reported_images = usage.get("generated_images")
            if (
                isinstance(reported_images, int)
                and not isinstance(reported_images, bool)
                and reported_images >= 0
            ):
                billed_images = reported_images or len(data)
            else:
                billed_images = len(data)
            item = data[0]
            encoded = item.get("b64_json")
            if encoded:
                image = base64.b64decode(encoded, validate=True)
            elif item.get("url"):
                image = self._download_image(item["url"])
            else:
                raise ValueError
        except GatewayError as exc:
            if "billed_images" in locals() and billed_images:
                exc.billed_images = billed_images
            raise
        except (ValueError, TypeError, KeyError, IndexError, json.JSONDecodeError):
            error = GatewayError(
                "seedream_invalid_response", "Seedream 没有返回可用图片。", 502
            )
            if "billed_images" in locals() and billed_images:
                error.billed_images = billed_images
            raise error from None
        if not image or len(image) > MAX_IMAGE_BYTES:
            error = GatewayError(
                "seedream_invalid_response", "Seedream 返回的图片为空或过大。", 502
            )
            if billed_images:
                error.billed_images = billed_images
            raise error
        return image, billed_images

    @staticmethod
    def _download_image(url):
        try:
            parsed = urllib.parse.urlsplit(url)
            if parsed.scheme != "https" or not parsed.hostname or parsed.username:
                raise ValueError
            request = urllib.request.Request(url, headers={"Accept": "image/*"})
            with urllib.request.urlopen(request, timeout=60) as response:
                image = response.read(MAX_IMAGE_BYTES + 1)
        except (ValueError, urllib.error.URLError, TimeoutError, OSError):
            raise GatewayError(
                "seedream_download_failed", "无法下载 Seedream 返回的图片。", 502
            ) from None
        if len(image) > MAX_IMAGE_BYTES:
            raise GatewayError(
                "seedream_response_too_large", "Seedream 返回的图片过大。", 502
            )
        return image

    @staticmethod
    def _raise_http_error(status):
        if status == 401 or status == 403:
            raise GatewayError(
                "seedream_authentication_failed",
                "火山方舟 API Key 无效或没有该模型的调用权限，请在平台设置中检查。",
                502,
            ) from None
        if status == 429:
            raise GatewayError(
                "seedream_rate_limited",
                "Seedream 请求受限或额度不足，请检查火山方舟资源包与账户余额。",
                502,
            ) from None
        raise GatewayError(
            "seedream_request_failed",
            "Seedream 生成失败，请检查火山方舟模型权限、参数和账户状态。",
            502,
        ) from None
