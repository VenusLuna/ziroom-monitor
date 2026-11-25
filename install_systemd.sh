#!/usr/bin/env bash
# Linux（systemd）安装：每 2 小时跑一轮，用户级 timer，不需要 sudo
set -euo pipefail
DIR="$(cd "$(dirname "$0")" && pwd)"
LABEL="ziroom-monitor"
UNIT_DIR="${XDG_CONFIG_HOME:-$HOME/.config}/systemd/user"
PY="$DIR/.venv/bin/python"

[ -x "$PY" ] || { echo "缺少 $PY，先跑：python3 -m venv .venv && .venv/bin/pip install -r requirements.txt"; exit 1; }
mkdir -p "$DIR/logs" "$UNIT_DIR"

cat > "$UNIT_DIR/${LABEL}.service" <<UNIT
[Unit]
Description=自如房源监控

[Service]
Type=oneshot
WorkingDirectory=${DIR}
ExecStart=${PY} ${DIR}/monitor.py once --near --max-price 3700 --db ${DIR}/near.db \\
    --notify-kinds new,gone,price,status --misses 2 --sheet --images --image-limit 60 --workers 16
StandardOutput=append:${DIR}/logs/monitor.log
StandardError=append:${DIR}/logs/monitor.err
UNIT

cat > "$UNIT_DIR/${LABEL}.timer" <<UNIT
[Unit]
Description=自如房源监控（每 2 小时）

[Timer]
OnBootSec=1min
OnUnitActiveSec=2h
AccuracySec=30s

[Install]
WantedBy=timers.target
UNIT

systemctl --user daemon-reload
systemctl --user enable --now "${LABEL}.timer"
systemctl --user list-timers "${LABEL}.timer" --no-pager || true

echo
echo "已启用 ${LABEL}.timer（每 2 小时；重启后要自动跑需 loginctl enable-linger $USER）"
echo "立即跑一轮： systemctl --user start ${LABEL}.service"
echo "看日志：     tail -f ${DIR}/logs/monitor.log"
echo "停掉：       systemctl --user disable --now ${LABEL}.timer"
