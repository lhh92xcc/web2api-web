"""Client configuration reaches HTTP bodies; all transports are simulated."""

import argparse
import io
import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import api_example
import batch_example
import client_config
import reference_client


def options(arguments):
    parser = argparse.ArgumentParser()
    client_config.add_generation_arguments(parser)
    return parser.parse_args(arguments)


@pytest.fixture
def config_path(tmp_path):
    path = tmp_path / "client-config.json"
    path.write_text(
        json.dumps(
            {
                "base_url": "http://127.0.0.1:8765/v1/",
                "api_key": "${GEMINI_WEB2API_KEY}",
                "parameters": {
                    "model": "gemini-argon",
                    "n": 2,
                    "negative_prompt": "watermark",
                    "size": "2K",
                    "aspect_ratio": "16:9",
                    "full_size": False,
                    "response_format": "b64_json",
                    "reference_images": [],
                },
            }
        ),
        encoding="utf-8",
    )
    return path


def test_config_loads_env_key_and_explicit_overrides(config_path, monkeypatch):
    monkeypatch.setenv("GEMINI_WEB2API_KEY", "gw_fake-client-test")
    base, token, parameters = client_config.load_client_options(
        options(
            [
                "--config",
                str(config_path),
                "--model",
                "default",
                "--size",
                "4K",
                "--aspect-ratio",
                "auto",
                "--full-size",
                "--n",
                "1",
            ]
        )
    )
    assert base == "http://127.0.0.1:8765"
    assert token == "gw_fake-client-test"
    assert parameters["negative_prompt"] == "watermark"
    assert parameters["full_size"] is True
    assert parameters["aspect_ratio"] is None
    assert parameters["size"] == "4K" and parameters["n"] == 1
    assert parameters["model"] == "default"
    _, _, again = client_config.load_client_options(
        options(["--config", str(config_path)])
    )
    assert again["full_size"] is False and again["n"] == 2


@pytest.mark.parametrize(
    "bad",
    [
        [],
        {"api_key": 12},
        {"unknown": "never-echo-this"},
        {"base_url": "file:///etc/passwd"},
        {"base_url": 12},
        {"parameters": {"reference_images": "ref_not_a_list"}},
        {"parameters": {"unexpected": "never-echo-this"}},
    ],
)
def test_invalid_configs_are_actionable_without_echoing_secrets(
    tmp_path, monkeypatch, bad
):
    monkeypatch.setenv("GEMINI_WEB2API_KEY", "gw_fake-client-test")
    path = tmp_path / "bad.json"
    path.write_text(json.dumps(bad))
    with pytest.raises(ValueError) as failure:
        client_config.load_client_options(options(["--config", str(path)]))
    assert "never-echo-this" not in str(failure.value)


def test_unset_environment_placeholder_fails_before_network(config_path, monkeypatch):
    monkeypatch.delenv("GEMINI_WEB2API_KEY", raising=False)
    with pytest.raises(ValueError, match="调用 Key"):
        client_config.load_client_options(options(["--config", str(config_path)]))


def test_reference_overrides_and_uploads_merge_without_losing_parameters(
    config_path, monkeypatch
):
    monkeypatch.setenv("GEMINI_WEB2API_KEY", "gw_fake-client-test")
    base, token, parameters = client_config.load_client_options(
        options(
            [
                "--config",
                str(config_path),
                "--reference-id",
                "ref_existing",
            ]
        )
    )
    calls = []

    def upload(base_url, key, files):
        calls.append((base_url, key, files))
        return ["ref_uploaded"] * len(files)

    monkeypatch.setattr(client_config, "upload_references", upload)
    result = client_config.prepare_references(parameters, base, token, ["local.png"])
    assert result["reference_images"] == ["ref_existing", "ref_uploaded"]
    assert result["negative_prompt"] == "watermark"
    assert parameters["reference_images"] == ["ref_existing"]
    assert len(calls) == 1
    no_existing_ids = {**parameters, "reference_images": []}
    ten_files = client_config.prepare_references(
        no_existing_ids, base, token, ["a"] * 10
    )
    assert len(ten_files["reference_images"]) == 10
    with pytest.raises(ValueError, match="10 张"):
        client_config.prepare_references(result, base, token, ["a"] * 10)
    assert len(calls) == 2


def test_reference_client_accepts_ten_files_and_rejects_eleven(tmp_path, monkeypatch):
    files = []
    for index in range(11):
        path = tmp_path / f"ref-{index}.png"
        path.write_bytes(b"test")
        files.append(str(path))
    calls = []

    def urlopen(request, **kwargs):
        calls.append(request)
        assert b"reference-10" in request.data
        data = {"data": [{"id": f"ref_{index}"} for index in range(10)]}
        return io.BytesIO(json.dumps(data).encode())

    monkeypatch.setattr(reference_client.urllib.request, "urlopen", urlopen)
    assert (
        len(reference_client.upload_references("http://localhost", "key", files[:10]))
        == 10
    )
    with pytest.raises(SystemExit, match="10 张"):
        reference_client.upload_references("http://localhost", "key", files)
    assert len(calls) == 1


@pytest.mark.parametrize("preview", [False, True])
def test_single_client_sends_config_and_cli_overrides(
    config_path, monkeypatch, capsys, preview
):
    monkeypatch.setenv("GEMINI_WEB2API_KEY", "gw_fake-client-test")
    requests = []

    def urlopen(request, **kwargs):
        requests.append(request)
        return io.BytesIO(b'{"data":[]}')

    monkeypatch.setattr(api_example.urllib.request, "urlopen", urlopen)
    argv = [
        "api_example.py",
        "  a ceramic mug  ",
        "--config",
        str(config_path),
        "--size",
        "4K",
        "--idempotency-key",
        "client-single",
    ]
    if preview:
        argv.append("--preview")
    monkeypatch.setattr(sys, "argv", argv)
    api_example.main()
    request = requests[0]
    assert request.full_url.endswith(
        "/v1/requests/preview" if preview else "/v1/images/generations"
    )
    body = json.loads(request.data)
    assert body["size"] == "4K" and body["negative_prompt"] == "watermark"
    assert body["full_size"] is False and body["n"] == 2
    assert body["prompt"] == "  a ceramic mug  "
    assert request.get_header("Idempotency-key") == "client-single"
    assert "gw_fake-client-test" not in capsys.readouterr().out


@pytest.mark.parametrize("preview", [False, True])
def test_batch_client_applies_configuration_to_every_item(
    config_path, tmp_path, monkeypatch, capsys, preview
):
    monkeypatch.setenv("GEMINI_WEB2API_KEY", "gw_fake-client-test")
    prompts = tmp_path / "prompts.txt"
    prompts.write_text("  first prompt  \n\n  second prompt  \n", encoding="utf-8")
    output = tmp_path / "result.json"
    requests = []

    def urlopen(request, **kwargs):
        requests.append(request)
        if request.full_url.endswith("/v1/batches"):
            result = {"id": "batch_simulated"}
        elif "/v1/batches/" in request.full_url:
            result = {"status": "completed", "counts": {"succeeded": 2}, "items": []}
        else:
            assert request.full_url.endswith("/v1/requests/preview")
            result = {"parameters": json.loads(request.data)}
        return io.BytesIO(json.dumps(result).encode())

    monkeypatch.setattr(batch_example.urllib.request, "urlopen", urlopen)
    argv = [
        "batch_example.py",
        str(prompts),
        "--config",
        str(config_path),
        "--output",
        str(output),
    ]
    if preview:
        argv.append("--preview")
    monkeypatch.setattr(sys, "argv", argv)
    batch_example.main()
    items = (
        [json.loads(request.data) for request in requests]
        if preview
        else json.loads(requests[0].data)["items"]
    )
    assert [item["prompt"] for item in items] == ["first prompt", "second prompt"]
    for item in items:
        assert item["model"] == "gemini-argon" and item["n"] == 2
        assert item["negative_prompt"] == "watermark"
        assert item["full_size"] is False and item["size"] == "2K"
        assert item["response_format"] == "b64_json"
    assert output.exists() is not preview
    assert "gw_fake-client-test" not in capsys.readouterr().out
