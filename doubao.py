"""Small, opt-in adapter for the logged-in Doubao web endpoints.

Doubao's web application uses first-party request parameters in addition to its
cookies.  This adapter accepts those values from the user and sends the same
web request shape.  It deliberately does *not* derive a device id, generate
``X-Bogus``/WebMSSDK signatures, solve CAPTCHA challenges, or automate a
browser.  A session that needs one of those checks returns a clear error and
the caller can finish the check in the normal Doubao page.

The credential is a JSON string stored locally by :class:`VideoConfigStore`::

    {"cookie":"...", "device_id":"...", "web_id":"...",
     "fp":"...", "ms_token":"..."}

``fp``, ``ms_token`` and ``tea_uuid`` may also be present as cookies.  The
device and web ids must be copied from the user's own web request or local
session; they are never generated here.
"""

from __future__ import annotations

import asyncio
import base64
import json
import mimetypes
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid
from http.cookies import SimpleCookie
from pathlib import Path
from typing import Any

from storage import GatewayError

DOUBAO_ID = "doubao-web"
DOUBAO_BASE_URL = "https://www.doubao.com"
DOUBAO_WEB_URL = "https://www.doubao.com/chat/"
DOUBAO_IMAGE_MODELS = {
    "doubao-image": "豆包网页图片",
    "seedream-web": "Seedream 网页图片",
}
DOUBAO_VIDEO_MODELS = {
    "seedance2.0-web": "Seedance 2.0 网页",
    "seedance2.0-t2v": "Seedance 2.0 文生视频",
    "seedance2.0-i2v": "Seedance 2.0 图生视频",
}
DOUBAO_TEXT_MODELS = {
    "doubao-web": "豆包网页对话",
    "doubao-pro": "豆包 Pro",
    "doubao-lite": "豆包 Lite",
}
DOUBAO_TEXT_MODEL_KEYS = {
    "doubao-web": "0",
    "doubao-pro": "5",
    "doubao-lite": "seed-lite-7b",
}
DOUBAO_DEFAULT_BOT_ID = "7338286299411103781"
DOUBAO_DEFAULT_IMAGE_MODEL = "doubao-image"
DOUBAO_DEFAULT_VIDEO_MODEL = "seedance2.0-web"
DOUBAO_DEFAULT_TEXT_MODEL = "doubao-web"
DOUBAO_MAX_IMAGE_BYTES = 100 * 1024 * 1024
DOUBAO_MAX_VIDEO_BYTES = 1024 * 1024 * 1024
DOUBAO_POLL_INTERVAL = 3


def _json_object(value: Any, label: str) -> dict[str, Any]:
    if isinstance(value, dict):
        return value
    try:
        parsed = json.loads(str(value or ""))
    except (TypeError, ValueError):
        raise GatewayError(
            "doubao_credential_invalid",
            f"豆包凭据应是包含 cookie、device_id 和 web_id 的 JSON（{label}）。",
            422,
        ) from None
    if not isinstance(parsed, dict):
        raise GatewayError(
            "doubao_credential_invalid",
            "豆包凭据 JSON 格式不正确。",
            422,
        )
    return parsed


def _cookie_map(cookie: str) -> dict[str, str]:
    value = str(cookie or "").strip()
    if value.lower().startswith("cookie:"):
        value = value.split(":", 1)[1].strip()
    if not value or "\r" in value or "\n" in value:
        raise GatewayError(
            "doubao_credential_invalid",
            "豆包 Cookie Header 不能为空且不能包含换行。",
            422,
        )
    parsed = SimpleCookie()
    try:
        parsed.load(value)
    except Exception:  # noqa: BLE001 - malformed user input is handled below
        parsed = SimpleCookie()
    result = {key: morsel.value for key, morsel in parsed.items()}
    if not result:
        raise GatewayError(
            "doubao_credential_invalid",
            "豆包 Cookie Header 格式不正确，请从已登录网页请求复制。",
            422,
        )
    return result


def parse_credential(value: str | dict[str, Any]) -> dict[str, Any]:
    """Validate the user-supplied web session without manufacturing context."""

    raw = _json_object(value, "顶层")
    cookie = str(raw.get("cookie", "")).strip()
    cookies = _cookie_map(cookie)

    def pick(*names: str) -> str:
        for name in names:
            candidate = str(raw.get(name, "") or cookies.get(name, "")).strip()
            if candidate:
                return candidate
        return ""

    device_id = pick("device_id", "deviceId")
    web_id = pick("web_id", "webId")
    fp = pick("fp", "s_v_web_id")
    ms_token = pick("ms_token", "msToken")
    tea_uuid = pick("tea_uuid", "teaUuid") or web_id
    # These values are required by the first-party query shape.  Requiring them
    # is intentional: deriving a stable id from a cookie would be device
    # fingerprint fabrication and would make the adapter a risk-control bypass.
    if not device_id or not web_id:
        raise GatewayError(
            "doubao_context_required",
            "豆包网页还需要 device_id 和 web_id。请从已登录网页的请求参数或会话存储复制后，与 Cookie 一起填写。",
            422,
        )
    if not tea_uuid:
        raise GatewayError(
            "doubao_context_required",
            "豆包网页还需要 tea_uuid；通常可填写网页请求中的 tea_uuid。",
            422,
        )
    if len(cookie) > 64 * 1024:
        raise GatewayError("doubao_credential_invalid", "豆包 Cookie 过长。", 422)
    return {
        "cookie": cookie,
        "cookies": cookies,
        "device_id": device_id,
        "web_id": web_id,
        "tea_uuid": tea_uuid,
        "fp": fp,
        "ms_token": ms_token,
        "aid": str(raw.get("aid", "582478") or "582478"),
        "pc_version": str(raw.get("pc_version", "2.1.7") or "2.1.7"),
        "chromium_version": str(raw.get("chromium_version", "") or ""),
    }


def _json_load(value: Any) -> Any:
    if isinstance(value, (dict, list)):
        return value
    try:
        return json.loads(value)
    except (TypeError, ValueError):
        return value


def _event_error(code: Any, message: str = "") -> GatewayError:
    text = str(message or "")
    try:
        numeric = int(code)
    except (TypeError, ValueError):
        numeric = 0
    if numeric in {710012000, 710012001} or "登录" in text or "login" in text.lower():
        return GatewayError(
            "doubao_authentication_failed",
            "豆包网页登录已失效，请重新复制本机网页 Cookie 和上下文。",
            401,
        )
    if numeric in {710022002, 710022004} or any(
        word in text.lower() for word in ("captcha", "verify", "风控", "验证", "安全校验")
    ):
        return GatewayError(
            "doubao_verification_required",
            "豆包网页要求在官方页面完成验证；本地 Web2API 不会绕过验证码或设备风控。",
            429,
        )
    if any(word in text.lower() for word in ("quota", "limit", "额度", "上限", "credit")):
        return GatewayError(
            "doubao_free_quota_empty",
            "豆包网页账号当前没有可用的网页额度。",
            429,
        )
    return GatewayError(
        "doubao_upstream_error",
        "豆包网页请求失败，请先在官方网页确认账号和模型可用。",
        502,
    )


def _sse_events(raw: bytes) -> list[tuple[str, Any]]:
    text = raw.decode("utf-8", errors="replace")
    result: list[tuple[str, Any]] = []
    for block in text.replace("\r\n", "\n").split("\n\n"):
        if not block.strip():
            continue
        event_name = "message"
        data_lines: list[str] = []
        for line in block.splitlines():
            if line.startswith("event:"):
                event_name = line.split(":", 1)[1].strip()
            elif line.startswith("data:"):
                data_lines.append(line.split(":", 1)[1].strip())
        if not data_lines:
            continue
        data = _json_load("\n".join(data_lines))
        if event_name == "gateway-error":
            obj = data if isinstance(data, dict) else {}
            raise _event_error(obj.get("code"), obj.get("message") or obj.get("msg"))
        result.append((event_name, data))
    return result


def _message_from_event(data: Any) -> dict[str, Any] | None:
    if not isinstance(data, dict) or data.get("event_type") != 2001:
        return None
    event_data = _json_load(data.get("event_data", {}))
    if not isinstance(event_data, dict):
        return None
    message = _json_load(event_data.get("message", {}))
    return message if isinstance(message, dict) else None


def _find_urls(value: Any, keys: set[str]) -> list[str]:
    found: list[str] = []
    if isinstance(value, dict):
        for key, item in value.items():
            if key in keys and isinstance(item, str) and item.startswith("https://"):
                found.append(item)
            found.extend(_find_urls(item, keys))
    elif isinstance(value, list):
        for item in value:
            found.extend(_find_urls(item, keys))
    return found


class DoubaoWebBackend:
    """Cookie-backed Doubao web adapter with no browser or anti-bot bypass."""

    def __init__(self, config: dict, timeout: int):
        self.config = config
        self.timeout = timeout
        self.context = parse_credential(config.get("credential", config.get("api_key", "")))

    def _params(self, *, omit_fp: bool = False) -> dict[str, str]:
        context = self.context
        params = {
            "aid": context["aid"],
            "real_aid": context["aid"],
            "device_id": context["device_id"],
            "tea_uuid": context["tea_uuid"],
            "web_id": context["web_id"],
            "device_platform": "web",
            "language": "zh",
            "region": "CN",
            "sys_region": "CN",
            "pkg_type": "release_version",
            "version_code": "20800",
            "pc_version": context["pc_version"],
            "client_platform": "pc_client",
            "runtime": "web",
            "runtime_version": "3.5.4",
            "samantha_web": "1",
            "use-olympus-account": "1",
            "web_tab_id": str(uuid.uuid4()),
        }
        if context.get("chromium_version"):
            params["chromium_version"] = context["chromium_version"]
        if context.get("fp") and not omit_fp:
            params["fp"] = context["fp"]
        if context.get("ms_token"):
            params["msToken"] = context["ms_token"]
        return params

    def _headers(self, accept: str = "text/event-stream") -> dict[str, str]:
        cookies = self.context["cookies"]
        headers = {
            "Accept": accept,
            "Accept-Language": "zh-CN,zh;q=0.9",
            "Content-Type": "application/json",
            "Cookie": self.context["cookie"],
            "Origin": DOUBAO_BASE_URL,
            "Referer": DOUBAO_WEB_URL,
            "User-Agent": (
                "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/131.0.0.0 Safari/537.36"
            ),
            "agw-js-conv": "str",
        }
        csrf = cookies.get("passport_csrf_token") or cookies.get(
            "passport_csrf_token_default", ""
        )
        if csrf:
            headers["x-tt-passport-csrf-token"] = csrf
        return headers

    def _post(self, path: str, payload: dict[str, Any], *, poll=False) -> bytes:
        params = self._params(omit_fp=poll)
        url = DOUBAO_BASE_URL.rstrip("/") + path + "?" + urllib.parse.urlencode(params)
        request = urllib.request.Request(
            url,
            data=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
            headers=self._headers(),
            method="POST",
        )
        try:
            with urllib.request.urlopen(request, timeout=self.timeout) as response:
                raw = response.read(DOUBAO_MAX_VIDEO_BYTES + 1)
                if len(raw) > DOUBAO_MAX_VIDEO_BYTES:
                    raise GatewayError("doubao_response_too_large", "豆包网页响应过大。", 502)
                rotated = response.headers.get("x-ms-token")
                if rotated:
                    self.context["ms_token"] = rotated
                return raw
        except GatewayError:
            raise
        except urllib.error.HTTPError as exc:
            body = exc.read(64 * 1024)
            # HTTP error bodies are parsed only for a numeric code/message;
            # raw upstream text is never returned to the caller.
            obj = _json_load(body)
            if isinstance(obj, dict):
                raise _event_error(obj.get("code"), obj.get("message") or obj.get("msg")) from None
            raise _event_error(exc.code, "") from None
        except (urllib.error.URLError, TimeoutError, OSError):
            raise GatewayError("doubao_network_error", "豆包网页暂时无法连接。", 502) from None

    @staticmethod
    def _parse_gateway_events(raw: bytes) -> list[Any]:
        events: list[Any] = []
        for event_name, data in _sse_events(raw):
            if event_name == "gateway-error":
                # _sse_events already raises, retained for defensive clarity.
                obj = data if isinstance(data, dict) else {}
                raise _event_error(obj.get("code"), obj.get("message"))
            events.append(data)
        return events

    @staticmethod
    def _image_payload(prompt: str, ratio: str | None) -> dict[str, Any]:
        content: dict[str, Any] = {"text": prompt}
        if ratio:
            content["ratio"] = ratio
        return {
            "messages": [
                {
                    "content": json.dumps(content, ensure_ascii=False),
                    "content_type": 2009,
                    "attachments": [],
                    "references": [],
                    "skill": {
                        "skill_type": 3,
                        "skill_type_no_default": 3,
                        "skill_id": "3",
                        "skill_id_no_default": "3",
                    },
                }
            ],
            "completion_option": {
                "is_regen": False,
                "with_suggest": True,
                "need_create_conversation": True,
                "launch_stage": 1,
                "is_replace": False,
                "is_delete": False,
                "is_ai_playground": False,
                "memory_type": 2,
                "message_from": 0,
                "use_deep_think": False,
                "use_auto_cot": False,
                "resend_for_regen": False,
                "enable_commerce_credit": False,
                "action_bar_skill_id": 3,
            },
            "evaluate_option": {"web_ab_params": ""},
            "local_conversation_id": str(uuid.uuid4()),
            "local_message_id": str(uuid.uuid4()),
        }

    @staticmethod
    def _video_payload(prompt: str, model: str, request) -> dict[str, Any]:
        content = {
            "text": prompt,
            "model": model,
            "duration": request.duration,
            "resolution": request.resolution,
            "ratio": request.aspect_ratio,
            "audio": bool(request.audio),
        }
        return {
            "messages": [
                {
                    "content": json.dumps(content, ensure_ascii=False),
                    "content_type": 2020,
                    "attachments": [],
                    "references": [],
                    "skill": {
                        "skill_type": 17,
                        "skill_type_no_default": 17,
                        "skill_id": "17",
                        "skill_id_no_default": "17",
                    },
                }
            ],
            "completion_option": {
                "is_regen": False,
                "with_suggest": True,
                "need_create_conversation": True,
                "launch_stage": 1,
                "is_replace": False,
                "is_delete": False,
                "is_ai_playground": False,
                "memory_type": 2,
                "message_from": 0,
                "use_deep_think": False,
                "use_auto_cot": False,
                "resend_for_regen": False,
                "enable_commerce_credit": False,
                "action_bar_skill_id": 17,
            },
            "evaluate_option": {"web_ab_params": ""},
            "local_conversation_id": str(uuid.uuid4()),
            "local_message_id": str(uuid.uuid4()),
        }

    @staticmethod
    def _chat_payload(prompt: str, model: str) -> dict[str, Any]:
        local = str(uuid.uuid4())
        model_key = DOUBAO_TEXT_MODEL_KEYS.get(model, "0")
        is_work = model in {"doubao-pro", "doubao-lite"}
        return {
            "client_meta": {
                "local_conversation_id": local,
                "conversation_id": "",
                "bot_id": DOUBAO_DEFAULT_BOT_ID,
                "last_section_id": "",
                "last_message_index": 0,
                "local_permissions": [],
            },
            "messages": [
                {
                    "local_message_id": str(uuid.uuid4()),
                    "content_block": [
                        {
                            "block_type": 10000,
                            "content": {
                                "text_block": {
                                    "text": prompt,
                                    "icon_url": "",
                                    "icon_url_dark": "",
                                    "summary": "",
                                },
                                "pc_event_block": "",
                            },
                            "block_id": str(uuid.uuid4()),
                            "parent_id": "",
                            "meta_info": [],
                            "append_fields": [],
                        }
                    ],
                    "message_status": 0,
                }
            ],
            "option": {
                "create_time_ms": int(time.time() * 1000),
                "is_audio": False,
                "answer_with_suggest": False,
                "agent_mode": 1 if is_work else 2,
                "tts_switch": False,
                "need_deep_think": 0,
                "need_create_conversation": True,
                "conversation_init_ext": {"model_item_key": model_key},
                "sse_recv_event_options": {"support_chunk_delta": True},
                "support_lazy_fetch_stream": True,
                "conversation_mode": 1,
                "model_config": {
                    "model_item_key": model_key,
                    "model_extra_params": (
                        {"total_window_size": "256000"} if is_work else {}
                    ),
                    "reasoning_effort": 5 if is_work else 3,
                },
                "aggregate_params": {
                    "model_item_key": model_key,
                    "conversation_mode": "1",
                    "agent_mode": "1" if is_work else "2",
                },
            },
            "user_context": [],
            "ext": {"use_deep_think": "0", "collection_id": ""},
        }

    @staticmethod
    def _ensure_no_references(reference_paths):
        if reference_paths:
            raise GatewayError(
                "doubao_references_require_browser",
                "豆包网页参考图上传需要官方页面生成的签名；当前直连适配器不绕过该校验，请先使用无参考图任务或官方网页。",
                422,
            )

    @staticmethod
    def _parse_image_events(raw: bytes) -> list[str]:
        urls: list[str] = []
        for data in DoubaoWebBackend._parse_gateway_events(raw):
            message = _message_from_event(data)
            if not message or message.get("content_type") != 2010:
                if isinstance(data, dict) and data.get("event_type") == 2005:
                    raise _event_error(0, str(data.get("event_data", "")))
                continue
            content = _json_load(message.get("content", {}))
            urls.extend(_find_urls(content, {"url", "main_url"}))
        return list(dict.fromkeys(urls))

    @staticmethod
    def _parse_video_events(raw: bytes) -> tuple[list[str], str | None]:
        urls: list[str] = []
        task_id = None
        for data in DoubaoWebBackend._parse_gateway_events(raw):
            message = _message_from_event(data)
            if isinstance(data, dict):
                event_data = _json_load(data.get("event_data", {}))
                fin = event_data.get("fin_reason", {}) if isinstance(event_data, dict) else {}
                if isinstance(fin, dict) and isinstance(fin.get("async_task"), dict):
                    task_id = str(fin["async_task"].get("id", "") or "") or task_id
            if not message:
                if isinstance(data, dict) and data.get("event_type") == 2005:
                    raise _event_error(0, str(data.get("event_data", "")))
                continue
            if message.get("content_type") != 2021:
                continue
            content = _json_load(message.get("content", {}))
            urls.extend(_find_urls(content, {"url", "main_url", "video_url"}))
            if isinstance(content, dict):
                for item in content.get("data", [content]):
                    if not isinstance(item, dict):
                        continue
                    model_value = _json_load(item.get("video_model", {}))
                    urls.extend(_find_urls(model_value, {"main_url"}))
                    if isinstance(item.get("video_model"), str):
                        try:
                            vm = json.loads(item["video_model"])
                            for info in (vm.get("video_list", {}) or {}).values():
                                encoded = info.get("main_url", "") if isinstance(info, dict) else ""
                                if encoded:
                                    try:
                                        decoded = base64.b64decode(encoded).decode("utf-8")
                                    except Exception:  # noqa: BLE001
                                        decoded = ""
                                    if decoded.startswith("https://"):
                                        urls.append(decoded)
                        except (TypeError, ValueError, json.JSONDecodeError):
                            pass
        return list(dict.fromkeys(urls)), task_id

    @staticmethod
    def _download(url: str, path: Path, limit: int):
        parsed = urllib.parse.urlsplit(url)
        if parsed.scheme != "https" or not parsed.hostname:
            raise GatewayError("doubao_invalid_media_url", "豆包返回了无效下载地址。", 502)
        request = urllib.request.Request(url, headers={"Accept": "*/*"})
        try:
            with urllib.request.urlopen(request, timeout=120) as response, path.open("wb") as output:
                total = 0
                while True:
                    chunk = response.read(1024 * 1024)
                    if not chunk:
                        break
                    total += len(chunk)
                    if total > limit:
                        raise GatewayError("doubao_media_too_large", "豆包媒体超过本地保存上限。", 413)
                    output.write(chunk)
        except GatewayError:
            path.unlink(missing_ok=True)
            raise
        except (urllib.error.URLError, TimeoutError, OSError):
            path.unlink(missing_ok=True)
            raise GatewayError("doubao_download_failed", "豆包生成结果下载失败。", 502) from None

    async def generate_image(self, request, folder: Path, reference_paths):
        self._ensure_no_references(reference_paths)
        prompt = request.render_prompt()
        raw = await asyncio.to_thread(
            self._post,
            "/samantha/chat/completion",
            self._image_payload(prompt, request.composition()),
        )
        urls = self._parse_image_events(raw)
        if not urls:
            raise GatewayError(
                "doubao_generation_failed",
                "豆包网页未返回图片；如果网页要求验证，请先在官方页面完成验证。",
                502,
            )
        paths = []
        for index, url in enumerate(urls[: max(1, request.n)]):
            suffix = mimetypes.guess_extension(urllib.parse.urlsplit(url).path.rsplit(".", 1)[-1]) or ".jpg"
            path = folder / f"doubao-{uuid.uuid4().hex}{suffix}"
            await asyncio.to_thread(self._download, url, path, DOUBAO_MAX_IMAGE_BYTES)
            paths.append(path)
        return paths

    async def generate(self, request, folder: Path, reference_paths):
        self._ensure_no_references(reference_paths)
        model = request.model if request.model != "default" else DOUBAO_DEFAULT_VIDEO_MODEL
        raw = await asyncio.to_thread(
            self._post,
            "/samantha/chat/completion",
            self._video_payload(request.prompt, model, request),
        )
        urls, task_id = self._parse_video_events(raw)
        deadline = time.monotonic() + self.timeout
        while not urls and task_id and time.monotonic() < deadline:
            await asyncio.sleep(DOUBAO_POLL_INTERVAL)
            poll_raw = await asyncio.to_thread(
                self._post,
                "/samantha/chat/async/stream",
                {"task_id": task_id, "event_id": 0},
                poll=True,
            )
            urls, next_task = self._parse_video_events(poll_raw)
            task_id = next_task or task_id
        if not urls:
            if task_id:
                raise GatewayError("doubao_timeout", "等待豆包网页视频超时，请检查官方网页任务记录。", 504)
            raise GatewayError("doubao_generation_failed", "豆包网页未返回视频。", 502)
        path = folder / f"doubao-{uuid.uuid4().hex}.mp4"
        await asyncio.to_thread(self._download, urls[0], path, DOUBAO_MAX_VIDEO_BYTES)
        return path

    async def chat(self, request, reference_paths=None):
        self._ensure_no_references(reference_paths or [])
        model = request.model if request.model != "default" else DOUBAO_DEFAULT_TEXT_MODEL
        prompt = request.render_prompt()
        raw = await asyncio.to_thread(
            self._post,
            "/chat/completion",
            self._chat_payload(prompt, model),
        )
        parts: list[str] = []
        for data in self._parse_gateway_events(raw):
            if not isinstance(data, dict):
                continue
            if data.get("event_type") == 2005:
                raise _event_error(0, str(data.get("event_data", "")))
            if data.get("event_type") == "CHUNK_DELTA" and data.get("text"):
                parts.append(str(data["text"]))
                continue
            message = _message_from_event(data)
            if not message:
                continue
            blocks = _json_load(message.get("content", {}))
            if isinstance(blocks, dict):
                for block in blocks.get("content_block", []):
                    text = ((block.get("content") or {}).get("text_block") or {}).get("text")
                    if text:
                        parts.append(str(text))
        answer = "".join(parts)
        if not answer:
            raise GatewayError("doubao_generation_failed", "豆包网页未返回文字回复。", 502)
        return answer

