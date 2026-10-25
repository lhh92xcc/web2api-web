#!/bin/zsh
set -eu
task_app_dir="$(cd -- "$(dirname -- "$0")" && pwd)"
cd "$task_app_dir"

# Reuse this chat's dependency environment; copied projects keep their own .local.
if [[ "$(basename -- "$(dirname -- "$task_app_dir")")" == "outputs" && -d "$task_app_dir/../../work" ]]; then
  task_runtime_root="$(cd "$task_app_dir/../../work" && pwd)"
  task_default_venv="$task_runtime_root/venv"
  task_default_state="$task_runtime_root/gemini-web2api-data"
else
  task_default_venv="$task_app_dir/.venv"
  task_default_state="$task_app_dir/.local"
fi
task_venv_dir="${WEB2API_VENV_DIR:-$task_default_venv}"
export WEB2API_STATE_DIR="${WEB2API_STATE_DIR:-$task_default_state}"

if [[ ! -x "$task_venv_dir/bin/python" ]]; then
  if command -v uv >/dev/null 2>&1; then
    uv venv --python 3.12 "$task_venv_dir"
  else
    python3 -m venv "$task_venv_dir"
  fi
fi

if ! "$task_venv_dir/bin/python" -c 'from importlib.metadata import version; assert version("gemini-webapi") == "2.1.1"; assert version("fastapi") == "0.146.0"; assert version("uvicorn") == "0.54.0"; assert version("python-dotenv") == "1.2.4"; assert version("python-multipart") == "0.0.32"; assert version("pillow") == "12.3.0"; assert version("oss2") == "2.19.1"' >/dev/null 2>&1; then
  if command -v uv >/dev/null 2>&1; then
    uv pip install --python "$task_venv_dir/bin/python" -r requirements.txt
  else
    "$task_venv_dir/bin/python" -m pip install -r requirements.txt
  fi
fi

exec "$task_venv_dir/bin/python" run.py

