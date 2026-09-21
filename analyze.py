# -*- coding: utf-8 -*-
"""集思录 ETF 全量分析
输入: etf_full.json / gold_full.json / money_full.json (截面)
      etf_history.jsonl (每只近50个交易日历史, crawl_history.py 产出)
输出: ETF分析报告_YYYYMMDD.xlsx (多sheet) + ETF分析摘要_YYYYMMDD.md
单位约定: 规模 unit_total=亿元, 成交额 volume=万元, 份额 amount=万份
"""
import json
import re
from datetime import date

import numpy as np
import pandas as pd

from metrics import lookback_change_pct

TODAY = date.today().strftime("%Y%m%d")

# ---------- 数据加载与清洗 ----------

def num(s):
    """'0.07'/'0.07%'/'-' -> float(nan)"""
    if s is None or s == "" or s == "-":
        return np.nan
    if isinstance(s, (int, float)):
        return float(s)
    return float(re.sub(r"[%,]", "", str(s).strip()))


def load_cross():
    frames = []
    for src, cat in [("etf_full.json", "指数ETF"), ("gold_full.json", "黄金ETF"),
                     ("money_full.json", "货币ETF")]:
        d = json.load(open(src, encoding="utf-8"))
        df = pd.DataFrame([r["cell"] for r in d["rows"]])
        df["category"] = cat
        frames.append(df)
    df = pd.concat(frames, ignore_index=True)

    out = pd.DataFrame({
        "代码": df["fund_id"],
        "名称": df["fund_nm"],
        "类别": df["category"],
        "收盘价": df["price"].map(num),
        "今日涨跌%": df["increase_rt"].map(num),
        "规模(亿)": df["unit_total"].map(num),
        "成交额(万)": df["volume"].map(num),
        "场内份额(万份)": df["amount"].map(num),
        "份额日增(万份)": df["amount_incr"].map(lambda v: num(v) if v not in (None, "") else np.nan),
        "净值": df["fund_nav"].map(num),
        "净值折溢价%": df["nav_discount_rt"].map(num),
        "总费率%": df["fee"].map(num),
        "管理费%": df["m_fee"].map(num),
        "托管费%": df["t_fee"].map(num),
        "跟踪指数": df["index_nm"].replace({"-": "", None: ""}),
        "指数今日涨跌%": df["index_increase_rt"].map(num),
        "T0": df["t0"],
        "管理人": df["issuer_nm"],
        "最小申赎单位(万份)": df["creation_unit"].map(num),
    })
    return out


def load_history():
    """返回 DataFrame: 代码/日期/收盘价/溢价率%/成交额(万)/份额(万份)/份额日增/指数涨跌%

    数据质量处理: 全局最新交易日为窗口终点, 只保留窗口内(约最近50个交易日)的行。
    已发现有81只基金(多为2026-07-01前有份额折算/数据断更的品种)返回的历史混有
    2020年老数据, 不加过滤会把"6年份额变化"当成"20日变化"。窗口过滤后这些基金
    历史行数<2, 自动不参与份额变化/溢价率统计(今日截面数据不受影响)。
    """
    recs = []
    try:
        with open("etf_history.jsonl", encoding="utf-8") as f:
            for line in f:
                r = json.loads(line)
                fid = r["fund_id"]
                for c in r["history"]:
                    recs.append({
                        "代码": fid,
                        "日期": c.get("hist_dt"),
                        "收盘价": num(c.get("trade_price")),
                        "溢价率%": num(c.get("discount_rt")),
                        "成交额(万)": num(c.get("volume")),
                        "份额(万份)": num(c.get("amount")),
                        "份额日增(万份)": num(c.get("amount_incr")),
                        "指数涨跌%": num(c.get("idx_incr_rt")),
                    })
    except OSError:
        pass
    if not recs:
        return pd.DataFrame()
    df = pd.DataFrame(recs)
    latest = df["日期"].max()
    cutoff = (pd.Timestamp(latest) - pd.Timedelta(days=71)).strftime("%Y-%m-%d")
    df = df[df["日期"] >= cutoff]
    return df


# ---------- 分析 ----------

def idx_series_count(cross):
    s = cross.loc[cross["类别"] == "指数ETF", "跟踪指数"]
    return int(s[s != ""].nunique())


def fmt_scale(v):
    return f"{v:,.1f}"


def fmt_or_dash(v, spec="+.1f"):
    return format(v, spec) if pd.notna(v) else "-"


def overview(cross):
    idx = cross[cross["类别"] != "货币ETF"]
    money = cross[cross["类别"] == "货币ETF"]
    rows = []
    for cat, g in cross.groupby("类别"):
        rows.append({
            "类别": cat, "数量": len(g),
            "总规模(亿)": round(g["规模(亿)"].sum(), 1),
            "总成交额(亿)": round(g["成交额(万)"].sum() / 1e4, 1),
            "规模中位数(亿)": round(g["规模(亿)"].median(), 2),
            "平均总费率%": round(g["总费率%"].mean(), 3),
        })
    ov = pd.DataFrame(rows)

    g = cross["规模(亿)"]
    buckets = pd.Series({
        "≥1000亿": (g >= 1000).sum(),
        "100~1000亿": ((g >= 100) & (g < 1000)).sum(),
        "10~100亿": ((g >= 10) & (g < 100)).sum(),
        "2~10亿": ((g >= 2) & (g < 10)).sum(),
        "0.5~2亿": ((g >= 0.5) & (g < 2)).sum(),
        "<0.5亿(清盘风险区)": (g < 0.5).sum(),
    }, name="数量")
    top10_share = cross.nlargest(10, "规模(亿)")["规模(亿)"].sum() / g.sum()
    return ov, buckets, top10_share, idx, money


def index_analysis(cross):
    idx = cross[(cross["类别"] == "指数ETF") & (cross["跟踪指数"] != "")]
    grp = idx.groupby("跟踪指数").agg(
        ETF数量=("代码", "count"),
        指数总规模=("规模(亿)", "sum"),
        最大单只规模=("规模(亿)", "max"),
        最低总费率=("总费率%", "min"),
    ).sort_values("指数总规模", ascending=False)

    # 同指数多只 -> 找出每指数的规模冠军 + 冠军是否也是最低费率
    multi = grp[grp["ETF数量"] > 1].copy()
    leader = idx.loc[idx.groupby("跟踪指数")["规模(亿)"].idxmax(),
                     ["跟踪指数", "代码", "名称", "规模(亿)", "总费率%", "成交额(万)"]]
    leader = leader.rename(columns={"代码": "龙头代码", "名称": "龙头名称",
                                    "规模(亿)": "龙头规模(亿)", "总费率%": "龙头总费率%",
                                    "成交额(万)": "龙头成交额(万)"})
    multi = multi.join(leader.set_index("跟踪指数"), how="left")
    # 尾部: 同指数里规模不足龙头10%的"陪跑"数量
    def stragglers(g):
        mx = g["规模(亿)"].max()
        return int((g["规模(亿)"] < mx * 0.1).sum())
    sl = idx.groupby("跟踪指数").apply(stragglers, include_groups=False)
    multi["陪跑数量(<龙头10%)"] = sl
    return grp, multi.reset_index()


def history_stats(hist):
    """每只ETF的50日统计: 份额流、折溢价、成交"""
    if hist.empty:
        return pd.DataFrame()
    hist = hist.sort_values(["代码", "日期"])
    agg = hist.groupby("代码").agg(
        有效天数=("日期", "count"),
        最新份额=("份额(万份)", "last"),
        期初份额=("份额(万份)", "first"),
        **{"日均成交额(万)": ("成交额(万)", "mean"),
           "近5日日均成交(万)": ("成交额(万)", lambda x: x.tail(5).mean())},
    )
    agg["份额50日变化%"] = (agg["最新份额"] / agg["期初份额"] - 1) * 100

    def pct_change_ndays(x, n):
        return lookback_change_pct(x, n)

    agg["份额近5日变化%"] = hist.groupby("代码")["份额(万份)"].apply(
        lambda x: pct_change_ndays(x, 5))
    agg["份额近20日变化%"] = hist.groupby("代码")["份额(万份)"].apply(
        lambda x: pct_change_ndays(x, 20))

    # 折溢价统计；|溢价|>20% 视为净值/价格口径错位（如份额折算日），剔除并计数
    dp_all = hist[hist["溢价率%"].notna()]
    dp = dp_all[dp_all["溢价率%"].abs() <= 20].groupby("代码")["溢价率%"].agg(
        溢价率均值="mean", 溢价率标准差="std",
        溢价率最大="max", 溢价率最小="min")
    dp["溢价异常天数"] = dp_all[dp_all["溢价率%"].abs() > 20].groupby("代码").size()
    # 连续净申购/净赎回天数（以最近一天为终点）
    def consec_incr(x):
        x = x.dropna()
        n = 0
        for v in reversed(x.tolist()):
            if v > 0:
                n += 1
            else:
                break
        return n
    agg["连续净申购天数"] = hist.groupby("代码")["份额日增(万份)"].apply(consec_incr)
    agg["连续净赎回天数"] = hist.groupby("代码")["份额日增(万份)"].apply(
        lambda x: consec_incr(-x))
    return agg.join(dp).reset_index()


# ---------- 输出 ----------

def fmt(df, cols=None):
    d = df.copy()
    for c in d.columns:
        if pd.api.types.is_float_dtype(d[c]):
            d[c] = d[c].round(3)
    return d[cols] if cols else d


def main():
    cross = load_cross()
    hist = load_history()
    stats = history_stats(hist)
    if not stats.empty:
        cross = cross.merge(stats, on="代码", how="left")

    ov, buckets, top10_share, idx, money = overview(cross)
    grp, multi = index_analysis(cross)

    with pd.ExcelWriter(f"ETF分析报告_{TODAY}.xlsx", engine="openpyxl") as w:
        # 1 概览
        ov2 = pd.concat([ov,
                         pd.DataFrame([{"类别": "合计", "数量": len(cross),
                                        "总规模(亿)": round(cross["规模(亿)"].sum(), 1),
                                        "总成交额(亿)": round(cross["成交额(万)"].sum() / 1e4, 1),
                                        "规模中位数(亿)": round(cross["规模(亿)"].median(), 2),
                                        "平均总费率%": np.nan}])],
                        ignore_index=True)
        ov2.to_excel(w, sheet_name="1概览", index=False)
        pd.DataFrame({
            "规模区间": buckets.index, "数量": buckets.values,
            "占比%": (buckets.values / buckets.sum() * 100).round(1),
        }).to_excel(w, sheet_name="1概览", index=False, startrow=len(ov2) + 3)
        pd.DataFrame({"指标": ["TOP10规模集中度%", "T+0 ETF数量", "覆盖跟踪指数数"],
                      "值": [round(top10_share * 100, 1),
                            int((cross["T0"] == "Y").sum()),
                            idx_series_count(cross)]
                      }).to_excel(w, sheet_name="1概览", index=False,
                                  startrow=len(ov2) + 3 + len(buckets) + 3)

        # 2 全量明细
        fmt(cross).assign(集思录详情=lambda d: "https://www.jisilu.cn/data/etf/detail/" + d["代码"]
        ).to_excel(w, sheet_name="2全量明细", index=False)

        # 3 规模TOP50
        fmt(cross.nlargest(50, "规模(亿)")).to_excel(w, sheet_name="3规模TOP50", index=False)

        # 4 跟踪指数
        fmt(grp.head(80)).to_excel(w, sheet_name="4跟踪指数竞争", index=False)
        fmt(multi.head(80)).to_excel(w, sheet_name="4跟踪指数竞争", index=False,
                                     startrow=len(grp.head(80)) + 3)

        # 5 份额流
        if "份额近20日变化%" in cross.columns:
            sf = cross[cross["份额近20日变化%"].notna()]
            pd.concat([
                sf.nlargest(30, "份额近20日变化%").assign(方向="近20日净申购TOP30"),
                sf.nsmallest(30, "份额近20日变化%").assign(方向="近20日净赎回TOP30"),
                sf[sf["连续净申购天数"] >= 5].assign(方向="连续≥5日净申购"),
            ])[["方向", "代码", "名称", "类别", "跟踪指数", "规模(亿)", "份额近5日变化%",
                "份额近20日变化%", "份额50日变化%", "连续净申购天数", "连续净赎回天数",
                "净值折溢价%", "成交额(万)"]].to_excel(w, sheet_name="5份额流", index=False)

        # 6 折溢价
        pd.concat([
            cross[cross["净值折溢价%"].abs() >= 1].assign(异常="|今日净值折溢价|≥1%")
                .nlargest(50, "净值折溢价%")[["异常", "代码", "名称", "类别", "规模(亿)",
                                              "净值折溢价%", "成交额(万)", "今日涨跌%"]],
            (cross.nlargest(30, "溢价率标准差")[["代码", "名称", "类别", "规模(亿)",
                    "溢价率均值", "溢价率标准差", "溢价率最大", "溢价率最小"]]
                .assign(异常="50日溢价率波动TOP30")),
            (cross[cross["溢价率均值"] >= 0.5].nlargest(30, "溢价率均值")
                [["代码", "名称", "类别", "规模(亿)", "溢价率均值", "溢价率标准差",
                  "份额近20日变化%", "成交额(万)"]].assign(异常="50日均溢价≥0.5%(持续溢价)")),
        ]).to_excel(w, sheet_name="6折溢价与异常", index=False)

        # 7 流动性与清盘风险
        liq_cols = ["代码", "名称", "类别", "跟踪指数", "规模(亿)", "成交额(万)",
                    "日均成交额(万)", "近5日日均成交(万)", "总费率%"]
        zombie = cross[(cross["日均成交额(万)"] < 100) & (cross["规模(亿)"] < 2)] \
            .sort_values("规模(亿)")
        delist = cross[cross["规模(亿)"] < 0.5].sort_values("规模(亿)")
        shrink = cross[(cross["份额50日变化%"] < -30) & cross["规模(亿)"].notna()] \
            .sort_values("份额50日变化%")
        pd.concat([
            delist.head(60).assign(风险="规模<0.5亿·清盘观察")[["风险"] + liq_cols],
            shrink.head(40).assign(风险="50日份额缩水>30%")[["风险"] + liq_cols +
                ["份额50日变化%", "份额近20日变化%"]],
            zombie.head(40).assign(风险="日均成交<100万且规模<2亿·流动性差")[["风险"] + liq_cols],
        ]).to_excel(w, sheet_name="7流动性与清盘风险", index=False)

        # 8 T+0 名单
        cross[cross["T0"] == "Y"].sort_values("规模(亿)", ascending=False)[
            ["代码", "名称", "类别", "跟踪指数", "规模(亿)", "成交额(万)", "总费率%",
             "净值折溢价%", "溢价率均值", "份额近20日变化%"]
        ].to_excel(w, sheet_name="8T0名单", index=False)

        # 9 低费率
        cross[cross["总费率%"].notna()].nsmallest(60, "总费率%")[
            ["代码", "名称", "类别", "跟踪指数", "总费率%", "管理费%", "托管费%",
             "规模(亿)", "成交额(万)"]
        ].to_excel(w, sheet_name="9低费率TOP60", index=False)

    # 明细sheet的详情链接列转为可点击超链接
    import openpyxl
    wb = openpyxl.load_workbook(f"ETF分析报告_{TODAY}.xlsx")
    ws = wb["2全量明细"]
    headers = {c.value: c.column for c in ws[1]}
    if "集思录详情" in headers:
        col = headers["集思录详情"]
        for cell in ws.iter_rows(min_row=2, min_col=col, max_col=col):
            c = cell[0]
            if c.value:
                c.hyperlink = c.value
                c.style = "Hyperlink"
    wb.save(f"ETF分析报告_{TODAY}.xlsx")

    # ---------- Markdown 摘要 ----------
    L = []
    A = L.append
    A(f"# 集思录 ETF 全量分析摘要（{date.today().isoformat()} 收盘）\n")
    A(f"数据来源: www.jisilu.cn 登录接口；覆盖 {len(cross)} 只场内基金"
      f"（指数ETF {(cross['类别']=='指数ETF').sum()} / 黄金ETF {(cross['类别']=='黄金ETF').sum()}"
      f" / 货币ETF {(cross['类别']=='货币ETF').sum()}），"
      f"含每只近 50 个交易日历史。\n")
    if not hist.empty:
        n_valid = int((stats["有效天数"] >= 2).sum()) if not stats.empty else 0
        try:
            n_total = sum(1 for _ in open("etf_history.jsonl", encoding="utf-8"))
        except OSError:
            n_total = len(stats)
        n_stale = n_total - len(stats)
        A(f"数据说明: 逐日历史有效覆盖 {n_valid}/{n_total} 只"
          f"（{n_stale}只品种在集思录侧历史断更于2026-07-01，仅参与今日截面统计）；"
          f"折溢价统计已剔除单日|溢价|>20%的口径错位记录（如份额折算日）。\n")
    A(f"- 全市场总规模 **{cross['规模(亿)'].sum():,.0f} 亿元**，"
      f"总成交额 {cross['成交额(万)'].sum()/1e4:,.0f} 亿元；"
      f"TOP10 规模集中度 {top10_share*100:.1f}%")
    A(f"- 规模分布: <0.5亿 **{buckets['<0.5亿(清盘风险区)']}只** 处于清盘风险区；"
      f"≥1000亿 {buckets['≥1000亿']}只；100~1000亿 {buckets['100~1000亿']}只")
    A(f"- 跟踪指数 {idx_series_count(cross)} 个，"
      f"同指数多只竞争的有 {len(multi)} 个指数；T+0 品种 {(cross['T0']=='Y').sum()} 只\n")

    if not hist.empty:
        sf = cross[cross["份额近20日变化%"].notna()]
        A("## 资金流向（近20日份额变化）\n")
        A("**净申购 TOP10**")
        for _, r in sf.nlargest(10, "份额近20日变化%").iterrows():
            A(f"- {r['名称']}({r['代码']}) {r['份额近20日变化%']:+.1f}% "
              f"规模{fmt_scale(r['规模(亿)'])}亿 {r['跟踪指数']}")
        A("\n**净赎回 TOP10**")
        for _, r in sf.nsmallest(10, "份额近20日变化%").iterrows():
            A(f"- {r['名称']}({r['代码']}) {r['份额近20日变化%']:+.1f}% "
              f"规模{fmt_scale(r['规模(亿)'])}亿 {r['跟踪指数']}")
        A("")

    A("## 折溢价观察\n")
    exc = cross[cross["净值折溢价%"].abs() >= 1].sort_values("净值折溢价%", key=abs, ascending=False)
    A(f"今日 |净值折溢价|≥1% 共 **{len(exc)}只**；典型:")
    for _, r in exc.head(8).iterrows():
        A(f"- {r['名称']}({r['代码']}) {r['净值折溢价%']:+.2f}% 成交{r['成交额(万)']:,.0f}万")
    A("")
    if "溢价率均值" in cross.columns:
        hp = cross[cross["溢价率均值"].notna()].nlargest(8, "溢价率均值")
        A("近50日平均溢价率最高(已剔除单日口径错位):")
        for _, r in hp.iterrows():
            A(f"- {r['名称']}({r['代码']}) 均值{r['溢价率均值']:+.2f}% "
              f"波动±{r['溢价率标准差']:.2f}% "
              f"份额20日{fmt_or_dash(r.get('份额近20日变化%'))}% "
              f"规模{fmt_scale(r['规模(亿)'])}亿")

    A("\n## 风险清单\n")
    A(f"- 清盘观察(规模<0.5亿): **{len(cross[cross['规模(亿)']<0.5])}只**")
    if "份额50日变化%" in cross.columns:
        sh = cross[cross["份额50日变化%"] < -30]
        A(f"- 50日份额缩水>30%: **{len(sh)}只**，如 " +
          "、".join(f"{r['名称']}({r['份额50日变化%']:.0f}%)" for _, r in sh.head(5).iterrows()))
    zz = cross[(cross["日均成交额(万)"] < 100) & (cross["规模(亿)"] < 2)]
    A(f"- 流动性差(日均成交<100万且规模<2亿): **{len(zz)}只**")

    open(f"ETF分析摘要_{TODAY}.md", "w", encoding="utf-8").write("\n".join(L))
    print("已生成:")
    print(f"  ETF分析报告_{TODAY}.xlsx")
    print(f"  ETF分析摘要_{TODAY}.md")


if __name__ == "__main__":
    main()
