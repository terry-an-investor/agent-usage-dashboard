#!/bin/bash
# 启动仪表盘本地服务（后台常驻）并打开页面；重复执行安全
DIR="$(cd "$(dirname "$0")" && pwd)"
if ! lsof -i :8931 -sTCP:LISTEN >/dev/null 2>&1; then
  nohup python3 "$DIR/serve.py" >/dev/null 2>&1 &
  sleep 1
fi
open "http://127.0.0.1:8931/index.html"
