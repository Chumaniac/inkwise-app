#!/bin/sh
set -eu
app_dir=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
uv_bin=$(command -v uv || true)
if [ -z "$uv_bin" ] && [ -x "$HOME/.local/bin/uv" ]; then uv_bin="$HOME/.local/bin/uv"; fi
if [ -z "$uv_bin" ]; then
  echo 'Install uv first, then open this launcher again. See docs/local-distribution.md.' >&2
  exit 2
fi
cd "$app_dir"
"$uv_bin" sync --frozen --no-dev --python 3.12
exec "$uv_bin" run --frozen --no-dev --python 3.12 python "$app_dir/scripts/start_local.py" --start-model "$@"
