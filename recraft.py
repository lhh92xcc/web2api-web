"""Recraft web connector using a user's own Free plan session token.

The Recraft consumer web application exposes a small first party API that is
also used by the web page.  This adapter intentionally accepts only a
user-provided Bearer token, verifies that the account is on Recraft's Free
plan, and spends only ``subscription_credits``.  Purchased/top-up credits are
never used as a fallback.
"""

import asyncio
import json
import re
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid
from pathlib import Path

from pydantic import BaseModel, ConfigDict, Field, field_validator

from storage import GatewayError, atomic_json

RECRAFT_ID = "recraft-web"
RECRAFT_WEB_URL = "https://www.recraft.ai/"
RECRAFT_BASE_URL = "https://api.recraft.ai"
RECRAFT_DEFAULT_MODEL = "gpt_image_2p5_flare"
RECRAFT_MODELS = [RECRAFT_DEFAULT_MODEL]
RECRAFT_MAX_RESPONSE_BYTES = 32 * 1024 * 1024
RECRAFT_MAX_IMAGE_BYTES = 100 * 1024 * 1024
RECRAFT_POLL_INTERVAL = 2
RECRAFT_USER_AGENT = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"
)


class RecraftCredentialRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    credential: str | None = Field(default=None, max_length=64 * 1024, repr=False)

    @field_validator("credential")
    @classmethod
    def credential_value(cls, value):
        if not value:
            return value
        value = value.strip()
        if value.lower().startswith("bearer "):
            value = value[7:].strip()
        if not value or any(ord(char) < 32 or ord(char) == 127 for char in value):
            raise ValueError("Recraft Bearer Token 不能包含控制字符。")
        return value


class RecraftConfigStore:
    """Persist a Recraft web token without ever exposing the token."""

    def __init__(self, state_dir: Path):
        self.path = state_dir / "recraft.json"
        self.credential = ""
        if self.path.exists():
            try:
                raw = json.loads(self.path.read_text(encoding="utf-8"))
                self.credential = str(
                    raw.get("credential", raw.get("token", ""))
                ).strip()
            except (AttributeError, OSError, TypeError, ValueError):
                self.credential = ""

    @property
    def configured(self):
        return bool(self.credential)

    def public(self):
        return {
            "id": RECRAFT_ID,
            "name": "Recraft 网页 Free plan",
            "web_url": RECRAFT_WEB_URL,
            "base_url": RECRAFT_BASE_URL,
            "docs_url": RECRAFT_WEB_URL,
            "configured": self.configured,
            "auth_type": "bearer_token",
            "credential_label": "Recraft 网页 Bearer Token",
            "free_plan_only": True,
            "verification_status": "web_candidate",
            "verification_label": "网页候选 · 需账号实测",
            "free_bucket": "Free plan subscription credits",
            "browser_required": False,
            "free_note": (
                "只允许 Recraft Free plan 的 subscription credits；"
                "不会使用购买/充值 credits。Free plan 图片按 Recraft 规则公开展示，"
                "请确认符合你的使用场景。"
            ),
            "images": True,
            "videos": False,
            "text": False,
            "references": False,
            "models": list(RECRAFT_MODELS),
            "image_models": list(RECRAFT_MODELS),
            "image_default_model": RECRAFT_DEFAULT_MODEL,
            "fit": "Q3 / Q2 图片替代；网页 Free plan",
            "capabilities": "Recraft 网页文生图；不需要常驻浏览器",
        }

    def save(self, credential: str | None):
        try:
            value = RecraftCredentialRequest(credential=credential).credential or ""
        except ValueError as exc:
            raise GatewayError("invalid_recraft_credential", str(exc), 422) from None
        if not value:
            raise GatewayError(
                "credential_required", "请填写 Recraft 网页 Bearer Token。", 422
            )
        try:
            atomic_json(self.path, {"credential": value})
        except OSError:
            raise GatewayError("storage_error", "Recraft 凭据保存失败。", 500) from None
        self.credential = value
        return self.public()

    def clear(self):
        try:
            self.path.unlink(missing_ok=True)
        except OSError:
            raise GatewayError("storage_error", "Recraft 凭据删除失败。", 500) from None
        self.credential = ""
        return self.public()


class RecraftBackend:
    """Generate images through Recraft's first party web API."""

    def __init__(self, credential: str, timeout: int = 450):
        if not credential:
            raise GatewayError("recraft_not_configured", "请先配置 Recraft 网页 Token。", 409)
        self.credential = credential.strip()
        if self.credential.lower().startswith("bearer "):
            self.credential = self.credential[7:].strip()
        self.timeout = max(10, timeout)

    def _headers(self, *, accept="application/json"):
        return {
            "Accept": accept,
            "Authorization": f"Bearer {self.credential}",
            "Content-Type": "application/json",
            "User-Agent": RECRAFT_USER_AGENT,
            "Origin": "https://www.recraft.ai",
            "Referer": RECRAFT_WEB_URL,
            "X-Client-Type": "web",
        }

    @staticmethod
    def _error_code(status: int | None, detail: str = ""):
        lower = (detail or "").lower()
        if status in {401, 403} or any(
            word in lower for word in ("unauthorized", "authentication", "invalid token", "login")
        ):
            return "recraft_authentication_failed"
        if status == 429 or re.search(r"quota|credit|limit|rate.?limit|额度|配额|上限", lower):
            return "recraft_quota_exceeded"
        return "recraft_upstream_error"

    def _request(self, path: str, *, method="GET", body=None, accept="application/json"):
        url = path if path.startswith("http") else urllib.parse.urljoin(RECRAFT_BASE_URL, path)
        request = urllib.request.Request(
            url,
            data=body,
            headers=self._headers(accept=accept),
            method=method,
        )
        try:
            with urllib.request.urlopen(request, timeout=self.timeout) as response:
                raw = response.read(RECRAFT_MAX_RESPONSE_BYTES + 1)
                status = response.status
                content_type = response.headers.get("Content-Type", "")
        except urllib.error.HTTPError as exc:
            try:
                detail = exc.read(64 * 1024).decode("utf-8", "replace")
            except OSError:
                detail = ""
            code = self._error_code(exc.code, detail)
            messages = {
                "recraft_authentication_failed": "Recraft 网页 Token 已失效，请从已登录网页更新。",
                "recraft_quota_exceeded": "Recraft Free plan 的 subscription credits 不足。",
                "recraft_upstream_error": f"Recraft 网页请求失败（HTTP {exc.code}）。",
            }
            raise GatewayError(
                code,
                messages[code],
                401 if code == "recraft_authentication_failed" else 429 if code == "recraft_quota_exceeded" else 502,
            ) from None
        except (urllib.error.URLError, TimeoutError, OSError):
            raise GatewayError("recraft_network_error", "Recraft 网页暂时无法连接。", 502) from None
        if len(raw) > RECRAFT_MAX_RESPONSE_BYTES:
            raise GatewayError("recraft_response_too_large", "Recraft 返回内容过大。", 502)
        return status, raw, content_type

    def _json(self, path: str, *, method="GET", payload=None):
        body = (
            json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
            if payload is not None
            else None
        )
        _, raw, _ = self._request(path, method=method, body=body)
        try:
            value = json.loads(raw)
        except (TypeError, ValueError, json.JSONDecodeError):
            raise GatewayError("recraft_invalid_response", "Recraft 返回了无效响应。", 502) from None
        if not isinstance(value, dict):
            raise GatewayError("recraft_invalid_response", "Recraft 返回了无效响应。", 502)
        return value

    @staticmethod
    def _credits(value):
        raw = value.get("subscription_credits")
        if isinstance(raw, dict):
            raw = raw.get("remaining", raw.get("amount", raw.get("credits")))
        if isinstance(raw, bool):
            return None
        if isinstance(raw, (int, float)):
            return float(raw)
        if isinstance(raw, str):
            try:
                return float(raw.strip())
            except ValueError:
                return None
        return None

    def _free_credits(self, required: int):
        value = self._json("/users/me")
        plan = value.get("plan")
        if not isinstance(plan, dict) or plan.get("is_free") is not True:
            raise GatewayError(
                "recraft_free_plan_required",
                "Recraft 账号不是已确认的 Free plan；为避免误用付费额度，已拒绝提交。",
                403,
            )
        credits = self._credits(value)
        if credits is None:
            raise GatewayError(
                "recraft_quota_unknown",
                "Recraft 未返回可确认的 subscription credits，已拒绝提交。",
                409,
            )
        if credits < required:
            raise GatewayError(
                "recraft_quota_exceeded",
                f"Recraft Free plan subscription credits 不足（需要 {required}，可用 {credits:g}）。",
                429,
            )
        return credits

    def _start(self, request, count):
        self._free_credits(count)
        model = request.model if request.model != "default" else RECRAFT_DEFAULT_MODEL
        if model not in RECRAFT_MODELS:
            raise GatewayError("invalid_model", "Recraft 当前网页模型不可用。", 422)
        payload = {
            "prompt": request.render_prompt(),
            "image_type": "raster",
            "num_images_per_prompt": count,
            "aspect_ratio": request.aspect_ratio or request.composition() or "1:1",
            "negative_prompt": request.negative_prompt or "",
            "transform_model": model,
        }
        value = self._json("/queue_recraft/prompt_to_image", method="POST", payload=payload)
        operation_id = value.get("operation_id") or value.get("id")
        if not isinstance(operation_id, str) or not operation_id:
            raise GatewayError("recraft_invalid_response", "Recraft 没有返回生成任务 ID。", 502)
        return operation_id

    def _poll(self, operation_id):
        deadline = time.monotonic() + self.timeout
        query = urllib.parse.urlencode(
            {"operation_id": operation_id, "request_image_data": "true"}
        )
        while time.monotonic() < deadline:
            value = self._json(f"/poll_recraft?{query}")
            status = str(value.get("status", "")).lower()
            if value.get("error") or status in {"failed", "error", "cancelled", "canceled"}:
                raise GatewayError("recraft_generation_failed", "Recraft 图片生成失败。", 502)
            images = value.get("images")
            if isinstance(images, list) and images:
                return images
            if status in {"succeeded", "success", "completed", "complete", "done"}:
                raise GatewayError("recraft_no_image", "Recraft 已完成请求但没有返回图片。", 502)
            time.sleep(RECRAFT_POLL_INTERVAL)
        raise GatewayError("recraft_timeout", "等待 Recraft 图片生成超时，请到网页任务记录查看。", 504)

    def _download(self, image, folder: Path):
        image_id = image.get("image_id") if isinstance(image, dict) else None
        if not isinstance(image_id, str) or not image_id:
            raise GatewayError("recraft_invalid_response", "Recraft 没有返回可下载的图片 ID。", 502)
        _, raw, content_type = self._request(
            "/image/" + urllib.parse.quote(image_id, safe=""),
            accept="image/*",
        )
        if len(raw) > RECRAFT_MAX_IMAGE_BYTES or not raw:
            raise GatewayError("recraft_download_failed", "Recraft 图片为空或超过本地保存上限。", 502)
        extension = ".png"
        if "jpeg" in content_type.lower() or "jpg" in content_type.lower():
            extension = ".jpg"
        elif "webp" in content_type.lower():
            extension = ".webp"
        path = folder / f"recraft-{uuid.uuid4().hex}{extension}"
        path.write_bytes(raw)
        return path

    async def generate(self, request, folder: Path, remaining: int, reference_paths=None, **_kwargs):
        if reference_paths:
            raise GatewayError(
                "recraft_references_unsupported",
                "Recraft 网页接口当前只支持文生图；参考图请使用 Gemini 或其他支持图生图的平台。",
                422,
            )
        count = max(1, min(int(remaining), 4))
        operation_id = await asyncio.to_thread(self._start, request, count)
        images = await asyncio.to_thread(self._poll, operation_id)
        saved = []
        for image in images[:remaining]:
            saved.append(await asyncio.to_thread(self._download, image, folder))
        if not saved:
            raise GatewayError("recraft_no_image", "Recraft 没有返回可保存的图片。", 502)
        return saved
