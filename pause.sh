#!/bin/bash
# 临时暂停采集。到点自动恢复 —— 忘记恢复比忘记暂停常见得多。
#
#   bash pause.sh        暂停 30 分钟
#   bash pause.sh 120    暂停 120 分钟
#   bash pause.sh 0      立即恢复
set -euo pipefail

DIR="$(cd "$(dirname "$0")" && pwd)"
MINUTES="${1:-30}"
FILE="$DIR/.paused-until"

if [ "$MINUTES" = "0" ]; then
    rm -f "$FILE"
    echo "已恢复采集。"
    exit 0
fi

UNTIL=$(( $(date +%s) + MINUTES * 60 ))
umask 077
echo "$UNTIL" > "$FILE"
echo "已暂停采集 ${MINUTES} 分钟，$(date -r "$UNTIL" '+%H:%M') 自动恢复。"
echo "提前恢复：bash $DIR/pause.sh 0"
