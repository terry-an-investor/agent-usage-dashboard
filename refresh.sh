#!/bin/bash
# 刷新 Kimi Code token 仪表盘数据（数据来自本地 wire.jsonl，经 ccusage 聚合）
set -e
DIR="$(cd "$(dirname "$0")" && pwd)"
TMP="$(mktemp -d)"

# 直接执行脚本时也防并发（serve.py 的锁只护住网页入口）；锁目录已存在说明有刷新在跑
LOCK="$DIR/.refresh.lock"
if ! mkdir "$LOCK" 2>/dev/null; then
  echo "另一个刷新正在进行中，跳过" >&2
  exit 1
fi
trap 'rm -rf "$TMP" "$LOCK"' EXIT

CCUSAGE="$DIR/node_modules/.bin/ccusage"  # 本地固定版本，避免每次 npx 联网查 registry
OFFLINE="--offline"  # 定价用本地缓存（页面成本按自己的 PRICE 表算，不依赖 ccusage 定价）
"$CCUSAGE" kimi daily --json   $OFFLINE > "$TMP/daily.json"   2>"$TMP/daily.err"   &
P1=$!
"$CCUSAGE" kimi monthly --json $OFFLINE > "$TMP/monthly.json" 2>"$TMP/monthly.err" &
P2=$!
"$CCUSAGE" kimi session --json $OFFLINE > "$TMP/session.json" 2>"$TMP/session.err" &
P3=$!

FAIL=0
for pair in "$P1 daily" "$P2 monthly" "$P3 session"; do
  set -- $pair
  wait "$1" || { FAIL=1; echo "ccusage $2 失败：" >&2; cat "$TMP/$2.err" >&2; }
done
[ "$FAIL" -eq 0 ] || exit 1

node - "$TMP" "$DIR/data.js" <<'EOF'
const fs = require('fs');
const [tmp, out] = process.argv.slice(2);
const read = f => JSON.parse(fs.readFileSync(`${tmp}/${f}.json`, 'utf8'));
const payload = {
  generatedAt: new Date().toISOString(),
  daily: read('daily').daily,
  monthly: read('monthly').monthly,
  sessions: read('session').sessions,
  totals: read('daily').totals,
};
// 先写临时文件再原子替换，避免写入中断留下残缺的 data.js
const tmpOut = out + '.tmp';
fs.writeFileSync(tmpOut, 'window.DASHBOARD_DATA = ' + JSON.stringify(payload) + ';\n');
fs.renameSync(tmpOut, out);
console.log('data.js updated:', payload.daily.length, 'days,', payload.sessions.length, 'sessions');
EOF
