"""SeaArt web-account image connector.

SeaArt exposes a first-party HTTP surface used by its web application.  This
adapter accepts a token copied from the user's own logged-in session and only
submits generation requests with ``speed_type=3`` (the web free-generation
bucket).  Paid coins are never used as a fallback.  A model ID is required in
the local credential JSON because SeaArt's model catalogue is account and
region dependent.

The adapter deliberately does not create a tourist session or generate a
device fingerprint.  If SeaArt requires a device ID for a particular account,
the user can supply the already existing value in the local JSON form.
"""

from __future__ import annotations

import asyncio
import json
import mimetypes
import random
import re
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid
from pathlib import Path

from pydantic import BaseModel, ConfigDict, Field, field_validator

from storage import GatewayError, atomic_json

SEAART_ID = "seaart-web"
SEAART_WEB_URL = "https://www.seaart.ai"
SEAART_BASE_URL = f"{SEAART_WEB_URL}/api/v1"
SEAART_UPLOAD_URL = f"{SEAART_WEB_URL}/api/upload/image-v2"
SEAART_DEFAULT_MODEL = "default"
SEAART_MODELS = [SEAART_DEFAULT_MODEL]
SEAART_MAX_RESPONSE_BYTES = 32 * 1024 * 1024
SEAART_MAX_IMAGE_BYTES = 100 * 1024 * 1024
SEAART_POLL_INTERVAL = 2
SEAART_USER_AGENT = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/142.0.0.0 Safari/537.36"
)


def _clean_header_value(value: str | None, label: str) -> str | None:
    if value is None:
        return None
    value = str(value).strip()
    if not value:
        return None
    if any(ord(char) < 32 or ord(char) == 127 for char in value):
        raise ValueError(f"SeaArt {label} 不能包含控制字符。")
    return value


class SeaArtCredential(BaseModel):
    """Fields saved locally for a SeaArt web session."""

    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    token: str = Field(min_length=1, max_length=64 * 1024, repr=False)
    device_id: str | None = Field(default=None, max_length=512, repr=False)
    model_no: str = Field(min_length=1, max_length=256)
    model_ver_no: str | None = Field(default=None, max_length=256)

    @field_validator("token")
    @classmethod
    def token_value(cls, value):
        value = _clean_header_value(value, "Token")
        if not value:
            raise ValueError("SeaArt Token 不能为空。")
        return value

    @field_validator("device_id")
    @classmethod
    def device_value(cls, value):
        return _clean_header_value(value, "device_id")

    @field_validator("model_no", "model_ver_no")
    @classmethod
    def model_value(cls, value):
        return _clean_header_value(value, "模型 ID")


class SeaArtConfigStore:
    """Persist SeaArt credentials without exposing the token in API output."""

    def __init__(self, state_dir: Path):
        self.path = state_dir / "seaart.json"
        self.credentials: dict = {}
        if self.path.exists():
            try:
                raw = json.loads(self.path.read_text(encoding="utf-8"))
                if isinstance(raw, dict):
                    self.credentials = SeaArtCredential.model_validate(raw).model_dump(
                        exclude_none=True
                    )
            except (OSError, TypeError, ValueError):
                self.credentials = {}

    @property
    def configured(self) -> bool:
        return bool(self.credentials.get("token"))

    def public(self) -> dict:
        return {
            "id": SEAART_ID,
            "name": "SeaArt 网页（免费生成桶）",
            "web_url": SEAART_WEB_URL,
            "base_url": SEAART_BASE_URL,
            "docs_url": SEAART_WEB_URL,
            "configured": self.configured,
            "auth_type": "web_token",
            "credential_label": "SeaArt Token + 模型 JSON",
            "verification_status": "web_quota_declared",
            "verification_label": "有明确免费桶 · 需账号实测",
            "free_bucket": "free_coins / temp_free_coins / try_out_limit",
            "browser_required": False,
            "supports_credits": True,
            "free_plan_only": True,
            "free_note": (
                "提交前查询 SeaArt 网页账户的 free_coins、temp_free_coins 和试用次数；"
                "固定使用 speed_type=3。付费 paid_coins 不会被使用，免费桶无法确认时不会提交。"
            ),
            "images": True,
            "videos": False,
            "text": False,
            "references": True,
            "models": list(SEAART_MODELS),
            "image_models": list(SEAART_MODELS),
            "image_default_model": SEAART_DEFAULT_MODEL,
            "fit": "Q3 / Q2 图片替代；网页免费桶与图生图",
            "capabilities": "SeaArt 网页文生图、单张参考图生图；不需要常驻浏览器",
        }

    def config(self) -> dict:
        if not self.configured:
            raise GatewayError(
                "seaart_not_configured",
                "请先填写 SeaArt Token 和 model_no JSON。",
                409,
            )
        return dict(self.credentials)

    @staticmethod
    def _parse(value) -> SeaArtCredential:
        if isinstance(value, SeaArtCredential):
            return value
        if isinstance(value, dict):
            raw = value
        elif isinstance(value, str):
            text = value.strip()
            if not text:
                raise ValueError("请填写 SeaArt 凭据 JSON。")
            try:
                raw = json.loads(text)
            except (TypeError, ValueError, json.JSONDecodeError):
                raise ValueError(
                    'SeaArt 凭据必须是 JSON，例如 {"token":"…","model_no":"…"}。'
                ) from None
        else:
            raise ValueError("SeaArt 凭据格式无效。")
        if not isinstance(raw, dict):
            raise ValueError("SeaArt 凭据必须是 JSON 对象。")
        return SeaArtCredential.model_validate(raw)

    def save(self, value):
        try:
            credentials = self._parse(value)
        except (TypeError, ValueError) as exc:
            raise GatewayError("invalid_seaart_credential", str(exc), 422) from None
        data = credentials.model_dump(exclude_none=True)
        try:
            atomic_json(self.path, data)
        except OSError:
            raise GatewayError("storage_error", "SeaArt 凭据保存失败。", 500) from None
        self.credentials = data
        return self.public()

    def clear(self):
        try:
            self.path.unlink(missing_ok=True)
        except OSError:
            raise GatewayError("storage_error", "SeaArt 凭据删除失败。", 500) from None
        self.credentials = {}
        return self.public()


class SeaArtBackend:
    """Direct HTTP image generation using SeaArt's web session."""

    def __init__(self, config: dict, timeout: int = 450):
        self.config = config
        self.timeout = max(10, min(int(timeout), 600))
        try:
            parsed = SeaArtCredential.model_validate(config)
        except (TypeError, ValueError) as exc:
            raise GatewayError("invalid_seaart_credential", "SeaArt 凭据格式无效。", 422) from exc
        self.token = parsed.token
        self.device_id = parsed.device_id
        self.model_no = parsed.model_no
        self.model_ver_no = parsed.model_ver_no or ""

    @staticmethod
    def _number(value):
        if isinstance(value, bool):
            return None
        try:
            number = float(value)
        except (TypeError, ValueError):
            return None
        if number != number or abs(number) == float("inf"):
            return None
        return int(number) if number.is_integer() else number

    @classmethod
    def _text(cls, value) -> str:
        if isinstance(value, str):
            return " ".join(value.split())[:500]
        if isinstance(value, dict):
            for key in ("msg", "message", "detail", "error", "reason", "status_desc"):
                text = cls._text(value.get(key))
                if text:
                    return text
        if isinstance(value, list):
            for item in value:
                text = cls._text(item)
                if text:
                    return text
        return ""

    @classmethod
    def _error(cls, status: int | None, value=None):
        text = cls._text(value)
        lower = text.lower()
        if status in {401, 403} or any(
            word in lower
            for word in ("login", "unauthorized", "authentication", "token", "登录", "认证")
        ):
            raise GatewayError(
                "seaart_authentication_failed",
                "SeaArt Token 无效或已失效，请从已登录网页重新复制。",
                401,
            )
        if status == 429 or any(
            word in lower
            for word in (
                "quota",
                "credit",
                "balance",
                "insufficient",
                "limit",
                "积分",
                "额度",
                "余额",
                "上限",
            )
        ):
            raise GatewayError(
                "seaart_free_quota_exceeded",
                "SeaArt 网页免费桶不足，本次未继续提交。",
                429,
            )
        if any(word in lower for word in ("captcha", "waf", "challenge", "verify", "验证")):
            raise GatewayError(
                "seaart_verification_required",
                "SeaArt 网页要求完成验证，请先在官方网页完成验证后重试。",
                403,
            )
        suffix = f"（HTTP {status}）" if status else ""
        raise GatewayError("seaart_upstream_error", f"SeaArt 网页请求失败{suffix}。", 502)

    def _headers(self, extra: dict | None = None) -> dict:
        headers = {
            "Accept": "application/json, text/plain, */*",
            "User-Agent": SEAART_USER_AGENT,
            "Origin": SEAART_WEB_URL,
            "Referer": SEAART_WEB_URL + "/",
            "x-app-id": "web_global_seaart",
            "token": self.token,
        }
        # A device ID is optional for an authenticated web token.  If a site
        # revision requests one, only forward the user's existing value.
        if self.device_id:
            headers["x-device-id"] = self.device_id
        headers.update(extra or {})
        return headers

    @classmethod
    def _decode(cls, raw: bytes):
        try:
            value = json.loads(raw.decode("utf-8", "replace"))
        except (TypeError, ValueError, UnicodeDecodeError):
            raise GatewayError("seaart_invalid_response", "SeaArt 返回了无效响应。", 502) from None
        if not isinstance(value, dict):
            raise GatewayError("seaart_invalid_response", "SeaArt 返回了无效响应。", 502)
        return value

    def _json_request(self, method: str, path: str, payload: dict | None = None):
        if path.startswith("http://") or path.startswith("https://"):
            url = path
        else:
            url = SEAART_BASE_URL.rstrip("/") + "/" + path.lstrip("/")
        parsed = urllib.parse.urlsplit(url)
        if parsed.scheme != "https" or parsed.hostname != urllib.parse.urlsplit(SEAART_WEB_URL).hostname:
            raise GatewayError("seaart_invalid_url", "SeaArt 请求地址不受支持。", 502)
        body = None
        headers = self._headers()
        if payload is not None:
            body = json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
            headers["Content-Type"] = "application/json"
        request = urllib.request.Request(url, data=body, headers=headers, method=method.upper())
        try:
            with urllib.request.urlopen(request, timeout=self.timeout) as response:
                raw = response.read(SEAART_MAX_RESPONSE_BYTES + 1)
                status = response.status
        except urllib.error.HTTPError as exc:
            try:
                detail = self._decode(exc.read(256 * 1024))
            except Exception:  # noqa: BLE001 - upstream details are not returned verbatim
                detail = {}
            self._error(exc.code, detail)
        except (urllib.error.URLError, TimeoutError, OSError):
            raise GatewayError("seaart_network_error", "SeaArt 网页暂时无法连接。", 502) from None
        if len(raw) > SEAART_MAX_RESPONSE_BYTES:
            raise GatewayError("seaart_response_too_large", "SeaArt 返回内容过大。", 502)
        value = self._decode(raw)
        status_info = value.get("status")
        if isinstance(status_info, dict):
            code = status_info.get("code")
            if code not in (None, "", 0, "0", 10000, "10000"):
                self._error(status, status_info)
        elif status >= 400:
            self._error(status, value)
        return value.get("data", value)

    def _upload(self, path: Path) -> str:
        raw = path.read_bytes()
        if len(raw) > SEAART_MAX_IMAGE_BYTES:
            raise GatewayError("seaart_reference_too_large", "SeaArt 参考图超过 100 MB。", 413)
        filename = path.name or "reference.png"
        content_type = mimetypes.guess_type(filename)[0] or "application/octet-stream"
        boundary = "----Web2APISeaArt" + uuid.uuid4().hex
        chunks = []

        def field(name: str, value: str):
            chunks.extend(
                [
                    f"--{boundary}\r\n".encode(),
                    f'Content-Disposition: form-data; name="{name}"\r\n\r\n'.encode(),
                    str(value).encode(),
                    b"\r\n",
                ]
            )

        field("filename", filename)
        field("category", "36")
        chunks.extend(
            [
                f"--{boundary}\r\n".encode(),
                (
                    f'Content-Disposition: form-data; name="file"; filename="{filename}"\r\n'
                    f"Content-Type: {content_type}\r\n\r\n"
                ).encode(),
                raw,
                b"\r\n",
                f"--{boundary}--\r\n".encode(),
            ]
        )
        request = urllib.request.Request(
            SEAART_UPLOAD_URL,
            data=b"".join(chunks),
            headers=self._headers({"Content-Type": f"multipart/form-data; boundary={boundary}"}),
            method="POST",
        )
        try:
            with urllib.request.urlopen(request, timeout=self.timeout) as response:
                response_raw = response.read(SEAART_MAX_RESPONSE_BYTES + 1)
                status = response.status
        except urllib.error.HTTPError as exc:
            try:
                detail = self._decode(exc.read(256 * 1024))
            except Exception:  # noqa: BLE001
                detail = {}
            self._error(exc.code, detail)
        except (urllib.error.URLError, TimeoutError, OSError):
            raise GatewayError("seaart_network_error", "SeaArt 图片上传暂时失败。", 502) from None
        if len(response_raw) > SEAART_MAX_RESPONSE_BYTES:
            raise GatewayError("seaart_response_too_large", "SeaArt 上传响应过大。", 502)
        value = self._decode(response_raw)
        status_info = value.get("status")
        if isinstance(status_info, dict) and status_info.get("code") not in (None, "", 0, "0", 10000, "10000"):
            self._error(status, status_info)
        url = self._find_url(value.get("data", value), ("url", "signed", "access_url", "image_url", "img_url"))
        if not url:
            raise GatewayError("seaart_upload_failed", "SeaArt 没有返回参考图地址。", 502)
        return url

    @classmethod
    def _walk(cls, value):
        if isinstance(value, dict):
            for key, item in value.items():
                yield str(key), item
                yield from cls._walk(item)
        elif isinstance(value, list):
            for item in value:
                yield from cls._walk(item)

    @classmethod
    def _find_url(cls, value, names):
        wanted = {name.lower() for name in names}
        if isinstance(value, dict):
            for key, item in value.items():
                if str(key).lower() in wanted and isinstance(item, str) and item.startswith("https://"):
                    return item
                found = cls._find_url(item, names)
                if found:
                    return found
        elif isinstance(value, list):
            for item in value:
                found = cls._find_url(item, names)
                if found:
                    return found
        return None

    @classmethod
    def _find_urls(cls, value):
        urls = []
        if isinstance(value, dict):
            for key, item in value.items():
                if str(key).lower() in {"url", "image_url", "img_url", "uri", "access_url"} and isinstance(item, str) and item.startswith("https://"):
                    urls.append(item)
                else:
                    urls.extend(cls._find_urls(item))
        elif isinstance(value, list):
            for item in value:
                urls.extend(cls._find_urls(item))
        return list(dict.fromkeys(urls))

    @classmethod
    def _find_task_id(cls, value):
        if isinstance(value, dict):
            for key in ("task_id", "taskId", "id"):
                candidate = value.get(key)
                if isinstance(candidate, (str, int)) and str(candidate):
                    return str(candidate)
            for item in value.values():
                found = cls._find_task_id(item)
                if found:
                    return found
        elif isinstance(value, list):
            for item in value:
                found = cls._find_task_id(item)
                if found:
                    return found
        return None

    @classmethod
    def _find_model_version(cls, value):
        if isinstance(value, dict):
            for key in ("model_ver_no", "model_version_no", "version_no"):
                candidate = value.get(key)
                if isinstance(candidate, str) and candidate.strip():
                    return candidate.strip()
            for item in value.values():
                found = cls._find_model_version(item)
                if found:
                    return found
        elif isinstance(value, list):
            for item in value:
                found = cls._find_model_version(item)
                if found:
                    return found
        return None

    async def _resolve_model_version(self):
        if self.model_ver_no:
            return
        data = await asyncio.to_thread(
            self._json_request,
            "POST",
            "model/detail",
            {"id": self.model_no, "scene": "model_detail_v2"},
        )
        version = self._find_model_version(data)
        if not version:
            raise GatewayError(
                "seaart_model_invalid",
                "SeaArt model_no 无法解析模型版本，请从网页模型链接补充 model_ver_no。",
                422,
            )
        self.model_ver_no = version

    @classmethod
    def _credit_snapshot(cls, value):
        if not isinstance(value, dict):
            return {"free_buckets_known": False, "free_available": None}
        # The API may wrap the wallet object under data/assets/balance.  Only
        # explicitly free-named fields authorize a generation request.
        def find_wallet(node):
            if isinstance(node, dict):
                if any(key in node for key in ("free_coins", "temp_free_coins", "try_out_limit")):
                    return node
                for child in node.values():
                    found = find_wallet(child)
                    if found is not None:
                        return found
            elif isinstance(node, list):
                for child in node:
                    found = find_wallet(child)
                    if found is not None:
                        return found
            return None

        wallet = find_wallet(value) or value
        free_coins = cls._number(wallet.get("free_coins"))
        temp_free = cls._number(wallet.get("temp_free_coins"))
        try_out = wallet.get("try_out_limit")
        try_out_total = None
        if isinstance(try_out, dict):
            numbers = [cls._number(item) for item in try_out.values()]
            numbers = [item for item in numbers if item is not None and item >= 0]
            try_out_total = sum(numbers) if numbers else 0
        elif (number := cls._number(try_out)) is not None:
            try_out_total = max(0, number)
        known = any(key in wallet for key in ("free_coins", "temp_free_coins", "try_out_limit"))
        parts = [item for item in (free_coins, temp_free, try_out_total) if item is not None]
        available = sum(parts) if parts else None
        return {
            "free_coins": free_coins,
            "temp_free_coins": temp_free,
            "try_out_limit": try_out_total,
            "paid_coins": cls._number(wallet.get("paid_coins")),
            "free_available": available,
            "free_buckets_known": bool(known),
        }

    async def credits(self):
        data = await asyncio.to_thread(self._json_request, "POST", "payment/assets/get", {})
        return {"provider": SEAART_ID, "credits": self._credit_snapshot(data)}

    async def _require_free_credit(self):
        result = await self.credits()
        credits = result.get("credits") or {}
        if not credits.get("free_buckets_known"):
            raise GatewayError(
                "seaart_free_quota_unverified",
                "无法确认 SeaArt 网页免费额度；为避免误扣付费 coins，本次未提交任务。",
                409,
            )
        if (credits.get("free_available") or 0) <= 0:
            raise GatewayError(
                "seaart_free_quota_exceeded",
                "SeaArt 网页免费额度为 0，本次未提交任务。",
                429,
            )
        return result

    @staticmethod
    def _dimensions(request):
        ratio = request.aspect_ratio or request.composition() or "1:1"
        width, height = {
            "1:1": (1024, 1024),
            "16:9": (1344, 768),
            "9:16": (768, 1344),
            "4:3": (1152, 864),
            "3:4": (864, 1152),
            "3:2": (1216, 832),
            "2:3": (832, 1216),
        }.get(ratio, (1024, 1024))
        if request.size.upper() in {"1K", "2K", "4K"}:
            factor = {"1K": 1, "2K": 2, "4K": 2}[request.size.upper()]
            width, height = width * factor, height * factor
        elif "x" in request.size:
            try:
                width, height = (int(value) for value in request.size.split("x", 1))
                scale = min(4096 / max(width, height), 1)
                width, height = round(width * scale), round(height * scale)
            except (TypeError, ValueError):
                pass
        return max(384, width), max(384, height)

    def _payload(self, request, prompt: str, num_images: int, image_url: str | None = None):
        width, height = self._dimensions(request)
        meta = {
            "prompt": prompt,
            "negative_prompt": request.negative_prompt or "",
            "width": width,
            "height": height,
            "steps": 30 if image_url is None else 25,
            "cfg_scale": 5 if image_url is None else 6,
            "sampler_name": "Euler",
            "n_iter": max(1, min(int(num_images), 4)),
            "seed": random.SystemRandom().randint(0, 999_999_999),
            "lora_models": [],
            "embeddings": [],
            "clip_skip": 2,
            "generate": {
                "anime_enhance": 0,
                "mode": 0,
                "gen_mode": 1 if image_url is None else 0,
                "prompt_magic_mode": 2,
            },
        }
        if image_url:
            meta["init_images"] = [image_url]
            meta["denoising_strength"] = 0.35
        else:
            meta["lab_base"] = {"conds": []}
        payload = {
            "model_no": self.model_no,
            "channel_id": "",
            # SeaArt's SDK maps free_creation=True to speed_type=3.
            "speed_type": 3,
            "meta": meta,
        }
        if self.model_ver_no:
            payload["model_ver_no"] = self.model_ver_no
        return payload

    async def _wait(self, task_id: str, folder: Path):
        deadline = time.monotonic() + self.timeout
        while time.monotonic() < deadline:
            data = await asyncio.to_thread(
                self._json_request, "POST", "task/batch-progress", {"task_ids": [task_id]}
            )
            items = data.get("items", []) if isinstance(data, dict) else []
            item = next(
                (row for row in items if isinstance(row, dict) and str(row.get("task_id", row.get("id", ""))) == task_id),
                items[0] if items and isinstance(items[0], dict) else None,
            )
            if not item:
                await asyncio.sleep(SEAART_POLL_INTERVAL)
                continue
            urls = self._find_urls(item.get("img_uris") or item.get("images") or item)
            process = self._number(item.get("process")) or 0
            status = str(item.get("status", "")).lower()
            status_desc = self._text(item.get("status_desc"))
            if urls and (process >= 100 or status in {"2", "3", "done", "success", "succeeded"}):
                saved = []
                for url in urls:
                    path = await self._download(url, folder)
                    saved.append(path)
                return saved
            if status in {"4", "5", "failed", "error", "cancelled", "canceled"} or any(
                word in status_desc.lower() for word in ("fail", "error", "失败", "拒绝", "quota", "credit", "额度")
            ):
                self._error(429 if any(word in status_desc.lower() for word in ("quota", "credit", "额度")) else 502, item)
            await asyncio.sleep(min(SEAART_POLL_INTERVAL, max(0.01, deadline - time.monotonic())))
        raise GatewayError("seaart_timeout", "等待 SeaArt 图片超时，请到网页任务记录查看状态。", 504)

    async def _download(self, url: str, folder: Path):
        parsed = urllib.parse.urlsplit(url)
        if parsed.scheme != "https" or not parsed.hostname:
            raise GatewayError("seaart_invalid_response", "SeaArt 返回的图片地址无效。", 502)
        request = urllib.request.Request(
            url,
            headers=self._headers({"Accept": "image/*"}),
            method="GET",
        )
        try:
            with urllib.request.urlopen(request, timeout=self.timeout) as response:
                raw = response.read(SEAART_MAX_IMAGE_BYTES + 1)
                content_type = response.headers.get("Content-Type", "")
        except (urllib.error.URLError, urllib.error.HTTPError, TimeoutError, OSError):
            raise GatewayError("seaart_download_failed", "SeaArt 图片下载失败。", 502) from None
        if len(raw) > SEAART_MAX_IMAGE_BYTES:
            raise GatewayError("seaart_result_too_large", "SeaArt 返回图片超过本地保存上限。", 413)
        suffix = self._image_suffix(raw, content_type)
        if not suffix:
            raise GatewayError("seaart_invalid_response", "SeaArt 返回的文件不是图片。", 502)
        path = folder / f"seaart-{uuid.uuid4().hex}{suffix}"
        path.write_bytes(raw)
        return path

    @staticmethod
    def _image_suffix(raw: bytes, content_type: str = "") -> str | None:
        normalized = content_type.split(";", 1)[0].strip().lower()
        if normalized in {"image/png", "image/jpeg", "image/jpg", "image/webp", "image/gif"}:
            return {"image/png": ".png", "image/jpeg": ".jpg", "image/jpg": ".jpg", "image/webp": ".webp", "image/gif": ".gif"}[normalized]
        if raw.startswith(b"\x89PNG\r\n\x1a\n"):
            return ".png"
        if raw.startswith(b"\xff\xd8\xff"):
            return ".jpg"
        if raw[:4] == b"RIFF" and raw[8:12] == b"WEBP":
            return ".webp"
        if raw.startswith((b"GIF87a", b"GIF89a")):
            return ".gif"
        return None

    async def generate_image(self, request, folder: Path, reference_paths=None):
        await self._require_free_credit()
        await self._resolve_model_version()
        refs = list(reference_paths or [])
        if len(refs) > 1:
            raise GatewayError("reference_count", "SeaArt 网页图生图当前最多支持 1 张参考图。", 422)
        image_url = await asyncio.to_thread(self._upload, Path(refs[0])) if refs else None
        prompt = request.render_prompt(has_references=bool(refs))
        endpoint = "task/v2/img-to-img" if image_url else "task/v2/text-to-img"
        data = await asyncio.to_thread(
            self._json_request,
            "POST",
            endpoint,
            self._payload(request, prompt, request.n, image_url),
        )
        task_id = self._find_task_id(data)
        if not task_id:
            raise GatewayError("seaart_invalid_response", "SeaArt 没有返回任务 ID。", 502)
        return await self._wait(task_id, folder)
