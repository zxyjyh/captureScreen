#!/bin/bash
# 停止采集服务
set -euo pipefail

DIR="$(cd "$(dirname "$0")" && pwd)"
LABEL="com.gleaner.agent"
PLIST="$HOME/Library/LaunchAgents/$LABEL.plist"

# KeepAlive 会把被 kill 的进程重新拉起来，所以必须 unload 而不是 kill
if [ -f "$PLIST" ] && launchctl list 2>/dev/null | grep -q "$LABEL"; then
    launchctl unload "$PLIST"
    echo "采集服务已停止（重新启动：bash $DIR/start.sh）"
else
    echo "采集服务未在运行"
fi

rm -f "$DIR/capture.pid"
