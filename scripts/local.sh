#!/bin/sh
set -eu
repo=$(CDPATH= cd -- "$(dirname -- "$0")/.." && pwd)
cd "$repo"
config_file=${COMPLAINT_CONFIG:-"$HOME/.local/share/kesu-local/config.json"}
if [ ! -x "$repo/.venv/bin/python" ] || [ ! -f "$config_file" ]; then
  echo '本地环境尚未配置，请参见 README.md 的本机运行说明。' >&2
  exit 1
fi
exec "$repo/.venv/bin/python" "$repo/scripts/dev_service.py" "${1:-start}" --config "$config_file"
