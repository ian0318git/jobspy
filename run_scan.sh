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
# LOCK / LIVE / STATE 同樣可用環境變數覆寫。理由不是彈性，是【測試隔離】：
# JOBSCAN_SCRIPT 鉤子跑的是同一條生產路徑，所以測一次就覆寫一次生產的
# search_state.json 與 search_current.log。2026-09-19 審查兩次踩到這件事
# （MINOR-12 那個過期的 locktest state 就是這樣來的，當時只是手工還原），
# 產生髒 state 的機制本身還在。預設值完全不變，只有測試會覆寫。
LOCK="${JOBSCAN_LOCK:-$DIR/logs/jobscan.lock}"
LIVE="${JOBSCAN_LIVE:-$DIR/logs/search_current.log}"
STATE="${JOBSCAN_STATE:-$DIR/logs/search_state.json}"
TRIGGER="${1:-manual}"
RUN_ID="$(date +%Y%m%d_%H%M%S)"
# 硬上界須 >= deploy/jobscan.service 的 TimeoutStartSec（5h），否則 systemd 會先開槍。
HARD_TIMEOUT="${JOBSCAN_TIMEOUT:-18000}"

# 每行都帶時戳。沒有時戳就無從對齊「START」與「END」，也無法事後回答
# 「掃描結束後看板隔多久才換檔」—— 2026-09-19 審查指出原本的日誌格式讓
# 規劃階段的「≤15 秒換檔」判準根本不可量測。RUN_ID 只有啟動當下的秒級時間，
# 同一個 run 的所有行長得一模一樣。
log()    { echo "[jobscan] $(date +%H:%M:%S) $*"; }
logerr() { echo "[jobscan] $(date +%H:%M:%S) $*" >&2; }

mkdir -p "$DIR/logs"

[ -x "$PY" ]     || { logerr "$RUN_ID ERROR: venv python 不存在: $PY"; exit 1; }
[ -f "$SCRIPT" ] || { logerr "$RUN_ID ERROR: 爬蟲腳本不存在: $SCRIPT"; exit 1; }

# 環境錯誤必須與「鎖被別人持有」分開。修正前，只要 flock 指令不在 PATH（精簡環境）
# 或 fd 9 沒開成功，`flock -n 9` 就回非零 → 被當成「別人正在跑」→ 印出
# 「SKIPPED: 另一個掃描正在執行」這句**不實**的訊息 → exit 0，而且每一輪都重複。
# 使用者會去查一個根本不存在的並發掃描。這裡讓它大聲失敗。
command -v flock >/dev/null 2>&1 || { logerr "$RUN_ID ERROR: 找不到 flock 指令，無法保證掃描互斥"; exit 1; }

# 原子寫入：先寫暫存檔再 mv（同一檔案系統上的 rename 是原子的），
# 讀者永遠只會看到完整的 JSON，不會讀到寫到一半的內容。
#
# 【回傳值有意義】0 = 確實落到 STATE 上。呼叫端【必須】檢查。
# 2026-10-06 事故（10-05 22:00 那一輪，run_id=20261005_220014）：
#   磁碟滿 → line 63 的 printf 回非零 → `&&` 短路 → mv 不執行 → 呼叫端沒檢查
#   → 整輪掃描在 state 檔裡【不存在】。看板看不到這一輪，而現場留下一個 0 byte 的
#   logs/search_state.json.3730474（那個 pid 就是那一輪的 wrapper，屍體至今還在）。
# 沒有錯誤處理的原子寫入只是「寫入」，不是「原子寫入」。
write_state() {   # $1=phase(running|finished)  $2=exit_code 或空字串
    local tmp="$STATE.$$" fin
    if [ "$1" = running ]; then fin=null; else fin="\"$(date +%Y-%m-%dT%H:%M:%S)\""; fi
    if ! printf '{"run_id":"%s","trigger":"%s","phase":"%s","pid":%s,"exit_code":%s,"started_at":"%s","finished_at":%s}\n' \
            "$RUN_ID" "$TRIGGER" "$1" "$$" "${2:-null}" "$STARTED_AT" "$fin" > "$tmp"; then
        logerr "$RUN_ID ERROR: 寫入 state 暫存檔失敗（磁碟滿？）: $tmp"
        rm -f "$tmp"                      # 不留 0 byte 屍體，免得騙過後續排查
        return 1
    fi
    if ! mv -f "$tmp" "$STATE"; then
        logerr "$RUN_ID ERROR: 置換 state 檔失敗: $tmp -> $STATE"
        rm -f "$tmp"
        return 1
    fi
    return 0
}

# fd 9 持有鎖，shell 活著鎖就活著。python 子程序會繼承這個 fd，
# 所以即使 wrapper 被 SIGKILL，只要爬蟲還活著，鎖就不會鬆手。
exec 9>"$LOCK"

# fd 9 必須真的開著才往下走。`exec 9>file` 失敗時 bash 通常會直接結束整個腳本，
# 但這裡沒有 set -e，所以明確認證一次：少了這道，失敗會退化成一連串**不實**的
# 「另一個掃描正在執行」，把環境問題誤報成並發掃描。
[ -e /proc/self/fd/9 ] || { logerr "$RUN_ID ERROR: fd 9 未開啟（$LOCK 開啟失敗？）"; exit 1; }

# 取鎖前先重試，不是為了搶贏誰，而是為了不把【看板的探測】誤判成【有掃描在跑】。
# 看板的 _lock_held() 用 LOCK_EX|LOCK_NB 探測、隨即 LOCK_UN —— 那個窗口只有微秒級，
# 但若 timer 恰好在那一刻觸發，`flock -n` 就會失敗 → 整輪 SKIPPED → exit 0 →
# 日誌一切正常，而這個時段【整整 12 小時不會再掃描】。單次機率很小，後果卻是一整輪
# 資料的靜默缺口。真實掃描會持鎖約 19 分鐘，所以只要等幾秒就能乾淨區分兩者。
#
# 這不會削弱互斥：重試只改變「我們多有耐心」，flock 仍然只讓一個行程取得鎖。
LOCK_WAIT="${JOBSCAN_LOCK_WAIT:-20}"
# LOCK_WAIT 必須是非負整數。`[ "$_waited" -ge "$LOCK_WAIT" ]` 對非整數回傳 2
# （錯誤，不是「成立」），而這裡沒有 set -e，所以下面的 break 永遠不會執行 ——
# 迴圈永不結束：每小時 3600 行 "integer expression expected" 灌進 cron_search.log，
# 而 SKIPPED 那句永遠印不出來，只能等 systemd 在 5 小時後開槍。這正是本案要根除的
# 「日誌看起來正常、實際靜默停擺」，所以壞輸入退回預設值。
if ! [[ "$LOCK_WAIT" =~ ^[0-9]+$ ]]; then
    logerr "$RUN_ID WARN: JOBSCAN_LOCK_WAIT='$LOCK_WAIT' 不是非負整數，改用預設值 20"
    LOCK_WAIT=20
elif [ "$LOCK_WAIT" -gt "$HARD_TIMEOUT" ]; then
    # 等待超過自己的硬逾時沒有意義 —— systemd 會先開槍，SKIPPED 永遠印不出來，
    # 使用者只會看到一個 failed 的 unit 而不知道原因是「另一個掃描在跑」。夾到硬逾時，
    # 保證我們一定比 systemd 早收工，且一定會留下 SKIPPED 的痕跡。
    logerr "$RUN_ID WARN: JOBSCAN_LOCK_WAIT=${LOCK_WAIT}s 超過硬逾時 ${HARD_TIMEOUT}s，夾至 ${HARD_TIMEOUT}s"
    LOCK_WAIT="$HARD_TIMEOUT"
fi
LOCK_ACQUIRED=0
_waited=0
while :; do
    if flock -n 9; then LOCK_ACQUIRED=1; break; fi
    [ "$_waited" -ge "$LOCK_WAIT" ] && break
    sleep 1
    _waited=$((_waited + 1))
done

if [ "$LOCK_ACQUIRED" -ne 1 ]; then
    # 刻意不覆寫 search_state.json —— 裡面是【正在跑的那一輪】的狀態，蓋掉會讓看板錯亂。
    log "$RUN_ID SKIPPED: 另一個掃描正在執行（等待 ${LOCK_WAIT}s 後仍未取得，lock=$LOCK）"
    exit 0        # 不是失敗：資料最多舊 19 分鐘，重跑只是浪費爬取額度並增加被限速的風險
fi
# 等超過 1 秒才拿到 = 真的撞上了探測窗口。留痕，讓這個原本不可見的競態可被觀測。
[ "$_waited" -ge 1 ] && log "$RUN_ID 取鎖重試 ${_waited}s 後成功（撞到看板的鎖探測窗口）"

STARTED_AT="$(date +%Y-%m-%dT%H:%M:%S)"
# $? 以參數傳入而非在函式內讀取：local 會改寫 $?，在函式內讀會拿到錯誤的值。
finish() {
    local rc="$1"
    log "$RUN_ID END exit=$rc"
    if ! write_state finished "$rc"; then
        logerr "$RUN_ID ERROR: 收尾時無法寫入 state（$STATE）—— 這一輪的結束狀態沒有留下紀錄"
        # 必須【明示 exit】。EXIT trap 的退出碼不會自動變成腳本的退出碼：2026-10-06
        # 實測 body `exit 7` + trap 最後一道指令 `false` → 腳本仍然是 7。少了這一行，
        # state 寫入失敗會被 systemd 記成 Finished，而那正是本案要根除的靜默失敗。
        # rc 已非零時不覆寫 —— 訊號中斷（143）比收尾失敗更該被看見。
        if [ "$rc" -eq 0 ]; then exit 1; fi
    fi
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

log "$RUN_ID START trigger=$TRIGGER pid=$$ timeout=${HARD_TIMEOUT}s"

# state 寫不進去就【不要掃】。2026-10-06 的結論：看不見的掃描是最糟的結果。
# 10-05 22:00 那一輪就是這樣：state 沒寫成 → 看板不知道它在跑 → 也讀不到它的
# live log（tee 同時因磁碟滿而失敗）→ last_read_at 凍結。而一個凍結的讀取時鐘，
# 正是 watchdog 在 06:01:09 用 SIGKILL 殺掉一個只跑了 16.4 秒的掃描的直接原因
# （systemd 自己的紀錄：06:00:53.390 Starting → 06:01:09.774 Sent signal SIGKILL；
# 見 DECISIONS.md 第十輪 MAJOR-1）。看不見的掃描不只沒資料，還會連鎖造成誤殺。
#
# 代價說明白：若只是 state 檔的權限問題（磁碟其實有空間），這一輪本來可能跑得出
# 結果，現在會被放棄。接受這個代價 —— 本檔開頭 line 52-56、72-75 已經立下同一條
# 原則：環境前置條件不成立就大聲失敗，不要留下「日誌看起來正常」的假象。
if ! write_state running ""; then
    logerr "$RUN_ID ERROR: 無法寫入 state（$STATE），放棄本輪 —— 不產生看板看不見的掃描"
    exit 1
fi

# 截斷：看板的 follower 從 offset 0 開始跟讀。與 state 同一個道理 —— 做不到就
# 不要掃。跟讀基準不成立時，看板讀到的內容會與這一輪錯位，而錯位的跟讀正是
# 上面那條「凍結的 last_read_at」的另一半。
if ! : > "$LIVE"; then
    logerr "$RUN_ID ERROR: 無法截斷 live log: $LIVE（權限？磁碟？）"
    exit 1
fi

set -o pipefail
# -u：不緩衝，行即時到達 LIVE 與 systemd 的 append 日誌
# tee 的 stdout 由 systemd append 到 logs/cron_search.log（完整逐字稿）
timeout --signal=TERM --kill-after=60s "$HARD_TIMEOUT" \
    "$PY" -u "$SCRIPT" 2>&1 | tee "$LIVE"
# PIPESTATUS 必須【一次抓完】。這裡有個實際踩過的坑：分兩道敘述寫
#     _scan_rc="${PIPESTATUS[0]}"; _tee_rc="${PIPESTATUS[1]}"
# 第一道【賦值敘述本身就會把 PIPESTATUS 重設】成只有 1 個元素，於是第二道在
# `set -u` 之下直接 "PIPESTATUS[1]: unbound variable" 中止整個腳本 ——
# 每一輪掃描都會以失敗收場（2026-10-06 實測）。改用單一敘述抓整個陣列。
#
# 2026-10-06 事故：舊版只看 PIPESTATUS[0]（爬蟲回 0）就 exit 0，於是 systemd 記
# Finished —— 而實際上 tee 在那一輪【第一次寫入就失敗】（log 第 889 行
# "tee: .../logs/search_current.log: No space left on device"），
# search_current.log 整輪是空的 → 看板的 _follow_live_log() 一個字都讀不到 →
# last_read_at 不更新 → 六小時後誤殺下一輪掃描。tee 的失敗是這條鏈的第一環，
# 卻被 exit "${PIPESTATUS[0]}" 丟掉了。
# 注意 set -o pipefail 不會改變這件事：它只影響 `$?`，不影響 PIPESTATUS 的內容
# （2026-10-06 實測）。
_rcs=("${PIPESTATUS[@]}")
_scan_rc="${_rcs[0]}"
_tee_rc="${_rcs[1]:-0}"
if [ "$_scan_rc" -ne 0 ]; then exit "$_scan_rc"; fi   # 爬蟲自己的失敗優先，那是資料缺口的根因
if [ "$_tee_rc" -ne 0 ]; then
    logerr "$RUN_ID ERROR: tee 寫入失敗（exit=$_tee_rc）—— $LIVE 不完整，看板無法跟讀"
    exit "$_tee_rc"
fi
exit 0
