# Jisilu 工作目录迁移到服务器指南

## 一、迁移内容

**要复制到服务器的**（建议整体打包上传）：

| 文件 | 说明 |
|---|---|
| `jisilu.py` `daily_sync.py` | 登录工具 + 每日同步主程序 |
| `crawl_history.py` `analyze.py` | 历史爬取 + 分析报告（可选） |
| `requirements.txt` `setup_server.sh` | 服务器初始化（本次新增） |
| `etf.db` | **核心资产**：50个交易日历史+每日增量，SQLite单文件可直接搬 |
| `jisilu_cred.json` | 登录凭据（明文，注意传输安全，建议 scp 而非网盘中转） |
| `etf_full.json` 等原始 JSON | 可选，数据已在库里 |

**不用复制**：`sync.cmd`（Windows专用）、`jisilu_session.json`（会话cookie，换机器会自动重新登录）、`__pycache__/`、`daily_sync.log`、分析报告产物（可重新生成）。

## 二、服务器端步骤（Linux）

```bash
# 1. 上传（在本机执行, 路径按实际改; 注意引号内的空格）
scp -r "C:/Users/wanneng.zhang/OneDrive/2-Work/AI_work/Jisilu" user@服务器IP:~/jisilu

# 2. 登录服务器初始化（一条命令完成: venv + 依赖 + 试跑 + cron）
cd ~/jisilu && bash setup_server.sh
```

setup_server.sh 会做四件事：建 `.venv` 虚拟环境、装依赖、**试运行一次**（验证服务器能否访问集思录、登录是否正常、数据库完好）、注册每日 22:00 的 cron。

## 三、Windows 本机收尾（服务器确认正常后）

```powershell
# 停用本机计划任务，避免两边同时跑产生两份分叉数据
Disable-ScheduledTask -TaskName 'JisiluETF每日同步'
# 确认不再需要后可彻底删除:
# Unregister-ScheduledTask -TaskName 'JisiluETF每日同步' -Confirm:$false
```

## 四、注意事项

1. **时区**：cron 按服务器本地时间触发。若服务器不是北京时间，先 `sudo timedatectl set-timezone Asia/Shanghai`，或把 cron 里的小时改掉（北京 22 点 = UTC 14 点）。
2. **海外服务器风险**：集思录是国内站点，海外 IP 访问/登录可能变慢或触发风控。建议用国内服务器；试跑若登录失败就是这个原因。
3. **拷贝时机**：复制 `etf.db` 前先停掉本机计划任务（或避开 22:00 前后），防止拷到写了一半的文件。更稳妥用 `sqlite3 etf.db ".backup etf_backup.db"` 再传备份文件改名。
4. **账号安全**：`jisilu_cred.json` 是明文密码，用 scp/sftp 直传，不要走网盘/聊天工具。
5. **验证迁移成功**：服务器上 `python daily_sync.py` 输出 `OK 交易日=...`，且 `crontab -l` 能看到任务，第二天 `tail daily_sync.log` 有新记录即完全就绪。
6. 若服务器是 **Windows Server**：不需要 setup_server.sh，装 Python 后 `pip install -r requirements.txt`，计划任务照本机 `sync.cmd` 的做法重建即可（路径改为服务器路径）。
