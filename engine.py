"""Persistent account-pool jobs backed by gemini-webapi (AGPL-3.0-or-later)."""

import asyncio
import contextlib
import hashlib
import json
import logging
import os
import re
import time
import uuid
from dataclasses import dataclass
from pathlib import Path

from accounts import AccountPool
from bridge import BrowserBridge
from doubao import (
    DOUBAO_ID,
    DOUBAO_IMAGE_MODELS,
    DOUBAO_TEXT_MODELS,
    DOUBAO_VIDEO_MODELS,
)
from imagefx import IMAGEFX_ID, IMAGEFX_MODELS, ImageFxBackend, ImageFxConfigStore
from jimeng import IMAGE_MODELS, VIDEO_MODELS
from leonardo import (
    LEONARDO_ID,
    LEONARDO_MODELS,
    LeonardoBackend,
    LeonardoConfigStore,
)
from limits import (
    MAX_BATCH_PROMPTS,
    MAX_CHAT_MESSAGE_CHARACTERS,
    MAX_CHAT_MESSAGES,
    MAX_CHAT_TOTAL_CHARACTERS,
    MAX_NEGATIVE_PROMPT_CHARACTERS,
    MAX_OUTPUT_IMAGES,
    MAX_PENDING_JOBS,
    MAX_PROMPT_CHARACTERS,
    MAX_REFERENCE_BYTES,
    MAX_REFERENCE_IMAGES,
    MAX_REFERENCE_PIXELS,
    MAX_REFERENCE_TOTAL_BYTES,
)
from providers import ProviderStore
from recraft import (
    RECRAFT_ID,
    RECRAFT_MODELS,
    RecraftBackend,
    RecraftConfigStore,
)
from seaart import SEAART_ID, SeaArtBackend, SeaArtConfigStore
from whisk import WHISK_ID, WhiskBackend, WhiskConfigStore, WHISK_MODELS
from references import ReferenceStore
from schemas import (
    BatchRequest,
    ChatRequest,
    CredentialsRequest,
    ImageRequest,
    VideoBatchRequest,
    VideoRequest,
)
from seedream import (
    SEEDREAM_MODEL,
    SEEDREAM_OUTPUT_PRICE_CNY,
    SeedreamBackend,
    SeedreamConfigStore,
)
from storage import GatewayError, atomic_json
from video import (
    POLLINATIONS_ID,
    POLLINATIONS_MODELS,
    PUTER_ID,
    PUTER_IMAGE_MODELS,
    PUTER_VIDEO_MODELS,
    TONGYI_IMAGE_MODELS,
    TONGYI_VIDEO_MODELS,
    VIDU_Q2_IMAGE_MODELS,
    VIDU_Q2_MODELS,
    VIDU_Q2_REFERENCE_MODELS,
    VIDU_Q2_START_END_MODELS,
    VIDU_Q2_TEXT_MODELS,
    VIDU_Q3_IMAGE_MODELS,
    VIDU_Q3_REFERENCE_MODELS,
    VIDU_Q3_START_END_MODELS,
    VIDU_Q3_TEXT_MODELS,
    VIDU_REFERENCE_ONLY_MODELS,
    VIDU_WEB_ID,
    VIDU_WEB_VIDEO_MODELS,
    VideoConfigStore,
    backend_for as video_backend_for,
)
from web_media import BingConfigStore, BingWebBackend

log = logging.getLogger("web2api")
PROJECT = Path(__file__).resolve().parent
TERMINAL = {"succeeded", "failed", "cancelled"}
# The gateway deliberately has two global workers.  AccountPool keeps each
# Gemini account exclusive, so this is also the hard upper bound for Gemini
# and Seedream requests together.
MAX_PARALLEL_WORKERS = 2
_REFUSAL_PATTERNS = (
    re.compile(
        r"^(?:as an? (?:ai|artificial intelligence|language) model|"
        r"作为(?:一个)?(?:人工智能|语言)模型|身为(?:一个)?(?:人工智能|语言)模型)"
        r".{0,180}(?:cannot|can't|can’t|unable to|没办法|无法|不能)"
        r".{0,140}(?:help|assist|提供|帮助|协助)",
        re.IGNORECASE | re.DOTALL,
    ),
    re.compile(
        r"^(?:i(?:'m| am) sorry|i cannot|i can't|i can’t|抱歉|对不起)"
        r".{0,140}(?:help|assist|generate|create|provide|无法|不能|没办法|帮助|生成|提供)",
        re.IGNORECASE | re.DOTALL,
    ),
    re.compile(
        r"^(?:because|due to|由于|因为).{0,100}"
        r"(?:limitations?|capabilities|局限|限制|能力|规则|政策).{0,100}"
        r"(?:i )?(?:cannot|can't|can’t|unable|won't|没法|无法|不能|不可以|办不到)",
        re.IGNORECASE | re.DOTALL,
    ),
    re.compile(
        r"^我只是.{0,80}(?:文本|语言|AI|人工智能).{0,80}"
        r"(?:没法|无法|不能|不可以|办不到)",
        re.DOTALL,
    ),
    re.compile(
        r"(?:超出|超出了).{0,60}(?:我的)?(?:设计用途|设计范围|能力范围|职责范围)",
        re.DOTALL,
    ),
)


def is_generation_refusal(text: str) -> bool:
    """Recognize clear text refusals early while leaving normal image progress alone."""
    normalized = " ".join((text or "").split())
    return len(normalized) >= 14 and any(
        pattern.search(normalized) for pattern in _REFUSAL_PATTERNS
    )


def is_quota_response(text: str) -> bool:
    return bool(
        re.search(
            r"(?:image|图片|图像|生图).{0,100}(?:limit|quota|额度|上限)|(?:limit|quota|额度|上限).{0,100}(?:image|图片|图像|生图)|you(?:'ve| have) reached .{0,50}(?:limit|quota)|you(?:'ve| have) exceeded .{0,50}(?:limit|quota)|(?:daily|free|usage|request) (?:limit|quota) (?:has been )?(?:reached|exceeded)|(?:quota|usage) (?:has been |is )?(?:exceeded|exhausted|reached)|(?:quota|额度|配额).{0,40}(?:reset|重置)|(?:额度|配额).{0,40}(?:用尽|耗尽|达到|已满|超出|上限)|(?:达到|超过|超出).{0,40}(?:使用|请求|生成)?(?:上限|额度|配额|限制)",
            text or "",
            re.IGNORECASE | re.DOTALL,
        )
    )


async def completed_stream_response(client, output):
    """Confirm that streamed text is the completed Gemini turn before stopping."""
    metadata = getattr(output, "metadata", None) or []
    if len(metadata) < 2 or not metadata[0]:
        return None
    try:
        async with asyncio.timeout(4):
            history = await client.read_chat(metadata[0], limit=2)
    except Exception:  # noqa: BLE001 - keep consuming the stream if history is unavailable
        return None
    for turn in getattr(history, "turns", []) or []:
        if getattr(turn, "role", None) != "model":
            continue
        final = getattr(turn, "model_output", None)
        final_metadata = getattr(final, "metadata", None) or []
        if final and len(final_metadata) > 1 and final_metadata[1] == metadata[1]:
            return final
    return None


@dataclass
class Settings:
    state_dir: Path
    images_dir: Path
    timeout: int = 450
    max_pending: int = MAX_PENDING_JOBS
    retry_cooldown: float = 30

    @classmethod
    def from_environment(cls):
        return cls(
            state_dir=Path(os.getenv("WEB2API_STATE_DIR", str(PROJECT / ".local"))),
            images_dir=Path(
                os.getenv("WEB2API_IMAGES_DIR", str(PROJECT / "generated"))
            ),
            timeout=max(30, min(600, int(os.getenv("WEB2API_TIMEOUT", "450")))),
        )


def safe_error(exc: Exception) -> GatewayError:
    """Never send raw upstream exception text (which may contain credentials)."""
    if isinstance(exc, GatewayError):
        return exc
    kind = type(exc).__name__
    if kind == "AuthError":
        return GatewayError(
            "authentication_failed",
            "登录信息已失效，请从已登录的 Gemini 网页更新 Cookie。",
            401,
        )
    if kind == "UsageLimitExceededError":
        return GatewayError(
            "quota_exceeded",
            "Gemini 网页账号额度已耗尽，本次请求会自动尝试下一个账号。",
            429,
        )
    if kind == "TemporarilyBlockedError":
        return GatewayError(
            "temporarily_blocked",
            "Google 暂时限制了请求，请先在网页检查账号状态并稍后再试。",
            429,
        )
    if "Timeout" in kind or isinstance(exc, TimeoutError):
        return GatewayError(
            "upstream_timeout",
            "等待 Gemini 超时。任务可能已在网页完成，请先检查网页记录，再决定是否重新提交。",
            504,
        )
    if kind == "ModelInvalidError":
        return GatewayError(
            "invalid_model",
            "账号当前不支持此模型，请通过 /v1/models 查看可用模型。",
            400,
        )
    if kind == "ImageGenerationError":
        return GatewayError(
            "image_generation_failed",
            "Gemini 未能完成生图。请检查提示词、账号生图权限和网页状态。",
        )
    return GatewayError(
        "upstream_error",
        "Gemini 请求失败。请检查网页登录、网络或代理；任务不会自动重新提交。",
    )


class GeminiBackend:
    def __init__(self, credentials: CredentialsRequest, settings: Settings):
        self.credentials = credentials
        self.settings = settings
        self.client = None
        self.models: list[dict] = []
        self.ready = False
        self._lock = asyncio.Lock()
        self.quota_reset_at = 0

    def remember_quota_reset(self):
        """Use only numeric reset times for exhausted buckets; never parse/log secrets."""
        resets = []

        def visit(value):
            if not isinstance(value, dict):
                return
            usage = value.get("usage_percentage")
            exhausted = (
                value.get("remaining") == 0
                or value.get("is_blocked") is True
                or (isinstance(usage, (int, float)) and usage >= 100)
            )
            reset = value.get("reset_time")
            if (
                exhausted
                and isinstance(reset, (int, float))
                and time.time() < reset < time.time() + 8 * 86400
            ):
                resets.append(reset)
            for item in value.values():
                if isinstance(item, dict):
                    visit(item)

        visit(getattr(self.client, "quotas", {}))
        self.quota_reset_at = min(resets) if resets else 0

    async def connect(self) -> list[dict]:
        async with self._lock:
            if self.ready and self.client is not None:
                return self.models
            cookie_dir = self.settings.state_dir / "cookies"
            cookie_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
            os.environ["GEMINI_COOKIE_PATH"] = str(cookie_dir)

            # No browser-cookie extra is installed; credentials are supplied explicitly.
            from gemini_webapi import GeminiClient
            from gemini_webapi.constants import AccountStatus
            from loguru import logger

            logger.disable("gemini_webapi")
            self.client = GeminiClient(
                self.credentials.secure_1psid,
                self.credentials.secure_1psidts or None,
                proxy=self.credentials.proxy or None,
            )
            try:
                async with asyncio.timeout(90):
                    await self.client.init(
                        timeout=self.settings.timeout,
                        watchdog_timeout=self.settings.timeout,
                        auto_close=False,
                        auto_refresh=True,
                        verbose=False,
                    )
                status = self.client.account_status
                if status != AccountStatus.AVAILABLE:
                    if status == AccountStatus.UNAUTHENTICATED:
                        raise GatewayError(
                            "authentication_failed",
                            "Cookie 未能登录账号，请重新从 Gemini 网页复制。",
                            401,
                        )
                    if status == AccountStatus.LOCATION_REJECTED:
                        raise GatewayError(
                            "location_rejected",
                            "Google 拒绝了当前网络所在地区。请使用可访问 Gemini 的网络，或在连接设置填写本机代理。",
                            403,
                        )
                    raise GatewayError(
                        "account_unavailable",
                        "账号当前无法使用 Gemini，请先在网页完成登录并确认可以生图。",
                        403,
                    )
                self.models = [
                    {
                        "id": model.model_name,
                        "object": "model",
                        "created": 0,
                        "owned_by": "google-web",
                        "display_name": model.display_name,
                        "available": model.is_available,
                    }
                    for model in self.client.list_models() or []
                ]
                self.ready = True
                return self.models
            except BaseException:
                await self.close()
                raise

    async def generate(
        self,
        request: ImageRequest,
        folder: Path,
        remaining: int,
        reference_paths: list[Path] | None = None,
        on_text=None,
    ) -> list[Path]:
        from gemini_webapi.types import GeneratedImage

        await self.connect()
        client = self.client
        selected_model = None
        if request.model != "default":
            try:
                selected_model = client.resolve_model(request.model)
            except ValueError:
                raise GatewayError(
                    "invalid_model",
                    "账号没有此模型，请通过 /v1/models 查看可用模型。",
                    400,
                ) from None
            if not selected_model.is_available:
                raise GatewayError(
                    "invalid_model", "此模型当前不可用，请选择账号可用模型。", 400
                )

        prompt = request.render_prompt(has_references=bool(reference_paths))
        try:
            response_text = ""
            images = []
            last_history_check = 0.0
            last_history_output = None

            async def finish_from_history(output):
                nonlocal response_text, images
                completed = await completed_stream_response(client, output)
                if not completed:
                    return False
                response_text = getattr(completed, "text", "") or response_text
                images = [
                    image
                    for image in getattr(completed, "images", [])
                    if isinstance(image, GeneratedImage)
                ]
                if images:
                    return True
                if on_text and response_text:
                    await on_text(response_text)
                if is_quota_response(response_text):
                    raise GatewayError(
                        "quota_exceeded",
                        "此账号 Gemini 额度暂时用尽，正在尝试其他可用账号。",
                        429,
                        response=response_text,
                    )
                if is_generation_refusal(response_text):
                    raise GatewayError(
                        "generation_refused",
                        "Gemini 拒绝了这次生图请求。",
                        422,
                        response=response_text,
                    )
                raise GatewayError(
                    "no_generated_image",
                    "Gemini 已完成回复，但没有生成图片。",
                    response=response_text or None,
                )

            async with asyncio.timeout(self.settings.timeout + 30):
                # Poll chat history while the stream is quiet so a saved final reply
                # can finish the local job even if Gemini stalls before its last frame.
                stream = client.generate_content_stream(
                    prompt,
                    files=reference_paths or None,
                    model=selected_model,
                    current_retry=0,
                )
                try:
                    stream_iterator = stream.__aiter__()
                    pending_output = None
                    while True:
                        if pending_output is None:
                            pending_output = asyncio.create_task(
                                stream_iterator.__anext__()
                            )
                        metadata = getattr(last_history_output, "metadata", None) or []
                        if len(metadata) >= 2 and metadata[0]:
                            ready, _ = await asyncio.wait({pending_output}, timeout=2)
                            if not ready:
                                last_history_check = time.monotonic()
                                if await finish_from_history(last_history_output):
                                    break
                                continue
                        try:
                            output = await pending_output
                        except StopAsyncIteration:
                            pending_output = None
                            if last_history_output is not None:
                                await finish_from_history(last_history_output)
                            break
                        pending_output = None
                        output_metadata = getattr(output, "metadata", None) or []
                        if len(output_metadata) >= 2 and output_metadata[0]:
                            last_history_output = output
                        response_text = getattr(output, "text", "") or response_text
                        images = [
                            image
                            for image in getattr(output, "images", [])
                            if isinstance(image, GeneratedImage)
                        ]
                        if images:
                            break
                        if on_text and response_text:
                            await on_text(response_text)
                        if is_generation_refusal(response_text):
                            raise GatewayError(
                                "generation_refused",
                                "Gemini 拒绝了这次生图请求。",
                                422,
                                response=response_text,
                            )
                        if (
                            output_metadata
                            and time.monotonic() - last_history_check >= 2
                        ):
                            last_history_check = time.monotonic()
                            if await finish_from_history(output):
                                break
                finally:
                    if pending_output is not None:
                        if not pending_output.done():
                            pending_output.cancel()
                        with contextlib.suppress(asyncio.CancelledError, Exception):
                            await pending_output
                    await stream.aclose()
            if not images:
                if is_quota_response(response_text):
                    raise GatewayError(
                        "quota_exceeded",
                        "此账号 Gemini 额度暂时用尽，正在尝试其他可用账号。",
                        429,
                        response=response_text,
                    )
                raise GatewayError(
                    "no_generated_image",
                    "Gemini 已完成请求但未返回图片。请查看任务详情中的账号尝试；如果网页返回了文字，回复内容会一并显示。",
                    response=response_text or None,
                )
            saved = []
            for image in images[:remaining]:
                # No extension: upstream picks the actual image MIME extension.
                filename = f"image_{uuid.uuid4().hex}"
                for attempt in range(3):
                    try:
                        async with asyncio.timeout(90):
                            result = await image.save(
                                path=str(folder),
                                filename=filename,
                                full_size=request.full_size,
                                verbose=False,
                            )
                        break
                    except Exception:  # noqa: BLE001 - retry the same download, never the prompt
                        if attempt == 2:
                            raise GatewayError(
                                "download_failed",
                                "图片已生成，但下载失败；请到 Gemini 网页下载。未重新生图。",
                            ) from None
                        await asyncio.sleep(attempt + 1)
                saved.append(Path(result))
            return saved
        except BaseException as exc:
            if isinstance(exc, Exception) and safe_error(exc).code == "quota_exceeded":
                self.remember_quota_reset()
            self.ready = False
            await self.close()
            raise

    async def chat(self, request: ChatRequest, reference_paths=None) -> str:
        await self.connect()
        selected_model = None
        if request.model != "default":
            try:
                selected_model = self.client.resolve_model(request.model)
            except ValueError:
                raise GatewayError(
                    "invalid_model", "账号没有此模型，请查看可用模型列表。", 400
                ) from None
            if not selected_model.is_available:
                raise GatewayError("invalid_model", "此模型当前不可用。", 400)
        try:
            async with asyncio.timeout(self.settings.timeout + 30):
                output = await self.client.generate_content(
                    request.render_prompt(),
                    files=reference_paths or None,
                    model=selected_model,
                    current_retry=0,
                )
            text = output.text
            if not text or not text.strip():
                raise GatewayError("no_text", "网页未返回文字回复。")
            if is_quota_response(text):
                self.remember_quota_reset()
                raise GatewayError(
                    "quota_exceeded",
                    "此账号 Gemini 额度暂时用尽，正在尝试其他可用账号。",
                    429,
                    response=text,
                )
            return text
        except BaseException as exc:
            if isinstance(exc, Exception) and safe_error(exc).code == "quota_exceeded":
                self.remember_quota_reset()
            await self.close()
            raise

    async def generate_video(self, request: VideoRequest, folder: Path, reference_paths=None):
        """Generate a video through the Gemini web account's own allowance.

        Gemini web accounts expose video generation only when the account and
        region have the feature enabled.  The account pool owns retries and
        account switching; this method only translates the returned media into
        a local MP4 file.
        """
        from gemini_webapi.types import GeneratedMedia, GeneratedVideo

        await self.connect()
        selected_model = None
        if request.model != "default":
            try:
                selected_model = self.client.resolve_model(request.model)
            except ValueError:
                raise GatewayError(
                    "invalid_model", "账号没有此 Gemini 网页模型，请使用 default 或 /v1/models 中的模型。", 400
                ) from None
            if not selected_model.is_available:
                raise GatewayError("invalid_model", "此 Gemini 网页模型当前不可用。", 400)
        try:
            async with asyncio.timeout(self.settings.timeout + 30):
                output = await self.client.generate_content(
                    request.prompt,
                    files=reference_paths or None,
                    model=selected_model,
                    current_retry=0,
                )
            videos = [
                media
                for media in getattr(output, "videos", [])
                if isinstance(media, (GeneratedVideo, GeneratedMedia))
            ]
            response_text = getattr(output, "text", "") or ""
            if not videos:
                if is_quota_response(response_text):
                    raise GatewayError(
                        "quota_exceeded",
                        "Gemini 网页视频额度已耗尽，正在尝试其他 Gemini 账号。",
                        429,
                        response=response_text,
                    )
                raise GatewayError(
                    "video_not_available",
                    "此 Gemini 网页账号没有可用的视频生成功能，或网页没有返回视频。",
                    403,
                    response=response_text or None,
                )
            media = videos[0]
            filename = f"gemini-{uuid.uuid4().hex}.mp4"
            result = await media.save(
                path=str(folder), filename=filename, verbose=False, download_type="video"
            )
            path = Path((result or {}).get("video", "")).resolve()
            if not path.is_file() or path.stat().st_size <= 0:
                raise GatewayError("video_download_failed", "Gemini 视频已返回，但没有保存到本机。", 502)
            return path
        except BaseException as exc:
            if isinstance(exc, Exception) and safe_error(exc).code == "quota_exceeded":
                self.remember_quota_reset()
            await self.close()
            raise

    async def close(self):
        self.ready = False
        client, self.client = self.client, None
        if client is not None:
            with contextlib.suppress(Exception):
                await client.close()


class JobService:
    def __init__(self, settings: Settings, backend_factory=GeminiBackend):
        self.settings = settings
        settings.state_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
        os.chmod(settings.state_dir, 0o700)
        settings.images_dir.mkdir(parents=True, exist_ok=True)
        self.references = ReferenceStore(settings.state_dir)
        self.providers = ProviderStore(settings.state_dir)
        self.seedream = SeedreamConfigStore(settings.state_dir)
        self.video = VideoConfigStore(settings.state_dir)
        self.bing = BingConfigStore(settings.state_dir)
        self.imagefx = ImageFxConfigStore(settings.state_dir)
        self.recraft = RecraftConfigStore(settings.state_dir)
        self.leonardo = LeonardoConfigStore(settings.state_dir)
        self.seaart = SeaArtConfigStore(settings.state_dir)
        self.whisk = WhiskConfigStore(settings.state_dir)
        self.bridge = BrowserBridge(
            settings.state_dir, self.providers, settings.timeout
        )
        self.credentials_path = settings.state_dir / "credentials.json"
        self.history_path = settings.state_dir / "jobs.json"
        self.backend_factory = backend_factory
        self.credentials = self._load_credentials()
        self.accounts = AccountPool(settings, backend_factory, self.credentials)
        self.backend = None
        self.jobs: dict[str, dict] = {}
        self.events: dict[str, asyncio.Event] = {}
        self.queue: asyncio.Queue[str] = asyncio.Queue(maxsize=settings.max_pending)
        self.worker: asyncio.Task | None = None
        self.workers: list[asyncio.Task] = []
        self.active_ids: set[str] = set()
        self._browser_locks: dict[str, asyncio.Lock] = {}
        self.connecting = False
        self.connection_error = None
        self._control = asyncio.Lock()
        self.resume_ids = []
        self._load_jobs()

    def _load_credentials(self) -> CredentialsRequest | None:
        if self.credentials_path.exists():
            try:
                return CredentialsRequest.model_validate_json(
                    self.credentials_path.read_text()
                )
            except (ValueError, OSError):
                log.warning(
                    "Local credentials could not be loaded; configure them in the local UI."
                )
        value = os.getenv("GEMINI_SECURE_1PSID", "").strip()
        if not value:
            return None
        try:
            return CredentialsRequest(
                secure_1psid=value,
                secure_1psidts=os.getenv("GEMINI_SECURE_1PSIDTS", ""),
                proxy=os.getenv("GEMINI_PROXY", ""),
            )
        except ValueError:
            log.warning(
                "Environment credentials are invalid; configure them in the local UI."
            )
            return None

    def _load_jobs(self):
        if not self.history_path.exists():
            return
        try:
            history = json.loads(self.history_path.read_text())
            for job in history:
                job.setdefault("videos", [])
                self._restore_seedream_billing(job)
                if job["status"] in {"queued", "waiting"} and not any(
                    a.get("status") == "running" for a in job.get("attempts", [])
                ):
                    self.resume_ids.append(job["id"])
                    self.events[job["id"]] = asyncio.Event()
                elif job["status"] not in TERMINAL:
                    job["status"] = "failed"
                    job["error"] = GatewayError(
                        "service_restarted",
                        "服务已重启；为避免重复消耗额度，未自动重新提交。请先检查网页记录。",
                        503,
                    ).as_dict()
                    job["finished_at"] = time.time()
                self.jobs[job["id"]] = job
            self._persist()
        except (ValueError, KeyError, TypeError, OSError):
            log.warning("Job history could not be loaded.")

    @staticmethod
    def _new_seedream_billing(generated_images=0, estimate_source="ark_usage"):
        return {
            "provider": "seedream",
            "model": SEEDREAM_MODEL,
            "currency": "CNY",
            "unit_price_cny": SEEDREAM_OUTPUT_PRICE_CNY,
            "generated_images": generated_images,
            "estimated_amount_cny": (
                round(generated_images * SEEDREAM_OUTPUT_PRICE_CNY, 2)
                if generated_images is not None
                else None
            ),
            "estimate_source": estimate_source,
        }

    def _restore_seedream_billing(self, job):
        """Estimate older records from saved Seedream filenames when possible."""
        if job.get("fallback_provider") != "seedream" or job.get("billing"):
            return
        generated_images = sum(
            Path(relative).name.startswith("seedream-")
            for relative in job.get("images", [])
        )
        if generated_images:
            job["billing"] = self._new_seedream_billing(
                generated_images, "legacy_saved_outputs"
            )
        else:
            job["billing"] = self._new_seedream_billing(
                None, "legacy_usage_unavailable"
            )

    def _persist(self):
        atomic_json(self.history_path, list(self.jobs.values()))

    def start(self):
        for job_id in self.resume_ids:
            self.queue.put_nowait(job_id)
        self.workers = [
            asyncio.create_task(self._worker(), name=f"image-worker-{i}")
            for i in range(MAX_PARALLEL_WORKERS)
        ]
        self.worker = self.workers[0]

    @property
    def active_id(self):
        return next(iter(self.active_ids), None)

    def pending_count(self):
        return sum(j["status"] in {"queued", "waiting"} for j in self.jobs.values())

    def status(self) -> dict:
        return {
            "status": "ok",
            "configured": any(a.enabled for a in self.accounts.items.values()),
            "connected": any(
                a.enabled and a.backend and a.backend.ready
                for a in self.accounts.items.values()
            ),
            "connecting": any(a.connecting for a in self.accounts.items.values()),
            "busy": self.active_id is not None,
            "active_job": self.active_id,
            "active_jobs": list(self.active_ids),
            "running_count": sum(j["status"] == "running" for j in self.jobs.values()),
            "waiting_count": sum(j["status"] == "waiting" for j in self.jobs.values()),
            "account_count": sum(a.enabled for a in self.accounts.items.values()),
            "queue_size": self.pending_count(),
            "max_concurrency": MAX_PARALLEL_WORKERS,
            "routing": {
                "gemini_first": True,
                "gemini_probe_each_request": True,
                "seedream_fallback_only_after_all_gemini_quota": True,
            },
            "generation_timeout_seconds": self.settings.timeout + 30,
            "limits": {
                "prompt_characters": MAX_PROMPT_CHARACTERS,
                "negative_prompt_characters": MAX_NEGATIVE_PROMPT_CHARACTERS,
                "reference_images": MAX_REFERENCE_IMAGES,
                "reference_image_bytes": MAX_REFERENCE_BYTES,
                "reference_total_bytes": MAX_REFERENCE_TOTAL_BYTES,
                "reference_image_pixels": MAX_REFERENCE_PIXELS,
                "output_images": MAX_OUTPUT_IMAGES,
                "batch_prompts": MAX_BATCH_PROMPTS,
                "pending_jobs": self.settings.max_pending,
                "chat_message_characters": MAX_CHAT_MESSAGE_CHARACTERS,
                "chat_total_characters": MAX_CHAT_TOTAL_CHARACTERS,
                "chat_messages": MAX_CHAT_MESSAGES,
            },
            "billing": "gemini_web_quota_with_seedream_fallback",
            "library_version": "2.1.1",
            "web_baseline": {
                "id": "gemini",
                "verification_status": "verified_web",
                "verification_label": "本机已接入 · 每次请求重新探测额度",
                "free_bucket": "Gemini Apps daily quota",
                "browser_required": False,
            },
            "seedream": self.seedream.public(),
            "web_providers": [
                self.bing.public(),
                self.imagefx.public(),
                self.recraft.public(),
                self.leonardo.public(),
                self.seaart.public(),
                self.whisk.public(),
            ],
            "video_providers": [
                {
                    "id": item["id"],
                    "name": item["name"],
                    "configured": (
                        any(a.enabled for a in self.accounts.items.values())
                        if item["id"] == "gemini"
                        else self.bing.configured
                        if item["id"] == "bing"
                        else item["configured"]
                    ),
                    "default_model": item["default_model"],
                    "auth_type": item.get("auth_type", "api_key"),
                    "models": item.get("models", []),
                    "image_models": item.get("image_models", []),
                    "text_models": item.get("text_models", []),
                    "image_default_model": item.get("image_default_model"),
                    "images": item.get("images", False),
                    "text": item.get("text", False),
                    "references": item.get("references", True),
                    "mode": "public"
                    if item.get("auth_type") == "public"
                    else "native"
                    if item.get("auth_type") in {"web_session", "web_cookie", "web_token"}
                    else "api",
                    "credential_label": item.get("credential_label", ""),
                    "verification_status": item.get("verification_status", "unknown"),
                    "verification_label": item.get("verification_label", "状态未知"),
                    "free_bucket": item.get("free_bucket", ""),
                    "browser_required": bool(item.get("browser_required", False)),
                    "fit": item.get("fit", ""),
                    "capabilities": item.get("capabilities", ""),
                    "api_free_note": item.get("api_free_note", ""),
                    "docs_url": item.get("docs_url", item.get("web_url", "")),
                    "base_url": item.get("base_url", item.get("web_url", "")),
                }
                for item in self.video.public()
            ],
            "version": "3.4.1",
            "connection_error": self.connection_error,
        }

    async def generate_with_quota_fallback(
        self, job, request, folder, remaining, reference_paths, on_text=None
    ):
        """Use Seedream only after the Gemini pool confirms quota exhaustion."""
        try:
            return await self.accounts.execute(
                job,
                request,
                lambda backend: backend.generate(
                    request,
                    folder,
                    remaining,
                    reference_paths=reference_paths,
                    on_text=on_text,
                ),
                self._persist,
            )
        except GatewayError as error:
            if error.code != "all_accounts_quota_exhausted":
                raise
            if not self.seedream.configured:
                raise GatewayError(
                    "seedream_not_configured",
                    "两个 Gemini 账号的生图额度都已耗尽；请先在 Gemini 账号页配置 Seedream API Key。",
                    429,
                ) from None

        job["fallback_provider"] = "seedream"
        job["billing"] = self._new_seedream_billing()

        def record_seedream_usage(generated_images):
            billing = job["billing"]
            billing["generated_images"] += generated_images
            billing["estimated_amount_cny"] = round(
                billing["generated_images"] * SEEDREAM_OUTPUT_PRICE_CNY, 2
            )
            try:
                self._persist()
            except OSError:
                log.error("Could not persist Seedream usage for job %s.", job["id"])

        attempt = {
            "provider": "seedream",
            "model": SEEDREAM_MODEL,
            "started_at": time.time(),
            "status": "running",
        }
        job.setdefault("attempts", []).append(attempt)
        self._persist()
        try:
            backend = SeedreamBackend(self.seedream.api_key, self.settings.timeout)
            paths = await backend.generate(
                request,
                folder,
                remaining,
                reference_paths,
                on_billed_images=record_seedream_usage,
            )
        except Exception as exc:  # noqa: BLE001 - redact provider output and key
            error = safe_error(exc)
            partial_paths = getattr(exc, "partial_paths", None)
            if partial_paths:
                error.partial_paths = partial_paths
            attempt.update(
                status="failed", error=error.as_dict(), finished_at=time.time()
            )
            self._persist()
            raise error from None
        attempt.update(status="succeeded", finished_at=time.time())
        self._persist()
        return paths

    async def configure(self, credentials: CredentialsRequest):
        async with self._control:
            if self.active_id or not self.queue.empty() or self.connecting:
                raise GatewayError("busy", "请等待当前任务结束后再修改连接设置。", 409)
            await self.accounts.save("primary", "Gemini 账号 1", credentials)
            atomic_json(self.credentials_path, credentials.model_dump())
            self.credentials = credentials
            self.connection_error = None

    def require_configured(self):
        self.accounts.require_configured()

    async def connect(self) -> list[dict]:
        self.require_configured()
        try:
            models = await self.accounts.connect()
            self.connection_error = None
            return models
        except Exception as exc:  # noqa: BLE001 - redact external connection errors
            self.connection_error = safe_error(exc).as_dict()
            raise safe_error(exc) from None

    def _new_job(
        self,
        request: ImageRequest | ChatRequest | VideoRequest,
        idempotency_key=None,
        owner_id=None,
        **extra,
    ) -> dict:
        kind = (
            "chat"
            if isinstance(request, ChatRequest)
            else "video"
            if isinstance(request, VideoRequest)
            else "image"
        )
        job_id = f"{'chat' if kind == 'chat' else 'vid' if kind == 'video' else 'img'}_{uuid.uuid4().hex}"
        job = {
            "id": job_id,
            "kind": kind,
            "status": "queued",
            "created_at": time.time(),
            "started_at": None,
            "finished_at": None,
            "request": request.model_dump(),
            "effective_prompt": request.render_prompt(),
            "images": [],
            "videos": [],
            "text": None,
            "error": None,
            "idempotency_key": idempotency_key,
            "owner_id": owner_id,
            **extra,
        }
        self.jobs[job_id] = job
        self.events[job_id] = asyncio.Event()
        return job

    def validate_provider(self, request, ready=True):
        if isinstance(request, VideoRequest):
            if request.provider == "gemini":
                if ready:
                    self.require_configured()
                if len(request.reference_images) > MAX_REFERENCE_IMAGES:
                    raise GatewayError(
                        "reference_count",
                        f"Gemini 视频参考图最多 {MAX_REFERENCE_IMAGES} 张。",
                        422,
                    )
                return
            if request.provider == "bing":
                if ready and not self.bing.configured:
                    raise GatewayError(
                        "bing_not_configured",
                        "请先在网页账号页填写 Bing _U Cookie。",
                        409,
                    )
                if request.reference_images:
                    raise GatewayError(
                        "bing_references_unsupported",
                        "Bing 网页视频接口当前不接受本地参考图。",
                        422,
                    )
                if request.model not in {"default", "bing-video"}:
                    raise GatewayError(
                        "invalid_model", "Bing 网页视频只支持 default 模型。", 422
                    )
                return
            if request.provider == "jimeng-web":
                if ready:
                    self.video.get(request.provider)
                if request.model != "default" and request.model not in VIDEO_MODELS:
                    raise GatewayError(
                        "invalid_model", "视频模型不在即梦网页模型列表中。", 422
                    )
                if len(request.reference_images) > 2:
                    raise GatewayError(
                        "reference_count", "即梦网页视频当前最多使用 2 张首尾帧参考图。", 422
                    )
                return
            if request.provider == "tongyi-web":
                if ready:
                    self.video.get(request.provider)
                if request.model != "default" and request.model not in TONGYI_VIDEO_MODELS:
                    raise GatewayError(
                        "invalid_model", "视频模型不在通义万相网页模型列表中。", 422
                    )
                if len(request.reference_images) > 2:
                    raise GatewayError(
                        "reference_count", "通义万相网页视频当前最多使用 2 张首尾帧参考图。", 422
                    )
                return
            if request.provider == DOUBAO_ID:
                if ready:
                    self.video.get(request.provider)
                if request.model != "default" and request.model not in DOUBAO_VIDEO_MODELS:
                    raise GatewayError(
                        "invalid_model", "视频模型不在豆包网页模型列表中。", 422
                    )
                if request.reference_images:
                    raise GatewayError(
                        "doubao_references_require_browser",
                        "豆包网页参考图上传需要官方网页签名；当前直连适配器只支持无参考图视频。",
                        422,
                    )
                return
            if request.provider == VIDU_WEB_ID:
                if ready:
                    self.video.get(request.provider)
                model = request.model if request.model != "default" else "vidu-web-q3"
                if model not in VIDU_WEB_VIDEO_MODELS:
                    raise GatewayError("invalid_model", "视频模型不在 Vidu 网页 Q2/Q3 模型列表中。", 422)
                if len(request.reference_images) > 7:
                    raise GatewayError("reference_count", "Vidu 网页 Q2/Q3 视频最多使用 7 张参考图。", 422)
                if not 1 <= request.duration <= 10:
                    raise GatewayError("invalid_duration", "Vidu 网页 Q2/Q3 视频时长支持 1–10 秒。", 422)
                return
            if request.provider == POLLINATIONS_ID:
                raise GatewayError(
                    "unsupported_capability",
                    "Pollinations 公共入口当前只支持图片生成。",
                    422,
                )
            self.video.get(request.provider)
            if request.model != "default":
                known = next(
                    (item for item in self.video.public() if item["id"] == request.provider),
                    None,
                )
                if known and request.model not in known["models"]:
                    raise GatewayError("invalid_model", "视频模型不在该平台的已知模型列表中。", 422)
            reference_limits = {
                "hailuo-web": 9,
                "pixverse-web": 1,
                "minimax": 9,
                "vidu": 7,
                "bailian": 9,
                "seedance": 9,
                "tongyi-web": 2,
                PUTER_ID: 2,
            }
            maximum = reference_limits.get(request.provider, 9)
            if request.provider == "vidu":
                model = request.model if request.model != "default" else "viduq3-turbo"
                q2_reference = model in VIDU_Q2_REFERENCE_MODELS
                q3_reference = model in VIDU_Q3_REFERENCE_MODELS
                if q2_reference or q3_reference:
                    # Subject/reference endpoints accept up to seven image
                    # references for Q2/Q3 subject models.  The Q2 Turbo,
                    # Q2 Pro Fast, Q3 Pro and Q3 Pro Fast image/start-end
                    # variants remain limited to one or two images.
                    maximum = 7
                else:
                    maximum = 2
            if len(request.reference_images) > maximum:
                raise GatewayError("reference_count", f"{request.provider} 视频参考图最多 {maximum} 张。", 422)
            if request.provider == "minimax":
                model = request.model if request.model != "default" else "MiniMax-H3"
                lower = model.lower()
                minimum, maximum_duration = (5, 15) if "max" in lower else (4, 15)
                if not minimum <= request.duration <= maximum_duration:
                    raise GatewayError(
                        "invalid_duration",
                        f"{model} 视频时长支持 {minimum}–{maximum_duration} 秒。",
                        422,
                    )
            elif request.provider == "vidu":
                model = request.model if request.model != "default" else "viduq3-turbo"
                if model in VIDU_Q2_MODELS:
                    if not 1 <= request.duration <= 10:
                        raise GatewayError("invalid_duration", "Vidu Q2 视频时长支持 1–10 秒。", 422)
                else:
                    # Q3 text/image/start-end calls allow 1–16 seconds;
                    # subject-reference Q3 models start at 3 seconds.  The
                    # advertising model has a 15-second upper bound and the
                    # drama model starts at 2 seconds.
                    minimum = (
                        2
                        if model == "viduq3-drama"
                        else 3
                        if model in VIDU_REFERENCE_ONLY_MODELS
                        or (model in VIDU_Q3_REFERENCE_MODELS and len(request.reference_images) > 2)
                        else 1
                    )
                    maximum_duration = 15 if model == "viduq3-ad" else 16
                    if not minimum <= request.duration <= maximum_duration:
                        raise GatewayError(
                            "invalid_duration",
                            f"Vidu {model} 视频时长支持 {minimum}–{maximum_duration} 秒。",
                            422,
                        )
                if not request.reference_images and model not in (VIDU_Q2_TEXT_MODELS | VIDU_Q3_TEXT_MODELS):
                    raise GatewayError(
                        "vidu_text_unsupported",
                        f"Vidu 模型 {model} 没有文生视频接口，请上传参考图或选择 Q2/Q3 文生模型。",
                        422,
                    )
                if len(request.reference_images) == 1 and model not in (
                    VIDU_Q2_IMAGE_MODELS | VIDU_Q3_IMAGE_MODELS | VIDU_REFERENCE_ONLY_MODELS
                ):
                    raise GatewayError(
                        "vidu_reference_unsupported",
                        f"Vidu 模型 {model} 不支持单图生视频接口。",
                        422,
                    )
                if len(request.reference_images) == 2 and model not in (
                    VIDU_Q2_START_END_MODELS | VIDU_Q3_START_END_MODELS | VIDU_REFERENCE_ONLY_MODELS
                ):
                    raise GatewayError(
                        "vidu_reference_unsupported",
                        f"Vidu 模型 {model} 不支持首尾帧接口。",
                        422,
                    )
                if len(request.reference_images) > 2 and model not in (
                    VIDU_Q2_REFERENCE_MODELS | VIDU_Q3_REFERENCE_MODELS
                ):
                    raise GatewayError(
                        "vidu_reference_unsupported",
                        f"Vidu 模型 {model} 的图生视频最多支持 2 张首尾帧；多主体参考请选择 Q2/Q3 主体模型。",
                        422,
                    )
            elif request.provider == "hailuo-web" and not 4 <= request.duration <= 15:
                raise GatewayError(
                    "invalid_duration",
                    "Hailuo 3.0 / Seedance 2.0 网页视频时长支持 4–15 秒。",
                    422,
                )
            elif request.provider == "pixverse-web" and not 1 <= request.duration <= 15:
                raise GatewayError(
                    "invalid_duration", "PixVerse 网页原生模型视频时长支持 1–15 秒。", 422
                )
            elif request.provider == PUTER_ID and not 1 <= request.duration <= 30:
                raise GatewayError(
                    "invalid_duration", "Puter 视频时长支持 1–30 秒，具体上限由所选模型决定。", 422
                )
            return
        if request.provider == "bing":
            if not isinstance(request, ImageRequest):
                raise GatewayError("unsupported_capability", "Bing 网页连接只支持图片生成。", 422)
            if ready and not self.bing.configured:
                raise GatewayError(
                    "bing_not_configured",
                    "请先在网页额度与平台连接中填写 Bing _U Cookie。",
                    409,
                )
            if request.reference_images:
                raise GatewayError(
                    "bing_references_unsupported",
                    "Bing 网页图片接口当前不接受本地参考图。",
                    422,
                )
            if request.model not in {"default", "bing-dalle3", "bing-gpt4o", "bing-mai-image-1"}:
                raise GatewayError("invalid_model", "Bing 图片模型不在网页模型列表中。", 422)
            return
        if request.provider == IMAGEFX_ID:
            if not isinstance(request, ImageRequest):
                raise GatewayError("unsupported_capability", "ImageFX 网页连接只支持图片生成。", 422)
            if ready and not self.imagefx.configured:
                raise GatewayError(
                    "imagefx_not_configured",
                    "请先在网页额度与平台连接中填写 Google Labs Cookie 或 Bearer Token。",
                    409,
                )
            if request.reference_images:
                raise GatewayError(
                    "imagefx_references_unsupported",
                    "ImageFX 网页接口当前只支持文生图；参考图请使用 Gemini、即梦或 Whisk。",
                    422,
                )
            if request.model != "default" and request.model not in IMAGEFX_MODELS:
                raise GatewayError("invalid_model", "ImageFX 图片模型不在网页模型列表中。", 422)
            return
        if request.provider == RECRAFT_ID:
            if not isinstance(request, ImageRequest):
                raise GatewayError("unsupported_capability", "Recraft 网页连接只支持图片生成。", 422)
            if ready and not self.recraft.configured:
                raise GatewayError(
                    "recraft_not_configured",
                    "请先在网页额度与平台连接中填写 Recraft Bearer Token。",
                    409,
                )
            if request.reference_images:
                raise GatewayError(
                    "recraft_references_unsupported",
                    "Recraft 网页接口当前只支持文生图；参考图请使用 Gemini 或其他支持图生图的平台。",
                    422,
                )
            if request.model != "default" and request.model not in RECRAFT_MODELS:
                raise GatewayError("invalid_model", "Recraft 图片模型不在网页模型列表中。", 422)
            return
        if request.provider == LEONARDO_ID:
            if not isinstance(request, ImageRequest):
                raise GatewayError("unsupported_capability", "Leonardo 网页连接只支持图片生成。", 422)
            if ready and not self.leonardo.configured:
                raise GatewayError(
                    "leonardo_not_configured",
                    "请先在网页额度与平台连接中填写 Leonardo Bearer Token。",
                    409,
                )
            if request.reference_images:
                raise GatewayError(
                    "leonardo_references_unsupported",
                    "Leonardo 网页接口当前只支持文生图；参考图请使用 Gemini、即梦或其他支持图生图的平台。",
                    422,
                )
            if request.model != "default" and request.model not in LEONARDO_MODELS:
                raise GatewayError("invalid_model", "Leonardo 图片模型不在网页模型列表中。", 422)
            return
        if request.provider == SEAART_ID:
            if not isinstance(request, ImageRequest):
                raise GatewayError(
                    "unsupported_capability",
                    "SeaArt 网页连接只支持图片生成。",
                    422,
                )
            if ready and not self.seaart.configured:
                raise GatewayError(
                    "seaart_not_configured",
                    "请先在网页额度与平台连接中填写 SeaArt Token + model_no JSON。",
                    409,
                )
            if request.model != "default":
                raise GatewayError(
                    "invalid_model",
                    "SeaArt 模型 ID 在本机凭据 JSON 的 model_no 字段中配置，请传 model: default。",
                    422,
                )
            if len(request.reference_images) > 1:
                raise GatewayError(
                    "reference_count",
                    "SeaArt 网页图生图当前最多使用 1 张参考图。",
                    422,
                )
            return
        if request.provider == WHISK_ID:
            if not isinstance(request, ImageRequest):
                raise GatewayError(
                    "unsupported_capability",
                    "Whisk 网页连接只支持图片生成。",
                    422,
                )
            if ready and not self.whisk.configured:
                raise GatewayError(
                    "whisk_not_configured",
                    "请先在网页额度与平台连接中填写 Google Labs Whisk Cookie。",
                    409,
                )
            if request.model != "default" and request.model not in WHISK_MODELS:
                raise GatewayError("invalid_model", "Whisk 图片模型不在网页模型列表中。", 422)
            if len(request.reference_images) > 3:
                raise GatewayError(
                    "reference_count",
                    "Whisk 网页图生图当前最多使用 3 张参考图。",
                    422,
                )
            return
        if request.provider == "gemini":
            if ready:
                self.require_configured()
            return
        if request.provider == PUTER_ID:
            if not isinstance(request, ImageRequest):
                raise GatewayError("unsupported_capability", "Puter 当前只支持图片和视频生成，不支持文字对话。", 422)
            if ready:
                self.video.get(request.provider)
            if request.model != "default" and request.model not in PUTER_IMAGE_MODELS:
                raise GatewayError("invalid_model", "Puter 图片模型不在当前支持列表中。", 422)
            if len(request.reference_images) > 10:
                raise GatewayError("reference_count", "Puter 图片当前最多使用 10 张参考图。", 422)
            return
        if request.provider == POLLINATIONS_ID:
            if not isinstance(request, ImageRequest):
                raise GatewayError("unsupported_capability", "Pollinations 公共入口当前只支持图片生成。", 422)
            if request.model != "default" and request.model not in POLLINATIONS_MODELS:
                raise GatewayError("invalid_model", "未认证 Pollinations 公共接口当前只提供 Sana。", 422)
            if request.reference_images:
                raise GatewayError(
                    "pollinations_references_unsupported",
                    "Pollinations 公共 Sana 接口不接收本地参考图。",
                    422,
                )
            return
        if request.provider == "jimeng-web":
            if ready:
                self.video.get(request.provider)
            if request.model != "default" and request.model not in IMAGE_MODELS:
                raise GatewayError(
                    "invalid_model", "图片模型不在即梦网页模型列表中。", 422
                )
            return
        if request.provider == "tongyi-web":
            if ready:
                self.video.get(request.provider)
            if request.model != "default" and request.model not in TONGYI_IMAGE_MODELS:
                raise GatewayError(
                    "invalid_model", "图片模型不在通义万相网页模型列表中。", 422
                )
            if len(request.reference_images) > 9:
                raise GatewayError(
                    "reference_count", "通义万相网页图片当前最多使用 9 张参考图。", 422
                )
            return
        if request.provider == "hailuo-web":
            if ready:
                self.video.get(request.provider)
            known = next(
                (item for item in self.video.public() if item["id"] == request.provider),
                None,
            )
            if request.model != "default" and known and request.model not in known.get("image_models", []):
                raise GatewayError(
                    "invalid_model", "图片模型不在 Hailuo 网页当前模型列表中。", 422
                )
            if len(request.reference_images) > 1:
                raise GatewayError(
                    "reference_count", "Hailuo 网页图片当前最多使用 1 张参考图。", 422
                )
            return
        if request.provider == DOUBAO_ID and isinstance(request, ImageRequest):
            if ready:
                self.video.get(request.provider)
            if request.model != "default" and request.model not in DOUBAO_IMAGE_MODELS:
                raise GatewayError("invalid_model", "图片模型不在豆包网页模型列表中。", 422)
            if request.reference_images:
                raise GatewayError(
                    "doubao_references_require_browser",
                    "豆包网页参考图上传需要官方网页签名；当前直连适配器只支持文生图。",
                    422,
                )
            return
        if isinstance(request, ChatRequest) and request.provider == DOUBAO_ID:
            if ready:
                self.video.get(request.provider)
            if request.model != "default" and request.model not in DOUBAO_TEXT_MODELS:
                raise GatewayError("invalid_model", "文字模型不在豆包网页模型列表中。", 422)
            if request.reference_images:
                raise GatewayError(
                    "doubao_references_require_browser",
                    "豆包网页文字直连暂不支持参考图上传。",
                    422,
                )
            return
        item = self.providers.get(request.provider)
        if not item.enabled:
            raise GatewayError("provider_disabled", "此平台已停用。", 409)
        kind = "text" if isinstance(request, ChatRequest) else "images"
        if not getattr(item, kind) or (
            request.reference_images and not item.references
        ):
            raise GatewayError(
                "unsupported_capability", "此适配器未启用所请求的能力。", 422
            )
        if request.model != "default":
            raise GatewayError(
                "invalid_model",
                "浏览器连接使用专用网页中当前选择的模型，请传 model: default。",
                422,
            )
        if ready:
            self.bridge.require_ready(request.provider)

    async def enqueue(
        self,
        request: ImageRequest | ChatRequest | VideoRequest,
        idempotency_key: str | None = None,
        owner_id: str | None = None,
        retry_of: str | None = None,
    ) -> dict:
        async with self._control:
            if idempotency_key:
                for job in self.jobs.values():
                    if (
                        job.get("idempotency_key") == idempotency_key
                        and job.get("owner_id") == owner_id
                    ):
                        schema = {
                            "chat": ChatRequest,
                            "video": VideoRequest,
                            "image": ImageRequest,
                        }.get(job.get("kind"), ImageRequest)
                        if schema.model_validate(job["request"]) != request:
                            raise GatewayError(
                                "idempotency_conflict",
                                "同一 Idempotency-Key 已用于不同参数。",
                                409,
                            )
                        return job
            self.validate_provider(request)
            self.references.paths(request.reference_images, owner_id)
            if self.queue.full() or self.pending_count() >= self.settings.max_pending:
                raise GatewayError("queue_full", "任务队列已满，请稍后再提交。", 429)
            job = self._new_job(request, idempotency_key, owner_id, retry_of=retry_of)
            job_id = job["id"]
            try:
                self._persist()
            except OSError:
                self.jobs.pop(job_id)
                self.events.pop(job_id)
                raise GatewayError(
                    "storage_error", "任务记录写入失败，请检查磁盘空间与目录权限。", 500
                ) from None
            self.queue.put_nowait(job_id)
            return job

    async def retry(self, job_id: str, idempotency_key: str | None = None) -> dict:
        original = self.get(job_id)
        if original["status"] not in {"failed", "cancelled"}:
            raise GatewayError(
                "job_not_retryable",
                "只有未完成或已取消的任务可以重新提交。",
                409,
            )
        schema = {
            "chat": ChatRequest,
            "video": VideoRequest,
            "image": ImageRequest,
        }.get(original.get("kind"), ImageRequest)
        request = schema.model_validate(original["request"])
        retry_key = f"retry:{job_id}:{idempotency_key}" if idempotency_key else None
        return await self.enqueue(
            request,
            retry_key,
            original.get("owner_id"),
            retry_of=job_id,
        )

    async def enqueue_batch(
        self, request: BatchRequest, idempotency_key=None, owner_id=None
    ) -> str:
        payload = request.model_dump()
        # Keep existing text-only batch idempotency valid after this upgrade.
        for item in payload["items"]:
            if not item["reference_images"]:
                item.pop("reference_images")
            for field, value in {
                "negative_prompt": "",
                "prompt_mode": "enhanced",
                "full_size": True,
                "provider": "gemini",
            }.items():
                if item[field] == value:
                    item.pop(field)
        digest = hashlib.sha256(
            json.dumps(payload, sort_keys=True).encode()
        ).hexdigest()
        async with self._control:
            if idempotency_key:
                for job in self.jobs.values():
                    if (
                        job.get("batch_idempotency_key") == idempotency_key
                        and job.get("owner_id") == owner_id
                    ):
                        if job.get("batch_digest") != digest:
                            raise GatewayError(
                                "idempotency_conflict",
                                "同一 Idempotency-Key 已用于不同的批量参数。",
                                409,
                            )
                        return job["batch_id"]
            for item in request.items:
                self.validate_provider(item)
                self.references.paths(item.reference_images, owner_id)
            if self.pending_count() + len(request.items) > self.settings.max_pending:
                raise GatewayError(
                    "queue_full",
                    "当前队列容量不足，整批任务均未提交；请稍后再试。",
                    429,
                )
            batch_id = "batch_" + uuid.uuid4().hex
            created = []
            for item in request.items:
                image_request = ImageRequest.model_validate(
                    item.model_dump(exclude={"custom_id"})
                )
                job = self._new_job(
                    image_request,
                    owner_id=owner_id,
                    batch_id=batch_id,
                    custom_id=item.custom_id,
                    batch_idempotency_key=idempotency_key,
                    batch_digest=digest,
                )
                created.append(job)
            try:
                self._persist()
            except OSError:
                for job in created:
                    self.jobs.pop(job["id"])
                    self.events.pop(job["id"])
                raise GatewayError(
                    "storage_error", "批次记录未能保存，任务均未提交。", 500
                ) from None
            for job in created:
                self.queue.put_nowait(job["id"])
            return batch_id

    async def enqueue_video_batch(
        self, request: VideoBatchRequest, idempotency_key=None, owner_id=None
    ) -> str:
        """Atomically enqueue provider-neutral video jobs as one batch."""
        payload = request.model_dump()
        digest = hashlib.sha256(
            json.dumps(payload, sort_keys=True).encode()
        ).hexdigest()
        async with self._control:
            if idempotency_key:
                for job in self.jobs.values():
                    if (
                        job.get("batch_idempotency_key") == idempotency_key
                        and job.get("owner_id") == owner_id
                    ):
                        if job.get("batch_digest") != digest:
                            raise GatewayError(
                                "idempotency_conflict",
                                "同一 Idempotency-Key 已用于不同的视频批量参数。",
                                409,
                            )
                        return job["batch_id"]
            for item in request.items:
                self.validate_provider(item)
                self.references.paths(item.reference_images, owner_id)
            if self.pending_count() + len(request.items) > self.settings.max_pending:
                raise GatewayError(
                    "queue_full",
                    "当前队列容量不足，整批视频任务均未提交；请稍后再试。",
                    429,
                )
            batch_id = "vbatch_" + uuid.uuid4().hex
            created = []
            for item in request.items:
                video_request = VideoRequest.model_validate(
                    item.model_dump(exclude={"custom_id"})
                )
                job = self._new_job(
                    video_request,
                    owner_id=owner_id,
                    batch_id=batch_id,
                    custom_id=item.custom_id,
                    batch_idempotency_key=idempotency_key,
                    batch_digest=digest,
                )
                created.append(job)
            try:
                self._persist()
            except OSError:
                for job in created:
                    self.jobs.pop(job["id"], None)
                    self.events.pop(job["id"], None)
                raise GatewayError(
                    "storage_error", "视频批次未能保存，任务均未提交。", 500
                ) from None
            for job in created:
                self.queue.put_nowait(job["id"])
            return batch_id

    def get_batch(self, batch_id: str, owner_id=None, admin=False) -> list[dict]:
        jobs = [job for job in self.jobs.values() if job.get("batch_id") == batch_id]
        if not jobs or (not admin and jobs[0].get("owner_id") != owner_id):
            raise GatewayError("batch_not_found", "批次不存在。", 404)
        return jobs

    async def wait(self, job_id: str) -> dict:
        job = self.get(job_id)
        if job["status"] not in TERMINAL:
            await self.events[job_id].wait()
        return job

    def get(self, job_id: str) -> dict:
        if job_id not in self.jobs:
            raise GatewayError("job_not_found", "任务不存在。", 404)
        return self.jobs[job_id]

    async def cancel(self, job_id: str) -> dict:
        job = self.get(job_id)
        if job["status"] == "running":
            raise GatewayError(
                "already_running",
                "任务已提交给上游网页，请等待完成；关闭控制台不会停止上游生成。",
                409,
            )
        if job["status"] in {"queued", "waiting"}:
            job["status"] = "cancelled"
            job["finished_at"] = time.time()
            self._persist()
            self.events[job_id].set()
            await self.accounts.wake()
        return job

    async def _worker(self):
        while True:
            job_id = await self.queue.get()
            job = self.jobs[job_id]
            if job["status"] == "cancelled":
                self.queue.task_done()
                continue
            self.active_ids.add(job_id)
            browser_lock = None
            browser_acquired = False
            try:
                self._persist()
                is_chat = job.get("kind") == "chat"
                schema = (
                    ChatRequest
                    if is_chat
                    else VideoRequest
                    if job.get("kind") == "video"
                    else ImageRequest
                )
                request = schema.model_validate(job["request"])
                reference_paths = self.references.paths(
                    request.reference_images, job.get("owner_id")
                )
                native_image = request.provider in {"gemini", "bing", "jimeng-web", "tongyi-web", "hailuo-web", DOUBAO_ID, IMAGEFX_ID, RECRAFT_ID, LEONARDO_ID, SEAART_ID, WHISK_ID, PUTER_ID, POLLINATIONS_ID} and not isinstance(
                    request, VideoRequest
                )
                if not native_image and not isinstance(request, VideoRequest):
                    browser_lock = self._browser_locks.setdefault(
                        request.provider, asyncio.Lock()
                    )
                    await browser_lock.acquire()
                    browser_acquired = True
                    if job["status"] == "cancelled":
                        raise asyncio.CancelledError
                    job["status"], job["started_at"] = "running", time.time()
                elif native_image and job["status"] != "running":
                    # Native connectors do not need a browser lease, but they
                    # still occupy one of the two global workers and should be
                    # visible as running while the upstream request is active.
                    job["status"], job["started_at"] = "running", time.time()
                if is_chat:
                    job["text"] = (
                        await self.accounts.execute(
                            job,
                            request,
                            lambda backend, req=request, refs=reference_paths: (
                                backend.chat(req, reference_paths=refs)
                            ),
                            self._persist,
                        )
                        if request.provider == "gemini"
                        else await video_backend_for(
                            self.video, request.provider, self.settings.timeout
                        ).chat(request, reference_paths)
                        if request.provider == DOUBAO_ID
                        else await self.bridge.run(request, "chat", reference_paths)
                    )
                    job["status"] = "succeeded"
                    continue
                if isinstance(request, VideoRequest):
                    folder = self.settings.images_dir / job_id
                    folder.mkdir(parents=True, exist_ok=True)
                    job["status"], job["started_at"] = "running", time.time()
                    self._persist()
                    if request.provider == "gemini":
                        path = await self.accounts.execute(
                            job,
                            request,
                            lambda backend, req=request, refs=reference_paths: backend.generate_video(
                                req, folder, refs
                            ),
                            self._persist,
                        )
                    elif request.provider == "bing":
                        path = await BingWebBackend(
                            self.bing.cookie, self.settings.timeout
                        ).generate_video(request, folder, reference_paths)
                    elif request.provider == "jimeng-web":
                        path = await video_backend_for(
                            self.video, request.provider, self.settings.timeout
                        ).generate(request, folder, reference_paths)
                    else:
                        path = await video_backend_for(
                            self.video, request.provider, self.settings.timeout
                        ).generate(request, folder, reference_paths)
                    path = Path(path).resolve()
                    if (
                        not path.is_relative_to(folder.resolve())
                        or not path.is_file()
                        or path.stat().st_size <= 0
                    ):
                        raise GatewayError("video_download_failed", "视频未能保存到本地。", 502)
                    job["videos"].append(
                        str(path.relative_to(self.settings.images_dir.resolve()))
                    )
                    job["status"] = "succeeded"
                    continue
                folder = self.settings.images_dir / job_id
                folder.mkdir(parents=True, exist_ok=True)
                while len(job["images"]) < request.n:
                    remaining = request.n - len(job["images"])
                    job["text"] = None
                    try:
                        self._persist()
                    except OSError:
                        log.error("Could not clear previous streamed Gemini text.")
                    last_persist = [0.0]

                    async def update_text(
                        text, current_job=job, last_written=last_persist
                    ):
                        value = (text or "")[:4000]
                        if not value or value == current_job.get("text"):
                            return
                        current_job["text"] = value
                        now = time.monotonic()
                        if now - last_written[0] >= 1:
                            last_written[0] = now
                            try:
                                self._persist()
                            except OSError:
                                log.error("Could not persist streamed Gemini text.")

                    paths = (
                        await self.generate_with_quota_fallback(
                            job,
                            request,
                            folder,
                            remaining,
                            reference_paths,
                            update_text,
                        )
                        if request.provider == "gemini"
                        else await ImageFxBackend(
                            self.imagefx.credential, self.settings.timeout
                        ).generate(request, folder, remaining, reference_paths)
                        if request.provider == IMAGEFX_ID
                        else await RecraftBackend(
                            self.recraft.credential, self.settings.timeout
                        ).generate(request, folder, remaining, reference_paths)
                        if request.provider == RECRAFT_ID
                        else await LeonardoBackend(
                            self.leonardo.credential, self.settings.timeout
                        ).generate(request, folder, remaining, reference_paths)
                        if request.provider == LEONARDO_ID
                        else await SeaArtBackend(
                            self.seaart.config(), self.settings.timeout
                        ).generate_image(request, folder, reference_paths)
                        if request.provider == SEAART_ID
                        else await WhiskBackend(
                            self.whisk.credential, self.settings.timeout
                        ).generate(request, folder, remaining, reference_paths)
                        if request.provider == WHISK_ID
                        else await video_backend_for(
                            self.video, request.provider, self.settings.timeout
                        ).generate_image(request, folder, reference_paths)
                        if request.provider in {"jimeng-web", "tongyi-web", "hailuo-web", DOUBAO_ID, PUTER_ID, POLLINATIONS_ID}
                        else await BingWebBackend(
                            self.bing.cookie, self.settings.timeout
                        ).generate(
                            request, folder, remaining, reference_paths
                        )
                        if request.provider == "bing"
                        else await self.bridge.run(
                            request,
                            "image",
                            reference_paths,
                            folder,
                            remaining,
                        )
                    )
                    if not paths:
                        raise GatewayError(
                            "no_generated_image", "所选平台未返回生成图片。"
                        )
                    for path in paths[: request.n - len(job["images"])]:
                        path = Path(path).resolve()
                        if (
                            not path.is_relative_to(folder.resolve())
                            or not path.is_file()
                        ):
                            raise GatewayError(
                                "download_failed", "图片未能保存到本地。"
                            )
                        if not image_mime(path):
                            path.unlink(missing_ok=True)
                            raise GatewayError(
                                "invalid_image",
                                "下载结果不是支持的图片，请检查登录状态。",
                            )
                        job["images"].append(
                            str(path.relative_to(self.settings.images_dir.resolve()))
                        )
                    self._persist()
                job["text"] = None
                job["status"] = "succeeded"
            except asyncio.CancelledError:
                if job["status"] in {"queued", "waiting"}:
                    raise
                if job["status"] != "cancelled":
                    job["status"] = "failed"
                    job["error"] = GatewayError(
                        "service_stopped",
                        "服务已停止，任务未重新提交。请先检查网页生成记录。",
                        503,
                    ).as_dict()
                    raise
            except Exception as exc:  # noqa: BLE001 - keep the worker alive after upstream failures
                log.warning("Job %s failed: %s", job_id, type(exc).__name__)
                job["status"], job["error"] = "failed", safe_error(exc).as_dict()
                if job.get("kind") == "image":
                    folder = (self.settings.images_dir / job_id).resolve()
                    for partial_path in getattr(exc, "partial_paths", []):
                        path = Path(partial_path).resolve()
                        if (
                            path.is_relative_to(folder)
                            and path.is_file()
                            and image_mime(path)
                        ):
                            relative = str(
                                path.relative_to(self.settings.images_dir.resolve())
                            )
                            if relative not in job["images"]:
                                job["images"].append(relative)
                if job.get("kind") == "image" and job["error"].get("response"):
                    job["text"] = job["error"]["response"]
            finally:
                if browser_acquired:
                    browser_lock.release()
                if job["status"] in TERMINAL:
                    job["finished_at"] = time.time()
                self.active_ids.discard(job_id)
                self.events[job_id].set()
                self.queue.task_done()
                try:
                    self._persist()
                except OSError:
                    log.error("Could not persist final job state.")

    async def close(self):
        for worker in self.workers:
            worker.cancel()
        for worker in self.workers:
            with contextlib.suppress(asyncio.CancelledError):
                await worker
        await self.accounts.close()
        self.bridge.close()


def image_mime(path: Path) -> str | None:
    with path.open("rb") as stream:
        header = stream.read(16)
    if header.startswith(b"\x89PNG\r\n\x1a\n"):
        return "image/png"
    if header.startswith(b"\xff\xd8\xff"):
        return "image/jpeg"
    if header[:4] == b"RIFF" and header[8:12] == b"WEBP":
        return "image/webp"
    if header.startswith((b"GIF87a", b"GIF89a")):
        return "image/gif"
    return None


def video_mime(path: Path) -> str | None:
    """Recognize common downloaded video containers without trusting suffixes."""
    with path.open("rb") as stream:
        header = stream.read(16)
    if len(header) >= 12 and header[4:8] == b"ftyp":
        return "video/mp4"
    if header.startswith(b"\x1a\x45\xdf\xa3"):
        return "video/webm"
    return None
