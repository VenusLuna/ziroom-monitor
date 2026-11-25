#!/usr/bin/env bash
# 生成并装载 launchd 任务：每 2 小时扫一次附近房源，有变化推飞书
set -euo pipefail
DIR="$(cd "$(dirname "$0")" && pwd)"
LABEL="com.ziroom.monitor"
PLIST="$DIR/${LABEL}.plist"

sed "s|__PROJECT_DIR__|$DIR|g" "$DIR/${LABEL}.plist.tpl" > "$PLIST"
mkdir -p "${DIR}/logs"

cp "$PLIST" "$HOME/Library/LaunchAgents/${LABEL}.plist"
launchctl bootout "gui/$(id -u)/${LABEL}" 2>/dev/null || true
launchctl bootstrap "gui/$(id -u)" "$HOME/Library/LaunchAgents/${LABEL}.plist"
echo "已装载 ${LABEL}：每 2 小时执行一次，日志在 ${DIR}/logs/"
echo "立即跑一轮： launchctl kickstart -k gui/$(id -u)/${LABEL}"
echo "停掉：       launchctl bootout gui/$(id -u)/${LABEL}"
