# -*- coding: utf-8 -*-
"""程序化解读报告 - 由 etf.db 当前数据按固定规则生成

build_report(D) 接收 app.load_all() 的缓存数据, 输出结构化报告(dict)。
数据每日 22:00 daily_sync 后自动刷新(app 按 db mtime 重新计算)。
"""
import numpy as np
import pandas as pd

TECH = ("科技|科创|芯片|半导体|人工智能|AI|计算机|信息|软件|通信|电子|机器人"
        "|大数据|云计算|创业板|创新药|军工")

SECTORS = {
    "红利/低波": ["红利", "低波", "国企"],
    "医药/创新药": ["医药", "医疗", "创新药", "疫苗", "生科"],
    "消费": ["消费", "食品", "酒", "家电", "畜牧", "养殖"],
    "金融": ["银行", "证券", "保险", "金融"],
    "周期资源": ["有色", "煤炭", "钢铁", "化工", "油气", "电力", "基建", "地产", "稀土"],
    "大盘蓝筹": ["沪深300", "上证50", "A50", "中证800"],
    "军工": ["军工", "国防"],
    "科技": ["科创", "芯片", "半导体", "AI", "人工智能", "软件", "计算机", "云计算",
             "大数据", "机器人", "通信", "电子", "信息"],
    "创业板/成长": ["创业板", "1000", "2000", "成长"],
    "新能源": ["新能源", "光伏", "电池", "碳中和"],
}


def _sector(idx_nm, category):
    s = str(idx_nm or "")
    if category == "黄金ETF":
        return "黄金"
    for name, kws in SECTORS.items():
        if any(k in s for k in kws):
            return name
    return None


def build_report(D):
    cross, daily = D["cross"], D["daily"]
    g = D["gauge"]
    d = daily.sort_values(["fund_id", "trade_dt"]).copy()
    d["ret"] = d.groupby("fund_id")["price"].pct_change() * 100
    d.loc[d["ret"].abs() > 20, "ret"] = np.nan
    d["tech"] = d["fund_id"].isin(set(cross.loc[cross["is_tech"], "fund_id"]))
    start_dt = d["trade_dt"].min()
    as_of = d["trade_dt"].max()

    # ---- 板块表现(每只自start累计, 组内中位) ----
    cum = d.groupby("fund_id").apply(
        lambda x: ((1 + x["ret"].fillna(0) / 100).cumprod().iloc[-1] - 1) * 100,
        include_groups=False).rename("cum")
    info = cross.set_index("fund_id")[["index_nm", "category"]]
    df = info.join(cum)
    df["sector"] = [_sector(r["index_nm"], r["category"]) for _, r in df.iterrows()]
    sec = df.groupby("sector")["cum"].agg(["median", "count"])
    sec = sec[sec["count"] >= 2].sort_values("median", ascending=False)
    sector_tbl = [[i, int(r["count"]), f"{r['median']:+.1f}%"] for i, r in sec.iterrows()]

    # ---- 资金流 top ----
    f = cross.dropna(subset=["scale_chg_20d"]).copy()
    fl = f.groupby("index_nm").filter(lambda x: len(x) >= 2)
    grp = fl.groupby("index_nm")["scale_chg_20d"].median().sort_values(ascending=False)
    grp = grp[grp.index != ""]
    flow_in = [[i, f"{v:+.1f}%"] for i, v in grp.head(6).items()]
    flow_out = [[i, f"{v:+.1f}%"] for i, v in grp.tail(6).items()]

    # ---- 科技近况 ----
    t = d[d["tech"]]
    tret = t.groupby("trade_dt")["ret"].median()
    tpm = t.groupby("trade_dt")["nav_discount_rt"].median()
    tpm5 = tpm.tail(5).mean()
    tech_cum = ((1 + tret.fillna(0) / 100).cumprod().iloc[-1] - 1) * 100
    worst = df[df["sector"].isin(["科技", "创业板/成长", "新能源"])]["cum"].min()

    # ---- 榜单 ----
    trend = D["trend"].head(6)
    knife = cross[(cross["knife"] == True)  # noqa: E712
                  & cross["drawdown"].notna()].nsmallest(6, "drawdown")
    trend_tbl = [[r["fund_nm"], r["index_nm"] or "-", f"{r['ret_20d']:+.1f}%",
                  f"{r['scale_chg_20d']:+.1f}%"] for _, r in trend.iterrows()]
    knife_tbl = [[r["fund_nm"], r["index_nm"] or "-", f"{r['ret_5d']:+.1f}%",
                  f"{r['scale_chg_5d']:+.1f}%"] for _, r in knife.iterrows()]

    sec_lines = []
    sec_lines.append(["h", "一、核心可交易池结构：钱从哪里来，到哪里去"])
    sec_lines.append(["table", ["板块", f"自{start_dt[5:]}累计(中位)", "构成"],
                      sector_tbl])
    sec_lines.append(["p", f"资金流(近20日份额中位变化)流入前列: " +
                      "、".join(f"{i} {v}" for i, v in flow_in) +
                      "；流出前列: " + "、".join(f"{i} {v}" for i, v in flow_out) +
                      "。若流入集中于下跌板块(散户接盘)、流出集中于上涨板块(机构兑现)，"
                      "说明行情由情绪而非机构主导，持续性存疑。"])

    sec_lines.append(["h", "二、科技板块：这轮下跌走到哪了"])
    sec_lines.append(["p", f"科技等权指数自 {start_dt} 以来累计 {tech_cum:+.1f}%，"
                      f"最深品种跌幅约 {worst:.0f}%。近5日溢价中位 {tpm5:+.2f}%。"])
    sec_lines.append(["ul", g["signals_tech"] and list(g["signals_tech"]) or ["无明显信号"]])
    sec_lines.append(["p", "底部确认需要三个条件同时出现：①缩量(量比<0.7) ②5日线走平不再创新低 "
                      "③放量长阳。在份额持续流入(承接充足)的情况下，阴跌通常未结束。"])

    sec_lines.append(["h", "三、当前趋势机会(顺势)"])
    sec_lines.append(["table", ["品种", "跟踪指数", "20日", "份额20日"], trend_tbl])
    sec_lines.append(["p", "注意区分'买入型上涨'(份额增)与'卖出型上涨'(份额减, 机构兑现)。"])

    sec_lines.append(["h", "四、当前风险(回避)"])
    sec_lines.append(["table", ["品种", "跟踪指数", "5日", "份额5日"], knife_tbl])
    sec_lines.append(["p", "深回撤+仍在下跌+散户涌入 = 接飞刀组合，本轮7月回测验证了"
                      "'涨得多可以跌得更深'。高溢价(>2%)品种任何时候不追。"])

    sec_lines.append(["h", "五、背离预警(逃顶信号)"])
    sec_lines.append(["ul", g.get("divergence", []) or ["当前无明显背离信号"]])
    sec_lines.append(["p", "背离信号提示的是未来数日的顶部风险，而非精确的次日预测"
                      "(样本统计上月度级别显著、次日级别不显著)。"])

    return {
        "as_of": as_of, "start": start_dt,
        "level_all": g["level_all"], "level_tech": g["level_tech"],
        "tech_cum": round(tech_cum, 1),
        "data_quality": D.get("data_quality", {}),
        "sections": sec_lines,
        "generated": D["loaded_at"],
    }
