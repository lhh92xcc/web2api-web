"""Authenticated browser bridge with single-delivery task leases."""

import asyncio
import base64
import binascii
import contextlib
import hmac
import io
import json
import secrets
import time
import uuid
import warnings
from pathlib import Path
from typing import Literal

from fastapi import FastAPI, Header, Request
from fastapi.responses import FileResponse
from PIL import Image, UnidentifiedImageError
from pydantic import BaseModel, ConfigDict, Field

from providers import ProviderStore, WebsiteProvider
from storage import GatewayError, atomic_json

MAX_IMAGE_BYTES = 20 * 1024 * 1024
MAX_RESULT_BYTES = 4 * 28 * 1024 * 1024 + 1024 * 1024
BRIDGE_ERRORS = {
    "login_required": "请在专用网页标签中登录，再重新提交任务。",
    "page_changed": "网页结构与适配器不匹配，请检查页面或修改平台选择器。",
    "page_busy": "专用标签中已有生成或未发送内容，请处理后再提交。",
    "upload_failed": "参考图未能附加到网页，任务未发送。",
    "send_failed": "未能确认网页提交状态。请检查专用标签，确认后再决定是否重新提交。",
    "download_failed": "网页已生成图片，但无法读取文件。请从专用标签下载，避免重复提交。",
    "no_generated_image": "网页回复中没有识别到生成图片。请检查额度、提示词和适配器。",
    "upstream_timeout": "等待网页结果超时。请先检查专用标签中的生成记录。",
    "browser_interrupted": "专用标签已关闭、刷新或断开。请先检查网页记录；任务没有自动重新提交。",
    "upstream_error": "网页操作失败。请检查专用标签；任务没有自动重新提交。",
}


class BridgeModel(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)


class TabState(BridgeModel):
    provider: str = Field(pattern=r"^[a-z][a-z0-9_-]{0,39}$")
    status: Literal["ready", "busy", "login_required", "page_changed", "disconnected"]


class Heartbeat(BridgeModel):
    session_id: str = Field(pattern=r"^[a-zA-Z0-9_-]{12,100}$")
    tabs: list[TabState] = Field(max_length=30)


class Claim(BridgeModel):
    session_id: str = Field(pattern=r"^[a-zA-Z0-9_-]{12,100}$")


class Lease(Claim):
    lease: str = Field(min_length=20, max_length=200, repr=False)


class BridgeResult(Lease):
    text: str = Field(default="", max_length=200000)
    images: list[str] = Field(default_factory=list, max_length=4, repr=False)
    error: (
        Literal[
            "login_required",
            "page_changed",
            "page_busy",
            "upload_failed",
            "send_failed",
            "download_failed",
            "no_generated_image",
            "upstream_timeout",
            "browser_interrupted",
            "upstream_error",
        ]
        | None
    ) = None


def inspect_image(encoded: str) -> tuple[bytes, str]:
    if len(encoded) > (MAX_IMAGE_BYTES * 4 // 3 + 8):
        raise GatewayError("image_too_large", "网页图片最大 20 MB。", 413)
    try:
        data = base64.b64decode(encoded, validate=True)
        if len(data) > MAX_IMAGE_BYTES:
            raise GatewayError("image_too_large", "网页图片最大 20 MB。", 413)
        with warnings.catch_warnings():
            warnings.simplefilter("error", Image.DecompressionBombWarning)
            with Image.open(io.BytesIO(data)) as image:
                extension = {"PNG": ".png", "JPEG": ".jpg", "WEBP": ".webp"}.get(
                    image.format
                )
                if not extension or image.width * image.height > 50_000_000:
                    raise ValueError
                image.verify()
            with Image.open(io.BytesIO(data)) as image:
                image.load()
        return data, extension
    except (
        binascii.Error,
        ValueError,
        OSError,
        SyntaxError,
        UnidentifiedImageError,
        Image.DecompressionBombWarning,
        Image.DecompressionBombError,
    ):
        raise GatewayError(
            "invalid_image", "网页返回的文件不是完整的 PNG、JPEG 或 WebP 图片。", 415
        ) from None


class BrowserBridge:
    def __init__(self, state_dir: Path, providers: ProviderStore, timeout: int):
        self.providers, self.timeout = providers, timeout
        self.secret_path = state_dir / "bridge-secret.json"
        self.token = (
            json.loads(self.secret_path.read_text()).get("token", "")
            if self.secret_path.exists()
            else ""
        )
        self.sessions: dict[str, dict] = {}
        self.pending: dict[str, dict] = {}

    def pair(self) -> str:
        if self.pending:
            raise GatewayError("busy", "请等待网页任务结束后再重新配对。", 409)
        token = "br_" + secrets.token_urlsafe(32)
        atomic_json(self.secret_path, {"token": token})
        self.token = token
        self.sessions.clear()
        return token

    def authenticate(self, authorization: str):
        expected = "Bearer " + self.token
        if not self.token or not hmac.compare_digest(authorization, expected):
            raise GatewayError(
                "bridge_unauthorized", "扩展配对已失效，请重新填写配对码。", 401
            )

    def heartbeat(self, body: Heartbeat):
        known = {}
        for tab in body.tabs:
            item = self.providers.get(tab.provider)
            if item.enabled:
                known[tab.provider] = tab.status
        self.sessions[body.session_id] = {"seen": time.time(), "tabs": known}
        self.sessions = {
            key: value
            for key, value in self.sessions.items()
            if time.time() - value["seen"] < 120
        }

    def state(self, provider: str) -> str:
        statuses = [
            session["tabs"][provider]
            for session in self.sessions.values()
            if time.time() - session["seen"] < 75 and provider in session["tabs"]
        ]
        for state in ("ready", "busy", "login_required", "page_changed"):
            if state in statuses:
                return state
        return "disconnected"

    def require_ready(self, provider: str):
        item = self.providers.get(provider)
        if not item.enabled:
            raise GatewayError("provider_disabled", "此平台已停用。", 409)
        status = self.state(provider)
        if status not in {"ready", "busy"}:
            raise GatewayError(
                "browser_not_ready",
                "请先安装并配对浏览器扩展，打开此平台专用标签并登录。",
                503,
            )

    def claim(self, body: Claim) -> dict | None:
        session = self.sessions.get(body.session_id)
        if not session or time.time() - session["seen"] >= 75:
            raise GatewayError("heartbeat_required", "请先报告专用标签状态。", 409)
        for ticket in self.pending.values():
            if ticket["claimed"] or ticket["expires_at"] <= time.time():
                continue
            if session["tabs"].get(ticket["provider"]) != "ready":
                continue
            ticket["claimed"] = body.session_id
            return {
                key: value
                for key, value in ticket.items()
                if key not in {"future", "claimed", "paths"}
            }
        return None

    def leased(self, ticket_id: str, body: Lease) -> dict:
        ticket = self.pending.get(ticket_id)
        if (
            not ticket
            or ticket["claimed"] != body.session_id
            or ticket["expires_at"] <= time.time()
            or not hmac.compare_digest(ticket["lease"], body.lease)
        ):
            raise GatewayError(
                "lease_expired", "任务租约已失效，结果未写入。请检查网页记录。", 409
            )
        return ticket

    def resolve(self, ticket_id: str, body: BridgeResult):
        ticket = self.leased(ticket_id, body)
        if ticket["future"].done():
            raise GatewayError("result_already_received", "此任务已接收结果。", 409)
        if body.error:
            ticket["future"].set_exception(
                GatewayError(body.error, BRIDGE_ERRORS[body.error])
            )
        elif ticket["kind"] == "chat":
            if body.images or not body.text.strip():
                raise GatewayError("invalid_result", "文字任务需要非空文本回复。", 422)
            ticket["future"].set_result(body.text)
        else:
            if not body.images or len(body.images) > ticket["remaining"]:
                raise GatewayError("invalid_result", "图片数量与任务不匹配。", 422)
            images = [inspect_image(value) for value in body.images]
            ticket["future"].set_result(images)

    async def run(
        self, request, kind: str, paths: list[Path], folder=None, remaining=1
    ):
        self.require_ready(request.provider)
        ticket_id = "web_" + uuid.uuid4().hex
        future = asyncio.get_running_loop().create_future()
        ticket = {
            "id": ticket_id,
            "lease": secrets.token_urlsafe(32),
            "provider": request.provider,
            "kind": kind,
            "adapter": self.providers.get(request.provider).model_dump(),
            "prompt": request.render_prompt(),
            "remaining": remaining,
            "full_size": getattr(request, "full_size", True),
            "reference_count": len(paths),
            "paths": paths,
            "expires_at": time.time() + self.timeout,
            "claimed": None,
            "future": future,
        }
        self.pending[ticket_id] = ticket
        try:
            async with asyncio.timeout(self.timeout):
                output = await future
            if kind == "chat":
                return output
            saved = []
            for content, extension in output:
                target = folder / ("web_" + uuid.uuid4().hex + extension)
                target.write_bytes(content)
                saved.append(target)
            return saved
        except TimeoutError:
            raise GatewayError(
                "upstream_timeout", BRIDGE_ERRORS["upstream_timeout"], 504
            ) from None
        finally:
            self.pending.pop(ticket_id, None)
            if future.done() and not future.cancelled():
                with contextlib.suppress(Exception):
                    future.exception()

    def close(self):
        for ticket in self.pending.values():
            if not ticket["future"].done():
                ticket["future"].cancel()


def register_bridge(app: FastAPI, get_service):
    @app.get("/api/providers")
    async def providers(request: Request):
        engine = get_service(request)
        data = [
            {
                "id": "gemini",
                "name": "Gemini",
                "mode": "native",
                "enabled": True,
                "images": True,
                "text": True,
                "references": True,
                "state": "ready"
                if engine.status()["connected"]
                else "configured"
                if engine.status()["configured"]
                else "unconfigured",
            }
        ]
        data.extend(
            {
                **item.model_dump(),
                "mode": "browser",
                "state": engine.bridge.state(item.id),
            }
            for item in engine.providers.items.values()
        )
        for item in (
            engine.bing.public(),
            engine.imagefx.public(),
            engine.recraft.public(),
            engine.leonardo.public(),
        ):
            data.append(
                {
                    "id": item["id"],
                    "name": item["name"],
                    "mode": "public" if item.get("auth_type") == "public" else "native",
                    "enabled": item["configured"],
                    "images": bool(item.get("images")),
                    "text": bool(item.get("text")),
                    "references": bool(item.get("references")),
                    "state": "ready" if item["configured"] else "unconfigured",
                    "models": [
                        {
                            "id": model_id,
                            "display_name": model_id,
                            "available": True,
                        }
                        for model_id in item.get("models", [])
                    ],
                    "image_models": item.get("image_models", []),
                    "default_model": item.get("image_default_model"),
                    "auth_type": item.get("auth_type", "api_key"),
                    "credential_label": item.get("credential_label", ""),
                    "capabilities": item.get("capabilities", ""),
                    "api_free_note": item.get("free_note", ""),
                }
            )
        data.extend(
            {
                "id": item["id"],
                "name": item["name"],
                "mode": "public" if item.get("auth_type") == "public" else "native",
                "enabled": item["configured"],
                "images": bool(item.get("images")),
                "text": bool(item.get("text")),
                "references": bool(item.get("references")),
                "state": "ready" if item["configured"] else "unconfigured",
                "models": [
                    {
                        "id": model_id,
                        "display_name": model_id,
                        "available": True,
                    }
                    for model_id in item.get("image_models", [])
                ],
                "image_models": item.get("image_models", []),
                "default_model": item.get("image_default_model"),
                "auth_type": item.get("auth_type", "api_key"),
                "credential_label": item.get("credential_label", ""),
                "capabilities": item.get("capabilities", ""),
                "api_free_note": item.get("api_free_note", ""),
                "verification_status": item.get("verification_status", "unknown"),
                "verification_label": item.get("verification_label", "状态未知"),
                "free_bucket": item.get("free_bucket", ""),
                "billing_mode": item.get("billing_mode", ""),
            }
            for item in engine.video.public()
            if item.get("images")
        )
        return {"data": data, "paired": bool(engine.bridge.token)}

    @app.put("/api/providers/{provider_id}")
    async def save_provider(provider_id: str, body: WebsiteProvider, request: Request):
        engine = get_service(request)
        if provider_id != body.id:
            raise GatewayError("provider_id_mismatch", "平台 ID 不匹配。", 422)
        if engine.active_id or not engine.queue.empty():
            raise GatewayError("busy", "请等待队列空闲后修改平台。", 409)
        return engine.providers.save(body).model_dump()

    @app.post("/api/bridge/pair")
    async def pair(request: Request):
        return {"token": get_service(request).bridge.pair()}

    def authenticated(request, authorization):
        engine = get_service(request)
        engine.bridge.authenticate(authorization)
        return engine

    @app.get("/bridge/v1/providers")
    async def bridge_providers(
        request: Request, authorization: str = Header(default="")
    ):
        engine = authenticated(request, authorization)
        return {
            "data": [
                item.model_dump()
                for item in engine.providers.items.values()
                if item.enabled
            ]
        }

    @app.post("/bridge/v1/heartbeat")
    async def heartbeat(
        body: Heartbeat, request: Request, authorization: str = Header(default="")
    ):
        authenticated(request, authorization).bridge.heartbeat(body)
        return {"ok": True}

    @app.post("/bridge/v1/claim")
    async def claim(
        body: Claim, request: Request, authorization: str = Header(default="")
    ):
        return {"task": authenticated(request, authorization).bridge.claim(body)}

    @app.post("/bridge/v1/tasks/{ticket_id}/result")
    async def result(
        ticket_id: str,
        body: BridgeResult,
        request: Request,
        authorization: str = Header(default=""),
    ):
        authenticated(request, authorization).bridge.resolve(ticket_id, body)
        return {"ok": True}

    @app.post("/bridge/v1/tasks/{ticket_id}/references/{index}")
    async def reference(
        ticket_id: str,
        index: int,
        body: Lease,
        request: Request,
        authorization: str = Header(default=""),
    ):
        ticket = authenticated(request, authorization).bridge.leased(ticket_id, body)
        if not 0 <= index < len(ticket["paths"]):
            raise GatewayError("reference_not_found", "参考图不存在。", 404)
        return FileResponse(ticket["paths"][index])

    @app.get("/extension", include_in_schema=False)
    async def extension():
        path = Path(__file__).resolve().parent / "browser-extension.zip"
        if not path.is_file():
            raise GatewayError(
                "extension_not_found", "请从项目 browser-extension 目录加载扩展。", 404
            )
        return FileResponse(
            path, filename="web2api-browser-extension.zip", media_type="application/zip"
        )
