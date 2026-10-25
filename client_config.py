"""Shared, standard-library-only configuration for the included API clients."""

import argparse
import json
import os
from pathlib import Path
from urllib.parse import urlsplit

from limits import MAX_OUTPUT_IMAGES, MAX_REFERENCE_IMAGES
from reference_client import upload_references

DEFAULT_PARAMETERS = {
    "provider": "gemini",
    "model": "gemini-pro",
    "n": 1,
    "prompt_mode": "enhanced",
    "negative_prompt": "",
    "size": "auto",
    "aspect_ratio": None,
    "full_size": True,
    "response_format": "url",
    "reference_images": [],
}


def add_generation_arguments(parser):
    parser.add_argument("--config", help="工作台导出的 client-config.json")
    parser.add_argument("--base-url", help="服务地址，可包含 /v1")
    parser.add_argument("--model")
    parser.add_argument("--provider", help="平台 ID，默认 gemini")
    parser.add_argument("--n", type=int, choices=range(1, MAX_OUTPUT_IMAGES + 1))
    parser.add_argument("--negative-prompt")
    parser.add_argument("--size", help="auto、1K / 2K / 4K 或 宽x高")
    parser.add_argument(
        "--aspect-ratio",
        choices=["auto", "1:1", "16:9", "9:16", "4:3", "3:4", "3:2", "2:3"],
    )
    parser.add_argument("--prompt-mode", choices=["enhanced", "raw"])
    parser.add_argument(
        "--full-size",
        action=argparse.BooleanOptionalAction,
        default=None,
        help="请求原图；--no-full-size 下载预览版本",
    )
    parser.add_argument("--response-format", choices=["url", "b64_json"])
    parser.add_argument(
        "--reference",
        action="append",
        default=[],
        help=f"本地参考图，可重复传入，最多 {MAX_REFERENCE_IMAGES} 张",
    )
    parser.add_argument(
        "--reference-id", action="append", help="复用参考图 ID；指定后替换配置中的 ID"
    )
    parser.add_argument(
        "--preview", action="store_true", help="只验证参数并显示实际提示词，不生成图片"
    )


def normalize_base_url(value):
    if not isinstance(value, str):
        raise ValueError("base_url 应为 HTTP 服务地址。")  # noqa: TRY004 - config value error
    try:
        parsed = urlsplit(value)
        _ = parsed.port
        if (
            parsed.scheme not in {"http", "https"}
            or not parsed.hostname
            or parsed.username
            or parsed.password
            or parsed.query
            or parsed.fragment
            or parsed.path.rstrip("/") not in {"", "/v1"}
        ):
            raise ValueError
    except (TypeError, ValueError):
        raise ValueError("base_url 应类似 http://127.0.0.1:8765/v1。") from None
    return f"{parsed.scheme}://{parsed.netloc}"


def load_client_options(args):
    config = {}
    if args.config:
        try:
            config = json.loads(Path(args.config).read_text(encoding="utf-8-sig"))
        except (OSError, UnicodeError, ValueError):
            raise ValueError("配置文件读取失败，请检查路径和 JSON 格式。") from None
        if not isinstance(config, dict) or set(config) - {
            "base_url",
            "api_key",
            "parameters",
        }:
            raise ValueError("配置文件仅支持 base_url、api_key、parameters。")
    configured = config.get("parameters", {})
    if not isinstance(configured, dict) or set(configured) - set(DEFAULT_PARAMETERS):
        raise ValueError(
            "parameters 包含未知参数；提示词请通过命令行或提示词文件传入。"
        )
    parameters = {**DEFAULT_PARAMETERS, **configured}
    parameters["reference_images"] = (
        list(parameters["reference_images"])
        if isinstance(parameters["reference_images"], list)
        else None
    )
    if parameters["reference_images"] is None or not all(
        isinstance(item, str) for item in parameters["reference_images"]
    ):
        raise ValueError("reference_images 应为参考图 ID 数组。")
    for field in DEFAULT_PARAMETERS:
        value = getattr(args, field, None)
        if value is not None:
            parameters[field] = (
                None if field == "aspect_ratio" and value == "auto" else value
            )
    if (
        parameters["provider"] != "gemini"
        and not getattr(args, "model", None)
        and "model" not in configured
    ):
        parameters["model"] = "default"
    if args.reference_id is not None:
        parameters["reference_images"] = args.reference_id.copy()
    base_url = normalize_base_url(
        args.base_url or config.get("base_url", "http://127.0.0.1:8765/v1")
    )
    raw_token = config.get("api_key", "")
    if not isinstance(raw_token, str):
        raise ValueError("api_key 应为字符串或环境变量引用。")  # noqa: TRY004 - config value error
    raw_token = raw_token or os.getenv("GEMINI_WEB2API_KEY", "")
    token = os.path.expandvars(raw_token).strip()
    if (
        not token
        or "$" in token
        or token == "YOUR_API_KEY"
        or any(char.isspace() for char in token)
    ):
        raise ValueError(
            "请在配置的 api_key 填写调用 Key，或设置环境变量 GEMINI_WEB2API_KEY。"
        )
    return base_url, token, parameters


def prepare_references(parameters, base_url, token, files):
    ids = parameters["reference_images"]
    if len(ids) + len(files) > MAX_REFERENCE_IMAGES:
        raise ValueError(f"最多使用 {MAX_REFERENCE_IMAGES} 张参考图。")
    return {
        **parameters,
        "reference_images": ids + upload_references(base_url, token, files),
    }


def batch_payload(prompts, parameters):
    if not 1 <= len(prompts) <= 20:
        raise ValueError("每批需包含 1–20 条非空提示词。")
    return {
        "items": [
            {**parameters, "prompt": prompt, "custom_id": f"item-{index + 1}"}
            for index, prompt in enumerate(prompts)
        ]
    }
