#!/usr/bin/env python3
"""统一采集本机所有 agent 的 token 用量，生成 data.js。

数据源：
  1. ccusage（kimi / codex / opencode / kilo / pi / claude / gemini ... 自动探测）
  2. commandcode  ~/.commandcode/projects/*/*.jsonl          行内 "usage" 字段
  3. dimcode      ~/.dimcode/v2/dimcode.sqlite               usage_run_stats 表
  4. devin        ~/.local/share/devin/cli/sessions.db       message_nodes.metrics
  5. kimix        ~/.kimix/sessions/*/*/updates.jsonl        turn_completed.usage
  6. cursor       cursor.com 用量 CSV 导出（用本地存的登录态只读拉取，失败降级为本地活动量）
                  ~/.cursor/projects/*/agent-transcripts + ~/.cursor/chats/*/store.db
  7. antigravity  ~/.gemini/antigravity{,-ide}/conversations/*.db   IDE 应用
     antigravity-cli ~/.gemini/antigravity-cli/conversations/*.db   agy CLI
                  gen_metadata 为明文 protobuf，可离线解出真实 token

注意：~/.claude/projects 下的文件全是 dimcode/commandcode 的 mirror，不计，避免重复。
仅使用标准库；所有 sqlite 先复制到临时目录再读，避免锁/损坏正在使用的 WAL 库。
"""
import hashlib
import json
import os
import pickle
import re
import shutil
import sqlite3
import subprocess
import sys
import tempfile
import threading
import time
from collections import defaultdict
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone
from pathlib import Path

DIR = Path(__file__).resolve().parent
HOME = Path.home()
CCUSAGE = DIR / "node_modules" / ".bin" / "ccusage"

# agent id -> 展示名 / 是否产出 token 数据 / 是否有成本
AGENT_META = {
    "kimi":        {"label": "Kimi Code",   "color": "#2563eb"},
    "codex":       {"label": "Codex",       "color": "#10b981"},
    "commandcode": {"label": "CommandCode", "color": "#8b5cf6"},
    "dimcode":     {"label": "DimCode",     "color": "#f59e0b"},
    "devin":       {"label": "Devin",       "color": "#ef4444"},
    "kimix":       {"label": "KimiX",       "color": "#06b6d4"},
    "cursor":      {"label": "Cursor",      "color": "#64748b"},
    "antigravity": {"label": "Antigravity", "color": "#84cc16"},
    "antigravity-cli": {"label": "Antigravity CLI", "color": "#22c55e"},
    "opencode":    {"label": "OpenCode",    "color": "#ec4899"},
    "kilo":        {"label": "Kilo",        "color": "#f97316"},
    "pi":          {"label": "Pi",          "color": "#a855f7"},
    "claude":      {"label": "Claude Code", "color": "#d97706"},
    "gemini":      {"label": "Gemini CLI",  "color": "#3b82f6"},
    "amp":         {"label": "Amp",         "color": "#14b8a6"},
    "droid":       {"label": "Droid",       "color": "#eab308"},
    "goose":       {"label": "Goose",       "color": "#78716c"},
    "qwen":        {"label": "Qwen",        "color": "#7c3aed"},
    "copilot":     {"label": "Copilot",     "color": "#0ea5e9"},
    "codebuff":    {"label": "Codebuff",    "color": "#dc2626"},
    "hermes":      {"label": "Hermes",      "color": "#059669"},
    "openclaw":    {"label": "OpenClaw",    "color": "#9333ea"},
    "grok":        {"label": "Grok",        "color": "#111827"},
}
# hasTokens 动态判定：采集到 token 的 agent 会被打上 tokens 标记；
# cursor/antigravity 在数据源不可用时自动退化为活动量视图。
NO_COST = {"devin", "kimix", "cursor", "antigravity", "antigravity-cli"}  # 无可靠成本数据

SOURCE_DESC = {
    "kimi":        "~/.kimi-code/sessions (ccusage)",
    "codex":       "~/.codex/sessions (ccusage)",
    "commandcode": "~/.commandcode/projects",
    "dimcode":     "~/.dimcode/v2/dimcode.sqlite",
    "devin":       "~/.local/share/devin/cli/sessions.db",
    "kimix":       "~/.kimix/sessions",
    "cursor":      "cursor.com 用量导出 + ~/.cursor 本地日志",
    "antigravity": "~/.gemini/antigravity{,-ide}/conversations",
    "antigravity-cli": "~/.gemini/antigravity-cli/conversations",
    "opencode":    "~/.local/share/opencode (ccusage)",
    "kilo":        "~/.local/share/kilo (ccusage)",
    "pi":          "ccusage 自动探测",
    "claude":      "~/.claude/projects (ccusage)",
    "gemini":      "~/.gemini (ccusage)",
}

# ---------- 模型价目（models.dev，OpenCode 同源数据库） ----------
# https://models.dev/api.json -> {provider: {models: {id: {cost: {input,output,cache_read,cache_write}}}}}
# 价目单位为 USD / 1M tokens。用于给本地无记账成本的来源（devin/antigravity/kimix/cursor）
# 提供 costEst 估算；有真实记账成本的来源不受影响。USAGE_DASH_PRICES=0 可禁用。
PRICES_URL = "https://models.dev/api.json"
PRICES_CACHE = DIR / ".cache" / "models-api.json"
PRICES_TTL = 24 * 3600


def _norm_model(s):
    return re.sub(r"[^a-z0-9]+", "-", s.lower()).strip("-")


CANON_RAWS = {}   # canon -> {剥离强度后缀前的写法}：供输出前复核归并
_canon_cache = {}  # 原始名 -> canon：同一模型名会被反复查询（实测 3.9 万次/次刷新）


def canon_model(name):
    """模型名规范化：同一模型在不同来源写法不同（[pi] deepseek-v4-flash /
    deepseek/deepseek-v4.1-flash / cursor-grok-4.5-high / xxx-expires-on-0910 /
    claude-sonnet-4-5-20250929），统一收敛到 canonical key 以去重聚合"""
    if not name:
        return "?"
    cached = _canon_cache.get(name)
    if cached is not None:
        return cached          # 同一写法必然得到同一 canon，CANON_RAWS 无需重复登记
    s = str(name).strip().lower()
    s = re.sub(r"^\[[^\]]+\]\s*", "", s)   # [pi] xxx
    s = s.rsplit("/", 1)[-1]               # provider/model -> model
    s = re.sub(r"^cursor-", "", s)         # cursor-grok-4.5 -> grok-4.5
    s = re.sub(r"-expires-on-\d+$", "", s)
    s = re.sub(r"-\d{8}$", "", s)          # 末尾日期戳 -20250929
    s = re.sub(r"[^a-z0-9._~-]+", "-", s).strip("-")   # 清洗非 ASCII/括号等乱码字符
    pre = s
    # 思考强度后缀不是独立模型：xxx-low/medium/high/xhigh 归并到基础模型。
    # 输出前按 CANON_RAWS 复核：若该基础名只对应单一写法（真实型号名本身以
    # -high 结尾，如 swe-2-high），则还原原名，避免改名无中生有
    s = re.sub(r"(-(minimal|low|medium|high|xhigh))+$", "", s)
    if not s or s.isdigit():               # 退化结果（如 火山)-0 -> 0）视为无模型信息
        _canon_cache[name] = "?"
        return "?"
    with _state_lock:                      # CANON_RAWS 会被所有采集线程写
        CANON_RAWS.setdefault(s, set()).add(pre)
    _canon_cache[name] = s
    return s


def _build_price_map(raw):
    """api.json -> {规范化模型ID: {in,out,cr,cw}}

    同一模型名常同时出现在一手厂商与 openrouter 等聚合商下，价格可能相差数倍
    （缓存实测 950 个多 provider 模型中有 599 个价格不同，如 o3 2.0 vs 10.0 USD/M），
    因此优先一手厂商报价、聚合商兜底。

    但 FIRST_PARTY_PROVIDERS 里还混着订阅/套餐 SKU（xxx-coding-plan / xxx-token-plan）
    与纯转发 provider：它们的 cost 全为 0（表示"套餐内不按 token 计费"），若直接参与
    "一手优先"会把真实单价覆盖成 0（实测 55 个模型被归零，如 deepseek-v4-pro、glm-5.2、
    kimi-k3）。所以分四档按优先级合并：非零一手 > 非零其他 > 零一手 > 零其他。
    """
    buckets = {}   # (is_first, is_zero) -> {key: price}
    for pk, prov in raw.items():
        is_first = pk in FIRST_PARTY_PROVIDERS
        for mid, m in (prov.get("models") or {}).items():
            c = m.get("cost")
            if not c:
                continue
            key = _norm_model(mid)
            if not key:
                continue
            price = {
                "in": float(c.get("input") or 0),
                "out": float(c.get("output") or 0),
                "cr": float(c.get("cache_read") or 0),
                "cw": float(c.get("cache_write") or 0),
            }
            is_zero = not any(price.values())
            buckets.setdefault((is_first, is_zero), {}).setdefault(key, price)
    out = {}
    for tier in ((False, True), (True, True), (False, False), (True, False)):
        out.update(buckets.get(tier, {}))
    return out


# models.dev 里的一手 provider（模型开发商自营 API）key -> (展示名, 该开发商的模型名族前缀)。
# 归属判定要求两边一致：该模型被这家 provider 收录 且 模型名以它的族前缀开头 ——
# 这样 alibaba/dashscope 托管的 deepseek-* 不会错归 Alibaba；
# 一手 provider 没收录或前缀不符时返回 None，由前端规则兜底。
FIRST_PARTY_PROVIDERS = {
    "anthropic": ("Anthropic", ["claude"]),
    "openai": ("OpenAI", ["gpt", "o1", "o3", "o4", "codex", "chatgpt", "openai"]),
    "google": ("Google", ["gemini", "gemma"]),
    "xai": ("xAI", ["grok"]),
    "deepseek": ("DeepSeek", ["deepseek"]),
    "moonshotai": ("Moonshot AI", ["kimi", "moonshot", "k2", "k3"]),
    "moonshotai-cn": ("Moonshot AI", ["kimi", "moonshot", "k2", "k3"]),
    "kimi-for-coding": ("Moonshot AI", ["kimi", "k2", "k3"]),
    "zhipuai": ("Zhipu AI", ["glm", "chatglm"]),
    "zhipuai-coding-plan": ("Zhipu AI", ["glm", "chatglm"]),
    "zai": ("Zhipu AI", ["glm"]),
    "zai-coding-plan": ("Zhipu AI", ["glm"]),
    "alibaba": ("Alibaba", ["qwen", "qwq"]),
    "alibaba-cn": ("Alibaba", ["qwen", "qwq"]),
    "alibaba-coding-plan": ("Alibaba", ["qwen", "qwq"]),
    "alibaba-token-plan": ("Alibaba", ["qwen", "qwq"]),
    "meta": ("Meta", ["llama"]),
    "llama": ("Meta", ["llama"]),
    "mistral": ("Mistral AI", ["mistral", "codestral", "devstral", "pixtral", "magistral", "voxtral"]),
    "minimax": ("MiniMax", ["minimax", "abab"]),
    "minimax-cn": ("MiniMax", ["minimax", "abab"]),
    "minimax-coding-plan": ("MiniMax", ["minimax", "abab"]),
    "minimax-cn-coding-plan": ("MiniMax", ["minimax", "abab"]),
    "xiaomi": ("Xiaomi", ["mimo"]),
    "xiaomi-token-plan-cn": ("Xiaomi", ["mimo"]),
    "xiaomi-token-plan-ams": ("Xiaomi", ["mimo"]),
    "xiaomi-token-plan-sgp": ("Xiaomi", ["mimo"]),
    "stepfun": ("StepFun", ["step"]),
    "stepfun-ai": ("StepFun", ["step"]),
    "stepfun-step-plan": ("StepFun", ["step"]),
    "stepfun-ai-step-plan": ("StepFun", ["step"]),
    "tencent-tokenhub": ("Tencent", ["hy", "hunyuan"]),
    "tencent-coding-plan": ("Tencent", ["hy", "hunyuan"]),
    "tencent-token-plan": ("Tencent", ["hy", "hunyuan"]),
    "volcengine": ("ByteDance", ["doubao", "seed", "skylark"]),
    "volcengine-coding-plan": ("ByteDance", ["doubao", "seed", "skylark"]),
    "nova": ("Amazon", ["nova"]),
    "cohere": ("Cohere", ["command", "c4ai"]),
    "nvidia": ("NVIDIA", ["nemotron"]),
    "databricks": ("Databricks", ["dbrx"]),
    "sakana": ("Sakana AI", ["sakana"]),
    "sensenova": ("SenseTime", ["sensenova", "sense"]),
    "upstage": ("Upstage", ["solar"]),
    "poolside": ("Poolside", ["poolside"]),
    "arcee": ("Arcee AI", ["arcee", "trinity"]),
    "inception": ("Inception Labs", ["mercury"]),
    "bailing": ("Ant Group", ["bailing", "ling"]),
    "longcat": ("Meituan", ["longcat"]),
    "perplexity": ("Perplexity", ["sonar"]),
    "microsoft": ("Microsoft", ["phi"]),
    "snowflake-cortex": ("Snowflake", ["arctic"]),
}


def _build_dev_map(raw):
    """规范化模型ID -> 开发商：仅当一手 provider 收录了它自家族系的模型才采信"""
    out = {}
    for pk, prov in raw.items():
        info = FIRST_PARTY_PROVIDERS.get(pk)
        if not info:
            continue
        label, families = info
        for mid in (prov.get("models") or {}):
            key = _norm_model(mid)
            if key and key not in out and any(key.startswith(f) for f in families):
                out[key] = label
    return out


def load_prices():
    """下载 models.dev 数据（24h 本地缓存，失败用过期缓存兜底）；返回 (价目表, 开发商表)"""
    if os.environ.get("USAGE_DASH_PRICES") == "0":
        return {}, {}
    import urllib.request
    raw = None
    try:
        if PRICES_CACHE.exists() and time.time() - PRICES_CACHE.stat().st_mtime < PRICES_TTL:
            raw = json.loads(PRICES_CACHE.read_text())
    except Exception:
        pass
    if raw is None:
        try:
            timeout = float(os.environ.get("USAGE_DASH_PRICES_TIMEOUT", "20"))
            req = urllib.request.Request(PRICES_URL, headers={"User-Agent": "local-usage-dashboard"})
            with urllib.request.urlopen(req, timeout=timeout) as r:
                raw = json.loads(r.read().decode("utf-8"))
            PRICES_CACHE.parent.mkdir(exist_ok=True)
            PRICES_CACHE.write_text(json.dumps(raw))
        except Exception as e:
            warnings.append(f"models.dev 价目下载失败，使用缓存/跳过估算: {e}")
            try:
                raw = json.loads(PRICES_CACHE.read_text())
            except Exception:
                return {}, {}
    try:
        return _build_price_map(raw), _build_dev_map(raw)
    except Exception:
        return {}, {}


_lookup_cache = {}   # (id(表), 模型名) -> 表项：最长包含匹配要遍历整表，缓存后省下重复扫描


def _lookup_map(tbl, name):
    """模型名 -> 表项。依次尝试：规范化全名精确、basename 精确、最长包含匹配（防误判要求 >=6 字符）"""
    if not name or not tbl:
        return None
    ck = (id(tbl), name)
    if ck in _lookup_cache:
        return _lookup_cache[ck]
    full = _norm_model(name)
    base = _norm_model(name.rsplit("/", 1)[-1])
    for cand in (full, base):
        if cand in tbl:
            _lookup_cache[ck] = tbl[cand]
            return tbl[cand]
    best = None
    for cand in (full, base):
        for pid, p in tbl.items():
            if len(pid) >= 6 and pid in cand and (best is None or len(pid) > len(best[0])):
                best = (pid, p)
    res = best[1] if best else None
    _lookup_cache[ck] = res
    return res


def price_of(prices, name):
    """模型名 -> 价目"""
    return _lookup_map(prices, name)


def est_cost(prices, name, inp, out, cr, cc):
    """按价目估算 USD 成本；无价目**或无用量**时返回 None

    没有用量时返回 None 而不是 0.0：否则前端会把"无 token 的行"渲染成 ≈$0.00，
    与「未定价」语义混淆（例如 cursor cloud agent 会话只保留活动量之后）。
    """
    p = price_of(prices, name)
    if p is None:
        return None
    if not (inp or out or cr or cc):
        return None
    return (inp * p["in"] + out * p["out"] + cr * p["cr"] + cc * p["cw"]) / 1e6


def _model_bucket():
    return {
        "inputTokens": 0, "outputTokens": 0, "cacheReadTokens": 0,
        "cacheCreationTokens": 0, "cost": 0.0, "hasCost": False,
    }


def _bucket():
    return {
        "inputTokens": 0, "outputTokens": 0, "cacheReadTokens": 0,
        "cacheCreationTokens": 0, "totalTokens": 0, "costUsd": 0.0,
        "costKnown": False, "events": 0, "models": defaultdict(_model_bucket),
    }


def _hourly_bucket():
    return {
        "inputTokens": 0, "outputTokens": 0, "cacheReadTokens": 0,
        "cacheCreationTokens": 0, "costUsd": 0.0, "costKnown": False, "events": 0,
    }


# daily_rows[(date, agent)] / proj_rows[(date, agent, project)] -> 聚合桶
daily_rows = defaultdict(_bucket)
proj_rows = defaultdict(_bucket)   # 仅原生解析器可产出项目级用量
# hourly_rows[(hourStr, agent, model, project)] -> 小时粒度桶（详细记录/分时活跃用）
hourly_rows = defaultdict(_hourly_bucket)
sessions = []          # 会话级记录
seen_session = set()   # (agent, sessionId) 去重
agent_found = {}       # agent -> 统计信息
warnings = []

# 各来源现在并行采集（见 main），以下共享状态必须串行化：
# dict/list 的"读-改-写"（如 b["inputTokens"] += x）在 CPython 下不是原子操作，
# 并发写会丢数据。解析是 CPU 密集、加锁点都在单次累加上，锁开销可忽略。
_state_lock = threading.RLock()


_date_re = re.compile(r"^\d{4}-\d{2}-\d{2}$")


def day_of_ts(ts):
    """epoch 秒或毫秒 -> 本地日期 YYYY-MM-DD；无法解析时返回 None（调用方跳过）"""
    try:
        if not ts:                  # None / 0：0 会被当成 1970-01-01 污染全站日期轴
            return None
        ts = float(ts)
        if ts > 1e12:
            ts /= 1000.0
        return datetime.fromtimestamp(ts).strftime("%Y-%m-%d")
    except (TypeError, ValueError, OverflowError, OSError):
        return None


def day_of_iso(s):
    """ISO 时间串 -> 本地日期；解析失败只在形如 YYYY-MM-DD 时兜底，否则返回 None"""
    try:
        dt = datetime.fromisoformat(str(s).replace("Z", "+00:00"))
        return dt.astimezone().strftime("%Y-%m-%d")
    except Exception:
        head = str(s or "")[:10]
        return head if _date_re.match(head) else None


def hour_of_iso(s):
    """ISO 时间串 -> 本地小时 0-23"""
    try:
        return datetime.fromisoformat(s.replace("Z", "+00:00")).astimezone().hour
    except Exception:
        return None


def hour_of_ts(ts):
    """epoch 秒或毫秒 -> 本地小时 0-23"""
    if ts > 1e12:
        ts /= 1000.0
    return datetime.fromtimestamp(ts).hour


def _fresh(inp, cr):
    """部分来源的 inputTokens 含 cacheRead（commandcode/dimcode/kimix），
    归一化为 ccusage 口径：input = 未命中缓存的新输入，cacheRead 单列。

    注意 devin 与 antigravity 的 input 本身不含 cacheRead，不要对它们调用本函数
    （实测 devin 95.7% 的事件 cache_read_tokens > input_tokens）。
    """
    return max(0, (inp or 0) - (cr or 0))


def _accum(rows, key, inp=0, out=0, cr=0, cc=0, cost=None,
           model=None, m_inp=None, m_out=None, m_cr=None, m_cc=None, m_cost=None,
           events=0):
    b = rows[key]
    with _state_lock:
        b["inputTokens"] += inp
        b["outputTokens"] += out
        b["cacheReadTokens"] += cr
        b["cacheCreationTokens"] += cc
        b["totalTokens"] += inp + out + cr + cc
        if cost is not None:
            b["costUsd"] += cost
            b["costKnown"] = True
        if events:
            b["events"] += events
        if model:
            mb = b["models"][canon_model(model)]
            # m_* 为 None 表示"未单独给出该模型的量"，回退到行级总量；
            # 不能用 `x or y`，否则显式传 0（该模型确实为 0）会被当成缺失
            mb["inputTokens"] += inp if m_inp is None else m_inp
            mb["outputTokens"] += out if m_out is None else m_out
            mb["cacheReadTokens"] += cr if m_cr is None else m_cr
            mb["cacheCreationTokens"] += cc if m_cc is None else m_cc
            if m_cost is not None:
                mb["cost"] += m_cost
                mb["hasCost"] = True
            elif cost is not None:
                mb["cost"] += cost
                mb["hasCost"] = True


_bad_date_seen = set()


def _warn_bad_date(agent, date):
    """日期无法解析时告警（同一 agent+日期只报一次，避免刷屏）"""
    key = (agent, str(date))
    with _state_lock:
        if key in _bad_date_seen:
            return
        _bad_date_seen.add(key)
        warnings.append(f"跳过日期无法解析的 {agent} 记录: date={date!r}")


def add_usage(date, agent, project=None, hour=None, **kw):
    """写入日粒度 + 项目粒度 + 小时粒度三个桶（后两者需要相应上下文）。

    返回是否写入成功。日期非法时返回 False，调用方必须据此跳过同一事件的
    会话累加，否则会出现"会话里有、日粒度里没有"（会话之和 > 日之和）。
    """
    if not date or not _date_re.match(str(date)):
        _warn_bad_date(agent, date)
        return False
    _accum(daily_rows, (date, agent), **kw)
    if project:
        _accum(proj_rows, (date, agent, project), **kw)
    if hour is not None:
        with _state_lock:
            hb = hourly_rows[(f"{date} {hour:02d}", agent,
                              canon_model(kw.get("model")), project or "")]
            hb["inputTokens"] += kw.get("inp") or 0
            hb["outputTokens"] += kw.get("out") or 0
            hb["cacheReadTokens"] += kw.get("cr") or 0
            hb["cacheCreationTokens"] += kw.get("cc") or 0
            if kw.get("cost") is not None:
                hb["costUsd"] += kw["cost"]
                hb["costKnown"] = True
            hb["events"] += kw.get("events") or 0
    return True


def add_session(agent, sid, project="", last="", models=None,
                inp=0, out=0, cr=0, cc=0, cost=None, events=0):
    key = (agent, sid)
    with _state_lock:
        if key in seen_session:
            return
        seen_session.add(key)
        sessions.append({
            "sessionId": sid, "agent": agent, "project": project,
            "lastActivity": last,
            "modelsUsed": sorted({canon_model(m) for m in (models or [])}),
            "inputTokens": inp, "outputTokens": out,
            "cacheReadTokens": cr, "cacheCreationTokens": cc,
            "totalTokens": inp + out + cr + cc,
            "costUsd": cost, "events": events,
        })


def mark(agent, **kw):
    with _state_lock:
        a = agent_found.setdefault(agent, {"sessions": 0, "days": set()})
        a.update({k: v for k, v in kw.items() if v is not None})


# ---------------------------------------------------------------- ccusage
def _ccusage_raw():
    """跑一次 ccusage 拿原始 JSON 文本（None = 不可用）。
    单独抽出来是因为它的输出既是指纹的一部分，也是缓存未命中时的必需输入。"""
    if not CCUSAGE.exists():
        warnings.append("ccusage 未安装（node_modules/.bin/ccusage 不存在），跳过 kimi/codex 等来源")
        return None
    try:
        r = subprocess.run(
            [str(CCUSAGE), "daily", "--json", "--offline",
             "--by-agent", "--sections", "daily,monthly,session"],
            capture_output=True, text=True, timeout=600, cwd=str(DIR))
        if r.returncode != 0:
            warnings.append(f"ccusage 执行失败: {r.stderr.strip()[:300]}")
            return None
        return r.stdout
    except Exception as e:
        warnings.append(f"ccusage 异常: {e}")
        return None


def collect_ccusage(raw=None):
    if raw is None:
        raw = _ccusage_raw()
    if raw is None:
        return
    try:
        data = json.loads(raw)
    except Exception as e:
        warnings.append(f"ccusage 输出解析失败: {e}")
        return

    agents_seen = set()
    agents_daily = set()   # 仅 daily 段有数据的 agent 才算"可产出 token"
    # daily[]: {period, agents:[{agent, tokens..., totalCost, modelBreakdowns[]}]}
    for row in data.get("daily") or []:
        date = row.get("period") or row.get("date")
        if not date or not _date_re.match(str(date)):
            # 这里会直接写 daily_rows[...]["models"]，绕过 add_usage 的守卫，
            # 必须在整行处理前拦掉，否则会留下"0 token 但有 modelBreakdowns"的日行
            _warn_bad_date("ccusage", date)
            continue
        for ag in row.get("agents") or []:
            agent = ag.get("agent") or "unknown"
            agents_seen.add(agent)
            agents_daily.add(agent)
            models = ag.get("modelBreakdowns") or []
            add_usage(
                date, agent,
                inp=ag.get("inputTokens", 0), out=ag.get("outputTokens", 0),
                cr=ag.get("cacheReadTokens", 0), cc=ag.get("cacheCreationTokens", 0),
                cost=ag.get("totalCost"))
            for mb in models:
                with _state_lock:
                    b = daily_rows[(date, agent)]["models"][canon_model(mb.get("modelName"))]
                    b["inputTokens"] += mb.get("inputTokens", 0)
                    b["outputTokens"] += mb.get("outputTokens", 0)
                    b["cacheReadTokens"] += mb.get("cacheReadTokens", 0)
                    b["cacheCreationTokens"] += mb.get("cacheCreationTokens", 0)
                    if mb.get("cost") is not None:
                        b["cost"] += mb["cost"]
                        b["hasCost"] = True

    # session[]: {agent, period=sessionId, metadata{projectPath,lastActivity}, ...}
    for s in data.get("session") or []:
        agent = s.get("agent") or "unknown"
        agents_seen.add(agent)
        meta = s.get("metadata") or {}
        add_session(
            agent, s.get("period") or "?",
            project=meta.get("projectPath") or "",
            last=meta.get("lastActivity") or "",
            models=s.get("modelsUsed") or [],
            inp=s.get("inputTokens", 0), out=s.get("outputTokens", 0),
            cr=s.get("cacheReadTokens", 0), cc=s.get("cacheCreationTokens", 0),
            cost=s.get("totalCost"))

    for a in agents_seen:
        mark(a, tokens=a in agents_daily or None)


# ------------------------------------------------------------ commandcode
def collect_commandcode():
    base = HOME / ".commandcode" / "projects"
    if not base.is_dir():
        return
    found = False
    for fp in base.glob("*/*.jsonl"):
        if fp.name.endswith(".checkpoints.jsonl"):
            continue
        sid = fp.stem
        project = fp.parent.name
        # 项目目录名是扁平化的绝对路径（users-zhaozhoubin-myprojects-agent-usage-dashboard）。
        # 只取最后一段会把不同项目压成同一个标签（实测 13 个目录撞成 11 个：
        # agent-usage-dashboard 与 kimi-usage-dashboard 都成了 "dashboard"，
        # market-data 与 trading-logic-wt-data 都成了 "data"），
        # 因此只剥掉 users-<用户名>- 前缀，其余原样保留以保证可区分
        parts = project.split("-")
        proj_short = ("~/" + "-".join(parts[2:])
                      if project.startswith("users-") and len(parts) > 2 else project)
        tot = {"i": 0, "o": 0, "cr": 0, "cc": 0, "cost": 0.0, "has_cost": False}
        models_used = set()
        last_ts = ""
        try:
            with open(fp, "r", errors="replace") as f:
                for line in f:
                    if '"usage"' not in line:
                        continue
                    try:
                        o = json.loads(line)
                    except Exception:
                        continue
                    u = o.get("usage")
                    if not isinstance(u, dict):
                        continue
                    found = True
                    model = o.get("model") or (o.get("message") or {}).get("model") or "?"
                    models_used.add(model)
                    ts = o.get("timestamp") or ""
                    if ts > last_ts:
                        last_ts = ts
                    date = day_of_iso(ts) if ts else day_of_ts(fp.stat().st_mtime)
                    inp = u.get("inputTokens") or u.get("input_tokens") or 0
                    out = u.get("outputTokens") or u.get("output_tokens") or 0
                    cr = u.get("cacheReadTokens") or u.get("cache_read_input_tokens") or 0
                    cc = u.get("cacheWriteTokens") or u.get("cache_creation_input_tokens") or 0
                    cost = u.get("costUsd")
                    tot["i"] += _fresh(inp, cr); tot["o"] += out; tot["cr"] += cr; tot["cc"] += cc
                    if cost is not None:
                        tot["cost"] += cost; tot["has_cost"] = True
                    if not add_usage(date, "commandcode", project=proj_short,
                                     hour=hour_of_iso(ts),
                                     inp=_fresh(inp, cr), out=out,
                                     cr=cr, cc=cc, cost=cost, model=model):
                        continue   # 日期非法：不要只进会话汇总
        except Exception as e:
            warnings.append(f"commandcode 解析失败（该文件剩余行已跳过）: {fp.name}: {e}")
            continue
        if found and (tot["i"] or tot["o"] or tot["cr"]):
            add_session("commandcode", sid, project=proj_short,
                        last=last_ts, models=sorted(models_used),
                        inp=tot["i"], out=tot["o"], cr=tot["cr"], cc=tot["cc"],
                        cost=tot["cost"] if tot["has_cost"] else None)
    if found:
        mark("commandcode", tokens=True)


def _copy_db(src, tmpdir, name):
    """把 sqlite 复制到临时目录再读，避免锁正在使用的库。

    优先用 sqlite3 的 backup API 拿一致快照（逐页复制，含 WAL 中已提交的事务）；
    只读打开失败（如 WAL 库在只读模式下的 -shm 限制）时回退为文件复制，
    此时必须把 -wal/-shm 一起复制，否则会读到旧快照。
    """
    dst = Path(tmpdir) / name
    if dst.exists():
        dst.unlink()
    try:
        sc = sqlite3.connect(f"file:{src}?mode=ro", uri=True)
        try:
            dc = sqlite3.connect(str(dst))
            try:
                sc.backup(dc)
            finally:
                dc.close()
        finally:
            sc.close()
        return dst
    except Exception:
        pass
    shutil.copy2(src, dst)
    for ext in ("-wal", "-shm"):
        if os.path.exists(str(src) + ext):
            shutil.copy2(str(src) + ext, str(dst) + ext)
    return dst


# ---------------------------------------------------------------- dimcode
def _open_ro(src, tmpdir, name):
    """只读打开 sqlite：优先直接打开原库，失败才复制到临时目录。

    只读连接不取写锁、不修改源库；WAL 模式下读者看到的是一致快照，
    因此可以安全地直读正在被使用的库，省掉整个库的复制（实测 devin 433MB 省 0.38s、
    dimcode 386MB 省 0.35s）。只有在直读受限时（如 -shm 不可写）才回退到复制。
    """
    try:
        con = sqlite3.connect(f"file:{src}?mode=ro", uri=True)
        con.execute("SELECT 1").fetchone()      # connect 是惰性的，这里真正打开一次
        return con
    except Exception:
        pass
    return sqlite3.connect(f"file:{_copy_db(src, tmpdir, name)}?mode=ro", uri=True)


def collect_dimcode(tmpdir):
    src = HOME / ".dimcode" / "v2" / "dimcode.sqlite"
    if not src.exists():
        return
    try:
        con = _open_ro(src, tmpdir, "dimcode.sqlite")
        con.row_factory = sqlite3.Row
        proj = {}
        try:
            for r in con.execute("SELECT sessionId, cwd, title FROM sessions"):
                proj[r["sessionId"]] = r["cwd"] or r["title"] or ""
        except Exception:
            pass
        agg = {}
        try:
            rows = con.execute(
                "SELECT sessionId, modelId, inputTokens, outputTokens,"
                " cacheReadTokens, cacheWriteTokens, cost, endedAt"
                " FROM usage_run_stats")
            for r in rows:
                sid = r["sessionId"] or "?"
                model = r["modelId"] or "?"
                date = day_of_iso(r["endedAt"] or "")
                cost = None
                if r["cost"]:
                    try:
                        cost = json.loads(r["cost"]).get("totalCostUsd")
                    except Exception:
                        pass
                inp, out = r["inputTokens"] or 0, r["outputTokens"] or 0
                cr, cc = r["cacheReadTokens"] or 0, r["cacheWriteTokens"] or 0
                p = proj.get(sid, "")
                if not add_usage(date, "dimcode",
                                 project="/".join(p.split("/")[-2:]) if p else "",
                                 hour=hour_of_iso(r["endedAt"] or ""),
                                 inp=_fresh(inp, cr), out=out, cr=cr,
                                 cc=cc, cost=cost, model=model):
                    continue   # 日期非法：不要只进会话汇总
                a = agg.setdefault(sid, {"i": 0, "o": 0, "cr": 0, "cc": 0,
                                         "cost": 0.0, "hc": False,
                                         "models": set(), "last": ""})
                a["i"] += _fresh(inp, cr); a["o"] += out; a["cr"] += cr; a["cc"] += cc
                a["models"].add(model)
                if cost is not None:
                    a["cost"] += cost; a["hc"] = True
                if (r["endedAt"] or "") > a["last"]:
                    a["last"] = r["endedAt"]
            for sid, a in agg.items():
                if a["i"] or a["o"]:
                    p = proj.get(sid, "")
                    add_session("dimcode", sid,
                                project="/".join(p.split("/")[-2:]) if p else "",
                                last=a["last"], models=sorted(a["models"]),
                                inp=a["i"], out=a["o"], cr=a["cr"], cc=a["cc"],
                                cost=a["cost"] if a["hc"] else None)
            if agg:
                mark("dimcode", tokens=True)
        except sqlite3.Error as e:
            warnings.append(f"dimcode 读取失败: {e}")
        con.close()
    except Exception as e:
        warnings.append(f"dimcode 异常: {e}")


# ------------------------------------------------------------------ devin
def collect_devin(tmpdir):
    src = HOME / ".local" / "share" / "devin" / "cli" / "sessions.db"
    if not src.exists():
        return
    try:
        con = _open_ro(src, tmpdir, "devin.db")
        con.row_factory = sqlite3.Row
        sess_meta = {}
        for r in con.execute("SELECT id, working_directory, model FROM sessions"):
            sess_meta[r["id"]] = {"wd": r["working_directory"] or "",
                                  "model": r["model"] or "?"}
        agg = {}
        seen_turn = set()
        found = False
        for r in con.execute("SELECT row_id, session_id, chat_message, created_at"
                             " FROM message_nodes"):
            cm = r["chat_message"]
            if '"metrics"' not in cm:
                continue
            try:
                o = json.loads(cm)
            except Exception:
                continue
            md = o.get("metadata") or {}
            m = md.get("metrics") or {}
            inp = m.get("input_tokens") or 0
            out = m.get("output_tokens") or 0
            if not inp and not out:
                continue
            sid = r["session_id"] or "?"
            # 同一轮推理的 message 会在 message_nodes 里重复落库（实测多为 2 份，
            # 少量 3 份），按 message_id/request_id 去重，否则用量成倍虚增
            turn = (sid, o.get("message_id") or md.get("request_id")
                    or str(r["row_id"]))
            if turn in seen_turn:
                continue
            seen_turn.add(turn)
            found = True
            cr = m.get("cache_read_tokens") or 0
            cc = m.get("cache_creation_tokens") or 0
            model = sess_meta.get(sid, {}).get("model", "?")
            # 真实发生时间在 chat_message.metadata.created_at；message_nodes.created_at
            # 会被会话重载整体改写（一次批量写入把历史节点覆盖成同一分钟），
            # 只能作为兜底，否则整段历史用量都会堆到重载那一刻
            dt = None
            ts_iso = md.get("created_at") or md.get("started_generation_at")
            if ts_iso:
                try:
                    dt = datetime.fromisoformat(
                        ts_iso.replace("Z", "+00:00")).astimezone()
                except Exception:
                    dt = None
            if dt is not None:
                date, hour, last = dt.strftime("%Y-%m-%d"), dt.hour, dt.isoformat()
            else:
                raw = r["created_at"] or 0
                raw = raw / 1000 if raw > 1e12 else raw
                date, hour = day_of_ts(raw), hour_of_ts(raw)
                last = datetime.fromtimestamp(raw).astimezone().isoformat() if raw else ""
            wd = sess_meta.get(sid, {}).get("wd", "")
            # devin 的 metrics.input_tokens 已经是"未命中缓存的新输入"（实测 95.7% 的
            # 事件 cache_read_tokens > input_tokens，若 input 含 cacheRead 则不可能），
            # 因此不能再套 _fresh，否则绝大多数行的 input 会被 clamp 成 0
            if not add_usage(date, "devin",
                             project="/".join(wd.split("/")[-2:]) if wd else "",
                             hour=hour,
                             inp=inp, out=out, cr=cr, cc=cc,
                             model=model):
                continue   # 日期非法：不要只进会话汇总
            a = agg.setdefault(sid, {"i": 0, "o": 0, "cr": 0, "cc": 0,
                                     "models": set(), "last": ""})
            a["i"] += inp; a["o"] += out; a["cr"] += cr; a["cc"] += cc
            a["models"].add(model)
            if last > a["last"]:
                a["last"] = last
        for sid, a in agg.items():
            wd = sess_meta.get(sid, {}).get("wd", "")
            add_session("devin", sid,
                        project="/".join(wd.split("/")[-2:]) if wd else "",
                        last=a["last"],
                        models=sorted(a["models"]),
                        inp=a["i"], out=a["o"], cr=a["cr"], cc=a["cc"])
        if found:
            mark("devin", tokens=True)
        con.close()
    except Exception as e:
        warnings.append(f"devin 异常: {e}")


# ------------------------------------------------------------------ kimix
def collect_kimix():
    base = HOME / ".kimix" / "sessions"
    if not base.is_dir():
        return
    found = False
    for fp in base.glob("*/*/updates.jsonl"):
        agg = {}
        try:
            with open(fp, "r", errors="replace") as f:
                for line in f:
                    if "turn_completed" not in line:
                        continue
                    try:
                        o = json.loads(line)
                    except Exception:
                        continue
                    upd = (o.get("params") or {}).get("update") or {}
                    u = upd.get("usage")
                    if not isinstance(u, dict):
                        continue
                    found = True
                    sid = (o.get("params") or {}).get("sessionId") or fp.parent.name
                    ts = o.get("timestamp") or ((o.get("_meta") or {}).get("agentTimestampMs") or 0) / 1000
                    date = day_of_ts(ts)
                    proj = fp.parent.parent.name.replace("%2F", "/")
                    proj = "/".join(proj.split("/")[-2:])
                    mu = u.get("modelUsage") or {}
                    hr = hour_of_ts(ts)
                    wrote = True
                    if mu:
                        for model, mm in mu.items():
                            mcr = mm.get("cachedReadTokens") or 0
                            kw = dict(inp=_fresh(mm.get("inputTokens") or 0, mcr),
                                      out=mm.get("outputTokens") or 0,
                                      cr=mcr, cc=0, model=model)
                            wrote = add_usage(date, "kimix", project=proj,
                                              hour=hr, **kw) and wrote
                    else:
                        ucr = u.get("cachedReadTokens") or 0
                        kw = dict(inp=_fresh(u.get("inputTokens") or 0, ucr),
                                  out=u.get("outputTokens") or 0,
                                  cr=ucr, cc=0)
                        wrote = add_usage(date, "kimix", project=proj, hour=hr, **kw)
                    if not wrote:
                        continue   # 日期非法：不要只进会话汇总
                    a = agg.setdefault(sid, {"i": 0, "o": 0, "cr": 0,
                                             "models": set(), "last": 0})
                    a["i"] += _fresh(u.get("inputTokens") or 0,
                                     u.get("cachedReadTokens") or 0)
                    a["o"] += u.get("outputTokens") or 0
                    a["cr"] += u.get("cachedReadTokens") or 0
                    a["models"].update(mu.keys() or ["?"])
                    a["last"] = max(a["last"], ts)
        except Exception as e:
            warnings.append(f"kimix 解析失败（该文件剩余行已跳过）: {fp.name}: {e}")
            continue
        proj = "/".join(fp.parent.parent.name.replace("%2F", "/").split("/")[-2:])
        for sid, a in agg.items():
            add_session("kimix", sid,
                        project=proj,
                        last=datetime.fromtimestamp(a["last"]).isoformat() if a["last"] else "",
                        models=sorted(a["models"]),
                        inp=a["i"], out=a["o"], cr=a["cr"])
    if found:
        mark("kimix", tokens=True)


# ----------------------------------------------------------------- cursor
CURSOR_CACHE = DIR / ".cache" / "cursor-usage.csv"


def _cursor_fetch_csv(tmpdir):
    """从 cursor.com 拉一次用量 CSV（只读自己的账户）。失败时写 warnings 并返回 None。"""
    import urllib.request
    tok, sub = _cursor_access_token(tmpdir)
    if not tok:
        return None
    timeout = int(os.environ.get("USAGE_DASH_CURSOR_TIMEOUT", "90"))
    url = "https://cursor.com/api/dashboard/export-usage-events-csv?strategy=tokens"
    uid = sub.split("|")[-1] if "|" in sub else None
    cookies = [c for c in (f"{sub}%3A%3A{tok}" if sub else "",
                           f"{uid}%3A%3A{tok}" if uid else "",
                           tok) if c]
    base_headers = {
        "Accept": "text/csv,*/*;q=0.8",
        "Origin": "https://cursor.com",
        "Referer": "https://cursor.com/dashboard?tab=usage",
        "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                      "AppleWebKit/537.36",
    }
    for cv in cookies + [None]:
        headers = dict(base_headers)
        if cv:
            headers["Cookie"] = f"WorkosCursorSessionToken={cv}"
        else:
            headers["Authorization"] = f"Bearer {tok}"
        try:
            r = urllib.request.urlopen(
                urllib.request.Request(url, headers=headers), timeout=timeout)
            return r.read().decode("utf-8", "replace")
        except urllib.error.HTTPError as e:
            if e.code not in (401, 403):
                warnings.append(f"cursor 用量导出 HTTP {e.code}，仅显示本地活动量")
                return None
        except Exception:
            warnings.append("cursor 用量导出网络失败，仅显示本地活动量")
            return None
    warnings.append("cursor 登录态失效（在 Cursor 中重新登录后可恢复 token 统计），"
                    "仅显示本地活动量")
    return None


def _cursor_access_token(tmpdir):
    """从 Cursor 的 state.vscdb 读登录态（只读）"""
    import base64
    src = (HOME / "Library" / "Application Support" / "Cursor" / "User"
           / "globalStorage" / "state.vscdb")
    if not src.exists():
        return None, None

    def _read(path):
        con = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
        try:
            return con.execute("SELECT value FROM ItemTable"
                               " WHERE key='cursorAuth/accessToken'").fetchone()
        finally:
            con.close()

    row = None
    try:
        # 直接只读打开原库：这是一张 KV 表，不需要一致快照，可省掉复制 500MB（约 0.45s）
        row = _read(src)
    except Exception:
        try:                              # 只读打开受限（如 WAL 的 -shm 不可写）时回退到复制
            row = _read(_copy_db(src, tmpdir, "cursor-state.vscdb"))
        except Exception:
            return None, None
    tok = (row[0] or "").strip() if row else ""
    if not tok:
        return None, None
    try:
        p = tok.split(".")[1]
        p += "=" * (-len(p) % 4)
        sub = json.loads(base64.urlsafe_b64decode(p)).get("sub", "")
    except Exception:
        sub = ""
    return tok, sub


def collect_cursor_api(tmpdir):
    """拉取 cursor.com 用量 CSV（只读自己的账户数据）。

    Cursor 的 token 只在云端（本地日志不含 token），而这个接口的回答受跨洋往返 +
    线路带宽限制（实测 TTFB ~0.5s、194KB 下载 ~0.7s，共 ~1.3s），无法再压缩；
    因此把响应缓存到 .cache/，在 TTL 内刷新直接复用，做到"按下去就出来"。

    USAGE_DASH_CURSOR_API=0 可禁用；USAGE_DASH_CURSOR_TIMEOUT 调整超时秒数；
    USAGE_DASH_CURSOR_TTL 调整缓存有效期（秒，默认 300，设 0 表示每次都拉最新）。
    """
    import urllib.request
    if os.environ.get("USAGE_DASH_CURSOR_API") == "0":
        return False
    ttl = int(os.environ.get("USAGE_DASH_CURSOR_TTL", "300"))
    body = None
    if ttl > 0:
        try:
            if CURSOR_CACHE.exists() and time.time() - CURSOR_CACHE.stat().st_mtime < ttl:
                body = CURSOR_CACHE.read_text()
        except Exception:
            body = None

    if body is None:
        body = _cursor_fetch_csv(tmpdir)
        if not body:
            return False
        try:
            CURSOR_CACHE.parent.mkdir(exist_ok=True)
            CURSOR_CACHE.write_text(body)
        except Exception:
            pass

    import csv as _csv
    import io
    rows = list(_csv.reader(io.StringIO(body)))
    if len(rows) < 2:
        return False
    hdr = [h.strip() for h in rows[0]]
    idx = {h: i for i, h in enumerate(hdr)}

    def col(row, name):
        i = idx.get(name)
        return row[i].strip() if i is not None and i < len(row) else ""

    def num(v):
        try:
            return max(0, round(float(str(v).replace(",", "") or 0)))
        except Exception:
            return 0

    per_day = {}   # date -> agg
    cloud = {}     # cloud agent id -> agg
    for row in rows[1:]:
        date_raw = col(row, "Date")
        date = day_of_iso(date_raw)
        model = col(row, "Model") or "?"
        inp = num(col(row, "Input (w/ Cache Write)")) + num(col(row, "Input (w/o Cache Write)"))
        cr = num(col(row, "Cache Read"))
        out = num(col(row, "Output Tokens"))
        cost_v = col(row, "Cost")
        cost = None
        if cost_v.startswith("$"):
            try:
                cost = float(cost_v[1:].replace(",", ""))
            except Exception:
                pass
        if not (inp or out or cr) or not date:
            continue
        # Date 列若只有日期没有时刻，hour_of_iso 会返回 0，把当天全部用量堆到 00:00
        # 的假高峰；只有确实带时刻时才记录小时
        hour = hour_of_iso(date_raw) if len(date_raw) > 10 else None
        if not add_usage(date, "cursor", project="cursor.com", hour=hour,
                         inp=inp, out=out, cr=cr, cost=cost,
                         model=model, events=1):
            continue   # 日期非法：不要只进会话汇总
        d = per_day.setdefault(date, {"i": 0, "o": 0, "cr": 0, "n": 0,
                                      "cost": 0.0, "hc": False, "models": set()})
        d["i"] += inp; d["o"] += out; d["cr"] += cr; d["n"] += 1
        d["models"].add(model)
        if cost is not None:
            d["cost"] += cost; d["hc"] = True
        caid = col(row, "Cloud Agent ID")
        if caid:
            c = cloud.setdefault(caid, {"i": 0, "o": 0, "cr": 0, "n": 0,
                                        "models": set(), "last": ""})
            c["i"] += inp; c["o"] += out; c["cr"] += cr; c["n"] += 1
            c["models"].add(model)
            ts = col(row, "Date")
            if ts > c["last"]:
                c["last"] = ts
    for date, d in per_day.items():
        add_session("cursor", f"api-{date}", project="cursor.com",
                    last=date, models=sorted(d["models"]),
                    inp=d["i"], out=d["o"], cr=d["cr"],
                    cost=d["cost"] if d["hc"] else None, events=d["n"])
    for caid, c in cloud.items():
        # 这些 token 已按日计入 api-{date} 会话，若在此再计一次会让"会话之和 > 日之和"
        # （实测多出 47428）；cloud agent 视图只保留活动量与模型信息
        add_session("cursor", f"cloud-{caid}", project="cursor cloud agent",
                    last=c["last"], models=sorted(c["models"]), events=c["n"])
    return bool(per_day)


def collect_cursor(tmpdir):
    api_ok = collect_cursor_api(tmpdir)
    found = api_ok
    # CLI agent transcripts: 无 token，按消息条数统计活动量，日期用文件 mtime
    for fp in (HOME / ".cursor" / "projects").glob("*/agent-transcripts/*/*.jsonl"):
        if fp.parent.name == "subagents":
            continue  # 子代理并入主会话，不重复计
        try:
            n_user = n_asst = 0
            with open(fp, "r", errors="replace") as f:
                for line in f:
                    if '"role"' not in line:
                        continue
                    try:
                        o = json.loads(line)
                    except Exception:
                        continue
                    role = o.get("role") or ((o.get("message") or {}).get("role"))
                    if role == "user":
                        n_user += 1
                    elif role == "assistant":
                        n_asst += 1
            if not (n_user or n_asst):
                continue
            found = True
            date = day_of_ts(fp.stat().st_mtime)
            proj = fp.parent.parent.parent.name
            add_usage(date, "cursor",
                      project=proj.replace("Users-zhaozhoubin-", "~/"),
                      hour=hour_of_ts(fp.stat().st_mtime),
                      events=n_user + n_asst)
            add_session("cursor", fp.stem,
                        project=proj.replace("Users-zhaozhoubin-", "~/"),
                        last=datetime.fromtimestamp(fp.stat().st_mtime).isoformat(),
                        events=n_user + n_asst)
        except Exception:
            continue
    # IDE 聊天库 chats/<hash>/<chat>/store.db
    for fp in (HOME / ".cursor" / "chats").glob("*/*/store.db"):
        try:
            con = _open_ro(fp, tmpdir, f"cursor-{fp.parent.name}.db")
            n = 0
            try:
                for (blob,) in con.execute("SELECT data FROM blobs"):
                    if b'"role"' in blob:
                        n += 1
            except sqlite3.Error:
                pass
            con.close()
            if n:
                found = True
                date = day_of_ts(fp.stat().st_mtime)
                add_usage(date, "cursor", project="cursor-ide-chat",
                          hour=hour_of_ts(fp.stat().st_mtime), events=n)
                add_session("cursor", fp.parent.name,
                            project="cursor-ide-chat",
                            last=datetime.fromtimestamp(fp.stat().st_mtime).isoformat(),
                            events=n)
        except Exception:
            continue
    if found:
        mark("cursor", tokens=api_ok or None)


# ------------------------------------------------------------- antigravity
# gen_metadata blob 是明文 protobuf（参考 vibe-usage 的 wire 格式逆向）：
#   chatModel = 1
#     usage = 4: input=4.2 output=4.3 cacheRead=4.5 thinking=4.9 responseId=4.11
#     chatStartMetadata.createdAt = 9.4.1（3.7+ CLI 已省略，回退 steps.metadata 1.1）
#     responseModel = 19, modelDisplayName = 21
# 注意：input(4.2) 不含 cacheRead，与 dimcode 等不同，无需 _fresh 归一化。

def _pb_varint(b, p):
    r = s = 0
    while p < len(b):
        x = b[p]; p += 1
        r |= (x & 0x7F) << s
        if not (x & 0x80):
            break
        s += 7
    return r, p


def _pb_fields(b):
    f = {}
    p = 0
    while p < len(b):
        try:
            tag, p = _pb_varint(b, p)
        except Exception:
            break
        n, wt = tag >> 3, tag & 7
        if wt == 0:
            try:
                v, p = _pb_varint(b, p)
            except Exception:
                break
        elif wt == 2:
            ln, p = _pb_varint(b, p)
            v = b[p:p + ln]; p += ln
        elif wt == 5:
            v = b[p:p + 4]; p += 4
        elif wt == 1:
            v = b[p:p + 8]; p += 8
        else:
            break
        f.setdefault(n, []).append((wt, v))
    return f


def _pbv(f, n):
    for wt, v in f.get(n, []):
        if wt == 0:
            return v
    return None


def _pbb(f, n):
    for wt, v in f.get(n, []):
        if wt == 2:
            return v
    return None


def _pbm(f, n):
    b = _pbb(f, n)
    return _pb_fields(b) if b is not None else None


def _pbs(f, n):
    b = _pbb(f, n)
    return b.decode("utf-8", "replace") if b is not None else None


# 两个产品分开统计：IDE 应用 vs agy CLI
AGY_DIRS = {
    "antigravity": [".gemini/antigravity/conversations",
                    ".gemini/antigravity-ide/conversations"],
    "antigravity-cli": [".gemini/antigravity-cli/conversations"],
}


def collect_antigravity(tmpdir):
    seen_rid = set()   # responseId 全局去重（同一 cascade 可能出现在多个目录）
    found = set()
    for agent, dirs in AGY_DIRS.items():
      for rel in dirs:
        base = HOME / rel
        if not base.is_dir():
            continue
        for fp in base.glob("*.db"):
            if fp.name == "db.sqlite":
                continue
            try:
                con = _open_ro(fp, tmpdir, f"agy-{rel.split('/')[1]}-{fp.stem}.db")
                try:
                    gen = list(con.execute(
                        "SELECT idx, data FROM gen_metadata ORDER BY idx"))
                except sqlite3.Error:
                    con.close()
                    continue
                step_ts = {}
                try:
                    for i, blob in con.execute(
                            "SELECT idx, metadata FROM steps"
                            " WHERE metadata IS NOT NULL"):
                        ca = _pbm(_pb_fields(blob), 1)
                        sec = _pbv(ca, 1) if ca else None
                        if sec:
                            step_ts[i] = sec
                except sqlite3.Error:
                    pass
                project = ""
                try:
                    r = con.execute(
                        "SELECT data FROM trajectory_metadata_blob LIMIT 1"
                    ).fetchone()
                    if r and r[0]:
                        ws = _pbm(_pb_fields(r[0]), 1)
                        uri = _pbs(ws, 1) if ws else ""
                        if uri:
                            project = uri.rstrip("/").split("/")[-1]
                except sqlite3.Error:
                    pass
                con.close()
            except Exception:
                continue

            sess = {"i": 0, "o": 0, "cr": 0, "n": 0,
                    "models": set(), "last": 0}
            for i, blob in gen:
                try:
                    chat = _pbm(_pb_fields(blob), 1)
                    if not chat:
                        continue
                    u = _pbm(chat, 4)
                    if not u:
                        continue
                    inp = _pbv(u, 2) or 0
                    out = _pbv(u, 3) or 0
                    cr = _pbv(u, 5) or 0
                    think = _pbv(u, 9) or 0
                    rid = _pbs(u, 11) or ""
                    if not (inp or out or cr or think):
                        continue
                    if rid and rid in seen_rid:
                        continue
                    if rid:
                        seen_rid.add(rid)
                    csm = _pbm(chat, 9)
                    ca = _pbm(csm, 4) if csm else None
                    sec = (_pbv(ca, 1) if ca else None) or step_ts.get(i)
                    if not sec:
                        sec = fp.stat().st_mtime
                    model = _pbs(chat, 21) or _pbs(chat, 19) or "?"
                    date = day_of_ts(sec)
                    found.add(agent)
                    add_usage(date, agent, project=project or agent,
                              hour=hour_of_ts(sec),
                              inp=inp, out=out + think,
                              cr=cr, model=model, events=1)
                    sess["i"] += inp; sess["o"] += out + think; sess["cr"] += cr
                    sess["n"] += 1; sess["models"].add(model)
                    sess["last"] = max(sess["last"], sec)
                except Exception:
                    continue
            if sess["n"]:
                add_session(agent, fp.stem,
                            project=project or agent,
                            last=datetime.fromtimestamp(
                                sess["last"]).isoformat(),
                            models=sorted(sess["models"]),
                            inp=sess["i"], out=sess["o"], cr=sess["cr"],
                            events=sess["n"])
    for agent in found:
        mark(agent, tokens=True)


# -------------------------------------------------------------------- main
# ---------------------------------------------------- 增量缓存（输入指纹）
# 思路：不做逐来源的增量解析（容易漏更新），而是给"全部输入"算一个指纹 ——
# 文件清单本身 + 每项的 (size, mtime_ns)，sqlite 还会带上 -wal/-shm。
# 指纹没变 ⇒ 上次的聚合结果仍然完整有效，直接反序列化（~20ms）跳过全部解析；
# 指纹变了 ⇒ 全量重算并覆盖缓存。这样"数据没变时的刷新"是亚秒级的，
# 而正确性由指纹兜底：任何新增/修改/删除/换登录态都会让指纹失效。
CACHE_VERSION = 2
STATE_CACHE = DIR / ".cache" / "collect-state.pkl"

_INPUT_GLOBS = (
    (".commandcode/projects/*/*.jsonl", "file", None),
    (".kimix/sessions/*/*/updates.jsonl", "file", None),
    (".cursor/projects/*/agent-transcripts/*/*.jsonl", "file", None),
    (".cursor/chats/*/*/store.db", "db", "blobs"),
    (".gemini/antigravity/conversations/*.db", "db", "gen_metadata"),
    (".gemini/antigravity-ide/conversations/*.db", "db", "gen_metadata"),
    (".gemini/antigravity-cli/conversations/*.db", "db", "gen_metadata"),
)
_INPUT_SINGLES = (
    (".dimcode/v2/dimcode.sqlite", "db", "usage_run_stats"),
    (".local/share/devin/cli/sessions.db", "db", "message_nodes"),
    ("Library/Application Support/Cursor/User/globalStorage/state.vscdb", "db", "ItemTable"),
)


def _input_sources():
    """采集用到的全部输入：(路径, 类型, 表名)。'db' 走 SQL 内容指纹，'file' 走 size+mtime。"""
    out = []
    for pat, kind, table in _INPUT_GLOBS:
        for f in HOME.glob(pat):
            if f.name.endswith(".checkpoints.jsonl"):
                continue
            out.append((str(f), kind, table))
    for rel, kind, table in _INPUT_SINGLES:
        out.append((str(HOME / rel), kind, table))
    out.append((str(CURSOR_CACHE), "file", None))
    return out


def _sqlite_signature(path, table):
    """sqlite 库的内容指纹：(行数, 最大 rowid) —— 精确对应"我们要读的数据有没有新增"。

    刻意不用 mtime、也不用 -wal 大小：正在被使用的 WAL 库这两者都会因 checkpoint 或
    写入其它表而频繁变化（实测 dimcode 的 -wal 每几秒变一次），会让缓存永不命中。
    局限：只 UPDATE、不新增行的改动检测不到；本采集读取的列都在 INSERT 时写入，
    受影响的只有 devin.created_at 这类兜底字段。
    """
    try:
        con = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
        try:
            return con.execute(
                f'SELECT COUNT(*), IFNULL(MAX(rowid), 0) FROM "{table}"').fetchone()
        finally:
            con.close()
    except Exception:
        return None


def _input_fingerprint(ccusage_raw):
    """输入指纹。任何新增/修改/删除都会改变它，从而让缓存失效。"""
    items = []
    for path, kind, table in _input_sources():
        if kind == "db":
            items.append((path, _sqlite_signature(path, table)))
        else:
            try:
                st = os.stat(path)
                items.append((path, st.st_size, st.st_mtime_ns))
            except OSError:
                items.append((path, None))
    items.append(("ccusage", hashlib.sha256((ccusage_raw or "").encode()).hexdigest()))
    return hashlib.sha256(repr(sorted(items, key=repr)).encode()).hexdigest()


def _freeze_rows(rows):
    """把 defaultdict 形式的聚合桶转成可 pickle 的普通 dict（值里不能再有 lambda）"""
    return {k: {**b, "models": {m: dict(v) for m, v in b["models"].items()}}
            for k, b in rows.items()}


def _thaw_rows(frozen, factory):
    out = defaultdict(factory)
    for k, b in frozen.items():
        nb = factory()
        nb.update(b)
        out[k] = nb
    return out


def _save_state(fingerprint, ccusage_raw):
    try:
        STATE_CACHE.parent.mkdir(exist_ok=True)
        payload = {
            "version": CACHE_VERSION,
            "fingerprint": fingerprint,
            "ccusage_raw": ccusage_raw,
            "daily": _freeze_rows(daily_rows),
            "proj": _freeze_rows(proj_rows),
            "hourly": {k: dict(v) for k, v in hourly_rows.items()},
            "sessions": sessions,
            "agent_found": {k: dict(v) for k, v in agent_found.items()},
            "canon_raws": {k: set(v) for k, v in CANON_RAWS.items()},
            "warnings": warnings,
        }
        fd, tmp = tempfile.mkstemp(prefix="state-", dir=str(STATE_CACHE.parent))
        with os.fdopen(fd, "wb") as f:
            pickle.dump(payload, f, protocol=pickle.HIGHEST_PROTOCOL)
        os.chmod(tmp, 0o600)
        os.replace(tmp, STATE_CACHE)
    except Exception as e:
        warnings.append(f"增量缓存写入失败（不影响本次结果）: {e}")


def _load_state(fingerprint):
    try:
        with open(STATE_CACHE, "rb") as f:
            p = pickle.load(f)
    except Exception:
        return None
    if p.get("version") != CACHE_VERSION or p.get("fingerprint") != fingerprint:
        return None
    return p


def _restore_state(p):
    """把缓存恢复到模块级聚合状态（后续输出阶段照常重算 costEst 等派生值）"""
    daily_rows.clear()
    daily_rows.update(_thaw_rows(p["daily"], _bucket))
    proj_rows.clear()
    proj_rows.update(_thaw_rows(p["proj"], _bucket))
    hourly_rows.clear()
    hourly_rows.update(_thaw_rows(p["hourly"], _hourly_bucket))
    sessions.extend(s for s in p["sessions"] if (s["agent"], s["sessionId"]) not in seen_session)
    seen_session.update((s["agent"], s["sessionId"]) for s in p["sessions"])
    for k, v in p["agent_found"].items():
        agent_found[k] = dict(v)
    for k, v in p["canon_raws"].items():
        CANON_RAWS[k] = set(v)
    warnings.extend(p["warnings"])


def main():
    # ccusage 先单独跑：它的输出既参与输入指纹，也是缓存未命中时的必需输入（仅 ~0.1s）
    ccusage_raw = _ccusage_raw()
    fingerprint = _input_fingerprint(ccusage_raw)
    cached = _load_state(fingerprint)
    if cached is not None:
        _restore_state(cached)
        print("增量缓存命中（输入未变化），已跳过解析", file=sys.stderr)
    else:
        tmpdir = tempfile.mkdtemp(prefix="usage-collect-")
        try:
            # 各来源互不依赖，并行采集：cursor 是网络 I/O、devin/dimcode 是大库解析。
            # 串行时总耗时接近各项之和，并行后由最慢的一项决定。
            # 共享状态（daily_rows/proj_rows/hourly_rows/sessions/...）的写入由 _state_lock 串行化。
            jobs = [
                ("ccusage", collect_ccusage, (ccusage_raw,)),
                ("commandcode", collect_commandcode, ()),
                ("dimcode", collect_dimcode, (tmpdir,)),
                ("devin", collect_devin, (tmpdir,)),
                ("kimix", collect_kimix, ()),
                ("cursor", collect_cursor, (tmpdir,)),
                ("antigravity", collect_antigravity, (tmpdir,)),
            ]
            with ThreadPoolExecutor(max_workers=len(jobs)) as ex:
                futures = {ex.submit(fn, *args): name for name, fn, args in jobs}
                for fut in as_completed(futures):
                    try:
                        fut.result()
                    except Exception as e:
                        warnings.append(f"{futures[fut]} 采集异常: {e}")
        finally:
            shutil.rmtree(tmpdir, ignore_errors=True)
        _save_state(fingerprint, ccusage_raw)

    prices, dev_map = load_prices()
    if prices:
        print(f"models.dev 价目: {len(prices)} 个模型")

    # 思考强度归并复核：仅当某基础名确有多种写法（或基础名本身存在）时保持归并；
    # 单一写法被剥离改名的（真实型号就叫 xxx-high）还原为原名。成本估算仍用 canon 名查价
    RENAME = {}
    for c, raws in CANON_RAWS.items():
        if len(raws) == 1:
            r = next(iter(raws))
            if r != c and r and not r.isdigit():
                RENAME[c] = r
    CANON_OF = {v: k for k, v in RENAME.items()}

    def disp(m):
        return RENAME.get(m, m)

    def _models_list(b, agent):
        out = []
        for m, v in sorted(b["models"].items()):
            d = {"modelName": disp(m),
                 "inputTokens": v["inputTokens"], "outputTokens": v["outputTokens"],
                 "cacheReadTokens": v["cacheReadTokens"],
                 "cacheCreationTokens": v["cacheCreationTokens"],
                 "cost": round(v["cost"], 6), "hasCost": v["hasCost"]}
            if agent in NO_COST:
                e = est_cost(prices, m, v["inputTokens"], v["outputTokens"],
                             v["cacheReadTokens"], v["cacheCreationTokens"])
                if e is not None:
                    d["costEst"] = round(e, 6)
            out.append(d)
        return out

    def _row_est(models, agent, b):
        """行级估算成本：仅当该来源完全没有记账成本时"""
        if agent not in NO_COST or b["costKnown"]:
            return None
        s = sum(m.get("costEst") or 0 for m in models)
        return round(s, 6) if s else None

    daily = []
    for (date, agent), b in sorted(daily_rows.items()):
        models = _models_list(b, agent)
        daily.append({
            "date": date, "agent": agent,
            "inputTokens": b["inputTokens"], "outputTokens": b["outputTokens"],
            "cacheReadTokens": b["cacheReadTokens"],
            "cacheCreationTokens": b["cacheCreationTokens"],
            "totalTokens": b["totalTokens"],
            "costUsd": round(b["costUsd"], 6) if b["costKnown"] else None,
            "costEst": _row_est(models, agent, b),
            "events": b["events"],
            "modelBreakdowns": models,
        })

    proj_daily = []
    for (date, agent, project), b in sorted(proj_rows.items()):
        models = _models_list(b, agent)
        proj_daily.append({
            "date": date, "agent": agent, "project": project,
            "inputTokens": b["inputTokens"], "outputTokens": b["outputTokens"],
            "cacheReadTokens": b["cacheReadTokens"],
            "cacheCreationTokens": b["cacheCreationTokens"],
            "totalTokens": b["totalTokens"],
            "costUsd": round(b["costUsd"], 6) if b["costKnown"] else None,
            "costEst": _row_est(models, agent, b),
            "events": b["events"],
            "modelBreakdowns": models,
        })

    for s in sessions:
        # 会话级没有按模型的 token 拆分：多模型会话若拿 modelsUsed[0] 的单价给整段会话
        # 定价，会与"日/项目级按模型分别估算"的口径冲突，且可能偏差数倍。
        # 只在确知单一模型时才估算，其余留白由前端显示"未定价"。
        if (s["costUsd"] is None and s["agent"] in NO_COST
                and len(s["modelsUsed"]) == 1):
            e = est_cost(prices, s["modelsUsed"][0], s["inputTokens"],
                         s["outputTokens"], s["cacheReadTokens"],
                         s["cacheCreationTokens"])
            if e is not None:
                s["costEst"] = round(e, 6)
        s["modelsUsed"] = sorted({disp(m) for m in s["modelsUsed"]})

    hourly = []
    for (h, agent, model, project), b in sorted(hourly_rows.items()):
        row = {
            "hour": h, "agent": agent, "model": disp(model), "project": project,
            "inputTokens": b["inputTokens"], "outputTokens": b["outputTokens"],
            "cacheReadTokens": b["cacheReadTokens"],
            "cacheCreationTokens": b["cacheCreationTokens"],
            "totalTokens": b["inputTokens"] + b["outputTokens"]
            + b["cacheReadTokens"] + b["cacheCreationTokens"],
            "costUsd": round(b["costUsd"], 6) if b["costKnown"] else None,
            "events": b["events"],
        }
        if agent in NO_COST:
            e = est_cost(prices, model, b["inputTokens"], b["outputTokens"],
                         b["cacheReadTokens"], b["cacheCreationTokens"])
            if e is not None:
                row["costEst"] = round(e, 6)
        hourly.append(row)

    agents = []
    for aid, info in sorted(agent_found.items()):
        meta = AGENT_META.get(aid, {})
        rows = [d for d in daily if d["agent"] == aid]
        agents.append({
            "id": aid,
            "label": meta.get("label", aid),
            "color": meta.get("color", "#64748b"),
            "hasTokens": bool(info.get("tokens")),
            "hasCost": bool(info.get("tokens")) and aid not in NO_COST,
            "source": SOURCE_DESC.get(aid, "ccusage 自动探测"),
            "days": len({d["date"] for d in rows}),
            "totalTokens": sum(d["totalTokens"] for d in rows),
            "totalCostUsd": round(sum(d["costUsd"] or 0 for d in rows), 4),
            "totalEvents": sum(d["events"] for d in rows),
            "sessions": sum(1 for s in sessions if s["agent"] == aid),
        })

    # 模型 -> 开发商（models.dev provider 归属，前端分组用；未命中的由前端规则兜底）
    model_names = set()
    for r in daily + proj_daily:
        for mb in r["modelBreakdowns"]:
            model_names.add(mb["modelName"])
    for s in sessions:
        model_names.update(s["modelsUsed"])
    for h in hourly:
        model_names.add(h["model"])
    model_devs = {}
    for m in sorted(model_names):
        d = _lookup_map(dev_map, m) or _lookup_map(dev_map, CANON_OF.get(m, m))
        if d:
            model_devs[m] = d

    payload = {
        "generatedAt": datetime.now(timezone.utc).isoformat(),
        "agents": agents,
        "modelDevs": model_devs,
        "daily": daily,
        "projectDaily": proj_daily,
        "hourly": hourly,
        "sessions": sorted(sessions, key=lambda s: s["totalTokens"], reverse=True),
        "warnings": warnings,
    }
    json_str = json.dumps(payload, ensure_ascii=False).replace("<", "\\u003c")
    fd, tmp_path = tempfile.mkstemp(prefix="datajs-", dir=str(DIR))
    with os.fdopen(fd, "w") as f:
        f.write("window.DASHBOARD_DATA = " + json_str + ";\n")
    os.chmod(tmp_path, 0o600)
    os.replace(tmp_path, DIR / "data.js")

    n_tok = sum(1 for a in agents if a["hasTokens"])
    print(f"data.js updated: {len(daily)} (date,agent) rows, "
          f"{len(sessions)} sessions, {len(agents)} agents "
          f"({n_tok} with tokens)", file=sys.stderr)
    for a in agents:
        print(f"  {a['id']:<12} days={a['days']:<3} tokens={a['totalTokens']:>12,} "
              f"events={a['totalEvents']:>6} sessions={a['sessions']}", file=sys.stderr)
    for w in warnings:
        print(f"  ! {w}", file=sys.stderr)


if __name__ == "__main__":
    main()
