#!/bin/bash
# 刷新 Kimi Code token 仪表盘数据（数据来自本地 wire.jsonl，经 ccusage 聚合）
set -e
DIR="$(cd "$(dirname "$0")" && pwd)"
TMP="$(mktemp -d)"

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
// JSON 中的 < 转义为 <（JSON 等价写法），防止日志数据里的 </script>
// 截断页面的 <script src="data.js"> 标签造成脚本注入
const json = JSON.stringify(payload).replace(/</g, '\\u003c');
// 先在临时目录写好再原子替换，避免写入中断留下残缺的 data.js
const tmpOut = `${tmp}/data.js`;
fs.writeFileSync(tmpOut, 'window.DASHBOARD_DATA = ' + json + ';\n');
fs.renameSync(tmpOut, out);
fs.chmodSync(out, 0o600);  // 个人用量数据仅本用户可读
console.log('data.js updated:', payload.daily.length, 'days,', payload.sessions.length, 'sessions');
EOF
