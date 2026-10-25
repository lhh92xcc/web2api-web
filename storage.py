"""Private local persistence shared by keys, jobs and reference images."""

import json
import os
from pathlib import Path
from typing import Any


class GatewayError(Exception):
    def __init__(self, code: str, message: str, status: int = 502, response=None):
        self.code, self.message, self.status = code, message, status
        self.response = response
        super().__init__(message)

    def as_dict(self) -> dict:
        result = {"code": self.code, "message": self.message, "status": self.status}
        if self.response:
            result["response"] = self.response[:4000]
        return result


def private_opener(path, flags):
    return os.open(path, flags, 0o600)


def atomic_json(path: Path, data: Any) -> None:
    temporary = path.with_suffix(".tmp")
    with open(temporary, "w", encoding="utf-8", opener=private_opener) as stream:
        json.dump(data, stream, ensure_ascii=False, indent=2)
    os.chmod(temporary, 0o600)
    temporary.replace(path)
