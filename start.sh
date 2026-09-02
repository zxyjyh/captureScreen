#!/bin/bash
# 启动采集服务
#
# 这里不再注册 crontab 跑 analyze.py：capture.py 主循环内部已经
# schedule 了每小时一次分析。两边都跑就是分析两遍、账单两份。
set -euo pipefail

DIR="$(cd "$(dirname "$0")" && pwd)"
LABEL="com.gleaner.agent"
PLIST="$HOME/Library/LaunchAgents/$LABEL.plist"

[ -f "$PLIST" ] || { echo "未安装，先跑：bash $DIR/install.sh" >&2; exit 1; }

# 清掉早期版本留下的 cron 任务，否则和 capture.py 内部调度重复
if crontab -l 2>/dev/null | grep -qE "analyze\.py|summarize\.py"; then
    crontab -l 2>/dev/null | grep -vE "analyze\.py|summarize\.py" | crontab -
    echo "已移除重复的 cron 分析任务（capture.py 内部已在调度）"
fi

if launchctl list 2>/dev/null | grep -q "$LABEL"; then
    launchctl unload "$PLIST" 2>/dev/null || true
fi
launchctl load "$PLIST"

sleep 1
bash "$DIR/status.sh"
