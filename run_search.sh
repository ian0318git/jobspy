#!/usr/bin/env bash
# ═══════════════════════════════════════════════════════════════════════════════
# 🔍 手動執行工作搜尋（爬蟲）
#
#   這是 run_scan.sh 的薄殼：實際工作全部在 run_scan.sh，包括 flock 防並發。
#   過去這支腳本自己 source venv 並直接呼叫 python，繞過了看板的防重入檢查，
#   是「手動掃描與排程掃描同時跑」的來源之一。
#
#   與 systemd timer 共用 logs/jobscan.lock —— 若 timer 的掃描正在跑，
#   這裡會直接印 SKIPPED 結束，不會並發。
# ═══════════════════════════════════════════════════════════════════════════════
set -u

DIR="$(cd "$(dirname "$0")" && pwd)"

echo "============================================"
echo "🔍 開始搜尋工作（Melbourne Embedded Jobs）"
echo "   平台: LinkedIn + Indeed + Seek + Jora"
echo "   即時輸出: logs/search_current.log"
echo "   完整逐字稿: logs/cron_search.log"
echo "============================================"

# 用 exec 而非呼叫：讓 run_scan.sh 直接取代本行程，訊號（Ctrl-C / SIGTERM）
# 才能原樣傳到爬蟲，退出碼也不會被中間多一層 shell 吃掉。
exec "$DIR/run_scan.sh" manual
