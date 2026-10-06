#!/usr/bin/env bash
# 兩支界線探針的【護欄負向對照】—— 證明三條拒絕路徑真的會拒絕，而不只是寫在註解裡。
#
# 為什麼要進版控：第十六輪審查時 reviewer 說他只重現得出四個對照裡的兩個，第三道
# （`jobscan.service` active）「需要真的把生產 unit 變成 active，我不會去做」。那個
# 障礙是真的，但**可以繞過**：把探針與 job_board.py 複製到一個隔離的假樹，再放一個
# 假的 `systemctl` 到 PATH 前面，讓 `is-active jobscan.service` 回 `active`。
# 生產環境完全沒被碰到。這個檔案就是那個做法 —— 任何人一行指令就能複查。
#
# 用法：
#     bash tests/guard_controls.sh
#
# 預期輸出：A–E 版 4 個對照（1 個放行 + 3 個 rc=2）、F/G 版 3 個拒絕對照，
# 最後兩支在真環境各跑一次（rc=0）。任何一格不符就 exit 1。
set -u

REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
PY="$REPO/.venv/bin/python"
[ -x "$PY" ] || { echo "✗ 找不到 $PY"; exit 2; }

TREE="$(mktemp -d)"; trap 'rm -rf "$TREE"' EXIT
mkdir -p "$TREE/bin" "$TREE/tests" "$TREE/logs"
cp "$REPO/tests/c_boundary_probe.py" "$REPO/tests/c_boundary_probe_fg.py" "$TREE/tests/"
cp "$REPO/job_board.py" "$TREE/"

# 假 systemctl：只服務護欄那一行查詢，其餘照實回 inactive（探針在護欄之後才會
# import job_board，所以這個 stub 只需要應付護欄，不會影響任何掃描判斷）。
cat > "$TREE/bin/systemctl" <<'SH'
#!/bin/sh
case "$*" in
  *"is-active jobscan.service"*) echo active ;;
  *) echo inactive ;;
esac
SH
chmod +x "$TREE/bin/systemctl"

FAIL=0
# run <顯示名> <PATH> <探針檔> <期望 rc> <期望訊息片段（可空）>
run() {
  local name="$1" path="$2" probe="$3" want="$4" want_msg="${5:-}"
  local out rc
  out="$(PATH="$path" "$PY" "$TREE/tests/$probe" 2>&1)"; rc=$?
  if [ "$rc" != "$want" ]; then
    printf '  ✗ %-46s rc=%s（期望 %s）\n' "$name" "$rc" "$want"; FAIL=1; return
  fi
  if [ -n "$want_msg" ] && ! printf '%s' "$out" | grep -qF "$want_msg"; then
    printf '  ✗ %-46s rc 對但訊息不含「%s」\n' "$name" "$want_msg"; FAIL=1; return
  fi
  printf '  ✓ %-46s rc=%s\n' "$name" "$rc"
}

safe()   { printf '{"phase": "finished"}' > "$TREE/logs/search_state.json"
           printf '{"enabled": false}'     > "$TREE/.job_board_schedule.json"; }
runng()  { printf '{"phase": "running", "pid": 1}' > "$TREE/logs/search_state.json"; }
schedon(){ printf '{"enabled": true}' > "$TREE/.job_board_schedule.json"; }

echo "== A–E 版（tests/c_boundary_probe.py）=="
safe;     run "1/4 全部安全 → 放行"              "$PATH" c_boundary_probe.py 0
runng;    run "2/4 phase=running → 拒絕"          "$PATH" c_boundary_probe.py 2 "watcher 執行緒會把讀數汙染掉"
safe; schedon
          run "3/4 排程 enabled=true → 拒絕"      "$PATH" c_boundary_probe.py 2 "排程器可能真的去掃描"
safe;     run "4/4 jobscan.service active → 拒絕" "$TREE/bin:$PATH" c_boundary_probe.py 2 "jobscan.service 正在執行"

echo "== F/G 版（tests/c_boundary_probe_fg.py）=="
runng;    run "1/3 phase=running → 拒絕"          "$PATH" c_boundary_probe_fg.py 2 "watcher 執行緒會把讀數汙染掉"
safe; schedon
          run "2/3 排程 enabled=true → 拒絕"      "$PATH" c_boundary_probe_fg.py 2 "排程器可能真的去掃描"
safe;     run "3/3 jobscan.service active → 拒絕" "$TREE/bin:$PATH" c_boundary_probe_fg.py 2 "jobscan.service 正在執行"

echo "== 真環境（護欄全過，探針要能跑完）=="
safe
run "A–E 版 rc=0"  "$PATH" c_boundary_probe.py 0
run "F/G 版 rc=0"  "$PATH" c_boundary_probe_fg.py 0

if [ "$FAIL" = 0 ]; then echo "✅ 全數符合預期"; else echo "❌ 有對照不符預期"; fi
exit "$FAIL"
