#!/usr/bin/env python3
"""掃描鎖回歸測試 —— 對應 Senior Reviewer 2026-09-19 退回的 CRITICAL-1 / MAJOR-1 / MAJOR-2。

不需 pytest、不需網路、不碰外部服務：

    .venv/bin/python tests/test_scan_lock.py

退出碼 0 = 全數通過；1 = 有失敗（失敗清單印在最後）。

⚠️ 關於判定方式：import job_board 會在模組層啟動 scheduler / watchdog /
jobscan-watch 三個背景執行緒（既有設計，非本測試引入）。watchdog 每輪也會
呼叫 kill_stalled_search()，所以「是我們的執行緒還是 watchdog 先殺掉子行程」
不確定 —— 因此死鎖的判定刻意不依賴誰先動手，而是檢查
「呼叫之後主執行緒能否再取得 SEARCH_LOCK」：只要有任何一個執行緒持鎖卡死，
這項檢查就永遠失敗。這正是 CRITICAL-1 的可觀測特徵。

⚠️ 已知覆蓋限制（誠實揭露）：MAJOR-1 的「檢查與清空之間」窗口無法用黑箱測試
穩定重現 —— 要重現得在 Check 與 Act 之間精準插入排程切換。本測試改為驗證
可觀測的不變式（過期 owner 絕不生效、併發猛敲後狀態仍正確、無 fd 洩漏），
窗口本身是否關閉則以程式碼閱讀佐證（兩行已被 `with SEARCH_LOCK:` 包住）。
"""
import os
import subprocess
import sys
import threading
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import job_board as jb  # noqa: E402

# MINOR-9：測試【不得】佔用生產鎖 logs/jobscan.lock。缺陷版路徑會卡死並持續持有
# 全機鎖約 53 秒，若剛好撞上 timer 觸發，真實掃描就會白等 20 秒後 SKIP ——
# 也就是說測試本身能製造這次遷移要消滅的那種失敗。改指向暫存目錄的私人鎖檔。
import tempfile  # noqa: E402

jb.JOBSCAN_LOCK = os.path.join(tempfile.mkdtemp(prefix="jobscan-test-"), "jobscan.lock")
print(f"（測試鎖檔：{jb.JOBSCAN_LOCK}）")

# 安全護欄：import job_board 會在【本測試行程】內啟動背景 watchdog，而它的
# kill_stalled_external() 動手路徑會執行 `systemctl --user kill --signal=SIGKILL
# jobscan.service`。若測試途中 _EXTERNAL 恰好處於 active 且被判定逾時，這個測試
# 就會殺掉【真實的掃描】—— 例如 22:00 那一輪。
#
# 只攔截這一個函式，不動 SEARCH_STALL_TIMEOUT：kill_stalled_search() 共用同一個
# 門檻，把門檻調大會連帶讓測試 A2 永遠殺不掉子行程（已實際踩到）。
# 背景 watchdog 走的是模組全域，因此它拿到的是這個 no-op 版本；測試 C 要驗證
# 真正的護欄時，改呼叫下面保存下來的原函式。
_REAL_KILL_EXTERNAL = jb.kill_stalled_external

# ⚠️⚠️ 這個護欄【只保護背景 watchdog，不保護測試本文】。
# 2026-09-19 第四輪審查抓到：下面直接呼叫 _REAL_KILL_EXTERNAL() 的那幾處會繞過
# 護欄，其中「第二道閘門」那一段用的是生產 unit 的【即時】狀態 —— 只要當下 unit
# 是 activating（06:00／22:00 各約 19 分鐘的掃描窗口，或任何手動掃描），測試就會
# 對生產的 jobscan.service 送出真的 SIGKILL，殺掉使用者正在跑的那一輪。
# 上面那段註解聲稱的保護範圍與實際不符，已由本輪修正。
#
# 【往後新增測試的鐵律】任何直接呼叫 _REAL_KILL_EXTERNAL() 的地方都必須：
#   1. 用 mock.patch.object(jb.subprocess, "run", ...) 攔下所有 systemctl 呼叫 ——
#      systemd 分支的判斷依據是【生產 unit 的即時狀態】，這是唯一會漏出去的縫；
#   2. 優先用 trigger="manual" 走 PID 路徑，並讓 pid 指向本測試自己生的誘餌行程。
# 目前唯一的例外是 F7b：它【故意】要驗證 systemd 分支會開火，因此 subprocess.run
# 全程被 mock 掉，只檢查有沒有下達正確的指令，絕不接觸真的 unit。


def _noop_kill_external(*_a, **_k):
    """背景 watchdog 專用：這個行程永遠不對外部的掃描動手。"""
    return None


jb.kill_stalled_external = _noop_kill_external

# ═══ 跳線（tripwire）：結構性地防止「測試殺掉生產掃描」再犯 ═══════════════════
#
# 上面那個護欄只換掉 kill_stalled_external 這個【名字】，但測試本文是直接呼叫
# 保存下來的 _REAL_KILL_EXTERNAL()，繞得過去 —— 第四輪審查就是這樣抓到它真的
# 對生產 unit 送出 SIGKILL 的。只修那一行並不足以防止未來再犯：下一個人（或
# 下一輪的我）新增測試時，同樣的錯會再發生一次，而且一樣是靜默的。
#
# 所以改成從【最底層】擋：把 subprocess.run 包一層，任何內容含 "kill" 的呼叫
# 一律當場拋例外。合法的用途（F7b）本來就會把 subprocess.run mock 掉，所以
# 根本不會走到這裡 —— 也就是說這個跳線【只會在寫錯的時候響】，不需要任何
# opt-in 開關，也就沒有「忘記開」或「忘記關」的問題。
#
# 不檢查 "SIGKILL"（大寫）而檢查小寫的 "kill"：那正是 systemctl 的子命令。
_REAL_SUBPROCESS_RUN = jb.subprocess.run


def _guarded_run(cmd, *args, **kwargs):
    if isinstance(cmd, (list, tuple)) and "kill" in cmd:
        raise AssertionError(
            "🛑 測試跳線：這一行會對【生產】的 unit 下達 systemctl kill ——\n"
            f"   {list(cmd)}\n"
            "   這幾乎一定是漏了 mock.patch.object(jb.subprocess, 'run', ...)。\n"
            "   若你【故意】要驗證 systemd 分支，請照 F7b 的寫法：把 subprocess.run\n"
            "   全程 mock 掉，只斷言下達了哪些指令，絕不接觸真的 unit。"
        )
    return _REAL_SUBPROCESS_RUN(cmd, *args, **kwargs)


jb.subprocess.run = _guarded_run

FAILURES: list[str] = []


def check(name: str, ok: bool, detail: str = "") -> None:
    print(f"  [{'PASS' if ok else 'FAIL'}] {name}" + (f"  ({detail})" if detail else ""))
    if not ok:
        FAILURES.append(name)


def proc_alive(pid: int) -> bool:
    """Z 狀態（zombie）不算活著 —— os.kill(pid, 0) 對 zombie 也會成功回傳。"""
    try:
        with open(f"/proc/{pid}/stat") as f:
            return f.read().rsplit(")", 1)[1].split()[0] != "Z"
    except OSError:
        return False


# ═══ A. CRITICAL-1：kill_stalled_search() 自我死鎖 ═══════════════════════════
print("=== A. CRITICAL-1：SEARCH_LOCK 必須可重入 ===")

# 行為判準，不用 introspect：threading.RLock 是工廠函式而非類別，
# `isinstance(x, threading.RLock)` 與私有屬性探測都不可靠（實測 _count 在
# 這個 CPython 版本的 _thread.RLock 上根本不存在）。同一執行緒連取兩次，
# 可重入者兩次都成立；不可重入的 Lock 第二次會逾時回 False。
_take1 = jb.SEARCH_LOCK.acquire(timeout=2)
_take2 = jb.SEARCH_LOCK.acquire(timeout=2)
if _take2:
    jb.SEARCH_LOCK.release()
if _take1:
    jb.SEARCH_LOCK.release()
check(
    "SEARCH_LOCK 可重入（同執行緒連取兩次都成功）",
    _take1 and _take2,
    f"第一次={_take1} 第二次={_take2}",
)

nested_done = threading.Event()


def nested_call() -> None:
    """複製 kill_stalled_search() 的呼叫巢狀：持鎖 → _append_output（內部再取鎖）。"""
    with jb.SEARCH_LOCK:
        jb._append_output("回歸測試：在已持鎖的區塊內寫入一行輸出")
    nested_done.set()


threading.Thread(target=nested_call, daemon=True).start()
check("持鎖狀態下呼叫 _append_output() 不會死鎖", nested_done.wait(10))

print("=== A2. CRITICAL-1：完整 kill_stalled_search() 路徑 ===")
child = subprocess.Popen(["sleep", "600"])
jb.SEARCH_PROCESS = child
jb.SEARCH_LAST_OUTPUT_AT = time.monotonic() - 1000  # 遠超 SEARCH_STALL_TIMEOUT
jb.SEARCH_START_TIME = time.monotonic() - 1000


def do_kill() -> None:
    jb.kill_stalled_search()


killer = threading.Thread(target=do_kill, daemon=True)
killer.start()
killer.join(20)
check("kill_stalled_search() 在 20 秒內返回（未死鎖）", not killer.is_alive())

acquired = jb.SEARCH_LOCK.acquire(timeout=5)
check("呼叫後主執行緒仍能取得 SEARCH_LOCK（無執行緒持鎖卡死）", acquired)
if acquired:
    jb.SEARCH_LOCK.release()

time.sleep(1.5)
check("停滯的子行程已被終止", not proc_alive(child.pid), f"pid={child.pid}")
child.kill()
jb.SEARCH_PROCESS = None
jb.SEARCH_LAST_OUTPUT_AT = None

# ═══ B. MAJOR-1：_release_search_lock() 的 owner 檢查 ════════════════════════
print("=== B. MAJOR-1：過期 owner 不得釋放現任鎖 ===")

fd = jb._acquire_search_lock()
check("能取得掃描鎖（測試前提）", fd is not None, f"fd={fd}")

token = object()
jb.SEARCH_LOCK_OWNER = token

jb._release_search_lock(owner=object())  # 過期／不相符的 owner
check("owner 不相符時不得釋放", jb.SEARCH_LOCK_FD == fd, f"SEARCH_LOCK_FD={jb.SEARCH_LOCK_FD}")

# 併發猛敲：8 個執行緒用不相符的 owner 連續嘗試釋放。
# 修復前 fd 可能在「檢查通過之後、清空之前」被換手而誤放，這裡會觀察到洩漏。
stop = threading.Event()


def hammer() -> None:
    while not stop.is_set():
        jb._release_search_lock(owner=object())


hammers = [threading.Thread(target=hammer, daemon=True) for _ in range(8)]
for h in hammers:
    h.start()
time.sleep(1.5)
stop.set()
for h in hammers:
    h.join(5)

check(
    "8 執行緒猛敲 1.5 秒後，鎖仍屬於現任 owner 且 fd 未洩漏",
    jb.SEARCH_LOCK_FD == fd,
    f"SEARCH_LOCK_FD={jb.SEARCH_LOCK_FD}",
)

jb._release_search_lock(owner=token)
check("owner 相符時正常釋放", jb.SEARCH_LOCK_FD is None, f"SEARCH_LOCK_FD={jb.SEARCH_LOCK_FD}")

# ═══ C. MAJOR-2：看板持有鎖時 _EXTERNAL 必須被收掉 ═══════════════════════════
print("=== C. MAJOR-2：看板持鎖時不得留下凍結的 active 狀態 ===")

fd = jb._acquire_search_lock()
check("模擬：看板自己持有掃描鎖", jb._we_hold_scan_lock())

with jb.SEARCH_LOCK:
    jb._EXTERNAL["active"] = True
    jb._EXTERNAL["last_read_at"] = time.monotonic() - 1000  # 早就超過停滯門檻
    jb._EXTERNAL["trigger"] = "systemd-timer"
    jb._EXTERNAL["run_id"] = "regression-test-stale"

jb._jobscan_watch_tick()

check(
    "看板持鎖時 _jobscan_watch_tick() 必須清掉 active",
    jb._EXTERNAL["active"] is False,
    f"active={jb._EXTERNAL['active']}",
)
# 呼叫保存下來的【原】函式（jb.kill_stalled_external 已被換成 no-op 護欄）。
check("護欄條件成立：鎖確實在我們手上", jb._we_hold_scan_lock() is True)
check("kill_stalled_external() 不得對自己的掃描動手", _REAL_KILL_EXTERNAL() is None)

# 負向對照：就算 active 被外力重新設回 True（模擬防禦性檢查的處境），
# 只要鎖在我們手上，kill_stalled_external() 依然必須拒絕動手。
with jb.SEARCH_LOCK:
    jb._EXTERNAL["active"] = True
    jb._EXTERNAL["last_read_at"] = time.monotonic() - 1000
check(
    "即使 active 被重設為 True，持鎖者仍受 _we_hold_scan_lock() 保護",
    _REAL_KILL_EXTERNAL() is None,
)

with jb.SEARCH_LOCK:
    jb._EXTERNAL["active"] = False
    jb._EXTERNAL["last_read_at"] = None
jb._release_search_lock()

# ═══ D. MAJOR-3：start_search() 的 Popen 失敗不得洩漏全機掃描鎖 ══════════════
print("=== D. MAJOR-3：Popen 失敗時必須把鎖還回去 ===")

jb.SEARCH_PROCESS = None
jb._release_search_lock()
check("測試前提：目前未持有鎖", jb.SEARCH_LOCK_FD is None)

import unittest.mock as mock  # noqa: E402

with mock.patch.object(
    jb.subprocess, "Popen",
    side_effect=FileNotFoundError("[Errno 2] 模擬爬蟲檔不存在"),
):
    ok, msg = jb.start_search()
check("start_search() 正確回報失敗", ok is False, f"ok={ok} msg={msg!r}")
check(
    "失敗後不得留下鎖（fd 必須已釋放）",
    jb.SEARCH_LOCK_FD is None,
    f"SEARCH_LOCK_FD={jb.SEARCH_LOCK_FD}",
)
check("失敗後 SEARCH_PROCESS 仍為 None", jb.SEARCH_PROCESS is None)

# 最關鍵的一項：下一次必須還能取鎖。修復前這裡會永遠回 "A scan is already
# running"，而 timer 每輪都 SKIPPED、exit 0 —— 永久且靜默的全機掃描停擺。
with mock.patch.object(
    jb.subprocess, "Popen", side_effect=FileNotFoundError("再來一次"),
):
    ok2, msg2 = jb.start_search()
check(
    "第二次仍能取鎖（沒有把自己鎖死）",
    "already running" not in (msg2 or ""),
    f"ok={ok2} msg={msg2!r}",
)
check("第二次失敗後鎖同樣已釋放", jb.SEARCH_LOCK_FD is None)

# ═══ E. 孤兒鎖自癒（reap_orphan_search_lock）══════════════════════════════════
print("=== E. 孤兒鎖自癒：持有鎖卻沒有子程序 ===")

fd = jb._acquire_search_lock()
check("取得鎖（測試前提）", fd is not None, f"fd={fd}")
jb.SEARCH_LOCK_HELD_SINCE = time.monotonic() - (jb.ORPHAN_LOCK_GRACE + 10)
jb.SEARCH_PROCESS = None
reaped = jb.reap_orphan_search_lock()
check(
    "超過寬限值的孤兒鎖被回收",
    reaped == fd and jb.SEARCH_LOCK_FD is None,
    f"回傳={reaped!r} SEARCH_LOCK_FD={jb.SEARCH_LOCK_FD}",
)

fd = jb._acquire_search_lock()
# 「剛取得」本身就是未達寬限值的情況 —— 用新鮮的時間戳，不要回推，否則就是
# 在測「已達寬限值」而斷言相反的事（第一版就是這樣寫錯的）。
jb.SEARCH_LOCK_HELD_SINCE = time.monotonic()
check("未達寬限值時不得回收", jb.reap_orphan_search_lock() is None)
jb._release_search_lock()

fd = jb._acquire_search_lock()
jb.SEARCH_LOCK_HELD_SINCE = time.monotonic() - (jb.ORPHAN_LOCK_GRACE + 10)
owner_proc = subprocess.Popen(["sleep", "600"])
jb.SEARCH_PROCESS = owner_proc
check(
    "有子程序在跑時不得回收（那是有主的鎖，誤放會導致並發爬蟲）",
    jb.reap_orphan_search_lock() is None,
)
owner_proc.kill()
owner_proc.wait()
jb.SEARCH_PROCESS = None
jb._release_search_lock()
check("清理：鎖已釋放", jb.SEARCH_LOCK_FD is None)

# ═══ F. 第三輪審查退回項（2026-09-19）════════════════════════════════════════
print("=== F. 第三輪退回：孤兒鎖盲區 / 誤殺無關行程 / 幻影掃描 ===")

# F1：兩邊都不管的盲區 —— 子行程已死、但 SEARCH_PROCESS 仍指向它。
# 修正前 kill_stalled_search() 看 poll() 不為 None 而放棄、reap_orphan_search_lock()
# 看 SEARCH_PROCESS 不為 None 而放棄，於是鎖永遠不放、timer 每輪 SKIPPED + exit 0。
fd = jb._acquire_search_lock()
dead = subprocess.Popen(["true"])
dead.wait()                                     # 已結束，poll() 回 0（不是 None）
jb.SEARCH_PROCESS = dead
jb.SEARCH_LOCK_HELD_SINCE = time.monotonic() - (jb.ORPHAN_LOCK_GRACE + 10)
check(
    "子行程已死時必須回收（修正前這個盲區兩邊都不管）",
    jb.reap_orphan_search_lock() == fd and jb.SEARCH_LOCK_FD is None,
    f"SEARCH_LOCK_FD={jb.SEARCH_LOCK_FD}",
)
jb.SEARCH_PROCESS = None

# F2：對照組 —— 子程序還活著就絕不能回收（那是有主的鎖，誤放 = 兩套爬蟲並發，
# 正是 2026-08-13 事故的成因）。
fd = jb._acquire_search_lock()
alive = subprocess.Popen(["sleep", "600"])
jb.SEARCH_PROCESS = alive
jb.SEARCH_LOCK_HELD_SINCE = time.monotonic() - (jb.ORPHAN_LOCK_GRACE + 10)
check("子程序還活著時不得回收（即使超過寬限值）", jb.reap_orphan_search_lock() is None)
alive.kill()
alive.wait()
jb.SEARCH_PROCESS = None
jb._release_search_lock()

# F3：PID 身分驗證 —— state 檔是上一輪留下的，裡面的 pid 可能已被回收給別的行程。
bystander = subprocess.Popen(["sleep", "600"])
check(
    "_pid_is_our_scan() 必須認出無關行程",
    jb._pid_is_our_scan(bystander.pid) is False,
    f"pid={bystander.pid} cmdline=sleep 600",
)
# 誘餌必須真的活得下來。GNU sleep 對非數字參數會直接報錯退出，所以
# `sleep 600 linkedin_job_search.py` 當場 exit 1（第一版就是這樣寫的，害這項檢查
# 時好時壞 —— 不穩定的測試比沒有測試更糟）。用 `exec -a` 改 argv[0] 才做得出來。
#
# 2026-09-19 第四輪審查：這一項原本斷言 is True，等於把「子字串比對」的弱點
# 寫成規格 —— 冒名者被認可。要送的訊號是 SIGKILL（不可逆），寬鬆的方向剛好是
# 最危險的那一邊，所以改成斷言【必須拒絕】。
impersonator = subprocess.Popen(["bash", "-c", "exec -a linkedin_job_search.py sleep 600"])
time.sleep(0.5)          # 等 exec 完成，cmdline 才是最終內容
check("冒名者必須被拒（argv[0] 不是 python解譯器，只是名字取得像）",
      jb._pid_is_our_scan(impersonator.pid) is False,
      f"cmdline={open(f'/proc/{impersonator.pid}/cmdline','rb').read().decode('utf-8','replace')!r}")
impersonator.kill()
impersonator.wait()

check("不存在的 pid 必須回 False（不確定就不動手）", jb._pid_is_our_scan(999999) is False)

# 正向對照組：兩種【真身】都必須被認出來。少了這些，把 _pid_is_our_scan() 改成
# 永遠回 False 也會全部通過 —— 那就變成「永遠不開槍」，卡死的掃描再也不會被收掉。
# 爬蟲的真實形狀是 run_scan.sh 用 `"$PY" -u "$SCRIPT"` 啟動，所以 argv[0] 是
# python、腳本路徑在參數裡（不是 argv[0]）。
ours = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(600)",
                         "/tmp/linkedin_job_search.py"])
time.sleep(0.5)
check("爬蟲形狀必須被認出（argv[0]=python 解譯器、argv 含我們的路徑）",
      jb._pid_is_our_scan(ours.pid) is True,
      f"cmdline={open(f'/proc/{ours.pid}/cmdline','rb').read().decode('utf-8','replace')!r}")
ours.kill()
ours.wait()

wrapper = subprocess.Popen(["bash", "-c", "exec -a run_scan.sh sleep 600"])
time.sleep(0.5)
check("wrapper 形狀必須被認出（argv[0] 就是 run_scan.sh）",
      jb._pid_is_our_scan(wrapper.pid) is True,
      f"cmdline={open(f'/proc/{wrapper.pid}/cmdline','rb').read().decode('utf-8','replace')!r}")
wrapper.kill()
wrapper.wait()

# F4：kill_stalled_external() 的兩道身分閘門。
# 用真實的 state 檔內容，但 phase=finished —— 這正是審查重現誤殺時的情境。
with mock.patch.object(jb, "read_jobscan_state",
                       return_value={"phase": "finished", "pid": bystander.pid,
                                     "run_id": "stale_000000", "trigger": "systemd-timer"}):
    with mock.patch.object(jb, "_external_idle_seconds",
                           return_value=jb.SEARCH_STALL_TIMEOUT + 100):
        with mock.patch.object(jb, "_we_hold_scan_lock", return_value=False):
            killed = _REAL_KILL_EXTERNAL()
check("state 自稱 finished 時，即使沉默超時也不得動手", killed is None, f"回傳={killed!r}")
check("旁觀者必須還活著（WTERMSIG=9 的事故不得重演）",
      bystander.poll() is None, f"poll={bystander.poll()}")

# F4c：phase 閘門必須獨立於 cmdline 檢查而存在 —— 這一項是 mutant 實驗揪出來的。
# 真正的危險情境不是「pid 被回收」（那由 cmdline 檢查擋），而是
# 「state 停在 finished、但 jobscan.service 此刻真的有一輪新掃描在跑」：
# 少了 phase 閘門，看板會拿著上一輪的殘骸去 SIGKILL 這一輪【合法】的掃描。
# 把假的 systemctl 攔下來，驗證「連查詢都不該發生」。
syscalls = []


class _FakeRun:
    returncode = 0
    stdout = "activating\n"     # 假裝 unit 正在跑 —— 正是「殺了會很慘」的狀態


def _fake_run(cmd, **kw):
    syscalls.append(cmd)
    return _FakeRun()


with mock.patch.object(jb, "read_jobscan_state",
                       return_value={"phase": "finished", "pid": 999999,
                                     "run_id": "stale_000000", "trigger": "systemd-timer"}):
    with mock.patch.object(jb, "_external_idle_seconds",
                           return_value=jb.SEARCH_STALL_TIMEOUT + 100):
        with mock.patch.object(jb, "_we_hold_scan_lock", return_value=False):
            with mock.patch.object(jb.subprocess, "run", side_effect=_fake_run):
                gated = _REAL_KILL_EXTERNAL()
check("phase=finished 時連 systemctl 查詢都不該發生（新掃描可能正在跑）",
      syscalls == [], f"實際呼叫={syscalls}")
check("且必須回 None，不得宣稱殺了東西（對 inactive unit 的假成功）",
      gated is None, f"回傳={gated!r}")

# 第二道閘門：phase 自稱 running，但 pid 的 cmdline 不是我們的掃描 → 仍須拒絕。
#
# ⚠️ 2026-09-19 第四輪審查（MAJOR）：這一段原本用 trigger="systemd-timer"，而且
# 【沒有】把 subprocess.run 攔下來，於是它拿【生產 unit 的即時狀態】做判斷 ——
# 若剛好撞上 06:00/22:00 的掃描窗口（unit 是 activating），這支測試就會對生產的
# jobscan.service 送出真的 SIGKILL，殺掉使用者當下的掃描。這正是本輪在修的那種
# 「不該動手卻動手」，只是換成測試來犯。修法兩層，缺一不可：
#   1. 改走 PID 路徑（trigger="manual"）。cmdline 檢查【只在 PID 路徑上有意義】，
#      走 systemd 分支根本測不到它 —— 那條分支殺的是整个 unit，本來就該殺。
#   2. 仍然攔下 subprocess.run 當保險：若以後有人把 trigger 改回去，測試會記錄到
#      呼叫而不是真的開槍。
syscalls2 = []


def _fake_run2(cmd, **kw):
    syscalls2.append(cmd)
    return _FakeRun()


with mock.patch.object(jb, "read_jobscan_state",
                       return_value={"phase": "running", "pid": bystander.pid,
                                     "run_id": "recycled", "trigger": "manual"}):
    with mock.patch.object(jb, "_external_idle_seconds",
                           return_value=jb.SEARCH_STALL_TIMEOUT + 100):
        with mock.patch.object(jb, "_we_hold_scan_lock", return_value=False):
            with mock.patch.object(jb.subprocess, "run", side_effect=_fake_run2):
                killed2 = _REAL_KILL_EXTERNAL()
check("phase=running 但 cmdline 不符時仍須拒絕", killed2 is None, f"回傳={killed2!r}")
check("旁觀者仍未被殺", bystander.poll() is None)
check("PID 路徑不得呼叫 systemctl（那是 systemd 分支的事）",
      syscalls2 == [], f"實際呼叫={syscalls2}")
bystander.kill()
bystander.wait()

# F5：幻影外部掃描 —— 不得採用「已結束那一輪」的 state 身分。
with mock.patch.object(jb, "read_jobscan_state",
                       return_value={"phase": "finished", "run_id": "20260919_220000",
                                     "trigger": "systemd-timer", "pid": 162966}):
    jb._external_begin()
check(
    "不得沿用已結束那輪的 run_id（否則日誌會冒出根本不存在的掃描）",
    jb._EXTERNAL["run_id"] is None,
    f"run_id={jb._EXTERNAL['run_id']!r}",
)
check("來源不明時應標示為 unknown 而非沿用舊 trigger",
      jb._EXTERNAL["trigger"] == "unknown", f"trigger={jb._EXTERNAL['trigger']!r}")
with jb.SEARCH_LOCK:
    jb._EXTERNAL["active"] = False
    jb._EXTERNAL["last_read_at"] = None

# F6：reader thread 的解碼失敗不得帶走掃描鎖。
# 這是 F1 盲區最可能的觸發源：爬蟲輸出的是抓回來的網頁文字，errors= 若為 strict，
# 讀到無法解碼的位元組就拋 UnicodeDecodeError。修正前該例外會讓執行緒當場死亡、
# 永遠走不到 _release_search_lock()。
class _BoomStream:
    def __iter__(self):
        raise UnicodeDecodeError("utf-8", b"\xff", 0, 1, "invalid start byte")


class _FakeProc:
    pid = 0
    def __init__(self):
        self.stdout = _BoomStream()
    def wait(self, timeout=None):
        return 0
    def poll(self):
        return 0


fd = jb._acquire_search_lock()
fake = _FakeProc()
jb.SEARCH_LOCK_OWNER = fake        # 讓 _release_search_lock(fake) 的 owner 檢查通過
raised = False
try:
    jb._read_search_output(fake)
except UnicodeDecodeError:
    raised = True
check("讀取端拋出 UnicodeDecodeError 時仍必須釋放掃描鎖（try/finally）",
      raised and jb.SEARCH_LOCK_FD is None,
      f"有拋出={raised} SEARCH_LOCK_FD={jb.SEARCH_LOCK_FD}")

# ═══ F7. 第四輪退回：殺戮路徑必須有【正向】測試 ═══════════════════════════════
#
# section F 到此為止【全部】是「不得動手」的否定斷言。審查用覆蓋探針證實了後果：
# 在 kill_stalled_external() 開頭插入 `return None`（整個功能失效）→ 39 項全過、
# 0 FAIL。也就是說這支測試無法區分「正確地拒絕開槍」與「永遠不開槍」，而
# 「矯正成不殺」正是本輪修正唯一的新風險方向：卡死的掃描只能等 systemd 五小時後
# 開槍，而手動掃描（trigger=manual）根本沒有 unit 可以等，是無界的。
print("=== F7. 第四輪退回：殺戮路徑的正向覆蓋 ===")

# F7a：PID 路徑 —— state 自稱 running、cmdline 真的是我們的 → 必須動手，且真的殺掉。
victim = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(600)",
                           "/tmp/linkedin_job_search.py"])
time.sleep(0.5)
victim_pid = victim.pid
syscalls_pos = []


def _fake_run_pos(cmd, **kw):
    syscalls_pos.append(cmd)
    return _FakeRun()


with mock.patch.object(jb, "read_jobscan_state",
                       return_value={"phase": "running", "pid": victim_pid,
                                     "run_id": "pos_manual", "trigger": "manual"}):
    with mock.patch.object(jb, "_external_idle_seconds",
                           return_value=jb.SEARCH_STALL_TIMEOUT + 100):
        with mock.patch.object(jb, "_we_hold_scan_lock", return_value=False):
            with mock.patch.object(jb.subprocess, "run", side_effect=_fake_run_pos):
                fired = _REAL_KILL_EXTERNAL()
check("卡死且身分相符時【必須】動手（否則就是矯正成不殺）",
      fired is not None and "pos_manual" in fired, f"回傳={fired!r}")
check("（承上）PID 路徑不得順便去動 systemctl",
      syscalls_pos == [], f"實際呼叫={syscalls_pos}")
victim.wait(timeout=5)
check("目標必須真的死亡（不是只回報殺了）",
      victim.poll() is not None, f"poll={victim.poll()}")

# F7b：systemd 分支 —— unit 在 activating（＝真的有一輪在跑）時必須送出 kill。
syscalls3 = []


class _FakeRunKill:
    returncode = 0
    stdout = "activating\n"


def _fake_run3(cmd, **kw):
    syscalls3.append(cmd)
    return _FakeRunKill()


with mock.patch.object(jb, "read_jobscan_state",
                       return_value={"phase": "running", "pid": 999999,
                                     "run_id": "pos_timer", "trigger": "systemd-timer"}):
    with mock.patch.object(jb, "_external_idle_seconds",
                           return_value=jb.SEARCH_STALL_TIMEOUT + 100):
        with mock.patch.object(jb, "_we_hold_scan_lock", return_value=False):
            with mock.patch.object(jb.subprocess, "run", side_effect=_fake_run3):
                fired2 = _REAL_KILL_EXTERNAL()
kill_calls = [c for c in syscalls3 if "kill" in c]
check("unit 在 activating 時必須送出 systemctl kill",
      len(kill_calls) == 1 and "--signal=SIGKILL" in kill_calls[0],
      f"實際呼叫={syscalls3}")
check("且必須回報終止了 jobscan.service",
      fired2 is not None and "jobscan.service" in fired2, f"回傳={fired2!r}")

# F7c：unit 不在跑時【絕對不能】送出 kill。
# 對 inactive 的 unit 送 `systemctl --user kill` 會回 0 但什麼也沒殺（本次實測），
# 照單全收就會留下「已終止 jobscan.service」這筆假成功 —— 比不殺更糟，因為它會讓
# 下一個追查事故的人以為停滯偵測正常運作過。
#
# 這一項非補不可：F4b 為了安全改成 trigger="manual" 之後，就走不到 systemd 分支了，
# 移除 ActiveState 檢查（變異 M6）會變成沒有任何測試抓得到。修正一個缺陷時把另一個
# 缺陷的覆蓋一起弄丟，是這個專案反覆出現的模式，所以補覆蓋與補修正一樣重要。
syscalls4 = []


class _FakeRunIdle:
    returncode = 0
    stdout = "inactive\n"      # 生產 unit 的真實狀態：沒有掃描在跑


def _fake_run4(cmd, **kw):
    syscalls4.append(cmd)
    return _FakeRunIdle()


with mock.patch.object(jb, "read_jobscan_state",
                       return_value={"phase": "running", "pid": 999999,
                                     "run_id": "idle_unit", "trigger": "systemd-timer"}):
    with mock.patch.object(jb, "_external_idle_seconds",
                           return_value=jb.SEARCH_STALL_TIMEOUT + 100):
        with mock.patch.object(jb, "_we_hold_scan_lock", return_value=False):
            with mock.patch.object(jb.subprocess, "run", side_effect=_fake_run4):
                idle_res = _REAL_KILL_EXTERNAL()
check("unit 不在跑時不得送出 kill（inactive unit 的 kill 回 0 卻什麼也沒殺）",
      [c for c in syscalls4 if "kill" in c] == [], f"實際呼叫={syscalls4}")
check("且不得宣稱終止了 jobscan.service（假成功比不殺更糟）",
      idle_res is None or "jobscan.service" not in idle_res, f"回傳={idle_res!r}")

# ═══ G. 第四輪退回：兩個「有修正但沒有回歸保護」的缺口 ════════════════════════
print("=== G. 第四輪退回：errors=replace 與 TOCTOU 重檢的回歸保護 ===")

# G1：Popen 必須帶 errors="replace"。
# 審查變異 M3 證實：拿掉 errors="replace", → 39 項全過，沒有任何測試會失敗。
# 它被列為 MAJOR-A 的修法之一，卻完全沒有覆蓋。手法沿用 section D：記錄參數後
# 立刻失敗，讓 start_search() 走 except 分支 —— 這樣不會真的啟動 reader thread。
popen_kwargs = {}


def _record_popen(*args, **kwargs):
    popen_kwargs.update(kwargs)
    raise FileNotFoundError("只為了記錄參數，立刻失敗")


jb.SEARCH_PROCESS = None
jb._release_search_lock()
with mock.patch.object(jb.subprocess, "Popen", side_effect=_record_popen):
    ok3, msg3 = jb.start_search()
check('Popen 必須帶 errors="replace"（strict 會讓解碼失敗帶走 reader thread）',
      popen_kwargs.get("errors") == "replace",
      f"ok={ok3} msg={msg3!r} 實際 errors={popen_kwargs.get('errors')!r}")
check("（承上）這次失敗後鎖同樣必須已釋放", jb.SEARCH_LOCK_FD is None)

# G2：TOCTOU 重檢 —— 判定與動手之間若看板取得了掃描鎖，必須放棄動手。
# 審查變異 M9：拿掉第二個 _we_hold_scan_lock() → 39 項全過（無覆蓋）。
# DECISIONS 把它列為「刻意接受的殘餘風險」，但程式碼既然留著這道檢查，
# 就該有東西證明它還在。
victim2 = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(600)",
                            "/tmp/linkedin_job_search.py"])
time.sleep(0.5)
hold_calls = []


def _hold_seq():
    hold_calls.append(1)
    return len(hold_calls) > 1     # 第一次 False（判定時未持鎖），之後 True（動手前已持鎖）


with mock.patch.object(jb, "read_jobscan_state",
                       return_value={"phase": "running", "pid": victim2.pid,
                                     "run_id": "toctou", "trigger": "manual"}):
    with mock.patch.object(jb, "_external_idle_seconds",
                           return_value=jb.SEARCH_STALL_TIMEOUT + 100):
        with mock.patch.object(jb, "_we_hold_scan_lock", side_effect=_hold_seq):
            toctou = _REAL_KILL_EXTERNAL()
check("動手前若已取得掃描鎖，必須放棄（TOCTOU 重檢）",
      toctou is None and len(hold_calls) >= 2,
      f"回傳={toctou!r} 檢查次數={len(hold_calls)}")
check("（承上）目標必須還活著", victim2.poll() is None)
victim2.kill()
victim2.wait()

# ═══ 結果 ════════════════════════════════════════════════════════════════════
print()
if FAILURES:
    print(f"❌ {len(FAILURES)} 項失敗：")
    for f in FAILURES:
        print(f"   - {f}")
    sys.exit(1)
print("✅ 全數通過")
sys.exit(0)
