# -*- coding: utf-8 -*-
"""集思录 ETF 每日自动同步 -> 本地 SQLite (etf.db)

设计:
- 核心池(约300只): 有投资价值的ETF, 存全字段逐日快照 (etf_daily)
  规则: ①规模≥2亿 且 流动性达标(近20日日均成交≥1000万 或 当日≥1000万)
           -> 每个跟踪指数只留规模龙头
        ②黄金ETF 全保留
        ③货币ETF 留规模前5
        ④T+0跨境品种 且 规模≥2亿 全保留
- 全市场瘦身快照 (market_daily): 所有ETF只存 规模/成交/折溢价/份额 4项,
  作为保险网, 池外品种日后变热也能回溯
- etf_info: 全市场静态资料 + 池内标记(in_pool)与原因, 每次运行刷新
- 首次运行: 从 etf_history.jsonl 回填近50个交易日历史
- 交易日期以接口返回的 last_dt 为准, INSERT OR REPLACE 保证幂等, 重复跑无副作用

用法: python daily_sync.py  (可反复运行; 由 Windows 计划任务每日 22:00 调起)
"""
import json
import os
import re
import sqlite3
import sys
import time
import traceback
from datetime import datetime
from pathlib import Path

import numpy as np
import pandas as pd

os.chdir(Path(__file__).resolve().parent)  # 计划任务无工作目录, 统一切到脚本目录
sys.path.insert(0, ".")
from jisilu import get_session, BASE  # noqa: E402

DB = "etf.db"
POOL_MIN_SCALE = 2.0        # 亿
POOL_MIN_AVG_VOL = 1000.0   # 万, 近20日日均成交
POOL_MIN_TODAY_VOL = 1000.0 # 万, 当日成交
MONEY_TOP_N = 5
# 收盘数据就绪时间(北京时间): 场内收盘15:00, 净值/份额晚间陆续更新, 20点前视为盘中数据
DATA_READY_HOUR = 20


def valid_cross(cross):
    """盘中运行时返回 None(跳过当日日线写入), 否则返回可写回的行。

    白天运行时接口返回的 last_dt 全部是今天(盘中快照), 当日收盘价/净值/份额
    尚未最终确定, 写库会造成"半成品"快照 -> 整体跳过, 保留库中上一交易日数据。
    20点后运行则正常写入; 周末/节假日 last_dt 本来就是最近交易日, 不受影响。
    """
    today = datetime.now().strftime("%Y-%m-%d")
    if datetime.now().hour >= DATA_READY_HOUR:
        return cross
    if (cross["last_dt"] >= today).any():
        return None
    return cross

SCHEMA = """
PRAGMA journal_mode=WAL;
PRAGMA busy_timeout=5000;
CREATE TABLE IF NOT EXISTS etf_info (
  fund_id TEXT PRIMARY KEY, fund_nm TEXT, category TEXT, index_nm TEXT,
  fee REAL, m_fee REAL, t_fee REAL, issuer_nm TEXT, t0 TEXT,
  creation_unit REAL, in_pool INTEGER DEFAULT 0, pool_reason TEXT,
  first_seen TEXT, updated TEXT
);
CREATE TABLE IF NOT EXISTS etf_daily (
  fund_id TEXT, trade_dt TEXT,
  price REAL, increase_rt REAL, fund_nav REAL, nav_discount_rt REAL,
  volume REAL, amount REAL, amount_incr REAL,
  unit_total REAL, index_increase_rt REAL,
  PRIMARY KEY (fund_id, trade_dt)
);
CREATE TABLE IF NOT EXISTS market_daily (
  fund_id TEXT, trade_dt TEXT,
  unit_total REAL, volume REAL, nav_discount_rt REAL, amount REAL,
  price REAL, increase_rt REAL,
  PRIMARY KEY (fund_id, trade_dt)
);
CREATE TABLE IF NOT EXISTS run_log (
  run_at TEXT PRIMARY KEY, status TEXT, trade_dt TEXT,
  pool_n INTEGER, market_n INTEGER, detail TEXT
);
CREATE INDEX IF NOT EXISTS idx_daily_dt ON etf_daily(trade_dt);
CREATE INDEX IF NOT EXISTS idx_mkt_dt ON market_daily(trade_dt);
"""


def num(s):
    if s is None or s in ("", "-"):
        return None
    if isinstance(s, (int, float)):
        return float(s)
    try:
        return float(re.sub(r"[%,]", "", str(s).strip()))
    except ValueError:
        return None


def fetch_lists(session):
    """拉三类ETF列表, 返回 DataFrame + 每行的交易日 last_dt

    接口兼容: 2026-09 起货币ETF接口不再返回 last_dt, 改为 price_dt(交易日)
    + last_time(时间), 这里统一归一化成 last_dt。
    """
    import requests
    frames = []
    for path, cat in [("/data/etf/etf_list/", "指数ETF"),
                      ("/data/etf/gold_list/", "黄金ETF"),
                      ("/data/etf/money_list/", "货币ETF")]:
        r = session.get(f"{BASE}{path}", params={"___jsl": int(time.time() * 1000)},
                        timeout=30)
        r.raise_for_status()
        d = r.json()
        df = pd.DataFrame([row["cell"] for row in d.get("rows", [])])
        if df.empty:
            raise RuntimeError(f"{path} 返回空, 可能登录失效")
        if "last_dt" not in df.columns:
            if "price_dt" not in df.columns:
                raise RuntimeError(f"{path} 缺少交易日字段(last_dt/price_dt都没有)")
            df = df.rename(columns={"price_dt": "last_dt"})
        required = {"fund_id", "fund_nm", "last_dt", "price", "volume", "unit_total"}
        missing = required - set(df.columns)
        if missing:
            raise RuntimeError(f"{path} 缺少必要字段: {sorted(missing)}")
        df["category"] = cat
        frames.append(df)
    return pd.concat(frames, ignore_index=True)


def avg_volume_from_db(conn, days=20):
    """最近 N 个交易日的日均成交(万), 首日无历史返回空DataFrame。"""
    try:
        return pd.read_sql_query(
            """WITH recent_dates AS (
                   SELECT DISTINCT trade_dt FROM market_daily
                   WHERE trade_dt IS NOT NULL
                   ORDER BY trade_dt DESC LIMIT ?
               )
               SELECT fund_id, AVG(volume) AS avg_vol
               FROM market_daily
               WHERE trade_dt IN (SELECT trade_dt FROM recent_dates)
               GROUP BY fund_id""", conn, params=(days,))
    except pd.errors.DatabaseError:
        return pd.DataFrame(columns=["fund_id", "avg_vol"])


def decide_pool(cross, avg_vol):
    """返回全市场 df(含 in_pool/pool_reason 列)"""
    cross = cross.merge(avg_vol, on="fund_id", how="left")
    scale = cross["unit_total"].map(num)
    today_vol = cross["volume"].map(num)
    liquid = (cross["avg_vol"].fillna(0) >= POOL_MIN_AVG_VOL) | \
             (today_vol.fillna(0) >= POOL_MIN_TODAY_VOL)

    cross["in_pool"] = 0
    cross["pool_reason"] = None

    # ① 指数龙头: 规模+流动性达标后, 每指数规模最大
    cand = cross[(scale >= POOL_MIN_SCALE) & liquid & (cross["category"] == "指数ETF") &
                 (cross["index_nm"].map(lambda x: x not in ("", "-", None)))]
    lead = cand.sort_values("unit_total", key=lambda s: s.map(num), ascending=False) \
               .drop_duplicates("index_nm")
    cross.loc[lead.index, "in_pool"] = 1
    cross.loc[lead.index, "pool_reason"] = "指数龙头"

    # ② 黄金全保留
    m = (cross["category"] == "黄金ETF")
    cross.loc[m, "in_pool"] = 1
    cross.loc[m, "pool_reason"] = "黄金ETF"

    # ③ 货币前N
    money = cross[cross["category"] == "货币ETF"].assign(_s=scale)
    money_idx = money.nlargest(MONEY_TOP_N, "_s").index
    cross.loc[money_idx, "in_pool"] = 1
    cross.loc[money_idx, "pool_reason"] = "货币头部"

    # ④ T+0 且 规模达标（已入池的保留原原因）
    m = (cross["t0"] == "Y") & (scale >= POOL_MIN_SCALE) & (cross["in_pool"] == 0)
    cross.loc[m, "in_pool"] = 1
    cross.loc[m, "pool_reason"] = "T+0跨境"
    return cross


def sync_info(conn, cross, now):
    rows = []
    for _, r in cross.iterrows():
        rows.append((r["fund_id"], r["fund_nm"], r["category"],
                     r.get("index_nm"), num(r.get("fee")), num(r.get("m_fee")),
                     num(r.get("t_fee")), r.get("issuer_nm"), r.get("t0"),
                     num(r.get("creation_unit")), int(r["in_pool"]) if r["in_pool"] else 0,
                     r["pool_reason"], now, now))
    conn.executemany(
        """INSERT INTO etf_info (fund_id, fund_nm, category, index_nm, fee, m_fee, t_fee,
                                 issuer_nm, t0, creation_unit, in_pool, pool_reason,
                                 first_seen, updated)
           VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)
           ON CONFLICT(fund_id) DO UPDATE SET
             fund_nm=excluded.fund_nm, category=excluded.category,
             index_nm=excluded.index_nm, fee=excluded.fee, m_fee=excluded.m_fee,
             t_fee=excluded.t_fee, issuer_nm=excluded.issuer_nm, t0=excluded.t0,
             creation_unit=excluded.creation_unit, in_pool=excluded.in_pool,
             pool_reason=excluded.pool_reason, updated=excluded.updated""",
        rows)


def sync_daily(conn, cross, trade_dt):
    """核心池 -> etf_daily; 全市场 -> market_daily。

    每行使用接口自己的 last_dt，避免停牌或单个接口延迟时把旧价格伪装成
    当日价格。返回的 trade_dt 仅代表本次运行的市场主交易日（众数）。
    """
    if trade_dt is None:
        last = cross["last_dt"].mode()
        if last.empty:
            raise RuntimeError("无法确定交易日(last_dt为空)")
        trade_dt = str(last.iloc[0])

    pool = cross[cross["in_pool"] == 1]
    def row_dt(r):
        value = r.get("last_dt")
        return str(value) if value not in (None, "", "-") else trade_dt

    rows = [(r["fund_id"], row_dt(r), num(r.get("price")), num(r.get("increase_rt")),
             num(r.get("fund_nav")), num(r.get("nav_discount_rt")),
             num(r.get("volume")), num(r.get("amount")), num(r.get("amount_incr")),
             num(r.get("unit_total")), num(r.get("index_increase_rt")))
            for _, r in pool.iterrows()]
    conn.executemany(
        """INSERT OR REPLACE INTO etf_daily VALUES (?,?,?,?,?,?,?,?,?,?,?)""", rows)

    mrows = [(r["fund_id"], row_dt(r), num(r.get("unit_total")), num(r.get("volume")),
              num(r.get("nav_discount_rt")), num(r.get("amount")),
              num(r.get("price")), num(r.get("increase_rt")))
             for _, r in cross.iterrows()]
    conn.executemany(
        """INSERT OR REPLACE INTO market_daily VALUES (?,?,?,?,?,?,?,?)""", mrows)
    return trade_dt, len(rows), len(mrows)


def backfill_history(conn):
    """首次运行: 把 etf_history.jsonl 的近50日历史灌入 etf_daily/market_daily"""
    if conn.execute("SELECT COUNT(*) FROM etf_daily").fetchone()[0] > 0:
        return 0
    if not os.path.exists("etf_history.jsonl"):
        return 0
    pool_ids = {r[0] for r in conn.execute("SELECT fund_id FROM etf_info WHERE in_pool=1")}
    daily, mkt = [], []
    with open("etf_history.jsonl", encoding="utf-8") as f:
        for line in f:
            rec = json.loads(line)
            fid = rec["fund_id"]
            for c in rec["history"]:
                dt = c.get("hist_dt")
                if not dt or dt < "2026-07-07":
                    continue
                if fid in pool_ids:
                    daily.append((fid, dt, num(c.get("trade_price")),
                                  num(c.get("increase_rt")), num(c.get("fund_nav")),
                                  num(c.get("discount_rt")), num(c.get("volume")),
                                  num(c.get("amount")), num(c.get("amount_incr")),
                                  None, num(c.get("idx_incr_rt"))))
                mkt.append((fid, dt, None, num(c.get("volume")),
                            num(c.get("discount_rt")), num(c.get("amount")),
                            num(c.get("trade_price")), num(c.get("increase_rt"))))
    conn.executemany("INSERT OR REPLACE INTO etf_daily VALUES (?,?,?,?,?,?,?,?,?,?,?)", daily)
    conn.executemany("INSERT OR REPLACE INTO market_daily VALUES (?,?,?,?,?,?,?,?)", mkt)
    return len(daily)


def say(msg):
    """pythonw 下 sys.stdout 为 None, print 会抛异常"""
    try:
        print(msg, flush=True)
    except Exception:
        pass


def main():
    now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    conn = sqlite3.connect(DB)
    conn.executescript(SCHEMA)
    detail = ""
    try:
        session = get_session()
        cross = fetch_lists(session)
        cross = valid_cross(cross)
        if cross is None:
            prev = conn.execute("SELECT MAX(trade_dt) FROM market_daily").fetchone()[0] or "-"
            conn.execute("INSERT OR REPLACE INTO run_log VALUES (?,?,?,?,?,?)",
                         (now, "SKIP", prev, 0, 0, "盘中运行, 当日数据未就绪, 跳过写入"))
            conn.commit()
            say(f"[{now}] SKIP 盘中运行, 库中最新交易日={prev}, 等待{DATA_READY_HOUR}点后正式同步")
            return
        cross = decide_pool(cross, avg_volume_from_db(conn))
        sync_info(conn, cross, now)
        n_backfill = backfill_history(conn)
        trade_dt, pool_n, mkt_n = sync_daily(conn, cross, None)
        extra = (f" backfill={n_backfill}" if n_backfill else "")
        conn.execute("INSERT OR REPLACE INTO run_log VALUES (?,?,?,?,?,?)",
                     (now, "OK", trade_dt, pool_n, mkt_n, extra.strip()))
        conn.commit()
        say(f"[{now}] OK 交易日={trade_dt} 核心池={pool_n} 全市场={mkt_n}" + extra)
    except Exception as e:
        detail = f"{type(e).__name__}: {e}\n{traceback.format_exc()[-500:]}"
        try:
            conn.execute("INSERT OR REPLACE INTO run_log VALUES (?,?,?,?,?,?)",
                         (now, "FAIL", None, 0, 0, detail))
            conn.commit()
        except Exception:
            pass
        say(f"[{now}] FAIL {detail}")
        raise
    finally:
        conn.close()


if __name__ == "__main__":
    main()
