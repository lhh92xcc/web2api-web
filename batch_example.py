"""Submit a prompt file with shared parameters, then poll and save its batch result."""

import argparse
import json
import time
import urllib.error
import urllib.request
import uuid
from pathlib import Path

from client_config import (
    add_generation_arguments,
    batch_payload,
    load_client_options,
    prepare_references,
)


def main():
    parser = argparse.ArgumentParser(description="批量提交提示词文件，每行一个生图任务")
    parser.add_argument("prompts", nargs="?", help="UTF-8 文本文件路径，每行一条提示词")
    parser.add_argument("--batch-id", help="继续查询已提交批次，不重新提交")
    parser.add_argument("--output", default="batch-result.json")
    parser.add_argument("--idempotency-key", default=str(uuid.uuid4()))
    add_generation_arguments(parser)
    args = parser.parse_args()

    try:
        base_url, token, parameters = load_client_options(args)

        def call(path, payload=None):
            request = urllib.request.Request(
                base_url + path,
                data=json.dumps(payload).encode() if payload is not None else None,
                headers={
                    "Content-Type": "application/json",
                    "Authorization": "Bearer " + token,
                    "Idempotency-Key": args.idempotency_key,
                },
            )
            with urllib.request.urlopen(request, timeout=120) as response:
                return json.load(response)

        batch_id = args.batch_id
        if not batch_id:
            if not args.prompts:
                parser.error("请提供提示词文件，或用 --batch-id 查询已有批次。")
            raw = parameters["prompt_mode"] == "raw"
            prompts = [
                line if raw else line.strip()
                for line in Path(args.prompts)
                .read_text(encoding="utf-8-sig")
                .splitlines()
                if line.strip()
            ]
            # Validate the batch size before any uploads.
            batch_payload(prompts, parameters)
            parameters = prepare_references(parameters, base_url, token, args.reference)
            payload = batch_payload(prompts, parameters)
            if args.preview:
                previews = [
                    call(
                        "/v1/requests/preview",
                        {
                            key: value
                            for key, value in item.items()
                            if key != "custom_id"
                        },
                    )
                    for item in payload["items"]
                ]
                print(json.dumps({"items": previews}, ensure_ascii=False, indent=2))
                return
            print(f"Idempotency-Key: {args.idempotency_key}", flush=True)
            result = call("/v1/batches", payload)
            batch_id = result["id"]
            print(f"批次已提交：{batch_id}；可用 --batch-id 继续查询。", flush=True)
        elif args.preview:
            parser.error("--preview 用于预览提示词文件，不能与 --batch-id 同用。")
        last_counts = None
        while True:
            result = call(f"/v1/batches/{batch_id}")
            if result["counts"] != last_counts:
                print(json.dumps(result["counts"], ensure_ascii=False), flush=True)
                last_counts = result["counts"]
            if result["status"] != "in_progress":
                Path(args.output).write_text(
                    json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8"
                )
                print(f"结果已保存到 {args.output}")
                break
            time.sleep(3)
    except ValueError as error:
        raise SystemExit(str(error)) from None
    except urllib.error.HTTPError as error:
        print(error.read().decode())
        raise SystemExit(1) from None
    except urllib.error.URLError:
        raise SystemExit(
            "本地服务连接失败，请在工作台检查任务；请勿直接重新提交同一批次。"
        ) from None
    except KeyboardInterrupt:
        raise SystemExit(
            "查询已停止，后台生成仍会继续。可用 --batch-id 继续查询。"
        ) from None
    except (TimeoutError, ConnectionError):
        raise SystemExit(
            "请求等待超时，请先查看工作台或使用 --batch-id 继续查询。"
        ) from None
    except OSError:
        raise SystemExit("本地文件读写失败，请检查提示词、参考图和输出路径。") from None


if __name__ == "__main__":
    main()
