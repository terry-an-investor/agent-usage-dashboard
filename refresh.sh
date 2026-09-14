#!/bin/bash
# 刷新全 Agent token 仪表盘数据（各 agent 本地日志 -> collect.py -> data.js）
set -e
DIR="$(cd "$(dirname "$0")" && pwd)"

# 直接执行脚本时也防并发（serve.py 的锁只护住网页入口）
LOCK="$DIR/.refresh.lock"
if ! mkdir "$LOCK" 2>/dev/null; then
  # 锁已存在：若里面的 PID 已死，说明是上次异常退出（如 SIGKILL）留下的死锁，清掉重来
  old="$(cat "$LOCK/pid" 2>/dev/null || true)"
  if [ -n "$old" ] && kill -0 "$old" 2>/dev/null; then
    echo "另一个刷新正在进行中（PID $old），跳过" >&2
    exit 1
  fi
  rm -rf "$LOCK"
  mkdir "$LOCK"
fi
echo $$ > "$LOCK/pid"
trap 'rm -rf "$LOCK"' EXIT

python3 "$DIR/collect.py"
