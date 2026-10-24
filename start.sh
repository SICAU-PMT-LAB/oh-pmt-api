#!/usr/bin/env bash
# 启动 oh-pmt-api。首次运行会自动生成管理令牌并打印出来。
set -eu
cd "$(dirname "$0")"

# 环境变量两套名字都认：PMT_* 优先，Q2A_* 是老名字
envval() {
  local name=$1 fallback=${2:-}
  local v
  v=$(eval "printf '%s' \"\${PMT_${name}:-}\"")
  [ -n "$v" ] || v=$(eval "printf '%s' \"\${Q2A_${name}:-}\"")
  [ -n "$v" ] || v=$fallback
  printf '%s' "$v"
}

ADMIN_TOKEN=$(envval ADMIN_TOKEN)
if [ -z "$ADMIN_TOKEN" ]; then
  if [ -f .admin-token ]; then
    ADMIN_TOKEN=$(cat .admin-token)
  else
    ADMIN_TOKEN=$(openssl rand -hex 24)
    echo "$ADMIN_TOKEN" > .admin-token
    chmod 600 .admin-token
    echo "已生成新的管理令牌，保存在 .admin-token"
  fi
fi
export PMT_ADMIN_TOKEN="$ADMIN_TOKEN"
export Q2A_ADMIN_TOKEN="$ADMIN_TOKEN"

PORT=$(envval PORT 8791)
echo "管理界面： http://127.0.0.1:${PORT}/"
exec python3 src/server.py
