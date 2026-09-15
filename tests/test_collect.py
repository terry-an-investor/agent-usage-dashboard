#!/usr/bin/env python3
"""采集端单元测试（仅标准库）。

    python3 -m unittest discover -s tests -p 'test_*.py' -v
    npm run test:py

这里的每一条断言都对应一个**曾经真实出现过并被修掉**的缺陷，或一条不该被
悄悄改掉的语义（`SourceInvariantsTest`）。改 collect.py 时若让它们变红，
先确认是有意变更，再同步更新测试。
"""
import json
import os
import shutil
import sqlite3
import sys
import tempfile
import time
import unittest
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import collect as m  # noqa: E402


def _reset_state():
    m.daily_rows.clear()
    m.proj_rows.clear()
    m.hourly_rows.clear()
    m.sessions.clear()
    m.seen_session.clear()
    m.agent_found.clear()
    m.warnings.clear()
    m._bad_date_seen.clear()


class DateHandlingTest(unittest.TestCase):
    """日期解析：坏值不得变成 1970-01-01 或空日期串（会污染整条日期轴）"""

    def test_rejects_falsy_and_garbage(self):
        self.assertIsNone(m.day_of_ts(0))
        self.assertIsNone(m.day_of_ts(None))
        self.assertIsNone(m.day_of_ts("abc"))      # 非数值不应抛异常
        self.assertIsNone(m.day_of_iso(""))
        self.assertIsNone(m.day_of_iso("13/09/2026 10:00"))
        self.assertIsNone(m.day_of_iso(None))

    def test_accepts_seconds_and_millis(self):
        dt = datetime(2026, 9, 15, 12, 0, 0)
        ts = dt.timestamp()
        self.assertEqual(m.day_of_ts(ts), "2026-09-15")
        self.assertEqual(m.day_of_ts(ts * 1000), "2026-09-15")     # 毫秒

    def test_accepts_iso(self):
        self.assertEqual(m.day_of_iso("2026-09-15T10:00:00Z"), "2026-09-15")

    def test_fallback_only_for_plain_date_like_prefix(self):
        # 解析失败时只在形如 YYYY-MM-DD 时才兜底
        self.assertEqual(m.day_of_iso("2026-09-15 10:00:00 乱码"), "2026-09-15")


class AddUsageTest(unittest.TestCase):
    """非法日期必须整体跳过（否则会出现"会话里有、日粒度里没有"）"""

    def setUp(self):
        _reset_state()

    def test_bad_date_rejected_and_warned_once(self):
        self.assertFalse(m.add_usage("", "ag", inp=100))
        self.assertFalse(m.add_usage("13/09/2026", "ag", inp=100))
        self.assertTrue(m.add_usage("2026-09-15", "ag", inp=100))
        self.assertEqual([k for k in m.daily_rows if k[1] == "ag"],
                         [("2026-09-15", "ag")])
        # 同一 (agent, date) 只告警一次
        self.assertEqual(len([w for w in m.warnings if "ag" in w]), 2)

    def test_writes_all_three_granularities(self):
        self.assertTrue(m.add_usage("2026-09-15", "ag", project="p", hour=9,
                                    inp=1, out=2, cr=3, cc=4, model="mod", events=1))
        self.assertEqual(m.daily_rows[("2026-09-15", "ag")]["totalTokens"], 10)
        self.assertEqual(m.proj_rows[("2026-09-15", "ag", "p")]["totalTokens"], 10)
        # 小时桶不存 totalTokens（在输出阶段由分项相加得到）
        hb = m.hourly_rows[("2026-09-15 09", "ag", "mod", "p")]
        self.assertEqual(
            hb["inputTokens"] + hb["outputTokens"] + hb["cacheReadTokens"]
            + hb["cacheCreationTokens"], 10)


class AccumTest(unittest.TestCase):
    """模型级份额：显式 0 不能被行级总量顶替"""

    def setUp(self):
        _reset_state()

    def test_explicit_zero_is_respected(self):
        m._accum(m.daily_rows, ("2026-09-15", "t"), inp=500, model="m1", m_inp=0)
        self.assertEqual(m.daily_rows[("2026-09-15", "t")]["models"]["m1"]["inputTokens"], 0)

    def test_missing_model_amount_falls_back_to_row(self):
        m._accum(m.daily_rows, ("2026-09-15", "t"), inp=500, model="m1")
        self.assertEqual(m.daily_rows[("2026-09-15", "t")]["models"]["m1"]["inputTokens"], 500)


class EstCostTest(unittest.TestCase):
    """无价目、无用量都必须返回 None，不能返回 0.0（否则渲染成 ≈$0.00）"""

    PRICES = {"m": {"in": 1.0, "out": 2.0, "cr": 0.1, "cw": 0.0}}

    def test_none_without_usage(self):
        self.assertIsNone(m.est_cost(self.PRICES, "m", 0, 0, 0, 0))

    def test_none_without_price(self):
        self.assertIsNone(m.est_cost(self.PRICES, "unknown-model", 1000, 0, 0, 0))

    def test_computes_usd(self):
        self.assertAlmostEqual(m.est_cost(self.PRICES, "m", 1_000_000, 0, 0, 0), 1.0)
        self.assertAlmostEqual(m.est_cost(self.PRICES, "m", 0, 1_000_000, 0, 0), 2.0)


class PriceMapTest(unittest.TestCase):
    """回归：优先一手厂商报价时，全零报价（套餐 SKU / 纯转发）不能覆盖真实单价。
    曾经因此把 deepseek-v4-pro / glm-5.2 / kimi-k3 等 55 个模型估成 $0。"""

    def test_zero_price_does_not_override_real_price(self):
        raw = {
            "deepseek": {"models": {"deepseek-v4-pro": {"cost": {"input": 0.435, "output": 0.87}}}},
            # 套餐 SKU：在一手 provider 名单里，但价格全 0
            "alibaba-token-plan": {"models": {"deepseek-v4-pro": {"cost": {"input": 0, "output": 0}}}},
        }
        self.assertAlmostEqual(m._build_price_map(raw)["deepseek-v4-pro"]["in"], 0.435)

    def test_first_party_beats_aggregator(self):
        raw = {
            "openai": {"models": {"o3": {"cost": {"input": 2.0, "output": 8.0}}}},
            "some-aggregator": {"models": {"o3": {"cost": {"input": 10.0, "output": 40.0}}}},
        }
        self.assertAlmostEqual(m._build_price_map(raw)["o3"]["in"], 2.0)

    def test_zero_price_kept_when_nothing_else(self):
        raw = {"some-forwarder": {"models": {"free-model": {"cost": {"input": 0, "output": 0}}}}}
        self.assertEqual(m._build_price_map(raw)["free-model"]["in"], 0.0)


class CanonModelTest(unittest.TestCase):
    """模型名规范化：不同来源的同一模型要归并，且记下原始写法供复核"""

    def test_normalization(self):
        self.assertEqual(m.canon_model("[pi] deepseek-v4-flash"), "deepseek-v4-flash")
        self.assertEqual(m.canon_model("provider/some-model"), "some-model")
        self.assertEqual(m.canon_model("cursor-grok-4.5"), "grok-4.5")
        self.assertEqual(m.canon_model("claude-sonnet-4-5-20250929"), "claude-sonnet-4-5")

    def test_empty_is_unknown(self):
        self.assertEqual(m.canon_model(""), "?")
        self.assertEqual(m.canon_model(None), "?")

    def test_strength_suffix_merged_but_original_recorded(self):
        self.assertEqual(m.canon_model("gpt-5.4-high"), "gpt-5.4")
        self.assertIn("gpt-5.4-high", m.CANON_RAWS["gpt-5.4"])

    def test_memoized_consistently(self):
        m._canon_cache.clear()
        first = m.canon_model("memo-check__")
        self.assertEqual(first, m.canon_model("memo-check__"))
        self.assertIn("memo-check__", m._canon_cache)


class SqliteAccessTest(unittest.TestCase):
    """库访问：优先只读直读（省掉整库复制），复制路径仍要能读到一致快照"""

    def setUp(self):
        self.tmp = tempfile.mkdtemp()

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _make_db(self, path):
        con = sqlite3.connect(str(path))
        con.execute("CREATE TABLE t(x)")
        con.executemany("INSERT INTO t VALUES(?)", [(i,) for i in range(100)])
        con.commit()
        con.close()

    def test_open_ro_reads_in_place_without_copy(self):
        db = Path(self.tmp) / "src.db"
        self._make_db(db)
        workdir = Path(self.tmp) / "work"
        workdir.mkdir()
        con = m._open_ro(db, str(workdir), "copy.db")
        try:
            self.assertEqual(con.execute("SELECT COUNT(*) FROM t").fetchone()[0], 100)
        finally:
            con.close()
        self.assertEqual(list(workdir.iterdir()), [], "可直读时不应产生副本")

    def test_open_ro_falls_back_to_copy(self):
        bogus = Path(self.tmp) / "bogus.db"
        bogus.write_text("not a database")
        workdir = Path(self.tmp) / "work2"
        workdir.mkdir()
        with self.assertRaises(Exception):
            con = m._open_ro(bogus, str(workdir), "copy.db")
            con.execute("SELECT COUNT(*) FROM t").fetchone()

    def test_copy_db_snapshot_is_readable(self):
        db = Path(self.tmp) / "src2.db"
        self._make_db(db)
        copied = m._copy_db(db, self.tmp, "copy2.db")
        con = sqlite3.connect(f"file:{copied}?mode=ro", uri=True)
        try:
            self.assertEqual(con.execute("SELECT COUNT(*) FROM t").fetchone()[0], 100)
        finally:
            con.close()


class IncrementalCacheTest(unittest.TestCase):
    """增量缓存的正确性：指纹要稳定、要对输入变化敏感、坏缓存要能拒绝"""

    def setUp(self):
        _reset_state()
        m.CANON_RAWS.clear()
        self.tmp = tempfile.mkdtemp()
        self._state, self._cursor = m.STATE_CACHE, m.CURSOR_CACHE
        m.STATE_CACHE = Path(self.tmp) / "state.pkl"
        m.CURSOR_CACHE = Path(self.tmp) / "cursor.csv"

    def tearDown(self):
        m.STATE_CACHE, m.CURSOR_CACHE = self._state, self._cursor
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_fingerprint_stable_for_same_input(self):
        self.assertEqual(m._input_fingerprint("same"), m._input_fingerprint("same"))

    def test_fingerprint_sensitive_to_ccusage_output(self):
        self.assertNotEqual(m._input_fingerprint("a"), m._input_fingerprint("b"))

    def test_fingerprint_sensitive_to_input_file_change(self):
        m.CURSOR_CACHE.write_text("x")
        before = m._input_fingerprint("a")
        future = time.time() + 10
        os.utime(m.CURSOR_CACHE, (future, future))
        self.assertNotEqual(before, m._input_fingerprint("a"))

    def test_save_load_restore_roundtrip(self):
        m.add_usage("2026-09-15", "ag", project="p", hour=9, inp=10, out=5, cr=1, cc=0, model="m")
        m.add_session("ag", "s1", project="p", inp=10, out=5, cr=1, cc=0, events=2)
        m.mark("ag", tokens=True)
        m._save_state("fp", "ccusage-raw")

        _reset_state()
        m.CANON_RAWS.clear()
        payload = m._load_state("fp")
        self.assertIsNotNone(payload)
        m._restore_state(payload)

        self.assertEqual(m.daily_rows[("2026-09-15", "ag")]["totalTokens"], 16)
        hb = m.hourly_rows[("2026-09-15 09", "ag", "m", "p")]
        self.assertEqual(
            hb["inputTokens"] + hb["outputTokens"] + hb["cacheReadTokens"]
            + hb["cacheCreationTokens"], 16)
        self.assertEqual([s["sessionId"] for s in m.sessions], ["s1"])
        self.assertTrue(m.agent_found["ag"]["tokens"])

    def test_wrong_fingerprint_is_rejected(self):
        m._save_state("fp1", "raw")
        self.assertIsNone(m._load_state("fp2"))

    def test_corrupt_cache_is_rejected(self):
        m.STATE_CACHE.write_bytes(b"definitely-not-a-pickle")
        self.assertIsNone(m._load_state("fp"))

    def test_version_bump_invalidates(self):
        m._save_state("fp", "raw")
        payload = m._load_state("fp")
        payload["version"] = m.CACHE_VERSION + 1
        with open(m.STATE_CACHE, "wb") as f:
            import pickle
            pickle.dump(payload, f)
        self.assertIsNone(m._load_state("fp"))


class SourceInvariantsTest(unittest.TestCase):
    """源码级不变量：语义上不该被改回去的地方"""

    def setUp(self):
        self.src = (ROOT / "collect.py").read_text()

    def test_devin_does_not_use_fresh(self):
        # devin 的 metrics.input_tokens 已是"未命中缓存的新输入"（95.7% 的事件
        # cache_read > input），套 _fresh 会把绝大多数输入 clamp 成 0
        block = self.src.split("def collect_devin")[1].split("def collect_kimix")[0]
        calls = [ln for ln in block.splitlines()
                 if "_fresh(" in ln and not ln.strip().startswith("#")]
        self.assertEqual(calls, [], "devin 不能套 _fresh")

    def test_collectors_run_in_parallel(self):
        self.assertIn("ThreadPoolExecutor", self.src)

    def test_shared_state_is_locked(self):
        # 并行采集下，共享聚合状态的每次读改写都必须持锁
        for fn in ("def _accum", "def add_session", "def mark", "def _warn_bad_date"):
            block = self.src.split(fn)[1].split("\ndef ", 1)[0]
            self.assertIn("_state_lock", block, f"{fn} 缺少 _state_lock")


class DataJsConsistencyTest(unittest.TestCase):
    """对真实产物做自洽性校验（没有 data.js 时跳过）"""

    @classmethod
    def setUpClass(cls):
        path = ROOT / "data.js"
        if not path.exists():
            raise unittest.SkipTest("尚无 data.js（先跑 ./refresh.sh）")
        cls.data = json.loads(path.read_text()
                              .replace("window.DASHBOARD_DATA = ", "").rstrip().rstrip(";"))

    def test_row_totals_add_up(self):
        for r in self.data["daily"]:
            self.assertEqual(
                r["inputTokens"] + r["outputTokens"] + r["cacheReadTokens"]
                + r["cacheCreationTokens"], r["totalTokens"],
                f"{r['date']} {r['agent']} 行内 total 与分项不符")

    def test_model_breakdowns_match_row(self):
        for r in self.data["daily"]:
            mbs = r.get("modelBreakdowns") or []
            if not mbs:
                continue
            self.assertEqual(sum(x["inputTokens"] for x in mbs), r["inputTokens"],
                             f"{r['date']} {r['agent']} 模型级输入之和与行不符")

    def test_session_totals_never_exceed_daily(self):
        # 回归点：cursor 的 cloud agent 会话曾与按日会话共用同一批 token，导致
        # "会话之和 > 日之和"
        from collections import defaultdict
        by_day, by_sess = defaultdict(int), defaultdict(int)
        for r in self.data["daily"]:
            by_day[r["agent"]] += r["totalTokens"]
        for s in self.data["sessions"]:
            by_sess[s["agent"]] += s["totalTokens"]
        for agent, day_total in by_day.items():
            self.assertLessEqual(by_sess.get(agent, 0), day_total,
                                 f"{agent} 的会话总 token 超过日粒度总和（疑似重复计）")

    def test_no_bad_dates(self):
        for r in self.data["daily"]:
            self.assertRegex(r["date"], r"^\d{4}-\d{2}-\d{2}$")

    def test_hourly_format_is_date_plus_hour(self):
        for h in self.data["hourly"]:
            self.assertRegex(h["hour"], r"^\d{4}-\d{2}-\d{2} \d{2}$",
                             "hour 必须是 'YYYY-MM-DD HH'（前端按 slice 取值）")


if __name__ == "__main__":
    unittest.main(verbosity=2)
