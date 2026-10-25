"""Validated, private reference images scoped to their uploading project."""

import io
import json
import time
import uuid
import warnings
from pathlib import Path

from PIL import Image, UnidentifiedImageError

from limits import (
    MAX_REFERENCE_BYTES,
    MAX_REFERENCE_IMAGES,
    MAX_REFERENCE_PIXELS,
    MAX_REFERENCE_TOTAL_BYTES,
)
from storage import GatewayError, atomic_json, private_opener

MAX_REFERENCES = MAX_REFERENCE_IMAGES
FORMATS = {
    "PNG": ("image/png", ".png"),
    "JPEG": ("image/jpeg", ".jpg"),
    "WEBP": ("image/webp", ".webp"),
}


class ReferenceStore:
    def __init__(self, state_dir: Path):
        self.root = state_dir / "references"
        self.root.mkdir(parents=True, exist_ok=True, mode=0o700)
        self.root.chmod(0o700)
        self.index = state_dir / "references.json"
        self.items = json.loads(self.index.read_text()) if self.index.exists() else {}

    @staticmethod
    def inspect(content: bytes) -> dict:
        if len(content) > MAX_REFERENCE_BYTES:
            raise GatewayError(
                "reference_too_large",
                f"每张参考图最大 {MAX_REFERENCE_BYTES // (1024 * 1024)} MB。",
                413,
            )
        try:
            with warnings.catch_warnings():
                warnings.simplefilter("error", Image.DecompressionBombWarning)
                with Image.open(io.BytesIO(content)) as image:
                    if image.format not in FORMATS:
                        raise GatewayError(
                            "unsupported_reference",
                            "参考图仅支持 PNG、JPEG、WebP。",
                            415,
                        )
                    if getattr(image, "is_animated", False):
                        raise GatewayError(
                            "unsupported_reference", "请使用静态参考图。", 415
                        )
                    if image.width * image.height > MAX_REFERENCE_PIXELS:
                        raise GatewayError(
                            "reference_dimensions",
                            f"参考图最多 {MAX_REFERENCE_PIXELS // 1_000_000} 百万像素，请缩小图片后上传。",
                            413,
                        )
                    mime, extension = FORMATS[image.format]
                    width, height = image.size
                    image.verify()
                # Verify compressed data as well as the container headers.
                with Image.open(io.BytesIO(content)) as image:
                    image.load()
        except (Image.DecompressionBombWarning, Image.DecompressionBombError):
            raise GatewayError(
                "reference_dimensions", "参考图尺寸过大，请缩小图片后上传。", 413
            ) from None
        except (UnidentifiedImageError, OSError, ValueError, SyntaxError):
            raise GatewayError(
                "invalid_reference",
                "图片文件无法读取，请上传完整的 PNG、JPEG 或 WebP。",
                415,
            ) from None
        return {
            "mime_type": mime,
            "extension": extension,
            "width": width,
            "height": height,
            "bytes": len(content),
        }

    def save(self, files: list[tuple[str, bytes]], owner_id: str | None) -> list[dict]:
        if not 1 <= len(files) <= MAX_REFERENCES:
            raise GatewayError(
                "reference_count", f"一次可上传 1–{MAX_REFERENCES} 张参考图。", 422
            )
        if sum(len(content) for _, content in files) > MAX_REFERENCE_TOTAL_BYTES:
            raise GatewayError(
                "reference_too_large",
                f"本次参考图总大小不能超过 {MAX_REFERENCE_TOTAL_BYTES // (1024 * 1024)} MB。",
                413,
            )
        inspected = [self.inspect(content) for _, content in files]
        created = []
        try:
            for (name, content), metadata in zip(files, inspected, strict=True):
                reference_id = "ref_" + uuid.uuid4().hex
                item = {
                    "id": reference_id,
                    "name": name.replace("\\", "/").split("/")[-1][:200] or "参考图",
                    "filename": reference_id + metadata.pop("extension"),
                    "owner_id": owner_id,
                    "created_at": time.time(),
                    **metadata,
                }
                created.append(item)
                with open(
                    self.root / item["filename"], "wb", opener=private_opener
                ) as stream:
                    stream.write(content)
                self.items[reference_id] = item
            atomic_json(self.index, self.items)
        except OSError:
            for item in created:
                self.items.pop(item["id"], None)
                (self.root / item["filename"]).unlink(missing_ok=True)
            raise GatewayError(
                "storage_error", "参考图保存失败，请检查磁盘空间与权限。", 500
            ) from None
        return [self.public(item) for item in created]

    @staticmethod
    def public(item: dict) -> dict:
        return {
            key: value
            for key, value in item.items()
            if key not in {"filename", "owner_id"}
        }

    def get(self, reference_id: str, owner_id: str | None, admin=False) -> dict:
        item = self.items.get(reference_id)
        if item is None or (not admin and item["owner_id"] != owner_id):
            raise GatewayError(
                "reference_not_found", "参考图不存在或不属于当前项目，请重新上传。", 404
            )
        return item

    def path(self, item: dict) -> Path:
        path = (self.root / item["filename"]).resolve()
        if not path.is_relative_to(self.root.resolve()) or not path.is_file():
            raise GatewayError(
                "reference_not_found", "本地参考图文件已不存在，请重新上传。", 404
            )
        return path

    def paths(self, ids: list[str], owner_id: str | None) -> list[Path]:
        return [self.path(self.get(reference_id, owner_id)) for reference_id in ids]
