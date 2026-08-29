#!/bin/bash
# 刷新 Kimi Code token 仪表盘数据（数据来自本地 wire.jsonl，经 ccusage 聚合）
set -e
DIR="$(cd "$(dirname "$0")" && pwd)"
TMP="$(mktemp -d)"
trap 'rm -rf "$TMP"' EXIT

CCUSAGE="$DIR/node_modules/.bin/ccusage"  # 本地固定版本，避免每次 npx 联网查 registry
OFFLINE="--offline"  # 定价用本地缓存（页面成本按自己的 PRICE 表算，不依赖 ccusage 定价）
"$CCUSAGE" kimi daily --json   $OFFLINE > "$TMP/daily.json"   2>/dev/null &
"$CCUSAGE" kimi monthly --json $OFFLINE > "$TMP/monthly.json" 2>/dev/null &
"$CCUSAGE" kimi session --json $OFFLINE > "$TMP/session.json" 2>/dev/null &
wait

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
fs.writeFileSync(out, 'window.DASHBOARD_DATA = ' + JSON.stringify(payload) + ';\n');
console.log('data.js updated:', payload.daily.length, 'days,', payload.sessions.length, 'sessions');
EOF
