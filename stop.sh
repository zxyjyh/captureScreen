#!/bin/bash
# 停止截图采集 + 移除 crontab 任务
set -e

DIR="$(cd "$(dirname "$0")" && pwd)"
PID_FILE="$DIR/capture.pid"

# 停止截图进程
if [ -f "$PID_FILE" ]; then
    PID="$(cat "$PID_FILE")"
    if kill -0 "$PID" 2>/dev/null; then
        kill "$PID"
        echo "Capture stopped (PID=$PID)"
    else
        echo "Capture process not found (stale PID=$PID)"
    fi
    rm -f "$PID_FILE"
else
    echo "No PID file found. Capture may not be running."
fi

# 移除 crontab 中的任务
if crontab -l 2>/dev/null | grep -qE "analyze.py|summarize.py"; then
    crontab -l 2>/dev/null | grep -vE "analyze.py|summarize.py" | crontab -
    echo "Cron jobs removed."
fi

echo "Stopped."
