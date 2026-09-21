#!/bin/bash
echo "started $(date)" >> /tmp/jisilu_launchd_test.log
exec /Users/wannengzhang/work/Jisilu-Data/.venv/bin/python /Users/wannengzhang/work/Jisilu-Data/daily_sync.py 2>&1
