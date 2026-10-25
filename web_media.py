"""Direct adapters for web products that expose a user free allowance.

This module intentionally uses the signed-in user's own web session.  It does
not create accounts, bypass a queue, or turn a paid API into a free one.  The
Bing endpoints are the same endpoints used by Bing Image/Video Creator and
may change without notice; the account's available boosts and regional access
remain controlled by Microsoft.
"""

import asyncio
import html
import json
import mimetypes
import re
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid
from pathlib import Path

from pydantic import BaseModel, ConfigDict, Field, field_validator

from storage import GatewayError, atomic_json

BING_ID = "bing"
BING_WEB_URL = "https://www.bing.com/images/create"
BING_VIDEO_URL = "https://www.bing.com/images/create/ai-video-generator"
BING_POLL_INTERVAL = 3
BING_MAX_HTML_BYTES = 24 * 1024 * 1024
BING_MAX_OUTPUT_BYTES = 100 * 1024 * 1024
BING_USER_AGENT = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"
)


class BingCookieRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    cookie: str | None = Field(default=None, max_length=8192, repr=False)

    @field_validator("cookie")
    @classmethod
    def cookie_value(cls, value):
        if not value:
            return value
        value = value.strip()
        # Accept a copied `_U=...` pair as a convenience, but store only the
        # value.  A complete Cookie header is rejected to avoid persisting
        # unrelated Microsoft cookies.
        if value.startswith("_U="):
            value = value[3:]
        if not value or any(char.isspace() for char in value) or ";" in value:
            raise ValueError("请填写 Bing _U Cookie 的 Value，不要包含名称、空格或分号。")
        return value


class BingConfigStore:
    """Persist only the Bing `_U` value and never expose it through JSON."""

    def __init__(self, state_dir: Path):
        self.path = state_dir / "bing.json"
        self.cookie = ""
        if self.path.exists():
            try:
                value = json.loads(self.path.read_text(encoding="utf-8"))
                self.cookie = str(value.get("cookie", "")).strip()
            except (OSError, TypeError, ValueError):
                self.cookie = ""

    @property
    def configured(self):
        return bool(self.cookie)

    def public(self):
        return {
            "id": BING_ID,
            "name": "Bing Image / Video Creator",
            "web_url": BING_WEB_URL,
            "base_url": "https://www.bing.com",
            "docs_url": "https://www.bing.com/images/create",
            "configured": self.configured,
            "auth_type": "bing_cookie",
            "verification_status": "web_candidate",
            "verification_label": "网页候选 · 需账号实测",
            "free_bucket": "Boost / 网页免费额度",
            "browser_required": False,
            "free_note": "使用微软网页账号自己的 Boost/免费额度；额度和可用模型由 Bing 账户页面决定。",
            "images": True,
            "videos": True,
            "references": False,
            "fit": "Q3 / Q2 图片替代；微软网页免费 Boost",
            "capabilities": "Bing Image Creator 文生图；可选 DALL·E 3、GPT-4o、MAI Image",
            "models": ["bing-dalle3", "bing-gpt4o", "bing-mai-image-1"],
        }

    def save(self, cookie: str | None):
        try:
            value = BingCookieRequest(cookie=cookie).cookie or ""
        except ValueError as exc:
            raise GatewayError("invalid_bing_cookie", str(exc), 422) from None
        if not value:
            raise GatewayError("cookie_required", "请填写 Bing 账号的 _U Cookie Value。", 422)
        try:
            atomic_json(self.path, {"cookie": value})
        except OSError:
            raise GatewayError("storage_error", "Bing Cookie 保存失败。", 500) from None
        self.cookie = value
        return self.public()

    def clear(self):
        try:
            self.path.unlink(missing_ok=True)
        except OSError:
            raise GatewayError("storage_error", "Bing Cookie 删除失败。", 500) from None
        self.cookie = ""
        return self.public()


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):  # noqa: D401
        return None


def _is_bing_url(value: str) -> bool:
    try:
        parsed = urllib.parse.urlsplit(value)
    except ValueError:
        return False
    host = (parsed.hostname or "").lower().rstrip(".")
    return parsed.scheme == "https" and (host == "bing.com" or host.endswith(".bing.com"))


def _normalize_bing_media_url(value: str, *, video=False) -> str | None:
    value = html.unescape(value).replace("\\/", "/").replace("\\u0026", "&")
    if value.startswith("/"):
        value = urllib.parse.urljoin("https://www.bing.com", value)
    if not _is_bing_url(value):
        return None
    parsed = urllib.parse.urlsplit(value)
    host = (parsed.hostname or "").lower()
    if host.startswith("tse") and host.endswith(".mm.bing.net"):
        host = "www.bing.com"
    query = urllib.parse.parse_qs(parsed.query, keep_blank_values=True)
    if video:
        if query.get("pid", [""])[0].lower() != "videocreator":
            return None
        if any(key in query for key in ("w", "h", "c", "rs", "qlt", "o")):
            return None
        return urllib.parse.urlunsplit(("https", host, parsed.path, parsed.query, ""))
    if not parsed.path.startswith("/th/id/") or parsed.path.lower().endswith((".js", ".svg")):
        return None
    pid = query.get("pid", ["ImgGn"])[0] or "ImgGn"
    return urllib.parse.urlunsplit(("https", host, parsed.path, "pid=" + urllib.parse.quote(pid), ""))


class BingWebBackend:
    """Bing web image and short-video generation using an `_U` cookie."""

    def __init__(self, cookie: str, timeout: int = 450):
        if not cookie:
            raise GatewayError("bing_not_configured", "请先配置 Bing _U Cookie。", 409)
        self.cookie = cookie
        self.timeout = timeout

    def _headers(self, accept="text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8"):
        return {
            "Cookie": f"_U={self.cookie}",
            "User-Agent": BING_USER_AGENT,
            "Accept": accept,
            "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8",
            "Referer": BING_WEB_URL,
        }

    def _request(self, method: str, url: str, data=None, accept=None):
        request = urllib.request.Request(
            url,
            data=data,
            headers=self._headers(accept or "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8"),
            method=method,
        )
        opener = urllib.request.build_opener(_NoRedirect)
        try:
            return opener.open(request, timeout=self.timeout)
        except urllib.error.HTTPError as exc:
            if 300 <= exc.code < 400:
                return exc
            if exc.code in {401, 403}:
                raise GatewayError(
                    "bing_authentication_failed",
                    "Bing Cookie 已失效或当前账号没有创作权限，请从 Bing 网页更新 _U Cookie。",
                    401,
                ) from None
            if exc.code == 429:
                raise GatewayError(
                    "bing_rate_limited",
                    "Bing 网页请求受限或免费额度暂时不可用，请稍后重试。",
                    429,
                ) from None
            raise GatewayError(
                "bing_upstream_error", f"Bing 网页请求失败（HTTP {exc.code}）。", 502
            ) from None
        except (urllib.error.URLError, TimeoutError, OSError):
            raise GatewayError("bing_network_error", "Bing 网页暂时无法连接。", 502) from None

    def _start(self, prompt: str, *, model: int, aspect_ratio: int, video=False) -> str:
        if video:
            base = BING_VIDEO_URL
            query = {
                "q": prompt,
                "rt": "4",
                "mdl": "0",
                "ar": str(aspect_ratio),
                "FORM": "GENCRE",
                "hva": "4",
                "pt": "4",
                "sm": "1",
            }
        else:
            base = BING_WEB_URL
            query = {
                "q": prompt,
                "rt": "4",
                "mdl": str(model),
                "ar": str(aspect_ratio),
                "FORM": "GENCRE",
            }
        url = base + "?" + urllib.parse.urlencode(query)
        response = self._request("POST", url, data=b"")
        try:
            location = response.headers.get("Location", "")
        finally:
            response.close()
        if not location:
            raise GatewayError(
                "bing_authentication_failed",
                "Bing 没有返回生成任务，Cookie 可能已失效或网页额度不可用。",
                401,
            )
        parsed = urllib.parse.urlsplit(urllib.parse.urljoin("https://www.bing.com", location))
        request_id = urllib.parse.parse_qs(parsed.query).get("id", [""])[0]
        if not request_id:
            raise GatewayError("bing_invalid_response", "Bing 没有返回任务 ID。", 502)
        return request_id

    @staticmethod
    def _image_urls(body: str) -> list[str]:
        urls = []
        for raw in re.findall(r"(?:src|data-src)=[\"']([^\"']+)[\"']", body, re.I):
            normalized = _normalize_bing_media_url(raw)
            if normalized and normalized not in urls:
                urls.append(normalized)
        return urls

    @staticmethod
    def _video_url(body: str) -> str | None:
        for raw in re.findall(r"ourl=[\"']([^\"']+)[\"']", body, re.I):
            normalized = _normalize_bing_media_url(raw, video=True)
            if normalized:
                return normalized
        return None

    def _text(self, url: str) -> str:
        response = self._request("GET", url)
        try:
            body = response.read(BING_MAX_HTML_BYTES + 1)
        finally:
            response.close()
        if len(body) > BING_MAX_HTML_BYTES:
            raise GatewayError("bing_response_too_large", "Bing 返回内容过大。", 502)
        return body.decode("utf-8", errors="replace")

    def _poll_image(self, prompt: str, request_id: str, model: int, aspect_ratio: int) -> list[str]:
        url = "https://www.bing.com/images/create/async/results/" + urllib.parse.quote(request_id)
        url += "?" + urllib.parse.urlencode({"q": prompt, "mdl": model, "ar": aspect_ratio})
        deadline = time.monotonic() + self.timeout
        while time.monotonic() < deadline:
            body = self._text(url)
            if body.strip():
                if body.lstrip().startswith("{"):
                    try:
                        result = json.loads(body)
                    except ValueError:
                        result = {}
                    if result.get("errorMessage"):
                        raise GatewayError("bing_generation_failed", str(result["errorMessage"]), 502)
                urls = self._image_urls(body)
                if urls:
                    return urls
            # This legacy synchronous helper is retained for compatibility;
            # the production path uses `_poll_image_async` below.  Keep this
            # branch synchronous so importing the module works on all Python
            # versions (an `await` here would make the module invalid syntax).
            time.sleep(BING_POLL_INTERVAL)
        raise GatewayError("bing_timeout", "等待 Bing 图片生成超时，请到 Bing 任务记录查看。", 504)

    def _poll_video(self, prompt: str, request_id: str, aspect_ratio: int) -> str:
        raise NotImplementedError

    async def generate(self, request, folder: Path, remaining: int, reference_paths=None, **_kwargs):
        if reference_paths:
            raise GatewayError("bing_references_unsupported", "Bing 网页接口当前不接受本地参考图。", 422)
        model_map = {"default": 0, "bing-dalle3": 0, "bing-gpt4o": 1, "bing-mai-image-1": 4}
        if request.model not in model_map:
            raise GatewayError("invalid_model", "Bing 图片模型支持 bing-dalle3、bing-gpt4o 或 bing-mai-image-1。", 422)
        ratio = request.aspect_ratio or request.composition() or "1:1"
        aspect_ratio = 1 if ratio in {"1:1"} else 3 if ratio in {"9:16", "3:4", "2:3"} else 2
        prompt = request.render_prompt()
        urls = await asyncio.to_thread(
            self._start, prompt, model=model_map[request.model], aspect_ratio=aspect_ratio, video=False
        )
        # Polling performs blocking HTTP requests; run the whole polling loop
        # in the event loop only for sleeps would block other jobs, so each
        # request is delegated below through a small async wrapper.
        urls = await self._poll_image_async(prompt, urls, model_map[request.model], aspect_ratio)
        saved = []
        for index, url in enumerate(urls[:remaining]):
            path = folder / f"bing-{uuid.uuid4().hex}.png"
            await asyncio.to_thread(self._download, url, path)
            saved.append(path)
        return saved

    async def _poll_image_async(self, prompt, request_id, model, aspect_ratio):
        url = "https://www.bing.com/images/create/async/results/" + urllib.parse.quote(request_id)
        url += "?" + urllib.parse.urlencode({"q": prompt, "mdl": model, "ar": aspect_ratio})
        deadline = time.monotonic() + self.timeout
        while time.monotonic() < deadline:
            body = await asyncio.to_thread(self._text, url)
            if body.strip():
                if body.lstrip().startswith("{"):
                    try:
                        result = json.loads(body)
                    except ValueError:
                        result = {}
                    if result.get("errorMessage"):
                        raise GatewayError("bing_generation_failed", str(result["errorMessage"]), 502)
                urls = self._image_urls(body)
                if urls:
                    return urls
            await asyncio.sleep(BING_POLL_INTERVAL)
        raise GatewayError("bing_timeout", "等待 Bing 图片生成超时，请到 Bing 任务记录查看。", 504)

    def _download(self, url: str, path: Path):
        response = self._request("GET", url, accept="image/*")
        try:
            contents = response.read(BING_MAX_OUTPUT_BYTES + 1)
        finally:
            response.close()
        if len(contents) > BING_MAX_OUTPUT_BYTES:
            raise GatewayError("bing_response_too_large", "Bing 图片超过本地保存上限。", 413)
        if not contents:
            raise GatewayError("bing_download_failed", "Bing 图片为空。", 502)
        path.write_bytes(contents)

    async def generate_video(self, request, folder: Path, reference_paths=None):
        if reference_paths:
            raise GatewayError("bing_references_unsupported", "Bing 视频网页接口当前不接受本地参考图。", 422)
        ratio = request.aspect_ratio
        aspect_ratio = 4 if ratio in {"9:16", "3:4"} else 5
        prompt = request.prompt
        request_id = await asyncio.to_thread(
            self._start, prompt, model=0, aspect_ratio=aspect_ratio, video=True
        )
        async_url = "https://www.bing.com/images/create/async/results/" + urllib.parse.quote(request_id)
        async_url += "?" + urllib.parse.urlencode({"q": prompt, "mdl": 0, "ar": aspect_ratio})
        result_url = BING_VIDEO_URL + "?" + urllib.parse.urlencode(
            {"q": prompt, "id": request_id, "rt": 4, "pt": 4, "FORM": "GUH2CR", "dmreload": 1}
        )
        deadline = time.monotonic() + self.timeout
        video_url = None
        while time.monotonic() < deadline:
            body = await asyncio.to_thread(self._text, async_url)
            video_url = self._video_url(body)
            if not video_url and body.lstrip().startswith("{"):
                try:
                    result = json.loads(body)
                except ValueError:
                    result = {}
                message = str(result.get("errorMessage", ""))
                if message and "pending" not in message.lower() and "still" not in message.lower():
                    raise GatewayError("bing_generation_failed", message, 502)
            if not video_url:
                page = await asyncio.to_thread(self._text, result_url)
                video_url = self._video_url(page)
            if video_url:
                break
            await asyncio.sleep(BING_POLL_INTERVAL)
        if not video_url:
            raise GatewayError("bing_timeout", "等待 Bing 视频生成超时，请到 Bing 任务记录查看。", 504)
        path = folder / f"bing-{uuid.uuid4().hex}.mp4"
        await asyncio.to_thread(self._download, video_url, path)
        return path

