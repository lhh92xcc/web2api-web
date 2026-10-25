"""Manage this local gateway as a per-user macOS LaunchAgent."""

import argparse
import json
import os
import plistlib
import re
import shutil
import subprocess
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

from dotenv import load_dotenv

PROJECT = Path(__file__).resolve().parent
LABEL = "local.gemini-web2api"
INSTALL = Path.home() / "Library" / "Application Support" / "GeminiWeb2API"


def service_paths():
    workspace = PROJECT.parent.parent / "work"
    default_state = (
        workspace / "gemini-web2api-data"
        if PROJECT.parent.name == "outputs" and workspace.is_dir()
        else PROJECT / ".local"
    )
    state = (
        Path(os.getenv("WEB2API_STATE_DIR", str(default_state))).expanduser().resolve()
    )
    images = (
        Path(os.getenv("WEB2API_IMAGES_DIR", str(PROJECT / "generated")))
        .expanduser()
        .resolve()
    )
    return state, images


def copy_tree_once(source, destination):
    if destination.exists():
        return
    staging = destination.with_name(destination.name + ".installing")
    if staging.exists():
        shutil.rmtree(staging)
    if source.is_dir():
        shutil.copytree(source, staging, symlinks=True)
    else:
        staging.mkdir(mode=0o700)
    staging.rename(destination)


def prepare_installation():
    """Keep launchd's executable, working directory and logs outside Documents."""
    app = INSTALL / "app"
    marker = INSTALL / "installation.json"
    if marker.exists():
        origin = json.loads(marker.read_text())["source_project"]
        if str(PROJECT) not in {origin, str(app)}:
            raise RuntimeError("已有其他目录安装的 GeminiWeb2API，未覆盖。")
    INSTALL.mkdir(parents=True, exist_ok=True, mode=0o700)
    INSTALL.chmod(0o700)
    runtime = INSTALL / "runtime"
    if not runtime.exists():
        if sys.prefix == sys.base_prefix:
            raise RuntimeError("请通过 service.command 使用项目的虚拟环境安装。")
        print("正在安装独立运行环境…", flush=True)
        copy_tree_once(Path(sys.prefix), runtime)
    app.mkdir(exist_ok=True)
    if PROJECT != app:
        for path in PROJECT.iterdir():
            if path.is_file() and (
                path.suffix in {".py", ".command", ".md"}
                or path.name
                in {
                    "requirements.txt",
                    "LICENSE",
                    ".env",
                    ".env.example",
                    "client-config.example.json",
                    "browser-extension.zip",
                }
            ):
                shutil.copy2(path, app / path.name)
                if path.name == ".env":
                    (app / path.name).chmod(0o600)
        shutil.copytree(PROJECT / "static", app / "static", dirs_exist_ok=True)
        if (PROJECT / "browser-extension").is_dir():
            shutil.copytree(
                PROJECT / "browser-extension",
                app / "browser-extension",
                dirs_exist_ok=True,
            )
        archive = PROJECT.parent / "gemini-web2api-source.zip"
        if archive.is_file():
            shutil.copy2(archive, INSTALL / archive.name)
    source_state, source_images = service_paths()
    copy_tree_once(source_state, INSTALL / "data")
    copy_tree_once(source_images, INSTALL / "images")
    (INSTALL / "data").chmod(0o700)
    if not marker.exists():
        marker.write_text(
            json.dumps(
                {"source_project": str(PROJECT), "installed_at": time.time()},
                ensure_ascii=False,
            ),
            encoding="utf-8",
        )
        marker.chmod(0o600)
    return app, runtime / "bin" / "python"


def launchctl(*args, check=True):
    result = subprocess.run(
        ["/bin/launchctl", *args],
        capture_output=True,
        text=True,
        check=False,
    )
    if check and result.returncode:
        raise RuntimeError(
            f"launchctl {args[0]} 失败：{result.stderr.strip() or result.returncode}"
        )
    return result


def health(port):
    try:
        with urllib.request.urlopen(
            f"http://127.0.0.1:{port}/health", timeout=2
        ) as response:
            return json.load(response)
    except (OSError, ValueError, urllib.error.URLError):
        return None


def require_idle(port):
    current = health(port)
    if current and (current.get("busy") or current.get("queue_size")):
        raise RuntimeError("仍有生成或排队任务，请等队列空闲后再停止或重启。")


def runtime_environment(state, images, port, previous=None):
    previous = previous or {}
    environment = {
        "PATH": "/usr/bin:/bin:/usr/sbin:/sbin:/usr/local/bin:/opt/homebrew/bin",
        "PYTHONUNBUFFERED": "1",
        "WEB2API_STATE_DIR": str(state),
        "WEB2API_IMAGES_DIR": str(images),
        "WEB2API_PORT": str(port),
    }
    for name, default in (("WEB2API_TIMEOUT", "450"),):
        environment[name] = os.getenv(name, previous.get(name, default))
    return environment


def wait_ready(target, port):
    deadline = time.monotonic() + 25
    while time.monotonic() < deadline:
        current = health(port)
        if current and current.get("status") == "ok":
            return current
        time.sleep(0.3)
    details = launchctl("print", target, check=False).stdout
    exit_code = re.search(r"last exit code = (\d+)", details)
    suffix = f"（退出码 {exit_code[1]}）" if exit_code else ""
    raise RuntimeError(f"后台服务尚未响应{suffix}，请查看私有数据目录 logs 中的日志。")


def wait_stopped(port, target):
    deadline = time.monotonic() + 35
    while time.monotonic() < deadline:
        if (
            health(port) is None
            and launchctl("print", target, check=False).returncode != 0
        ):
            return
        time.sleep(0.2)
    raise RuntimeError("原服务仍未停止，未启动第二个实例。")


def main():
    parser = argparse.ArgumentParser(description="Gemini Web2API 的 macOS 后台常驻管理")
    parser.add_argument(
        "action",
        nargs="?",
        default="install",
        choices=["install", "status", "restart", "stop", "uninstall"],
    )
    parser.add_argument(
        "--interrupt-running",
        action="store_true",
        help="明确允许中断正在生成的任务；不会自动重新生图。",
    )
    args = parser.parse_args()
    if sys.platform != "darwin":
        raise SystemExit(
            "此脚本用于 macOS；其他系统请使用 start.command 或自己的进程管理工具。"
        )
    load_dotenv(PROJECT / ".env")
    port = int(os.getenv("WEB2API_PORT", "8765"))
    domain = f"gui/{os.getuid()}"
    target = f"{domain}/{LABEL}"
    agent_path = Path.home() / "Library" / "LaunchAgents" / f"{LABEL}.plist"
    app = INSTALL / "app"
    state, images = INSTALL / "data", INSTALL / "images"
    logs = state / "logs"

    saved = {}
    try:
        if agent_path.exists():
            with agent_path.open("rb") as source:
                saved = plistlib.load(source)
            if saved.get("Label") != LABEL or saved.get("WorkingDirectory") not in {
                str(PROJECT),
                str(app),
            }:
                raise RuntimeError("同名后台服务属于其他目录，未修改；请先检查原服务。")
        result = launchctl("print", target, check=False)
        loaded = result.returncode == 0
        if args.action == "status":
            pid = re.search(r"\bpid = (\d+)", result.stdout)
            print(
                json.dumps(
                    {
                        "installed": agent_path.is_file(),
                        "loaded": loaded,
                        "pid": int(pid[1]) if pid else None,
                        "url": f"http://127.0.0.1:{port}",
                        "installation": str(INSTALL),
                        "health": health(port),
                    },
                    ensure_ascii=False,
                    indent=2,
                )
            )
            return
        if args.action in {"stop", "uninstall", "restart"}:
            if not args.interrupt_running:
                require_idle(port)
            if loaded:
                launchctl("bootout", target)
                wait_stopped(port, target)
                loaded = False
            if args.action in {"stop", "uninstall"}:
                if args.action == "uninstall":
                    agent_path.unlink(missing_ok=True)
                print(
                    "已停止后台服务，账号、Key、任务和图片均保留。"
                    if args.action == "stop"
                    else "已取消后台常驻和登录启动，所有业务数据保留。"
                )
                return
        if loaded:
            wait_ready(target, port)
            print(f"后台常驻服务已经运行：http://127.0.0.1:{port}")
            return
        if health(port):
            raise RuntimeError(
                "此端口已有其他启动方式运行的服务，请先停止前台服务再安装后台常驻。"
            )
        app, python = prepare_installation()
        agent_path.parent.mkdir(parents=True, exist_ok=True)
        state.mkdir(parents=True, exist_ok=True, mode=0o700)
        logs.mkdir(parents=True, exist_ok=True, mode=0o700)
        for filename in ["server.log", "error.log"]:
            descriptor = os.open(
                logs / filename, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600
            )
            os.close(descriptor)
        definition = {
            "Label": LABEL,
            "ProgramArguments": [
                str(python),
                str(app / "run.py"),
            ],
            "WorkingDirectory": str(app),
            "RunAtLoad": True,
            "KeepAlive": True,
            "ThrottleInterval": 10,
            "ExitTimeOut": 30,
            "ProcessType": "Background",
            "Umask": 0o077,
            "EnvironmentVariables": runtime_environment(
                state, images, port, saved.get("EnvironmentVariables")
            ),
            "StandardOutPath": str(logs / "server.log"),
            "StandardErrorPath": str(logs / "error.log"),
        }
        with agent_path.open("wb") as destination:
            plistlib.dump(definition, destination)
        agent_path.chmod(0o600)
        launchctl("enable", target)
        launchctl("bootstrap", domain, str(agent_path))
        wait_ready(target, port)
        print(f"已开启后台常驻、登录自动启动、退出自动恢复：http://127.0.0.1:{port}")
        print(
            f"服务管理：{PROJECT / 'service.command'} status / restart / stop / uninstall"
        )
    except (OSError, ValueError, RuntimeError) as error:
        raise SystemExit(str(error)) from None


if __name__ == "__main__":
    main()
