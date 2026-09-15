#!/usr/bin/env python3
"""e2e 测试的静态服务器：服务**真实的 index.html** + 合成 data.js。

为什么要单独起服务，而不是直接用 serve.py：
  - serve.py 服务的是仓库里真实的 data.js（用户个人数据），断言会随数据漂移；
  - 这里用合成 fixture，且日期相对"今天"生成，测试不会随时间腐化；
  - /refresh 被 stub 掉，e2e 不去跑真实采集。

用法：python3 tests/e2e/server.py [port]
"""
import json
import sys
from datetime import date, timedelta
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]      # 仓库根目录（index.html 所在处）
PORT = int(sys.argv[1]) if len(sys.argv) > 1 else 8951


def _mb(model, inp=0, out=0, cr=0, cc=0, cost=None, cost_est=None, has_cost=False):
    return {
        "modelName": model, "inputTokens": inp, "outputTokens": out,
        "cacheReadTokens": cr, "cacheCreationTokens": cc,
        "cost": cost, "hasCost": has_cost,
        **({"costEst": cost_est} if cost_est is not None else {}),
    }


def build_fixture():
    """覆盖已被修复的每一类口径问题（见 AGENTS.md「测试」一节）。

    角色：
      acct    有记账成本（costUsd）＋ gpt-x 有记账成本
      est     无记账成本、走 models.dev 估算；其中一个模型无价目（应显示「未定价」）
      evonly  本地无 token，只有活动量（应触发"活动量"模式）
    """
    today = date.today()

    def d(n):
        return (today - timedelta(days=n)).isoformat()

    def ts(n, hh="10:00:00"):
        return f"{d(n)}T{hh}+08:00"

    daily = [
        # 同一天既有记账(acct) 又有估算(est)：KPI 成本必须两者相加
        {"date": d(1), "agent": "acct", "inputTokens": 100, "outputTokens": 50,
         "cacheReadTokens": 200, "cacheCreationTokens": 10, "totalTokens": 360,
         "costUsd": 1.5, "costEst": None, "events": 0,
         "modelBreakdowns": [_mb("gpt-x", 100, 50, 200, 10, cost=1.5, has_cost=True)]},
        {"date": d(1), "agent": "est", "inputTokens": 20, "outputTokens": 10,
         "cacheReadTokens": 30, "cacheCreationTokens": 0, "totalTokens": 60,
         "costUsd": None, "costEst": 2.25, "events": 0,
         "modelBreakdowns": [_mb("claude-y", 20, 10, 30, 0, cost=0, cost_est=2.25)]},
        {"date": d(2), "agent": "acct", "inputTokens": 40, "outputTokens": 20,
         "cacheReadTokens": 80, "cacheCreationTokens": 0, "totalTokens": 140,
         "costUsd": 0.5, "costEst": None, "events": 0,
         "modelBreakdowns": [_mb("gpt-x", 40, 20, 80, 0, cost=0.5, has_cost=True)]},
        # 无公开价目的模型：应显示「未定价」，而不是 ≈$0.00
        {"date": d(2), "agent": "est", "inputTokens": 10, "outputTokens": 5,
         "cacheReadTokens": 0, "cacheCreationTokens": 0, "totalTokens": 15,
         "costUsd": None, "costEst": None, "events": 0,
         "modelBreakdowns": [_mb("no-price-model", 10, 5, 0, 0, cost=0)]},
        # 只有活动量的来源
        {"date": d(3), "agent": "evonly", "inputTokens": 0, "outputTokens": 0,
         "cacheReadTokens": 0, "cacheCreationTokens": 0, "totalTokens": 0,
         "costUsd": None, "costEst": None, "events": 7, "modelBreakdowns": []},
        # 只有"今天"的记录：用于验证会话表/统计是否跟随时间范围
        {"date": d(0), "agent": "acct", "inputTokens": 7, "outputTokens": 3,
         "cacheReadTokens": 0, "cacheCreationTokens": 0, "totalTokens": 10,
         "costUsd": 0.25, "costEst": None, "events": 0,
         "modelBreakdowns": [_mb("gpt-x", 7, 3, 0, 0, cost=0.25, has_cost=True)]},
        # proj-shared：同一个项目名被**两个** agent 使用（acct 有项目级日粒度、
        # est 只有会话记录）。回归点：兜底曾按"项目名"全局判断有无日粒度，
        # 于是 est 的会话被 acct 的日粒度挡住 → 下钻时整段丢失
        {"date": d(4), "agent": "acct", "inputTokens": 1000, "outputTokens": 0,
         "cacheReadTokens": 0, "cacheCreationTokens": 0, "totalTokens": 1000,
         "costUsd": 1.0, "costEst": None, "events": 0,
         "modelBreakdowns": [_mb("gpt-x", 1000, 0, 0, 0, cost=1.0, has_cost=True)]},
    ]

    project_daily = [
        {"date": d(1), "agent": "acct", "project": "proj-a", "inputTokens": 100,
         "outputTokens": 50, "cacheReadTokens": 200, "cacheCreationTokens": 10,
         "totalTokens": 360, "costUsd": 1.5, "costEst": None, "events": 0,
         "modelBreakdowns": [_mb("gpt-x", 100, 50, 200, 10, cost=1.5, has_cost=True)]},
        {"date": d(2), "agent": "acct", "project": "proj-a", "inputTokens": 40,
         "outputTokens": 20, "cacheReadTokens": 80, "cacheCreationTokens": 0,
         "totalTokens": 140, "costUsd": 0.5, "costEst": None, "events": 0,
         "modelBreakdowns": [_mb("gpt-x", 40, 20, 80, 0, cost=0.5, has_cost=True)]},
        {"date": d(1), "agent": "est", "project": "proj-b", "inputTokens": 20,
         "outputTokens": 10, "cacheReadTokens": 30, "cacheCreationTokens": 0,
         "totalTokens": 60, "costUsd": None, "costEst": 2.25, "events": 0,
         "modelBreakdowns": [_mb("claude-y", 20, 10, 30, 0, cost=0, cost_est=2.25)]},
        # 与 daily 里 d(4) 的 acct 行对应：同名项目下 acct 有日粒度、est 没有
        {"date": d(4), "agent": "acct", "project": "proj-shared", "inputTokens": 1000,
         "outputTokens": 0, "cacheReadTokens": 0, "cacheCreationTokens": 0,
         "totalTokens": 1000, "costUsd": 1.0, "costEst": None, "events": 0,
         "modelBreakdowns": [_mb("gpt-x", 1000, 0, 0, 0, cost=1.0, has_cost=True)]},
    ]

    sessions = [
        {"sessionId": "s-multi", "agent": "acct", "project": "proj-a",
         "lastActivity": ts(1), "modelsUsed": ["claude-y", "gpt-x"],
         "inputTokens": 100, "outputTokens": 50, "cacheReadTokens": 200,
         "cacheCreationTokens": 10, "totalTokens": 360, "costUsd": 1.5, "events": 0},
        {"sessionId": "s-single", "agent": "est", "project": "proj-b",
         "lastActivity": ts(1), "modelsUsed": ["claude-y"],
         "inputTokens": 20, "outputTokens": 10, "cacheReadTokens": 30,
         "cacheCreationTokens": 0, "totalTokens": 60, "costUsd": None,
         "costEst": 2.25, "events": 0},
        {"sessionId": "s-today", "agent": "acct", "project": "proj-a",
         "lastActivity": ts(0), "modelsUsed": ["gpt-x"],
         "inputTokens": 7, "outputTokens": 3, "cacheReadTokens": 0,
         "cacheCreationTokens": 0, "totalTokens": 10, "costUsd": 0.25, "events": 0},
        # 只存在于会话记录里的项目（projectDaily 中没有）：下钻后不应归零
        {"sessionId": "s-sesonly", "agent": "evonly", "project": "proj-session-only",
         "lastActivity": ts(3), "modelsUsed": ["gpt-x"],
         "inputTokens": 0, "outputTokens": 0, "cacheReadTokens": 0,
         "cacheCreationTokens": 0, "totalTokens": 0, "costUsd": None, "events": 7},
        # 同上，但该 agent 在别处**另有**项目级明细：回归点 —— 曾按 agent 判断兜底，
        # 会把这类项目整盘归零（真实案例：cursor cloud agent）
        {"sessionId": "s-acct-orphan", "agent": "acct", "project": "proj-acct-orphan",
         "lastActivity": ts(1), "modelsUsed": ["gpt-x"],
         "inputTokens": 0, "outputTokens": 0, "cacheReadTokens": 0,
         "cacheCreationTokens": 0, "totalTokens": 0, "costUsd": None, "events": 3},
        # 同名项目 proj-shared 下**只**有会话的那个 agent（acct 另有日粒度）：
        # 项目下钻时必须被兜底补上，且不能与 acct 的日粒度重复计
        {"sessionId": "s-shared-est", "agent": "est", "project": "proj-shared",
         "lastActivity": ts(4), "modelsUsed": ["claude-y"],
         "inputTokens": 400, "outputTokens": 0, "cacheReadTokens": 0,
         "cacheCreationTokens": 0, "totalTokens": 400, "costUsd": None,
         "costEst": 0.4, "events": 0},
    ]

    hourly = [
        {"hour": f"{d(1)} 10", "agent": "acct", "model": "gpt-x", "project": "proj-a",
         "inputTokens": 100, "outputTokens": 50, "cacheReadTokens": 200,
         "cacheCreationTokens": 10, "totalTokens": 360, "costUsd": 1.5, "events": 0},
        {"hour": f"{d(1)} 11", "agent": "est", "model": "claude-y", "project": "proj-b",
         "inputTokens": 20, "outputTokens": 10, "cacheReadTokens": 30,
         "cacheCreationTokens": 0, "totalTokens": 60, "costUsd": None,
         "costEst": 2.25, "events": 0},
    ]

    return {
        "generatedAt": f"{d(0)}T04:00:00+00:00",
        "agents": [
            {"id": "acct", "label": "Acct Agent", "color": "#2563eb",
             "hasTokens": True, "hasCost": True, "source": "test"},
            {"id": "est", "label": "Est Agent", "color": "#10b981",
             "hasTokens": True, "hasCost": False, "source": "test"},
            {"id": "evonly", "label": "Events Only", "color": "#ef4444",
             "hasTokens": False, "hasCost": False, "source": "test"},
        ],
        "modelDevs": {"gpt-x": "OpenAI", "claude-y": "Anthropic", "no-price-model": "Custom"},
        "daily": daily,
        "projectDaily": project_daily,
        "hourly": hourly,
        "sessions": sorted(sessions, key=lambda s: s["totalTokens"], reverse=True),
        "warnings": [],
    }


FIXTURE_JS = "window.DASHBOARD_DATA = " + json.dumps(build_fixture(), ensure_ascii=False) + ";\n"


class Handler(SimpleHTTPRequestHandler):
    def __init__(self, *a, **kw):
        super().__init__(*a, directory=str(ROOT), **kw)

    def _send(self, body, ctype):
        data = body.encode()
        self.send_response(200)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(data)

    def do_GET(self):
        path = self.path.split("?", 1)[0]
        if path in ("/", "/index.html"):
            self._send((ROOT / "index.html").read_text(), "text/html; charset=utf-8")
            return
        if path == "/data.js":                      # 合成数据，不碰仓库里真实的 data.js
            self._send(FIXTURE_JS, "application/javascript; charset=utf-8")
            return
        self.send_error(404)

    def do_POST(self):
        if self.path == "/refresh":                 # e2e 不跑真实采集
            self._send(json.dumps({"ok": True, "log": "stubbed"}), "application/json")
            return
        self.send_error(404)

    def log_message(self, *a):
        pass


if __name__ == "__main__":
    print(f"e2e fixture server: http://127.0.0.1:{PORT}/index.html", file=sys.stderr)
    ThreadingHTTPServer(("127.0.0.1", PORT), Handler).serve_forever()
