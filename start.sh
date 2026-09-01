#!/bin/bash
# 启动截图采集 + 注册每小时分析任务
set -e

DIR="$(cd "$(dirname "$0")" && pwd)"
PID_FILE="$DIR/capture.pid"
LOG_FILE="$DIR/capture.log"
PYTHON="$DIR/venv/bin/python"
ENV_FILE="$DIR/.env"

# 从当前 shell 环境导出 API key 到 .env 文件（供 cron 使用）
source ~/.zshrc 2>/dev/null || true
if [ -n "$ZHIPU_API_KEY" ]; then
    echo "ZHIPU_API_KEY=$ZHIPU_API_KEY" > "$ENV_FILE"
    echo "API key saved to .env"
elif [ -n "$ZHIPUAI_API_KEY" ]; then
    echo "ZHIPUAI_API_KEY=$ZHIPUAI_API_KEY" > "$ENV_FILE"
    echo "API key saved to .env"
else
    echo "Warning: no ZHIPU_API_KEY found in environment"
fi

# 检查是否已在运行
if [ -f "$PID_FILE" ] && kill -0 "$(cat "$PID_FILE")" 2>/dev/null; then
    echo "Capture already running (PID=$(cat "$PID_FILE"))"
    exit 1
fi

# 启动截图采集（后台）
echo "Starting screen capture..."
nohup "$PYTHON" "$DIR/capture.py" > "$LOG_FILE" 2>&1 &
echo "Capture started (PID=$!). Log: $LOG_FILE"

# 注册 crontab：用 wrapper 脚本加载 .env 环境变量
CRON_CMD="env \$(cat $ENV_FILE 2>/dev/null | xargs) $PYTHON $DIR/analyze.py >> $DIR/analyze.log 2>&1"
CRON_ENTRY="0 * * * * $CRON_CMD"

DAILY_CMD="env \$(cat $ENV_FILE 2>/dev/null | xargs) $PYTHON $DIR/summarize.py >> $DIR/summarize.log 2>&1"
DAILY_ENTRY="59 23 * * * $DAILY_CMD"

# 检查是否已注册
(crontab -l 2>/dev/null || true) | grep -qF "analyze.py" || {
    (crontab -l 2>/dev/null || true; echo "$CRON_ENTRY"; echo "$DAILY_ENTRY") | crontab -
    echo "Hourly analysis + daily summary cron jobs registered."
}

echo ""
echo "All set! Screenshots will be captured every 5 minutes."
echo "Hourly analysis runs automatically via cron."
echo ""
echo "Commands:"
echo "  stop:    bash $DIR/stop.sh"
echo "  status:  cat $PID_FILE 2>/dev/null && kill -0 \$(cat $PID_FILE) && echo 'running' || echo 'stopped'"
echo "  reports: ls $DIR/reports/"
