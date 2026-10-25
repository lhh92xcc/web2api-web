"""Local-only HTTP gateway and control panel (AGPL-3.0-or-later)."""

import base64
from contextlib import asynccontextmanager
from typing import Annotated
from urllib.parse import urlsplit

from access import KeyStore
from bridge import MAX_RESULT_BYTES, register_bridge
from doubao import DOUBAO_ID, DOUBAO_IMAGE_MODELS
from dotenv import load_dotenv
from engine import (
    MAX_PARALLEL_WORKERS,
    PROJECT,
    GatewayError,
    JobService,
    Settings,
    image_mime,
    video_mime,
)
from imagefx import IMAGEFX_ID, ImageFxCredentialRequest
from leonardo import LEONARDO_ID, LeonardoCredentialRequest
from recraft import RECRAFT_ID, RecraftCredentialRequest
from seaart import SEAART_ID, SeaArtBackend
from whisk import WHISK_ID, WhiskCredentialRequest
from fastapi import Depends, FastAPI, File, Header, HTTPException, Request, UploadFile
from fastapi.exceptions import RequestValidationError
from fastapi.responses import FileResponse, JSONResponse
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from fastapi.staticfiles import StaticFiles
from limits import MAX_REFERENCE_TOTAL_BYTES
from PIL import Image
from references import MAX_REFERENCE_BYTES, MAX_REFERENCES
from schemas import (
    AccountRequest,
    BatchRequest,
    ChatRequest,
    CredentialsRequest,
    ImageRequest,
    KeyRequest,
    VideoBatchRequest,
    VideoRequest,
)
from seedream import SeedreamKeyRequest
from video import (
    POLLINATIONS_ID,
    PUTER_ID,
    VideoKeyRequest,
    backend_for as video_backend_for,
)
from video import TONGYI_IMAGE_MODELS
from web_media import BingCookieRequest
from starlette.middleware.trustedhost import TrustedHostMiddleware

load_dotenv(PROJECT / ".env")


class UploadTooLarge(HTTPException):
    def __init__(self, bridge=False):
        self.code = "result_too_large" if bridge else "reference_too_large"
        super().__init__(
            413, "网页结果大小超出限制。" if bridge else "参考图上传总大小超出限制。"
        )


class UploadBodyLimit:
    """Bound streamed multipart bodies before the form parser spools them."""

    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        path = scope.get("path", "")
        bridge_result = path.startswith("/bridge/v1/tasks/") and path.endswith(
            "/result"
        )
        if scope["type"] != "http" or (
            not bridge_result
            and path
            not in {
                "/api/references",
                "/v1/references",
            }
        ):
            return await self.app(scope, receive, send)
        total = 0
        limit = (
            MAX_RESULT_BYTES
            if bridge_result
            else MAX_REFERENCE_TOTAL_BYTES + 1024 * 1024
        )

        async def bounded_receive():
            nonlocal total
            message = await receive()
            total += len(message.get("body", b""))
            if total > limit:
                raise UploadTooLarge(bridge_result)
            return message

        await self.app(scope, bounded_receive, send)


def create_app(settings: Settings | None = None, backend_factory=None) -> FastAPI:
    @asynccontextmanager
    async def lifespan(app):
        kwargs = {"backend_factory": backend_factory} if backend_factory else {}
        app.state.service = JobService(
            settings or Settings.from_environment(), **kwargs
        )
        app.state.keys = KeyStore(app.state.service.settings.state_dir)
        app.state.service.start()
        try:
            yield
        finally:
            await app.state.service.close()

    app = FastAPI(
        title="Web2API Studio",
        version="3.4.1",
        description="Gemini 双账号与网页免费额度优先、Seedream 付费额度兜底的本地图像生成网关。",
        lifespan=lifespan,
    )
    app.add_middleware(
        TrustedHostMiddleware,
        allowed_hosts=["localhost", "127.0.0.1", "[::1]", "testserver"],
    )
    app.add_middleware(UploadBodyLimit)

    @app.middleware("http")
    async def local_browser_guard(request: Request, call_next):
        origin = request.headers.get("origin")
        project_api = request.url.path.startswith("/v1/")
        bridge_api = request.url.path.startswith("/bridge/v1/")
        allow_project_origin = False
        allow_bridge_origin = False
        if origin:
            try:
                parsed = urlsplit(origin)
                allow_project_origin = (
                    project_api
                    and parsed.scheme == "http"
                    and parsed.hostname in {"127.0.0.1", "localhost", "::1"}
                )
                allow_bridge_origin = (
                    bridge_api
                    and parsed.scheme in {"chrome-extension", "moz-extension"}
                    and bool(parsed.hostname)
                )
            except ValueError:
                parsed = None
            if project_api:
                denied = not allow_project_origin
            elif bridge_api:
                denied = not allow_bridge_origin
            else:
                denied = (
                    parsed is None
                    or parsed.netloc != request.headers.get("host")
                    or parsed.scheme != request.url.scheme
                )
            if denied:
                return JSONResponse(
                    {
                        "error": {
                            "code": "origin_denied",
                            "message": "请使用本机项目或服务自身的后台页面。",
                        }
                    },
                    status_code=403,
                )
        if (
            not project_api
            and not bridge_api
            and request.method not in {"GET", "HEAD", "OPTIONS"}
            and request.headers.get("sec-fetch-site") == "cross-site"
        ):
            return JSONResponse(
                {"error": {"code": "origin_denied", "message": "跨站请求已拒绝。"}},
                status_code=403,
            )
        if (
            (project_api or bridge_api)
            and request.method == "OPTIONS"
            and (allow_project_origin or allow_bridge_origin)
        ):
            return JSONResponse(
                {},
                status_code=200,
                headers={
                    "Access-Control-Allow-Origin": origin,
                    "Access-Control-Allow-Methods": "GET, POST, OPTIONS",
                    "Access-Control-Allow-Headers": "Authorization, Content-Type, Idempotency-Key",
                    "Access-Control-Max-Age": "600",
                    "Vary": "Origin",
                },
            )
        response = await call_next(request)
        if allow_project_origin or allow_bridge_origin:
            response.headers["Access-Control-Allow-Origin"] = origin
            response.headers["Vary"] = "Origin"
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["Referrer-Policy"] = "no-referrer"
        response.headers["X-Frame-Options"] = "DENY"
        response.headers["Cache-Control"] = "no-store"
        return response

    @app.exception_handler(GatewayError)
    async def gateway_error_handler(request, exc):
        return JSONResponse(
            {
                "error": {
                    "code": exc.code,
                    "message": exc.message,
                    "type": "web2api_error",
                }
            },
            status_code=exc.status,
        )

    @app.exception_handler(UploadTooLarge)
    async def upload_too_large(request, exc):
        return JSONResponse(
            {"error": {"code": exc.code, "message": exc.detail}},
            status_code=413,
        )

    @app.exception_handler(RequestValidationError)
    async def validation_error_handler(request, exc):
        # FastAPI normally includes rejected input values, including secrets.
        fields = [".".join(map(str, err["loc"][1:])) or "body" for err in exc.errors()]
        message = "请检查参数：" + "、".join(fields)
        raw_hint = "raw 模式原样传递提示词，请清空 negative_prompt、将 size 设为 auto 并移除 aspect_ratio。"
        if any(err["msg"] == "Value error, " + raw_hint for err in exc.errors()):
            message = raw_hint
        elif any(err["loc"][-1] == "size" for err in exc.errors()):
            message += "。size 支持 auto、1K / 2K / 4K 或 64–8192 的宽x高。"
        return JSONResponse(
            {
                "error": {
                    "code": "invalid_request",
                    "message": message,
                    "type": "validation_error",
                }
            },
            status_code=422,
        )

    def service(request: Request) -> JobService:
        return request.app.state.service

    register_bridge(app, service)

    def public_job(job: dict) -> dict:
        private = {
            "idempotency_key",
            "batch_idempotency_key",
            "batch_digest",
            "owner_id",
        }
        return {key: value for key, value in job.items() if key not in private}

    bearer = HTTPBearer(auto_error=False, scheme_name="Project API Key")

    async def require_key(
        request: Request,
        credentials: Annotated[HTTPAuthorizationCredentials | None, Depends(bearer)],
    ) -> str:
        if credentials is None:
            raise GatewayError(
                "api_key_required", "请携带 Authorization: Bearer <调用 Key>。", 401
            )
        return request.app.state.keys.authenticate(credentials.credentials)

    def owned_job(job_id: str, request: Request, owner_id: str) -> dict:
        job = service(request).get(job_id)
        if job.get("owner_id") != owner_id:
            raise GatewayError("job_not_found", "任务不存在。", 404)
        return job

    def image_data(job: dict, request: Request) -> list[dict]:
        engine = service(request)
        items = []
        for relative in job["images"]:
            path = engine.settings.images_dir / relative
            item = {"mime_type": image_mime(path)}
            try:
                with Image.open(path) as image:
                    item.update(width=image.width, height=image.height)
            except (OSError, ValueError):
                pass
            if job["request"]["response_format"] == "b64_json":
                item["b64_json"] = base64.b64encode(path.read_bytes()).decode()
            else:
                item["url"] = str(request.base_url).rstrip("/") + "/images/" + relative
            items.append(item)
        return items

    def video_data(job: dict, request: Request) -> list[dict]:
        engine = service(request)
        items = []
        for relative in job.get("videos", []):
            path = engine.settings.images_dir / relative
            if not path.is_file():
                continue
            items.append(
                {
                    "url": str(request.base_url).rstrip("/") + "/videos/" + relative,
                    "mime_type": video_mime(path) or "video/mp4",
                }
            )
        return items

    def job_data(job: dict, request: Request) -> list[dict]:
        return video_data(job, request) if job.get("kind") == "video" else image_data(job, request)

    async def save_references(files: list[UploadFile], request: Request, owner_id=None):
        try:
            if not 1 <= len(files) <= MAX_REFERENCES:
                raise GatewayError(
                    "reference_count", f"一次可上传 1–{MAX_REFERENCES} 张参考图。", 422
                )
            contents = []
            total_bytes = 0
            for file in files:
                content = await file.read(MAX_REFERENCE_BYTES + 1)
                if len(content) > MAX_REFERENCE_BYTES:
                    raise GatewayError(
                        "reference_too_large",
                        f"每张参考图最大 {MAX_REFERENCE_BYTES // (1024 * 1024)} MB。",
                        413,
                    )
                total_bytes += len(content)
                if total_bytes > MAX_REFERENCE_TOTAL_BYTES:
                    raise GatewayError(
                        "reference_too_large",
                        f"本次参考图总大小不能超过 {MAX_REFERENCE_TOTAL_BYTES // (1024 * 1024)} MB。",
                        413,
                    )
                contents.append((file.filename or "参考图", content))
            return {"data": service(request).references.save(contents, owner_id)}
        finally:
            for file in files:
                await file.close()

    @app.post("/api/references", status_code=201)
    async def admin_upload_references(
        request: Request,
        files: Annotated[
            list[UploadFile],
            File(
                description=f"1–{MAX_REFERENCES} 张 PNG、JPEG、WebP；每张最大 {MAX_REFERENCE_BYTES // (1024 * 1024)} MB，本次合计最大 {MAX_REFERENCE_TOTAL_BYTES // (1024 * 1024)} MB"
            ),
        ],
    ):
        return await save_references(files, request)

    @app.post("/v1/references", status_code=201)
    async def upload_references(
        request: Request,
        files: Annotated[
            list[UploadFile],
            File(
                description=(
                    f"1–{MAX_REFERENCES} 张 PNG、JPEG、WebP；每张最大 "
                    f"{MAX_REFERENCE_BYTES // (1024 * 1024)} MB，本次合计最大 "
                    f"{MAX_REFERENCE_TOTAL_BYTES // (1024 * 1024)} MB"
                )
            ),
        ],
        api_key_id: Annotated[str, Depends(require_key)],
    ):
        return await save_references(files, request, api_key_id)

    @app.get("/api/references/{reference_id}/content", include_in_schema=False)
    async def admin_reference_image(reference_id: str, request: Request):
        store = service(request).references
        item = store.get(reference_id, None, admin=True)
        return FileResponse(store.path(item), media_type=item["mime_type"])

    @app.get("/v1/references/{reference_id}/content")
    async def reference_image(
        reference_id: str,
        request: Request,
        api_key_id: Annotated[str, Depends(require_key)],
    ):
        store = service(request).references
        item = store.get(reference_id, api_key_id)
        return FileResponse(store.path(item), media_type=item["mime_type"])

    def batch_result(
        batch_id: str, request: Request, owner_id=None, admin=False
    ) -> dict:
        jobs = service(request).get_batch(batch_id, owner_id, admin)
        video_batch = all(job.get("kind") == "video" for job in jobs)
        counts = {
            state: sum(job["status"] == state for job in jobs)
            for state in (
                "queued",
                "waiting",
                "running",
                "succeeded",
                "failed",
                "cancelled",
            )
        }
        if counts["queued"] + counts["running"] + counts["waiting"]:
            state = "in_progress"
        elif counts["failed"] == len(jobs):
            state = "failed"
        elif counts["cancelled"] == len(jobs):
            state = "cancelled"
        else:
            state = "completed"
        return {
            "id": batch_id,
            "object": "video.batch" if video_batch else "image.batch",
            "status": state,
            "created_at": min(job["created_at"] for job in jobs),
            "counts": {"total": len(jobs), **counts},
            "items": [
                {
                    "custom_id": job["custom_id"],
                    "job_id": job["id"],
                    "status": job["status"],
                    "error": job["error"],
                    "text": job.get("text"),
                    "data": video_data(job, request)
                    if job.get("kind") == "video"
                    else image_data(job, request),
                    "parameters": job["request"],
                    "effective_prompt": job.get("effective_prompt"),
                }
                for job in jobs
            ],
        }

    @app.get("/", include_in_schema=False)
    async def index():
        return FileResponse(PROJECT / "static" / "index.html")

    @app.get("/health")
    async def health(request: Request):
        return {
            **service(request).status(),
            "active_keys": sum(key["enabled"] for key in request.app.state.keys.keys),
        }

    @app.get("/api/keys")
    async def list_keys(request: Request):
        return {"data": request.app.state.keys.list()}

    @app.post("/api/keys", status_code=201)
    async def create_key(body: KeyRequest, request: Request):
        return request.app.state.keys.create(body.name)

    @app.post("/api/keys/{key_id}/revoke")
    async def revoke_key(key_id: str, request: Request):
        return request.app.state.keys.revoke(key_id)

    @app.post("/api/config")
    async def configure(body: CredentialsRequest, request: Request):
        await service(request).configure(body)
        return {"saved": True, "configured": True}

    @app.get("/api/accounts")
    async def accounts(request: Request):
        pool = service(request).accounts
        return {
            "data": [a.public() for a in pool.items.values()],
            "policy": {
                "parallel_accounts": MAX_PARALLEL_WORKERS,
                "max_concurrency": MAX_PARALLEL_WORKERS,
                "per_account_concurrency": 1,
                "order": "gemini_account_1_then_account_2_then_seedream_fallback",
                "probe_gemini_on_each_request": True,
            },
        }

    @app.get("/api/seedream")
    async def seedream_settings(request: Request):
        return service(request).seedream.public()

    @app.put("/api/seedream")
    async def save_seedream_settings(body: SeedreamKeyRequest, request: Request):
        return service(request).seedream.save(body.api_key)

    @app.delete("/api/seedream")
    async def clear_seedream_settings(request: Request):
        return service(request).seedream.clear()

    @app.get("/api/web-image-providers")
    async def web_image_providers(request: Request):
        engine = service(request)
        return {
            "data": [
                engine.bing.public(),
                engine.imagefx.public(),
                engine.recraft.public(),
                engine.leonardo.public(),
                engine.seaart.public(),
                engine.whisk.public(),
            ]
        }

    @app.put("/api/web-image-providers/{provider}")
    async def save_web_image_provider(
        provider: str, request: Request, body: dict
    ):
        engine = service(request)
        if provider == "bing":
            try:
                value = BingCookieRequest(
                    cookie=body.get("credential", body.get("cookie"))
                ).cookie
            except ValueError as exc:
                raise GatewayError("invalid_bing_cookie", str(exc), 422) from None
            return engine.bing.save(value)
        if provider == IMAGEFX_ID:
            try:
                value = ImageFxCredentialRequest(
                    credential=body.get("credential", body.get("token"))
                ).credential
            except ValueError as exc:
                raise GatewayError("invalid_imagefx_credential", str(exc), 422) from None
            return engine.imagefx.save(value)
        if provider == RECRAFT_ID:
            try:
                value = RecraftCredentialRequest(
                    credential=body.get("credential", body.get("token"))
                ).credential
            except ValueError as exc:
                raise GatewayError("invalid_recraft_credential", str(exc), 422) from None
            return engine.recraft.save(value)
        if provider == LEONARDO_ID:
            try:
                value = LeonardoCredentialRequest(
                    credential=body.get("credential", body.get("token"))
                ).credential
            except ValueError as exc:
                raise GatewayError("invalid_leonardo_credential", str(exc), 422) from None
            return engine.leonardo.save(value)
        if provider == SEAART_ID:
            # SeaArt needs a token plus the account's model_no.  Accept the
            # JSON string used by the local UI, or a JSON object for callers
            # that invoke this local management endpoint directly.
            value = body.get("credential", body)
            return engine.seaart.save(value)
        if provider == WHISK_ID:
            try:
                value = WhiskCredentialRequest(
                    credential=body.get("credential", body.get("token"))
                ).credential
            except ValueError as exc:
                raise GatewayError("invalid_whisk_credential", str(exc), 422) from None
            return engine.whisk.save(value)
        raise GatewayError("provider_not_found", "没有此网页图片平台。", 404)

    @app.delete("/api/web-image-providers/{provider}")
    async def clear_web_image_provider(provider: str, request: Request):
        engine = service(request)
        if provider == "bing":
            return engine.bing.clear()
        if provider == IMAGEFX_ID:
            return engine.imagefx.clear()
        if provider == RECRAFT_ID:
            return engine.recraft.clear()
        if provider == LEONARDO_ID:
            return engine.leonardo.clear()
        if provider == SEAART_ID:
            return engine.seaart.clear()
        if provider == WHISK_ID:
            return engine.whisk.clear()
        raise GatewayError("provider_not_found", "没有此网页图片平台。", 404)

    @app.get("/api/web-image-providers/{provider}/credits")
    async def web_image_provider_credits(provider: str, request: Request):
        engine = service(request)
        if provider != SEAART_ID:
            raise GatewayError("credits_unsupported", "此网页图片平台没有额度查询接口。", 404)
        return await SeaArtBackend(engine.seaart.config(), engine.settings.timeout).credits()

    @app.get("/api/video-providers")
    async def video_providers(request: Request):
        return {"data": service(request).video.public()}

    @app.put("/api/video-providers/{provider}")
    async def save_video_provider(
        provider: str, body: VideoKeyRequest, request: Request
    ):
        return service(request).video.save(
            provider,
            body.secret(),
            body.base_url,
            body.auth_type,
        )

    @app.delete("/api/video-providers/{provider}")
    async def clear_video_provider(provider: str, request: Request):
        return service(request).video.clear(provider)

    @app.get("/api/video-providers/{provider}/credits")
    async def video_provider_credits(provider: str, request: Request):
        config = service(request).video.get(provider)
        if not config.get("supports_credits"):
            raise GatewayError(
                "credits_unsupported", "此视频平台没有网页 credits 查询接口。", 404
            )
        backend = video_backend_for(
            service(request).video, provider, service(request).settings.timeout
        )
        return await backend.credits()

    @app.post("/api/accounts", status_code=201)
    async def add_account(body: AccountRequest, request: Request):
        return (await service(request).accounts.configure(body)).public()

    @app.put("/api/accounts/{account_id}")
    async def update_account(account_id: str, body: AccountRequest, request: Request):
        return (await service(request).accounts.configure(body, account_id)).public()

    @app.post("/api/accounts/{account_id}/connect")
    async def connect_account(account_id: str, request: Request):
        models = await service(request).accounts.connect_account(account_id)
        return {"connected": True, "models": models}

    @app.post("/api/accounts/{account_id}/retry")
    async def reset_account(account_id: str, request: Request):
        return await service(request).accounts.reset(account_id)

    @app.post("/api/connect")
    async def connect(request: Request):
        models = await service(request).connect()
        return {"connected": True, "models": models}

    async def provider_models(request: Request, provider: str):
        engine = service(request)
        if provider == "gemini":
            return await engine.connect()
        if provider == "jimeng-web":
            info = next(
                (item for item in engine.video.public() if item["id"] == provider),
                None,
            )
            if info is None:
                raise GatewayError("provider_not_found", "没有此图片平台。", 404)
            if not info["configured"]:
                raise GatewayError(
                    "provider_not_configured", "请先在账号页填写即梦 sessionid。", 409
                )
            return [
                {
                    "id": model_id,
                    "object": "model",
                    "created": 0,
                    "owned_by": "jimeng-web",
                    "display_name": model_id,
                    "available": True,
                }
                for model_id in info.get("image_models", [])
            ]
        if provider in {"tongyi-web", "hailuo-web", DOUBAO_ID}:
            info = next(
                (item for item in engine.video.public() if item["id"] == provider),
                None,
            )
            if info is None:
                raise GatewayError("provider_not_found", "没有此图片平台。", 404)
            if not info["configured"]:
                raise GatewayError(
                    "provider_not_configured",
                    "请先在账号页填写通义万相 Cookie Header。"
                    if provider == "tongyi-web"
                    else "请先在账号页填写豆包 Cookie + device_id/web_id JSON。"
                    if provider == DOUBAO_ID
                    else "请先在账号页填写 Hailuo 网页 Token。",
                    409,
                )
            return [
                {
                    "id": model_id,
                    "object": "model",
                    "created": 0,
                    "owned_by": provider,
                    "display_name": (
                        TONGYI_IMAGE_MODELS[model_id]["name"]
                        if provider == "tongyi-web"
                        else DOUBAO_IMAGE_MODELS.get(model_id, model_id)
                        if provider == DOUBAO_ID
                        else model_id
                    ),
                    "available": True,
                }
                for model_id in info.get("image_models", [])
            ]
        if provider in {PUTER_ID, POLLINATIONS_ID}:
            info = next(
                (item for item in engine.video.public() if item["id"] == provider),
                None,
            )
            if info is None:
                raise GatewayError("provider_not_found", "没有此图片平台。", 404)
            if not info["configured"]:
                raise GatewayError(
                    "provider_not_configured",
                    "Pollinations 公共入口无需凭据，请刷新页面后重试。"
                    if provider == POLLINATIONS_ID
                    else "请先在账号页填写 Puter auth_token_v2。",
                    409,
                )
            return [
                {
                    "id": model_id,
                    "object": "model",
                    "created": 0,
                    "owned_by": provider,
                    "display_name": model_id,
                    "available": True,
                }
                for model_id in info.get("image_models", [])
            ]
        if provider == "bing":
            info = service(request).bing.public()
            if not info["configured"]:
                raise GatewayError(
                    "provider_not_configured", "请先在账号页填写 Bing _U Cookie。", 409
                )
            return [
                {
                    "id": model_id,
                    "object": "model",
                    "created": 0,
                    "owned_by": "bing-web",
                    "display_name": model_id,
                    "available": True,
                }
                for model_id in info["models"]
            ]
        if provider == SEAART_ID:
            info = service(request).seaart.public()
            if not info["configured"]:
                raise GatewayError(
                    "provider_not_configured",
                    "请先在网页额度与平台连接中填写 SeaArt Token + model_no JSON。",
                    409,
                )
            return [
                {
                    "id": model_id,
                    "object": "model",
                    "created": 0,
                    "owned_by": SEAART_ID,
                    "display_name": model_id,
                    "available": True,
                }
                for model_id in info["models"]
            ]
        if provider == IMAGEFX_ID:
            info = service(request).imagefx.public()
            if not info["configured"]:
                raise GatewayError(
                    "provider_not_configured",
                    "请先在账号页填写 Google Labs Cookie 或 Bearer Token。",
                    409,
                )
            return [
                {
                    "id": model_id,
                    "object": "model",
                    "created": 0,
                    "owned_by": IMAGEFX_ID,
                    "display_name": model_id,
                    "available": True,
                }
                for model_id in info["models"]
            ]
        if provider == WHISK_ID:
            info = service(request).whisk.public()
            if not info["configured"]:
                raise GatewayError(
                    "provider_not_configured",
                    "请先在网页额度与平台连接中填写 Google Labs Whisk Cookie。",
                    409,
                )
            return [
                {
                    "id": model_id,
                    "object": "model",
                    "created": 0,
                    "owned_by": WHISK_ID,
                    "display_name": model_id,
                    "available": True,
                }
                for model_id in info["models"]
            ]
        if provider == RECRAFT_ID:
            info = service(request).recraft.public()
            if not info["configured"]:
                raise GatewayError(
                    "provider_not_configured",
                    "请先在网页额度与平台连接中填写 Recraft Bearer Token。",
                    409,
                )
            return [
                {
                    "id": model_id,
                    "object": "model",
                    "created": 0,
                    "owned_by": RECRAFT_ID,
                    "display_name": model_id,
                    "available": True,
                }
                for model_id in info["models"]
            ]
        if provider == LEONARDO_ID:
            info = service(request).leonardo.public()
            if not info["configured"]:
                raise GatewayError(
                    "provider_not_configured",
                    "请先在网页额度与平台连接中填写 Leonardo Bearer Token。",
                    409,
                )
            return [
                {
                    "id": model_id,
                    "object": "model",
                    "created": 0,
                    "owned_by": LEONARDO_ID,
                    "display_name": model_id,
                    "available": True,
                }
                for model_id in info["models"]
            ]
        # The same discovery endpoint is also useful for video-only
        # providers such as Vidu, Seedance and MiniMax.  Keep the older image
        # branches above intact, then expose their configured video catalogue
        # here instead of falling through to the browser-adapter registry.
        video_info = next(
            (item for item in engine.video.public() if item["id"] == provider),
            None,
        )
        if video_info is not None:
            if not video_info["configured"]:
                credential_label = video_info.get("credential_label") or "平台凭据"
                raise GatewayError(
                    "provider_not_configured",
                    f"请先在账号页填写 {credential_label}。",
                    409,
                )
            return [
                {
                    "id": model_id,
                    "object": "model",
                    "created": 0,
                    "owned_by": provider,
                    "display_name": model_id,
                    "available": True,
                }
                for model_id in video_info.get("models", [])
            ]
        item = engine.providers.get(provider)
        return [
            {
                "id": "default",
                "object": "model",
                "created": 0,
                "owned_by": item.id + "-web",
                "display_name": "专用网页当前模型",
                "available": item.enabled
                and engine.bridge.state(item.id) in {"ready", "busy"},
            }
        ]

    @app.get("/api/models")
    async def admin_models(request: Request, provider: str = "gemini"):
        return {"data": await provider_models(request, provider)}

    @app.get("/v1/models")
    async def models(
        request: Request,
        api_key_id: Annotated[str, Depends(require_key)],
        provider: str = "gemini",
    ):
        return {"object": "list", "data": await provider_models(request, provider)}

    @app.get("/v1/providers")
    async def public_providers(
        request: Request, api_key_id: Annotated[str, Depends(require_key)]
    ):
        engine = service(request)
        return {
            "data": [
                {
                    "id": "gemini",
                    "name": "Gemini",
                    "images": True,
                    "text": True,
                    "references": True,
                },
                *[
                    {
                        key: value
                        for key, value in item.model_dump().items()
                        if key
                        in {"id", "name", "enabled", "images", "text", "references"}
                    }
                    for item in engine.providers.items.values()
                ],
                *[
                    {
                        "id": item["id"],
                        "name": item["name"],
                        "mode": "public" if item.get("auth_type") == "public" else "native",
                        "enabled": item["configured"],
                        "state": "ready" if item["configured"] else "unconfigured",
                        "images": bool(item.get("images")),
                        "text": bool(item.get("text")),
                        "references": bool(item.get("references")),
                        "models": item.get("models", []),
                        "image_models": item.get("image_models", []),
                        "text_models": item.get("text_models", []),
                        "default_model": item.get("image_default_model"),
                        "auth_type": item.get("auth_type", "api_key"),
                        "credential_label": item.get("credential_label", ""),
                        "verification_status": item.get("verification_status", "unknown"),
                        "verification_label": item.get("verification_label", "状态未知"),
                        "free_bucket": item.get("free_bucket", ""),
                        "browser_required": bool(item.get("browser_required", False)),
                        "api_free_note": item.get("free_note", ""),
                        "capabilities": item.get("capabilities", ""),
                    }
                    for item in [
                        engine.bing.public(),
                        engine.imagefx.public(),
                        engine.recraft.public(),
                        engine.leonardo.public(),
                        engine.seaart.public(),
                        engine.whisk.public(),
                    ]
                ],
                *[
                    {
                        "id": item["id"],
                        "name": item["name"],
                        "enabled": item["configured"],
                        "videos": bool(item.get("videos", True)),
                        "references": True,
                        "verification_status": item.get("verification_status", "unknown"),
                        "verification_label": item.get("verification_label", "状态未知"),
                        "free_bucket": item.get("free_bucket", ""),
                        "browser_required": bool(item.get("browser_required", False)),
                        "billing_mode": item.get("billing_mode", ""),
                        "api_free_note": item.get("api_free_note", ""),
                        "video_models": item.get("models", []),
                    }
                    for item in engine.video.public()
                    if not item.get("images")
                ],
                *[
                    {
                        "id": item["id"],
                        "name": item["name"],
                        "mode": "public" if item.get("auth_type") == "public" else "native",
                        "enabled": item["configured"],
                        "state": "ready" if item["configured"] else "unconfigured",
                        "images": bool(item.get("images")),
                        "text": bool(item.get("text")),
                        "references": bool(item.get("references")),
                        "videos": bool(item.get("videos", True)),
                        "models": item.get("image_models", []),
                        "image_models": item.get("image_models", []),
                        "video_models": item.get("models", []),
                        "text_models": item.get("text_models", []),
                        "default_model": item.get("image_default_model"),
                        "auth_type": item.get("auth_type", "api_key"),
                        "credential_label": item.get("credential_label", ""),
                        "verification_status": item.get("verification_status", "unknown"),
                        "verification_label": item.get("verification_label", "状态未知"),
                        "free_bucket": item.get("free_bucket", ""),
                        "browser_required": bool(item.get("browser_required", False)),
                        "billing_mode": item.get("billing_mode", ""),
                        "api_free_note": item.get("api_free_note", ""),
                    }
                    for item in engine.video.public()
                    if item.get("images")
                ],
            ]
        }

    @app.post("/api/requests/preview")
    async def admin_preview(body: ImageRequest, request: Request):
        service(request).validate_provider(body, ready=False)
        service(request).references.paths(body.reference_images, None)
        return body.preview()

    @app.post("/api/video/requests/preview")
    async def admin_video_preview(body: VideoRequest, request: Request):
        service(request).validate_provider(body, ready=False)
        service(request).references.paths(body.reference_images, None)
        return body.preview()

    @app.post("/v1/requests/preview")
    async def preview(
        body: ImageRequest,
        request: Request,
        api_key_id: Annotated[str, Depends(require_key)],
    ):
        service(request).validate_provider(body, ready=False)
        service(request).references.paths(body.reference_images, api_key_id)
        return body.preview()

    @app.post("/api/jobs", status_code=202)
    async def create_job(
        body: ImageRequest,
        request: Request,
        idempotency_key: Annotated[str | None, Header(max_length=200)] = None,
    ):
        job = await service(request).enqueue(body, idempotency_key)
        return public_job(job)

    @app.get("/api/jobs")
    async def list_jobs(request: Request):
        jobs = list(service(request).jobs.values())
        return {"data": [public_job(job) for job in reversed(jobs[-100:])]}

    @app.post("/api/chat/jobs", status_code=202)
    async def admin_chat(
        body: ChatRequest,
        request: Request,
        idempotency_key: Annotated[str | None, Header(max_length=200)] = None,
    ):
        return public_job(await service(request).enqueue(body, idempotency_key))

    @app.post("/api/video/jobs", status_code=202)
    async def admin_video_job(
        body: VideoRequest,
        request: Request,
        idempotency_key: Annotated[str | None, Header(max_length=200)] = None,
    ):
        return public_job(await service(request).enqueue(body, idempotency_key))

    @app.post("/api/video/batches", status_code=202)
    async def admin_video_batch(
        body: VideoBatchRequest,
        request: Request,
        idempotency_key: Annotated[str | None, Header(max_length=200)] = None,
    ):
        batch_id = await service(request).enqueue_video_batch(body, idempotency_key)
        return batch_result(batch_id, request, admin=True)

    @app.get("/api/video/batches/{batch_id}")
    async def admin_get_video_batch(batch_id: str, request: Request):
        return batch_result(batch_id, request, admin=True)

    @app.post("/v1/chat/jobs", status_code=202)
    async def chat_job(
        body: ChatRequest,
        request: Request,
        api_key_id: Annotated[str, Depends(require_key)],
        idempotency_key: Annotated[str | None, Header(max_length=200)] = None,
    ):
        return public_job(
            await service(request).enqueue(body, idempotency_key, api_key_id)
        )

    @app.post("/v1/chat/completions")
    async def chat_completion(
        body: ChatRequest,
        request: Request,
        api_key_id: Annotated[str, Depends(require_key)],
        idempotency_key: Annotated[str | None, Header(max_length=200)] = None,
    ):
        engine = service(request)
        job = await engine.enqueue(body, idempotency_key, api_key_id)
        job = await engine.wait(job["id"])
        if job["status"] != "succeeded":
            error = job["error"] or {
                "code": "cancelled",
                "message": "任务已取消。",
                "status": 409,
            }
            return JSONResponse(
                {"error": {**error, "job_id": job["id"]}}, status_code=error["status"]
            )
        return {
            "id": "chatcmpl_" + job["id"],
            "object": "chat.completion",
            "created": int(job["created_at"]),
            "model": body.model,
            "provider": body.provider,
            "job_id": job["id"],
            "choices": [
                {
                    "index": 0,
                    "message": {"role": "assistant", "content": job["text"]},
                    "finish_reason": "stop",
                }
            ],
        }

    @app.get("/api/jobs/{job_id}")
    async def get_job(job_id: str, request: Request):
        return public_job(service(request).get(job_id))

    @app.post("/api/jobs/{job_id}/cancel")
    async def cancel_job(job_id: str, request: Request):
        return public_job(await service(request).cancel(job_id))

    @app.post("/api/jobs/{job_id}/retry", status_code=202)
    async def admin_retry_job(
        job_id: str,
        request: Request,
        idempotency_key: Annotated[str | None, Header(max_length=200)] = None,
    ):
        return public_job(await service(request).retry(job_id, idempotency_key))

    @app.post("/api/batches", status_code=202)
    async def admin_batch(
        body: BatchRequest,
        request: Request,
        idempotency_key: Annotated[str | None, Header(max_length=200)] = None,
    ):
        batch_id = await service(request).enqueue_batch(body, idempotency_key)
        return batch_result(batch_id, request, admin=True)

    @app.post("/v1/jobs", status_code=202)
    async def public_create_job(
        body: ImageRequest,
        request: Request,
        api_key_id: Annotated[str, Depends(require_key)],
        idempotency_key: Annotated[str | None, Header(max_length=200)] = None,
    ):
        return public_job(
            await service(request).enqueue(body, idempotency_key, api_key_id)
        )

    @app.get("/v1/jobs/{job_id}")
    async def public_get_job(
        job_id: str, request: Request, api_key_id: Annotated[str, Depends(require_key)]
    ):
        job = owned_job(job_id, request, api_key_id)
        return {**public_job(job), "data": job_data(job, request)}

    @app.post("/v1/jobs/{job_id}/cancel")
    async def public_cancel_job(
        job_id: str, request: Request, api_key_id: Annotated[str, Depends(require_key)]
    ):
        owned_job(job_id, request, api_key_id)
        return public_job(await service(request).cancel(job_id))

    @app.post("/v1/jobs/{job_id}/retry", status_code=202)
    async def public_retry_job(
        job_id: str,
        request: Request,
        api_key_id: Annotated[str, Depends(require_key)],
        idempotency_key: Annotated[str | None, Header(max_length=200)] = None,
    ):
        owned_job(job_id, request, api_key_id)
        job = await service(request).retry(job_id, idempotency_key)
        return {**public_job(job), "data": job_data(job, request)}

    @app.post("/v1/batches", status_code=202)
    async def create_batch(
        body: BatchRequest,
        request: Request,
        api_key_id: Annotated[str, Depends(require_key)],
        idempotency_key: Annotated[str | None, Header(max_length=200)] = None,
    ):
        batch_id = await service(request).enqueue_batch(
            body, idempotency_key, api_key_id
        )
        return batch_result(batch_id, request, api_key_id)

    @app.get("/v1/batches/{batch_id}")
    async def get_batch(
        batch_id: str,
        request: Request,
        api_key_id: Annotated[str, Depends(require_key)],
    ):
        return batch_result(batch_id, request, api_key_id)

    @app.post("/v1/images/generations")
    async def generate(
        body: ImageRequest,
        request: Request,
        api_key_id: Annotated[str, Depends(require_key)],
        idempotency_key: Annotated[str | None, Header(max_length=200)] = None,
    ):
        engine = service(request)
        job = await engine.enqueue(body, idempotency_key, api_key_id)
        job = await engine.wait(job["id"])
        if job["status"] != "succeeded":
            failure = job["error"] or {
                "code": "cancelled",
                "message": "任务已取消。",
                "status": 409,
            }
            # Partial files remain available via this job, never trigger a full resubmit.
            return JSONResponse(
                {
                    "error": {**failure, "job_id": job["id"]},
                    "text": job.get("text"),
                    "data": image_data(job, request),
                },
                status_code=failure["status"],
            )
        return {
            "created": int(job["created_at"]),
            "data": image_data(job, request),
            "job_id": job["id"],
            "parameters": job["request"],
            "effective_prompt": job.get("effective_prompt"),
        }

    @app.post("/v1/videos/generations")
    async def generate_video(
        body: VideoRequest,
        request: Request,
        api_key_id: Annotated[str, Depends(require_key)],
        idempotency_key: Annotated[str | None, Header(max_length=200)] = None,
    ):
        engine = service(request)
        job = await engine.enqueue(body, idempotency_key, api_key_id)
        job = await engine.wait(job["id"])
        if job["status"] != "succeeded":
            failure = job["error"] or {
                "code": "cancelled",
                "message": "任务已取消。",
                "status": 409,
            }
            return JSONResponse(
                {"error": {**failure, "job_id": job["id"]}, "data": video_data(job, request)},
                status_code=failure["status"],
            )
        return {
            "created": int(job["created_at"]),
            "data": video_data(job, request),
            "job_id": job["id"],
            "parameters": job["request"],
        }

    @app.post("/v1/video/jobs", status_code=202)
    async def create_video_job(
        body: VideoRequest,
        request: Request,
        api_key_id: Annotated[str, Depends(require_key)],
        idempotency_key: Annotated[str | None, Header(max_length=200)] = None,
    ):
        return public_job(await service(request).enqueue(body, idempotency_key, api_key_id))

    @app.post("/v1/video/batches", status_code=202)
    async def create_video_batch(
        body: VideoBatchRequest,
        request: Request,
        api_key_id: Annotated[str, Depends(require_key)],
        idempotency_key: Annotated[str | None, Header(max_length=200)] = None,
    ):
        batch_id = await service(request).enqueue_video_batch(
            body, idempotency_key, api_key_id
        )
        return batch_result(batch_id, request, api_key_id)

    @app.get("/v1/video/batches/{batch_id}")
    async def get_video_batch(
        batch_id: str,
        request: Request,
        api_key_id: Annotated[str, Depends(require_key)],
    ):
        return batch_result(batch_id, request, api_key_id)

    @app.get("/images/{image_path:path}", include_in_schema=False)
    async def get_image(image_path: str, request: Request):
        root = service(request).settings.images_dir.resolve()
        target = (root / image_path).resolve()
        if not target.is_relative_to(root) or not target.is_file():
            raise GatewayError("image_not_found", "图片不存在。", 404)
        mime = image_mime(target)
        if mime is None:
            raise GatewayError("image_not_found", "图片不存在。", 404)
        return FileResponse(target, media_type=mime)

    @app.get("/videos/{video_path:path}", include_in_schema=False)
    async def get_video(video_path: str, request: Request):
        root = service(request).settings.images_dir.resolve()
        target = (root / video_path).resolve()
        if not target.is_relative_to(root) or not target.is_file():
            raise GatewayError("video_not_found", "视频不存在。", 404)
        mime = video_mime(target)
        if mime is None:
            raise GatewayError("video_not_found", "视频不存在。", 404)
        return FileResponse(target, media_type=mime)

    @app.get("/source", include_in_schema=False)
    async def source():
        archive = PROJECT.parent / "gemini-web2api-source.zip"
        if not archive.exists():
            raise GatewayError("source_not_found", "源代码位于本地项目目录。", 404)
        return FileResponse(
            archive, filename=archive.name, media_type="application/zip"
        )

    app.mount("/static", StaticFiles(directory=PROJECT / "static"), name="static")
    return app


app = create_app()
