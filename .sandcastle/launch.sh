#!/usr/bin/env bash
set -euo pipefail
export PATH=/usr/local/bin:/usr/bin:/bin

repo_root=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd -P)
mode=${1:-check}
case "$mode" in
  check) entry=check.mts ;;
  run) entry=main.mts ;;
  *) printf '运行模式只能是 check 或 run。\n' >&2; exit 1 ;;
esac

for executable in node npm git gh bwrap python3; do
  command -v "$executable" >/dev/null || { printf '缺少 Linux 工具：%s\n' "$executable" >&2; exit 1; }
done

runtime_key=$(printf '%s' "$repo_root" | sha256sum | cut -c 1-12)
runtime_directory="$HOME/.local/share/shopkeeper-brain/sandcastle/$runtime_key"
install -d -m 700 "$runtime_directory/scripts"
exec 9>"$runtime_directory/run.lock"
flock -n 9 || { printf '该项目已有 Sandcastle 进程运行。\n' >&2; exit 1; }

if ! cmp -s "$repo_root/package-lock.json" "$runtime_directory/package-lock.json" || [ ! -x "$runtime_directory/node_modules/.bin/tsx" ]; then
  cp "$repo_root/package.json" "$repo_root/package-lock.json" "$runtime_directory/"
  npm ci --prefix "$runtime_directory" --no-audit --no-fund
fi

cp "$repo_root/.sandcastle/main.mts" "$repo_root/.sandcastle/check.mts" "$repo_root/.sandcastle/settings.mts" "$runtime_directory/scripts/"
cd "$repo_root"
exec "$runtime_directory/node_modules/.bin/tsx" "$runtime_directory/scripts/$entry"
