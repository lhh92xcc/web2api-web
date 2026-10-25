"""Leonardo web-account connector.

Leonardo's consumer site uses the same GraphQL endpoint as its web client.
This adapter accepts a token copied from the user's already authenticated
browser session, checks that the account is on the Free plan, and spends only
the account's ``subscriptionTokens``.  Paid tokens and API credits are never
used as a fallback.

The endpoint is a private web surface and may change without notice.  The
connector deliberately does not create accounts, solve challenges, or bypass
WAF/device checks.  If the first-party request is rejected, the caller gets a
clear error and can finish the normal verification in the Leonardo website.
"""

import asyncio
import base64
import binascii
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

LEONARDO_ID = "leonardo-web"
LEONARDO_WEB_URL = "https://app.leonardo.ai/image-generation"
LEONARDO_GRAPHQL_URL = "https://api.leonardo.ai/v1/graphql"
LEONARDO_DEFAULT_MODEL = "leonardo-diffusion-xl"
LEONARDO_MODELS = [LEONARDO_DEFAULT_MODEL]
LEONARDO_POLL_INTERVAL = 2
LEONARDO_MAX_RESPONSE_BYTES = 32 * 1024 * 1024
LEONARDO_MAX_IMAGE_BYTES = 100 * 1024 * 1024
LEONARDO_USER_AGENT = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"
)

_USER_DETAILS_QUERY = """
query GetUserDetails($userSub: String) {
  users(where: {user_details: {cognitoId: {_eq: $userSub}}}) {
    id
    username
    user_details {
      auth0Email
      plan
      paidTokens
      apiCredit
      subscriptionTokens
      subscriptionModelTokens
      subscriptionGptTokens
      tokenRenewalDate
      planSubscribeFrequency
      __typename
    }
    __typename
  }
}
"""

_GENERATION_MUTATION = """
mutation CreateSDGenerationJob($arg1: SDGenerationInput!) {
  sdGenerationJob(arg1: $arg1) {
    generationId
    __typename
  }
}
"""

_GENERATION_FEED_QUERY = """
query GetAIGenerationFeed(
  $where: generations_bool_exp = {},
  $limit: Int,
  $offset: Int = 0
) {
  generations(
    limit: $limit
    offset: $offset
    order_by: [{createdAt: desc}]
    where: $where
  ) {
    id
    status
    generated_images(order_by: [{url: desc}]) {
      id
      url
      __typename
    }
    __typename
  }
}
"""


class LeonardoCredentialRequest(BaseModel):
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
            raise ValueError("Leonardo Bearer Token 不能包含控制字符。")
        return value


class LeonardoConfigStore:
    """Persist the user's Leonardo token without exposing it in public data."""

    def __init__(self, state_dir: Path):
        self.path = state_dir / "leonardo.json"
        self.credential = ""
        if self.path.exists():
            try:
                value = json.loads(self.path.read_text(encoding="utf-8"))
                self.credential = str(
                    value.get("credential", value.get("token", ""))
                ).strip()
            except (AttributeError, OSError, TypeError, ValueError):
                self.credential = ""

    @property
    def configured(self):
        return bool(self.credential)

    def public(self):
        return {
            "id": LEONARDO_ID,
            "name": "Leonardo 网页 Free plan",
            "web_url": LEONARDO_WEB_URL,
            "base_url": LEONARDO_GRAPHQL_URL,
            "docs_url": LEONARDO_WEB_URL,
            "configured": self.configured,
            "auth_type": "bearer_token",
            "credential_label": "Leonardo 网页 Bearer Token",
            "free_plan_only": True,
            "verification_status": "web_candidate",
            "verification_label": "网页候选 · 需账号实测",
            "free_bucket": "Free plan subscriptionTokens",
            "browser_required": False,
            "free_note": (
                "每次生成前检查网页账号为 Free plan，并只使用 subscriptionTokens；"
                "购买 Token 和官方 API credits 不会被使用。"
            ),
            "images": True,
            "videos": False,
            "text": False,
            "references": False,
            "models": list(LEONARDO_MODELS),
            "image_models": list(LEONARDO_MODELS),
            "image_default_model": LEONARDO_DEFAULT_MODEL,
            "fit": "Q3 / Q2 图片替代；Leonardo 网页 Free plan",
            "capabilities": "Leonardo 网页文生图；不需要常驻浏览器",
        }

    def save(self, credential: str | None):
        try:
            value = LeonardoCredentialRequest(credential=credential).credential or ""
        except ValueError as exc:
            raise GatewayError("invalid_leonardo_credential", str(exc), 422) from None
        if not value:
            raise GatewayError(
                "credential_required", "请填写 Leonardo 网页 Bearer Token。", 422
            )
        try:
            atomic_json(self.path, {"credential": value})
        except OSError:
            raise GatewayError("storage_error", "Leonardo 凭据保存失败。", 500) from None
        self.credential = value
        return self.public()

    def clear(self):
        try:
            self.path.unlink(missing_ok=True)
        except OSError:
            raise GatewayError("storage_error", "Leonardo 凭据删除失败。", 500) from None
        self.credential = ""
        return self.public()


def _jwt_subject(token: str) -> str:
    """Read only the JWT payload; signature verification remains Leonardo's job."""
    try:
        parts = token.split(".")
        if len(parts) != 3:
            raise ValueError
        payload = parts[1] + "=" * (-len(parts[1]) % 4)
        decoded = base64.urlsafe_b64decode(payload.encode("ascii"))
        subject = json.loads(decoded).get("sub")
    except (ValueError, TypeError, KeyError, UnicodeDecodeError, binascii.Error, json.JSONDecodeError):
        subject = None
    if not isinstance(subject, str) or not subject.strip():
        raise GatewayError(
            "leonardo_token_invalid",
            "Leonardo Bearer Token 不是可用的网页 JWT，请从已登录网页更新。",
            401,
        )
    return subject.strip()


def _number(value):
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        return float(value)
    if isinstance(value, str):
        try:
            return float(value.strip())
        except ValueError:
            return None
    if isinstance(value, dict):
        for key in ("remaining", "balance", "amount", "tokens", "value"):
            parsed = _number(value.get(key))
            if parsed is not None:
                return parsed
    return None


def _is_free_plan(value) -> bool:
    if isinstance(value, str):
        normalized = value.strip().lower().replace("_", " ").replace("-", " ")
        return normalized in {"free", "free plan", "free tier"}
    if isinstance(value, dict):
        if value.get("is_free") is True or value.get("isFree") is True:
            return True
        candidates = [
            value.get("name"),
            value.get("slug"),
            value.get("id"),
            value.get("code"),
            value.get("type"),
        ]
        return any(_is_free_plan(candidate) for candidate in candidates)
    return False


class LeonardoBackend:
    """Generate through the Leonardo web GraphQL endpoint."""

    def __init__(self, credential: str, timeout: int = 450):
        if not credential:
            raise GatewayError("leonardo_not_configured", "请先配置 Leonardo 网页 Token。", 409)
        self.credential = credential.strip()
        if self.credential.lower().startswith("bearer "):
            self.credential = self.credential[7:].strip()
        self.timeout = max(10, timeout)
        self.subject = _jwt_subject(self.credential)
        self.user_id = ""

    def _headers(self, accept="application/json"):
        return {
            "Accept": accept,
            "Authorization": f"Bearer {self.credential}",
            "Content-Type": "application/json",
            "User-Agent": LEONARDO_USER_AGENT,
            "Origin": "https://app.leonardo.ai",
            "Referer": LEONARDO_WEB_URL,
        }

    @staticmethod
    def _error_code(status: int | None, detail: str = ""):
        lower = (detail or "").lower()
        if status in {401, 403} or any(
            term in lower for term in ("unauthorized", "unauthenticated", "authentication", "login", "jwt")
        ):
            return "leonardo_authentication_failed"
        if status == 429 or re.search(r"quota|token|credit|limit|rate.?limit|额度|配额|上限", lower):
            return "leonardo_quota_exceeded"
        return "leonardo_upstream_error"

    def _request(self, payload):
        body = json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
        request = urllib.request.Request(
            LEONARDO_GRAPHQL_URL,
            data=body,
            headers=self._headers(),
            method="POST",
        )
        try:
            with urllib.request.urlopen(request, timeout=self.timeout) as response:
                raw = response.read(LEONARDO_MAX_RESPONSE_BYTES + 1)
                status = response.status
        except urllib.error.HTTPError as exc:
            try:
                detail = exc.read(64 * 1024).decode("utf-8", "replace")
            except OSError:
                detail = ""
            code = self._error_code(exc.code, detail)
            message = {
                "leonardo_authentication_failed": "Leonardo 网页 Token 已失效，请从已登录网页更新。",
                "leonardo_quota_exceeded": "Leonardo 网页 Free plan 的 subscriptionTokens 不足。",
                "leonardo_upstream_error": f"Leonardo 网页请求失败（HTTP {exc.code}）。",
            }[code]
            raise GatewayError(code, message, 401 if code == "leonardo_authentication_failed" else 429 if code == "leonardo_quota_exceeded" else 502) from None
        except (urllib.error.URLError, TimeoutError, OSError):
            raise GatewayError("leonardo_network_error", "Leonardo 网页暂时无法连接。", 502) from None
        if len(raw) > LEONARDO_MAX_RESPONSE_BYTES:
            raise GatewayError("leonardo_response_too_large", "Leonardo 返回内容过大。", 502)
        try:
            value = json.loads(raw)
        except (TypeError, ValueError, json.JSONDecodeError):
            raise GatewayError("leonardo_invalid_response", "Leonardo 返回了无效响应。", 502) from None
        if not isinstance(value, dict):
            raise GatewayError("leonardo_invalid_response", "Leonardo 返回了无效响应。", 502)
        errors = value.get("errors")
        if errors:
            detail = " ".join(
                str(item.get("message", "")) for item in errors if isinstance(item, dict)
            )
            code = self._error_code(status, detail)
            message = {
                "leonardo_authentication_failed": "Leonardo 网页 Token 已失效，请从已登录网页更新。",
                "leonardo_quota_exceeded": "Leonardo 网页 Free plan 的 subscriptionTokens 不足。",
                "leonardo_upstream_error": "Leonardo 网页请求未成功。",
            }[code]
            raise GatewayError(code, message, 401 if code == "leonardo_authentication_failed" else 429 if code == "leonardo_quota_exceeded" else 502)
        return value.get("data") or {}

    def _details(self):
        data = self._request(
            {
                "operationName": "GetUserDetails",
                "variables": {"userSub": self.subject},
                "query": _USER_DETAILS_QUERY,
            }
        )
        users = data.get("users")
        if not isinstance(users, list) or not users or not isinstance(users[0], dict):
            raise GatewayError("leonardo_user_not_found", "Leonardo 没有返回当前网页账号信息。", 401)
        user = users[0]
        details = user.get("user_details")
        if isinstance(details, list):
            details = details[0] if details else {}
        if not isinstance(details, dict):
            details = {}
        self.user_id = str(user.get("id", "")).strip()
        if not self.user_id:
            raise GatewayError("leonardo_invalid_response", "Leonardo 没有返回账号 ID。", 502)
        return user, details

    def _free_tokens(self, required: int):
        _, details = self._details()
        if not _is_free_plan(details.get("plan")):
            raise GatewayError(
                "leonardo_free_plan_required",
                "Leonardo 账号不是已确认的 Free plan；为避免误用付费额度，已拒绝提交。",
                403,
            )
        tokens = _number(details.get("subscriptionTokens"))
        if tokens is None:
            raise GatewayError(
                "leonardo_quota_unknown",
                "Leonardo 未返回可确认的 subscriptionTokens，已拒绝提交。",
                409,
            )
        if tokens < required:
            raise GatewayError(
                "leonardo_quota_exceeded",
                f"Leonardo Free plan subscriptionTokens 不足（需要 {required}，可用 {tokens:g}）。",
                429,
            )
        return tokens

    @staticmethod
    def _dimensions(request):
        size = request.size
        if size == "1K" or size == "auto":
            width = height = 1024
        elif size == "2K":
            width = height = 1536
        elif size == "4K":
            # The web model's common maximum is 1536; keep the request valid.
            width = height = 1536
        elif "x" in size:
            width, height = (int(part) for part in size.split("x", 1))
        else:
            width = height = 1024
        ratio = request.composition()
        if ratio and size in {"auto", "1K", "2K", "4K"}:
            left, right = (int(part) for part in ratio.split(":", 1))
            if left >= right:
                width, height = width, max(64, round(width * right / left / 64) * 64)
            else:
                height, width = height, max(64, round(height * left / right / 64) * 64)
        return max(64, int(width)), max(64, int(height))

    def _start(self, request, count, reference_paths):
        if reference_paths:
            raise GatewayError(
                "leonardo_references_unsupported",
                "Leonardo 网页连接当前只支持文生图；参考图请使用 Gemini、即梦或其他支持图生图的平台。",
                422,
            )
        self._free_tokens(count)
        model = request.model if request.model != "default" else LEONARDO_DEFAULT_MODEL
        if model not in LEONARDO_MODELS:
            raise GatewayError("invalid_model", "Leonardo 图片模型不在网页模型列表中。", 422)
        width, height = self._dimensions(request)
        payload = {
            "prompt": request.render_prompt(),
            "negative_prompt": request.negative_prompt or "",
            "num_images": count,
            "width": width,
            "height": height,
            "nsfw": True,
            "num_inference_steps": 10,
            "guidance_scale": 7,
            "init_strength": 0.4,
            "sd_version": "v2",
            "modelId": "b820ea11-02bf-4652-97ae-9ac0cc00593d",
            "presetStyle": "NONE",
            "scheduler": "LEONARDO",
            "public": True,
            "tiling": False,
            "leonardoMagic": True,
            "imagePrompts": [],
            "imagePromptWeight": 0.45,
            "poseToImage": False,
            "poseToImageType": "POSE",
            "weighting": 1,
            "highContrast": True,
        }
        data = self._request(
            {
                "operationName": "CreateSDGenerationJob",
                "variables": {"arg1": payload},
                "query": _GENERATION_MUTATION,
            }
        )
        generation = data.get("sdGenerationJob")
        generation_id = generation.get("generationId") if isinstance(generation, dict) else None
        if not isinstance(generation_id, str) or not generation_id:
            raise GatewayError("leonardo_invalid_response", "Leonardo 没有返回生成任务 ID。", 502)
        return generation_id

    def _poll(self, generation_id):
        deadline = time.monotonic() + self.timeout
        while time.monotonic() < deadline:
            data = self._request(
                {
                    "operationName": "GetAIGenerationFeed",
                    "variables": {
                        "where": {"id": {"_eq": generation_id}},
                        "limit": 10,
                        "offset": 0,
                    },
                    "query": _GENERATION_FEED_QUERY,
                }
            )
            generations = data.get("generations")
            generation = next(
                (item for item in generations or [] if isinstance(item, dict) and item.get("id") == generation_id),
                None,
            )
            if generation is None:
                time.sleep(LEONARDO_POLL_INTERVAL)
                continue
            status = str(generation.get("status", "")).upper()
            if status in {"FAILED", "ERROR", "CANCELLED", "CANCELED"}:
                raise GatewayError("leonardo_generation_failed", "Leonardo 图片生成失败。", 502)
            images = generation.get("generated_images")
            if isinstance(images, list) and images:
                return images
            if status in {"COMPLETE", "COMPLETED", "SUCCESS", "SUCCEEDED", "DONE"}:
                raise GatewayError("leonardo_no_image", "Leonardo 已完成请求但没有返回图片。", 502)
            time.sleep(LEONARDO_POLL_INTERVAL)
        raise GatewayError("leonardo_timeout", "等待 Leonardo 图片生成超时，请到网页任务记录查看。", 504)

    @staticmethod
    def _media_url(value):
        if not isinstance(value, str) or not value:
            return None
        parsed = urllib.parse.urlsplit(value)
        host = (parsed.hostname or "").lower().rstrip(".")
        allowed = host == "leonardo.ai" or host.endswith((".leonardo.ai", ".amazonaws.com", ".cloudfront.net"))
        if parsed.scheme != "https" or not host or not allowed or parsed.username or parsed.password:
            return None
        return value

    def _download(self, image, folder: Path):
        url = self._media_url(image.get("url") if isinstance(image, dict) else None)
        if not url:
            raise GatewayError("leonardo_invalid_response", "Leonardo 返回了不安全或无效的图片地址。", 502)
        request = urllib.request.Request(
            url,
            headers={
                "Accept": "image/*",
                "User-Agent": LEONARDO_USER_AGENT,
                "Referer": LEONARDO_WEB_URL,
            },
        )
        try:
            with urllib.request.urlopen(request, timeout=self.timeout) as response:
                raw = response.read(LEONARDO_MAX_IMAGE_BYTES + 1)
                content_type = response.headers.get("Content-Type", "")
        except urllib.error.HTTPError:
            raise GatewayError("leonardo_download_failed", "Leonardo 图片下载失败。", 502) from None
        except (urllib.error.URLError, TimeoutError, OSError):
            raise GatewayError("leonardo_network_error", "Leonardo 图片暂时无法下载。", 502) from None
        if len(raw) > LEONARDO_MAX_IMAGE_BYTES or not raw:
            raise GatewayError("leonardo_download_failed", "Leonardo 图片为空或超过本地保存上限。", 502)
        extension = ".png"
        lowered = content_type.lower()
        if "jpeg" in lowered or "jpg" in lowered:
            extension = ".jpg"
        elif "webp" in lowered:
            extension = ".webp"
        path = folder / f"leonardo-{uuid.uuid4().hex}{extension}"
        path.write_bytes(raw)
        return path

    async def generate(self, request, folder: Path, remaining: int, reference_paths=None, **_kwargs):
        count = max(1, min(int(remaining), 4))
        generation_id = await asyncio.to_thread(self._start, request, count, reference_paths or [])
        images = await asyncio.to_thread(self._poll, generation_id)
        saved = []
        for image in images[:remaining]:
            saved.append(await asyncio.to_thread(self._download, image, folder))
        if not saved:
            raise GatewayError("leonardo_no_image", "Leonardo 没有返回可保存的图片。", 502)
        return saved
