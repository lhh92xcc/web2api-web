"""Upload local reference files using only the Python standard library."""

import json
import urllib.request
import uuid
from pathlib import Path

from limits import MAX_REFERENCE_BYTES, MAX_REFERENCE_IMAGES


def upload_references(base_url: str, token: str, files: list[str]) -> list[str]:
    if not files:
        return []
    if len(files) > MAX_REFERENCE_IMAGES:
        raise SystemExit(f"最多上传 {MAX_REFERENCE_IMAGES} 张参考图。")
    boundary = "web2api-" + uuid.uuid4().hex
    parts = []
    for i, filename in enumerate(files):
        path = Path(filename)
        with path.open("rb") as source:
            content = source.read(MAX_REFERENCE_BYTES + 1)
        if len(content) > MAX_REFERENCE_BYTES:
            raise SystemExit(
                f"每张参考图最大 {MAX_REFERENCE_BYTES // (1024 * 1024)} MB。"
            )
        header = (
            f'--{boundary}\r\nContent-Disposition: form-data; name="files"; '
            f'filename="reference-{i + 1}"\r\n'
            "Content-Type: application/octet-stream\r\n\r\n"
        ).encode()
        parts.append(header + content + b"\r\n")
    body = b"".join(parts) + f"--{boundary}--\r\n".encode()
    request = urllib.request.Request(
        base_url.rstrip("/") + "/v1/references",
        data=body,
        headers={
            "Authorization": "Bearer " + token,
            "Content-Type": "multipart/form-data; boundary=" + boundary,
        },
        method="POST",
    )
    with urllib.request.urlopen(request, timeout=120) as response:
        result = json.load(response)
    ids = [item["id"] for item in result["data"]]
    print("参考图 ID（可用 --reference-id 复用）：" + ", ".join(ids), flush=True)
    return ids
