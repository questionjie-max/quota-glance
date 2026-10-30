#!/bin/bash
# 静默启动看板；已在跑就什么都不做。caffeinate 保证跑着期间 Mac 不睡。
DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
PORT="${PORT:-8787}"
LOG=/tmp/quota-glance.log
IP=$(ipconfig getifaddr en0 2>/dev/null || ipconfig getifaddr en1 2>/dev/null || echo "本机IP")

if lsof -nP -iTCP:$PORT -sTCP:LISTEN >/dev/null 2>&1; then
  echo "已在运行　访问 $IP:$PORT"
  exit 0
fi

cd "$DIR" || exit 1
nohup caffeinate -i python3 -m quota_glance serve --port "$PORT" --interval 180 >> "$LOG" 2>&1 &
sleep 3
if lsof -nP -iTCP:$PORT -sTCP:LISTEN >/dev/null 2>&1; then
  echo "已启动　访问 $IP:$PORT"
else
  echo "启动失败，看日志 $LOG"
fi
