#!/bin/zsh
set -eu
task_app_dir="$(cd -- "$(dirname -- "$0")" && pwd)"
if [[ "$(basename -- "$(dirname -- "$task_app_dir")")" == "outputs" && -d "$task_app_dir/../../work" ]]; then
  task_default_venv="$task_app_dir/../../work/venv"
elif [[ -f "$task_app_dir/../installation.json" && -x "$task_app_dir/../runtime/bin/python" ]]; then
  task_default_venv="$task_app_dir/../runtime"
else
  task_default_venv="$task_app_dir/.venv"
fi
task_venv_dir="${WEB2API_VENV_DIR:-$task_default_venv}"
if [[ ! -x "$task_venv_dir/bin/python" ]]; then
  print -u2 '请先运行一次 start.command 准备依赖，再运行 service.command。'
  exit 1
fi
if [[ $# -eq 0 ]]; then
  set -- install
fi
exec "$task_venv_dir/bin/python" "$task_app_dir/background_service.py" "$@"
