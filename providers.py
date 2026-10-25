"""Website adapter definitions; credentials remain in the user's browser."""

import json
from pathlib import Path
from urllib.parse import urlsplit

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from storage import GatewayError, atomic_json


class WebsiteProvider(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)
    id: str = Field(pattern=r"^[a-z][a-z0-9_-]{0,39}$")
    name: str = Field(min_length=1, max_length=60)
    url: str = Field(max_length=2048)
    enabled: bool = True
    images: bool = True
    text: bool = True
    references: bool = True
    prompt_selector: str = Field(min_length=1, max_length=1000)
    send_selector: str = Field(min_length=1, max_length=1000)
    response_selector: str = Field(min_length=1, max_length=1000)
    image_selector: str = Field(default="img", min_length=1, max_length=1000)
    file_selector: str = Field(default='input[type="file"]', max_length=1000)
    busy_selector: str = Field(default="", max_length=1000)

    @field_validator("url")
    @classmethod
    def website_url(cls, value):
        try:
            parsed = urlsplit(value)
            local = parsed.hostname in {"localhost", "127.0.0.1", "::1"}
            if (
                not parsed.hostname
                or parsed.username
                or parsed.password
                or parsed.fragment
            ):
                raise ValueError
            if parsed.scheme != "https" and not (local and parsed.scheme == "http"):
                raise ValueError
            if parsed.query:
                raise ValueError
            _ = parsed.port
        except ValueError:
            raise ValueError(
                "请输入无登录凭据、无查询参数的 HTTPS 网站地址。"
            ) from None
        return value

    @model_validator(mode="after")
    def capabilities(self):
        if not self.images and not self.text:
            raise ValueError("至少启用生图或文字能力之一。")
        if self.id == "gemini":
            raise ValueError("gemini 是内置连接器的保留 ID。")
        if self.id == "chatgpt" or urlsplit(self.url).hostname in {
            "chatgpt.com",
            "chat.openai.com",
        }:
            raise ValueError("ChatGPT 接入已移除。")
        return self


class ProviderStore:
    def __init__(self, state_dir: Path):
        self.path = state_dir / "providers.json"
        self.items = {}
        if self.path.exists():
            for value in json.loads(self.path.read_text()):
                if value.get("id") == "chatgpt":
                    continue
                item = WebsiteProvider.model_validate(value)
                self.items[item.id] = item

    def get(self, provider: str) -> WebsiteProvider:
        item = self.items.get(provider)
        if item is None:
            raise GatewayError(
                "provider_not_found", "没有此平台，请先在平台连接中添加。", 404
            )
        return item

    def save(self, item: WebsiteProvider):
        previous = self.items.get(item.id)
        self.items[item.id] = item
        try:
            atomic_json(
                self.path, [value.model_dump() for value in self.items.values()]
            )
        except OSError:
            if previous:
                self.items[item.id] = previous
            else:
                self.items.pop(item.id)
            raise GatewayError("storage_error", "平台设置保存失败。", 500) from None
        return item
