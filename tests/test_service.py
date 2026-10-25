"""Service upgrades preserve supported tuning and reject interruption by default."""

import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import background_service as service


def test_upgrade_preserves_existing_tuning_and_explicit_overrides(
    monkeypatch, tmp_path
):
    for key in ("WEB2API_TIMEOUT", "WEB2API_QUOTA_COOLDOWN"):
        monkeypatch.delenv(key, raising=False)
    previous = {"WEB2API_TIMEOUT": "2700", "WEB2API_QUOTA_COOLDOWN": "1200"}
    environment = service.runtime_environment(
        tmp_path / "data", tmp_path / "images", 8765, previous
    )
    assert environment["WEB2API_TIMEOUT"] == "2700"
    assert "WEB2API_QUOTA_COOLDOWN" not in environment
    monkeypatch.setenv("WEB2API_TIMEOUT", "900")
    assert (
        service.runtime_environment(
            tmp_path / "data", tmp_path / "images", 8765, previous
        )["WEB2API_TIMEOUT"]
        == "900"
    )


def test_busy_service_cannot_be_interrupted_accidentally(monkeypatch):
    monkeypatch.setattr(service, "health", lambda port: {"busy": True, "queue_size": 0})
    with pytest.raises(RuntimeError, match="仍有生成"):
        service.require_idle(8765)


def test_restart_waits_for_launchd_to_release_stopped_job(monkeypatch):
    responses = iter([SimpleNamespace(returncode=0), SimpleNamespace(returncode=1)])
    slept = []
    monkeypatch.setattr(service, "health", lambda port: None)
    monkeypatch.setattr(service, "launchctl", lambda *args, **kwargs: next(responses))
    monkeypatch.setattr(service.time, "sleep", slept.append)
    service.wait_stopped(8765, "gui/501/local.gemini-web2api")
    assert slept == [0.2]
