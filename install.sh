#!/bin/bash
# 拾遗.Gleaner 安装脚本 —— 从零到跑起来
#
# 这个脚本刻意不做的事：不把 API key 写进 LaunchAgent plist。
# plist 只描述「怎么跑」，凭证只存在于 .env（已被 .gitignore）。
# 否则每次想把启动配置提交进版本库，都要先记得手动抠掉密钥。
set -euo pipefail

DIR="$(cd "$(dirname "$0")" && pwd)"
VENV="$DIR/venv"
PY="$VENV/bin/python"
ENV_FILE="$DIR/.env"
LABEL="com.gleaner.agent"
PLIST="$HOME/Library/LaunchAgents/$LABEL.plist"
# 早期版本用过带用户名的 label，装新的之前先把旧的卸掉，避免两份同时采集
LEGACY_LABELS=("com.capturescreen.agent" "com.xufeifeng.capturescreen")

say()  { printf '\n\033[1m== %s\033[0m\n' "$1"; }
ok()   { printf '  \033[32m✓\033[0m %s\n' "$1"; }
warn() { printf '  \033[33m!\033[0m %s\n' "$1"; }
die()  { printf '  \033[31m✗\033[0m %s\n' "$1" >&2; exit 1; }

# ── 1. 环境检查 ────────────────────────────────────────────────
say "1/6 检查环境"
[ "$(uname)" = "Darwin" ] || die "只支持 macOS：无障碍树和 Vision OCR 都是系统能力"
SYS_PY="$(command -v python3 || true)"
[ -n "$SYS_PY" ] || die "找不到 python3。装一个：brew install python@3.12"
PY_VER="$("$SYS_PY" -c 'import sys; print("%d.%d" % sys.version_info[:2])')"
"$SYS_PY" -c 'import sys; sys.exit(0 if sys.version_info >= (3, 11) else 1)' \
  || die "需要 Python 3.11+，当前 $PY_VER"
ok "macOS $(sw_vers -productVersion) / Python $PY_VER"

# ── 2. 虚拟环境 ────────────────────────────────────────────────
# 不复用旧 venv：venv 里的脚本把创建时的绝对路径写死在 shebang 里，
# 项目目录一改名，venv/bin/pip 就报 bad interpreter。重建最省事。
say "2/6 准备虚拟环境"
if [ -x "$PY" ] && "$PY" -c 'pass' 2>/dev/null; then
  ok "沿用已有 venv"
else
  [ -e "$VENV" ] && { warn "已有 venv 不可用（多半是目录改过名），重建"; rm -rf "$VENV"; }
  "$SYS_PY" -m venv "$VENV"
  ok "venv 已创建"
fi

# ── 3. 依赖 ────────────────────────────────────────────────────
say "3/6 安装依赖"
"$PY" -m pip install --quiet --upgrade pip
"$PY" -m pip install --quiet -r "$DIR/requirements.txt"
ok "依赖就绪"

# ── 4. 凭证 ────────────────────────────────────────────────────
# 采集本身不需要 key，只有每小时的分析需要。所以这里可以跳过。
say "4/6 配置 API key"
if [ -f "$ENV_FILE" ] && grep -qE '^ZHIPU(AI)?_API_KEY=.+' "$ENV_FILE"; then
  ok ".env 已有 key"
else
  KEY="${ZHIPU_API_KEY:-${ZHIPUAI_API_KEY:-}}"
  if [ -n "$KEY" ]; then
    warn "从当前 shell 环境读到 key，写入 .env"
  elif [ -t 0 ]; then
    echo "  智谱 API key（留空跳过，之后可自己写进 .env）："
    read -rsp "  > " KEY; echo
  fi
  if [ -n "$KEY" ]; then
    umask 077
    printf 'ZHIPU_API_KEY=%s\n' "$KEY" > "$ENV_FILE"
    chmod 600 "$ENV_FILE"
    ok ".env 已写入（权限 600，已被 .gitignore）"
  else
    warn "跳过 —— 截图照常采集，每小时的 AI 分析会失败"
  fi
fi

# ── 5. 后台服务 ────────────────────────────────────────────────
# 用 launchd 不用 cron：cron 不会在开机时拉起长驻进程，
# 也不会在进程崩溃后重启它。
say "5/6 注册后台服务"
# 新系统上 `launchctl unload` 对已 bootstrap 的服务会静默失败，
# 结果是新旧两个 agent 同时采集 —— 截图翻倍、账单翻倍。bootout 才是可靠的。
# bootout 是异步的：它返回之后 launchd 可能还在拆卸，这时候立刻 load
# 会被静默丢弃，结果是脚本报告「已启动」但服务根本没起来。
# 所以卸载之后必须等 label 真的从 launchctl list 里消失。
unload_agent() {
  local label="$1" plist="$2"
  launchctl bootout "gui/$(id -u)/$label" 2>/dev/null \
    || launchctl unload -w "$plist" 2>/dev/null || true
  for _ in $(seq 1 20); do
    launchctl list 2>/dev/null | grep -q "$label" || return 0
    sleep 0.25
  done
  warn "$label 卸载超时，继续尝试"
}

for L in "${LEGACY_LABELS[@]}"; do
  LP="$HOME/Library/LaunchAgents/$L.plist"
  if launchctl list 2>/dev/null | grep -q "$L"; then
    unload_agent "$L" "$LP"
    warn "已停止旧服务 $L"
  fi
  # 早期版本把 API key 明文写在 plist 里，留着迟早会被误提交
  if [ -f "$LP" ] && grep -q "API_KEY" "$LP"; then
    mv "$LP" "$LP.bak"
    warn "旧 plist 含明文密钥，已改名为 $(basename "$LP").bak（确认无误后自行删除）"
  fi
done
unload_agent "$LABEL" "$PLIST"
mkdir -p "$HOME/Library/LaunchAgents"
cat > "$PLIST" <<PLISTEOF
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
  <key>Label</key><string>$LABEL</string>
  <key>ProgramArguments</key>
  <array>
    <string>$PY</string>
    <string>$DIR/capture.py</string>
  </array>
  <key>WorkingDirectory</key><string>$DIR</string>
  <!-- 不带凭证：capture.py 自己读 .env。
       PYTHONUNBUFFERED 是必须的 —— 非 tty 下 Python 会块缓冲 stdout，
       日志会一直是空的，看起来像没启动。 -->
  <key>EnvironmentVariables</key>
  <dict><key>PYTHONUNBUFFERED</key><string>1</string></dict>
  <key>RunAtLoad</key><true/>
  <key>KeepAlive</key><true/>
  <key>StandardOutPath</key><string>$DIR/capture.log</string>
  <key>StandardErrorPath</key><string>$DIR/capture.log</string>
</dict>
</plist>
PLISTEOF
launchctl load "$PLIST"
for _ in $(seq 1 20); do
  launchctl list 2>/dev/null | grep -q "$LABEL" && break
  sleep 0.25
done
if launchctl list 2>/dev/null | grep -q "$LABEL"; then
  ok "服务已注册并启动（开机自启 + 崩溃自动重拉）"
else
  warn "服务注册了但没能启动，看日志: tail -50 $DIR/capture.log"
fi

# ── 5.5 数据目录权限 ───────────────────────────────────────────
# 截图和屏幕文本是这台机器上最敏感的文件之一，默认 755/644 意味着
# 同机器的其他用户能直接读。
for d in screenshots reports chroma_db; do
  [ -e "$DIR/$d" ] && chmod -R go-rwx "$DIR/$d" 2>/dev/null || true
done
[ -f "$DIR/redact.local.yaml" ] && chmod 600 "$DIR/redact.local.yaml" || true
[ -f "$DIR/capture.log" ] && chmod 600 "$DIR/capture.log" || true

# ── 6. 权限 ────────────────────────────────────────────────────
# macOS 的权限授予对象是「可执行文件」，不是项目目录。
# 刚重建过 venv 的话，之前授过的权限不算数，要对新的 python 重新授权。
say "6/6 检查权限"
echo "  需要授权的可执行文件（下一步要用到这个路径）："
echo "    $PY"
echo
"$PY" "$DIR/doctor.py" || true

cat <<TIPEOF

$(printf '\033[1m接下来\033[0m')

  权限没通过的话，两处都要加上面那个 python 路径：
    系统设置 → 隐私与安全性 → 屏幕录制
    系统设置 → 隐私与安全性 → 辅助功能
  加完重启服务：  bash $DIR/start.sh

  隐私（强烈建议装完就做）：
    cp $DIR/redact.example.yaml $DIR/redact.local.yaml
    $PY $DIR/redact.py          # 从你自己的数据里找出人名候选
    然后把确认是人名的填进 redact.local.yaml —— 它们不会被发给模型。
    随时暂停：bash $DIR/pause.sh 30

  接进 Claude Code（可选，装完能直接问「我上周三在干什么」）：
    claude mcp add gleaner -- $PY $DIR/mcp_server.py

  常用命令：
    自检    $PY $DIR/doctor.py
    状态    bash $DIR/status.sh
    停止    bash $DIR/stop.sh
    日志    tail -f $DIR/capture.log
TIPEOF
