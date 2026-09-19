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


def _noop_kill_external(*_a, **_k):
    """背景 watchdog 專用：這個行程永遠不對外部的掃描動手。"""
    return None


jb.kill_stalled_external = _noop_kill_external

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
# 時好時壞 —— 不穩定的測試比沒有測試更糟）。用 `exec -a` 改 argv[0]，
# 做出一個 cmdline 含標記、而且持續存在的行程。
decoy = subprocess.Popen(["bash", "-c", "exec -a linkedin_job_search.py sleep 600"])
time.sleep(0.5)          # 等 exec 完成，cmdline 才是最終內容
check("cmdline 含爬蟲名者才判定為我們的掃描",
      jb._pid_is_our_scan(decoy.pid) is True,
      f"cmdline={open(f'/proc/{decoy.pid}/cmdline','rb').read().decode('utf-8','replace')!r}")
check("不存在的 pid 必須回 False（不確定就不動手）", jb._pid_is_our_scan(999999) is False)
decoy.kill()
decoy.wait()

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
with mock.patch.object(jb, "read_jobscan_state",
                       return_value={"phase": "running", "pid": bystander.pid,
                                     "run_id": "recycled", "trigger": "systemd-timer"}):
    with mock.patch.object(jb, "_external_idle_seconds",
                           return_value=jb.SEARCH_STALL_TIMEOUT + 100):
        with mock.patch.object(jb, "_we_hold_scan_lock", return_value=False):
            killed2 = _REAL_KILL_EXTERNAL()
check("phase=running 但 cmdline 不符時仍須拒絕", killed2 is None, f"回傳={killed2!r}")
check("旁觀者仍未被殺", bystander.poll() is None)
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

# ═══ 結果 ════════════════════════════════════════════════════════════════════
print()
if FAILURES:
    print(f"❌ {len(FAILURES)} 項失敗：")
    for f in FAILURES:
        print(f"   - {f}")
    sys.exit(1)
print("✅ 全數通過")
sys.exit(0)
