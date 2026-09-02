#!/bin/bash
# 实时监控拾遗状态，每 5 秒刷新
DIR="$(cd "$(dirname "$0")" && pwd)"

trap 'echo ""; echo "已退出"; exit 0' INT

while true; do
    clear
    bash "$DIR/status.sh"
    echo ""
    echo "$(date '+%H:%M:%S') 每5秒刷新 | 按 Ctrl+C 退出"
    sleep 5
done
