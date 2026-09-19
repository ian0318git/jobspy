#!/usr/bin/env bash
# ═══════════════════════════════════════════════════════════════════════════════
# run_scan.sh — 全機唯一的爬蟲入口
#
#   呼叫者：
#     systemd timer : ExecStart=.../run_scan.sh systemd-timer   (deploy/jobscan.service)
#     手動          : ./run_search.sh（內部 exec 這支）
#     dashboard     : 🔍 Re-Search 按鈕（job_board.py 以 Python fcntl.flock 取同一把鎖）
#
#   為什麼大家要共用同一把鎖：2026-08-13 發生過「兩套系統並發重複搜尋」——補跑腳本
#   與 dashboard 各產了一份結果檔。當時兩邊的防呆互不可見，因為一邊看檔案、一邊看
#   行程。flock 讓所有入口看同一件事實。
#
#   注意：flock 是 advisory lock，綁在 fd 上而非綁檔案存在與否，所以【不存在】
#   stale lock 問題 —— 行程被 SIGKILL 時 fd 由核心關閉，鎖自動釋放。不需要、也
#   不應該寫任何「清掉殘留鎖」的邏輯。
# ═══════════════════════════════════════════════════════════════════════════════
set -u   # 刻意不用 set -e：退出碼要自己判斷，中途失敗也要留下 state 紀錄

DIR="$(cd "$(dirname "$0")" && pwd)"
cd "$DIR" || exit 1

PY="$DIR/.venv/bin/python"
# 測試鉤子：JOBSCAN_SCRIPT 可指向假腳本，讓整條鏈路（鎖／state／tee／逾時）在
# 30 秒內驗證完畢，不必真的跑 19 分鐘的爬蟲。
SCRIPT="${JOBSCAN_SCRIPT:-$DIR/linkedin_job_search.py}"
LOCK="$DIR/logs/jobscan.lock"
LIVE="$DIR/logs/search_current.log"
STATE="$DIR/logs/search_state.json"
TRIGGER="${1:-manual}"
RUN_ID="$(date +%Y%m%d_%H%M%S)"
# 硬上界須 >= deploy/jobscan.service 的 TimeoutStartSec（5h），否則 systemd 會先開槍。
HARD_TIMEOUT="${JOBSCAN_TIMEOUT:-18000}"

mkdir -p "$DIR/logs"

[ -x "$PY" ]     || { echo "[jobscan] $RUN_ID ERROR: venv python 不存在: $PY" >&2; exit 1; }
[ -f "$SCRIPT" ] || { echo "[jobscan] $RUN_ID ERROR: 爬蟲腳本不存在: $SCRIPT" >&2; exit 1; }

# 原子寫入：先寫暫存檔再 mv（同一檔案系統上的 rename 是原子的），
# 讀者永遠只會看到完整的 JSON，不會讀到寫到一半的內容。
write_state() {   # $1=phase(running|finished)  $2=exit_code 或空字串
    local tmp="$STATE.$$" fin
    if [ "$1" = running ]; then fin=null; else fin="\"$(date +%Y-%m-%dT%H:%M:%S)\""; fi
    printf '{"run_id":"%s","trigger":"%s","phase":"%s","pid":%s,"exit_code":%s,"started_at":"%s","finished_at":%s}\n' \
        "$RUN_ID" "$TRIGGER" "$1" "$$" "${2:-null}" "$STARTED_AT" "$fin" > "$tmp" \
        && mv -f "$tmp" "$STATE"
}

# fd 9 持有鎖，shell 活著鎖就活著。python 子程序會繼承這個 fd，
# 所以即使 wrapper 被 SIGKILL，只要爬蟲還活著，鎖就不會鬆手。
exec 9>"$LOCK"
if ! flock -n 9; then
    # 刻意不覆寫 search_state.json —— 裡面是【正在跑的那一輪】的狀態，蓋掉會讓看板錯亂。
    echo "[jobscan] $RUN_ID SKIPPED: 另一個掃描正在執行（lock=$LOCK）"
    exit 0        # 不是失敗：資料最多舊 19 分鐘，重跑只是浪費爬取額度並增加被限速的風險
fi

STARTED_AT="$(date +%Y-%m-%dT%H:%M:%S)"
# $? 以參數傳入而非在函式內讀取：local 會改寫 $?，在函式內讀會拿到錯誤的值。
finish() {
    echo "[jobscan] $RUN_ID END exit=$1"
    write_state finished "$1"
}
trap 'finish $?' EXIT
# 訊號語意（2026-09-19 實測）：
#   對【整個 process group】送 TERM（systemd KillMode=control-group、終端機 Ctrl-C）
#     → 立即中止，state 記 exit_code=143。這是實際會走到的路徑。
#   對【wrapper 單一 PID】送 TERM
#     → bash 會延後執行 trap 直到前景管線結束，也就是要等掃描自己跑完才收尾。
#       掃描不會被中斷，但結束後 state 仍會記 143。這是刻意不修的：要走這條路只能
#       手動 kill 那個 PID，而 systemd 與 Ctrl-C 都不會這樣送訊號，為了它改用
#       background + wait 會讓退出碼的取得變複雜，得不償失。
trap 'exit 143' TERM INT

echo "[jobscan] $RUN_ID START trigger=$TRIGGER pid=$$ timeout=${HARD_TIMEOUT}s"
write_state running ""

: > "$LIVE"                   # 截斷：看板的 follower 從 offset 0 開始跟讀

set -o pipefail
# -u：不緩衝，行即時到達 LIVE 與 systemd 的 append 日誌
# tee 的 stdout 由 systemd append 到 logs/cron_search.log（完整逐字稿）
timeout --signal=TERM --kill-after=60s "$HARD_TIMEOUT" \
    "$PY" -u "$SCRIPT" 2>&1 | tee "$LIVE"
exit "${PIPESTATUS[0]}"
