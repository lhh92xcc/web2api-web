"""调用本机生图接口，读取工作台配置，无需安装额外 Python 包。"""

import argparse
import json
import urllib.error
import urllib.request
import uuid

from client_config import (
    add_generation_arguments,
    load_client_options,
    prepare_references,
)


def main():
    parser = argparse.ArgumentParser(description="调用本地 Gemini Web2API")
    parser.add_argument("prompt", help="图片提示词")
    parser.add_argument("--idempotency-key", default=str(uuid.uuid4()))
    add_generation_arguments(parser)
    args = parser.parse_args()
    try:
        base_url, token, parameters = load_client_options(args)
        parameters = prepare_references(parameters, base_url, token, args.reference)
        if not args.preview:
            print(f"Idempotency-Key: {args.idempotency_key}", flush=True)
        request = urllib.request.Request(
            base_url
            + ("/v1/requests/preview" if args.preview else "/v1/images/generations"),
            data=json.dumps({**parameters, "prompt": args.prompt}).encode(),
            headers={
                "Content-Type": "application/json",
                "Idempotency-Key": args.idempotency_key,
                "Authorization": "Bearer " + token,
            },
            method="POST",
        )
        with urllib.request.urlopen(request, timeout=2400) as response:
            print(json.dumps(json.load(response), indent=2, ensure_ascii=False))
    except ValueError as error:
        raise SystemExit(str(error)) from None
    except urllib.error.HTTPError as error:
        print(error.read().decode())
        raise SystemExit(1) from None
    except urllib.error.URLError:
        raise SystemExit(
            "连接未完成。请查看工作台任务记录；重复调用请沿用原 Idempotency-Key 和参考图 ID。"
        ) from None
    except (TimeoutError, ConnectionError):
        raise SystemExit(
            "请求等待超时，请先查看工作台任务记录，避免重复提交。"
        ) from None
    except OSError:
        raise SystemExit("参考图文件读取失败，请检查路径与权限。") from None


if __name__ == "__main__":
    main()
