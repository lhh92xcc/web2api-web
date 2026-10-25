"""Direct Tongyi Wanxiang web-account adapter.

The Wanxiang web application exposes first-party JSON endpoints for its own
logged-in web session.  This adapter deliberately uses only that session and
the web account's ``bonus`` inspiration bucket.  ``member`` and ``topUp``
balances are reported for visibility but are never used to submit a task.

The adapter does not automate login, solve CAPTCHA/WAF challenges, or call the
Alibaba Cloud developer API.  A user copies the Cookie header from a normal
logged-in Tongyi Wanxiang tab and stores it in the local gateway.
"""

from __future__ import annotations

import asyncio
import json
import mimetypes
import re
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid
from http.cookies import SimpleCookie
from pathlib import Path
from typing import Any

from storage import GatewayError

TONGYI_BASE_URL = "https://wanx.biz.aliyun.com"
TONGYI_WEB_ORIGIN = "https://tongyi.aliyun.com"
TONGYI_MAX_RESPONSE_BYTES = 64 * 1024 * 1024
TONGYI_MAX_IMAGE_BYTES = 100 * 1024 * 1024
TONGYI_MAX_VIDEO_BYTES = 1024 * 1024 * 1024
TONGYI_POLL_INTERVAL = 3

# The model versions below are the values used by the Wanxiang web schema.
# Keep public IDs readable while translating them to the first-party enum.
TONGYI_IMAGE_MODELS = {
    "wan2.7": {"name": "Wan 2.7 Pro · 通义万相网页", "version": "2_7"},
    "wan2.7-flash": {"name": "Wan 2.7 Flash · 通义万相网页", "version": "2_7_flash"},
    "wan2.6": {"name": "Wan 2.6 · 通义万相网页", "version": "2_6"},
    "wan2.5": {"name": "Wan 2.5 · 通义万相网页", "version": "2_5"},
    "wan2.2": {"name": "Wan 2.2 · 通义万相网页", "version": "2_2"},
    "wan2.1": {"name": "Wan 2.1 · 通义万相网页", "version": "2_1"},
    "wan3.0": {"name": "Wan 3.0 · 通义万相网页", "version": "3_0"},
}
TONGYI_VIDEO_MODELS = {
    "wan3.0": {"name": "Wan 3.0 · 通义万相网页", "version": "3_0"},
    "wan2.6": {"name": "Wan 2.6 · 通义万相网页", "version": "2_6"},
    "wan2.6-flash": {"name": "Wan 2.6 Flash · 通义万相网页", "version": "2_6_flash"},
    "wan2.5": {"name": "Wan 2.5 · 通义万相网页", "version": "2_5"},
    "wan2.2": {"name": "Wan 2.2 · 通义万相网页", "version": "2_2"},
    "wan2.1": {"name": "Wan 2.1 · 通义万相网页", "version": "2_1"},
}
TONGYI_DEFAULT_IMAGE_MODEL = "wan2.7-flash"
TONGYI_DEFAULT_VIDEO_MODEL = "wan3.0"

# The web UI describes image creation as one inspiration credit per image and
# the video purchase UI as roughly five credits per short video.  Requiring
# the complete free amount before submit prevents a task from falling through
# to a paid bucket after the web service accepts it.
TONGYI_IMAGE_COST = 1
TONGYI_VIDEO_COST = 5

_COOKIE_RE = re.compile(r"^[^\r\n]+(?:=[^\r\n;]*)?(?:;\s*[^\r\n]+(?:=[^\r\n;]*)?)*$")
_SUCCESS_STATES = {"success", "succeeded", "completed", "complete", "done", "successed"}
_FAILURE_STATES = {
    "error",
    "failed",
    "failure",
    "dash_error",
    "user_delete",
    "security_reject",
    "cancelled",
    "canceled",
}
_RESULT_URL_KEYS = {
    "downloadUrl",
    "downloadUrlWithLogo",
    "videoUrl",
    "videoURL",
    "resourceUrl",
    "resourceURL",
    "webpUrl",
    "url",
}


def _number(value: Any) -> int | None:
    if value is None or isinstance(value, bool):
        return None
    try:
        return max(0, int(value))
    except (TypeError, ValueError):
        return None


def _clean_cookie(value: str) -> str:
    value = str(value or "").strip()
    if value.lower().startswith("cookie:"):
        value = value.split(":", 1)[1].strip()
    # Browser copy operations sometimes include a single header line around
    # the value.  Do not accept arbitrary multi-line headers.
    value = value.replace("\t", " ")
    if not value or "\r" in value or "\n" in value or not _COOKIE_RE.fullmatch(value):
        raise GatewayError(
            "tongyi_authentication_failed",
            "通义万相 Cookie Header 格式不正确，请从已登录网页复制完整 Cookie。",
            401,
        )
    return value


def _csrf_token(cookie: str) -> str:
    parsed = SimpleCookie()
    try:
        parsed.load(cookie)
    except Exception:  # noqa: BLE001 - malformed cookies are handled below
        parsed = SimpleCookie()
    for key in ("XSRF-TOKEN", "xsrf-token", "XSRF_TOKEN", "_csrf"):
        morsel = parsed.get(key)
        if morsel and morsel.value:
            return urllib.parse.unquote(morsel.value)
    match = re.search(r"(?:^|;)\s*XSRF-TOKEN=([^;]*)", cookie, re.IGNORECASE)
    return urllib.parse.unquote(match.group(1)) if match else ""


def _walk_dicts(value: Any):
    if isinstance(value, dict):
        yield value
        for child in value.values():
            yield from _walk_dicts(child)
    elif isinstance(value, list):
        for child in value:
            yield from _walk_dicts(child)


def _first_value(value: Any, keys: tuple[str, ...]):
    for item in _walk_dicts(value):
        for key in keys:
            if key in item and item[key] not in (None, ""):
                return item[key]
    return None


def _find_policy(value: Any) -> dict | None:
    wanted = {"host", "policy", "signature", "key"}
    for item in _walk_dicts(value):
        if wanted.issubset(item) and isinstance(item.get("host"), str):
            return item
    return None


def _is_https_url(value: Any) -> bool:
    if not isinstance(value, str) or len(value) > 8192:
        return False
    try:
        parsed = urllib.parse.urlsplit(value)
    except ValueError:
        return False
    return parsed.scheme == "https" and bool(parsed.hostname) and not parsed.username and not parsed.password


def _result_urls(value: Any) -> list[str]:
    """Extract only known media result fields from a task response."""

    found: list[str] = []

    def visit(node: Any, allow_plain_url: bool = False):
        if isinstance(node, dict):
            for key, child in node.items():
                if key in _RESULT_URL_KEYS and isinstance(child, str) and _is_https_url(child):
                    if child not in found:
                        found.append(child)
                elif key in {
                    "taskResult",
                    "wanxImageDTO",
                    "result",
                    "results",
                    "resource",
                    "resources",
                    "output",
                    "outputs",
                    "data",
                }:
                    visit(child, True)
                elif isinstance(child, (dict, list)) and allow_plain_url:
                    visit(child, True)
        elif isinstance(node, list):
            for child in node:
                visit(child, allow_plain_url)

    visit(value)
    return found


def _status(value: Any):
    # Prefer task-level status fields over arbitrary nested resource metadata.
    if isinstance(value, dict):
        for key in ("status", "taskStatus", "task_status", "state", "taskState"):
            if key in value and value[key] not in (None, ""):
                return value[key]
        for key in ("data", "task", "taskDTO", "result"):
            if key in value:
                candidate = _status(value[key])
                if candidate is not None:
                    return candidate
    elif isinstance(value, list):
        for child in value:
            candidate = _status(child)
            if candidate is not None:
                return candidate
    return None


def _state_kind(value: Any) -> str:
    if isinstance(value, str):
        normalized = value.strip().lower().replace("-", "_").replace(" ", "_")
        if normalized in _SUCCESS_STATES or "success" in normalized or "complete" in normalized:
            return "success"
        if normalized in _FAILURE_STATES or any(word in normalized for word in ("fail", "error", "reject", "delete", "cancel")):
            return "failed"
        return "processing"
    numeric = _number(value)
    if numeric is None:
        return "processing"
    # Wanxiang's current image/video task enum uses 2 for success and 98/100/
    # 102/108 for failed terminal states.  -1/0/1 are queued/processing.
    if numeric == 2:
        return "success"
    if numeric in {98, 100, 102, 108, 90, 91, 99} or numeric >= 90:
        return "failed"
    return "processing"


class TongyiWebBackend:
    """Call Wanxiang's first-party web endpoints with a saved Cookie header."""

    def __init__(self, config: dict, timeout: int = 450):
        self.config = config
        self.timeout = max(20, min(int(timeout), 600))
        self.cookie = _clean_cookie(str(config.get("credential") or config.get("api_key") or ""))
        self.csrf = _csrf_token(self.cookie)
        if not self.csrf:
            raise GatewayError(
                "tongyi_authentication_failed",
                "通义万相 Cookie 中没有 XSRF-TOKEN，请复制完整 Cookie Header。",
                401,
            )

    @staticmethod
    def _path(path: str) -> str:
        if not path.startswith("/") or "://" in path:
            raise GatewayError("tongyi_invalid_request", "通义万相接口路径无效。", 500)
        return TONGYI_BASE_URL + path

    def _request_sync(self, method: str, path: str, payload: dict | None = None) -> dict:
        body = None if payload is None else json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode()
        headers = {
            "Accept": "application/json, text/plain, */*",
            "Content-Type": "application/json",
            "Cookie": self.cookie,
            "Origin": TONGYI_WEB_ORIGIN,
            "Referer": TONGYI_WEB_ORIGIN + "/",
            "X-Platform": "web",
            "X-XSRF-TOKEN": self.csrf,
        }
        request = urllib.request.Request(self._path(path), data=body, headers=headers, method=method.upper())
        try:
            with urllib.request.urlopen(request, timeout=self.timeout) as response:
                raw = response.read(TONGYI_MAX_RESPONSE_BYTES + 1)
                status = response.status
        except urllib.error.HTTPError as exc:
            if exc.code in {401, 403}:
                raise GatewayError(
                    "tongyi_authentication_failed",
                    "通义万相网页登录信息失效或需要网页验证，请更新 Cookie。",
                    401,
                ) from None
            if exc.code == 429:
                raise GatewayError(
                    "tongyi_rate_limited",
                    "通义万相网页请求过于频繁，请稍后再试。",
                    429,
                ) from None
            raise GatewayError("tongyi_upstream_error", "通义万相网页接口请求失败。", 502) from None
        except (urllib.error.URLError, TimeoutError, OSError):
            raise GatewayError("tongyi_network_error", "通义万相网页暂时无法连接。", 502) from None
        if len(raw) > TONGYI_MAX_RESPONSE_BYTES:
            raise GatewayError("tongyi_response_too_large", "通义万相网页返回内容过大。", 502)
        try:
            result = json.loads(raw)
        except (ValueError, UnicodeDecodeError):
            raise GatewayError(
                "tongyi_invalid_response",
                "通义万相返回了无法读取的响应，可能需要重新登录或完成人机验证。",
                502,
            ) from None
        if not isinstance(result, dict):
            raise GatewayError("tongyi_invalid_response", "通义万相返回了无效响应。", 502)
        code = str(result.get("errorCode") or result.get("code") or "").lower()
        if result.get("success") is False or result.get("failed") is True or (code and code not in {"0", "200"} and result.get("httpCode", 200) >= 400):
            if code in {"4001", "4002", "401", "unauthorized", "auth"}:
                raise GatewayError(
                    "tongyi_authentication_failed",
                    "通义万相网页登录信息失效或需要网页验证，请更新 Cookie。",
                    401,
                ) from None
            if any(word in code for word in ("credit", "quota", "inspiration", "limit")):
                raise GatewayError(
                    "tongyi_free_quota_empty",
                    "通义万相网页免费灵感值不足，任务已停止；没有调用付费 API。",
                    429,
                ) from None
            raise GatewayError("tongyi_upstream_error", "通义万相网页接口拒绝了请求。", 502) from None
        if status >= 400:
            raise GatewayError("tongyi_upstream_error", "通义万相网页请求失败。", 502)
        return result

    async def _request(self, method: str, path: str, payload: dict | None = None) -> dict:
        return await asyncio.to_thread(self._request_sync, method, path, payload)

    @staticmethod
    def _data(result: dict):
        value = result.get("data")
        return value if value is not None else result

    async def credits(self) -> dict:
        result = await self._request("POST", "/wanx/api/common/imagineCount")
        data = self._data(result)
        amount = None
        if isinstance(data, dict):
            amount = data.get("amount")
        if not isinstance(amount, dict):
            for item in _walk_dicts(data):
                if isinstance(item.get("amount"), dict):
                    amount = item["amount"]
                    break
        if not isinstance(amount, dict):
            raise GatewayError(
                "tongyi_free_quota_unverified",
                "通义万相没有返回可区分的免费灵感值；为避免误扣充值额度，任务已停止。",
                502,
            )
        bonus = _number(amount.get("bonus"))
        member = _number(amount.get("member"))
        top_up = _number(amount.get("topUp", amount.get("topup")))
        total = _number(amount.get("total"))
        if bonus is None:
            raise GatewayError(
                "tongyi_free_quota_unverified",
                "通义万相没有返回可区分的免费灵感值；为避免误扣充值额度，任务已停止。",
                502,
            )
        return {
            "provider": "tongyi-web",
            "credits": {
                "bonus": bonus,
                "member": member,
                "top_up": top_up,
                "total": total,
                "free_available": bonus,
                "free_buckets_known": True,
            },
        }

    async def claim_daily_credit(self) -> dict:
        try:
            await self._request("POST", "/wanx/api/common/inspiration/dailySignReward")
        except GatewayError as exc:
            # Already-claimed responses are intentionally refreshed below.  A
            # failed auth/network request is still surfaced to the caller.
            if exc.code not in {"tongyi_upstream_error", "tongyi_free_quota_empty"}:
                raise
        return await self.credits()

    async def _require_free_credit(self, cost: int) -> dict:
        current = await self.credits()
        credits = current["credits"]
        if credits["free_available"] < cost:
            current = await self.claim_daily_credit()
            credits = current["credits"]
        if credits["free_available"] < cost:
            raise GatewayError(
                "tongyi_free_quota_empty",
                f"通义万相网页免费灵感值不足（需要至少 {cost} 点）；任务已停止，没有调用付费 API。",
                429,
            )
        return credits

    @staticmethod
    def _multipart(fields: dict[str, str], file_path: Path) -> tuple[bytes, str]:
        boundary = "----Web2API" + uuid.uuid4().hex
        chunks: list[bytes] = []
        for name, value in fields.items():
            chunks.extend(
                [
                    f"--{boundary}\r\n".encode(),
                    f'Content-Disposition: form-data; name="{name}"\r\n\r\n'.encode(),
                    str(value).encode(),
                    b"\r\n",
                ]
            )
        filename = file_path.name.replace('"', "_").replace("\r", "_").replace("\n", "_")
        mime = mimetypes.guess_type(filename)[0] or "image/png"
        chunks.extend(
            [
                f"--{boundary}\r\n".encode(),
                f'Content-Disposition: form-data; name="file"; filename="{filename}"\r\n'.encode(),
                f"Content-Type: {mime}\r\n\r\n".encode(),
                file_path.read_bytes(),
                f"\r\n--{boundary}--\r\n".encode(),
            ]
        )
        return b"".join(chunks), f"multipart/form-data; boundary={boundary}"

    async def _upload_reference(self, path: Path, task_type: str) -> dict:
        path = Path(path)
        try:
            size = path.stat().st_size
        except OSError:
            raise GatewayError("tongyi_upload_failed", "通义万相参考图无法读取。", 422) from None
        if not size or size > TONGYI_MAX_IMAGE_BYTES:
            raise GatewayError("tongyi_upload_failed", "通义万相参考图大小不符合网页限制。", 413)
        policy_result = await self._request(
            "POST",
            "/wanx/api/oss/getPolicy",
            {"fileName": path.name, "taskType": task_type},
        )
        policy = _find_policy(policy_result)
        if not policy or not _is_https_url(policy.get("host")):
            raise GatewayError("tongyi_upload_failed", "通义万相没有返回有效的参考图上传凭证。", 502)
        fields = {
            "OSSAccessKeyId": str(policy.get("accessid", policy.get("accessId", ""))),
            "policy": str(policy.get("policy", "")),
            "signature": str(policy.get("signature", "")),
            "key": str(policy.get("key", "")),
            "dir": str(policy.get("dir", "")),
            "success_action_status": "200",
        }
        if not fields["OSSAccessKeyId"] or not fields["policy"] or not fields["signature"] or not fields["key"]:
            raise GatewayError("tongyi_upload_failed", "通义万相上传凭证不完整。", 502)
        body, content_type = await asyncio.to_thread(self._multipart, fields, path)
        request = urllib.request.Request(
            str(policy["host"]),
            data=body,
            headers={
                "Content-Type": content_type,
                "X-Requested-With": "XMLHttpRequest",
            },
            method="POST",
        )
        try:
            with urllib.request.urlopen(request, timeout=self.timeout) as response:
                response.read(1024 * 1024)
                if response.status < 200 or response.status >= 300:
                    raise GatewayError("tongyi_upload_failed", "通义万相参考图上传失败。", 502)
        except GatewayError:
            raise
        except (urllib.error.HTTPError, urllib.error.URLError, TimeoutError, OSError):
            raise GatewayError("tongyi_upload_failed", "通义万相参考图上传失败。", 502) from None

        key = fields["key"]
        url = next(
            (
                value
                for value in (
                    policy.get("url"),
                    policy.get("ossUrl"),
                    policy.get("cdnUrl"),
                )
                if _is_https_url(value)
            ),
            None,
        )
        if not url:
            url_result = await self._request(
                "POST",
                "/wanx/api/oss/generateOssUrl",
                {"key": key, "taskType": task_type},
            )
            url = next(iter(_result_urls(url_result)), None)
            if not url:
                raw_url = _first_value(url_result, ("ossUrl", "cdnUrl", "url"))
                if _is_https_url(raw_url):
                    url = raw_url
        if not url:
            raise GatewayError("tongyi_upload_failed", "通义万相没有返回参考图访问地址。", 502)
        resource_id = _first_value(policy, ("resourceId", "resource_id", "id")) or key
        return {"key": key, "url": url, "resource_id": str(resource_id)}

    @staticmethod
    def _image_resolution(request) -> str:
        size = str(getattr(request, "size", "auto") or "auto").upper()
        if size == "4K":
            return "2K"
        if size in {"1K", "2K"}:
            return size
        if "X" in size:
            try:
                width, height = (int(value) for value in size.split("X", 1))
                return "1K" if width * height <= 1_500_000 else "2K"
            except (TypeError, ValueError):
                pass
        return "2K"

    @staticmethod
    def _ratio(request) -> str:
        return str(getattr(request, "composition", lambda: None)() or "1:1")

    @staticmethod
    def _model_version(model: str, mapping: dict) -> str:
        selected = model if model != "default" else next(iter(mapping))
        item = mapping.get(selected)
        if not item:
            raise GatewayError("tongyi_invalid_model", "所选模型不在通义万相网页模型列表中。", 422)
        return item["version"]

    def _image_payload(self, request, uploaded: list[dict]) -> dict:
        model_version = self._model_version(request.model, TONGYI_IMAGE_MODELS)
        prompt = request.render_prompt(has_references=bool(uploaded))
        task_input: dict[str, Any] = {
            "subType": "basic",
            "modelVersion": model_version,
            "generationMode": "plain",
            "modelIds": [],
            "prompt": prompt,
            "selectedResolution": self._image_resolution(request),
            "ratio": self._ratio(request),
        }
        task_type = "text_to_image"
        if uploaded:
            task_type = "image_to_image"
            keys = [item["key"] for item in uploaded]
            elements = {
                key: {
                    "type": "image",
                    "payload": {
                        "key": key,
                        "url": item["url"],
                        "resourceId": item["resource_id"],
                    },
                }
                for key, item in zip(keys, uploaded)
            }
            task_input.update(
                {
                    "imageResolution": self._image_resolution(request),
                    "promptMeta": {
                        "originPrompt": request.prompt,
                        "orderedKeys": keys,
                    },
                    "reference": {"type": "ref-element", "refElementIds": keys},
                    "elements": elements,
                }
            )
        return {"deductMode": "credit_mode", "taskType": task_type, "taskInput": task_input}

    async def _submit_and_poll(self, payload: dict, kind: str) -> list[str]:
        response = await self._request("POST", "/wanx/api/common/imageGen", payload)
        task_id = _first_value(response, ("taskId", "task_id"))
        if not task_id or not isinstance(task_id, (str, int)):
            raise GatewayError("tongyi_invalid_response", "通义万相没有返回任务 ID。", 502)
        deadline = time.monotonic() + self.timeout
        while time.monotonic() < deadline:
            detail = await self._request(
                "POST", "/wanx/api/common/v2/taskResult", {"taskId": str(task_id)}
            )
            urls = _result_urls(detail)
            state = _state_kind(_status(detail))
            if state == "success" and urls:
                return urls
            if state == "failed":
                raise GatewayError("tongyi_generation_failed", "通义万相网页生成任务失败。", 502)
            await asyncio.sleep(min(TONGYI_POLL_INTERVAL, max(0.05, deadline - time.monotonic())))
        raise GatewayError("tongyi_timeout", "等待通义万相网页任务超时，请到网页任务记录查看状态。", 504)

    @staticmethod
    def _download_sync(url: str, path: Path, kind: str):
        if not _is_https_url(url):
            raise GatewayError("tongyi_download_failed", "通义万相返回了不安全的下载地址。", 502)
        maximum = TONGYI_MAX_VIDEO_BYTES if kind == "video" else TONGYI_MAX_IMAGE_BYTES
        request = urllib.request.Request(url, headers={"Accept": "video/*" if kind == "video" else "image/*"})
        try:
            with urllib.request.urlopen(request, timeout=60) as response:
                data = response.read(maximum + 1)
        except (urllib.error.HTTPError, urllib.error.URLError, TimeoutError, OSError):
            raise GatewayError("tongyi_download_failed", "通义万相结果下载失败。", 502) from None
        if len(data) > maximum:
            raise GatewayError("tongyi_response_too_large", "通义万相结果文件过大。", 502)
        if not data:
            raise GatewayError("tongyi_download_failed", "通义万相返回了空文件。", 502)
        if kind == "video" and not (
            (len(data) >= 12 and data[4:8] == b"ftyp")
            or data.startswith(b"\x1a\x45\xdf\xa3")
        ):
            raise GatewayError("tongyi_download_failed", "通义万相返回的文件不是可播放视频。", 502)
        path.write_bytes(data)

    async def generate_image(self, request, folder: Path, reference_paths):
        if request.model not in {"default", *TONGYI_IMAGE_MODELS}:
            raise GatewayError("tongyi_invalid_model", "所选图片模型不在通义万相网页模型列表中。", 422)
        await self._require_free_credit(TONGYI_IMAGE_COST)
        uploaded = [
            await self._upload_reference(Path(path), "image_to_image")
            for path in reference_paths[:9]
        ]
        urls = await self._submit_and_poll(self._image_payload(request, uploaded), "image")
        result = []
        for url in urls[:4]:
            path = Path(folder) / f"tongyi-{uuid.uuid4().hex}.png"
            await asyncio.to_thread(self._download_sync, url, path, "image")
            result.append(path)
        if not result:
            raise GatewayError("tongyi_invalid_response", "通义万相没有返回可用图片。", 502)
        return result

    def _video_payload(self, request, uploaded: list[dict]) -> dict:
        model_version = self._model_version(request.model, TONGYI_VIDEO_MODELS)
        task_input: dict[str, Any] = {
            "modelVersion": model_version,
            "smartDuration": False,
            "thinkMode": False,
            "audio": bool(request.audio),
            "generationMode": "plain",
            "prompt": request.prompt,
            "selectedResolution": request.resolution.upper(),
            "ratio": request.aspect_ratio,
            "subType": "basic",
            "duration": request.duration,
        }
        task_type = "text_to_video"
        if uploaded:
            task_type = "image_to_video"
            task_input["baseImage"] = uploaded[0]["url"]
            task_input["baseImageResourceId"] = uploaded[0]["resource_id"]
            if len(uploaded) > 1:
                task_input.update(
                    {
                        "subType": "headtail",
                        "tailImage": uploaded[1]["url"],
                        "tailImageResourceId": uploaded[1]["resource_id"],
                    }
                )
        return {"deductMode": "credit_mode", "taskType": task_type, "taskInput": task_input}

    async def generate(self, request, folder: Path, reference_paths):
        if request.model not in {"default", *TONGYI_VIDEO_MODELS}:
            raise GatewayError("tongyi_invalid_model", "所选视频模型不在通义万相网页模型列表中。", 422)
        if len(reference_paths) > 2:
            raise GatewayError("tongyi_reference_count", "通义万相网页视频当前支持最多 2 张首尾帧参考图。", 422)
        await self._require_free_credit(TONGYI_VIDEO_COST)
        uploaded = [
            await self._upload_reference(Path(path), "image_to_video")
            for path in reference_paths
        ]
        urls = await self._submit_and_poll(self._video_payload(request, uploaded), "video")
        url = next((item for item in urls if _is_https_url(item)), None)
        if not url:
            raise GatewayError("tongyi_invalid_response", "通义万相没有返回可用视频。", 502)
        path = Path(folder) / f"tongyi-{uuid.uuid4().hex}.mp4"
        await asyncio.to_thread(self._download_sync, url, path, "video")
        return path

