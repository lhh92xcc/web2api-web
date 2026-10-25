"""Google Labs ImageFX web connector.

ImageFX is a consumer web product rather than a paid developer API.  The
connector uses the user's own Google Labs session: a copied Cookie header is
exchanged for the short-lived access token that the ImageFX page itself uses,
then the image request is sent to the first-party ImageFX endpoint.  It never
creates an account, bypasses a challenge, or attempts to turn a paid API into
a free one.
"""

import asyncio
import base64
import binascii
import json
import random
import re
import time
import urllib.error
import urllib.request
import uuid
from pathlib import Path

from pydantic import BaseModel, ConfigDict, Field, field_validator

from storage import GatewayError, atomic_json

IMAGEFX_ID = "imagefx-web"
IMAGEFX_WEB_URL = "https://labs.google/fx/tools/image-fx"
IMAGEFX_BASE_URL = "https://labs.google"
IMAGEFX_SESSION_URL = f"{IMAGEFX_BASE_URL}/fx/api/auth/session"
IMAGEFX_GENERATE_URL = "https://aisandbox-pa.googleapis.com/v1:runImageFx"
IMAGEFX_MODEL = "imagefx-imagen-3.5"
IMAGEFX_MODEL_NAME = "IMAGEN_3_5"
IMAGEFX_MODELS = [IMAGEFX_MODEL]
IMAGEFX_MAX_RESPONSE_BYTES = 100 * 1024 * 1024
IMAGEFX_MAX_ERROR_BYTES = 64 * 1024
IMAGEFX_USER_AGENT = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"
)


class ImageFxCredentialRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)
    credential: str | None = Field(default=None, max_length=64 * 1024, repr=False)

    @field_validator("credential")
    @classmethod
    def credential_value(cls, value):
        if not value:
            return value
        value = value.strip()
        if value.lower().startswith("cookie:"):
            value = value.split(":", 1)[1].strip()
        # Cookie headers may contain spaces and semicolons, but control
        # characters would permit header injection and are never valid here.
        if any(ord(char) < 32 or ord(char) == 127 for char in value):
            raise ValueError("Google Labs 凭据不能包含控制字符。")
        return value


class ImageFxConfigStore:
    """Persist only the user's own Labs credential; never expose its value."""

    def __init__(self, state_dir: Path):
        self.path = state_dir / "imagefx.json"
        self.credential = ""
        if self.path.exists():
            try:
                raw = json.loads(self.path.read_text(encoding="utf-8"))
                self.credential = str(
                    raw.get("credential", raw.get("cookie", raw.get("token", "")))
                ).strip()
            except (OSError, TypeError, ValueError):
                self.credential = ""

    @property
    def configured(self):
        return bool(self.credential)

    def public(self):
        return {
            "id": IMAGEFX_ID,
            "name": "Google Labs ImageFX 网页",
            "web_url": IMAGEFX_WEB_URL,
            "base_url": IMAGEFX_BASE_URL,
            "configured": self.configured,
            "auth_type": "google_cookie_or_token",
            "credential_label": "Google Labs Cookie Header 或 Bearer Token",
            "verification_status": "web_candidate",
            "verification_label": "网页候选 · 需账号实测",
            "free_bucket": "ImageFX 网页免费额度",
            "browser_required": False,
            "free_note": (
                "使用 Google Labs ImageFX 网页账号自己的免费网页额度；" 
                "额度、地区和模型权限以 ImageFX 页面为准。"
            ),
            "images": True,
            "videos": False,
            "text": False,
            "references": False,
            "models": list(IMAGEFX_MODELS),
            "image_models": list(IMAGEFX_MODELS),
            "image_default_model": IMAGEFX_MODEL,
            "fit": "Q3 / Q2 图片替代；Google Imagen 网页入口",
            "capabilities": "ImageFX 文生图；不需要常驻浏览器",
            "docs_url": IMAGEFX_WEB_URL,
        }

    def save(self, credential: str | None):
        try:
            value = ImageFxCredentialRequest(credential=credential).credential or ""
        except ValueError as exc:
            raise GatewayError("invalid_imagefx_credential", str(exc), 422) from None
        if not value:
            raise GatewayError(
                "credential_required",
                "请填写 Google Labs Cookie Header 或 Bearer Token。",
                422,
            )
        try:
            atomic_json(self.path, {"credential": value})
        except OSError:
            raise GatewayError("storage_error", "ImageFX 凭据保存失败。", 500) from None
        self.credential = value
        return self.public()

    def clear(self):
        try:
            self.path.unlink(missing_ok=True)
        except OSError:
            raise GatewayError("storage_error", "ImageFX 凭据删除失败。", 500) from None
        self.credential = ""
        return self.public()


def _looks_like_bearer(value: str) -> bool:
    value = value.strip()
    if value.lower().startswith("bearer "):
        return True
    # Raw OAuth/JWT-like tokens are accepted as a convenience.  Cookie
    # headers contain '=' or ';' and therefore do not match this branch.
    return ";" not in value and "=" not in value and (
        value.startswith(("ya29.", "eyJ")) or value.count(".") >= 2
    )


class ImageFxBackend:
    """Call ImageFX with a user-owned Labs session and save PNG outputs."""

    def __init__(self, credential: str, timeout: int = 450):
        if not credential:
            raise GatewayError("imagefx_not_configured", "请先配置 ImageFX 网页凭据。", 409)
        self.credential = credential.strip()
        self.timeout = timeout

    def _credential_parts(self):
        value = self.credential
        if value.lower().startswith("bearer "):
            return value[7:].strip(), ""
        if _looks_like_bearer(value):
            return value, ""
        return "", value

    @staticmethod
    def _error_code(message: str, status: int | None = None):
        lower = (message or "").lower()
        if status in {401, 403} or any(
            term in lower
            for term in ("unauthenticated", "authentication", "access denied", "login")
        ):
            return "imagefx_authentication_failed"
        if status == 429 or re.search(
            r"quota|limit|exhaust|too many|rate.?limit|额度|配额|上限|次数", lower
        ):
            return "imagefx_quota_exceeded"
        return "imagefx_upstream_error"

    def _request(self, url: str, *, method="GET", body=None, headers=None):
        request = urllib.request.Request(
            url,
            data=body,
            headers={
                "Accept": "application/json",
                "User-Agent": IMAGEFX_USER_AGENT,
                "Origin": "https://labs.google",
                "Referer": IMAGEFX_WEB_URL,
                **(headers or {}),
            },
            method=method,
        )
        try:
            with urllib.request.urlopen(request, timeout=self.timeout) as response:
                raw = response.read(IMAGEFX_MAX_RESPONSE_BYTES + 1)
                status = response.status
        except urllib.error.HTTPError as exc:
            try:
                detail = exc.read(IMAGEFX_MAX_ERROR_BYTES).decode("utf-8", "replace")
            except OSError:
                detail = ""
            code = self._error_code(detail, exc.code)
            messages = {
                "imagefx_authentication_failed": "ImageFX 登录态已失效，请从 Google Labs 更新 Cookie。",
                "imagefx_quota_exceeded": "ImageFX 网页额度或请求频率已达到上限。",
                "imagefx_upstream_error": f"ImageFX 网页请求失败（HTTP {exc.code}）。",
            }
            raise GatewayError(code, messages[code], 429 if code == "imagefx_quota_exceeded" else 502) from None
        except (urllib.error.URLError, TimeoutError, OSError):
            raise GatewayError("imagefx_network_error", "ImageFX 网页暂时无法连接。", 502) from None
        if len(raw) > IMAGEFX_MAX_RESPONSE_BYTES:
            raise GatewayError("imagefx_response_too_large", "ImageFX 返回内容过大。", 502)
        return status, raw

    def _auth_headers(self):
        token, cookie = self._credential_parts()
        if not token:
            status, raw = self._request(
                IMAGEFX_SESSION_URL,
                headers={"Cookie": cookie},
            )
            try:
                result = json.loads(raw)
                token = str(result.get("access_token", "")).strip()
            except (TypeError, ValueError, json.JSONDecodeError):
                token = ""
            if not token:
                raise GatewayError(
                    "imagefx_authentication_failed",
                    "Google Labs Cookie 未能换取 ImageFX session，请更新网页 Cookie。",
                    401,
                )
        headers = {"Authorization": f"Bearer {token}"}
        if cookie:
            headers["Cookie"] = cookie
        return headers

    @staticmethod
    def _aspect_ratio(request):
        value = request.aspect_ratio or request.composition() or "1:1"
        return {
            "1:1": "IMAGE_ASPECT_RATIO_SQUARE",
            "16:9": "IMAGE_ASPECT_RATIO_LANDSCAPE",
            "9:16": "IMAGE_ASPECT_RATIO_PORTRAIT",
            "4:3": "IMAGE_ASPECT_RATIO_LANDSCAPE_FOUR_THREE",
            "3:4": "IMAGE_ASPECT_RATIO_PORTRAIT",
        }.get(value, "IMAGE_ASPECT_RATIO_SQUARE")

    def _generate(self, request, count):
        headers = self._auth_headers()
        payload = {
            "userInput": {
                "candidatesCount": count,
                "prompts": [request.render_prompt()],
                "seed": random.SystemRandom().randint(1, 2**31 - 1),
            },
            "clientContext": {
                "sessionId": ";" + str(int(time.time() * 1000)),
                "tool": "IMAGE_FX",
            },
            "modelInput": {"modelNameType": IMAGEFX_MODEL_NAME},
            "aspectRatio": self._aspect_ratio(request),
        }
        status, raw = self._request(
            IMAGEFX_GENERATE_URL,
            method="POST",
            body=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
            headers={"Content-Type": "application/json", **headers},
        )
        try:
            value = json.loads(raw)
        except (TypeError, ValueError, json.JSONDecodeError):
            raise GatewayError("imagefx_invalid_response", "ImageFX 返回了无效响应。", 502) from None
        if not isinstance(value, dict):
            raise GatewayError("imagefx_invalid_response", "ImageFX 返回了无效响应。", 502)
        if value.get("error"):
            detail = value.get("error")
            message = detail.get("message", "") if isinstance(detail, dict) else str(detail)
            code = self._error_code(message, status)
            raise GatewayError(
                code,
                "ImageFX 网页额度或请求频率已达到上限。"
                if code == "imagefx_quota_exceeded"
                else "ImageFX 网页请求失败，请检查登录态和模型权限。",
                429 if code == "imagefx_quota_exceeded" else 502,
            )
        images = (value.get("imagePanels") or [{}])[0].get("generatedImages") or []
        if not images:
            raise GatewayError("imagefx_no_image", "ImageFX 已完成请求但没有返回图片。", 502)
        return images

    async def generate(self, request, folder: Path, remaining: int, reference_paths=None, **_kwargs):
        if reference_paths:
            raise GatewayError(
                "imagefx_references_unsupported",
                "ImageFX 网页接口当前只支持文生图；参考图请使用 Gemini、即梦或 Whisk。",
                422,
            )
        if request.model not in {"default", *IMAGEFX_MODELS}:
            raise GatewayError("invalid_model", "ImageFX 当前只支持 ImageFX Imagen 模型。", 422)
        count = max(1, min(int(remaining), 8))
        try:
            generated = await asyncio.to_thread(self._generate, request, count)
        except GatewayError:
            raise
        saved = []
        for item in generated[:remaining]:
            encoded = item.get("encodedImage") if isinstance(item, dict) else None
            if not isinstance(encoded, str) or not encoded:
                continue
            if "," in encoded and encoded.lower().startswith("data:"):
                encoded = encoded.split(",", 1)[1]
            try:
                contents = base64.b64decode(encoded, validate=True)
            except (ValueError, binascii.Error):
                continue
            if not contents or len(contents) > IMAGEFX_MAX_RESPONSE_BYTES:
                continue
            path = folder / f"imagefx-{uuid.uuid4().hex}.png"
            path.write_bytes(contents)
            saved.append(path)
        if not saved:
            raise GatewayError("imagefx_invalid_image", "ImageFX 返回了无法保存的图片。", 502)
        return saved
