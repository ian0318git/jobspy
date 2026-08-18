#!/usr/bin/env bash
# ════════════════════════════════════════════════
# 🔍 早上 06:00 job scan 補跑腳本
#    機器凌晨睡眠/關機導致錯過 06:00 排程時，
#    在 06:00–12:00 期間每 15 分鐘檢查一次，補跑早上的 scan
# ════════════════════════════════════════════════

set -u

DIR="$(cd "$(dirname "$0")" && pwd)"
cd "$DIR" || exit 1

TODAY="$(date +%Y%m%d)"
LOG="$DIR/logs/cron_search.log"

log() { echo "[catchup $(date +%H:%M:%S)] $*" >> "$LOG"; }

# 1) 今天已經跑過（有結果檔）→ 不用補
if ls "search_results/kanban_jobs_${TODAY}_"*.json >/dev/null 2>&1; then
    exit 0
fi

# 2) 已有搜尋正在執行（app 內建排程 / 22:00 cron）→ 跳過，避免並發
if pgrep -f "linkedin_job_search.py" >/dev/null 2>&1; then
    exit 0
fi

# 3) flock 防並發，執行搜尋
log "檢測到早上 scan 未執行，開始補跑..."
if flock -n /tmp/jobscan_morning.lock \
    "$DIR/.venv/bin/python" "$DIR/linkedin_job_search.py" >> "$LOG" 2>&1; then
    log "補跑完成"
else
    log "補跑失敗（搜尋異常或 lock 被佔用）"
fi
