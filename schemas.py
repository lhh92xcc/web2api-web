"""Request contracts for the local Gemini web gateway (AGPL-3.0-or-later)."""

import re
from math import gcd
from typing import Annotated, Literal
from urllib.parse import urlsplit

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    StringConstraints,
    field_validator,
    model_validator,
)

from limits import (
    MAX_BATCH_PROMPTS,
    MAX_CHAT_MESSAGE_CHARACTERS,
    MAX_CHAT_MESSAGES,
    MAX_CHAT_TOTAL_CHARACTERS,
    MAX_NEGATIVE_PROMPT_CHARACTERS,
    MAX_OUTPUT_IMAGES,
    MAX_PROMPT_CHARACTERS,
    MAX_REFERENCE_IMAGES,
)

MAX_VIDEO_PROMPT_CHARACTERS = 20_000
MAX_VIDEO_DURATION = 30


class ImageRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    prompt: Annotated[str, StringConstraints(strip_whitespace=False)] = Field(
        min_length=1, max_length=MAX_PROMPT_CHARACTERS
    )
    provider: str = Field(default="gemini", pattern=r"^[a-z][a-z0-9_-]{0,39}$")
    model: str = Field(default="gemini-pro", min_length=1, max_length=160)
    n: int = Field(default=1, ge=1, le=MAX_OUTPUT_IMAGES, strict=True)
    response_format: Literal["url", "b64_json"] = "url"
    size: str = Field(
        default="auto",
        max_length=32,
        description="auto、1K / 2K / 4K 或 64–8192 的宽x高；作为提示，不强制缩放",
    )
    negative_prompt: str = Field(
        default="",
        max_length=MAX_NEGATIVE_PROMPT_CHARACTERS,
        description="在 enhanced 模式下写入最终提示词，不是 Google 独立负面参数",
    )
    prompt_mode: Literal["enhanced", "raw"] = Field(
        default="enhanced",
        description="enhanced 补充生图与构图要求；raw 原样传递 prompt，不可同时设置负面词、尺寸或比例提示",
    )
    full_size: bool = Field(
        default=True,
        strict=True,
        description="True 请求原图；False 下载预览版本，不保证目标分辨率",
    )
    aspect_ratio: Literal["1:1", "16:9", "9:16", "4:3", "3:4", "3:2", "2:3"] | None = (
        None
    )
    reference_images: list[Annotated[str, Field(pattern=r"^ref_[a-f0-9]{32}$")]] = (
        Field(
            default_factory=list,
            max_length=MAX_REFERENCE_IMAGES,
            description=f"先上传参考图，再传入返回的 ID；最多 {MAX_REFERENCE_IMAGES} 个文件。",
        )
    )

    @field_validator("prompt")
    @classmethod
    def nonblank_prompt(cls, value):
        if not value.strip():
            raise ValueError("提示词不能为空。")
        return value

    @field_validator("size")
    @classmethod
    def valid_size(cls, value):
        if value == "auto":
            return value
        if value.upper() in {"1K", "2K", "4K"}:
            return value.upper()
        match = re.fullmatch(r"([1-9]\d{1,3})[xX×]([1-9]\d{1,3})", value)
        if not match or not all(64 <= int(part) <= 8192 for part in match.groups()):
            raise ValueError("尺寸应为 auto、1K / 2K / 4K 或 64–8192 的宽x高。")
        return "x".join(match.groups())

    @model_validator(mode="after")
    def compatible_prompt_controls(self):
        if self.provider != "gemini" and "model" not in self.model_fields_set:
            self.model = "default"
        if self.prompt_mode == "raw":
            if (
                self.negative_prompt
                or self.aspect_ratio is not None
                or self.size != "auto"
            ):
                raise ValueError(
                    "raw 模式原样传递提示词，请清空 negative_prompt、将 size 设为 auto 并移除 aspect_ratio。"
                )
        else:
            self.prompt = self.prompt.strip()
        return self

    @field_validator("reference_images")
    @classmethod
    def unique_references(cls, values):
        if len(values) != len(set(values)):
            raise ValueError("参考图 ID 不能重复。")
        return values

    def composition(self) -> str | None:
        if self.aspect_ratio:
            return self.aspect_ratio
        legacy = {
            "1024x1024": "1:1",
            "1536x1024": "3:2",
            "1024x1536": "2:3",
            "1792x1024": "16:9",
            "1024x1792": "9:16",
        }.get(self.size)
        if legacy:
            return legacy
        if "x" in self.size:
            width, height = map(int, self.size.split("x"))
            divisor = gcd(width, height)
            return f"{width // divisor}:{height // divisor}"
        return None

    def render_prompt(self, has_references: bool = False) -> str:
        if self.prompt_mode == "raw":
            return self.prompt
        instruction = (
            "请结合上传的参考图，按以下要求编辑或生成一张图片，直接返回生成图片。"
            if self.reference_images or has_references
            else "请直接生成一张新图片，不要只返回描述或网络搜索图片。"
        )
        if ratio := self.composition():
            instruction += f"构图宽高比为 {ratio}。"
        if self.size != "auto":
            instruction += f"目标分辨率为 {self.size}，请尽量遵循；以构图比例为准。"
        prompt = f"{instruction}\n\n{self.prompt}"
        if self.negative_prompt:
            prompt += f"\n\n请避免以下内容：{self.negative_prompt}"
        return prompt

    def preview(self) -> dict:
        warnings = []
        if self.negative_prompt:
            warnings.append(
                "负面提示词会写入最终提示词，网页接口未提供独立的负面参数。"
            )
        if self.size != "auto" or self.aspect_ratio:
            warnings.append("尺寸和比例是提示词要求，实际输出像素由所选网站决定。")
        if self.full_size:
            warnings.append(
                "请求原图下载；原图获取失败时，上游可能返回较小版本。"
                if self.provider == "gemini"
                else "浏览器连接读取网页可下载的图片；full_size 仅控制 Gemini 下载，其他网站以网页可读版本为准。"
            )
        return {
            "parameters": self.model_dump(),
            "effective_prompt": self.render_prompt(),
            "warnings": warnings,
        }


class VideoRequest(BaseModel):
    """Provider-neutral image-to-video request.

    A request with no references is text-to-video. One reference is the first
    frame for providers that support I2V; two references are interpreted as
    start/end frames. Providers with a reference-to-video mode receive all
    supplied images as character/subject references.
    """

    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    prompt: Annotated[str, StringConstraints(strip_whitespace=False)] = Field(
        min_length=1, max_length=MAX_VIDEO_PROMPT_CHARACTERS
    )
    provider: str = Field(default="minimax", pattern=r"^[a-z][a-z0-9_-]{0,39}$")
    model: str = Field(default="default", min_length=1, max_length=160)
    duration: int = Field(default=5, ge=1, le=MAX_VIDEO_DURATION, strict=True)
    resolution: str = Field(default="720p", max_length=16)
    aspect_ratio: Literal["adaptive", "21:9", "16:9", "4:3", "1:1", "3:4", "9:16"] = "16:9"
    audio: bool = True
    reference_images: list[Annotated[str, Field(pattern=r"^ref_[a-f0-9]{32}$")]] = Field(
        default_factory=list, max_length=MAX_REFERENCE_IMAGES
    )

    @field_validator("prompt")
    @classmethod
    def video_prompt_not_blank(cls, value):
        if not value.strip():
            raise ValueError("提示词不能为空。")
        return value.strip()

    @field_validator("resolution")
    @classmethod
    def video_resolution(cls, value):
        normalized = value.upper()
        if normalized not in {"480P", "540P", "720P", "768P", "1080P", "2K"}:
            raise ValueError("视频分辨率支持 480P、540P、720P、768P、1080P 或 2K。")
        return normalized

    @field_validator("reference_images")
    @classmethod
    def unique_video_references(cls, values):
        if len(values) != len(set(values)):
            raise ValueError("参考图 ID 不能重复。")
        return values

    def render_prompt(self) -> str:
        return self.prompt

    def preview(self) -> dict:
        warnings = []
        if self.reference_images:
            warnings.append("视频参考图会按所选平台的首帧、首尾帧或主体参考规则传递。")
        if self.audio:
            warnings.append("有声视频是否可用取决于模型和账户权限；失败时平台会返回明确错误。")
        return {"parameters": self.model_dump(), "effective_prompt": self.prompt, "warnings": warnings}


class ChatMessage(BaseModel):
    model_config = ConfigDict(extra="forbid")
    role: Literal["system", "user", "assistant"]
    content: str = Field(min_length=1, max_length=MAX_CHAT_MESSAGE_CHARACTERS)


class ChatRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)
    provider: str = Field(default="gemini", pattern=r"^[a-z][a-z0-9_-]{0,39}$")
    model: str = Field(default="default", min_length=1, max_length=160)
    messages: list[ChatMessage] = Field(min_length=1, max_length=MAX_CHAT_MESSAGES)
    stream: Literal[False] = False
    reference_images: list[Annotated[str, Field(pattern=r"^ref_[a-f0-9]{32}$")]] = (
        Field(default_factory=list, max_length=MAX_REFERENCE_IMAGES)
    )

    @model_validator(mode="after")
    def valid_messages(self):
        if self.messages[-1].role != "user" or not self.messages[-1].content.strip():
            raise ValueError("最后一条消息应为非空的 user 消息。")
        if (
            sum(len(message.content) for message in self.messages)
            > MAX_CHAT_TOTAL_CHARACTERS
        ):
            raise ValueError(f"对话总长度不能超过 {MAX_CHAT_TOTAL_CHARACTERS} 字符。")
        if len(self.reference_images) != len(set(self.reference_images)):
            raise ValueError("参考图不能重复。")
        return self

    def render_prompt(self) -> str:
        if len(self.messages) == 1:
            return self.messages[0].content
        import json

        return (
            "以下 JSON 是调用方提供的对话上下文。请遵循其中的要求，继续回答最后一条 user 消息；只输出回复内容。\n\n"
            + json.dumps(
                [message.model_dump() for message in self.messages], ensure_ascii=False
            )
        )


class CredentialsRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    secure_1psid: str = Field(min_length=1, max_length=8192, repr=False)
    secure_1psidts: str = Field(default="", max_length=8192, repr=False)
    proxy: str = Field(default="", max_length=2048, repr=False)

    @field_validator("secure_1psid", "secure_1psidts")
    @classmethod
    def cookie_value_only(cls, value: str) -> str:
        if any(char.isspace() for char in value) or ";" in value:
            raise ValueError(
                "请填写 Cookie 的 Value，不要包含 Cookie 名称、空格或分号。"
            )
        if value.startswith("__Secure-"):
            raise ValueError("请只复制 Cookie 的 Value。")
        return value

    @field_validator("proxy")
    @classmethod
    def valid_proxy(cls, value: str) -> str:
        if value:
            try:
                parsed = urlsplit(value)
                port = parsed.port
                if (
                    parsed.scheme not in {"http", "https", "socks5", "socks5h"}
                    or not parsed.hostname
                    or not port
                    or parsed.query
                    or parsed.fragment
                    or parsed.path not in {"", "/"}
                ):
                    raise ValueError
            except ValueError:
                raise ValueError("代理地址应类似 http://127.0.0.1:7890。") from None
        return value


class KeyRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)
    name: str = Field(min_length=1, max_length=60)


class AccountRequest(CredentialsRequest):
    name: str = Field(min_length=1, max_length=60)
    secure_1psid: str = Field(default="", max_length=8192, repr=False)
    enabled: bool = True


class BatchItem(ImageRequest):
    custom_id: str = Field(min_length=1, max_length=128)


class BatchRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    items: list[BatchItem] = Field(min_length=1, max_length=MAX_BATCH_PROMPTS)

    @field_validator("items")
    @classmethod
    def unique_custom_ids(cls, items):
        identifiers = [item.custom_id for item in items]
        if len(identifiers) != len(set(identifiers)):
            raise ValueError("同一批次中的 custom_id 不能重复。")
        return items


class VideoBatchItem(VideoRequest):
    """One provider-neutral video request in a batch."""

    custom_id: str = Field(min_length=1, max_length=128)


class VideoBatchRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    items: list[VideoBatchItem] = Field(min_length=1, max_length=MAX_BATCH_PROMPTS)

    @field_validator("items")
    @classmethod
    def unique_custom_ids(cls, items):
        identifiers = [item.custom_id for item in items]
        if len(identifiers) != len(set(identifiers)):
            raise ValueError("同一批次中的 custom_id 不能重复。")
        return items
