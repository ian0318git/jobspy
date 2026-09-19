#!/usr/bin/env bash
# ═══════════════════════════════════════════════════════════════════════════════
# 🔍 Embedded Job Board — 啟動腳本（systemd 薄殼）
#
#   2026-09-19 起，看板由 systemd user service 常駐，這支腳本只是 restart 的別名。
#
#   為什麼【刻意不提供】nohup fallback：
#     1. `nohup python job_board.py > job_board.log` 會用 O_TRUNC 打開同一個檔案，
#        而 systemd 正以 append: 持有它 —— 等於每次手動啟動就清空一次日誌。
#     2. 舊版會另外起一個不受 systemd 管的行程去搶 5000 埠，與 Restart=always
#        形成「每 5 秒 EADDRINUSE 重啟一次」的 flap。
#   這兩件事都不會有錯誤訊息，只會讓服務莫名其妙不穩，所以不留後路。
# ═══════════════════════════════════════════════════════════════════════════════
set -u

SERVICE="jobboard.service"

echo "============================================"
echo "🔍 Embedded Job Board - 重啟中（systemd）"
echo "============================================"

systemctl --user restart "$SERVICE" || {
    echo "[失敗] restart 失敗，完整狀態如下："
    systemctl --user status "$SERVICE" --no-pager
    exit 1
}

sleep 1
systemctl --user status "$SERVICE" --no-pager | head -6

echo ""
echo "    URL  : http://127.0.0.1:5000   （僅 loopback，遠端請用 ssh -L 5000:127.0.0.1:5000）"
echo "    日誌 : tail -f job_board.log"
echo "    停止 : ./stop.sh"
echo "    排程 : 由 jobscan.timer 負責，systemctl --user list-timers jobscan.timer"
echo ""
echo "    常用指令:"
echo "    • 手動掃描 : ./run_search.sh"
echo "    • 服務狀態 : systemctl --user status $SERVICE"
