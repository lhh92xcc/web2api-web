"""Direct video-generation adapters with local credential storage.

The official API adapters and the Hailuo web adapter share the same local job
interface.  The web adapter uses the user's own Hailuo web token and the same
signed HTTP requests as the web application; it does not automate a browser or
attempt to bypass login, CAPTCHA, WAF, or subscription checks.
"""

import asyncio
import base64
import binascii
import hashlib
import json
import mimetypes
import os
import platform
import re
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid
from pathlib import Path

from pydantic import BaseModel, ConfigDict, Field, field_validator

from jimeng import IMAGE_MODELS, VIDEO_MODELS, JimengWebBackend
from doubao import (
    DOUBAO_DEFAULT_IMAGE_MODEL,
    DOUBAO_DEFAULT_VIDEO_MODEL,
    DOUBAO_ID,
    DOUBAO_IMAGE_MODELS,
    DOUBAO_TEXT_MODELS,
    DOUBAO_VIDEO_MODELS,
    DoubaoWebBackend,
    parse_credential as parse_doubao_credential,
)
from tongyi import (
    TONGYI_DEFAULT_IMAGE_MODEL,
    TONGYI_DEFAULT_VIDEO_MODEL,
    TONGYI_IMAGE_MODELS,
    TONGYI_VIDEO_MODELS,
    TongyiWebBackend,
)
from storage import GatewayError, atomic_json

MAX_VIDEO_BYTES = 1024 * 1024 * 1024
MAX_IMAGE_BYTES = 100 * 1024 * 1024
POLL_INTERVAL = 5
WEB_AUTH_TYPES = {"web_token", "web_session", "web_cookie"}

PUTER_ID = "puter"
PUTER_BASE_URL = "https://api.puter.com"
# These are the model IDs documented by Puter.  Puter currently exposes Vidu
# Q3/Q3 Turbo, but its public catalogue does not list Vidu Q2; Q2 remains
# available through the existing first-party ``vidu`` API connector below.
PUTER_VIDEO_MODELS = [
    "vidu/vidu-q3-turbo",
    "vidu/vidu-q3",
    "bytedance/seedance-2.5",
    "bytedance/seedance-2.0",
    "wan-ai/wan2.7-t2v",
    "wan-ai/wan2.7-i2v",
    "wan-ai/wan2.7-r2v",
    "pixverse/pixverse-v6",
    "pixverse/pixverse-v5.6",
    "minimax/hailuo-02",
    "minimax/video-01-director",
]
PUTER_IMAGE_MODELS = [
    "seedream-5-0-lite-260128",
    "gpt-image-2.5-sunburst",
    "gpt-image-2.5-flare",
    "gpt-image-2",
    "gemini-3.1-flash-image",
    "kling-v3",
]
POLLINATIONS_ID = "pollinations"
POLLINATIONS_BASE_URL = "https://image.pollinations.ai"
POLLINATIONS_MODELS = ["sana"]


def _image_suffix(raw: bytes, content_type: str = "") -> str | None:
    """Return a suffix for the image formats the local image route accepts."""
    normalized = (content_type or "").split(";", 1)[0].strip().lower()
    by_type = {
        "image/png": ".png",
        "image/jpeg": ".jpg",
        "image/jpg": ".jpg",
        "image/webp": ".webp",
        "image/gif": ".gif",
    }
    if normalized in by_type:
        return by_type[normalized]
    if raw.startswith(b"\x89PNG\r\n\x1a\n"):
        return ".png"
    if raw.startswith(b"\xff\xd8\xff"):
        return ".jpg"
    if raw.startswith((b"GIF87a", b"GIF89a")):
        return ".gif"
    if raw[:4] == b"RIFF" and raw[8:12] == b"WEBP":
        return ".webp"
    return None

# Hailuo exposes the current model catalogue from a public first-party
# endpoint.  Keep the known IDs here so the local API can offer the models
# without making a credential-bearing request just to render the UI.  The
# website may add/remove models; an unknown model is rejected with a clear
# message and the list can be updated independently.
HAILUO_VIDEO_MODELS = [
    "hailuo3.0-t2v",
    "hailuo3.0-i2v",
    "hailuo3.0-extend",
    "hailuo_h3_max_t2v",
    "hailuo_h3_max_i2v",
    "seedance2.0-t2v",
    "seedance2.0-i2v",
]
HAILUO_IMAGE_MODELS = [
    "gpt-image-2",
    "nano_banana21_flash",
    "nano-banana2",
    "gpt-image-1.5",
    "seedream-5.0",
]

# Vidu exposes Q2 and Q3 through the same API provider.  Keep the sets and
# endpoint capabilities explicit.  The API reuses the same task status
# endpoint, but model names are not interchangeable between text, image,
# start/end, and subject-reference requests.
VIDU_Q2_MODELS = frozenset(
    {"viduq2", "viduq2-turbo", "viduq2-pro", "viduq2-pro-fast"}
)
VIDU_Q3_MODELS = frozenset(
    {
        "viduq3-turbo",
        "viduq3-pro",
        "viduq3-pro-fast",
        "viduq3",
        "viduq3-mix",
        "viduq3-ad",
        "viduq3-drama",
    }
)
VIDU_Q2_TEXT_MODELS = frozenset({"viduq2"})
VIDU_Q2_IMAGE_MODELS = frozenset({"viduq2-turbo", "viduq2-pro"})
VIDU_Q2_START_END_MODELS = frozenset(
    {"viduq2-turbo", "viduq2-pro", "viduq2-pro-fast"}
)
VIDU_Q2_REFERENCE_MODELS = frozenset({"viduq2", "viduq2-pro"})
VIDU_Q3_TEXT_MODELS = frozenset({"viduq3-turbo", "viduq3-pro"})
VIDU_Q3_IMAGE_MODELS = frozenset(
    {"viduq3-turbo", "viduq3-pro", "viduq3-pro-fast"}
)
VIDU_Q3_START_END_MODELS = frozenset(
    {"viduq3-turbo", "viduq3-pro", "viduq3-pro-fast"}
)
VIDU_Q3_REFERENCE_MODELS = frozenset(
    {"viduq3-turbo", "viduq3", "viduq3-mix", "viduq3-ad", "viduq3-drama"}
)
VIDU_REFERENCE_MODELS = VIDU_Q2_REFERENCE_MODELS | VIDU_Q3_REFERENCE_MODELS
VIDU_SUBJECT_REFERENCE_MODELS = frozenset({"viduq2", "viduq3-turbo"})
VIDU_REFERENCE_ONLY_MODELS = frozenset(
    {"viduq2", "viduq3", "viduq3-mix", "viduq3-ad", "viduq3-drama"}
)

# The China Vidu web application uses the same public task API as the page,
# but maps its display names to these internal model versions.  This is kept
# as a separate provider from ``vidu``: the latter is the paid first-party API
# and requires an API key, while this provider uses the user's web Cookie and
# the web account's own free/daily credit buckets.
VIDU_WEB_ID = "vidu-web"
VIDU_WEB_BASE_URL = "https://service.vidu.cn"
VIDU_WEB_MODEL_VERSIONS = {
    "vidu-web-q2": "3.1",
    "vidu-web-q2-pro": "3.1_pro",
    "vidu-web-q3": "3.2",
    "vidu-web-q3-pro": "3.2_pro_m",
    "vidu-web-q3-fast": "3.2_fast_m",
    "vidu-web-q3-lite": "3.2_lite_m",
}
VIDU_WEB_VIDEO_MODELS = tuple(VIDU_WEB_MODEL_VERSIONS)
VIDU_WEB_Q2_MODELS = frozenset(
    model for model in VIDU_WEB_VIDEO_MODELS if "q2" in model
)
VIDU_WEB_REFERENCE_MODELS = frozenset(VIDU_WEB_VIDEO_MODELS)

VIDEO_PROVIDERS = {
    DOUBAO_ID: {
        "name": "豆包网页（Web2API 实验性）",
        "web_url": "https://www.doubao.com/chat/",
        "base_url": "https://www.doubao.com",
        "default_model": DOUBAO_DEFAULT_VIDEO_MODEL,
        "models": list(DOUBAO_VIDEO_MODELS),
        "image_models": list(DOUBAO_IMAGE_MODELS),
        "image_default_model": DOUBAO_DEFAULT_IMAGE_MODEL,
        "text_models": list(DOUBAO_TEXT_MODELS),
        "auth_type": "web_cookie",
        "free_web": True,
        "verification_status": "web_candidate",
        "verification_label": "网页候选 · 不处理 WebMSSDK / 验证挑战",
        "free_bucket": "豆包网页登录/赠送额度（上游不公开分类）",
        "browser_required": False,
        "credential_label": "豆包 Cookie + device_id/web_id JSON",
        "supports_credits": False,
        "images": True,
        "text": True,
        "references": False,
        "capabilities": "网页对话、文生图、Seedance 2.0 文生视频；参考图上传需官方网页签名",
        "fit": "Vidu Q3 / Q2 替代候选；使用豆包网页自身额度",
        "api_free_note": (
            "这是豆包网页入口，不调用火山方舟 API Key。需要从自己的已登录网页复制 Cookie、"
            "device_id 和 web_id；服务不生成设备指纹、不实现 WebMSSDK/X-Bogus、不绕过 CAPTCHA。"
            "如果上游返回验证挑战，请先在豆包官方网页完成验证。网页额度分类由豆包账号页面决定，"
            "因此目前标为候选，不能保证所有账号均有免费桶。"
        ),
        "docs_url": "https://www.doubao.com/chat/",
    },
    "jimeng-web": {
        "name": "即梦 AI 网页（Web2API）",
        "web_url": "https://jimeng.jianying.com/",
        "base_url": "https://jimeng.jianying.com",
        "default_model": "jimeng-seedance-2.5-draft",
        "models": list(VIDEO_MODELS),
        "image_models": list(IMAGE_MODELS),
        "image_default_model": "jimeng-seedream-5-flash",
        "auth_type": "web_session",
        "free_web": True,
        "verification_status": "web_quota_declared",
        "verification_label": "有明确免费桶 · 需账号实测",
        "free_bucket": "gift_credit（每日赠送）",
        "browser_required": False,
        "credential_label": "即梦网页 sessionid（Cookie Value）",
        "supports_credits": True,
        "images": True,
        "text": False,
        "references": True,
        "capabilities": "网页 Seedream 图片和 Seedance 视频；文生图、参考图生图、文生视频、首尾帧",
        "fit": "Q3 / Q2 替代；使用即梦网页每日赠送积分",
        "api_free_note": (
            "即梦网页官方说明每日登录赠送积分。默认只使用 gift_credit（赠送积分），"
            "不会自动消耗购买积分或会员积分；网页 sessionid 失效时需重新复制。"
        ),
        "docs_url": "https://jimeng.jianying.com/",
    },
    "tongyi-web": {
        "name": "通义万相网页（Web2API）",
        "web_url": "https://tongyi.aliyun.com/wanxiang/",
        "base_url": "https://wanx.biz.aliyun.com",
        "default_model": TONGYI_DEFAULT_VIDEO_MODEL,
        "models": list(TONGYI_VIDEO_MODELS),
        "image_models": list(TONGYI_IMAGE_MODELS),
        "image_default_model": TONGYI_DEFAULT_IMAGE_MODEL,
        "auth_type": "web_cookie",
        "free_web": True,
        "verification_status": "web_quota_declared",
        "verification_label": "有明确免费桶 · 需账号实测",
        "free_bucket": "bonus（奖励灵感值）",
        "browser_required": False,
        "credential_label": "通义万相完整 Cookie Header（含 XSRF-TOKEN）",
        "supports_credits": True,
        "images": True,
        "text": False,
        "references": True,
        "capabilities": "网页 Wan 2.7 / Wan 3.0；文生图、参考图生图、文生视频、首尾帧图生视频",
        "fit": "Q3 / Q2 替代候选；Wan 2.7/3.0 的网页免费灵感值",
        "api_free_note": (
            "使用通义万相网页账号自己的奖励灵感值（bonus），不是阿里云 Model Studio API Key。"
            "服务会在提交前查询并可尝试领取每日登录奖励，只使用 bonus；会员 member 和充值 topUp "
            "不会自动消耗。视频通常需要更多灵感值，免费桶不足时直接停止。"
        ),
        "docs_url": "https://tongyi.aliyun.com/wanxiang/",
    },
    "hailuo-web": {
        "name": "Hailuo / MiniMax 网页（Web2API）",
        "web_url": "https://hailuoai.video",
        "base_url": "https://hailuoai.video",
        "default_model": "hailuo3.0-t2v",
        "models": list(HAILUO_VIDEO_MODELS),
        "image_models": list(HAILUO_IMAGE_MODELS),
        "image_default_model": "gpt-image-2",
        "auth_type": "web_token",
        "free_web": True,
        "verification_status": "web_candidate",
        "verification_label": "网页候选 · 需账号实测",
        "free_bucket": "登录/订阅赠送 credits（分类需上游返回）",
        "browser_required": False,
        "credential_label": "Hailuo 网页 Token（localStorage 的 _token）",
        "supports_credits": True,
        "images": True,
        "references": True,
        "capabilities": "网页 Hailuo 3.0、Seedance 2.0；文生图、参考图生图、文生视频、首尾帧",
        "fit": "Q3 / Q2 网页免费额度候选",
        "api_free_note": (
            "使用你自己的 Hailuo 网页账号 credits/free allowance，不是官方 API Key。"
            "Hailuo 3.0 和 Seedance 2.0 是否可用、免费档和剩余额度由网页账号决定；"
            "生成前只允许登录/订阅赠送余额，购买 credits 不会自动使用；"
            "购买 credits 或仅有付费模型余额时会拒绝提交；Veo/Sora 等订阅模型不会因接入 Web2API 自动免费。"
        ),
        "docs_url": "https://hailuoai.video",
    },
    "kling-web": {
        "name": "Kling AI 网页（Web2API）",
        "web_url": "https://klingai.com/app/video/new",
        "base_url": "https://klingai.com",
        "default_model": "2.1",
        "models": ["1.6", "2.0", "2.1", "2.1-master", "2.5", "2.6"],
        "auth_type": "web_cookie",
        "free_web": True,
        "verification_status": "browser_challenge",
        "verification_label": "候选 · 可能要求网页风控验证",
        "free_bucket": "网页 points / tickets（分类不稳定）",
        "browser_required": True,
        "credential_label": "Kling 网页 Cookie Header",
        "supports_credits": True,
        "capabilities": "Kling 网页模型；文生视频、首帧图生视频、首尾帧；结果保存到本机",
        "fit": "实验性 Q3 / Q2 替代候选；动作、运镜和多镜头连续性",
        "api_free_note": (
            "使用你自己的 Kling 网页账号 points/tickets 和网页免费队列。"
            "免费积分、模型权限和地区规则由网页账号决定；这不是 Kling 官方 API Key。"
            "当前未完成纯 HTTP 端到端验证；如果网页要求登录、验证码、风控或动态签名，"
            "需要先在网页正常完成验证，本地接口不会绕过这些挑战。"
        ),
        "docs_url": "https://klingai.com/app/video/new",
    },
    "pixverse-web": {
        "name": "PixVerse / 拍我 AI 网页（Web2API）",
        "web_url": "https://pai.video",
        "base_url": "https://cn-app-api.pixverseai.cn/creative_platform",
        "default_model": "v6",
        "models": ["v6", "pixverse-c1", "v5.6", "v5.5", "v5-fast"],
        "auth_type": "web_token",
        "free_web": True,
        "verification_status": "web_candidate",
        "verification_label": "网页候选 · 需账号实测",
        "free_bucket": "每日/月度网页 credits（套餐桶不使用）",
        "browser_required": False,
        "credential_label": "PixVerse 网页 Token（Cookie _ai_token 的值）",
        "supports_credits": True,
        "capabilities": "PixVerse 原生模型；文生视频、单张参考图生视频；可查询网页账号 credits",
        "fit": "Q3 / Q2 风格替代候选；使用 PixVerse 网页账号额度",
        "api_free_note": (
            "使用你自己的 PixVerse / 拍我 AI 网页账号 credits，不是官方 API Key。"
            "生成前只允许每日/月度免费余额，套餐 credits 不会自动使用；"
            "可用模型和实际消耗以账号余额和网页规则为准；"
            "这里只列 PixVerse 原生模型，不把站内 Seedance、Veo 等外部模型标为免费。"
        ),
        "docs_url": "https://pai.video",
    },
    "minimax": {
        "name": "MiniMax H3",
        "web_url": "https://hailuoai.video/",
        "base_url": "https://api.minimax.cn",
        "default_model": "MiniMax-H3",
        "models": [
            "MiniMax-H3",
            "MiniMax-H3-Highspeed",
            "MiniMax-H3-Lite",
            "MiniMax-H3-Max",
        ],
        "capabilities": "文生视频、首尾帧图生视频、最多 9 张参考图、音画同步",
        "verification_status": "api_only",
        "verification_label": "仅官方 API",
        "free_bucket": "API 体验额度（按控制台）",
        "browser_required": False,
        "fit": "Q3 替代：对白、口型和音画一体优先",
        "api_free_note": "网页新用户额度不等同于 API 额度；H3 API 按平台账户规则计费或赠送体验额度。",
        "docs_url": "https://platform.minimax.cn/docs/guides/video-generation",
    },
    PUTER_ID: {
        "name": "Puter AI（Vidu Q3 / Seedance / Wan）",
        "web_url": "https://puter.com/",
        "base_url": PUTER_BASE_URL,
        "default_model": "vidu/vidu-q3-turbo",
        "models": list(PUTER_VIDEO_MODELS),
        "image_models": list(PUTER_IMAGE_MODELS),
        "image_default_model": "seedream-5-0-lite-260128",
        "auth_type": "web_token",
        "free_web": False,
        "billing_mode": "puter_user_pays",
        "verification_status": "puter_user_pays",
        "verification_label": "Puter 用户自付 · 不保证免费",
        "free_bucket": "无固定免费额度（test_mode 仅返回样片）",
        "browser_required": False,
        "credential_label": "Puter auth_token_v2",
        "supports_credits": False,
        "images": True,
        "videos": True,
        "text": False,
        "references": True,
        "capabilities": "Puter 统一驱动；Vidu Q3/Q3 Turbo、Seedance、Wan、PixVerse、Hailuo；文生视频、首尾帧和参考图",
        "fit": "Q3 替代候选；由 Puter 账户按 User-Pays 结算",
        "api_free_note": (
            "Puter 的 drivers/call 使用你的 Puter 登录令牌，由 Puter User-Pays 账户结算。"
            "这不是 Vidu 网页免费积分，也不保证免费；Puter 当前公开文档列出 Vidu Q3/Q3 Turbo，"
            "未列出 Q2。可在 Puter.js 使用 test_mode 返回样片做连通性测试，但正式生成仍可能收费。"
        ),
        "docs_url": "https://docs.puter.com/AI/txt2vid/",
    },
    "vidu": {
        "name": "Vidu Q3 / Q2",
        "web_url": "https://www.vidu.com/",
        "base_url": "https://api.vidu.com",
        "default_model": "viduq3-turbo",
        "models": [
            "viduq3-turbo",
            "viduq3-pro",
            "viduq3-pro-fast",
            "viduq3",
            "viduq3-mix",
            "viduq3-ad",
            "viduq3-drama",
            "viduq2",
            "viduq2-turbo",
            "viduq2-pro",
            "viduq2-pro-fast",
        ],
        "capabilities": "图生视频、首尾帧、Q2/Q3 主体模型最多 7 张参考图、音画同步",
        "verification_status": "api_only",
        "verification_label": "仅官方 API",
        "free_bucket": "API credits（网页积分不适用）",
        "browser_required": False,
        "fit": "原味 Q3 / Q2：多主体一致性、音画一体和首尾帧",
        "supports_credits": True,
        "api_free_note": (
            "同一个 Vidu API Key 可选择 Q3 或 Q2 模型；网页免费积分与 API 账户分开，"
            "是否有 API 体验额度以 Vidu 控制台实际显示为准。"
        ),
        "docs_url": "https://platform.vidu.com/docs/api-reference/video-models/vidu-q3/reference-to-video",
    },
    VIDU_WEB_ID: {
        "name": "Vidu 网页（Q3 / Q2 免费额度）",
        "web_url": "https://www.vidu.cn/",
        "base_url": VIDU_WEB_BASE_URL,
        "default_model": "vidu-web-q3",
        "models": list(VIDU_WEB_VIDEO_MODELS),
        "auth_type": "web_cookie",
        "free_web": True,
        "verification_status": "web_quota_declared",
        "verification_label": "网页免费额度 · 需账号实测",
        "free_bucket": "Vidu 网页每日 / 新手 credits",
        "browser_required": False,
        "credential_label": "Vidu 中国站完整 Cookie Header",
        "supports_credits": True,
        "images": False,
        "videos": True,
        "text": False,
        "references": True,
        "capabilities": "Vidu 中国网页 Q2/Q3；文生视频、单图、首尾帧和多图主体参考",
        "fit": "Q3 / Q2 替代；直接使用 Vidu 网页账号免费额度",
        "api_free_note": (
            "这是 Vidu 中国网页入口，不是 Vidu 官方 API Key。粘贴已登录 www.vidu.cn 的完整 Cookie Header，"
            "本机直接调用 service.vidu.cn，不需要常驻浏览器。提交前会查询网页额度；无法确认免费桶或免费额度为 0 时不会提交，"
            "避免把任务落到购买 credits。Cookie 失效、账号风控或上游要求验证时，需要先在 Vidu 网页完成验证后重新复制 Cookie。"
        ),
        "docs_url": "https://www.vidu.cn/create/character2video",
    },
    POLLINATIONS_ID: {
        "name": "Pollinations 公共图片（Sana）",
        "web_url": "https://pollinations.ai/",
        "base_url": POLLINATIONS_BASE_URL,
        "default_model": "sana",
        "models": [],
        "image_models": list(POLLINATIONS_MODELS),
        "image_default_model": "sana",
        "auth_type": "public",
        "free_web": True,
        "billing_mode": "public_free",
        "verification_status": "verified_public",
        "verification_label": "公共免费入口 · 可能限流",
        "free_bucket": "无需账号的公共接口",
        "browser_required": False,
        "credential_label": "无需凭据",
        "supports_credits": False,
        "images": True,
        "videos": False,
        "text": False,
        "references": False,
        "capabilities": "无需账号的 Sana 文生图公共入口；不支持本地参考图",
        "fit": "免费低成本图片兜底",
        "api_free_note": (
            "当前旧版未认证公共接口稳定提供 Sana；共享服务可能限流、排队或临时不可用。"
            "Pollinations 新版 gen.pollinations.ai 即使模型目录标记 paid_only=false，实际调用仍要求有效 API Key，"
            "并按 Pollen 计价，因此本连接器不会把新版模型或 Pollen 余额当成免费，也不会自动切换到新版接口。"
            "指定其他高级模型不会被伪装成免费，参考图需要选择支持图生图的平台。"
        ),
        "docs_url": "https://pollinations.ai/",
    },
    "bailian": {
        "name": "阿里百炼 HappyHorse / Wan",
        "web_url": "https://bailian.aliyun.com/",
        "base_url": "https://dashscope.aliyuncs.com",
        "default_model": "happyhorse-1.1-i2v",
        "models": [
            "happyhorse-1.1-t2v",
            "happyhorse-1.1-i2v",
            "happyhorse-1.1-r2v",
            "wan2.6-i2v-flash",
        ],
        "capabilities": "首帧图生视频、参考图生视频；HappyHorse/Wan 的能力和音频支持按模型变化",
        "verification_status": "api_only",
        "verification_label": "仅官方 API",
        "free_bucket": "API 体验额度（按控制台）",
        "browser_required": False,
        "fit": "Q2 替代：低成本批量打样和首帧图生视频",
        "api_free_note": "百炼新用户可能有模型体验额度；免费额度、地域和模型开通状态以控制台为准。",
        "docs_url": "https://www.alibabacloud.com/help/en/model-studio/legacy-image-to-video-api-reference/",
    },
    "seedance": {
        "name": "火山方舟 Seedance",
        "web_url": "https://seedance.ai/",
        "base_url": "https://ark.cn-beijing.volces.com/api/v3",
        # Keep the default on the generally available Seedance 2.0 endpoint.
        # 2.5 is listed for accounts that have explicitly enabled it, but its
        # web membership/free status does not grant API access automatically.
        "default_model": "doubao-seedance-2-0-260128",
        "models": [
            "doubao-seedance-2-0-260128",
            "doubao-seedance-1-0-pro-250528",
            "doubao-seedance-1-0-lite-i2v-250428",
            "doubao-seedance-2-5-260628",
        ],
        "capabilities": "文生视频、首帧/首尾帧和全模态参考；音频能力按 Seedance 模型变化",
        "verification_status": "api_only",
        "verification_label": "仅官方 API",
        "free_bucket": "方舟 API 额度（网页积分不适用）",
        "browser_required": False,
        "fit": "Q2 替代：动作、运镜和多镜头连续性优先",
        "api_free_note": "网页免费积分不自动转为方舟 API 额度；方舟 API 需要账户开通并按控制台规则计费。",
        "docs_url": "https://docs.volcengine.com/docs/ark/create-video-generation-task-api?lang=zh",
    },
}


class VideoKeyRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)
    api_key: str | None = Field(default=None, max_length=4096)
    credential: str | None = Field(default=None, max_length=4096)
    auth_type: str | None = Field(default=None, max_length=40)
    base_url: str | None = Field(default=None, max_length=2048)

    @field_validator("api_key")
    @classmethod
    def key_value(cls, value):
        return value.strip() if value else value

    @field_validator("credential")
    @classmethod
    def credential_value(cls, value):
        return value.strip() if value else value

    def secret(self) -> str | None:
        """Accept the old ``api_key`` field and the web-token field."""
        return (self.credential or self.api_key or "").strip() or None


class VideoConfigStore:
    """Store provider credentials locally and expose only non-secret metadata."""

    def __init__(self, state_dir: Path):
        self.path = state_dir / "video-providers.json"
        self.items: dict[str, dict] = {}
        if self.path.exists():
            try:
                raw = json.loads(self.path.read_text(encoding="utf-8"))
                if isinstance(raw, dict):
                    for provider, value in raw.items():
                        if provider in VIDEO_PROVIDERS and isinstance(value, dict):
                            key = str(
                                value.get("credential", value.get("api_key", ""))
                            ).strip()
                            if key:
                                self.items[provider] = {
                                    "credential": key,
                                    "auth_type": str(
                                        value.get(
                                            "auth_type",
                                            VIDEO_PROVIDERS[provider].get(
                                                "auth_type", "api_key"
                                            ),
                                        )
                                    ),
                                    "base_url": str(value.get("base_url", "")).strip(),
                                    "uuid": str(value.get("uuid", "")).strip(),
                                    "device_id": str(value.get("device_id", "")).strip(),
                                    "allow_paid": bool(value.get("allow_paid", False)),
                                }
            except (OSError, TypeError, ValueError):
                self.items = {}

    def public(self) -> list[dict]:
        result = []
        for provider, info in VIDEO_PROVIDERS.items():
            saved = self.items.get(provider, {})
            result.append(
                {
                    "id": provider,
                    **info,
                    "configured": (
                        True
                        if info.get("auth_type") == "public"
                        else bool(saved.get("credential"))
                    ),
                    "base_url": info["base_url"]
                    if info.get("auth_type") in WEB_AUTH_TYPES
                    else saved.get("base_url") or info["base_url"],
                    "allow_paid": bool(saved.get("allow_paid", False))
                    if info.get("auth_type") == "web_session"
                    else False,
                }
            )
        return result

    def get(self, provider: str) -> dict:
        if provider not in VIDEO_PROVIDERS:
            raise GatewayError("video_provider_not_found", "没有此视频平台。", 404)
        saved = self.items.get(provider)
        if VIDEO_PROVIDERS[provider].get("auth_type") == "public":
            saved = saved or {}
        elif not saved or not saved.get("credential"):
            raise GatewayError(
                "video_provider_not_configured",
                (
                    f"请先在平台设置中填写 {VIDEO_PROVIDERS[provider]['name']} 网页 Token。"
                    if VIDEO_PROVIDERS[provider].get("auth_type")
                    in WEB_AUTH_TYPES
                    else f"请先在平台设置中填写 {VIDEO_PROVIDERS[provider]['name']} API Key。"
                ),
                409,
            )
        value = {**VIDEO_PROVIDERS[provider], **saved}
        # Existing official backends still read ``api_key``.  This alias is
        # kept only inside the backend config and is never exposed by public().
        value["api_key"] = saved.get("credential", "")
        if value.get("auth_type") in WEB_AUTH_TYPES:
            value["base_url"] = VIDEO_PROVIDERS[provider]["base_url"]
        elif not str(value.get("base_url", "")).strip():
            # API-key providers use the registry's first-party endpoint unless
            # the user explicitly saved a validated custom BaseURL.  An empty
            # saved value must not replace the provider default with a
            # relative URL (which would make credits/generation fail locally).
            value["base_url"] = VIDEO_PROVIDERS[provider]["base_url"]
        return value

    def save(
        self,
        provider: str,
        credential: str | None,
        base_url: str | None = None,
        auth_type: str | None = None,
    ):
        if provider not in VIDEO_PROVIDERS:
            raise GatewayError("video_provider_not_found", "没有此视频平台。", 404)
        if VIDEO_PROVIDERS[provider].get("auth_type") == "public":
            raise GatewayError("credential_not_required", "此公共平台无需填写凭据。", 422)
        value = (credential or "").strip()
        if not value:
            label = (
                "网页 Cookie / Token"
                if VIDEO_PROVIDERS[provider].get("auth_type") in WEB_AUTH_TYPES
                else "API Key"
            )
            raise GatewayError("credential_required", f"请填写视频平台 {label}。", 422)
        if provider == DOUBAO_ID:
            # Validate the JSON and required user-supplied context now so a
            # card cannot appear configured while every request is doomed to
            # fail.  The secret is still stored as the original local value.
            parse_doubao_credential(value)
        expected_auth = VIDEO_PROVIDERS[provider].get("auth_type", "api_key")
        if auth_type and auth_type != expected_auth:
            raise GatewayError("invalid_auth_type", "此平台的认证类型不匹配。", 422)
        url = (base_url or "").strip()
        if expected_auth in WEB_AUTH_TYPES:
            # Web tokens are only ever sent to the provider's fixed first-party
            # endpoint; allowing an arbitrary URL would make token exfiltration easy.
            url = VIDEO_PROVIDERS[provider]["base_url"]
        if url and expected_auth not in WEB_AUTH_TYPES:
            parsed = urllib.parse.urlsplit(url)
            if (
                parsed.scheme != "https"
                or not parsed.hostname
                or parsed.username
                or parsed.password
                or parsed.query
                or parsed.fragment
                or parsed.path not in {"", "/"}
            ):
                raise GatewayError("invalid_base_url", "Base URL 必须是无路径的 HTTPS 地址。", 422)
            url = url.rstrip("/")
        try:
            previous = self.items.get(provider, {})
            self.items[provider] = {
                "credential": value,
                "auth_type": expected_auth,
                "base_url": url,
                "uuid": previous.get("uuid") or str(uuid.uuid4()),
                "device_id": previous.get("device_id") or str(uuid.uuid4()),
                "allow_paid": previous.get("allow_paid", False)
                if expected_auth == "web_session"
                else False,
            }
            atomic_json(self.path, self.items)
        except OSError:
            raise GatewayError("storage_error", "视频平台认证信息保存失败。", 500) from None
        return next(item for item in self.public() if item["id"] == provider)

    def clear(self, provider: str):
        if provider not in VIDEO_PROVIDERS:
            raise GatewayError("video_provider_not_found", "没有此视频平台。", 404)
        self.items.pop(provider, None)
        try:
            atomic_json(self.path, self.items)
        except OSError:
            raise GatewayError("storage_error", "视频平台 API Key 删除失败。", 500) from None
        return next(item for item in self.public() if item["id"] == provider)


def _safe_json(raw: bytes) -> dict:
    try:
        value = json.loads(raw)
    except (TypeError, ValueError):
        raise GatewayError("video_invalid_response", "视频平台返回了无效响应。", 502) from None
    if not isinstance(value, dict):
        raise GatewayError("video_invalid_response", "视频平台返回了无效响应。", 502)
    return value


class JimengWebVideoBackend:
    """Video-store wrapper around the shared direct Jimeng web adapter.

    Keeping this wrapper in the video registry lets the UI store one local
    ``sessionid`` credential while the image and video workers use the same
    first-party session and free-credit policy.
    """

    def __init__(self, config: dict, timeout: int):
        self.backend = JimengWebBackend(config, timeout)

    async def credits(self):
        return await self.backend.credits()

    async def claim_daily_credit(self):
        return await self.backend.claim_daily_credit()

    async def generate(self, request, folder: Path, reference_paths):
        return await self.backend.generate(request, folder, reference_paths)

    async def generate_image(self, request, folder: Path, reference_paths):
        return await self.backend.generate_image(request, folder, reference_paths)


class _HTTPBackend:
    def __init__(self, config: dict, timeout: int):
        self.config = config
        self.timeout = timeout

    def _request(self, method: str, url: str, payload=None, headers=None):
        body = None if payload is None else json.dumps(payload, ensure_ascii=False).encode()
        request = urllib.request.Request(
            url,
            data=body,
            headers={"Accept": "application/json", **(headers or {}), **({"Content-Type": "application/json"} if body else {})},
            method=method,
        )
        try:
            with urllib.request.urlopen(request, timeout=self.timeout) as response:
                return response.status, response.read(MAX_VIDEO_BYTES + 1)
        except urllib.error.HTTPError as exc:
            try:
                detail = _safe_json(exc.read(64 * 1024)).get("error")
                message = detail.get("message") if isinstance(detail, dict) else None
            except Exception:  # noqa: BLE001
                message = None
            raise GatewayError(
                "video_upstream_error",
                message or f"视频平台请求失败（HTTP {exc.code}）。",
                429 if exc.code == 429 else 502,
            ) from None
        except (urllib.error.URLError, TimeoutError, OSError):
            raise GatewayError("video_network_error", "视频平台暂时无法连接，请稍后重试。", 502) from None

    def _download(self, url: str, path: Path):
        parsed = urllib.parse.urlsplit(url)
        if parsed.scheme != "https" or not parsed.hostname:
            raise GatewayError("video_invalid_url", "视频平台返回了无效下载地址。", 502)
        request = urllib.request.Request(url, headers={"Accept": "video/*"})
        try:
            with urllib.request.urlopen(request, timeout=self.timeout) as response, path.open("wb") as output:
                total = 0
                while True:
                    chunk = response.read(1024 * 1024)
                    if not chunk:
                        break
                    total += len(chunk)
                    if total > MAX_VIDEO_BYTES:
                        raise GatewayError("video_too_large", "返回视频超过本地保存上限。", 413)
                    output.write(chunk)
        except GatewayError:
            path.unlink(missing_ok=True)
            raise
        except (urllib.error.URLError, TimeoutError, OSError):
            path.unlink(missing_ok=True)
            raise GatewayError("video_download_failed", "视频结果下载失败，请稍后重试。", 502) from None

    @staticmethod
    def _data_uri(path: Path) -> str:
        mime = mimetypes.guess_type(path.name)[0] or "image/png"
        return f"data:{mime};base64," + base64.b64encode(path.read_bytes()).decode("ascii")

    async def generate(self, request, folder: Path, reference_paths):
        raise NotImplementedError

    async def _poll_download(self, query, folder: Path, prefix: str):
        deadline = time.monotonic() + self.timeout
        while time.monotonic() < deadline:
            result = await asyncio.to_thread(query)
            status, url, error = self._status(result)
            if status in {"succeeded", "success", "completed"} and url:
                path = folder / f"{prefix}-{uuid.uuid4().hex}.mp4"
                await asyncio.to_thread(self._download, url, path)
                return path
            if status in {"failed", "error", "cancelled", "expired"}:
                raise GatewayError("video_generation_failed", error or "视频生成失败。", 502)
            await asyncio.sleep(POLL_INTERVAL)
        raise GatewayError("video_timeout", "等待视频生成超时，请到平台任务记录查看状态。", 504)

    @staticmethod
    def _status(_result):
        return "", None, None


class PuterVideoBackend(_HTTPBackend):
    """Use Puter's documented ``drivers/call`` interface with a local token.

    Puter.js sends both a bearer header and the same token in the JSON driver
    envelope.  Keeping that envelope here means the local service behaves like
    the official SDK without needing a browser tab or a Puter filesystem
    mount.  The driver returns a ready media Blob, so there is no task polling
    or duplicate submission after a timeout.
    """

    BASE_URL = PUTER_BASE_URL
    ORIGIN = "https://puter.work"
    MAX_RESPONSE_BYTES = max(MAX_VIDEO_BYTES, MAX_IMAGE_BYTES)

    def __init__(self, config: dict, timeout: int):
        super().__init__(config, timeout)
        self.base_url = self.BASE_URL
        self.token = str(config.get("credential", config.get("api_key", ""))).strip()
        if not self.token:
            raise GatewayError(
                "puter_not_configured", "请先填写 Puter auth_token_v2。", 409
            )

    @staticmethod
    def _safe_error_message(raw: bytes) -> str:
        try:
            value = json.loads(raw.decode("utf-8", "replace"))
        except (TypeError, ValueError, UnicodeError):
            return ""
        if not isinstance(value, dict):
            return ""
        for key in ("message", "error", "reason"):
            candidate = value.get(key)
            if isinstance(candidate, dict):
                candidate = candidate.get("message") or candidate.get("reason")
            if isinstance(candidate, str) and candidate.strip():
                text = " ".join(candidate.split())[:500]
                if "auth_token" in text.lower() or "bearer" in text.lower():
                    return "Puter 认证令牌无效或已过期。"
                return text
        return ""

    @classmethod
    def _raise_http_error(cls, status: int, raw: bytes):
        message = cls._safe_error_message(raw)
        if status in {401, 403}:
            raise GatewayError(
                "puter_authentication_failed",
                message or "Puter 登录令牌无效或已过期，请重新复制 auth_token_v2。",
                401,
            )
        if status in {402, 429} or any(
            word in message.lower()
            for word in ("credit", "quota", "insufficient", "funds", "额度", "余额")
        ):
            raise GatewayError(
                "puter_quota_exceeded",
                "Puter 账户余额、额度或共享接口频率限制不足。",
                429,
            )
        raise GatewayError(
            "puter_upstream_error",
            message or f"Puter 请求失败（HTTP {status}）。",
            502,
        )

    def _call(self, interface: str, driver: str, args: dict, *, media: str):
        envelope = {
            "interface": interface,
            "driver": driver,
            "test_mode": False,
            "method": "generate",
            "args": args,
            "auth_token": self.token,
        }
        body = json.dumps(envelope, ensure_ascii=False, separators=(",", ":")).encode(
            "utf-8"
        )
        request = urllib.request.Request(
            self.base_url.rstrip("/") + "/drivers/call",
            data=body,
            headers={
                "Accept": "image/*,video/*,application/octet-stream,application/json",
                "Authorization": f"Bearer {self.token}",
                "Content-Type": "text/plain;actually=json",
                "Origin": self.ORIGIN,
                "Referer": self.ORIGIN + "/",
                "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 Chrome/131 Safari/537.36",
            },
            method="POST",
        )
        limit = MAX_IMAGE_BYTES if media == "image" else MAX_VIDEO_BYTES
        try:
            with urllib.request.urlopen(request, timeout=self.timeout) as response:
                content_type = (response.headers.get("Content-Type") or "").split(
                    ";", 1
                )[0].strip().lower()
                raw = response.read(limit + 1)
                if len(raw) > limit:
                    raise GatewayError(
                        "puter_result_too_large",
                        "Puter 返回的媒体超过本地保存上限。",
                        413,
                    )
                return content_type, raw
        except GatewayError:
            raise
        except urllib.error.HTTPError as exc:
            raw = exc.read(128 * 1024)
            self._raise_http_error(exc.code, raw)
        except (urllib.error.URLError, TimeoutError, OSError):
            raise GatewayError(
                "puter_network_error", "Puter 暂时无法连接，请稍后重试。", 502
            ) from None

    @staticmethod
    def _is_media(raw: bytes, content_type: str, media: str) -> bool:
        if media == "image":
            return _image_suffix(raw, content_type) is not None
        return (len(raw) >= 12 and raw[4:8] == b"ftyp") or raw.startswith(
            b"\x1a\x45\xdf\xa3"
        ) or content_type.startswith("video/")

    @classmethod
    def _json_media_value(cls, raw: bytes):
        try:
            value = json.loads(raw.decode("utf-8", "replace"))
        except (TypeError, ValueError, UnicodeError):
            return None
        if isinstance(value, dict) and value.get("success") is False:
            message = cls._safe_error_message(raw)
            if any(word in message.lower() for word in ("credit", "quota", "funds", "余额", "额度")):
                raise GatewayError("puter_quota_exceeded", "Puter 账户余额或额度不足。", 429)
            raise GatewayError("puter_upstream_error", message or "Puter 驱动请求失败。", 502)

        def walk(item):
            if isinstance(item, str):
                return item if item.startswith(("https://", "data:")) else None
            if isinstance(item, dict):
                for key in ("url", "asset_url", "href", "data", "result"):
                    if key in item:
                        found = walk(item[key])
                        if found:
                            return found
            if isinstance(item, list):
                for child in item:
                    found = walk(child)
                    if found:
                        return found
            return None

        return walk(value)

    async def _materialize(self, content_type: str, raw: bytes, folder: Path, prefix: str, media: str):
        if not raw:
            raise GatewayError("puter_empty_result", "Puter 没有返回媒体结果。", 502)
        if self._is_media(raw, content_type, media):
            suffix = _image_suffix(raw, content_type) if media == "image" else ".mp4"
            path = folder / f"{prefix}-{uuid.uuid4().hex}{suffix}"
            path.write_bytes(raw)
            return path
        value = self._json_media_value(raw)
        if not value:
            raise GatewayError("puter_invalid_response", "Puter 返回了无法识别的媒体结果。", 502)
        if value.startswith("data:"):
            header, encoded = value.split(",", 1)
            try:
                decoded = base64.b64decode(encoded, validate=True)
            except (ValueError, binascii.Error):
                raise GatewayError("puter_invalid_response", "Puter 返回了无效媒体数据。", 502) from None
            return await self._materialize(header.split(";", 1)[0][5:], decoded, folder, prefix, media)
        parsed = urllib.parse.urlsplit(value)
        if parsed.scheme != "https" or not parsed.hostname:
            raise GatewayError("puter_invalid_url", "Puter 返回了无效媒体地址。", 502)
        path = folder / f"{prefix}-{uuid.uuid4().hex}.{'png' if media == 'image' else 'mp4'}"
        await asyncio.to_thread(self._download, value, path)
        return path

    @staticmethod
    def _ratio_dimensions(resolution: str, aspect_ratio: str) -> tuple[int, int] | None:
        short = {"480P": 480, "540P": 540, "720P": 720, "768P": 768, "1080P": 1080, "2K": 1080}.get(resolution)
        if not short:
            return None
        ratios = {
            "21:9": (21, 9),
            "16:9": (16, 9),
            "4:3": (4, 3),
            "1:1": (1, 1),
            "3:4": (3, 4),
            "9:16": (9, 16),
        }
        width_ratio, height_ratio = ratios.get(aspect_ratio, (16, 9))
        if width_ratio >= height_ratio:
            return round(short * width_ratio / height_ratio), short
        return short, round(short * height_ratio / width_ratio)

    @staticmethod
    def _image_provider(model: str) -> str | None:
        lowered = model.lower()
        if lowered.startswith("seedream-"):
            return "byteplus"
        if lowered.startswith("gpt-image-"):
            return "openai"
        if lowered.startswith("gemini-"):
            return "gemini"
        if lowered.startswith("kling-"):
            return "kling"
        return None

    async def generate(self, request, folder: Path, reference_paths):
        model = request.model if request.model != "default" else self.config["default_model"]
        if model not in PUTER_VIDEO_MODELS:
            raise GatewayError("invalid_model", "Puter 视频模型不在当前支持列表中。", 422)
        refs = list(reference_paths or [])
        if len(refs) > 2:
            raise GatewayError("reference_count", "Puter 视频当前最多支持首尾 2 张参考图。", 422)
        args = {
            "prompt": request.prompt,
            "model": model,
            "seconds": request.duration,
            "size": request.resolution.lower(),
            "generate_audio": request.audio,
        }
        if dimensions := self._ratio_dimensions(request.resolution, request.aspect_ratio):
            args["width"], args["height"] = dimensions
        if len(refs) == 1:
            args["input_reference"] = self._data_uri(refs[0])
        elif len(refs) == 2:
            args["input_reference"] = self._data_uri(refs[0])
            args["last_frame"] = self._data_uri(refs[1])
        content_type, raw = await asyncio.to_thread(
            self._call, "puter-video-generation", "ai-video", args, media="video"
        )
        return await self._materialize(content_type, raw, folder, "puter", "video")

    async def generate_image(self, request, folder: Path, reference_paths):
        model = request.model if request.model != "default" else self.config["image_default_model"]
        if model not in PUTER_IMAGE_MODELS:
            raise GatewayError("invalid_model", "Puter 图片模型不在当前支持列表中。", 422)
        refs = list(reference_paths or [])
        args = {"prompt": request.render_prompt(has_references=bool(refs)), "model": model}
        if provider := self._image_provider(model):
            args["provider"] = provider
        if refs:
            args["input_images"] = [self._data_uri(path) for path in refs]
        if request.aspect_ratio:
            args["aspect_ratio"] = request.aspect_ratio
        if request.size in {"1K", "2K", "4K"}:
            args["quality"] = request.size
        paths = []
        for _ in range(request.n):
            content_type, raw = await asyncio.to_thread(
                self._call, "puter-image-generation", "ai-image", args, media="image"
            )
            paths.append(
                await self._materialize(content_type, raw, folder, "puter", "image")
            )
        return paths


class PollinationsImageBackend:
    """Unauthenticated public Pollinations image endpoint.

    The public endpoint currently exposes Sana without an account.  It is a
    shared service and may rate-limit; it intentionally accepts no local
    references so an image is never uploaded to a public URL by accident.
    """

    def __init__(self, config: dict, timeout: int):
        self.config = config
        self.timeout = timeout

    @staticmethod
    def _dimensions(request):
        ratio = request.aspect_ratio or request.composition() or "1:1"
        width, height = {
            "1:1": (1024, 1024),
            "16:9": (1280, 720),
            "9:16": (720, 1280),
            "4:3": (1024, 768),
            "3:4": (768, 1024),
            "3:2": (1152, 768),
            "2:3": (768, 1152),
        }.get(ratio, (1024, 1024))
        return width, height

    def _request(self, prompt: str, model: str, request):
        width, height = self._dimensions(request)
        query = urllib.parse.urlencode(
            {
                "model": model,
                "width": width,
                "height": height,
                "nologo": "true",
            }
        )
        url = POLLINATIONS_BASE_URL + "/prompt/" + urllib.parse.quote(prompt, safe="") + "?" + query
        req = urllib.request.Request(
            url,
            headers={
                "Accept": "image/*",
                "User-Agent": "Web2API-Studio/1.0",
            },
            method="GET",
        )
        try:
            with urllib.request.urlopen(req, timeout=self.timeout) as response:
                raw = response.read(MAX_IMAGE_BYTES + 1)
                if len(raw) > MAX_IMAGE_BYTES:
                    raise GatewayError("pollinations_result_too_large", "Pollinations 返回图片超过本地保存上限。", 413)
                return raw
        except GatewayError:
            raise
        except urllib.error.HTTPError as exc:
            if exc.code == 429:
                raise GatewayError("pollinations_rate_limited", "Pollinations 公共接口当前限流，请稍后重试。", 429) from None
            if exc.code in {401, 403}:
                raise GatewayError(
                    "pollinations_auth_required",
                    "Pollinations 新版接口需要 API Key；本地免费连接器只使用旧版匿名 Sana 入口，未提交付费请求。",
                    401,
                ) from None
            raise GatewayError("pollinations_upstream_error", f"Pollinations 请求失败（HTTP {exc.code}）。", 502) from None
        except (urllib.error.URLError, TimeoutError, OSError):
            raise GatewayError("pollinations_network_error", "Pollinations 公共接口暂时无法连接。", 502) from None

    async def generate(self, request, folder: Path, remaining: int, reference_paths=None):
        if reference_paths:
            raise GatewayError("pollinations_references_unsupported", "Pollinations 公共 Sana 接口不接收本地参考图。", 422)
        model = request.model if request.model != "default" else "sana"
        if model not in POLLINATIONS_MODELS:
            raise GatewayError("invalid_model", "未认证 Pollinations 公共接口当前只提供 Sana。", 422)
        saved = []
        for _ in range(max(1, min(int(remaining), 4))):
            raw = await asyncio.to_thread(self._request, request.render_prompt(), model, request)
            suffix = _image_suffix(raw)
            if suffix is None:
                raise GatewayError("pollinations_invalid_response", "Pollinations 返回的文件不是图片。", 502)
            path = folder / f"pollinations-{uuid.uuid4().hex}{suffix}"
            path.write_bytes(raw)
            saved.append(path)
        return saved

    async def generate_image(self, request, folder: Path, reference_paths=None):
        return await self.generate(request, folder, request.n, reference_paths)


class MiniMaxVideoBackend(_HTTPBackend):
    async def generate(self, request, folder, reference_paths):
        model = request.model if request.model != "default" else self.config["default_model"]
        content = [{"type": "text", "text": request.prompt}]
        if len(reference_paths) <= 2:
            roles = ["first_frame", "last_frame"]
            for index, path in enumerate(reference_paths):
                content.append({"type": "image_url", "image_url": {"url": self._data_uri(path)}, "role": roles[index]})
        else:
            for path in reference_paths:
                content.append({"type": "image_url", "image_url": {"url": self._data_uri(path)}, "role": "reference_image"})
        resolution_value = request.resolution.upper()
        resolution = {"720P": "768P", "1080P": "2K"}.get(resolution_value, resolution_value)
        payload = {
            "model": model,
            "content": content,
            "resolution": resolution,
            "duration": request.duration,
            "ratio": "adaptive" if reference_paths else request.aspect_ratio,
            "aigc_watermark": False,
        }
        if request.audio:
            payload["extra"] = {"prompt_expansion_mode": "balanced"} if model == "MiniMax-H3-Max" else None
        payload = {key: value for key, value in payload.items() if value is not None}
        _, raw = await asyncio.to_thread(
            self._request,
            "POST",
            self.config["base_url"].rstrip("/") + "/v2/video_generation",
            payload,
            {"Authorization": f"Bearer {self.config['api_key']}"},
        )
        task_id = _safe_json(raw).get("task_id")
        if not task_id:
            raise GatewayError("video_invalid_response", "MiniMax 没有返回任务 ID。", 502)
        return await self._poll_download(
            lambda: _safe_json(self._request("GET", self.config["base_url"].rstrip("/") + f"/v2/query/video_generation/{task_id}", headers={"Authorization": f"Bearer {self.config['api_key']}"})[1]),
            folder,
            "minimax",
        )

    @staticmethod
    def _status(result):
        task = result.get("task") or {}
        return task.get("status", ""), (task.get("content") or {}).get("url"), (task.get("error") or {}).get("message")


class ViduVideoBackend(_HTTPBackend):
    async def credits(self):
        """Return Vidu API credit packages without exposing the API key."""
        _, raw = await asyncio.to_thread(
            self._request,
            "GET",
            self.config["base_url"].rstrip("/") + "/ent/v2/credits",
            headers={"Authorization": f"Token {self.config['api_key']}"},
        )
        value = _safe_json(raw)
        remains = value.get("remains") if isinstance(value.get("remains"), list) else []
        packages = value.get("packages") if isinstance(value.get("packages"), list) else []
        # Keep only documented balance fields.  This prevents an upstream
        # response from echoing arbitrary account metadata into the local UI.
        safe_remains = [
            {
                key: item[key]
                for key in ("type", "credit_remain", "concurrency_limit", "current_concurrency", "queue_count")
                if isinstance(item, dict) and key in item
            }
            for item in remains
        ]
        safe_packages = [
            {
                key: item[key]
                for key in ("id", "name", "type", "concurrency", "credit_amount", "credit_remain", "created_at", "purchase_at", "valid_from", "valid_to")
                if isinstance(item, dict) and key in item
            }
            for item in packages
        ]
        return {
            "provider": "vidu",
            "credits": {"remains": safe_remains, "packages": safe_packages},
        }

    async def generate(self, request, folder, reference_paths):
        model = request.model if request.model != "default" else self.config["default_model"]
        images = [self._data_uri(path) for path in reference_paths]
        resolution = request.resolution.lower()
        if resolution in {"768p", "2k"}:
            resolution = "1080p"
        # Vidu calls this value ``auto`` when the caller asks the service to
        # infer a ratio from the reference image.  The gateway UI uses
        # ``adaptive`` so the same request shape can be shared with other
        # providers.
        aspect_ratio = "auto" if request.aspect_ratio == "adaptive" else request.aspect_ratio
        if model not in VIDU_Q2_MODELS and model not in VIDU_Q3_MODELS:
            raise GatewayError("invalid_model", "Vidu Q2/Q3 模型不在当前支持列表中。", 422)

        def reference_payload():
            # Vidu's subject-reference endpoint uses @name markers.  Add
            # neutral markers only when the caller did not provide any, so a
            # normal image-to-video prompt remains useful without platform
            # syntax knowledge.  The q2/q3 turbo subject variants use the
            # documented temporary-subject shape; the other reference models
            # accept the direct multi-image form.
            prompt = request.prompt
            if "@" not in prompt:
                prompt = " ".join(
                    f"[@{index}]" for index in range(1, len(images) + 1)
                ) + " " + prompt
            payload = {
                "model": model,
                "prompt": prompt,
                "audio": request.audio,
                "duration": request.duration,
                "aspect_ratio": aspect_ratio,
                "resolution": resolution,
            }
            if model in VIDU_SUBJECT_REFERENCE_MODELS:
                payload["subjects"] = [
                    {"name": str(i + 1), "images": [image]}
                    for i, image in enumerate(images[:7])
                ]
            else:
                payload["images"] = images[0] if len(images) == 1 else images[:7]
            return payload

        if len(images) > 2:
            if model not in VIDU_REFERENCE_MODELS:
                raise GatewayError(
                    "vidu_reference_unsupported",
                    f"Vidu 模型 {model} 的图生视频最多支持 2 张首尾帧；多主体参考请选择 Q2/Q3 主体模型。",
                    422,
                )
            payload = reference_payload()
            endpoint = "/ent/v2/reference2video"
        elif len(images) == 2:
            if model in VIDU_REFERENCE_ONLY_MODELS:
                payload = reference_payload()
                endpoint = "/ent/v2/reference2video"
            elif model not in VIDU_Q2_START_END_MODELS | VIDU_Q3_START_END_MODELS:
                raise GatewayError(
                    "vidu_reference_unsupported",
                    f"Vidu 模型 {model} 不支持首尾帧接口。",
                    422,
                )
            else:
                payload = {
                    "model": model,
                    "images": images,
                    "prompt": request.prompt,
                    "audio": request.audio,
                    "duration": request.duration,
                    "aspect_ratio": aspect_ratio,
                    "resolution": resolution,
                }
                endpoint = "/ent/v2/start-end2video"
        elif len(images) == 1:
            if model in VIDU_REFERENCE_ONLY_MODELS:
                payload = reference_payload()
                endpoint = "/ent/v2/reference2video"
            elif model not in VIDU_Q2_IMAGE_MODELS | VIDU_Q3_IMAGE_MODELS:
                raise GatewayError(
                    "vidu_reference_unsupported",
                    f"Vidu 模型 {model} 不支持单图生视频接口。",
                    422,
                )
            else:
                # Vidu's API is inconsistent here: Q2 Turbo documents a
                # one-item array, while Q2 Pro and the Q3 image endpoints use
                # a single data URI string in their official examples.
                image_value = images if model == "viduq2-turbo" else images[0]
                payload = {
                    "model": model,
                    "images": image_value,
                    "prompt": request.prompt,
                    "audio": request.audio,
                    "duration": request.duration,
                    "aspect_ratio": aspect_ratio,
                    "resolution": resolution,
                }
                endpoint = "/ent/v2/img2video"
        else:
            text_models = VIDU_Q2_TEXT_MODELS | VIDU_Q3_TEXT_MODELS
            if model not in text_models:
                raise GatewayError(
                    "vidu_text_unsupported",
                    f"Vidu 模型 {model} 没有文生视频接口，请上传参考图或选择 Q2/Q3 文生模型。",
                    422,
                )
            payload = {
                "model": model,
                "prompt": request.prompt,
                "audio": request.audio,
                "duration": request.duration,
                "aspect_ratio": aspect_ratio,
                "resolution": resolution,
            }
            endpoint = "/ent/v2/text2video"
        _, raw = await asyncio.to_thread(self._request, "POST", self.config["base_url"].rstrip("/") + endpoint, payload, {"Authorization": f"Token {self.config['api_key']}"})
        task_id = _safe_json(raw).get("task_id")
        if not task_id:
            raise GatewayError("video_invalid_response", "Vidu 没有返回任务 ID。", 502)
        return await self._poll_download(
            lambda: _safe_json(self._request("GET", self.config["base_url"].rstrip("/") + f"/ent/v2/tasks/{task_id}/creations", headers={"Authorization": f"Token {self.config['api_key']}"})[1]),
            folder,
            "vidu",
        )

    @staticmethod
    def _status(result):
        creations = result.get("creations") or []
        first = creations[0] if creations else {}
        return result.get("state", ""), first.get("url"), result.get("err_code")


class ViduWebVideoBackend(_HTTPBackend):
    """Use the Vidu China web task service with the user's web Cookie.

    The web application talks to ``service.vidu.cn`` directly.  This adapter
    keeps that connection local and deliberately does not use a browser, an
    API key, or an arbitrary user supplied base URL.  The web account's free
    buckets are checked before a task is submitted; an unclassified balance is
    treated as unsafe and therefore never submitted.
    """

    BASE_URL = VIDU_WEB_BASE_URL
    ORIGIN = "https://www.vidu.cn"
    USER_AGENT = (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
        "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0 Safari/537.36"
    )
    RUNNING_STATES = {
        "created",
        "pending",
        "queued",
        "queueing",
        "waiting",
        "preparation",
        "scheduling",
        "processing",
        "running",
        "generating",
        "in_progress",
    }
    SUCCESS_STATES = {"success", "succeeded", "completed", "done", "finished"}
    FAILED_STATES = {
        "failed",
        "failure",
        "error",
        "canceled",
        "cancelled",
        "expired",
        "rejected",
    }
    QUOTA_WORDS = (
        "quota",
        "credit",
        "credits",
        "insufficient",
        "limit",
        "余额",
        "积分",
        "额度",
        "上限",
        "次数",
    )

    def __init__(self, config: dict, timeout: int):
        super().__init__(config, timeout)
        self.base_url = self.BASE_URL
        cookie = str(config.get("credential", config.get("api_key", ""))).strip()
        if cookie.lower().startswith("cookie:"):
            cookie = cookie.split(":", 1)[1].strip()
        if not cookie:
            raise GatewayError("video_provider_not_configured", "请先填写 Vidu 中国站完整 Cookie Header。", 409)
        self.cookie = cookie

    @staticmethod
    def _json_value(raw: bytes):
        try:
            return json.loads(raw.decode("utf-8", "replace"))
        except (TypeError, ValueError, UnicodeDecodeError):
            return None

    @classmethod
    def _text(cls, value) -> str:
        if isinstance(value, str):
            return " ".join(value.split())[:500]
        if isinstance(value, dict):
            for key in ("message", "msg", "detail", "error", "reason", "description"):
                text = cls._text(value.get(key))
                if text:
                    return text
        if isinstance(value, list):
            for item in value:
                text = cls._text(item)
                if text:
                    return text
        return ""

    @classmethod
    def _raise_upstream(cls, value, http_status: int | None = None):
        message = cls._text(value)
        lower = message.lower()
        if http_status in {401, 403} or any(
            word in lower
            for word in ("login", "unauthorized", "authentication", "token", "登录", "认证", "cookie")
        ):
            raise GatewayError(
                "vidu_web_authentication_failed",
                "Vidu 网页 Cookie 无效或已失效，请在 Vidu 中国站完成登录后重新复制完整 Cookie Header。",
                401,
            )
        if http_status == 429 or any(word in lower for word in cls.QUOTA_WORDS):
            raise GatewayError(
                "vidu_web_quota_exceeded",
                "Vidu 网页账号的免费额度不足或已达到生成限制。",
                429,
            )
        raise GatewayError(
            "video_upstream_error",
            "Vidu 网页请求失败。" + (f"（HTTP {http_status}）" if http_status else ""),
            502,
        )

    @classmethod
    def _business_data(cls, value):
        """Reject an error envelope while preserving the safe response value."""
        if not isinstance(value, (dict, list)):
            raise GatewayError("video_invalid_response", "Vidu 网页返回了无效响应。", 502)
        if isinstance(value, dict):
            code = value.get("code", value.get("status_code", value.get("err_code")))
            success = value.get("success")
            if success is False:
                cls._raise_upstream(value)
            if code not in (None, "", 0, "0", 200, "200", True):
                # Some responses use HTTP-like status codes and some return a
                # string such as ``ok``.  Only numeric non-zero codes are
                # treated as an error here.
                try:
                    numeric = int(code)
                except (TypeError, ValueError):
                    numeric = None
                if numeric is not None and numeric not in {0, 200}:
                    cls._raise_upstream(value)
        return value

    def _headers(self, extra: dict | None = None, cookie: bool = True) -> dict:
        headers = {
            "Accept": "application/json, text/plain, */*",
            "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8",
            "Origin": self.ORIGIN,
            "Referer": self.ORIGIN + "/",
            "X-Platform": "web",
            "User-Agent": self.USER_AGENT,
        }
        if cookie:
            headers["Cookie"] = self.cookie
        headers.update(extra or {})
        return headers

    def _web_request(self, method: str, path: str, payload=None, headers=None):
        if path.startswith("http://") or path.startswith("https://"):
            url = path
        else:
            url = self.base_url.rstrip("/") + "/" + path.lstrip("/")
        parsed = urllib.parse.urlsplit(url)
        if parsed.scheme != "https" or parsed.hostname != urllib.parse.urlsplit(self.BASE_URL).hostname:
            raise GatewayError("video_invalid_url", "Vidu 网页请求地址不受支持。", 502)
        body = None
        request_headers = self._headers(headers)
        # The first-party web client adds a request ID to mutating requests.
        # It is an ordinary correlation header, not a signature or challenge
        # token, and helps the service correlate retries without changing the
        # account or quota semantics.
        if method.upper() in {"POST", "PATCH"}:
            request_headers.setdefault("X-Request-Id", str(uuid.uuid4()))
        if payload is not None:
            body = json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
            request_headers["Content-Type"] = "application/json"
        request = urllib.request.Request(url, data=body, headers=request_headers, method=method)
        try:
            with urllib.request.urlopen(request, timeout=self.timeout) as response:
                raw = response.read(16 * 1024 * 1024)
                value = self._json_value(raw)
                if response.status == 204 and value is None:
                    return response.status, {}
                if value is None:
                    body_text = raw.decode("utf-8", "replace")[:4000].lower()
                    if any(word in body_text for word in ("captcha", "waf", "login", "登录", "验证", "challenge")):
                        self._raise_upstream(body_text, response.status)
                    raise GatewayError("video_invalid_response", "Vidu 网页返回了无效响应。", 502)
                return response.status, self._business_data(value)
        except GatewayError:
            raise
        except urllib.error.HTTPError as exc:
            try:
                detail = self._json_value(exc.read(256 * 1024)) or {}
            except Exception:  # noqa: BLE001 - error bodies are not user output
                detail = {}
            self._raise_upstream(detail, exc.code)
        except (urllib.error.URLError, TimeoutError, OSError):
            raise GatewayError("video_network_error", "Vidu 网页服务暂时无法连接，请稍后重试。", 502) from None

    @staticmethod
    def _walk(value):
        if isinstance(value, dict):
            for key, item in value.items():
                yield str(key), item
                yield from ViduWebVideoBackend._walk(item)
        elif isinstance(value, list):
            for item in value:
                yield from ViduWebVideoBackend._walk(item)

    @staticmethod
    def _number(value):
        if isinstance(value, bool):
            return None
        try:
            number = float(value)
        except (TypeError, ValueError):
            return None
        if number != number or abs(number) == float("inf"):
            return None
        return int(number) if number.is_integer() else number

    @classmethod
    def _credit_snapshot(cls, values: list):
        free_values = []
        totals = []
        named = {}
        for value in values:
            for raw_key, raw_value in cls._walk(value):
                key = re.sub(r"[^a-z0-9]+", "_", raw_key.lower()).strip("_")
                number = cls._number(raw_value)
                if number is None:
                    continue
                named.setdefault(key, number)
                if any(word in key for word in ("free", "daily", "gift", "trial", "new_user", "newuser", "register", "bonus", "give")) and not any(word in key for word in ("used", "consumed", "spent", "cost", "amount_used")):
                    free_values.append(number)
                elif any(word in key for word in ("total", "balance", "available", "remain", "remaining", "left", "quota")):
                    totals.append(number)
        # A named free bucket is the only value allowed to authorize a task.
        # Do not infer a free balance from a generic ``balance`` field because
        # that field may include purchased credits.
        # Nested API responses often repeat the same bucket under ``data`` and
        # a display object.  Taking the largest explicitly named value avoids
        # turning a repeated balance into a falsely large free balance.
        free_available = max(free_values) if free_values else None
        total_available = max(totals) if totals else None
        return {
            "free_available": free_available,
            "total_available": total_available,
            "every_day_credit": named.get("every_day_credit", named.get("daily_credit")),
            "new_user_credit": named.get("new_user_credit", named.get("register_credit")),
            "trial_credit": named.get("trial_credit"),
            "free_buckets_known": bool(free_values),
        }

    async def credits(self):
        responses = []
        errors = []
        for endpoint in ("/credit/v1/credits/me", "/vidu/v1/tasks/quota"):
            try:
                _, value = await asyncio.to_thread(self._web_request, "GET", endpoint)
                responses.append(value)
            except GatewayError as error:
                # Keep authentication and network errors visible.  A missing
                # optional quota route alone should not hide the other route.
                errors.append(error)
        if not responses:
            if errors:
                raise errors[0]
            raise GatewayError("video_invalid_response", "Vidu 网页未返回额度。", 502)
        snapshot = self._credit_snapshot(responses)
        # The task quota route is a free web quota endpoint.  Some revisions
        # expose only ``remaining`` / ``left`` there and do not label the value
        # as ``free``; use those fields only from that route, never from the
        # general credits balance where they could include purchased credits.
        if not snapshot.get("free_buckets_known") and len(responses) > 1:
            quota_values = []
            for raw_key, raw_value in self._walk(responses[1]):
                key = re.sub(r"[^a-z0-9]+", "_", raw_key.lower()).strip("_")
                number = self._number(raw_value)
                if number is not None and any(word in key for word in ("remaining", "remain", "left", "quota")) and not any(word in key for word in ("total", "used", "consumed")):
                    quota_values.append(number)
            if quota_values:
                snapshot["free_available"] = max(quota_values)
                snapshot["free_buckets_known"] = True
        return {"provider": VIDU_WEB_ID, "credits": snapshot}

    async def _require_free_credit(self):
        result = await self.credits()
        credits = result.get("credits", {})
        if not credits.get("free_buckets_known"):
            raise GatewayError(
                "video_free_quota_unverified",
                "无法确认 Vidu 网页免费额度；为避免误扣付费 credits，本次未提交任务。",
                409,
            )
        available = self._number(credits.get("free_available"))
        if available is None or available <= 0:
            raise GatewayError(
                "video_quota_exceeded",
                "Vidu 网页免费额度为 0，本次未提交任务。",
                429,
            )

    @classmethod
    def _find_value(cls, value, names: tuple[str, ...], urls_only=False):
        wanted = {name.lower() for name in names}
        if isinstance(value, dict):
            for key, item in value.items():
                key_lower = str(key).lower()
                if key_lower in wanted or any(key_lower.endswith("_" + name) for name in wanted):
                    if urls_only:
                        if isinstance(item, str) and item.startswith("https://"):
                            return item
                    elif isinstance(item, (str, int, float, dict)):
                        return item
                found = cls._find_value(item, names, urls_only)
                if found:
                    return found
        elif isinstance(value, list):
            for item in value:
                found = cls._find_value(item, names, urls_only)
                if found:
                    return found
        return None

    @classmethod
    def _find_task_id(cls, value):
        return cls._find_value(value, ("task_id", "taskId", "id"))

    async def _upload(self, path: Path):
        raw = path.read_bytes()
        content_type = mimetypes.guess_type(path.name)[0] or "image/png"
        _, created = await asyncio.to_thread(
            self._web_request,
            "POST",
            "/tools/v1/files/uploads",
            {"metadata": {"image-height": "0", "image-width": "0"}, "scene": "vidu"},
        )
        upload_id = self._find_value(created, ("upload_id", "uploadId", "id"))
        put_url = self._find_value(
            created,
            ("put_url", "putUrl", "upload_url", "uploadUrl", "presigned_url", "presignedUrl"),
            urls_only=True,
        )
        if not upload_id or not put_url:
            raise GatewayError("video_upload_failed", "Vidu 网页没有返回参考图上传地址。", 502)
        upload_headers = self._find_value(created, ("headers", "upload_headers", "uploadHeaders"))
        if not isinstance(upload_headers, dict):
            upload_headers = {}
        upload_headers = {str(key): str(value) for key, value in upload_headers.items()}
        upload_headers.setdefault("Content-Type", content_type)
        parsed = urllib.parse.urlsplit(put_url)
        if parsed.scheme != "https" or not parsed.hostname:
            raise GatewayError("video_upload_failed", "Vidu 返回了无效的参考图上传地址。", 502)
        request = urllib.request.Request(put_url, data=raw, headers=upload_headers, method="PUT")
        try:
            with urllib.request.urlopen(request, timeout=self.timeout) as response:
                etag = response.headers.get("ETag") or response.headers.get("etag") or hashlib.md5(raw).hexdigest()
        except (urllib.error.HTTPError, urllib.error.URLError, TimeoutError, OSError):
            raise GatewayError("video_upload_failed", "Vidu 网页参考图上传失败。", 502) from None
        _, finished = await asyncio.to_thread(
            self._web_request,
            "PUT",
            f"/tools/v1/files/uploads/{urllib.parse.quote(str(upload_id), safe='')}/finish",
            {"id": str(upload_id), "etag": etag},
        )
        uri = self._find_value(finished, ("uri", "file_uri", "fileUri", "content", "url"), urls_only=True)
        if not uri:
            # Some versions return the upload URI from the create response and
            # only acknowledge it from the finish endpoint.
            uri = self._find_value(created, ("uri", "file_uri", "fileUri", "content", "url"), urls_only=True)
        if not uri:
            raise GatewayError("video_upload_failed", "Vidu 网页没有返回参考图 URI。", 502)
        return uri

    @classmethod
    def _task_status(cls, value):
        status = cls._find_value(value, ("status", "state", "task_status", "taskStatus")) or ""
        status = str(status).strip().lower().replace("-", "_").replace(" ", "_")
        error = cls._text(value)
        url = cls._find_value(
            value,
            ("video_url", "videoUrl", "download_url", "downloadUrl", "result_url", "resultUrl", "url"),
            urls_only=True,
        )
        return status, url, error

    async def generate(self, request, folder: Path, reference_paths):
        await self._require_free_credit()
        images = [await self._upload(path) for path in reference_paths]
        model = request.model if request.model != "default" else "vidu-web-q3"
        model_version = VIDU_WEB_MODEL_VERSIONS.get(model)
        if not model_version:
            raise GatewayError("invalid_model", "Vidu 网页 Q2/Q3 模型不在当前支持列表中。", 422)
        prompt = request.prompt
        prompts = [{"type": "text", "content": prompt}]
        if images:
            if len(images) > 2 and "[@" not in prompt:
                prompt = " ".join(f"[@{index}]" for index in range(1, len(images) + 1)) + " " + prompt
            prompts = [{"type": "text", "content": prompt}]
            for index, uri in enumerate(images, 1):
                prompts.append({"type": "image", "content": uri, "src_imgs": [uri], "name": str(index)})
        task_type = "character2video" if len(images) > 2 else "img2video" if images else "text2video"
        settings = {
            "model_version": model_version,
            "duration": request.duration,
            "resolution": request.resolution.lower(),
            "aspect_ratio": "" if request.aspect_ratio == "adaptive" else request.aspect_ratio,
            "sample_count": 1,
            "schedule_mode": "normal",
        }
        payload = {
            "type": task_type,
            "input": {"prompts": prompts, "editor_mode": "normal", "enhance": True},
            "settings": settings,
        }
        _, created = await asyncio.to_thread(self._web_request, "POST", "/vidu/v1/tasks", payload)
        task_id = self._find_task_id(created)
        if not task_id:
            raise GatewayError("video_invalid_response", "Vidu 网页没有返回任务 ID。", 502)
        deadline = time.monotonic() + self.timeout
        while time.monotonic() < deadline:
            _, detail = await asyncio.to_thread(
                self._web_request, "GET", f"/vidu/v1/tasks/{urllib.parse.quote(str(task_id), safe='')}"
            )
            status, url, message = self._task_status(detail)
            if status in self.SUCCESS_STATES and url:
                path = folder / f"vidu-web-{uuid.uuid4().hex}.mp4"
                await asyncio.to_thread(self._download, url, path)
                return path
            if status in self.FAILED_STATES:
                if any(word in message.lower() for word in self.QUOTA_WORDS):
                    raise GatewayError("video_quota_exceeded", "Vidu 网页免费额度不足或已达到生成限制。", 429)
                raise GatewayError("video_generation_failed", "Vidu 网页视频生成失败。", 502)
            await asyncio.sleep(min(POLL_INTERVAL, max(0.01, deadline - time.monotonic())))
        raise GatewayError("video_timeout", "等待 Vidu 网页视频生成超时，请到网页任务记录查看状态。", 504)

    @staticmethod
    def _status(result):
        return ViduWebVideoBackend._task_status(result)


class DashScopeVideoBackend(_HTTPBackend):
    async def generate(self, request, folder, reference_paths):
        model = request.model if request.model != "default" else self.config["default_model"]
        input_data = {"prompt": request.prompt}
        if reference_paths:
            input_data["img_url"] = self._data_uri(reference_paths[0])
        payload = {
            "model": model,
            "input": input_data,
            "parameters": {
                "resolution": request.resolution,
                "duration": request.duration,
                "audio": request.audio,
            },
        }
        _, raw = await asyncio.to_thread(self._request, "POST", self.config["base_url"].rstrip("/") + "/api/v1/services/aigc/video-generation/video-synthesis", payload, {"Authorization": f"Bearer {self.config['api_key']}", "X-DashScope-Async": "enable"})
        task_id = (_safe_json(raw).get("output") or {}).get("task_id")
        if not task_id:
            raise GatewayError("video_invalid_response", "百炼没有返回任务 ID。", 502)
        return await self._poll_download(
            lambda: _safe_json(self._request("GET", self.config["base_url"].rstrip("/") + f"/api/v1/tasks/{task_id}", headers={"Authorization": f"Bearer {self.config['api_key']}"})[1]),
            folder,
            "bailian",
        )

    @staticmethod
    def _status(result):
        output = result.get("output") or {}
        return output.get("task_status", "").lower(), output.get("video_url"), output.get("message")


class SeedanceVideoBackend(_HTTPBackend):
    async def generate(self, request, folder, reference_paths):
        model = request.model if request.model != "default" else self.config["default_model"]
        content = [{"type": "text", "text": request.prompt}]
        if len(reference_paths) == 1:
            content.append({"type": "image_url", "image_url": {"url": self._data_uri(reference_paths[0])}, "role": "first_frame"})
        elif len(reference_paths) == 2:
            for role, path in zip(("first_frame", "last_frame"), reference_paths):
                content.append({"type": "image_url", "image_url": {"url": self._data_uri(path)}, "role": role})
        elif reference_paths:
            for path in reference_paths:
                content.append({"type": "image_url", "image_url": {"url": self._data_uri(path)}, "role": "reference_image"})
        payload = {
            "model": model,
            "content": content,
            "duration": request.duration,
            "ratio": "16:9" if request.aspect_ratio == "adaptive" else request.aspect_ratio,
            "resolution": request.resolution.lower(),
            "generate_audio": request.audio,
            "watermark": False,
        }
        _, raw = await asyncio.to_thread(
            self._request,
            "POST",
            self.config["base_url"].rstrip("/") + "/contents/generations/tasks",
            payload,
            {"Authorization": f"Bearer {self.config['api_key']}"},
        )
        task_id = _safe_json(raw).get("id")
        if not task_id:
            raise GatewayError("video_invalid_response", "Seedance 没有返回任务 ID。", 502)
        return await self._poll_download(
            lambda: _safe_json(self._request("GET", self.config["base_url"].rstrip("/") + f"/contents/generations/tasks/{task_id}", headers={"Authorization": f"Bearer {self.config['api_key']}"})[1]),
            folder,
            "seedance",
        )

    @staticmethod
    def _status(result):
        return result.get("status", ""), result.get("video_url"), result.get("error", {}).get("message") if isinstance(result.get("error"), dict) else None


class HailuoWebVideoBackend(_HTTPBackend):
    """Call Hailuo's first-party web endpoints with a local web token.

    Hailuo's web client signs each request with a short-lived ``yy`` MD5
    value.  The token itself is never put in a URL, persisted in a job record,
    or included in an error.  This adapter intentionally uses only the
    account's ordinary web permissions; it does not solve CAPTCHA/WAF checks
    or grant a paid model access that the account does not have.
    """

    BASE_PATH = "https://hailuoai.video"
    APP_ID = "3001"
    VERSION_CODE = "22203"
    BIZ_ID = "0"
    LANG = "en"
    OS_NAME = "Mac OS"
    BROWSER_NAME = "Chrome"
    STATUS_CREATING = {1, 11, 12}
    STATUS_SUCCEEDED = {2}
    STATUS_FAILED = {3, 5, 6, 7, 8, 9, 10, 14, 16}

    def __init__(self, config: dict, timeout: int):
        super().__init__(config, timeout)
        # ``VideoConfigStore`` fixes web-token URLs to this first-party host.
        # Keep the guard here too so a hand-built config cannot redirect a
        # credential to an arbitrary endpoint.
        self.base_url = self.BASE_PATH

    @staticmethod
    def _encode_component(value: str) -> str:
        # JavaScript encodeURIComponent's safe character set.
        return urllib.parse.quote(str(value), safe="-_.!~*'()")

    def _public_params(self, timestamp: int) -> dict[str, str]:
        return {
            "device_platform": "web",
            "app_id": self.APP_ID,
            "version_code": self.VERSION_CODE,
            "biz_id": self.BIZ_ID,
            "unix": str(timestamp),
            "lang": self.LANG,
            "uuid": self.config.get("uuid") or "",
            "device_id": self.config.get("device_id") or "",
            "os_name": self.OS_NAME,
            "browser_name": self.BROWSER_NAME,
        }

    @classmethod
    def _signature(
        cls,
        path: str,
        params: dict[str, object] | None,
        method: str,
        payload: object | None,
        timestamp: int,
        config: dict | None = None,
    ) -> tuple[str, str, str]:
        """Return ``(url, yy, body_string)`` exactly as the web client does."""
        values = config or {
            "uuid": "",
            "device_id": "",
        }
        public = {
            "device_platform": "web",
            "app_id": cls.APP_ID,
            "version_code": cls.VERSION_CODE,
            "biz_id": cls.BIZ_ID,
            "unix": str(timestamp),
            "lang": cls.LANG,
            "uuid": values.get("uuid") or "",
            "device_id": values.get("device_id") or "",
            "os_name": cls.OS_NAME,
            "browser_name": cls.BROWSER_NAME,
        }
        # Axios merges the caller's params before its public parameters.  The
        # order matters because yy signs the complete query string.
        query = {**(params or {}), **public}
        encoded_query = urllib.parse.urlencode(
            [(key, value) for key, value in query.items() if value is not None]
        )
        path_with_query = path + ("?" + encoded_query if encoded_query else "")
        is_body_method = method.lower() in {"post", "put", "patch", "delete"}
        body_value = payload if payload is not None else {}
        body_string = (
            json.dumps(body_value, ensure_ascii=False, separators=(",", ":"))
            if is_body_method
            else "{}"
        )
        yy_input = (
            cls._encode_component(path_with_query)
            + "_"
            + body_string
            + hashlib.md5(str(timestamp).encode("utf-8")).hexdigest()
            + "ooui"
        )
        yy = hashlib.md5(yy_input.encode("utf-8")).hexdigest()
        return path_with_query, yy, body_string

    @staticmethod
    def _error_text(value) -> str:
        if isinstance(value, dict):
            for key in ("message", "msg", "detail", "error"):
                text = HailuoWebVideoBackend._error_text(value.get(key))
                if text:
                    return text
        elif isinstance(value, str):
            return " ".join(value.split())[:500]
        return ""

    @classmethod
    def _raise_status_error(cls, status_info: object, http_status: int | None = None):
        code = status_info.get("code") if isinstance(status_info, dict) else None
        message = cls._error_text(status_info)
        lower = message.lower()
        if http_status in {401, 403} or any(
            word in lower
            for word in ("login", "unauthorized", "token", "authentication", "登录", "认证")
        ):
            raise GatewayError(
                "video_authentication_failed",
                "Hailuo 网页 Token 无效或已失效，请从已登录网页重新复制 localStorage 的 _token。",
                401,
            )
        if http_status == 429 or any(
            word in lower
            for word in (
                "quota",
                "credit",
                "limit",
                "insufficient",
                "额度",
                "积分",
                "上限",
                "余额",
            )
        ):
            raise GatewayError(
                "video_quota_exceeded",
                "Hailuo 网页账号的免费额度或 credits 暂时不足。",
                429,
            )
        suffix = f"（平台代码 {code}）" if code not in (None, "", 0, "0") else ""
        raise GatewayError(
            "video_upstream_error",
            "Hailuo 网页请求失败" + suffix + "。",
            502,
        )

    def _request(
        self,
        method: str,
        path: str,
        payload=None,
        headers=None,
        params: dict[str, object] | None = None,
    ):
        timestamp = int(time.time() * 1000)
        path_with_query, yy, body_string = self._signature(
            path,
            params,
            method,
            payload,
            timestamp,
            self.config,
        )
        body = body_string.encode("utf-8") if method.lower() in {"post", "put", "patch", "delete"} else None
        request_headers = {
            "Accept": "application/json",
            "Origin": self.BASE_PATH,
            "Referer": self.BASE_PATH + "/",
            "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 Chrome/131 Safari/537.36",
            "token": self.config.get("credential", self.config.get("api_key", "")),
            "yy": yy,
        }
        if body is not None:
            request_headers["Content-Type"] = "application/json"
        request_headers.update(headers or {})
        request = urllib.request.Request(
            self.base_url + path_with_query,
            data=body,
            headers=request_headers,
            method=method,
        )
        try:
            with urllib.request.urlopen(request, timeout=self.timeout) as response:
                raw = response.read(16 * 1024 * 1024)
                return response.status, raw
        except urllib.error.HTTPError as exc:
            try:
                raw = exc.read(128 * 1024)
                detail = _safe_json(raw)
            except Exception:  # noqa: BLE001 - do not echo upstream bodies
                detail = {}
            self._raise_status_error(detail.get("statusInfo", detail), exc.code)
        except (urllib.error.URLError, TimeoutError, OSError):
            raise GatewayError(
                "video_network_error", "Hailuo 网页暂时无法连接，请稍后重试。", 502
            ) from None

    @classmethod
    def _unwrap(cls, raw: bytes) -> object:
        body = _safe_json(raw)
        status_info = body.get("statusInfo")
        if isinstance(status_info, dict) and str(status_info.get("code")) not in {
            "0",
            "None",
        }:
            cls._raise_status_error(status_info)
        return body.get("data", body)

    def _json_request(self, method, path, payload=None, params=None):
        _, raw = self._request(method, path, payload, params=params)
        return self._unwrap(raw)

    @staticmethod
    def _credit_number(value):
        """Return a finite numeric credit value without exposing raw account data."""
        if isinstance(value, bool):
            return None
        try:
            number = float(value)
        except (TypeError, ValueError):
            return None
        if not (number == number and abs(number) != float("inf")):
            return None
        return int(number) if number.is_integer() else number

    @classmethod
    def _credit_snapshot(cls, value):
        """Normalize Hailuo's credit response into safe balance fields.

        The web client distinguishes purchased credits from credits granted by a
        subscription or login.  Keep those buckets separate so a free-only
        request never falls through to a purchased balance.
        """
        if not isinstance(value, dict):
            return {
                "total_available": None,
                "user_buy": None,
                "subscribe_give": None,
                "login_give": None,
                "free_available": None,
                "free_buckets_known": False,
            }
        total = cls._credit_number(value.get("total_credit"))
        rows = value.get("sub_credits")
        buckets = {
            "user_buy": None,
            "subscribe_give": None,
            "login_give": None,
        }
        known = isinstance(rows, list)
        if known:
            for row in rows:
                if not isinstance(row, dict):
                    continue
                amount = cls._credit_number(row.get("credit"))
                credit_type = row.get("credit_type")
                if credit_type in {1, "1", "USER_BUY_CREDIT"}:
                    buckets["user_buy"] = amount
                elif credit_type in {2, "2", "SUBSCRIBE_GIVE_CREDIT"}:
                    buckets["subscribe_give"] = amount
                elif credit_type in {3, "3", "LOGIN_GIVE_CREDIT"}:
                    buckets["login_give"] = amount
        free_parts = [
            amount
            for amount in (buckets["subscribe_give"], buckets["login_give"])
            if amount is not None
        ]
        free_available = sum(free_parts) if free_parts else (0 if known else None)
        return {
            "total_available": total,
            **buckets,
            "free_available": free_available,
            "free_buckets_known": known and bool(free_parts or total in (0, None)),
        }

    async def credits(self):
        """Query Hailuo's first-party credit buckets without returning secrets."""
        data = await asyncio.to_thread(
            self._json_request, "GET", "/v1/api/billing/credit"
        )
        return {
            "provider": "hailuo-web",
            "credits": self._credit_snapshot(data),
        }

    async def _require_free_credit(self):
        result = await self.credits()
        credits = result.get("credits") or {}
        if not credits.get("free_buckets_known"):
            raise GatewayError(
                "video_free_quota_unverified",
                "Hailuo 未返回免费额度分类，为避免误扣付费 credits，本次未提交任务。",
                409,
            )
        if (credits.get("free_available") or 0) <= 0:
            raise GatewayError(
                "video_quota_exceeded",
                "Hailuo 网页账号的免费额度不足；购买 credits 不会被自动使用。",
                429,
            )
        return result

    @staticmethod
    def _first_value(value: object, keys: tuple[str, ...]):
        if isinstance(value, dict):
            for key in keys:
                item = value.get(key)
                if isinstance(item, (str, int)) and str(item):
                    return str(item)
            for item in value.values():
                found = HailuoWebVideoBackend._first_value(item, keys)
                if found:
                    return found
        elif isinstance(value, list):
            for item in value:
                found = HailuoWebVideoBackend._first_value(item, keys)
                if found:
                    return found
        return None

    @staticmethod
    def _download_url(value: object) -> str | None:
        if isinstance(value, list):
            for item in value:
                found = HailuoWebVideoBackend._download_url(item)
                if found:
                    return found
            return None
        if not isinstance(value, dict):
            return None
        # Prefer the same no-watermark URL selected by the Hailuo web page.
        for key in (
            "withoutWatermarkURL",
            "withoutWatermarkUrl",
            "downloadURLWithoutWatermark",
            "downloadUrlWithoutWatermark",
            "url",
            "downloadURL",
            "downloadUrl",
        ):
            item = value.get(key)
            if isinstance(item, str) and item.startswith("https://"):
                return item
            if isinstance(item, dict):
                found = HailuoWebVideoBackend._download_url(item)
                if found:
                    return found
        for item in value.values():
            found = HailuoWebVideoBackend._download_url(item)
            if found:
                return found
        return None

    @classmethod
    def _download_urls(cls, value: object) -> list[str]:
        """Collect distinct media URLs from a completed Hailuo feed.

        The image endpoint returns a list of generated assets while the video
        endpoint returns one ``downloadURL`` object.  Walking only URL-shaped
        fields keeps this tolerant of both response shapes without echoing the
        rest of the feed into the job record.
        """
        found: list[str] = []

        def visit(item):
            if isinstance(item, str):
                if item.startswith("https://") and item not in found:
                    found.append(item)
                return
            if isinstance(item, list):
                for child in item:
                    visit(child)
                return
            if not isinstance(item, dict):
                return
            preferred = (
                "withoutWatermarkURL",
                "withoutWatermarkUrl",
                "downloadURLWithoutWatermark",
                "downloadUrlWithoutWatermark",
                "url",
                "imageURL",
                "imageUrl",
                "downloadURL",
                "downloadUrl",
            )
            for key in preferred:
                if key in item:
                    visit(item[key])
            for key, child in item.items():
                if key not in preferred:
                    visit(child)

        visit(value)
        return found

    @staticmethod
    def _download_image(url: str, path: Path):
        request = urllib.request.Request(
            url,
            headers={
                "Accept": "image/avif,image/webp,image/png,image/jpeg,image/*;q=0.8",
            },
        )
        try:
            with urllib.request.urlopen(request, timeout=450) as response:
                contents = response.read(MAX_IMAGE_BYTES + 1)
        except (urllib.error.URLError, TimeoutError, OSError):
            raise GatewayError("image_download_failed", "Hailuo 图片结果下载失败。", 502) from None
        if len(contents) > MAX_IMAGE_BYTES:
            raise GatewayError("image_too_large", "Hailuo 图片超过本地保存上限。", 413)
        if not contents:
            raise GatewayError("image_download_failed", "Hailuo 返回了空图片。", 502)
        path.write_bytes(contents)

    @classmethod
    def _status(cls, result: object):
        if isinstance(result, dict) and isinstance(result.get("feed"), dict):
            result = result["feed"]
        if not isinstance(result, dict):
            return "", None, ""
        common = result.get("commonInfo") or {}
        raw_status = common.get("status", result.get("status", ""))
        try:
            numeric_status = int(raw_status)
        except (TypeError, ValueError):
            numeric_status = None
        if numeric_status in cls.STATUS_SUCCEEDED:
            status = "succeeded"
        elif numeric_status in cls.STATUS_FAILED:
            status = "failed"
        elif numeric_status in cls.STATUS_CREATING:
            status = "running"
        else:
            normalized = str(raw_status).strip().lower()
            status = {
                "success": "succeeded",
                "succeeded": "succeeded",
                "completed": "succeeded",
                "successed": "succeeded",
                "fail": "failed",
                "failed": "failed",
                "error": "failed",
                "cancelled": "failed",
                "canceled": "failed",
                "expired": "failed",
                "creating": "running",
                "processing": "running",
                "running": "running",
                "pending": "running",
            }.get(normalized, normalized)
        url = cls._download_url(result)
        error = cls._error_text(
            result.get("feedMessage")
            or common.get("message")
            or result.get("message")
            or result.get("error")
        )
        return status, url, error

    @staticmethod
    def _file_extension(path: Path) -> str:
        value = path.suffix.lstrip(".").lower()
        return value if value and value.isalnum() else "png"

    @staticmethod
    def _file_mime(path: Path) -> str:
        return mimetypes.guess_type(path.name)[0] or "image/png"

    async def _upload_reference(self, path: Path, frame_type: int) -> dict:
        try:
            policy = self._json_request("GET", "/v1/api/files/request_policy")
            if not isinstance(policy, dict):
                raise GatewayError("video_upload_failed", "Hailuo 没有返回文件上传策略。", 502)
            file_uuid = str(uuid.uuid4())
            extension = self._file_extension(path)
            mime_type = self._file_mime(path)
            contents = path.read_bytes()
            policy_payload = {
                "fileName": f"{file_uuid}.{extension}",
                "originFileName": path.name,
                "dir": policy.get("dir", ""),
                "endpoint": policy.get("endpoint", ""),
                "bucketName": policy.get("bucketName", ""),
                "size": str(len(contents)),
                "mimeType": mime_type,
                "fileMd5": hashlib.md5(contents).hexdigest(),
                "fileScene": 10,
                "durationMs": 0,
                "assetFileType": "image",
            }
            callback = self._json_request(
                "POST", "/v1/api/files/policy_callback", policy_payload
            )
            if not isinstance(callback, dict):
                raise GatewayError("video_upload_failed", "Hailuo 文件回调返回无效结果。", 502)
            file_id = self._first_value(callback, ("fileID", "fileId", "id"))
            oss_path = self._first_value(callback, ("ossPath", "oss_path", "path"))
            if not file_id or not oss_path:
                raise GatewayError("video_upload_failed", "Hailuo 没有返回可上传的文件信息。", 502)
            await asyncio.to_thread(self._put_oss, policy, oss_path, path)
            # Match the web application's fileList shape.  The aliases are
            # accepted by current Hailuo versions and make the adapter robust
            # across the old/new response field names.
            return {
                "id": file_id,
                "fileID": file_id,
                "url": oss_path,
                "name": path.name,
                "type": extension,
                "fileType": extension,
                "frameType": frame_type,
                "assetFileType": "image",
            }
        except GatewayError:
            raise
        except (OSError, ValueError, TypeError):
            raise GatewayError("video_upload_failed", "参考图上传到 Hailuo 失败。", 502) from None

    @staticmethod
    def _put_oss(policy: dict, oss_path: str, path: Path):
        try:
            import oss2
        except ImportError:
            raise GatewayError(
                "video_upload_dependency",
                "本地环境缺少 oss2 依赖，请重新运行安装脚本。",
                500,
            ) from None
        endpoint = str(policy.get("endpoint") or "").strip()
        bucket_name = str(policy.get("bucketName") or "").strip()
        access_key_id = str(policy.get("accessKeyId") or "").strip()
        access_key_secret = str(policy.get("accessKeySecret") or "").strip()
        security_token = str(policy.get("securityToken") or "").strip()
        if not all((endpoint, bucket_name, access_key_id, access_key_secret, security_token)):
            raise GatewayError("video_upload_failed", "Hailuo 上传策略不完整。", 502)
        if not endpoint.startswith("http"):
            endpoint = "https://" + endpoint
        auth = oss2.StsAuth(
            access_key_id,
            access_key_secret,
            security_token,
        )
        bucket = oss2.Bucket(auth, endpoint, bucket_name)
        result = bucket.put_object_from_file(str(oss_path).lstrip("/"), str(path))
        if getattr(result, "status", 200) not in {200, 201}:
            raise GatewayError("video_upload_failed", "Hailuo 对象存储上传失败。", 502)

    async def generate(self, request, folder: Path, reference_paths):
        await self._require_free_credit()
        model = request.model if request.model != "default" else self.config["default_model"]
        refs = list(reference_paths or [])
        if len(refs) == 1:
            frame_types = [0]
            reference_mode = "start-end-frames"
        elif len(refs) == 2:
            frame_types = [0, 1]
            reference_mode = "start-end-frames"
        elif refs:
            frame_types = [3] * len(refs)
            reference_mode = "all-reference"
        else:
            frame_types = []
            reference_mode = ""
        file_list = []
        for path, frame_type in zip(refs, frame_types):
            file_list.append(await self._upload_reference(Path(path), frame_type))
        resolution = request.resolution.upper()
        if model in {"hailuo3.0-t2v", "hailuo3.0-i2v"}:
            resolution = "1440" if resolution in {"1080P", "2K"} else "768"
        else:
            resolution = {"480P": "720", "540P": "720", "720P": "720", "768P": "768", "1080P": "1080", "2K": "1080"}.get(resolution, "720")
        payload = {
            "quantity": 1,
            "parameter": {
                "modelID": model,
                "desc": request.prompt,
                "fileList": file_list,
                "useOriginPrompt": True,
                "resolution": resolution,
                "duration": request.duration,
                "aspectRatio": request.aspect_ratio,
                "referenceMode": reference_mode,
            },
            "videoExtra": {},
            "projectID": "0",
        }
        result = self._json_request(
            "POST", "/v2/api/multimodal/generate/video", payload
        )
        task_id = self._first_value(
            result, ("id", "feedID", "feedId", "taskID", "taskId", "creationID")
        )
        if not task_id:
            raise GatewayError("video_invalid_response", "Hailuo 没有返回任务 ID。", 502)
        return await self._poll_hailuo(task_id, folder)

    @staticmethod
    def _image_resolution(value: str) -> str:
        normalized = str(value or "auto").strip().lower()
        return {
            "1k": "Low",
            "low": "Low",
            "2k": "Medium",
            "medium": "Medium",
            "4k": "High",
            "high": "High",
        }.get(normalized, "Low")

    async def generate_image(self, request, folder: Path, remaining: int, reference_paths=None):
        """Generate an image through Hailuo's first-party web image endpoint.

        Hailuo's image catalogue is the same web account catalogue shown in
        the site.  We query the free-credit buckets before submitting so a
        purchased balance is never silently consumed.
        """
        await self._require_free_credit()
        model = request.model if request.model != "default" else self.config.get(
            "image_default_model", HAILUO_IMAGE_MODELS[0]
        )
        if model not in HAILUO_IMAGE_MODELS:
            raise GatewayError("invalid_model", "Hailuo 网页图片模型不在当前模型列表中。", 422)
        refs = list(reference_paths or [])
        if len(refs) > 1:
            raise GatewayError("reference_count", "Hailuo 网页图片当前最多支持 1 张参考图。", 422)
        file_list = []
        if refs:
            file_list.append(await self._upload_reference(Path(refs[0]), 3))
        payload = {
            "quantity": max(1, min(int(remaining), 4)),
            "parameter": {
                "modelID": model,
                "desc": request.render_prompt(has_references=bool(refs)),
                "fileList": file_list,
                "useOriginPrompt": request.prompt_mode == "raw",
                "aspectRatio": request.aspect_ratio or "Auto",
                "resolution": self._image_resolution(request.size),
                "quality": "",
                "referenceMode": "image-reference" if refs else "",
            },
            "projectID": "0",
            "imageExtra": {},
        }
        result = self._json_request(
            "POST", "/v2/api/multimodal/generate/image", payload
        )
        task_id = self._first_value(
            result, ("id", "feedID", "feedId", "taskID", "taskId", "creationID")
        )
        if not task_id:
            raise GatewayError("image_invalid_response", "Hailuo 没有返回图片任务 ID。", 502)
        deadline = time.monotonic() + self.timeout
        while time.monotonic() < deadline:
            feed = self._json_request(
                "GET", "/api/feed/detail", params={"id": task_id}
            )
            status, first_url, error = self._status(feed)
            urls = self._download_urls(feed)
            if first_url and first_url not in urls:
                urls.insert(0, first_url)
            if status == "succeeded" and urls:
                paths = []
                for url in urls[:remaining]:
                    path = folder / f"hailuo-{uuid.uuid4().hex}.png"
                    await asyncio.to_thread(self._download_image, url, path)
                    paths.append(path)
                return paths
            if status == "failed":
                lower = (error or "").lower()
                if any(word in lower for word in ("quota", "credit", "limit", "额度", "积分", "上限")):
                    raise GatewayError(
                        "video_quota_exceeded",
                        "Hailuo 网页账号的免费额度或 credits 暂时不足。",
                        429,
                    )
                raise GatewayError("image_generation_failed", error or "Hailuo 图片生成失败。", 502)
            await asyncio.sleep(min(POLL_INTERVAL, max(0.01, deadline - time.monotonic())))
        raise GatewayError("image_timeout", "等待 Hailuo 图片生成超时。", 504)

    async def _poll_hailuo(self, task_id: str, folder: Path):
        deadline = time.monotonic() + self.timeout
        while time.monotonic() < deadline:
            result = self._json_request(
                "GET", "/api/feed/detail", params={"id": task_id}
            )
            status, url, error = self._status(result)
            if url:
                path = folder / f"hailuo-{uuid.uuid4().hex}.mp4"
                await asyncio.to_thread(self._download, url, path)
                return path
            if status == "failed":
                lower = (error or "").lower()
                if any(word in lower for word in ("quota", "credit", "limit", "额度", "积分", "上限")):
                    raise GatewayError(
                        "video_quota_exceeded",
                        "Hailuo 网页账号的免费额度或 credits 暂时不足。",
                        429,
                    )
                raise GatewayError("video_generation_failed", error or "Hailuo 视频生成失败。", 502)
            await asyncio.sleep(min(POLL_INTERVAL, max(0.01, deadline - time.monotonic())))
        raise GatewayError(
            "video_timeout", "等待 Hailuo 视频生成超时，请到网页任务记录查看状态。", 504
        )


class KlingWebVideoBackend(_HTTPBackend):
    """Use the normal Kling web session through its first-party endpoints.

    Kling's web app authenticates these requests with the browser's Cookie
    header.  The adapter accepts a user-provided Cookie header, uploads
    references through the same short-lived upload token used by the page,
    and submits the page's task object.  It deliberately does not solve a
    CAPTCHA, generate a device signature, or turn a paid model into a free
    one.  A normal web session that is challenged by Kling therefore returns a
    clear authentication/risk error.
    """

    BASE_URL = "https://klingai.com"
    WEB_URL = "https://klingai.com/app/video/new"
    USER_AGENT = (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
        "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/130.0.0.0 Safari/537.36"
    )
    AUTH_ERROR_WORDS = (
        "login",
        "unauthorized",
        "authentication",
        "cookie",
        "captcha",
        "verify",
        "risk",
        "登录",
        "认证",
        "验证码",
        "风控",
    )
    QUOTA_WORDS = (
        "point",
        "ticket",
        "quota",
        "credit",
        "limit",
        "insufficient",
        "额度",
        "积分",
        "余额",
        "上限",
    )
    TERMINAL_SUCCESS = {98, 99}
    TERMINAL_FAILURE = {3, 4, 6, 7, 9, 11, 12, 13, 14, 16, 50, 51, 52, 53, 54, 55, 56, 58, 61}

    def __init__(self, config: dict, timeout: int):
        super().__init__(config, timeout)
        self.base_url = self.BASE_URL
        self.cookie = str(config.get("credential") or config.get("api_key") or "").strip()
        if self.cookie.lower().startswith("cookie:"):
            self.cookie = self.cookie.split(":", 1)[1].strip()
        if "\r" in self.cookie or "\n" in self.cookie:
            raise GatewayError("video_authentication_failed", "Kling Cookie Header 格式无效。", 422)

    def _headers(self, content_type: str | None = None) -> dict[str, str]:
        headers = {
            "Accept": "application/json, text/plain, */*",
            "Cookie": self.cookie,
            "Origin": self.BASE_URL,
            "Referer": self.WEB_URL,
            "User-Agent": self.USER_AGENT,
        }
        if content_type:
            headers["Content-Type"] = content_type
        return headers

    @staticmethod
    def _walk(value):
        if isinstance(value, dict):
            yield value
            for item in value.values():
                yield from KlingWebVideoBackend._walk(item)
        elif isinstance(value, list):
            for item in value:
                yield from KlingWebVideoBackend._walk(item)

    @classmethod
    def _first(cls, value, keys):
        if isinstance(value, dict):
            for key in keys:
                found = value.get(key)
                if found not in (None, ""):
                    return found
        for item in cls._walk(value):
            for key in keys:
                found = item.get(key)
                if found not in (None, ""):
                    return found
        return None

    @classmethod
    def _error_message(cls, value) -> str:
        found = cls._first(value, ("message", "msg", "detail", "brief", "error"))
        if isinstance(found, dict):
            found = cls._first(found, ("message", "msg", "detail", "brief"))
        return " ".join(str(found or "").split())[:500]

    @classmethod
    def _raise_upstream_error(cls, value, http_status: int | None = None):
        message = cls._error_message(value)
        lower = message.lower()
        error_type = cls._first(value, ("type", "errorType", "error_type"))
        if (
            http_status in {401, 403}
            or str(error_type or "").upper().startswith("AUTH.")
            or str(error_type or "").upper() in {"MID.S4B", "USER.NOT_LOGIN"}
            or any(word in lower for word in cls.AUTH_ERROR_WORDS)
        ):
            raise GatewayError(
                "video_authentication_failed",
                "Kling 网页会话无效，或被网页登录/验证码/风控拦截；请先在 Kling 网页正常完成验证后重新复制 Cookie Header。",
                401,
            )
        if http_status == 429 or any(word in lower for word in cls.QUOTA_WORDS):
            raise GatewayError(
                "video_quota_exceeded",
                "Kling 网页账号的免费 points/tickets 不足或已达到生成限制。",
                429,
            )
        raise GatewayError("video_upstream_error", message or "Kling 网页请求失败。", 502)

    def _web_request(self, method: str, path: str, payload=None, params=None):
        query = urllib.parse.urlencode(
            [(key, value) for key, value in (params or {}).items() if value is not None]
        )
        url = self.base_url.rstrip("/") + path
        if query:
            url += ("&" if "?" in url else "?") + query
        body = None if payload is None else json.dumps(payload, ensure_ascii=False).encode("utf-8")
        request = urllib.request.Request(
            url,
            data=body,
            headers=self._headers("application/json" if body is not None else None),
            method=method,
        )
        try:
            with urllib.request.urlopen(request, timeout=self.timeout) as response:
                raw = response.read(16 * 1024 * 1024)
                status = response.status
        except urllib.error.HTTPError as exc:
            try:
                value = _safe_json(exc.read(256 * 1024))
            except GatewayError:
                value = {}
            self._raise_upstream_error(value, exc.code)
        except (urllib.error.URLError, TimeoutError, OSError):
            raise GatewayError("video_network_error", "Kling 网页暂时无法连接，请稍后重试。", 502) from None
        try:
            value = _safe_json(raw)
        except GatewayError:
            raise GatewayError("video_invalid_response", "Kling 网页返回了无效响应。", 502) from None
        # The web API often returns HTTP 200 with an application-level error.
        error = value.get("error") if isinstance(value, dict) else None
        root_status = value.get("status") if isinstance(value, dict) else None
        if error or (isinstance(root_status, int) and root_status >= 400):
            self._raise_upstream_error(value, status)
        return value

    def _upload_request(self, endpoint: str, path: Path, upload_token: str):
        body = path.read_bytes()
        digest = hashlib.md5(body).hexdigest()
        query = urllib.parse.urlencode({"uploadToken": upload_token, "contentMd5": digest})
        host = endpoint if endpoint.startswith("https://") else "https://" + endpoint
        request = urllib.request.Request(
            host.rstrip("/") + "/api/upload?" + query,
            data=body,
            headers={
                # The browser uploader uses ``withCredentials: false`` for
                # this cross-origin object-storage request.  Never forward
                # the Kling login Cookie to an upload endpoint returned by
                # the service.
                "Accept": "application/json, text/plain, */*",
                "Origin": self.BASE_URL,
                "Referer": self.WEB_URL,
                "User-Agent": self.USER_AGENT,
                "Content-Type": "application/octet-stream",
                "X-File-MD5": digest,
            },
            method="POST",
        )
        try:
            with urllib.request.urlopen(request, timeout=self.timeout) as response:
                raw = response.read(4 * 1024 * 1024)
                return response.status, raw
        except urllib.error.HTTPError as exc:
            try:
                value = _safe_json(exc.read(128 * 1024))
            except GatewayError:
                value = {}
            self._raise_upstream_error(value, exc.code)
        except (urllib.error.URLError, TimeoutError, OSError):
            raise GatewayError("video_upload_failed", "Kling 参考图上传失败，请检查 Cookie 和网页状态。", 502) from None

    async def _upload_reference(self, path: Path):
        filename = path.name or "reference.png"
        issue = await asyncio.to_thread(
            self._web_request,
            "GET",
            "/api/upload/issue/token",
            params={"filename": filename},
        )
        token = self._first(issue, ("token", "uploadToken", "upload_token"))
        endpoints = self._first(issue, ("httpEndpoints", "http_endpoints", "endpoints"))
        if not token or not isinstance(endpoints, list) or not endpoints:
            raise GatewayError("video_upload_failed", "Kling 没有返回有效的参考图上传令牌。", 502)
        last_error = None
        for endpoint in endpoints:
            try:
                await asyncio.to_thread(self._upload_request, str(endpoint), path, str(token))
                verified = await asyncio.to_thread(
                    self._web_request,
                    "GET",
                    "/api/upload/verify/token",
                    params={"token": token, "type": "image"},
                )
                url = self._first(verified, ("url", "resourceUrl", "resource_url"))
                upload_id = self._first(verified, ("uploadAssetId", "upload_asset_id", "id"))
                if isinstance(url, str) and url.startswith("https://"):
                    return {
                        "name": path.name,
                        "url": url,
                        "fromUploadId": upload_id,
                    }
            except GatewayError as exc:
                last_error = exc
                if exc.status in {401, 403, 422}:
                    raise
        if last_error:
            raise last_error
        raise GatewayError("video_upload_failed", "Kling 没有确认参考图上传结果。", 502)

    @classmethod
    def _task_id(cls, value):
        found = cls._first(value, ("taskId", "task_id", "id"))
        return str(found) if found not in (None, "") else None

    @classmethod
    def _task_record(cls, value, task_id):
        if isinstance(value, dict):
            for key in ("taskId", "task_id", "id"):
                if str(value.get(key, "")) == str(task_id):
                    return value
            for key in ("task", "data", "taskInfos", "tasks", "list", "results"):
                if key in value:
                    found = cls._task_record(value[key], task_id)
                    if found:
                        return found
        elif isinstance(value, list):
            for item in value:
                found = cls._task_record(item, task_id)
                if found:
                    return found
        return None

    @classmethod
    def _video_url(cls, value):
        preferred = ("videoUrl", "video_url", "downloadUrl", "download_url", "playUrl", "play_url")
        for item in cls._walk(value):
            for key in preferred:
                found = item.get(key)
                if isinstance(found, str) and found.startswith("https://"):
                    return found
        for item in cls._walk(value):
            for key in ("resourceUrl", "resource_url", "resource", "url"):
                found = item.get(key)
                if isinstance(found, str) and found.startswith("https://") and ("video" in key.lower() or found.lower().split("?")[0].endswith((".mp4", ".mov", ".webm"))):
                    return found
                if isinstance(found, dict):
                    nested = cls._video_url(found)
                    if nested:
                        return nested
        return None

    @classmethod
    def _status(cls, value):
        raw = cls._first(value, ("status", "taskStatus", "task_status", "state"))
        try:
            numeric = int(raw)
        except (TypeError, ValueError):
            numeric = None
        normalized = str(raw or "").strip().lower()
        if numeric in cls.TERMINAL_SUCCESS or normalized in {"success", "succeeded", "completed", "complete", "finished"}:
            return "succeeded"
        if numeric in cls.TERMINAL_FAILURE or normalized in {"failed", "failure", "error", "cancelled", "canceled", "expired"}:
            return "failed"
        return "running"

    async def credits(self):
        value = await asyncio.to_thread(
            self._web_request,
            "GET",
            "/api/account/pointAndTicket",
            params={"scope": "PERSONAL"},
        )
        fields = {}
        aliases = {
            "points": ("points", "point", "userPoints", "user_points", "balance"),
            "tickets": ("tickets", "ticket", "userTickets", "user_tickets"),
            "free_points": ("freePoints", "free_points", "dailyFreePoints", "daily_free_points"),
            "free_tickets": ("freeTickets", "free_tickets", "dailyFreeTickets", "daily_free_tickets"),
        }
        for output, names in aliases.items():
            found = self._first(value, names)
            if isinstance(found, (int, float)) and not isinstance(found, bool):
                fields[output] = found
        # Points and tickets are separate Kling balances with different
        # semantics, so do not add them into a misleading single total.
        fields["total_available"] = None
        return {"provider": "kling-web", "credits": fields}

    def _payload(self, request, model, refs):
        version = model
        arguments = [
            {"name": "negative_prompt", "value": ""},
            {"name": "duration", "value": str(request.duration)},
            {"name": "imageCount", "value": "1"},
            {"name": "kling_version", "value": version},
            {"name": "prompt", "value": request.prompt},
            {"name": "rich_prompt", "value": request.prompt},
            {"name": "camera_json", "value": json.dumps({"type": "empty", "horizontal": 0, "vertical": 0, "zoom": 0, "tilt": 0, "pan": 0, "roll": 0}, separators=(",", ":"))},
            {"name": "camera_control_enabled", "value": "false"},
            {"name": "prefer_multi_shots", "value": "false"},
            {"name": "biz", "value": "klingai"},
            {"name": "enable_audio", "value": str(bool(request.audio)).lower()},
            {"name": "audio_prompt", "value": ""},
            {"name": "music_prompt", "value": ""},
            {"name": "enable_asmr", "value": "false"},
        ]
        if not refs:
            task_type = "m2v_txt2video"
            arguments.append({"name": "aspect_ratio", "value": request.aspect_ratio})
            inputs = []
        else:
            task_type = "m2v_img2video"
            inputs = []
            for index, item in enumerate(refs):
                name = "first_frame" if index == 0 else "tail_frame" if index == 1 else f"image_{index + 1}"
                inputs.append({"name": name, "inputType": "URL", "url": item["url"], "fromUploadId": item.get("fromUploadId")})
        return {
            "type": task_type,
            "inputs": inputs,
            "arguments": arguments,
            "callbackPayloads": [],
            "extraArgs": {"submit_source": "web2api"},
        }

    async def generate(self, request, folder: Path, reference_paths):
        refs = list(reference_paths or [])
        if len(refs) > 2:
            raise GatewayError("reference_count", "Kling 网页当前接入最多支持 2 张首尾帧参考图。", 422)
        model = request.model if request.model != "default" else self.config["default_model"]
        uploaded = [await self._upload_reference(Path(path)) for path in refs]
        payload = self._payload(request, model, uploaded)
        submitted = await asyncio.to_thread(self._web_request, "POST", "/api/task/submit", payload)
        task_id = self._task_id(submitted)
        if not task_id:
            raise GatewayError("video_invalid_response", "Kling 没有返回任务 ID。", 502)
        deadline = time.monotonic() + self.timeout
        while time.monotonic() < deadline:
            status_value = await asyncio.to_thread(
                self._web_request,
                "GET",
                "/api/task/status/batch",
                params={"taskIds": task_id},
            )
            record = self._task_record(status_value, task_id) or status_value
            state = self._status(record)
            url = self._video_url(record)
            if state == "succeeded" and url:
                path = folder / f"kling-{uuid.uuid4().hex}.mp4"
                await asyncio.to_thread(self._download, url, path)
                return path
            if state == "failed":
                message = self._error_message(record) or "Kling 视频生成失败。"
                lower = message.lower()
                if any(word in lower for word in self.QUOTA_WORDS):
                    raise GatewayError("video_quota_exceeded", "Kling 网页账号的免费 points/tickets 不足或已达到生成限制。", 429)
                raise GatewayError("video_generation_failed", message, 502)
            await asyncio.sleep(min(POLL_INTERVAL, max(0.01, deadline - time.monotonic())))
        raise GatewayError("video_timeout", "等待 Kling 视频生成超时，请到网页任务记录查看状态。", 504)


class PixverseWebVideoBackend(_HTTPBackend):
    """Use the user's PixVerse web token with the first-party web API."""

    BASE_URL = "https://cn-app-api.pixverseai.cn/creative_platform"
    OSS_BUCKET = "cn-pixverse-fe-upload"
    OSS_ENDPOINT = "https://oss-accelerate.aliyuncs.com"
    OSS_REGION = "cn-beijing"
    AUTH_ERRORS = {10001, 10003}
    QUOTA_WORDS = ("credit", "quota", "limit", "insufficient", "额度", "积分", "余额")

    def __init__(self, config: dict, timeout: int):
        super().__init__(config, timeout)
        # A saved web token must only ever be sent to PixVerse's first-party API.
        self.base_url = self.BASE_URL

    @staticmethod
    def _walk(value):
        if isinstance(value, dict):
            yield value
            for item in value.values():
                yield from PixverseWebVideoBackend._walk(item)
        elif isinstance(value, list):
            for item in value:
                yield from PixverseWebVideoBackend._walk(item)

    @classmethod
    def _first(cls, value, keys):
        if isinstance(value, dict):
            for key in keys:
                found = value.get(key)
                if found not in (None, ""):
                    return found
        for item in cls._walk(value):
            for key in keys:
                found = item.get(key)
                if found not in (None, ""):
                    return found
        return None

    @classmethod
    def _raise_api_error(cls, payload, http_status=None):
        raw_code = cls._first(payload, ("ErrCode", "err_code", "code"))
        try:
            code = int(raw_code or 0)
        except (TypeError, ValueError):
            code = 0
        message = str(cls._first(payload, ("ErrMsg", "err_msg", "message", "msg")) or "")
        lower = message.lower()
        if http_status in {401, 403} or code in cls.AUTH_ERRORS:
            raise GatewayError(
                "video_authentication_failed",
                "PixVerse 网页 Token 无效或已失效，请从已登录网页重新复制 Token。",
                401,
            )
        if http_status == 429 or any(word in lower for word in cls.QUOTA_WORDS):
            raise GatewayError(
                "video_quota_exceeded",
                "PixVerse 网页账号 credits 不足或已达到生成限制。",
                429,
            )
        suffix = f"（平台代码 {code}）" if code else ""
        raise GatewayError("video_upstream_error", "PixVerse 网页请求失败" + suffix + "。", 502)

    @classmethod
    def _unwrap(cls, payload, http_status=None):
        if http_status is not None and http_status >= 400:
            cls._raise_api_error(payload, http_status)
        raw_code = cls._first(payload, ("ErrCode", "err_code", "code"))
        if raw_code not in (None, "", 0, "0"):
            try:
                is_error = int(raw_code) != 0
            except (TypeError, ValueError):
                is_error = True
            if is_error:
                cls._raise_api_error(payload)
        if isinstance(payload, dict):
            for key in ("Resp", "resp", "data", "result"):
                value = payload.get(key)
                if isinstance(value, (dict, list)):
                    return value
        return payload

    def _pix_request(self, method, path, payload=None, refresh_credit=False):
        body = None if payload is None else json.dumps(payload, ensure_ascii=False).encode("utf-8")
        headers = {
            "Accept": "application/json",
            "Token": self.config.get("credential", ""),
            "X-Platform": "Web",
            "X-Device-ID": self.config.get("device_id", ""),
            "Workspace-Id": "0",
        }
        if refresh_credit:
            headers["Refresh"] = "credit"
        if body is not None:
            headers["Content-Type"] = "application/json"
        request = urllib.request.Request(
            self.base_url + path,
            data=body,
            headers=headers,
            method=method,
        )
        try:
            with urllib.request.urlopen(request, timeout=self.timeout) as response:
                raw = response.read(16 * 1024 * 1024)
                status = response.status
        except urllib.error.HTTPError as exc:
            try:
                raw = exc.read(128 * 1024)
                value = _safe_json(raw)
            except Exception:  # noqa: BLE001 - do not echo upstream bodies
                value = {}
            self._raise_api_error(value, exc.code)
        except (urllib.error.URLError, TimeoutError, OSError):
            raise GatewayError("video_network_error", "PixVerse 网页暂时无法连接，请稍后重试。", 502) from None
        try:
            value = _safe_json(raw)
        except GatewayError:
            raise GatewayError("video_invalid_response", "PixVerse 网页返回了无效响应。", 502) from None
        return self._unwrap(value, status)

    @staticmethod
    def _credit_number(value):
        if isinstance(value, bool):
            return None
        try:
            number = float(value)
        except (TypeError, ValueError):
            return None
        if not (number == number and abs(number) != float("inf")):
            return None
        return int(number) if number.is_integer() else number

    async def credits(self):
        data = await asyncio.to_thread(self._pix_request, "GET", "/user/credits")
        # Keep only numeric balance fields; never return raw account data.
        fields = {}
        aliases = {
            "credit_daily": ("credit_daily", "creditDaily"),
            "credit_monthly": ("credit_monthly", "creditMonthly"),
            "credit_package": ("credit_package", "creditPackage"),
        }
        for output, names in aliases.items():
            value = self._credit_number(self._first(data, names))
            if value is not None:
                fields[output] = value
        free_values = [
            fields[key]
            for key in ("credit_daily", "credit_monthly")
            if key in fields
        ]
        # PixVerse labels credit_package separately.  It can contain purchased
        # credits, so it is displayed but never counted for a free-only job.
        fields["free_available"] = sum(free_values) if free_values else None
        all_values = [fields[key] for key in aliases if key in fields]
        fields["total_available"] = sum(all_values) if all_values else None
        fields["free_buckets_known"] = bool(free_values)
        return {"provider": "pixverse-web", "credits": fields}

    async def _require_free_credit(self):
        result = await self.credits()
        credits = result.get("credits") or {}
        if not credits.get("free_buckets_known"):
            raise GatewayError(
                "video_free_quota_unverified",
                "PixVerse 未返回每日或月度免费额度，为避免误扣套餐 credits，本次未提交任务。",
                409,
            )
        if (credits.get("free_available") or 0) <= 0:
            raise GatewayError(
                "video_quota_exceeded",
                "PixVerse 网页账号的每日/月度免费额度不足；套餐 credits 不会被自动使用。",
                429,
            )
        return result

    @staticmethod
    def _oss_credentials(value):
        pairs = (
            ("Ak", "Sk", "Token"),
            ("ak", "sk", "token"),
            ("AccessKeyId", "AccessKeySecret", "SecurityToken"),
            ("accessKeyId", "accessKeySecret", "stsToken"),
        )
        for item in PixverseWebVideoBackend._walk(value):
            for access, secret, token in pairs:
                if all(isinstance(item.get(key), str) and item[key] for key in (access, secret, token)):
                    return item[access], item[secret], item[token]
        raise GatewayError("video_upload_failed", "PixVerse 没有返回有效的图片上传凭证。", 502)

    def _upload_to_oss(self, path: Path, object_key: str, credentials):
        try:
            import oss2

            access_key_id, access_key_secret, security_token = credentials
            auth = oss2.StsAuth(access_key_id, access_key_secret, security_token)
            bucket = oss2.Bucket(
                auth,
                self.OSS_ENDPOINT,
                self.OSS_BUCKET,
                region=self.OSS_REGION,
            )
            bucket.put_object(
                object_key,
                path.read_bytes(),
                headers={"Content-Type": mimetypes.guess_type(path.name)[0] or "image/png"},
            )
        except GatewayError:
            raise
        except Exception:  # noqa: BLE001 - hide temporary OSS credentials and SDK details
            raise GatewayError("video_upload_failed", "PixVerse 参考图上传失败，请检查 Token 和网页状态。", 502) from None

    async def _register_reference(self, path: Path):
        token_data = await asyncio.to_thread(self._pix_request, "POST", "/getUploadToken", {})
        credentials = self._oss_credentials(token_data)
        extension = path.suffix.lower() or ".png"
        filename = f"{uuid.uuid4().hex}{extension}"
        object_key = f"upload/{filename}"
        await asyncio.to_thread(self._upload_to_oss, path, object_key, credentials)
        payload = {
            "images": [{
                "name": filename,
                "size": path.stat().st_size,
                "path": object_key,
                "file_name": filename,
            }]
        }
        registered = await asyncio.to_thread(
            self._pix_request, "POST", "/media/batch_upload_media", payload
        )
        rows = registered.get("result", []) if isinstance(registered, dict) else registered
        if isinstance(rows, list) and rows:
            media = rows[0]
        elif isinstance(rows, dict):
            media = rows
        else:
            media = {}
        media_path = self._first(media, ("path", "img_path", "image_path"))
        if not isinstance(media_path, str) or not media_path:
            raise GatewayError("video_upload_failed", "PixVerse 没有确认参考图注册结果。", 502)
        return media_path

    @classmethod
    def _task_id(cls, value):
        task_id = cls._first(value, ("video_id", "videoId", "task_id", "taskId", "id"))
        return str(task_id) if task_id not in (None, "") else None

    @classmethod
    def _video_result(cls, value):
        status = cls._first(value, ("status", "video_status", "task_status", "state"))
        url = cls._first(value, ("video_url", "download_url", "url", "play_url"))
        message = cls._first(value, ("err_msg", "error_message", "message", "msg"))
        try:
            numeric = int(status)
        except (TypeError, ValueError):
            numeric = None
        normalized = str(status or "").strip().lower()
        if numeric in {2, 3}:
            normalized = "succeeded" if numeric == 2 else "failed"
        if normalized in {"success", "succeeded", "completed", "complete", "finished"}:
            state = "succeeded"
        elif normalized in {"failed", "error", "cancelled", "canceled", "expired"}:
            state = "failed"
        else:
            state = "running"
        return state, (url if isinstance(url, str) and url.startswith("https://") else None), str(message or "")

    async def generate(self, request, folder: Path, reference_paths):
        await self._require_free_credit()
        model = request.model if request.model != "default" else self.config["default_model"]
        refs = list(reference_paths or [])
        if len(refs) > 1:
            raise GatewayError("reference_count", "PixVerse 网页接入当前最多支持 1 张首帧参考图。", 422)
        media_paths = []
        for reference in refs:
            media_paths.append(await self._register_reference(Path(reference)))
        payload = {
            "model": model,
            "prompt": request.prompt,
            "quality": request.resolution.lower(),
            "duration": request.duration,
            "customer_img_paths": media_paths,
            "audio": request.audio,
            "multi_shot": False,
            "create_count": 1,
            "aspect_ratio": request.aspect_ratio,
        }
        endpoint = "/video/i2v" if media_paths else "/video/t2v"
        result = await asyncio.to_thread(
            self._pix_request, "POST", endpoint, payload, refresh_credit=True
        )
        task_id = self._task_id(result)
        if not task_id:
            raise GatewayError("video_invalid_response", "PixVerse 没有返回视频任务 ID。", 502)
        deadline = time.monotonic() + self.timeout
        while time.monotonic() < deadline:
            detail = await asyncio.to_thread(
                self._pix_request, "POST", "/video/list/detail", {"video_id": task_id}
            )
            state, url, message = self._video_result(detail)
            if state == "succeeded" and url:
                path = folder / f"pixverse-{uuid.uuid4().hex}.mp4"
                await asyncio.to_thread(self._download, url, path)
                return path
            if state == "failed":
                lower = message.lower()
                if any(word in lower for word in self.QUOTA_WORDS):
                    raise GatewayError("video_quota_exceeded", "PixVerse 网页账号 credits 不足或已达到生成限制。", 429)
                raise GatewayError("video_generation_failed", "PixVerse 视频生成失败。", 502)
            await asyncio.sleep(min(POLL_INTERVAL, max(0.01, deadline - time.monotonic())))
        raise GatewayError("video_timeout", "等待 PixVerse 视频生成超时，请到网页任务记录查看状态。", 504)


def backend_for(config: VideoConfigStore, provider: str, timeout: int):
    saved = config.get(provider)
    classes = {
        DOUBAO_ID: DoubaoWebBackend,
        PUTER_ID: PuterVideoBackend,
        POLLINATIONS_ID: PollinationsImageBackend,
        "jimeng-web": JimengWebVideoBackend,
        "tongyi-web": TongyiWebBackend,
        "hailuo-web": HailuoWebVideoBackend,
        "kling-web": KlingWebVideoBackend,
        "pixverse-web": PixverseWebVideoBackend,
        "minimax": MiniMaxVideoBackend,
        "vidu": ViduVideoBackend,
        VIDU_WEB_ID: ViduWebVideoBackend,
        "bailian": DashScopeVideoBackend,
        "seedance": SeedanceVideoBackend,
    }
    return classes[provider](saved, timeout)
