"""Legacy Google Labs Whisk connector.

Google has migrated the Whisk page to Google Flow.  The old Whisk HTTP
endpoints are retained only as a compatibility path for accounts where the
legacy route still responds; they are deliberately marked as a candidate and
must not be presented as a stable current Web2API.  The adapter accepts only a
credential copied from the user's own Labs session and never calls a paid
developer API or fabricates a browser/device identity.
"""

from __future__ import annotations

import asyncio
import base64
import binascii
import json
import re
import time
import urllib.error
import urllib.request
import uuid
from pathlib import Path

from pydantic import BaseModel, ConfigDict, Field, field_validator

from storage import GatewayError, atomic_json

WHISK_ID = "whisk-web"
WHISK_WEB_URL = "https://flow.google.com/?from=whisk"
WHISK_LEGACY_URL = "https://labs.google/fx/tools/whisk/project"
WHISK_BASE_URL = "https://labs.google"
WHISK_SESSION_URL = f"{WHISK_BASE_URL}/fx/api/auth/session"
WHISK_CREATE_WORKFLOW_URL = (
    f"{WHISK_BASE_URL}/fx/api/trpc/media.createOrUpdateWorkflow"
)
WHISK_CAPTION_URL = f"{WHISK_BASE_URL}/fx/api/trpc/backbone.captionImage"
WHISK_UPLOAD_URL = f"{WHISK_BASE_URL}/fx/api/trpc/backbone.uploadImage"
WHISK_GENERATE_URL = "https://aisandbox-pa.googleapis.com/v1/whisk:generateImage"
WHISK_RECIPE_URL = "https://aisandbox-pa.googleapis.com/v1/whisk:runImageRecipe"
WHISK_MODEL = "whisk-imagen-3.5"
WHISK_EDIT_MODEL = "whisk-gem-pix"
WHISK_MODELS = [WHISK_MODEL, WHISK_EDIT_MODEL]
WHISK_MAX_RESPONSE_BYTES = 100 * 1024 * 1024
WHISK_MAX_ERROR_BYTES = 128 * 1024
WHISK_MAX_IMAGE_BYTES = 100 * 1024 * 1024
WHISK_MAX_REFERENCE_BYTES = 32 * 1024 * 1024
WHISK_USER_AGENT = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"
)


class WhiskCredentialRequest(BaseModel):
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
        if any(ord(char) < 32 or ord(char) == 127 for char in value):
            raise ValueError("Whisk Cookie 或 Token 不能包含控制字符。")
        return value


class WhiskConfigStore:
    """Persist the user's Labs credential without exposing it in API output."""

    def __init__(self, state_dir: Path):
        self.path = state_dir / "whisk.json"
        self.credential = ""
        if self.path.exists():
            try:
                raw = json.loads(self.path.read_text(encoding="utf-8"))
                self.credential = str(
                    raw.get("credential", raw.get("cookie", raw.get("token", "")))
                ).strip()
            except (AttributeError, OSError, TypeError, ValueError):
                self.credential = ""

    @property
    def configured(self):
        return bool(self.credential)

    def public(self):
        return {
            "id": WHISK_ID,
            "name": "Google Flow / Whisk 旧入口（候选）",
            "web_url": WHISK_WEB_URL,
            "base_url": WHISK_BASE_URL,
            "docs_url": WHISK_WEB_URL,
            "configured": self.configured,
            "auth_type": "google_cookie_or_token",
            "credential_label": "Google Labs / Flow Cookie Header 或 Bearer Token",
            "verification_status": "browser_challenge",
            "verification_label": "旧入口已迁移 Flow · 需网页实测",
            "free_bucket": "Flow 网页每日刷新 credits（不读取付费 top-up）",
            "browser_required": True,
            "supports_credits": False,
            "free_plan_only": True,
            "free_note": (
                "Whisk 已重定向到 Google Flow。当前纯 HTTP 旧接口没有稳定的端到端验证，"
                "而 Flow 生成需要网页项目编辑器和账号会话；本适配器只保留旧入口兼容尝试，"
                "不会绕过浏览器验证，也不会改用 Gemini API 或其他付费接口。"
            ),
            "images": True,
            "videos": False,
            "text": False,
            "references": True,
            "models": list(WHISK_MODELS),
            "image_models": list(WHISK_MODELS),
            "image_default_model": WHISK_MODEL,
            "fit": "Q3 / Q2 图片替代候选；Flow 网页需要浏览器实测",
            "capabilities": "Flow/Whisk 旧接口文生图、参考图编辑；当前不保证纯 HTTP 稳定可用",
        }

    def save(self, credential: str | None):
        try:
            value = WhiskCredentialRequest(credential=credential).credential or ""
        except ValueError as exc:
            raise GatewayError("invalid_whisk_credential", str(exc), 422) from None
        if not value:
            raise GatewayError(
                "credential_required", "请填写 Google Labs Whisk Cookie 或 Token。", 422
            )
        try:
            atomic_json(self.path, {"credential": value})
        except OSError:
            raise GatewayError("storage_error", "Whisk 凭据保存失败。", 500) from None
        self.credential = value
        return self.public()

    def clear(self):
        try:
            self.path.unlink(missing_ok=True)
        except OSError:
            raise GatewayError("storage_error", "Whisk 凭据删除失败。", 500) from None
        self.credential = ""
        return self.public()


class WhiskBackend:
    """Generate Whisk images through the Labs first-party HTTP endpoints."""

    def __init__(self, credential: str, timeout: int = 450):
        if not credential:
            raise GatewayError("whisk_not_configured", "请先配置 Whisk 网页凭据。", 409)
        self.credential = credential.strip()
        self.timeout = max(20, min(int(timeout), 600))
        self.token = ""
        self.cookie = ""
        self.expires_at = 0.0

    @staticmethod
    def _looks_like_bearer(value: str) -> bool:
        value = value.strip()
        if value.lower().startswith("bearer "):
            return True
        return ";" not in value and "=" not in value and (
            value.startswith(("ya29.", "eyJ")) or value.count(".") >= 2
        )

    def _parts(self):
        value = self.credential
        if value.lower().startswith("bearer "):
            return value[7:].strip(), ""
        if self._looks_like_bearer(value):
            return value, ""
        return "", value

    @staticmethod
    def _error_code(status: int | None, detail: str = ""):
        lower = (detail or "").lower()
        if status in {401, 403} or any(
            term in lower
            for term in (
                "unauthenticated",
                "authentication",
                "access denied",
                "login",
                "session",
                "cookie",
                "登录",
                "认证",
            )
        ):
            return "whisk_authentication_failed"
        if status == 429 or re.search(
            r"quota|limit|exhaust|too many|rate.?limit|daily|次数|额度|配额|上限",
            lower,
        ):
            return "whisk_quota_exceeded"
        return "whisk_upstream_error"

    def _request(self, url: str, *, method="POST", payload=None, headers=None):
        body = (
            None
            if payload is None
            else json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode()
        )
        request = urllib.request.Request(
            url,
            data=body,
            headers={
                "Accept": "application/json",
                "User-Agent": WHISK_USER_AGENT,
                "Origin": "https://labs.google",
                "Referer": WHISK_WEB_URL,
                **(headers or {}),
            },
            method=method,
        )
        try:
            with urllib.request.urlopen(request, timeout=self.timeout) as response:
                raw = response.read(WHISK_MAX_RESPONSE_BYTES + 1)
                status = response.status
        except urllib.error.HTTPError as exc:
            try:
                detail = exc.read(WHISK_MAX_ERROR_BYTES).decode("utf-8", "replace")
            except OSError:
                detail = ""
            code = self._error_code(exc.code, detail)
            messages = {
                "whisk_authentication_failed": "Whisk 网页会话已失效，请从 Google Labs 更新 Cookie。",
                "whisk_quota_exceeded": "Whisk 网页每日免费额度或请求频率已达到上限。",
                "whisk_upstream_error": f"Whisk 网页请求失败（HTTP {exc.code}）。",
            }
            raise GatewayError(
                code,
                messages[code],
                401 if code == "whisk_authentication_failed" else 429 if code == "whisk_quota_exceeded" else 502,
            ) from None
        except (urllib.error.URLError, TimeoutError, OSError):
            raise GatewayError("whisk_network_error", "Whisk 网页暂时无法连接。", 502) from None
        if len(raw) > WHISK_MAX_RESPONSE_BYTES:
            raise GatewayError("whisk_response_too_large", "Whisk 返回内容过大。", 502)
        try:
            value = json.loads(raw)
        except (TypeError, ValueError, json.JSONDecodeError):
            raise GatewayError("whisk_invalid_response", "Whisk 返回了无效响应。", 502) from None
        if isinstance(value, dict) and value.get("error"):
            error = value.get("error")
            detail = error.get("message", "") if isinstance(error, dict) else str(error)
            code = self._error_code(status, detail)
            message = {
                "whisk_authentication_failed": "Whisk 网页会话已失效，请从 Google Labs 更新 Cookie。",
                "whisk_quota_exceeded": "Whisk 网页每日免费额度或请求频率已达到上限。",
                "whisk_upstream_error": "Whisk 网页请求未成功。",
            }[code]
            raise GatewayError(code, message, 401 if code == "whisk_authentication_failed" else 429 if code == "whisk_quota_exceeded" else 502)
        if not isinstance(value, dict):
            raise GatewayError("whisk_invalid_response", "Whisk 返回了无效响应。", 502)
        return value.get("result", {}).get("data", {}).get("json", {}).get("result", value)

    def _auth_headers(self):
        token, cookie = self._parts()
        if not token:
            if not cookie:
                raise GatewayError("whisk_authentication_failed", "Whisk 网页凭据为空。", 401)
            value = self._request(WHISK_SESSION_URL, method="GET", headers={"Cookie": cookie})
            token = str(value.get("access_token", "")).strip() if isinstance(value, dict) else ""
            if not token or (isinstance(value, dict) and value.get("error")):
                raise GatewayError(
                    "whisk_authentication_failed",
                    "Google Labs Cookie 未能换取 Whisk session，请更新网页 Cookie。",
                    401,
                )
            expires = value.get("expires") if isinstance(value, dict) else None
            try:
                # The session endpoint normally returns an ISO timestamp.  A
                # short conservative cache is enough; a fresh cookie is still
                # required if Google asks for refresh.
                self.expires_at = time.time() + min(50 * 60, max(60, float(expires) - time.time()))
            except (TypeError, ValueError):
                self.expires_at = time.time() + 45 * 60
            self.token = token
            self.cookie = cookie
        elif not self.token or time.time() >= self.expires_at:
            self.token = token
            self.cookie = cookie
            self.expires_at = time.time() + 30 * 60
        headers = {"Authorization": f"Bearer {self.token}"}
        if self.cookie:
            headers["Cookie"] = self.cookie
        return headers

    @classmethod
    def _unwrap_images(cls, value):
        if not isinstance(value, dict):
            return []
        panels = value.get("imagePanels") or []
        if not isinstance(panels, list):
            return []
        images = panels[0].get("generatedImages") if panels and isinstance(panels[0], dict) else []
        return images if isinstance(images, list) else []

    @staticmethod
    def _aspect_ratio(request):
        value = request.aspect_ratio or request.composition() or "1:1"
        return {
            "1:1": "IMAGE_ASPECT_RATIO_SQUARE",
            "16:9": "IMAGE_ASPECT_RATIO_LANDSCAPE",
            "9:16": "IMAGE_ASPECT_RATIO_PORTRAIT",
            "4:3": "IMAGE_ASPECT_RATIO_LANDSCAPE",
            "3:4": "IMAGE_ASPECT_RATIO_PORTRAIT",
        }.get(value, "IMAGE_ASPECT_RATIO_SQUARE")

    @staticmethod
    def _data_uri(path: Path) -> str:
        raw = path.read_bytes()
        if not raw or len(raw) > WHISK_MAX_REFERENCE_BYTES:
            raise GatewayError("whisk_reference_too_large", "Whisk 参考图必须非空且不超过 32 MB。", 413)
        mime = "image/png"
        suffix = path.suffix.lower()
        if suffix in {".jpg", ".jpeg"}:
            mime = "image/jpeg"
        elif suffix == ".webp":
            mime = "image/webp"
        return f"data:{mime};base64," + base64.b64encode(raw).decode("ascii")

    def _create_workflow(self):
        value = self._request(
            WHISK_CREATE_WORKFLOW_URL,
            headers={"Cookie": self.cookie, "Content-Type": "application/json"},
            payload={"json": {"workflowMetadata": {"workflowName": "Web2API Studio"}}},
        )
        workflow = value.get("workflowId") if isinstance(value, dict) else None
        if not isinstance(workflow, str) or not workflow:
            raise GatewayError("whisk_invalid_response", "Whisk 没有返回工作流 ID。", 502)
        return workflow

    def _caption(self, raw_bytes: str, category="MEDIA_CATEGORY_SUBJECT"):
        value = self._request(
            WHISK_CAPTION_URL,
            headers={"Cookie": self.cookie, "Content-Type": "application/json"},
            payload={
                "json": {
                    "captionInput": {
                        "candidatesCount": 1,
                        "mediaInput": {"rawBytes": raw_bytes, "mediaCategory": category},
                    }
                }
            },
        )
        candidates = value.get("candidates") if isinstance(value, dict) else None
        caption = candidates[0].get("output") if isinstance(candidates, list) and candidates and isinstance(candidates[0], dict) else None
        if not isinstance(caption, str) or not caption.strip():
            raise GatewayError("whisk_invalid_response", "Whisk 没有返回参考图描述。", 502)
        return caption.strip()

    def _upload_reference(self, raw_bytes: str, caption: str, category: str, workflow_id: str):
        value = self._request(
            WHISK_UPLOAD_URL,
            headers={"Cookie": self.cookie, "Content-Type": "application/json"},
            payload={
                "json": {
                    "clientContext": {"workflowId": workflow_id},
                    "uploadMediaInput": {
                        "mediaCategory": category,
                        "rawBytes": raw_bytes,
                        "caption": caption,
                    },
                }
            },
        )
        media_id = value.get("uploadMediaGenerationId") if isinstance(value, dict) else None
        if not isinstance(media_id, str) or not media_id:
            raise GatewayError("whisk_upload_failed", "Whisk 没有返回参考图 ID。", 502)
        return media_id

    def _generate(self, request, count, reference_paths):
        headers = self._auth_headers()
        if not self.cookie:
            raise GatewayError(
                "whisk_cookie_required",
                "Whisk 网页接口需要 Google Labs Cookie Header；请从已登录网页复制 Cookie。",
                422,
            )
        workflow_id = self._create_workflow()
        references = []
        for path in reference_paths:
            raw = self._data_uri(Path(path))
            caption = self._caption(raw)
            media_id = self._upload_reference(raw, caption, "MEDIA_CATEGORY_SUBJECT", workflow_id)
            references.append({"caption": caption, "mediaGenerationId": media_id})
        prompt = request.render_prompt()
        aspect = self._aspect_ratio(request)
        if references:
            recipe_inputs = [
                {
                    "caption": item["caption"],
                    "mediaInput": {
                        "mediaCategory": "MEDIA_CATEGORY_SUBJECT",
                        "mediaGenerationId": item["mediaGenerationId"],
                    },
                }
                for item in references
            ]
            value = self._request(
                WHISK_RECIPE_URL,
                headers={"Content-Type": "application/json", **headers},
                payload={
                    "clientContext": {"workflowId": workflow_id, "tool": "BACKBONE"},
                    "seed": 0,
                    "imageModelSettings": {"imageModel": "GEM_PIX", "aspectRatio": aspect},
                    "userInstruction": prompt,
                    "recipeMediaInputs": recipe_inputs,
                },
            )
        else:
            value = self._request(
                WHISK_GENERATE_URL,
                headers={"Content-Type": "application/json", **headers},
                payload={
                    "clientContext": {"workflowId": workflow_id},
                    "imageModelSettings": {"imageModel": "IMAGEN_3_5", "aspectRatio": aspect},
                    "seed": 0,
                    "prompt": prompt,
                    "mediaCategory": "MEDIA_CATEGORY_BOARD",
                },
            )
        images = self._unwrap_images(value)
        if not images:
            raise GatewayError("whisk_no_image", "Whisk 已完成请求但没有返回图片。", 502)
        return images

    async def generate(self, request, folder: Path, remaining: int, reference_paths=None, **_kwargs):
        # The public Whisk page now redirects to Flow.  Keep the old adapter
        # callable only when an account explicitly asks for it, and fail with
        # a useful state instead of treating a Flow HTML page as a JSON error.
        raise GatewayError(
            "whisk_migrated_to_flow",
            "Whisk 已迁移到 Google Flow；旧版纯 HTTP 入口暂不稳定，请直接使用 Gemini/即梦/通义/Vidu 网页额度。",
            409,
        )
        if request.model not in {"default", *WHISK_MODELS}:
            raise GatewayError("invalid_model", "Whisk 图片模型不在网页模型列表中。", 422)
        paths = list(reference_paths or [])
        if len(paths) > 3:
            raise GatewayError("reference_count", "Whisk 网页图生图当前最多使用 3 张参考图。", 422)
        count = max(1, min(int(remaining), 4))
        generated = await asyncio.to_thread(self._generate, request, count, paths)
        saved = []
        for item in generated[:remaining]:
            encoded = item.get("encodedImage") if isinstance(item, dict) else None
            if not isinstance(encoded, str) or not encoded:
                continue
            if encoded.lower().startswith("data:") and "," in encoded:
                encoded = encoded.split(",", 1)[1]
            try:
                raw = base64.b64decode(encoded, validate=True)
            except (ValueError, binascii.Error):
                continue
            if not raw or len(raw) > WHISK_MAX_IMAGE_BYTES:
                continue
            path = folder / f"whisk-{uuid.uuid4().hex}.png"
            path.write_bytes(raw)
            saved.append(path)
        if not saved:
            raise GatewayError("whisk_invalid_image", "Whisk 返回了无法保存的图片。", 502)
        return saved
