#!/usr/bin/env bash
# 集思录ETF同步 - Linux服务器一键初始化
# 用法: 把整个 Jisilu 目录上传到服务器后, 在目录内执行:
#   bash setup_server.sh
# 功能: 建虚拟环境 -> 装依赖 -> 试跑一次 -> 注册每日22:00 cron
set -euo pipefail
cd "$(dirname "$0")"

echo "==> 检查 Python3"
command -v python3 >/dev/null || { echo "错误: 未找到 python3, 请先安装"; exit 1; }
python3 --version

echo "==> 创建虚拟环境 .venv"
if [ ! -d .venv ]; then
  python3 -m venv .venv
fi
# shellcheck disable=SC1091
source .venv/bin/activate

echo "==> 安装依赖"
pip install --quiet --upgrade pip
pip install --quiet -r requirements.txt

echo "==> 试运行一次 (验证网络/登录/数据库)"
python daily_sync.py

echo "==> 注册 cron (每日 22:00 服务器本地时间)"
CRON_LINE="0 22 * * * cd $(pwd) && $(pwd)/.venv/bin/python daily_sync.py >> $(pwd)/daily_sync.log 2>&1"
( crontab -l 2>/dev/null | grep -v "daily_sync.py" ; echo "$CRON_LINE" ) | crontab -

echo "==> 完成。当前 crontab:"
crontab -l | grep daily_sync || true
echo "提示: 若服务器不是北京时间, 先 'timedatectl set-timezone Asia/Shanghai'"
echo "      或把上面 cron 的 22 改成对应时区小时 (北京时间22点 = UTC 14点)"
