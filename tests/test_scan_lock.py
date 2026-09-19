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
import ast
import atexit
import contextlib
import io
import json
import os
import re
import shutil
import subprocess
import sys
import threading
import time
from datetime import datetime
from pathlib import Path

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


# 誘餌行程一律走這裡生。兩個理由，都是實際踩到的：
#   1. stdout/stderr 必須導向 DEVNULL。子行程會【繼承本測試的 stdout】，而 stdout
#      通常是管線（`... | tail`）。只要有任何一個誘餌沒被殺掉，它就繼續握著管線的
#      寫入端 → 讀取端永遠等不到 EOF → 整個指令看起來像「卡死」。2026-09-19 診斷
#      M8 變異逾時 400 秒就是這個：兩個 PPID=1 的孤兒 python 握著管線，測試本身
#      其實早就跑完了。
#   2. 全部登記起來，收工時無條件清掉。測試【失敗】的時候最需要這個 —— 而失敗
#      正是變異測試刻意製造的情境（M8 就是讓應該被殺的誘餌活下來）。留著孤兒等於
#      每次跑變異都在機器上疊一個 sleep 600。
_SPAWNED: list[subprocess.Popen] = []


def spawn(*argv: str) -> subprocess.Popen:
    p = subprocess.Popen(list(argv), stdout=subprocess.DEVNULL,
                         stderr=subprocess.DEVNULL)
    _SPAWNED.append(p)
    return p


def wait_dead(proc: subprocess.Popen, timeout: float = 5) -> bool:
    """等子程序結束，回傳「真的結束了嗎」。

    刻意【不讓 TimeoutExpired 冒出去】：逾時正是「應該被殺的誘餌還活著」這個失敗
    情境本身，它必須變成一項 FAIL，不能把整個測試炸掉。2026-09-19 實測 —— M8
    （kill_stalled_external 整個 no-op）就是在這裡拋 TimeoutExpired，害 F7b / F7c /
    G 共 9 項檢查根本沒跑到：變異表面上「被逮」，實際是把後面的訊號全遮掉了。
    真實迴歸發生時有一模一樣的後果（只看到第一項失敗，看不到全貌）。

    【本檔鐵律】不得對誘餌呼叫無界的 .wait() —— 誘餌動輒 sleep 600，一旦突變讓它
    活下來，測試就會卡死而不是失敗。一律走這裡。
    """
    try:
        proc.wait(timeout=timeout)
        return True
    except subprocess.TimeoutExpired:
        return False


def reap_all_spawned() -> None:
    for p in _SPAWNED:
        if p.poll() is None:
            p.kill()
    for p in _SPAWNED:
        wait_dead(p)


# 掛在 atexit，不是只寫在檔尾 —— 因為「檔尾那一行」在【例外】路徑上根本到不了：
# 上面的跳線故意拋 AssertionError，任何非預期例外也會直接冒出去。atexit 在
# sys.exit()、未捕捉例外、正常結束三種收場都會跑，不必把 600 行全部再縮排一層。
# （唯一蓋不到的是 SIGKILL —— 但那種情況下誘餌最長 600 秒就自己結束，
#   而且已經導向 DEVNULL，不會再出現「握著管線假裝卡死」的問題。）
atexit.register(reap_all_spawned)


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
child = spawn("sleep", "600")
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
owner_proc = spawn("sleep", "600")
jb.SEARCH_PROCESS = owner_proc
check(
    "有子程序在跑時不得回收（那是有主的鎖，誤放會導致並發爬蟲）",
    jb.reap_orphan_search_lock() is None,
)
owner_proc.kill()
wait_dead(owner_proc)
jb.SEARCH_PROCESS = None
jb._release_search_lock()
check("清理：鎖已釋放", jb.SEARCH_LOCK_FD is None)

# ═══ F. 第三輪審查退回項（2026-09-19）════════════════════════════════════════
print("=== F. 第三輪退回：孤兒鎖盲區 / 誤殺無關行程 / 幻影掃描 ===")

# F1：兩邊都不管的盲區 —— 子行程已死、但 SEARCH_PROCESS 仍指向它。
# 修正前 kill_stalled_search() 看 poll() 不為 None 而放棄、reap_orphan_search_lock()
# 看 SEARCH_PROCESS 不為 None 而放棄，於是鎖永遠不放、timer 每輪 SKIPPED + exit 0。
fd = jb._acquire_search_lock()
dead = spawn("true")
wait_dead(dead)                                     # 已結束，poll() 回 0（不是 None）
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
alive = spawn("sleep", "600")
jb.SEARCH_PROCESS = alive
jb.SEARCH_LOCK_HELD_SINCE = time.monotonic() - (jb.ORPHAN_LOCK_GRACE + 10)
check("子程序還活著時不得回收（即使超過寬限值）", jb.reap_orphan_search_lock() is None)
alive.kill()
wait_dead(alive)
jb.SEARCH_PROCESS = None
jb._release_search_lock()

# F3：PID 身分驗證 —— state 檔是上一輪留下的，裡面的 pid 可能已被回收給別的行程。
bystander = spawn("sleep", "600")
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
impersonator = spawn("bash", "-c", "exec -a linkedin_job_search.py sleep 600")
time.sleep(0.5)          # 等 exec 完成，cmdline 才是最終內容
check("冒名者必須被拒（argv[0] 不是 python解譯器，只是名字取得像）",
      jb._pid_is_our_scan(impersonator.pid) is False,
      f"cmdline={open(f'/proc/{impersonator.pid}/cmdline','rb').read().decode('utf-8','replace')!r}")
impersonator.kill()
wait_dead(impersonator)

check("不存在的 pid 必須回 False（不確定就不動手）", jb._pid_is_our_scan(999999) is False)

# 正向對照組：兩種【真身】都必須被認出來。少了這些，把 _pid_is_our_scan() 改成
# 永遠回 False 也會全部通過 —— 那就變成「永遠不開槍」，卡死的掃描再也不會被收掉。
# 爬蟲的真實形狀是 run_scan.sh 用 `"$PY" -u "$SCRIPT"` 啟動，所以 argv[0] 是
# python、腳本路徑在參數裡（不是 argv[0]）。
ours = spawn(sys.executable, "-c", "import time; time.sleep(600)",
                         "/tmp/linkedin_job_search.py")
time.sleep(0.5)
check("爬蟲形狀必須被認出（argv[0]=python 解譯器、argv 含我們的路徑）",
      jb._pid_is_our_scan(ours.pid) is True,
      f"cmdline={open(f'/proc/{ours.pid}/cmdline','rb').read().decode('utf-8','replace')!r}")
ours.kill()
wait_dead(ours)

wrapper = spawn("bash", "-c", "exec -a run_scan.sh sleep 600")
time.sleep(0.5)
check("wrapper 形狀必須被認出（argv[0] 就是 run_scan.sh）",
      jb._pid_is_our_scan(wrapper.pid) is True,
      f"cmdline={open(f'/proc/{wrapper.pid}/cmdline','rb').read().decode('utf-8','replace')!r}")
wrapper.kill()
wait_dead(wrapper)

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
# ⚠️ 不可以寫 `bystander.poll() is None`。SIGKILL 送出到子行程真的死掉之間有窗口，
# `poll()` 若在窗口內呼叫會回 None —— 於是「該被殺」的變異體【假通過】。
# 第六輪審查實測：M5 之下這一項 20 次裡有 2 次假通過，讓 `tests/mutate.py` 的
# 數字漂移（56P/2F ↔ 57P/1F）。改成有界等待：等它真的死，再斷言它沒死。
check("旁觀者必須還活著（WTERMSIG=9 的事故不得重演）",
      not wait_dead(bystander, 0.5), f"poll={bystander.poll()}")

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
# 同 F4b：`poll()` 緊接在 SIGKILL 之後是 race，會讓 M5 的數字漂移。有界等待。
check("旁觀者仍未被殺", not wait_dead(bystander, 0.5))
check("PID 路徑不得呼叫 systemctl（那是 systemd 分支的事）",
      syscalls2 == [], f"實際呼叫={syscalls2}")
bystander.kill()
wait_dead(bystander)

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
victim = spawn(sys.executable, "-c", "import time; time.sleep(600)",
                           "/tmp/linkedin_job_search.py")
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
check("目標必須真的死亡（不是只回報殺了）",
      wait_dead(victim, 5), f"poll={victim.poll()}")

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
victim2 = spawn(sys.executable, "-c", "import time; time.sleep(600)",
                            "/tmp/linkedin_job_search.py")
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
wait_dead(victim2)

# 收工前清場。失敗路徑【最需要】這一行：變異測試刻意製造的情境就是「應該被殺的
# 誘餌活著」。上面 atexit 是備援，這裡是正常路徑的保證。
reap_all_spawned()

# ═══ H. 日誌：整行必須一次 write() ════════════════════════════════════════════
print("=== H. 日誌整行一次 write（print() 的換行會被別的行吃掉）===")

# 2026-09-19 在生產的 job_board.log 實際看到兩行黏在一起：
#   [23:39:24] [watchdog] Watchdog thread started[23:39:24] [jobscan] ... started=====
# 成因是 print() 分成兩次 write()（內容、換行），多執行緒在 O_APPEND 下交錯。
# 這正好抵銷 _log() 加時戳的目的（加時戳就是為了對齊，黏行反而更難追）。
# 23:05 啟動正常、23:39 啟動黏住 —— 是間歇性 race，不是每次都會中。
#
# ⚠️ 這裡【刻意不】用高併發去逼那個 race。試過了，行不通：用 8 執行緒 × 40 行
# 對 StringIO 猛寫，退回 print() 的變異（M11）照樣 55/55 全過 —— StringIO 太快，
# GIL 在兩次 write() 之間幾乎不切換。而把競爭拉高到會不定期失敗，就變成
# 「不穩定的測試」，本專案已經吃過那個虧（比沒有測試更糟）。
#
# 改成斷言 race 的【成因】，這是決定性的：print() 一定是兩次 write()。
# 只要 _out() 對每一行只呼叫一次 write()，黏行在結構上就不可能發生。
#
# 措辭要精確：**不是** O_APPEND 本身給了原子性（它只保證 offset 設到檔尾）。
# 不交錯來自 Linux 核心對同一 inode 的 write(2) 以 i_rwsem 序列化；POSIX 對一般
# 檔案並沒有保證這件事（PIPE_BUF 的保證只適用 pipe/FIFO，且只到 4096 bytes）。
# 另外，這個因果的**必要前提是 stdout 不緩衝**（unit 裡的 PYTHONUNBUFFERED=1）——
# 第五輪審查用有緩衝的檔案實測，print 竟然是 0 黏行。完整數據見 job_board.py
# 的 _out() 上方註解。


class _WriteRecorder(io.TextIOBase):
    """假的 stdout：記錄每一次 write() 呼叫，不保留內容。"""

    def __init__(self) -> None:
        self.calls: list[str] = []

    def write(self, s: str) -> int:      # type: ignore[override]
        self.calls.append(s)
        return len(s)

    def flush(self) -> None:
        pass


_rec = _WriteRecorder()
with contextlib.redirect_stdout(_rec):
    jb._log("測試訊息")

check(
    "_log() 必須把整行用【一次】write() 寫出（print 會拆成兩次，換行就會被吃掉）",
    len(_rec.calls) == 1 and bool(re.match(r"^\[\d\d:\d\d:\d\d\] 測試訊息\n$", _rec.calls[0])),
    f"write() 次數={len(_rec.calls)}  內容={_rec.calls!r}",
)

# 下面這一項原本只是【一般性煙霧測試】：用 8 執行緒對 StringIO 猛寫、斷言沒有
# 黏行 —— 而 M11（把 _out 退回 print）在它底下照樣通過，所以它守不住任何東西。
#
# 第五輪審查給了讓它復活的第三條路（不必刪、也不必只當煙霧測試）：**讓假 stdout
# 的 write() 主動讓出 GIL**。StringIO 太快是問題的根源；`time.sleep(0)` 會強制
# 排程器換手，print 的兩次 write() 就真的會交錯。審查實測（5 輪無重疊）：
#
#   假 stdout                     print(flush=True)      單次 write（現行）
#   （不讓出 GIL）                0 黏行（測不出來）      0 黏行
#   write() 內 sleep(0)           黏 67–88 / 320 行       0 / 320 行
#
# 但真正讓它成為【決定性】守衛的不是 sleep(0)，而是把斷言從「串接後再 split」
# 改成「逐次檢查 write() 呼叫」：_out() 對每行只呼叫一次 write()，所以每一次
# 呼叫都必須是「一整行、含結尾換行」。print 會產生兩個呼叫 —— 內容（無換行）
# 與裸的 "\n" —— 兩個都不符合，當場失敗。sleep(0) 只是讓 race 真的發生，
# 是加強而非機制。
_H_T, _H_L = 8, 40
_H_LINE = re.compile(r"^\[\d\d:\d\d:\d\d\] thread=\d+ line=\d+\n$")


class _ConcurrentRecorder(io.TextIOBase):
    """假的 stdout：記錄每一次 write() 呼叫，並在呼叫時讓出 GIL。"""

    def __init__(self) -> None:
        self.calls: list[str] = []

    def write(self, s: str) -> int:      # type: ignore[override]
        time.sleep(0)
        self.calls.append(s)
        return len(s)

    def flush(self) -> None:
        time.sleep(0)


def _h_spam(tid: int) -> None:
    for i in range(_H_L):
        jb._log(f"thread={tid} line={i}")


_rec2 = _ConcurrentRecorder()
with contextlib.redirect_stdout(_rec2):
    _h_ts = [threading.Thread(target=_h_spam, args=(t,)) for t in range(_H_T)]
    for _t in _h_ts:
        _t.start()
    for _t in _h_ts:
        _t.join()

# 只看本測試自己產生的 write()（背景 watchdog 可能插進來寫它自己的行）。
# 黏行的精確特徵是「一次 write() 裡有兩個以上時戳」——那代表兩行被融合。
_h_mine = [c for c in _rec2.calls if "thread=" in c]
_h_whole = [c for c in _h_mine if _H_LINE.match(c)]
_h_glued = [c for c in _h_mine if len(re.findall(r"\[\d\d:\d\d:\d\d\]", c)) > 1]
check(
    "併發下每一次 write() 都必須是【一整行含換行】（print 會拆成兩次，當場失敗）",
    len(_h_whole) == _H_T * _H_L and not _h_glued,
    f"write() 呼叫={len(_h_mine)}  完整行={len(_h_whole)}/{_H_T * _H_L}  黏行={len(_h_glued)}",
)

# ═══ I. 靜態不變式：模組裡不得有 live 的 print() ═══════════════════════════════
# H 區只觀測 _log() 這一條路徑。第五輪審查指出具體的失敗情境：有人在 scheduler
# 或 watchdog 執行緒加一行 print("[timer] ...", flush=True) —— H 區全部照過，
# 而審查實測該寫法在生產設定下黏 139/2400 行。模組級的不變式不能只活在註解裡。
#
# 用 AST 而不是 grep，因為 grep 分不出「程式碼」與「註解／docstring」——
# job_board.py 的 _out() 上方就有一大段「print 為什麼不能用」的說明，
# grep 會把它們全部算成違規（偽陽性），然後就會有人把這個測試關掉。
_jb_src = Path(jb.__file__).read_text(encoding="utf-8")
_jb_ast = ast.parse(_jb_src)

_out_node = next((n for n in ast.walk(_jb_ast)
                  if isinstance(n, ast.FunctionDef) and n.name == "_out"), None)
# _out() 自己的 sys.stdout.write 是唯一合法的直接寫入點。
_out_span = (_out_node.lineno, _out_node.end_lineno) if _out_node else (0, 0)


def _is_direct_stdout_write(node: ast.AST) -> bool:
    f = getattr(node, "func", None)
    return (isinstance(f, ast.Attribute) and f.attr == "write"
            and isinstance(f.value, ast.Attribute)
            and f.value.attr in ("stdout", "stderr")
            and isinstance(f.value.value, ast.Name)
            and f.value.value.id == "sys")


# ⚠️ 豁免的【只有】_out() 內部那一次 sys.stdout.write —— `print()` 是
# **任何位置**都違規，包括 _out() 自己的身體。
#
# 第一版把「落在 _out span 內」當成整段豁免，結果 M11（把 _out 的內容換成
# print(line, flush=True)）的那個 print 也在 span 內，被一起放行了 ——
# 這一項根本沒開火。是對照「M11 只有 2 個 FAIL」才發現的：照理 H1、H2、I
# 三項都該響。（感謝變異測試，它不只驗證修正，也驗證了驗證本身。）
_prints = [n.lineno for n in ast.walk(_jb_ast)
           if isinstance(n, ast.Call)
           and isinstance(n.func, ast.Name) and n.func.id == "print"]
_stray_writes = [n.lineno for n in ast.walk(_jb_ast)
                 if isinstance(n, ast.Call) and _is_direct_stdout_write(n)
                 and not (_out_span[0] <= n.lineno <= _out_span[1])]
_violations = sorted(_prints + _stray_writes)
check(
    "job_board.py 不得有 live 的 print()／直接 sys.stdout.write（一律走 _out）",
    _out_node is not None and not _violations,
    f"違規行={_violations}" if _violations else
    f"_out 位於 {_out_span}；print 呼叫 0 處、_out 外的直接寫入 {len(_stray_writes)} 處",
)

# ═══ J. 排程主權的啟動狀態轉移（含「回復舊制」那條路）═══════════════════════════
# 2026-09-20 實測發現「回復舊制」是【靜默失敗】：設了 JOB_BOARD_INTERNAL_SCHEDULER=1
# 之後執行緒確實啟動（log 有 "Scheduler thread started"），但
#   * schedule.enabled 停在 false（先前被壓平過）→ 永遠不觸發
#   * managed_by 仍是 "systemd-timer" → 前端 SCHEDULE_READONLY=true
#     → 面板唯讀，使用者【無法從 UI 重新啟用】
# 結果：照 deploy/README.md 的復原程序操作，會得到一個「再也不會掃描、而畫面顯示
# 排程由一個剛剛被停用的 timer 管理」的系統。那份復原計畫等於是虛構的。
#
# 這條路徑只在【啟動時】跑一次，所以用子行程測：兩個方向各啟動一次，看它把排程檔
# 寫成什麼。子行程同樣裝跳線 —— 它是真的 import job_board，會啟動背景 watchdog。
# （雖然 lock 指向暫存檔、jobscan.service 現在也是 inactive，但這個專案已經吃過
# 一次「測試對生產 unit 送真的 SIGKILL」的虧，第四輪 MAJOR。）
_J_TMP = tempfile.mkdtemp(prefix="jobspy-sched-")
#
# ⚠️ 結尾的 `os._exit(0)` 不是裝飾，是【必要的】。少了它，這個子行程有大約
# 10% 的機率不是 exit 0 而是 **SIGABRT（rc=-6）**，於是這一區會隨機 FAIL ——
# 2026-09-20 實測：forward 25 次中 3 次、reverse 25 次中 2 次，錯誤是
#
#   Fatal Python error: could not acquire lock for
#   <_io.BufferedWriter name='<stdout>'> at interpreter shutdown,
#   possibly due to daemon threads
#   Python runtime state: finalizing
#
# 成因：`import job_board` 會在模組層啟動 watchdog / jobscan 監看 / 排程器三個
# daemon 執行緒（`_out()` 會寫 stdout）。`-c` 程式一結束，主執行緒就進入
# interpreter finalization，此時那些執行緒還在跑，一寫 stdout 就撞上
# `Py_FatalError`。**這是「測試寫法」的問題，不是生產路徑的問題** ——
# 生產的 jobboard.service 收到 SIGTERM 時 Python 沒裝 handler（實測
# `signal.getsignal(SIGTERM)` 回 0 = SIG_DFL），核心直接終止、根本不跑
# finalization，所以 journal 裡 0 筆 ABRT。只有在「直譯器正常結束」時才會踩到。
#
# 用 `os._exit(0)` 跳過 finalization → 決定性 exit 0。這【不會】削弱這一區：
# 排程檔是 import 期間同步寫完的，斷言讀的是磁碟上的內容；若 `save_schedule()`
# 根本沒被呼叫，讀回來的就是 seed 本身，兩項都會 FAIL（已用 M14/M15 驗證）。
#
# ⚠️ 兩道跳線，缺一不可（第六輪審查的 MAJOR M2）：
#   * `subprocess.run` 擋 argv 含 "kill" 的呼叫（第四輪 MAJOR 的教訓）。
#   * `subprocess.Popen` 擋【啟動真爬蟲】。這一條原本沒有，而排程器啟動爬蟲走的
#     正是 `subprocess.Popen([sys.executable, "-u", linkedin_job_search.py])`，
#     **完全繞過 subprocess.run 那道跳線**。也就是說原版的隔離不是護欄，而是
#     「種子的 interval_hours 剛好很大」這個常數 —— 只要種子一逾期（本輪就真的
#     發生過兩次，`job_board.log` 兩筆 `[scheduler] Triggering search`），
#     子行程會在【生產目錄】跑起真的爬蟲。審查員用原封不動的跳線實測重現了。
#     現在它是一條斷言，不是一個但書。
#   * 兩道都要在 `import job_board`【之前】裝好 —— 插在其後會有 race，因為
#     job_board 是在模組層就把背景執行緒啟動起來的。
_J_TRIPWIRE = (
    "import subprocess, sys, os\n"
    "_real = subprocess.run\n"
    "def _guard(*a, **k):\n"
    "    cmd = a[0] if a else k.get('args')\n"
    "    if isinstance(cmd, (list, tuple)) and 'kill' in cmd:\n"
    "        raise AssertionError('tripwire: %r' % (cmd,))\n"
    "    return _real(*a, **k)\n"
    "subprocess.run = _guard\n"
    "_real_popen = subprocess.Popen\n"
    "def _guard_popen(*a, **k):\n"
    "    cmd = a[0] if a else k.get('args')\n"
    "    if isinstance(cmd, (list, tuple)) and any(\n"
    "            'linkedin_job_search.py' in str(x) for x in cmd):\n"
    "        raise AssertionError('tripwire: 試圖啟動真爬蟲 %r' % (cmd,))\n"
    "    return _real_popen(*a, **k)\n"
    "subprocess.Popen = _guard_popen\n"
    "import job_board\n"
    "sys.stdout.flush()\n"
    "os._exit(0)\n"   # ← 見上方：跳過 finalization，避開 daemon 執行緒的 SIGABRT
)


def _startup_schedule(seed: dict, internal: bool) -> dict:
    """在子行程 import job_board，回傳它寫出的排程檔內容。

    seed 的 mode 用 interval + 極長的 interval_hours，讓「是否逾期」與牆上時鐘
    無關 —— 測排程狀態轉移不該因為剛好跑在 06:00 就變成另一回事（更糟的是：
    一旦判定逾期，排程器會呼叫 start_search() 真的去啟動爬蟲）。
    """
    path = os.path.join(_J_TMP, f"sched-{time.time_ns()}.json")
    with open(path, "w") as f:
        json.dump(seed, f)
    env = dict(os.environ,
               JOB_BOARD_SCHEDULE_FILE=path,
               JOBSCAN_LOCK=os.path.join(_J_TMP, "jobscan.lock"),
               JOBSCAN_LIVE=os.path.join(_J_TMP, "live.log"),
               JOBSCAN_STATE=os.path.join(_J_TMP, "state.json"))
    env.pop("JOB_BOARD_INTERNAL_SCHEDULER", None)
    if internal:
        env["JOB_BOARD_INTERNAL_SCHEDULER"] = "1"
    r = subprocess.run([sys.executable, "-c", _J_TRIPWIRE], env=env,
                       capture_output=True, text=True, timeout=120)
    if r.returncode != 0:
        return {"__error__": (r.stderr or r.stdout)[-400:]}
    try:
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    except (OSError, json.JSONDecodeError) as e:
        # 壞掉的 JSON 要在這裡變成【一項 FAIL】，不是讓 traceback 冒出去把整支
        # 測試炸掉 —— 後者會讓後面的區段全部不執行，而且看起來像工具壞了。
        return {"__error__": f"排程檔讀不回來：{e!r}"}


def _seed(enabled: bool, managed_by: str) -> dict:
    return {"enabled": enabled, "mode": "interval", "interval_hours": 100000,
            "times": ["06:00", "22:00"], "last_run": datetime.now().isoformat(),
            "next_run": None, "last_run_date": None, "_fired_today": {},
            "managed_by": managed_by}


# 正式路徑：主權在 systemd，內建排程器必須被壓平（seed 故意寫成相反的值，
# 否則這一項是空轉的 —— 它得證明程式碼真的【改了】什麼）。
_fwd = _startup_schedule(_seed(enabled=True, managed_by="internal"), internal=False)
check(
    "啟動時（無 INTERNAL_SCHEDULER）必須把 enabled 壓平、managed_by 設為 systemd-timer",
    _fwd.get("enabled") is False and _fwd.get("managed_by") == "systemd-timer"
    and _fwd.get("next_run") is None,
    f"enabled={_fwd.get('enabled')} managed_by={_fwd.get('managed_by')} "
    f"next_run={_fwd.get('next_run')} {_fwd.get('__error__', '')}",
)

# 回復舊制：兩個欄位都必須被還原，否則使用者得到一個不會掃描卻看似正常的系統。
_rev = _startup_schedule(_seed(enabled=False, managed_by="systemd-timer"), internal=True)
check(
    "回復舊制（INTERNAL_SCHEDULER=1）必須把 enabled 還原、managed_by 設回 internal，"
    "且 next_run 不得為 None",
    _rev.get("enabled") is True and _rev.get("managed_by") == "internal"
    and _rev.get("next_run") is not None,
    f"enabled={_rev.get('enabled')} managed_by={_rev.get('managed_by')} "
    f"next_run={_rev.get('next_run')} {_rev.get('__error__', '')}",
)
shutil.rmtree(_J_TMP, ignore_errors=True)

print("=== K. 第六輪退回：api_schedule 的輸入驗證（幽靈排程）===")
# 第六輪審查的 m2：`times: []` 會產生一則【幽靈排程】。_compute_next_run() 的
# `cfg.get("times") or [...]` 對空清單會退回預設時段，於是面板顯示「06:00 會
# 掃描」；但排程器那條用 `cfg.get("times", [...])`（只有 key 不存在才退回），
# for 迴圈空轉 → due 永遠 False → **永遠不掃描**。
#
# 這是重構【製造】出來的：舊碼在同樣輸入下 IndexError → HTTP 500（醜，但大聲）。
# 「把吵的失敗變成靜默失敗」是這個專案獵捕的缺陷類別，所以補上入口驗證。
#
# 核心不變式：**next_run 必須永遠由【已儲存的】cfg 推導出來**。幽靈排程的定義
# 就是這條不變式被破壞 —— 畫面顯示的時段不在 cfg["times"] 裡。
#
# 這裡【不寫檔】：SCHEDULE_CONFIG 與 save_schedule 都被 mock 掉。直接在測試行程
# 裡 POST 而不 mock 的話會覆寫生產的 .job_board_schedule.json（本輪就發生過一次，
# 那次還順帶啟動了兩次真爬蟲）。
class _FakeReq:
    def __init__(self, payload) -> None:
        self.method = "POST"
        self._payload = payload

    def get_json(self, silent: bool = False):
        # 產品碼呼叫的是 `request.get_json(silent=True)`；只收 self 的假物件會 TypeError。
        return self._payload


def _post_schedule(payload, internal: bool = False, enabled: bool = True):
    """回傳 (cfg, resp)。cfg 是 api_schedule 就地改過的那份 dict。

    `internal=True` 會把 `jb.INTERNAL_SCHEDULER` 一起 mock 成 True（等於「回復舊制」
    那個組態）。這讓「enabled 被什麼東西改動」變成可觀察的行為，而不是只能比對
    警告文字 —— 字串比對會讓變異表測到措辭而不是測到行為。

    ⚠️ `jsonify` 是【位置】引數（`job_board.py` 的 `return jsonify(resp)`），不是關鍵字
    引數 —— 只收 `**k` 的 mock 會 TypeError。這裡兩種都收。

    ⚠️ 例外必須在這裡變成 dict，不能讓 traceback 冒出去。理由與 `_startup_schedule`
    的排程檔讀取相同：冒出去的 traceback 會讓**後面所有區段都不執行**，而
    `tests/mutate.py` 只數 `[FAIL]` —— 於是 0 個 FAIL，變異體**假通過**。
    這正是第六輪審查抓到的缺陷類別（把大聲的失敗變成靜默的失敗），只是換了個位置。
    """
    cfg = {"enabled": enabled, "mode": "times", "interval_hours": 6,
           "times": ["06:00", "22:00"], "next_run": None, "_fired_today": {}}
    try:
        with mock.patch.object(jb, "SCHEDULE_CONFIG", cfg), \
                mock.patch.object(jb, "INTERNAL_SCHEDULER", internal), \
                mock.patch.object(jb, "save_schedule"), \
                mock.patch.object(jb, "request", _FakeReq(payload)), \
                mock.patch.object(jb, "jsonify",
                                  side_effect=lambda *a, **k: a[0] if a else k):
            out = jb.api_schedule()
    except Exception as e:  # noqa: BLE001 — 任何例外都是一項 FAIL，不是一次崩潰
        return cfg, {"__error__": f"api_schedule 丟出例外：{e!r}"}
    if isinstance(out, tuple):  # `return jsonify(...), 400`
        body, status = out
        if isinstance(body, dict):
            body = {**body, "__status__": status}
        return cfg, body
    return cfg, out


def _next_run_time_in_times(cfg) -> bool:
    """不變式：next_run 的 HH:MM 必須是 cfg['times'] 裡的其中一個。"""
    nr = cfg.get("next_run")
    return bool(nr) and nr.split()[-1] in (cfg.get("times") or [])


# 合法／非法時段。字串邊界要逐一釘住：'6:00'（少一位）、'24:00'、'06:60'、
# 非字串、空清單、空字串。這些都是「面板存得進去、但排程器不會照做」的來源。
_K_VALID = [["06:00"], ["06:00", "22:00"], ["00:00", "23:59"]]
_K_INVALID = [[], ["6:00"], ["24:00"], ["06:60"], ["0600"], [""], ["06:00", 7],
              "06:00", None, 6]
_k_bad_accepted = [v for v in _K_INVALID if jb._valid_times(v) is not None]
_k_good_rejected = [v for v in _K_VALID if jb._valid_times(v) is None]
check(
    "_valid_times 必須擋掉畸形時段、放行合法時段",
    not _k_bad_accepted and not _k_good_rejected,
    f"誤放行={_k_bad_accepted} 誤拒絕={_k_good_rejected}",
)

# 本輪真實案例：空 times 必須【保留原值】且不得留下幽靈 next_run。
_k_cfg, _k_resp = _post_schedule({"times": []})
check(
    "POST times:[] 必須保留原時段，且 next_run 不得指向不在 times 裡的時段（幽靈排程）",
    _k_cfg["times"] == ["06:00", "22:00"] and _next_run_time_in_times(_k_cfg),
    f"times={_k_cfg['times']} next_run={_k_cfg.get('next_run')!r} "
    f"warning={(_k_resp.get('warning') or '')[:60]!r}",
)

# 每個非法輸入都要【有警告】—— 「未變更」如果沒有人說出來，就是靜默失敗。
_k_missing_warn = []
for _payload in ({"times": []}, {"times": ["25:00"]}, {"mode": "bogus"},
                 {"interval_hours": 0}, {"interval_hours": "x"}):
    _c, _r = _post_schedule(_payload)
    if not _r.get("warning"):
        _k_missing_warn.append(_payload)
check("非法輸入必須回報 warning（不得靜默忽略）",
      not _k_missing_warn, f"沒有 warning 的={_k_missing_warn}")

# 反向：合法的輸入必須真的生效，否則上面的檢查可以靠「什麼都不做」通過。
_k_cfg2, _k_resp2 = _post_schedule({"times": ["07:30", "19:30"]})
_k_cfg3, _ = _post_schedule({"mode": "interval", "interval_hours": 4})
check(
    "合法輸入仍須生效（否則上面幾項可靠「全部拒絕」空轉通過）",
    _k_cfg2["times"] == ["07:30", "19:30"] and _next_run_time_in_times(_k_cfg2)
    and not _k_resp2.get("warning")
    and _k_cfg3["mode"] == "interval" and _k_cfg3["interval_hours"] == 4,
    f"times={_k_cfg2['times']} next_run={_k_cfg2.get('next_run')!r} "
    f"warning={_k_resp2.get('warning')!r} mode={_k_cfg3['mode']} "
    f"interval_hours={_k_cfg3['interval_hours']}",
)

# ── 非物件的主體 ──────────────────────────────────────────────────────────────
# `request.get_json()` 對 `null` 回 None、對壞 JSON 也可能回 None。舊碼的
# `"enabled" in data` 於是 TypeError → HTTP 500。500 是大聲的，但 400 才是這個
# 請求真正的意思，而且 500 會把 traceback 印進日誌、看起來像程式壞了。
_k_status = {}
for _body in (None, [], "x", 6):
    _c, _r = _post_schedule(_body)
    _k_status[repr(_body)] = _r.get("__status__")
check(
    "非 JSON 物件的主體必須回 400（不是帶著 traceback 的 500）",
    all(v == 400 for v in _k_status.values()) and len(_k_status) == 4,
    f"狀態碼={_k_status}",
)

# ── `enabled` 必須是真正的 bool ───────────────────────────────────────────────
# `bool("false")` 是 True。舊碼照單全收，於是「字串 'true'」會被當成「打開排程」。
# 這裡刻意把 INTERNAL_SCHEDULER 設成 True（＝回復舊制那個組態），否則第三道鎖會
# 擋下所有 enabled=True，讓這項檢查變成在測鎖、而不是在測型別驗證。
_k_str_cfg, _k_str_resp = _post_schedule({"enabled": "true"}, internal=True,
                                         enabled=False)
_k_bool_cfg, _k_bool_resp = _post_schedule({"enabled": True}, internal=True,
                                           enabled=False)
check(
    "enabled 必須是真正的 bool：字串 'true' 不得把排程打開（bool('false') 是 True）",
    _k_str_cfg["enabled"] is False and _k_str_resp.get("warning")
    # 正向控制：真 bool 在同一個組態下【必須】生效，否則本項可靠「一律拒絕」空轉通過
    and _k_bool_cfg["enabled"] is True and not _k_bool_resp.get("warning"),
    f"字串→enabled={_k_str_cfg['enabled']} warning={_k_str_resp.get('warning')!r} "
    f"真bool→enabled={_k_bool_cfg['enabled']} warning={_k_bool_resp.get('warning')!r}",
)

# ═══ 結果 ════════════════════════════════════════════════════════════════════
print()
if FAILURES:
    print(f"❌ {len(FAILURES)} 項失敗：")
    for f in FAILURES:
        print(f"   - {f}")
    sys.exit(1)
print("✅ 全數通過")
sys.exit(0)
