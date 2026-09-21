# -*- coding: utf-8 -*-
"""ETF 数据看板 - 本地 Web 服务 (v2: 交易员视角)

直接读 etf.db, 标准库 + pandas。
信号体系(基于 2026-07 科技暴跌样本回测, 见 README_迁移.md 同级说明):
- 市场温度计: 情绪顶峰(单日暴涨) -> 反转确认 -> 趋势破位 -> 广度/量能恶化
- 暴跌风险: 高位兑现信号 / 回撤榜 / 破位放量
- 机会: 趋势上升(价>MA20>MA60且上行) / 底部特征(深回撤+缩量+企稳)
用法: .venv/bin/python webapp/app.py [端口]   默认 8787
"""
import json
import os
import sqlite3
import sys
import threading
from datetime import datetime
from pathlib import Path

import numpy as np
import pandas as pd

ROOT = Path(__file__).resolve().parent.parent
DB = ROOT / "etf.db"
WEB = Path(__file__).resolve().parent

TECH_KW = ("科技|科创|芯片|半导体|人工智能|AI|计算机|信息|软件|通信|电子|机器人"
           "|大数据|云计算|创业板|创新药|军工")

_lock = threading.Lock()
_cache = {"mtime": None, "data": None}


# ---------- 数据层 ----------

def load_all():
    """读库并计算派生指标, 按 db mtime 缓存"""
    mtime = DB.stat().st_mtime
    if _cache["mtime"] == mtime and _cache["data"] is not None:
        return _cache["data"]
    with _lock:
        if _cache["mtime"] == mtime and _cache["data"] is not None:
            return _cache["data"]
        conn = sqlite3.connect(f"file:{DB}?mode=ro", uri=True)
        info = pd.read_sql_query("SELECT * FROM etf_info", conn)
        daily = pd.read_sql_query(
            "SELECT fund_id, trade_dt, price, increase_rt, fund_nav, nav_discount_rt, volume, "
            "unit_total, index_increase_rt FROM etf_daily", conn)
        mkt = pd.read_sql_query("SELECT * FROM market_daily", conn)
        runs = pd.read_sql_query(
            "SELECT run_at, status, trade_dt, pool_n, market_n FROM run_log "
            "ORDER BY run_at DESC LIMIT 30", conn)
        conn.close()

        daily = daily.sort_values(["fund_id", "trade_dt"])
        mkt = mkt.sort_values(["fund_id", "trade_dt"])
        # 50日回填历史只有 amount(份额,万份) 有值 -> 资金流用份额口径
        mkt["shares"] = mkt["amount"].where(mkt["amount"].notna() & (mkt["amount"] > 0),
                                            mkt["unit_total"])

        # ---- 全市场: 资金流/成交/折溢价 ----
        def pct_ndays(g, col, n):
            g = g.dropna(subset=[col])
            if len(g) < 2:
                return np.nan
            return (g[col].iloc[-1] / g[col].iloc[min(n, len(g) - 1)] - 1) * 100

        rows = []
        for fid, g in mkt.groupby("fund_id"):
            g = g.sort_values("trade_dt")
            vol = g["volume"]
            pm50 = g["nav_discount_rt"].dropna().tail(50)
            pm20 = g["nav_discount_rt"].dropna().tail(20)
            disc_now = g["nav_discount_rt"].iloc[-1]
            rows.append({
                "fund_id": fid,
                "scale": g["unit_total"].dropna().iloc[-1] if g["unit_total"].notna().any() else np.nan,
                "scale_chg_5d": pct_ndays(g, "shares", 5),
                "scale_chg_20d": pct_ndays(g, "shares", 20),
                "scale_chg_50d": pct_ndays(g, "shares", 50),
                "vol_avg_20d": vol.tail(20).mean(),
                "vol_avg_5d": vol.tail(5).mean(),
                "discount_now": disc_now,
                "premium_mean": pm50.mean(),
                "premium_std": pm50.std(),
                "days": len(g),
                # 溢价过热: 当前溢价≥0.8% 且显著偏离自身20日中枢(均值+2σ 或 抬升≥0.3pct)
                # 回测: 科创50/科创芯片 7/10 溢价冲至1.25-1.47%(板块中位仅0.2%)后分别又跌10%+/折算前深跌
                "prem_hot": bool(pd.notna(disc_now) and disc_now >= 0.8 and len(pm20) >= 5
                                 and (disc_now > pm20.mean() + 2 * max(pm20.std(), 0.05)
                                      or disc_now - pm20.mean() >= 0.3)),
            })
        flow = pd.DataFrame(rows)

        # ---- 全市场: 价格动量 + 技术指标 (market_daily.price 已覆盖全部品种) ----
        tech = []
        for fid, g in mkt.groupby("fund_id"):
            g = g.sort_values("trade_dt")
            # 只屏蔽真实的>=20%跳变(份额折算); 价格断层日的pct_change为NaN不应屏蔽
            p = g["price"].where(~(g["price"].pct_change().abs() >= 0.2))
            if len(p.dropna()) < 2:
                continue
            ma20 = p.rolling(20, min_periods=10).mean()
            ma60 = p.rolling(60, min_periods=20).mean()
            hi50 = p.rolling(50, min_periods=10).max()
            volr = g["volume"].tail(5).mean() / max(g["volume"].tail(20).mean(), 1)

            def chg(n):
                pp = p.dropna()
                return (pp.iloc[-1] / pp.iloc[max(0, len(pp) - 1 - n)] - 1) * 100 if len(pp) > 1 else np.nan

            # 连续下跌天数
            r = p.pct_change().tail(10)
            streak = 0
            for v in reversed(r.tolist()):
                if v is not None and not np.isnan(v) and v < 0:
                    streak += 1
                else:
                    break
            tech.append({
                "fund_id": fid, "price": p.iloc[-1],
                "ret_1d": chg(1), "ret_5d": chg(5), "ret_20d": chg(20), "ret_60d": chg(60),
                "ma20": ma20.iloc[-1] if len(ma20) else np.nan,
                "ma60": ma60.iloc[-1] if len(ma60) else np.nan,
                "ma20_slope": ((ma20.iloc[-1] / ma20.iloc[-6] - 1) * 100
                               if len(ma20) >= 6 and pd.notna(ma20.iloc[-6]) else np.nan),
                "drawdown": (p.iloc[-1] / hi50.iloc[-1] - 1) * 100 if len(hi50) and pd.notna(hi50.iloc[-1]) else np.nan,
                "vol_ratio": volr, "down_streak": streak,
            })
        mom = pd.DataFrame(tech)
        # 日涨跌优先用接口自带的 increase_rt(权威), 价格断层时比 pct 回算准确
        last_inc = mkt.sort_values("trade_dt").groupby("fund_id")["increase_rt"].last()
        if len(mom):
            mom = mom.set_index("fund_id")
            mom["ret_1d"] = last_inc.reindex(mom.index).combine_first(mom["ret_1d"])
            mom = mom.reset_index()

        cross = info.merge(flow, on="fund_id", how="left").merge(mom, on="fund_id", how="left")
        cross["is_tech"] = (cross["index_nm"].fillna("").str.contains(TECH_KW)
                            | cross["fund_nm"].fillna("").str.contains(TECH_KW))

        # ---- 板块指数(等权中位数) + 市场温度计 ----
        mkt_sig = market_gauge(daily, cross, mkt)

        # ---- 机会: 趋势上升 / 底部特征 ----
        c = cross
        base = c[c["scale"].notna() & (c["scale"] >= 2)].copy()
        trend = base[(base["price"] > base["ma20"]) & (base["ma20"] > base["ma60"])
                     & (base["ma20_slope"] > 0.3) & (base["ret_20d"] > 3)].copy()
        trend["trend_score"] = (trend["ret_20d"].fillna(0).clip(-30, 60) * 0.4
                                + trend["scale_chg_20d"].fillna(0).clip(-50, 100) * 0.3
                                + trend["vol_avg_5d"].fillna(0).apply(lambda v: min(np.log10(max(v, 1)) * 8, 30))).round(1)
        bottom = base[(base["drawdown"] < -20) & (base["ret_5d"] > -2)
                      & (base["vol_ratio"] < 1.0)].copy()
        bottom["bottom_score"] = ((-bottom["drawdown"].fillna(0)).clip(0, 40) * 0.5
                                  + (-bottom["discount_now"].fillna(0)).clip(0, 5) * 3
                                  + bottom["scale_chg_5d"].fillna(0).clip(-10, 20) * 0.8
                                  + (1 - bottom["vol_ratio"].fillna(1)) * 20).round(1)

        # ---- 暴跌风险 ----
        cross["risk"] = None
        m = cross["ret_1d"] < -4
        cross.loc[m, "risk"] = "当日大跌"
        m = cross["drawdown"] < -15
        cross.loc[m, "risk"] = (cross.loc[m, "risk"].fillna("") + "|深回撤").str.strip("|")
        m = (cross["price"] < cross["ma20"]) & (cross["ma20_slope"] < -0.2)
        cross.loc[m, "risk"] = (cross.loc[m, "risk"].fillna("") + "|趋势破位").str.strip("|")
        m = (cross["ret_1d"] < -3) & (cross["vol_ratio"] > 1.2)
        cross.loc[m, "risk"] = (cross.loc[m, "risk"].fillna("") + "|放量下跌").str.strip("|")
        # 溢价过热: 个基溢价冲高(≥0.8%且偏离自身中枢), 7/10科创50/科创芯片1.3-1.5%后深跌
        m = cross["prem_hot"].fillna(False)
        cross.loc[m, "risk"] = (cross.loc[m, "risk"].fillna("") + "|溢价过热").str.strip("|")
        # 高位兑现信号: 前期大涨后单日暴涨(情绪顶峰, 回测: 7/9涨停潮后平均回撤30%+)
        cross["take_profit"] = ((cross["ret_20d"] > 15) & (cross["ret_1d"] > 5)) | \
                               ((cross["ret_5d"] > 10) & (cross["vol_ratio"] > 1.5))
        # 下跌接刀风险: 深回撤仍在放量下跌/份额暴增(散户接盘)
        cross["knife"] = ((cross["drawdown"] < -15) & (cross["ret_5d"] < -3)
                          & (cross["scale_chg_5d"] > 5))

        data = {"cross": cross, "trend": trend, "bottom": bottom, "daily": daily,
                "mkt": mkt, "runs": runs, "gauge": mkt_sig,
                "loaded_at": datetime.now().isoformat(timespec="seconds")}
        _cache.update(mtime=mtime, data=data)
        return data


def market_gauge(daily, cross, mkt):
    """板块级市场温度计: 等权中位数收益序列 + 规则评估"""
    d = daily.sort_values(["fund_id", "trade_dt"]).copy()
    d["ret"] = d.groupby("fund_id")["price"].pct_change() * 100
    d.loc[d["ret"].abs() > 20, "ret"] = np.nan
    tech_ids = set(cross.loc[cross["is_tech"], "fund_id"])
    d["is_tech"] = d["fund_id"].isin(tech_ids)

    def agg(df):
        g = df.groupby("trade_dt").agg(ret=("ret", "median"), vol=("volume", "sum"),
                                       down=("ret", lambda x: (x < 0).mean() * 100))
        g["cum"] = (g["ret"].fillna(0) / 100 + 1).cumprod() * 100
        g["ma20"] = g["cum"].rolling(20, min_periods=5).mean()
        g["volr"] = g["vol"] / g["vol"].rolling(20, min_periods=5).mean().shift(1)
        return g

    allx, tech = agg(d), agg(d[d["is_tech"]])

    def judge(g):
        """返回 (等级, 信号列表) 等级: danger/warn/neutral/calm"""
        if len(g) < 6:
            return "neutral", ["历史数据不足"]
        sig, last = [], g.iloc[-1]
        r = g["ret"].tail(5).tolist()
        # 1. 情绪顶峰: 近5日出现单日 >= +3.5% 且当前距近日高点回落
        climax = [i for i, v in enumerate(r) if v is not None and not np.isnan(v) and v >= 3.5]
        if climax:
            after = r[climax[-1] + 1:]
            sig.append(f"近期出现单日暴涨{r[climax[-1]]:+.1f}%(情绪顶峰, 回测显示此后的追高品种平均回撤30%+)")
            if any(v is not None and not np.isnan(v) and v <= -2 for v in after):
                sig.append("暴涨后出现 ≥2% 阴线 → 顶部反转确认, 应减仓而非抄底")
        # 2. 趋势
        if pd.notna(last["ma20"]):
            if last["cum"] < last["ma20"]:
                slope = last["ma20"] / g["ma20"].iloc[-6] - 1 if pd.notna(g["ma20"].iloc[-6]) else 0
                sig.append(f"指数位于MA20下方{'且MA20下行' if slope < 0 else ''} → 下行趋势未破坏")
            else:
                sig.append("指数位于MA20上方, 趋势健康")
        # 3. 广度
        if last["down"] > 60:
            sig.append(f"当日{last['down']:.0f}%品种下跌, 广度恶化")
        # 4. 量能
        if pd.notna(last["volr"]) and last["volr"] > 1.3 and (last["ret"] or 0) < -1:
            sig.append(f"放量下跌(量比{last['volr']:.1f}) → 出货特征")
        level = ("danger" if any("反转确认" in s or "出货" in s for s in sig)
                 else "warn" if any("顶峰" in s or "MA20下方" in s or "广度" in s for s in sig)
                 else "calm" if any("健康" in s for s in sig) else "neutral")
        if not sig:
            sig.append("无明显信号")
        return level, sig

    lv_a, sig_a = judge(allx)
    lv_t, sig_t = judge(tech)

    # ---- 背离预警(逃顶信号) ----
    div = divergences(d, tech, allx, tpm=None)
    return {
        "series_all": [{"dt": i, "cum": round(r["cum"], 2), "ret": None if pd.isna(r["ret"]) else round(r["ret"], 2),
                         "volr": None if pd.isna(r["volr"]) else round(r["volr"], 2)} for i, r in allx.tail(60).iterrows()],
        "series_tech": [{"dt": i, "cum": round(r["cum"], 2)} for i, r in tech.tail(60).iterrows()],
        "level_all": lv_a, "signals_all": sig_a,
        "level_tech": lv_t, "signals_tech": sig_t,
        "divergence": div,
    }


def divergences(d, tech_idx, all_idx, tpm=None):
    """背离类逃顶预警。基于7月回测 + 经典量价机制, 逐日检查最近5个交易日。

    信号分级: [已验证]=本轮7/9前后回测有效; [机制]=经典机制推演, 样本不足待验证
    """
    sig = []
    dd = d.copy()
    ret_t = tech_idx["ret"]
    cum_t = tech_idx["cum"]
    cum_a = all_idx["cum"]
    vol_t = tech_idx["vol"]
    # 溢价中位序列(科技, 单位已是百分点)
    pm = dd[dd["is_tech"]].groupby("trade_dt")["nav_discount_rt"].median()
    if len(cum_t) < 2 or len(pm) < 2:
        return sig

    def back(series, n, default=None):
        """倒数第n个值(0基), 不足返回default"""
        return series.iloc[-1 - n] if len(series) > n else default

    r5_t = cum_t.iloc[-1] / back(cum_t, 5, cum_t.iloc[0]) - 1
    r5_a = cum_a.iloc[-1] / back(cum_a, 5, cum_a.iloc[0]) - 1
    r20_t = cum_t.iloc[-1] / back(cum_t, 20, cum_t.iloc[0]) - 1
    vol5 = vol_t.tail(5).mean() / max(vol_t.tail(20).mean(), 1)
    pm_now = pm.iloc[-1]
    pm_chg5 = pm_now - back(pm, 5, pm.iloc[0])
    pm_base = pm.tail(20).mean()
    ret_now = ret_t.iloc[-1] if pd.notna(ret_t.iloc[-1]) else 0
    hi20 = cum_t.tail(20).max()

    # 1 溢价-价格同涨(追价) [已验证: 7/8-7/9 溢价0.08->0.22与+5.25%暴涨同步, 随后板块-19%]
    if pd.notna(pm_chg5) and pm_chg5 > 0.05 and r5_t > 0.02:
        sig.append(f"[已验证] 价格上涨伴随溢价5日抬升{pm_chg5:+.2f}pct → 追价买入/情绪过热, "
                   "顶部特征, 兑现窗口")
    # 2 跌而溢价不降(承接幻觉) [已验证: 7/10 价格-2.98%溢价仍0.215%, 次日继续崩]
    if ret_now < -2 and pd.notna(pm_now) and pm_now > pm_base:
        sig.append("[已验证] 大跌日溢价未回落 → 抄底盘仍在硬扛, 抛压未释放完毕")
    # 3 宽基-科技背离(抽血) [机制]
    if r5_t > 0.02 and r5_a < 0.003:
        sig.append(f"[机制] 科技5日{r5_t*100:+.1f}% vs 宽基{r5_a*100:+.1f}% → 资金抽血式行情, "
                   "缺乏全市场支撑的科技冲高易夭折")
    # 4 缩量新高 [机制]
    if cum_t.iloc[-1] >= hi20 * 0.999 and vol5 < 0.8 and r20_t > 0:
        sig.append(f"[机制] 指数逼近20日新高但量能仅5/20日的{vol5:.0%} → 缩量冲高, 持续性存疑")
    # 5 放量滞涨/派发 [机制]
    if vol5 > 1.2 and abs(r5_t) < 0.01:
        sig.append(f"[机制] 量能放大至5/20日的{vol5:.1f}倍但价格原地踏步 → 高位派发出货特征")
    # 6 资金-价格背离(份额) [已验证: 7月中旬以来科技份额+22%价格-19%]
    # (板块级份额判断在报告里, 这里不重复)
    # 7 动量衰竭 [机制]
    m1 = back(cum_t, 1) / back(cum_t, 6, cum_t.iloc[0]) - 1 if len(cum_t) > 7 else 0
    if r5_t < m1 < 0.05 and m1 > 0.02:
        sig.append("[机制] 5日动量连续收窄 → 上涨动能衰竭, 谨防拐头")
    return sig


def dfj(df, cols=None):
    d = df[cols] if cols else df
    return json.loads(d.to_json(orient="records", double_precision=3))


# ---------- HTTP ----------

from http.server import HTTPServer, SimpleHTTPRequestHandler  # noqa: E402
sys.path.insert(0, str(Path(__file__).resolve().parent))
from report import build_report  # noqa: E402


class Handler(SimpleHTTPRequestHandler):
    def __init__(self, *a, **kw):
        super().__init__(*a, directory=str(WEB), **kw)

    def log_message(self, fmt, *args):
        pass

    def _json(self, obj, code=200):
        body = json.dumps(obj, ensure_ascii=False).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        path = self.path.split("?")[0]
        try:
            if path.startswith("/api/"):
                return self.api(path)
            return super().do_GET()
        except BrokenPipeError:
            pass
        except Exception as e:
            self._json({"error": f"{type(e).__name__}: {e}"}, 500)

    def api(self, path):
        D = load_all()
        cross = D["cross"]
        opp_cols = ["fund_id", "fund_nm", "category", "index_nm", "price", "ret_1d", "ret_5d",
                    "ret_20d", "ret_60d", "scale", "scale_chg_5d", "scale_chg_20d",
                    "discount_now", "vol_avg_5d", "vol_ratio", "drawdown", "ma20_slope"]

        if path == "/api/overview":
            pool = cross[cross["in_pool"] == 1]
            total_scale = cross["scale"].sum()
            runs = D["runs"]
            return self._json({
                "as_of": runs.iloc[0]["trade_dt"] if len(runs) else None,
                "loaded_at": D["loaded_at"],
                "n_funds": len(cross), "n_pool": len(pool),
                "total_scale": round(total_scale, 1),
                "today_amount_yi": round(
                    D["mkt"].groupby("fund_id").tail(1)["volume"].sum() / 1e4, 1),
                "top10_share": round(
                    cross.nlargest(10, "scale")["scale"].sum() / total_scale * 100, 1),
                "cats": dfj(cross.groupby("category").agg(
                    n=("fund_id", "count"), scale=("scale", "sum")).reset_index()),
                "gauge": D["gauge"],
                "last_runs": dfj(runs.head(5)),
            })

        if path == "/api/table":
            cols = ["fund_id", "fund_nm", "category", "index_nm", "is_tech", "price",
                    "ret_1d", "ret_5d", "ret_20d", "ret_60d", "drawdown", "ma20", "ma60",
                    "scale", "scale_chg_5d", "scale_chg_20d", "scale_chg_50d",
                    "discount_now", "premium_mean", "vol_avg_5d", "vol_ratio",
                    "down_streak", "t0", "fee", "risk"]
            return self._json(dfj(cross, [c for c in cols if c in cross.columns]))

        if path == "/api/opportunity":
            t = D["trend"].sort_values("trend_score", ascending=False).head(40)
            b = D["bottom"].sort_values("bottom_score", ascending=False).head(40)
            return self._json({"trend": dfj(t, opp_cols + ["trend_score"]),
                               "bottom": dfj(b, opp_cols + ["bottom_score"])})

        if path == "/api/risk":
            c = cross
            take = c[c["take_profit"] == True].nlargest(30, "ret_20d")  # noqa: E712
            dd = c[c["drawdown"].notna()].nsmallest(60, "drawdown")
            knife = c[c["knife"] == True].nsmallest(30, "drawdown")  # noqa: E712
            rk = c[c["risk"].notna()].copy()
            rk["n_risk"] = rk["risk"].str.count(r"\|") + 1
            rk = rk.sort_values(["n_risk", "ret_1d"]).head(80)
            hot = c[c["prem_hot"] == True].nlargest(40, "discount_now")  # noqa: E712
            cols = ["fund_id", "fund_nm", "category", "index_nm", "is_tech", "price",
                    "ret_1d", "ret_5d", "ret_20d", "ret_60d", "drawdown", "vol_ratio",
                    "scale_chg_5d", "scale_chg_20d", "discount_now", "premium_mean",
                    "scale", "vol_avg_5d", "down_streak"]
            return self._json({"take_profit": dfj(take, cols),
                               "drawdown": dfj(dd, cols),
                               "knife": dfj(knife, cols),
                               "prem_hot": dfj(hot, cols),
                               "risk": dfj(rk, cols + ["risk"])})

        if path == "/api/premium":
            p = cross[cross["discount_now"].notna()].copy()
            per = cross[cross["premium_mean"].notna()].nlargest(30, "premium_mean")
            return self._json({"high": dfj(p.nlargest(30, "discount_now")),
                               "low": dfj(p.nsmallest(30, "discount_now")),
                               "persistent": dfj(per, ["fund_id", "fund_nm", "category",
                                                       "premium_mean", "premium_std",
                                                       "discount_now", "scale_chg_20d",
                                                       "scale", "vol_avg_5d"])})

        if path == "/api/report":
            return self._json(build_report(D))

        if path.startswith("/api/detail/"):
            fid = path.split("/")[-1]
            d = D["daily"][D["daily"]["fund_id"] == fid]
            m = D["mkt"][D["mkt"]["fund_id"] == fid]
            i = cross[cross["fund_id"] == fid]
            return self._json({
                "info": dfj(i.head(1)),
                "daily": dfj(d.assign(inc=lambda x: x["price"].pct_change(-1) * 100,
                                      chg=lambda x: x["increase_rt"]),
                             ["trade_dt", "price", "fund_nav", "nav_discount_rt",
                              "volume", "unit_total", "inc", "chg"]),
                "market": dfj(m.assign(shares=lambda x: x["amount"].where(
                    x["amount"].notna() & (x["amount"] > 0), x["unit_total"]),
                    inc=lambda x: x["price"].pct_change(-1) * 100,
                    chg=lambda x: x["increase_rt"]),
                    ["trade_dt", "price", "volume", "unit_total", "shares",
                     "nav_discount_rt", "inc", "chg"]),
            })

        return self._json({"error": "not found"}, 404)


def main():
    port = int(sys.argv[1]) if len(sys.argv) > 1 else 8787
    srv = HTTPServer(("0.0.0.0", port), Handler)
    load_all()  # 预热
    print(f"ETF 看板 v2: http://0.0.0.0:{port}")
    srv.serve_forever()


if __name__ == "__main__":
    main()
