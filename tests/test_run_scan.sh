#!/usr/bin/env bash
# ═══════════════════════════════════════════════════════════════════════════════
# run_scan.sh 回歸測試 —— 對應 2026-10-06 的「磁碟滿造成靜默失敗」事故
#
#   bash tests/test_run_scan.sh
#
# 退出碼 0 = 全數通過；1 = 有失敗（失敗清單印在最後）。
#
# 不需要真爬蟲：run_scan.sh 的 JOBSCAN_SCRIPT / JOBSCAN_LIVE / JOBSCAN_STATE /
# JOBSCAN_LOCK 四個鉤子（見該檔 line 24-34 的說明）讓整條【生產路徑】在幾秒內
# 跑完，而且每個案例都在自己的 mktemp 沙箱裡 —— 完全不碰生產檔案。
#
# ENOSPC 用 /dev/full 模擬（寫入必定回 ENOSPC），不必真的塞滿磁碟、也不必 root。
#
# 涵蓋 21 項。其中【11 項在修正前的版本上會失敗】（2026-10-06 實測，把 HEAD 版
# 的 run_scan.sh 抓回來跑即得，失敗清單見 DECISIONS.md）；其餘 10 項是兩個版本
# 都必須成立的不變式（正常路徑、退出碼傳遞、不留屍體），用途是防止未來改壞，
# 不具備區辨缺陷版本的能力 —— 兩者請分開看待。
#
# ⚠️ 覆蓋限制（誠實揭露）：
#   1. 本套件以黑箱方式驅動 run_scan.sh，【沒有】納入 tests/mutate.py 的變異
#      測試 —— 該工具目前只認 Python 目標。上面那 11 項的「區辨力」是用
#      HEAD 版對照實測出來的，不是用變異測試驗出來的。
#   2. 10-05 事故真正的現場是 ENOSPC：open(O_CREAT) 【成功建立】了 0 byte 的
#      search_state.json.3730474，之後寫入才失敗。要重現「檔案已建立、寫入才
#      失敗」需要一個真的滿了的檔案系統（mount 小 tmpfs 要 root），所以本套件
#      用唯讀目錄代替 —— 那條路徑下 printf 的 open 就先失敗，根本不會產生屍體，
#      兩版皆通過。真正的 ENOSPC 屍體路徑【未被自動化測試覆蓋】，只靠
#      write_state() 失敗分支上的 `rm -f "$tmp"` 以建構方式保證。
# ═══════════════════════════════════════════════════════════════════════════════
set -u

DIR="$(cd "$(dirname "$0")/.." && pwd)"
RUN_SCAN="$DIR/run_scan.sh"
PY="$DIR/.venv/bin/python"

[ -f "$RUN_SCAN" ] || { echo "找不到 $RUN_SCAN" >&2; exit 1; }
[ -x "$PY" ]       || { echo "找不到 venv python: $PY" >&2; exit 1; }

TMPROOT="$(mktemp -d)"
cleanup() {
    chmod -R u+rwX "$TMPROOT" 2>/dev/null
    rm -rf "$TMPROOT"
}
trap cleanup EXIT

PASS=0
FAILURES=()

# check <描述> <0=通過> [補充]
check() {
    if [ "$2" -eq 0 ]; then
        printf '  ✅ %s\n' "$1"
        PASS=$((PASS + 1))
    else
        printf '  ❌ %s    %s\n' "$1" "${3:-}"
        FAILURES+=("$1 — ${3:-}")
    fi
}

# 讀 state JSON 的某個欄位。刻意不呼叫 python 解析 —— 被測物就是「檔案有沒有
# 寫成」，用最笨的字串比對反而少一層可能自己出錯的東西。
state_field() {   # $1=檔案 $2=欄位
    [ -f "$1" ] || { printf '<無檔案>'; return; }
    sed -n "s/.*\"$2\":\([^,}]*\).*/\1/p" "$1" | tr -d '"'
}

new_case() {   # $1=名稱 → 設 CASE
    CASE="$TMPROOT/$1"
    mkdir -p "$CASE/logs" "$CASE/state"
}

# 產生假爬蟲。$1=沙箱 $2=退出碼 $3=額外 python 敘述（預設 pass）
make_fake() {
    local dir="$1" rc="$2" extra="${3:-pass}"
    cat > "$dir/fake_scan.py" <<PYEOF
import sys
print("假爬蟲：輸出第一行", flush=True)
print("假爬蟲：輸出第二行", flush=True)
open(r"$dir/scan_ran", "w").write("ran")   # 「我真的跑了」的記號
$extra
sys.exit($rc)
PYEOF
}

# 跑 run_scan.sh；呼叫前先設好 LIVE / STATE。結果放 CASE_RC。
run_scan_case() {
    env JOBSCAN_SCRIPT="$CASE/fake_scan.py" \
        JOBSCAN_LOCK="$CASE/logs/lock" \
        JOBSCAN_LIVE="$LIVE" \
        JOBSCAN_STATE="$STATE" \
        JOBSCAN_TIMEOUT=60 \
        JOBSCAN_LOCK_WAIT=0 \
        bash "$RUN_SCAN" test-harness >"$CASE/out.txt" 2>&1
    CASE_RC=$?
}

did_run() {   # 0 = 爬蟲跑過
    [ -e "$CASE/scan_ran" ] && echo 1 || echo 0
}
# 注意：did_run 回傳 1 代表【跑過】，所以「不該跑」的斷言要把結果反過來。

echo "═══ run_scan.sh 回歸測試（2026-10-06 磁碟滿靜默失敗）═══"

# ── ① 正常路徑：修正不得把正常情況也擋掉 ───────────────────────────────────
echo "① 正常路徑"
new_case normal
LIVE="$CASE/logs/search_current.log"
STATE="$CASE/state/search_state.json"
make_fake "$CASE" 0
run_scan_case
check "退出碼 0" "$([ "$CASE_RC" -eq 0 ] && echo 0 || echo 1)" "rc=$CASE_RC"
check "state = finished / exit_code=0" \
      "$([ "$(state_field "$STATE" phase)" = finished ] && [ "$(state_field "$STATE" exit_code)" = 0 ] && echo 0 || echo 1)" \
      "phase=$(state_field "$STATE" phase) exit_code=$(state_field "$STATE" exit_code)"
check "live log 有內容（看板跟讀得到）" \
      "$([ -s "$LIVE" ] && echo 0 || echo 1)" "size=$(stat -c%s "$LIVE" 2>/dev/null)"
check "逐字稿留下 END 行" \
      "$(grep -q 'END exit=0' "$CASE/out.txt" && echo 0 || echo 1)"

# ── ② 爬蟲自己失敗：退出碼必須傳出去 ───────────────────────────────────────
echo "② 爬蟲失敗（exit 3）"
new_case scan_fail
LIVE="$CASE/logs/search_current.log"
STATE="$CASE/state/search_state.json"
make_fake "$CASE" 3
run_scan_case
check "退出碼 3（爬蟲的失敗優先）" "$([ "$CASE_RC" -eq 3 ] && echo 0 || echo 1)" "rc=$CASE_RC"
check "state 記 exit_code=3（看板看得到）" \
      "$([ "$(state_field "$STATE" exit_code)" = 3 ] && echo 0 || echo 1)" \
      "exit_code=$(state_field "$STATE" exit_code)"

# ── ③ 開跑時 state 寫不進去 → 必須【放棄本輪】──────────────────────────────
# 這一項就是 10-05 22:00 那一輪的死因：state 沒寫成，看板不知道它在跑，
# 而看不見的掃描正是凍結 last_read_at、進而讓 watchdog 六小時後誤殺的起點。
echo "③ state 寫不進去（唯讀目錄）"
new_case state_unwritable
mkdir -p "$CASE/ro_state"
chmod 500 "$CASE/ro_state"
LIVE="$CASE/logs/search_current.log"
STATE="$CASE/ro_state/search_state.json"
make_fake "$CASE" 0
run_scan_case
chmod 700 "$CASE/ro_state"
check "退出碼非 0（不得靜默繼續）" "$([ "$CASE_RC" -ne 0 ] && echo 0 || echo 1)" "rc=$CASE_RC"
check "【爬蟲根本沒跑】—— 不產生看板看不見的掃描" \
      "$([ "$(did_run)" -eq 0 ] && echo 0 || echo 1)" "scan_ran 存在=不該跑"
check "留下明確的 ERROR 訊息" \
      "$(grep -q '無法寫入 state' "$CASE/out.txt" && echo 0 || echo 1)" \
      "$(grep -m1 ERROR "$CASE/out.txt" 2>/dev/null)"
check "沒有留下 state 暫存檔屍體（不變式；唯讀目錄下兩版皆過，見檔頭限制 2）" \
      "$(ls "$CASE/ro_state"/search_state.json.* >/dev/null 2>&1 && echo 1 || echo 0)"

# ── ④ 開跑時 live log 截不斷 → 同樣必須放棄本輪 ─────────────────────────────
echo "④ live log 截斷不了（唯讀目錄 + 檔案不存在）"
new_case live_unwritable
mkdir -p "$CASE/ro_live"
chmod 500 "$CASE/ro_live"
LIVE="$CASE/ro_live/search_current.log"
STATE="$CASE/state/search_state.json"
make_fake "$CASE" 0
run_scan_case
chmod 700 "$CASE/ro_live"
check "退出碼非 0" "$([ "$CASE_RC" -ne 0 ] && echo 0 || echo 1)" "rc=$CASE_RC"
check "【爬蟲根本沒跑】（跟讀基準不成立就不要掃）" \
      "$([ "$(did_run)" -eq 0 ] && echo 0 || echo 1)" "scan_ran 存在=不該跑"
check "留下明確的 ERROR 訊息" \
      "$(grep -q '無法截斷 live log' "$CASE/out.txt" && echo 0 || echo 1)" \
      "$(grep -m1 ERROR "$CASE/out.txt" 2>/dev/null)"

# ── ⑤ 本案主角：tee 因 ENOSPC 失敗 ─────────────────────────────────────────
# 舊版只看 PIPESTATUS[0]（爬蟲回 0）就 exit 0 → systemd 記 Finished，
# 而 search_current.log 整輪是空的 → 看板讀不到 → last_read_at 不更新。
echo "⑤ tee 寫入失敗（/dev/full 模擬 ENOSPC）"
new_case tee_enospc
LIVE=/dev/full
STATE="$CASE/state/search_state.json"
make_fake "$CASE" 0
run_scan_case
check "退出碼【非 0】—— 舊版這裡會是 0（本項即回歸鎖）" \
      "$([ "$CASE_RC" -ne 0 ] && echo 0 || echo 1)" "rc=$CASE_RC"
check "訊息明確指出是 tee 的錯" \
      "$(grep -q 'tee 寫入失敗' "$CASE/out.txt" && echo 0 || echo 1)" \
      "$(grep -m1 'tee 寫入失敗' "$CASE/out.txt" 2>/dev/null)"
check "爬蟲本身回 0，仍必須因 tee 而失敗（爬蟲成功≠這一輪可用）" \
      "$([ "$(did_run)" -eq 1 ] && echo 0 || echo 1)" "scan_ran 不存在=假爬蟲沒跑，測試無效"
check "state 的 exit_code 反映 tee 的失敗，不是 0" \
      "$([ "$(state_field "$STATE" exit_code)" != 0 ] && [ "$(state_field "$STATE" phase)" = finished ] && echo 0 || echo 1)" \
      "phase=$(state_field "$STATE" phase) exit_code=$(state_field "$STATE" exit_code)"

# ── ⑥ 開跑時寫得進、收尾時寫不進 ───────────────────────────────────────────
# EXIT trap 的退出碼【不會】自動變成腳本的退出碼（2026-10-06 實測：
# body `exit 7` + trap 最後一道指令 `false` → 腳本仍是 7）。所以 finish()
# 裡必須【明示 exit】。少了那一行，state 寫入失敗會被 systemd 記成 Finished。
echo "⑥ 收尾時 state 寫入失敗（開跑後才把目錄鎖住）"
new_case finish_state_fail
LIVE="$CASE/logs/search_current.log"
STATE="$CASE/state/search_state.json"
make_fake "$CASE" 0 "import os; os.chmod(r\"$CASE/state\", 0o500)"
run_scan_case
chmod 700 "$CASE/state"
check "退出碼非 0（EXIT trap 不會自動改退出碼 —— 這項就是那個坑的鎖）" \
      "$([ "$CASE_RC" -ne 0 ] && echo 0 || echo 1)" "rc=$CASE_RC"
check "留下明確的 ERROR 訊息" \
      "$(grep -q '收尾時無法寫入 state' "$CASE/out.txt" && echo 0 || echo 1)" \
      "$(grep -m1 ERROR "$CASE/out.txt" 2>/dev/null)"
check "state 停在 running —— 檔案本身說不出真相（不變式；兩版皆然，這正是非零退出不可的理由）" \
      "$([ "$(state_field "$STATE" phase)" = running ] && echo 0 || echo 1)" \
      "phase=$(state_field "$STATE" phase)"

# ── ⑦ 全體：不得留下任何 state 暫存檔屍體 ──────────────────────────────────
# 10-05 22:00 那輪留下的 logs/search_state.json.3730474 就是這種屍體，
# 它在事故後仍留在磁碟上，還差點被誤讀成「那一輪有寫 state」。
echo "⑦ 跨案例：state 暫存檔屍體"
_leftover="$(find "$TMPROOT" -name 'search_state.json.*' 2>/dev/null | head -5 | tr '\n' ' ')"
check "所有案例都沒有殘留 state 暫存檔（不變式；防止未來改壞）" \
      "$([ -z "$_leftover" ] && echo 0 || echo 1)" "殘留：$_leftover"

# ═══ 結果 ════════════════════════════════════════════════════════════════════
echo
if [ "${#FAILURES[@]}" -ne 0 ]; then
    echo "❌ ${#FAILURES[@]} 項失敗："
    for f in "${FAILURES[@]}"; do
        echo "   - $f"
    done
    exit 1
fi
echo "✅ 全數通過（$PASS 項）"
exit 0
