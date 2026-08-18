#!/usr/bin/env bash
# ════════════════════════════════════════════════
# 🔍 Embedded Job Board — 一鍵啟動腳本
# ════════════════════════════════════════════════

set -e

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
cd "$SCRIPT_DIR"

echo "============================================"
echo "🔍 Embedded Job Board - 啟動中..."
echo "============================================"

# 1️⃣ 啟用虛擬環境
if [ -d ".venv" ]; then
    source .venv/bin/activate
    echo "[OK] 虛擬環境已啟用 (.venv)"
else
    echo "[!] 找不到 .venv，正在建立..."
    python3 -m venv .venv
    source .venv/bin/activate
    pip install --quiet pandas python-jobspy flask
    echo "[OK] 虛擬環境已建立並安裝依賴"
fi

# 2️⃣ 檢查是否已在執行
if pgrep -f "job_board.py" > /dev/null 2>&1; then
    echo "[!] job_board.py 已在執行中"
    echo ""
    echo "    PID      : $(pgrep -f job_board.py | head -1)"
    echo "    Log      : job_board.log (tail -f job_board.log)"
    echo "    URL      : http://192.168.44.128:5000"
    echo ""
    echo "    使用停止腳本先停止再重啟: ./stop.sh"
    exit 1
fi

# 3️⃣ 啟動 Flask 伺服器（背景執行）
nohup python job_board.py > job_board.log 2>&1 &
PID=$!

# 等待啟動完成
sleep 2

if kill -0 "$PID" 2>/dev/null; then
    echo "[OK] job_board.py 已啟動 (PID: $PID)"
    echo ""
    echo "    ┌──────────────────────────────────────────┐"
    echo "    │  📋  Kanban Board                        │"
    echo "    │  URL : http://192.168.44.128:5000        │"
    echo "    │  Log : tail -f job_board.log             │"
    echo "    │  Stop:  ./stop.sh                        │"
    echo "    └──────────────────────────────────────────┘"
    echo ""
    echo "    常用指令:"
    echo "    • 看即時日誌 : tail -f job_board.log"
    echo "    • 手動搜尋   : source .venv/bin/activate && python linkedin_job_search.py"
    echo "    • 停止服務   : pkill -f job_board.py"
else
    echo "[失敗] 服務啟動失敗！請檢查 job_board.log"
    cat job_board.log
    exit 1
fi
