#!/usr/bin/env bash
# ════════════════════════════════════════════════
# 🔍 Embedded Job Board — 停止腳本
# ════════════════════════════════════════════════

set -e

PID=$(pgrep -f "job_board.py" 2>/dev/null | head -1)

if [ -z "$PID" ]; then
    echo "[!] job_board.py 目前沒有在執行"
    exit 0
fi

echo "正在停止 job_board.py (PID: $PID)..."
kill "$PID"
sleep 1

if kill -0 "$PID" 2>/dev/null; then
    echo "強制終止中..."
    kill -9 "$PID" 2>/dev/null
fi

echo "[OK] job_board.py 已停止"
