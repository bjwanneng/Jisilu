# Jisilu ETF 数据系统

集思录 ETF 数据的自动同步、本地存储与交易员视角的 Web 看板。

## 组成

| 文件 | 说明 |
|---|---|
| `jisilu.py` | 集思录登录工具（凭据读 `jisilu_cred.json`，**不上传**） |
| `daily_sync.py` | 每日同步主程序：核心池全字段快照 + 全市场价格/份额/折溢价入库；含盘中有效性判断（20 点前运行跳过当日未就绪数据） |
| `crawl_history.py` | 一次性历史爬取（近 50 交易日） |
| `analyze.py` | Excel/Markdown 全量分析报告（独立于看板，可选） |
| `webapp/app.py` | Web 看板服务：直接读 `etf.db`，按库 mtime 自动刷新缓存 |
| `webapp/index.html` | 前端单页（ECharts） |
| `webapp/report.py` | 程序化 AI 解读报告（数据驱动，每日自动更新） |

## 快速开始

```bash
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt   # 网络有证书劫持时加 --trusted-host pypi.org --trusted-host files.pythonhosted.org
cp jisilu_cred.json.example jisilu_cred.json  # 填入集思录账号密码(仅本地)

.venv/bin/python daily_sync.py        # 同步(幂等, 可反复运行)
.venv/bin/python webapp/app.py 8787   # 看板 http://127.0.0.1:8787
```

## 定时任务 (macOS launchd)

- 每日同步：`~/Library/LaunchAgents/com.jisilu.etf-sync.plist`（22:00，经 `sync_mac.sh` 调起）
  - 手动触发：`launchctl kickstart gui/$(id -u)/com.jisilu.etf-sync`
- 看板服务建议同样注册 launchd 常驻（当前为手动启动）

注意：launchd 任务无法访问 `~/Desktop` 下的目录（macOS TCC 限制），项目需放在桌面以外的路径。

## 数据结构 (etf.db, SQLite)

- `etf_info`：全市场静态资料 + 核心池标记（同一跟踪指数只留规模龙头）
- `etf_daily`：核心池逐日全字段（价格/净值/折溢价/成交/份额）
- `market_daily`：全市场逐日（价格/涨跌幅/成交/份额/折溢价/规模）
- `run_log`：同步运行记录（OK/SKIP/FAIL）

## 看板功能

1. **市场温度计**：等权指数 + MA20 + 量比 + 广度；背离预警（溢价-价格背离、宽基-科技抽血、缩量新高、放量滞涨、动量衰竭），信号分「已验证」（7 月科技暴跌回测）与「机制」两级
2. **AI 解读**：板块表现/资金流/科技状态/机会与风险清单，随数据每日自动更新
3. **机会雷达**：趋势上升（价>MA20>MA60 且上行）/ 底部观察（深回撤+缩量+企稳）
4. **暴跌风险**：溢价过热（7/10 科创50/芯片 1.3-1.5% 溢价后深跌的形态）、高位兑现、回撤榜、接飞刀（散户涌入）
5. **折溢价 / 全量明细 / 单品种详情**（50 日图 + 历史数据表）

## 信号回测摘要（2026-07 科技暴跌）

- 7/9 情绪顶峰：科技单日 +5.25%，涨停品种与随后最大回撤相关 -0.657
- 个基溢价过热：科创50 0.76%→1.47%、科创芯片 0.85%→1.25%（板块中位仅 0.2%），随后深跌
- 份额逆流：下跌中科技份额 +22%（散户接盘），下跌未结束的确认

*数据来自 www.jisilu.cn，仅供个人研究，不构成投资建议。*
