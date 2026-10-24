#!/usr/bin/env bash
# 启动 oh-pmt-api。首次运行会自动生成管理令牌并打印出来。
set -eu
cd "$(dirname "$0")"

if [ -z "${Q2A_ADMIN_TOKEN:-}" ]; then
  if [ -f .admin-token ]; then
    Q2A_ADMIN_TOKEN=$(cat .admin-token)
  else
    Q2A_ADMIN_TOKEN=$(openssl rand -hex 24)
    echo "$Q2A_ADMIN_TOKEN" > .admin-token
    chmod 600 .admin-token
    echo "已生成新的管理令牌，保存在 .admin-token"
  fi
  export Q2A_ADMIN_TOKEN
fi

PORT="${Q2A_PORT:-8791}"
echo "管理界面： http://127.0.0.1:${PORT}/"
exec python3 src/server.py
