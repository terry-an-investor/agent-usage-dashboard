#!/bin/bash
# 启动仪表盘本地服务（后台常驻）并打开页面；重复执行安全
DIR="$(cd "$(dirname "$0")" && pwd)"
URL="http://127.0.0.1:8931"
if ! curl -s -o /dev/null "$URL/" 2>/dev/null; then
  nohup python3 "$DIR/serve.py" >/dev/null 2>&1 &
  # 轮询等服务真正监听再打开页面，避免服务启动失败时打开空白/错误页
  up=0
  for i in $(seq 1 20); do
    if curl -s -o /dev/null "$URL/" 2>/dev/null; then up=1; break; fi
    sleep 0.5
  done
  if [ "$up" -ne 1 ]; then
    echo "服务启动失败：请检查 python3 是否可用、端口 8931 是否被其它进程占用" >&2
    exit 1
  fi
fi
open "$URL/index.html"
