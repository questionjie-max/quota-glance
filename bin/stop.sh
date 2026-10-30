#!/bin/bash
PORT="${PORT:-8787}"
if lsof -nP -iTCP:$PORT -sTCP:LISTEN >/dev/null 2>&1; then
  pkill -f "quota_glance serve"
  sleep 1
  echo "已停止"
else
  echo "本来就没在跑"
fi
