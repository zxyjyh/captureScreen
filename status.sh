#!/bin/bash
# 查看截图采集系统状态
DIR="$(cd "$(dirname "$0")" && pwd)"
LABEL="com.capturescreen.agent"

# 进程状态以 launchd 为准：KeepAlive 重拉进程后 capture.pid 可能还是旧的
AGENT_LINE=$(launchctl list 2>/dev/null | grep -E "capturescreen|$LABEL" | head -1)
if [ -n "$AGENT_LINE" ]; then
    PID=$(echo "$AGENT_LINE" | cut -f1)
    if [ "$PID" != "-" ]; then
        ELAPSED=$(ps -o etime= -p "$PID" 2>/dev/null | tr -d ' ')
        echo "状态: 运行中 (PID=$PID, 已运行 ${ELAPSED:-?})"
    else
        echo "状态: 已注册但未运行 (上次退出码 $(echo "$AGENT_LINE" | cut -f2))"
    fi
else
    echo "状态: 未运行"
fi

# 今日截图
TODAY=$(date +%Y-%m-%d)
TODAY_DIR="$DIR/screenshots/$TODAY"
if [ -d "$TODAY_DIR" ]; then
    PNG_COUNT=$(ls "$TODAY_DIR"/*.png 2>/dev/null | wc -l | tr -d ' ')
    LATEST=$(ls -t "$TODAY_DIR"/*.png 2>/dev/null | head -1)
    if [ -n "$LATEST" ]; then
        # 副屏文件名带 -s2 后缀，直接换成冒号会显示成 10:27:24:s2
        LATEST_TIME=$(basename "$LATEST" .png | sed 's/-s[0-9]*$//' | tr '-' ':')
        echo "今日截图: ${PNG_COUNT} 张 (最新: ${LATEST_TIME})"
    fi
else
    echo "今日截图: 0 张"
fi

# 历史统计
TOTAL=0
for d in "$DIR"/screenshots/*/; do
    [ -d "$d" ] && TOTAL=$((TOTAL + $(ls "$d"/*.png 2>/dev/null | wc -l)))
done
echo "累计截图: ${TOTAL} 张"

# 报告
REPORT_DIR="$DIR/reports"
if [ -d "$REPORT_DIR" ]; then
    REPORT_COUNT=$(find "$REPORT_DIR" -name "*.md" 2>/dev/null | wc -l | tr -d ' ')
    LATEST_REPORT=$(find "$REPORT_DIR" -name "*.md" -exec ls -t {} + 2>/dev/null | head -1)
    if [ -n "$LATEST_REPORT" ]; then
        LATEST_REPORT_NAME=$(basename "$LATEST_REPORT")
        echo "报告: ${REPORT_COUNT} 份 (最新: ${LATEST_REPORT_NAME})"
    else
        echo "报告: ${REPORT_COUNT} 份"
    fi
fi

# cron 任务
# 无障碍文本覆盖率 —— 掉到 0 通常意味着辅助功能权限失效了，
# 而这个失效是静默的：截图照常，只是分析会退回昂贵的图片模式
TODAY_TXT=0
[ -d "$TODAY_DIR" ] && TODAY_TXT=$(ls "$TODAY_DIR"/*.txt 2>/dev/null | wc -l | tr -d ' ')
echo "今日文本: ${TODAY_TXT} 份"

echo ""
if crontab -l 2>/dev/null | grep -q "analyze.py"; then
    echo "⚠ crontab 里还有 analyze.py —— 与 capture.py 内部调度重复，会分析两遍"
    echo "  清理: bash $DIR/start.sh"
fi
PENDING=$("$DIR/venv/bin/python" -c "
import capture
from pathlib import Path
print(len(capture.pending_hours(Path('$DIR/screenshots'), Path('$DIR/reports'))))
" 2>/dev/null || echo "?")
[ "$PENDING" != "0" ] && echo "待分析: ${PENDING} 个小时（下个整点自动补）"

echo "自检: $DIR/venv/bin/python $DIR/doctor.py"
