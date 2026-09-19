#!/usr/bin/env python3
"""
Kanban Job Board — Chrome web UI for embedded job search results.

Usage:
    source .venv/bin/activate
    pip install flask
    python job_board.py

    Then open Chrome → http://127.0.0.1:5000
    (On a remote host, reach it through an SSH tunnel:
     ssh -N -L 5000:127.0.0.1:5000 user@host — then browse 127.0.0.1:5000)

Features:
  - Full-width job cards with compact status controls
  - Status filter tabs (New / Applied / Interview / Offer / Rejected)
  - 📂 Historical file browser — switch between past search results
  - 💾 Cross-file status persistence — Applied/Interview status survives
    across search runs (saved to .job_statuses.json by job URL)
  - 💻 4 job boards: LinkedIn, Indeed, Google, and Seek (via custom scraper)
  - Relevance-tier color coding, embedded depth badges
  - Filter by tier, career track, source + keyword search
  - 🔍 Re-search button — triggers linkedin_job_search.py live
  - ⏰ Auto-scheduler — configurable interval, background thread
"""

import json
import glob
import fcntl
import os
import signal
import sys
import subprocess
import threading
import time
from datetime import datetime, timedelta

from flask import Flask, jsonify, request

# ── Config ───────────────────────────────────────────────────────────────────
# Bind address is env-overridable so a remote deployment can restrict itself to
# loopback (JOB_BOARD_HOST=127.0.0.1) without editing code. The default is kept
# at 0.0.0.0 so existing LAN usage is unchanged.
HOST = os.environ.get("JOB_BOARD_HOST", "0.0.0.0")
PORT = int(os.environ.get("JOB_BOARD_PORT", "5000"))
# Optional label appended to the dashboard title, so two instances (say a local
# one and a VPS one) can be told apart when both are open in browser tabs —
# otherwise the tab strip shows identical titles for different data.
# Empty by default, so existing single-machine usage is unchanged.
LABEL = os.environ.get("JOB_BOARD_LABEL", "").strip()
BOARD_TITLE = f"Embedded Job Board ({LABEL})" if LABEL else "Embedded Job Board"
SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
os.chdir(SCRIPT_DIR)

SCHEDULE_FILE = os.path.join(SCRIPT_DIR, ".job_board_schedule.json")
STATUS_FILE   = os.path.join(SCRIPT_DIR, ".job_statuses.json")

# 看板結果檔實際存放的子目錄。爬蟲寫在這裡，但前端只知道 basename —— 這個落差
# 就是 2026-09-19「用 ?file= 指定檔案會把看板清空」的原因，見 _resolve_kanban_path()。
KANBAN_DIR = "search_results"
KANBAN_PREFIX = "kanban_jobs_"

# 資料超過這個秒數沒更新就示警。排程是 06:00 與 22:00，最長的正常間隔是 16 小時，
# 取 20 小時可在「至少漏掉一個時段」時才亮燈，避免正常運作下誤報。
DATA_STALE_SECONDS = 20 * 3600

# ── Scheduler ownership ──────────────────────────────────────────────────────
# 2026-09-19：搜尋排程的主權已移交 systemd（deploy/jobscan.timer）。原因是內建
# 排程器的補跑視窗是硬性 6 小時，機器休眠到中午過後才醒，當天的早上掃描就會被
# 整天跳過；而這台 VM 是跟著宿主機一起被 suspend、無法自行醒來（所以
# WakeSystem= 無效），唯一可行的是「醒來後補跑」，那正是 systemd 的
# Persistent=true 的原生行為。
#
# 內建排程器【停用但保留】：程式碼與 watchdog 都還在，UI 的 🔍 Re-Search 仍共用
# start_search()。要救回內建排程只需在 jobboard.service 加
# Environment=JOB_BOARD_INTERNAL_SCHEDULER=1 後重啟，不必改任何程式碼。
INTERNAL_SCHEDULER = os.environ.get("JOB_BOARD_INTERNAL_SCHEDULER", "0") == "1"

# 掃描狀態的空檔（由 run_scan.sh 維護，看板只讀）。看板靠它們「看見」由 systemd
# timer 或手動啟動的外部爬蟲 —— 那時 SEARCH_PROCESS 是 None，光看行程內狀態
# 會誤判成「沒在跑」。
LOG_DIR       = os.path.join(SCRIPT_DIR, "logs")
JOBSCAN_LOCK  = os.path.join(LOG_DIR, "jobscan.lock")
JOBSCAN_STATE = os.path.join(LOG_DIR, "search_state.json")
JOBSCAN_LIVE  = os.path.join(LOG_DIR, "search_current.log")

app = Flask(__name__)

# ── Status definitions ───────────────────────────────────────────────────────
STATUSES = [
    {"id": "New",       "emoji": "📥", "label": "New"},
    {"id": "Applied",   "emoji": "📝", "label": "Applied"},
    {"id": "Interview", "emoji": "📞", "label": "Interview"},
    {"id": "Offer",     "emoji": "🎉", "label": "Offer"},
    {"id": "Rejected",  "emoji": "❌", "label": "Rejected"},
]

TIER_COLORS = {
    "🔥🔥 Bare-Metal Gold":      "#fbbf24",
    "🔥 Strong Match":           "#f97316",
    "✅ Good Match":             "#22c55e",
    "⚠️ Possible Match":         "#eab308",
    "🤔 Weak Signal":            "#9ca3af",
    "❌ IT Noise / Irrelevant":  "#6b7280",
}
TIER_ORDER = list(TIER_COLORS.keys())

# ═══════════════════════════════════════════════════════════════════════════════
# Data: kanban files + global status persistence
# ═══════════════════════════════════════════════════════════════════════════════

def list_kanban_files():
    """Return all kanban JSON files sorted newest-first with metadata."""
    files = []
    for subdir in [KANBAN_DIR, ""]:
        pattern = os.path.join(SCRIPT_DIR, subdir, f"{KANBAN_PREFIX}*.json")
        for f in glob.glob(pattern):
            name = os.path.relpath(f, SCRIPT_DIR)
            mtime = datetime.fromtimestamp(os.path.getmtime(f))
            # Parse timestamp from filename like kanban_jobs_20260629_1435.json
            ts_match = os.path.basename(f).replace(KANBAN_PREFIX, "").replace(".json", "")
            try:
                file_ts = datetime.strptime(ts_match, "%Y%m%d_%H%M")
                ts_display = file_ts.strftime("%m/%d %H:%M")
            except ValueError:
                ts_display = mtime.strftime("%m/%d %H:%M")
            files.append({
                "filename": os.path.basename(f),
                "path": name,
                "mtime": mtime.isoformat(),
                "display": f"{ts_display} — {os.path.basename(f)}",
                "ts": ts_display,
            })
    files.sort(key=lambda x: x["mtime"], reverse=True)
    return files


def load_global_statuses():
    """Load cross-file status persistence (keyed by job URL)."""
    if os.path.exists(STATUS_FILE):
        try:
            with open(STATUS_FILE, "r") as f:
                return json.load(f)
        except (json.JSONDecodeError, IOError):
            pass
    return {}


def save_global_status(url, status, notes=""):
    """Update one entry in the global status file."""
    all_statuses = load_global_statuses()
    all_statuses[url] = {
        "status": status,
        "notes": notes,
        "updated_at": datetime.now().isoformat(),
    }
    with open(STATUS_FILE, "w") as f:
        json.dump(all_statuses, f, indent=2)


def merge_statuses(jobs):
    """Overlay global statuses onto loaded jobs (matched by URL)."""
    gs = load_global_statuses()
    for j in jobs:
        url = j.get("url", "")
        if url in gs:
            j["status"] = gs[url].get("status", "New")
            j["notes"] = gs[url].get("notes", "")
        else:
            j.setdefault("status", "New")
            j.setdefault("notes", "")
    return jobs


def find_latest_kanban():
    files = list_kanban_files()
    if not files:
        return None
    return os.path.join(SCRIPT_DIR, files[0]["path"])


def _safe_kanban_basename(file_path):
    """把外部可控的檔名驗證成純 basename；不合法回 None。

    這是安全邊界。`/api/jobs?file=` 是外部輸入，未經驗證就丟進 os.path.join
    可以用 "../../" 讀到 SCRIPT_DIR 以外的任意 .json。只接受兩種形式：純檔名，
    或 search_results/ 前綴（相容既有連結），且必須符合 kanban_jobs_*.json。
    """
    if not file_path or not isinstance(file_path, str):
        return None
    base = os.path.basename(file_path)
    if file_path not in (base, f"{KANBAN_DIR}/{base}"):
        return None
    if not (base.startswith(KANBAN_PREFIX) and base.endswith(".json")):
        return None
    return base


def _resolve_kanban_path(file_path):
    """把檔名解析成實際存在的看板檔絕對路徑；找不到回 None。

    前端送的是 basename，但檔案實際在 search_results/ 底下。舊版只做
    os.path.join(SCRIPT_DIR, basename) → 找不到 → 回 ([], None) →
    CURRENT_FILE 變 None → 之後任何 PATCH 都 500。2026-09-19 10:58 的 log
    就是這樣：使用者從下拉選單選了檔案，看板直接變 0 筆。
    """
    base = _safe_kanban_basename(file_path)
    if base is None:
        return None
    for sub in (KANBAN_DIR, ""):
        cand = os.path.join(SCRIPT_DIR, sub, base)
        if os.path.exists(cand):
            return cand
    return None


def try_load_jobs(path):
    """讀看板 JSON。成功回傳套用過 status 的 list；失敗回 None。

    刻意區分「讀不到」與「空清單」：呼叫端據此決定要不要動畫面上的資料。
    用壞資料蓋掉好資料，比暫時顯示舊資料糟得多。
    """
    try:
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
    except (json.JSONDecodeError, OSError, ValueError) as e:
        print(f"[jobs] 無法解析 {path}: {e}", flush=True)
        return None
    if not isinstance(data, list):
        print(f"[jobs] {path} 最外層不是陣列，忽略", flush=True)
        return None
    return merge_statuses(data)


def load_jobs(file_path=None):
    """Load jobs from a specific kanban file, or the latest if None.
    Merges cross-file statuses from .job_statuses.json.

    失敗時回傳 ([], None)。需要區分失敗原因的呼叫端（例如 api_jobs 要回 404
    而不是清空畫面）請改用 _resolve_kanban_path() + try_load_jobs()。
    """
    if file_path:
        full_path = _resolve_kanban_path(file_path)
    else:
        full_path = find_latest_kanban()
    if not full_path:
        return [], None
    jobs = try_load_jobs(full_path)
    if jobs is None:
        return [], None
    return jobs, os.path.basename(full_path)


def save_jobs_to_kanban(jobs, filename):
    """原子寫回看板檔（先寫 .tmp 再 os.replace）。

    非原子寫入時，正好在讀同一個檔的人（瀏覽器按 Reload、或掃描剛結束要
    自動換檔）會讀到寫到一半的 JSON 而解析失敗。os.replace 在同一檔案系統上
    是原子的，讀者只會看到舊版或新版。
    """
    base = _safe_kanban_basename(filename)
    if base is None:
        raise ValueError(f"save_jobs_to_kanban: 不合法的檔名 {filename!r}")
    path = _resolve_kanban_path(base) or os.path.join(SCRIPT_DIR, KANBAN_DIR, base)
    tmp = f"{path}.tmp.{os.getpid()}"
    try:
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(jobs, f, indent=2, ensure_ascii=False)
        os.replace(tmp, path)
    except OSError:
        # 暫存檔留著會累積，而且下一次同名寫入會覆蓋它，所以失敗時清掉。
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


CURRENT_FILE = None  # tracks which file is loaded; set by first load
JOBS, DATA_FILE = load_jobs()
CURRENT_FILE = DATA_FILE


# ═══════════════════════════════════════════════════════════════════════════════
# Background Search
# ═══════════════════════════════════════════════════════════════════════════════
SEARCH_PROCESS = None
SEARCH_OUTPUT = []
SEARCH_START_TIME = None
SEARCH_LAST_OUTPUT_AT = None   # time.monotonic()：最後一次收到子程序輸出的時刻
# 必須是 RLock 而非 Lock：_append_output() 本身會取這把鎖，而它會在
# kill_stalled_search() 已持鎖的區塊內被呼叫（要記錄「判定卡死」那行）。
# 用不可重入的 Lock 會讓 watchdog 執行緒在那一行自我死鎖 —— 而它不會拋例外，
# 所以 except 攔不到、日誌一行都不會印，watchdog 與外部掃描偵測就此永久癱瘓、
# 卡死的爬蟲永遠殺不掉、flock 永遠不放、之後每輪排程都靜默 SKIPPED。
SEARCH_LOCK = threading.RLock()

# 子程序沉默超過這個秒數就判定卡死並終止。爬蟲每跑完一個搜尋詞就會印進度，
# 正常情況下不會安靜這麼久。Jora 走 tls_client（Go 共享庫），Go 層死鎖時
# 連 Python 傳進去的 timeout_seconds 都不會生效 —— 2026-09-17 就是這樣卡了
# 近 50 小時，並讓排程器陷入每 30 秒重試一次的空轉。這裡是最後一道防線。
SEARCH_STALL_TIMEOUT = 900

# SEARCH_OUTPUT 的長度上限。前端只顯示最後 80 行，但整份緩衝區沒有上限的話，
# 一輪 48 個搜尋詞的長跑會讓記憶體無界成長。4000 行足以涵蓋任何合理的回溯需求。
OUTPUT_KEEP = 4000

# 本行程持有的掃描鎖。只有 start_search() 啟動的子程序會用到；由 timer 或手動
# 啟動的外部爬蟲，鎖在它們自己的行程裡，看板只能「觀察」不能代為釋放。
# SEARCH_LOCK_OWNER 記住鎖屬於哪個 Popen，用來防止「上一輪的 reader thread 拖到
# 現在才收尾，卻把新一輪的鎖放掉」。
SEARCH_LOCK_FD = None
SEARCH_LOCK_OWNER = None
# 取鎖的時刻（time.monotonic()）。只用來讓 reap_orphan_search_lock() 判斷
# 「持有鎖卻沒有子程序」這個不該持續的狀態已經持續多久。
SEARCH_LOCK_HELD_SINCE = None


def _lock_held(path):
    """檢查 flock 是否正被（別的行程）持有。回傳 True/False；無法判斷時 None。

    用 fcntl.flock 而不是去呼叫 flock(1)：兩者是同一把鎖，所以不需要 spawn 子程序。
    注意「檔案存在」不代表有人在跑 —— flock 是 advisory lock 且綁在 fd 上，行程
    結束時由核心自動釋放，因此永遠不會有 stale lock，不需要也不應該寫清理邏輯。
    """
    try:
        os.makedirs(LOG_DIR, exist_ok=True)
        fd = os.open(path, os.O_RDWR | os.O_CREAT, 0o644)
    except OSError:
        return None
    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        return True                       # 有人持有
    else:
        fcntl.flock(fd, fcntl.LOCK_UN)
        return False
    finally:
        os.close(fd)


def _acquire_search_lock():
    """為本行程的子程序取得掃描鎖。成功回傳 fd；已被別人持有則回傳 None。"""
    global SEARCH_LOCK_FD, SEARCH_LOCK_OWNER, SEARCH_LOCK_HELD_SINCE
    try:
        os.makedirs(LOG_DIR, exist_ok=True)
        fd = os.open(JOBSCAN_LOCK, os.O_RDWR | os.O_CREAT, 0o644)
    except OSError as e:
        print(f"[search] 無法開啟掃描鎖 {JOBSCAN_LOCK}: {e}", flush=True)
        return None
    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        os.close(fd)
        return None
    SEARCH_LOCK_FD, SEARCH_LOCK_OWNER = fd, None
    SEARCH_LOCK_HELD_SINCE = time.monotonic()
    return fd


def _release_search_lock(owner=None):
    """釋放本行程持有的掃描鎖。可重複呼叫；沒持有時是 no-op。

    owner 給定時，只有當鎖仍屬於該子程序才釋放 —— 否則一個拖到很晚才收尾的舊
    reader thread 會把新一輪搜尋的鎖放掉，等於門戶洞開。
    """
    global SEARCH_LOCK_FD, SEARCH_LOCK_OWNER, SEARCH_LOCK_HELD_SINCE
    # 「檢查 owner」與「清空」必須在同一個臨界區內。少了這道鎖，舊掃描的 reader
    # thread 可能在通過 owner 檢查之後被排程器切換掉，等 start_search() 換上
    # 新一輪的 fd 之後才醒來執行清空 —— 結果是把【新掃描的鎖】關掉，新掃描在
    # 無鎖狀態下跑，而 run_scan.sh 的 flock 此刻可以成功取鎖，兩套爬蟲並發
    # 各產一份結果檔（2026-08-13 事故的同一類別，也正是 owner guard 存在的理由）。
    # flock/close 等 syscall 留在鎖外，需要原子化的只有「檢查＋換手」。
    with SEARCH_LOCK:
        if owner is not None and SEARCH_LOCK_OWNER is not owner:
            return
        fd, SEARCH_LOCK_FD, SEARCH_LOCK_OWNER = SEARCH_LOCK_FD, None, None
        SEARCH_LOCK_HELD_SINCE = None
    if fd is None:
        return
    try:
        fcntl.flock(fd, fcntl.LOCK_UN)
    except OSError:
        pass
    try:
        os.close(fd)
    except OSError:
        pass


def start_search():
    global SEARCH_PROCESS, SEARCH_OUTPUT, SEARCH_START_TIME, SEARCH_LAST_OUTPUT_AT
    global SEARCH_LOCK_OWNER, _LAST_SCAN_SOURCE
    with SEARCH_LOCK:
        if SEARCH_PROCESS and SEARCH_PROCESS.poll() is None:
            return False, "A search is already running"
        # 上一輪的子程序已結束、但 reader thread 可能還沒收尾，鎖或許還握在我們
        # 手上。先放掉再重新取，才能正確回答「現在到底有沒有人在跑」。
        if SEARCH_PROCESS is not None:
            _release_search_lock()
        # 全機唯一的鎖：systemd timer 的 run_scan.sh 與手動 run_search.sh 也取同一把。
        if _acquire_search_lock() is None:
            return False, "A scan is already running (systemd timer or manual run)"
        script = os.path.join(SCRIPT_DIR, "linkedin_job_search.py")
        try:
            proc = subprocess.Popen(
                [sys.executable, "-u", script],
                stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                text=True, bufsize=1, cwd=SCRIPT_DIR,
                # errors="replace"：輸出是抓回來的網頁文字，遇到無法以 locale 解碼的
                # 位元組時，strict 會讓讀取端拋 UnicodeDecodeError 並帶走整個 reader
                # thread（詳見 _read_search_output 的說明）。寧可看到一個 U+FFFD，
                # 也不要一條會靜默停掉全機掃描的路徑。_follow_live_log 讀 live log
                # 用的也是 errors="replace"，這裡只是統一。
                errors="replace",
            )
        except Exception as e:
            # 鎖【已經在我們手上了】（本函式開頭呼叫的 _acquire_search_lock()；刻意
            # 不寫行號 —— 行號會隨每次編輯飄移，讀者照著找會找到無關的程式碼），Popen 失敗
            # 若不還回去，這個 fd 會一直開到行程結束，而且沒有任何機制救得回來：
            #   kill_stalled_search()   → SEARCH_PROCESS is None，直接 return
            #   kill_stalled_external() → _we_hold_scan_lock() 早退（鎖確實在我們手上）
            #   下次 start_search()     → 對同一個檔案開第二個 fd 取 flock 仍然衝突，
            #                             所以永遠回 "A scan is already running"
            # 使用者看到的是一句「有掃描在跑」的錯誤訊息，而 timer 每一輪都等 20 秒後
            # SKIPPED、exit 0、日誌一切正常 —— 正是本次遷移要根除的那種靜默資料缺口，
            # 而且是【無限期】的。實際觸發路徑：爬蟲檔被改名／刪除（run_scan.sh 有
            # [ -f "$SCRIPT" ] 檢查，這裡沒有），或 VM 在 suspend/resume 後 fork/exec
            # 失敗（EAGAIN/ENOMEM）—— 這台正是會 suspend 的 VM。
            _release_search_lock()
            print(f"[search] 無法啟動爬蟲 {script}: {e}", flush=True)
            return False, f"無法啟動爬蟲：{e}"
        # 到這裡才動狀態。上面任何一步失敗時，這些欄位都必須維持原樣，否則 UI 會
        # 顯示一個根本沒開始的掃描，而 stall 偵測會拿著假的起始時間。
        SEARCH_PROCESS = proc
        SEARCH_OUTPUT = []
        SEARCH_START_TIME = datetime.now().isoformat()
        SEARCH_LAST_OUTPUT_AT = time.monotonic()
        _LAST_SCAN_SOURCE = "own"
        # 鎖現在屬於這一輪；之後 reader thread / watchdog 才能據此安全釋放。
        SEARCH_LOCK_OWNER = proc
        t = threading.Thread(target=_read_search_output, args=(proc,), daemon=True)
        t.start()
        return True, "Search started"


def _append_output(line, when=None):
    """把一行輸出推進 SEARCH_OUTPUT，並維持長度上限。

    外部掃描（timer / 手動）與看板自己觸發的掃描共用這個緩衝區，所以「上限」
    必須在唯一的入口處理，否則長跑會讓記憶體無界成長。
    """
    with SEARCH_LOCK:
        SEARCH_OUTPUT.append(
            ((when or datetime.now()).strftime("%H:%M:%S"), line)
        )
        if len(SEARCH_OUTPUT) > OUTPUT_KEEP:
            del SEARCH_OUTPUT[:-OUTPUT_KEEP]


def _read_search_output(proc):
    """讀子程序的 stdout 直到 EOF。

    proc 由參數傳入而不是讀全域：start_search() 可能在這個執行緒還在讀的時候
    就換掉 SEARCH_PROCESS，屆時讀全域會混到新程序的輸出。
    """
    global SEARCH_LAST_OUTPUT_AT
    # try/finally 是必要的，不是防禦性裝飾。這個迴圈是唯一會呼叫
    # _release_search_lock(proc) 的地方，而流進來的是【爬蟲抓回來的網頁文字】。
    # 若 Popen 的 errors= 是預設的 strict，任何一個無法以 locale 解碼的位元組都會讓
    # 這一行拋 UnicodeDecodeError、執行緒當場死亡，永遠走不到下面的釋放。若子行程
    # 恰好已自行結束，就落進「SEARCH_PROCESS 非 None 但子行程已死」的盲區：
    # kill_stalled_search() 看 poll() 不為 None 而放棄，reap_orphan_search_lock()
    # 看 SEARCH_PROCESS 不為 None 而放棄 —— 兩邊都不管，鎖永遠不放，timer 每輪
    # SKIPPED、exit 0、日誌一切正常，全機掃描就此永久停擺（2026-09-19 審查實測）。
    try:
        for line in proc.stdout:
            _append_output(line.rstrip("\n"))
            SEARCH_LAST_OUTPUT_AT = time.monotonic()
        proc.wait()
    finally:
        # 子程序結束＝這一輪掃描結束，鎖必須立刻放掉，否則 timer 的下一輪會被擋住。
        # 放在 finally：不論是正常 EOF、解碼失敗、還是讀取途中被 kill，鎖都會還回去。
        # _release_search_lock 有 owner 檢查，重複呼叫是安全的 no-op。
        _release_search_lock(proc)


def get_search_stall_seconds():
    """子程序已沉默的秒數；未在執行中或仍正常時回傳 None。"""
    with SEARCH_LOCK:
        proc = SEARCH_PROCESS
        last = SEARCH_LAST_OUTPUT_AT
    if proc is None or proc.poll() is not None or last is None:
        return None
    idle = time.monotonic() - last
    return idle if idle > SEARCH_STALL_TIMEOUT else None


def kill_stalled_search():
    """終止卡死的子程序。回傳被終止的 PID；未達門檻或無程序時回傳 None。"""
    global SEARCH_PROCESS
    idle = get_search_stall_seconds()
    if idle is None:
        return None
    with SEARCH_LOCK:
        proc = SEARCH_PROCESS
        if proc is None or proc.poll() is not None:
            return None
        pid = proc.pid
        _append_output(f"🛑 已 {idle / 60:.1f} 分鐘無輸出，判定卡死，終止 PID {pid}")
        proc.kill()
        # 立刻放鎖：reader thread 可能正卡在 proc.stdout 的讀取上，不會那麼快收尾，
        # 拖著不放會讓 timer 的下一輪掃描無謂地被擋掉。
        _release_search_lock(proc)
    try:
        proc.wait(timeout=10)
    except subprocess.TimeoutExpired:
        pass
    return pid


# 「持有鎖但沒有子程序在跑」持續超過這個秒數，就判定為洩漏。
# 正常情況下這個狀態只存在於 start_search() 內「取鎖 → Popen」之間的微秒級窗口，
# 所以 60 秒是極寬鬆的門檻；刻意取寬是因為誤放鎖的代價（兩套爬蟲並發，重演
# 2026-08-13 事故）比多等一分鐘高。
ORPHAN_LOCK_GRACE = 60


def reap_orphan_search_lock():
    """回收「持有鎖、卻沒有【活著的】子程序」的孤兒鎖。回傳釋放的 fd；沒事回 None。

    這是最後一道防線。已知的洩漏路徑（Popen 失敗）已在 start_search() 內直接修掉，
    這裡防的是「還沒想到的那一條」。之所以值得為它加一個自癒機制，是因為這個缺陷
    類別的代價是【永久且靜默的全機掃描停擺】：timer 每輪等 20 秒後 SKIPPED、exit 0、
    日誌全部正常，只有 search_results/ 從此不再長大。

    ⚠️ 判準是「子程序還活著嗎」，不是「SEARCH_PROCESS 是不是 None」。
    2026-09-19 審查實測到這個差別會造就一個【兩邊都不管】的盲區 —— 子行程已死、
    但 SEARCH_PROCESS 仍指向它時：
        kill_stalled_search()  → get_search_stall_seconds() 看 poll() 不為 None
                                  → 回 None → 直接放棄
        本函式（修正前）        → 看 SEARCH_PROCESS 不為 None → 直接放棄
    鎖因此永遠不放。而 start_search() 裡那條「回收已死子程序」的救援路徑，只有
    使用者手動按下重新搜尋才會走到，timer 完全碰不到。審查者用一個已死的子行程
    重現了完整後果：之後每一輪 run_scan.sh 都印 SKIPPED、exit 0，state 檔停在
    上一輪的「正常結束」，從日誌與 UI 完全看不出掃描已經永久停擺。

    仍保留的保守之處：子程序只要還活著（poll() is None）就一律不碰，即使 reader
    thread 已經死了 —— 那是有主的鎖，誤放會造成兩套爬蟲並發，重演 2026-08-13 事故。
    """
    with SEARCH_LOCK:
        if SEARCH_LOCK_FD is None:
            return None
        proc = SEARCH_PROCESS
        if proc is not None and proc.poll() is None:
            return None                     # 子程序還活著 → 有主的鎖，不是孤兒
        held_since = SEARCH_LOCK_HELD_SINCE
        if held_since is None or time.monotonic() - held_since < ORPHAN_LOCK_GRACE:
            return None
        fd = SEARCH_LOCK_FD
    who = ("沒有任何子程序在跑" if proc is None
           else f"子程序 pid {proc.pid} 已結束但 SEARCH_PROCESS 未清空")
    print(
        f"[watchdog] 掃描鎖 fd={fd} 已被持有超過 {ORPHAN_LOCK_GRACE}s，"
        f"{who} —— 判定為洩漏，主動釋放以免全機排程永久停擺",
        flush=True,
    )
    _release_search_lock()
    return fd


# ═══════════════════════════════════════════════════════════════════════════════
# External scan observation
#
# 由 systemd timer 或手動 ./run_search.sh 啟動的爬蟲，對看板而言 SEARCH_PROCESS
# 是 None —— 光看行程內狀態會誤判成「沒在跑」，跑完也不會換檔，UI 直接退步。
#
# 解法：把 logs/jobscan.lock 當成全機唯一的掃描狀態來源。鎖由誰持有是唯一可信
# 的事實，因為 run_scan.sh 與看板用的是同一把鎖；再搭配 run_scan.sh 維護的
# search_state.json（身分）與 search_current.log（逐字輸出）補齊細節。
# ═══════════════════════════════════════════════════════════════════════════════
_EXTERNAL = {
    "active": False,        # 目前觀察到外部掃描正在跑
    "gen": 0,               # 跟讀世代；每次重新開始跟讀就 +1，用來丟棄過期的讀取
    "run_id": None,
    "trigger": None,        # systemd-timer / manual
    "started_at": None,
    "lines": [],            # 跟讀 search_current.log 累積的輸出
    "pending": "",          # 尚未成行的尾段（檔案正在被寫入）
    "offset": 0,
    "last_read_at": 0.0,    # time.monotonic()；沉默偵測用
    "pre_run_file": None,   # 掃描開始時使用者在看的檔（用來尊重他的選檔）
    "finished": None,       # 最近一次外部掃描的結果
}

# 最近一次掃描是看板自己觸發的還是外部的。閒置時要顯示哪一份逐字稿取決於此，
# 否則外部掃描跑完後畫面會跳回更早那次本機掃描的舊內容。
_LAST_SCAN_SOURCE = "own"


def read_jobscan_state():
    """讀 logs/search_state.json（run_scan.sh 以暫存檔 + mv 原子維護）。

    讀不到或格式不對一律回 None —— 這是輔助資訊，不該讓看板崩掉。
    """
    try:
        with open(JOBSCAN_STATE, "r", encoding="utf-8") as f:
            data = json.load(f)
    except (OSError, json.JSONDecodeError, ValueError):
        return None
    return data if isinstance(data, dict) else None


def _we_hold_scan_lock():
    """掃描鎖是否由本行程持有（＝正在跑的掃描是看板自己啟動的）。"""
    with SEARCH_LOCK:
        return SEARCH_LOCK_FD is not None


def _child_pids(pid):
    """讀 /proc/<pid>/task/<pid>/children 取得直接子行程；失敗回空清單。"""
    try:
        with open(f"/proc/{pid}/task/{pid}/children") as f:
            return [int(x) for x in f.read().split()]
    except (OSError, ValueError):
        return []


def _kill_tree(pid, sig=signal.SIGKILL, depth=0):
    """由下往上終止 pid 及其子孫，回傳實際送出訊號的 PID 清單。

    只殺 wrapper 不夠：run_scan.sh 的 python 子程序繼承了 fd 9（掃描鎖），
    殺掉 wrapper 只會留下一個孤兒繼續持有鎖，之後所有掃描永遠被擋住。
    刻意不用 os.killpg()：手動執行時 wrapper 的 pgid 就是使用者終端機的
    pgid，killpg 會連使用者的 shell 一起殺掉。
    """
    if depth > 10:
        return []
    killed = []
    for child in _child_pids(pid):
        killed.extend(_kill_tree(child, sig, depth + 1))
    try:
        os.kill(pid, sig)
        killed.append(pid)
    except OSError:
        pass
    return killed


def _external_note(text):
    """把一行診斷訊息寫進外部掃描的逐字稿（前端讀的是這一份）。"""
    with SEARCH_LOCK:
        _EXTERNAL["lines"].append((datetime.now().strftime("%H:%M:%S"), text))
        if len(_EXTERNAL["lines"]) > OUTPUT_KEEP:
            del _EXTERNAL["lines"][:-OUTPUT_KEEP]


def _external_idle_seconds():
    """外部掃描已沉默的秒數；沒在跟讀時回 None。"""
    with SEARCH_LOCK:
        if not _EXTERNAL["active"] or not _EXTERNAL["last_read_at"]:
            return None
        return time.monotonic() - _EXTERNAL["last_read_at"]


def _log(msg):
    """掃描／看門狗生命週期訊息的統一輸出：前面帶時戳。

    為什麼非要有時戳不可：這幾行是判斷「掃描結束 → 看板自動換檔」延遲的唯一線索，
    而沒有時戳就無從對齊。2026-09-19 審查指出，規劃階段的「≤15 秒換檔」判準在
    當時的日誌格式下**根本不可量測** —— 只能改用 API 輪詢另外量。job_board.log 裡
    Flask 的存取日誌本來就帶時戳，只有我們自己的 print 沒有，事故追查時看不出順序。
    """
    print(f"[{datetime.now().strftime('%H:%M:%S')}] {msg}", flush=True)


def _pid_is_our_scan(pid):
    """pid 真的是我們的掃描（run_scan.sh wrapper 或爬蟲）嗎？

    非檢查不可的理由：state 檔是【上一輪留下的】，而且 phase 已經是 finished 也照讀
    （見 kill_stalled_external 的 gating）。裡面的 pid 早就可能被系統回收給別的行程，
    拿它直接 SIGKILL 就是隨機殺掉一棵無關的行程樹。2026-09-19 審查以 WTERMSIG=9
    實測證實了這件事：受害者是一個與 jobspy 完全無關的 sleep 300。這台又是會 suspend
    的 VM，PID 回收比一般機器快。專案自己的 MINOR-10 測試也用 `flock -c 'sleep'`
    製造過 stray holder，所以這條路徑不是純理論。

    判準用 cmdline 而不是「pid 存不存在」：PID 被回收後同一個號碼會是別的行程，
    只有 cmdline 能區分。「不確定」一律回 False —— 不確定就不動手。
    """
    try:
        with open(f"/proc/{pid}/cmdline", "rb") as f:
            cmdline = f.read().decode("utf-8", "replace")
    except OSError:
        return False
    if not cmdline:
        return False        # zombie：cmdline 為空，不是可殺的目標
    return any(m in cmdline for m in ("run_scan.sh", "linkedin_job_search.py"))


def kill_stalled_external():
    """終止卡死的外部掃描。回傳被終止對象的說明；未達門檻或無對象回 None。

    看板自己啟動的掃描由 kill_stalled_search() 負責，這裡只處理鎖不在我們
    手上的情況。
    """
    # 鎖在我們手上就沒有「外部掃描」這回事（互斥）。防禦性檢查：即使
    # _EXTERNAL["active"] 因故沒被清掉，也絕不對著自己的掃描動手。
    if _we_hold_scan_lock():
        return None
    idle = _external_idle_seconds()
    if idle is None or idle <= SEARCH_STALL_TIMEOUT:
        return None
    state = read_jobscan_state() or {}

    # 身分閘門（2026-09-19 審查 MAJOR）：state 自稱「已結束」時，還持有鎖的那個人
    # 一定不是我們的掃描 —— 那是 stray holder（例如有人用 flock 手動占著、或 wrapper
    # 被 SIGKILL 後殘留），而 state 裡的 pid 已經是上一輪的遺物。放行就會拿一個可能
    # 被回收的 PID 去 SIGKILL，殺掉無關的行程樹。
    #
    # 這裡刻意選擇「寧可不殺」：誤殺是無界的傷害（可能殺掉別的服務），而不殺的代價
    # 有界 —— 真正卡死的掃描仍會被 systemd 的 TimeoutStartSec=5h 收掉，這條退路在
    # 規劃階段就已明確接受（見 DECISIONS.md 風險 #7）。phase 停在 running 的正常卡死
    # 情境（wrapper 被 SIGKILL → EXIT trap 不會執行 → state 永遠停在 running）
    # 不受影響，照樣會被這裡終止。
    if state.get("phase") != "running":
        return None

    run_id, trigger, pid = state.get("run_id"), state.get("trigger"), state.get("pid")

    # TOCTOU 收尾（MINOR-11）：上面的判定與下面的動手之間隔著讀 state 檔與組字串，
    # 而動手是不可逆的 SIGKILL。若看板恰好在這個窗口內取得掃描鎖（使用者按了
    # 重新搜尋），我們就會拿著【前一個外部掃描】留下的 run_id/trigger 去殺一個
    # 已經結束的目標 —— 最壞情況是那個 PID 已被回收，殺到無關的行程樹。
    # 送訊號前重新確認「我們仍然沒有持有鎖」。
    #
    # 誠實揭露：這把窗口從「無界（含檔案 I/O 與 subprocess 建立）」縮到微秒級，
    # 但沒有歸零 —— 要歸零得把鎖一路持有到訊號送出為止，而 systemctl 是阻塞呼叫，
    # 那會製造更嚴重的問題（整個 watchdog 卡住、連自己的掃描都放不掉）。
    if _we_hold_scan_lock():
        return None

    target = None
    if trigger == "systemd-timer":
        # 交給 systemd：只有它知道完整的 cgroup，能一次收掉 wrapper 與爬蟲。
        #
        # 動手前必須確認 unit 真的在跑。2026-09-19 實測：對【inactive】的 unit 送
        # `systemctl --user kill`，**退出碼是 0 但什麼也沒殺**。不確認就會留下
        # 「已終止 jobscan.service」這筆假成功紀錄 —— 那比不殺更糟，因為它會讓下一個
        # 追查事故的人以為停滯偵測正常運作過（正是本案要根除的那種靜默謊言）。
        # 對照：oneshot 執行期間的狀態是 activating，不是 active，兩者都要接受。
        try:
            act = subprocess.run(
                ["systemctl", "--user", "show", "jobscan.service",
                 "-p", "ActiveState", "--value"],
                capture_output=True, text=True, timeout=10,
                env={**os.environ, "LC_ALL": "C"},
            ).stdout.strip()
        except (OSError, subprocess.SubprocessError) as e:
            act = ""
            _log(f"[watchdog] 查詢 jobscan.service 狀態失敗，不接手: {e}")
        if act in ("active", "activating", "reloading"):
            try:
                r = subprocess.run(
                    ["systemctl", "--user", "kill", "--signal=SIGKILL", "jobscan.service"],
                    capture_output=True, text=True, timeout=10,
                    env={**os.environ, "LC_ALL": "C"},
                )
                if r.returncode == 0:
                    target = f"jobscan.service (run_id={run_id})"
            except (OSError, subprocess.SubprocessError) as e:
                _log(f"[watchdog] systemctl kill 失敗: {e}")
        else:
            _log(f"[watchdog] jobscan.service 目前為 {act or '未知'}，"
                 f"沒有可終止的掃描 → 改走 PID 路徑（並受 cmdline 身分檢查）")
    if target is None and isinstance(pid, int) and pid > 1 and pid != os.getpid():
        # 第二層：即使 state 自稱 running，pid 也可能在我們讀它之後被回收。送不可逆的
        # SIGKILL 前用 cmdline 確認身分，不符合就只留一行 log、不動手。
        if _pid_is_our_scan(pid):
            killed = _kill_tree(pid)
            if killed:
                target = f"PID {', '.join(str(p) for p in killed)} (run_id={run_id})"
        else:
            _log(f"[watchdog] 拒絕對 pid={pid} 動手：cmdline 不屬於 jobspy 掃描"
                 f"（state 可能來自上一輪，pid 已被回收）")
    if target is None:
        return None
    _external_note(f"🛑 外部掃描已 {idle / 60:.1f} 分鐘無輸出，判定卡死，終止 {target}")
    return target


def _reset_external_follow(state):
    """（重新）開始跟讀一次外部掃描。回傳 (run_id, trigger)。"""
    global _LAST_SCAN_SOURCE
    with SEARCH_LOCK:
        _EXTERNAL.update({
            "active": True,
            "gen": _EXTERNAL["gen"] + 1,
            "run_id": state.get("run_id"),
            "trigger": state.get("trigger") or "manual",
            "started_at": state.get("started_at"),
            "lines": [],
            "pending": "",
            "offset": 0,
            "last_read_at": time.monotonic(),
            # 掃描結束若換檔，會把使用者從他正在看的檔案上踢走。先記下來。
            "pre_run_file": CURRENT_FILE,
        })
        _LAST_SCAN_SOURCE = "external"
        return _EXTERNAL["run_id"], _EXTERNAL["trigger"]


def _external_begin():
    """偵測到有行程持有掃描鎖 → 開始跟讀。

    只採用 phase == "running" 的 state 身分。state 檔是上一輪留下的，phase 已是
    finished 時裡面的 run_id/trigger/pid 全是【過去式】，照抄會產生兩種傷害：
      (a) 日誌與 UI 冒出根本不存在的掃描事件 —— 2026-09-19 審查在生產日誌實證：
          27 分鐘前就結束的那一輪被重新「偵測到」一次，連 exit=0 都是從舊 state 抄的；
      (b) 那個 stale pid 正是 kill_stalled_external() 誤殺無關行程的燃料。
    真實身分由 _external_pump() 在 state 轉為 running 後補認 —— run_scan.sh 是
    「先取鎖、才寫 state」，那個微秒級窗口本來就設計成由 pump 接手，這裡不是新機制。
    """
    state = read_jobscan_state() or {}
    if state.get("phase") != "running":
        # 來源不明的鎖持有者（stray holder，或 state 剛好不可讀）：不套用任何過期身分。
        # started_at 用現在時間，讓 ext_id 為 None 時合成的 run_id 仍然可辨識
        # （get_search_status 會退回 f"external:{started_at}"）。
        state = {
            "trigger": "unknown",
            "run_id": None,
            "started_at": datetime.now().isoformat(),
        }
    rid, trig = _reset_external_follow(state)
    _log(f"[jobscan] 偵測到外部掃描 run_id={rid} trigger={trig}")


def _follow_live_log():
    """把 search_current.log 自上次讀取處起的新內容收進 _EXTERNAL['lines']。"""
    try:
        size = os.path.getsize(JOBSCAN_LIVE)
    except OSError:
        return
    with SEARCH_LOCK:
        off, gen = _EXTERNAL["offset"], _EXTERNAL["gen"]
        if size < off:
            # run_scan.sh 每輪開頭會 : > "$LIVE" 截斷，logrotate copytruncate
            # 也會。截斷後必須從 0 重讀，否則會卡在愈來愈大的 offset 上永遠
            # 讀不到東西（主控台停更但 running 仍為 true）。
            off = 0
            _EXTERNAL["offset"] = 0
            _EXTERNAL["pending"] = ""
            _EXTERNAL["lines"] = []
        if size == off:
            return
        pending = _EXTERNAL["pending"]
    try:
        with open(JOBSCAN_LIVE, "r", encoding="utf-8", errors="replace") as f:
            f.seek(off)
            chunk = f.read()
            new_off = f.tell()
    except OSError:
        return
    parts = (pending + chunk).split("\n")
    tail = parts.pop()            # 最後一段通常是半行，留到下次
    now, stamp = time.monotonic(), datetime.now().strftime("%H:%M:%S")
    with SEARCH_LOCK:
        if _EXTERNAL["gen"] != gen:
            return                # 期間已開始新的一輪，這批資料屬於舊的
        for line in parts:
            if line:
                _EXTERNAL["lines"].append((stamp, line))
        if len(_EXTERNAL["lines"]) > OUTPUT_KEEP:
            del _EXTERNAL["lines"][:-OUTPUT_KEEP]
        _EXTERNAL["pending"] = tail
        _EXTERNAL["offset"] = new_off
        if chunk:
            _EXTERNAL["last_read_at"] = now


def maybe_adopt_new_results(pre_run_file):
    """掃描結束後自動切換到最新結果檔 —— 但只在不會打斷使用者時。"""
    global JOBS, DATA_FILE, CURRENT_FILE
    latest = find_latest_kanban()
    if not latest:
        _log("[jobscan] 沒有可用的結果檔，維持現況")
        return False
    latest_name = os.path.basename(latest)
    with SEARCH_LOCK:
        viewed = CURRENT_FILE
    # 使用者在掃描期間自己換過檔 → 尊重他的選擇，不要把他拉走
    if pre_run_file and viewed != pre_run_file:
        _log(f"[jobscan] 使用者已切換到 {viewed}，不自動換檔")
        return False
    if viewed == latest_name:
        return False
    jobs = try_load_jobs(latest)
    if jobs is None:
        _log(f"[jobscan] 新結果檔 {latest_name} 無法解析，保留現有畫面")
        return False
    if not jobs:
        # 空結果（例如整輪被擋）蓋掉目前有 40 筆的看板，比不換更糟。
        _log(f"[jobscan] 新結果檔 {latest_name} 是空的，保留現有畫面")
        return False
    JOBS, DATA_FILE = jobs, latest_name
    CURRENT_FILE = latest_name
    _log(f"[jobscan] 已自動切換到 {latest_name}（{len(jobs)} 筆）")
    return True


def _external_end():
    """外部掃描結束：記錄結果，並在適當條件下自動切換到新結果檔。"""
    with SEARCH_LOCK:
        run_id, pre = _EXTERNAL["run_id"], _EXTERNAL["pre_run_file"]
        _EXTERNAL["active"] = False
        _EXTERNAL["pre_run_file"] = None
    state = read_jobscan_state() or {}
    exit_code = state.get("exit_code")
    if state.get("phase") == "finished" and state.get("run_id") == run_id:
        with SEARCH_LOCK:
            _EXTERNAL["finished"] = {
                "run_id": run_id,
                "trigger": state.get("trigger"),
                "exit_code": exit_code,
                "finished_at": state.get("finished_at"),
            }
        _log(f"[jobscan] 外部掃描結束 run_id={run_id} exit={exit_code}")
    else:
        # 鎖放掉了但 state 沒收尾 —— 可能是 wrapper 被 SIGKILL（EXIT trap 不會
        # 執行），或這一輪根本沒走到 finish()。誠實記錄，不要假裝成功。
        _log(f"[jobscan] 外部掃描結束但 state 未收尾 "
             f"(phase={state.get('phase')} run_id={state.get('run_id')} "
             f"預期={run_id})，不自動換檔")
        return
    if exit_code == 0:
        maybe_adopt_new_results(pre)


def _jobscan_watch_tick():
    if _we_hold_scan_lock():
        # 鎖在我們手上 → 互斥保證此刻不可能有外部掃描在跑，所以必須把 active 收掉，
        # 不能只是 return。否則「外部掃描結束後、下一個 tick 之前使用者按了重新搜尋」
        # 會讓 _EXTERNAL 停在 active=True 且 last_read_at 凍結在舊掃描結束那一刻；
        # 而看板自己的掃描動輒跑 19 分鐘 > SEARCH_STALL_TIMEOUT(900s)，watchdog
        # 就會拿過期的 last_read_at 判定「外部掃描卡死」並動手 —— 依舊 state 的
        # trigger 去 SIGKILL jobscan.service，或對著一個可能已被回收的 PID 送
        # SIGKILL，殺掉無關的行程樹。
        with SEARCH_LOCK:
            _EXTERNAL["active"] = False
            _EXTERNAL["last_read_at"] = None
        return                    # 自己的掃描，由 reader thread 與 watchdog 管
    held = _lock_held(JOBSCAN_LOCK)
    with SEARCH_LOCK:
        was_active = _EXTERNAL["active"]
    if held is True:
        if not was_active:
            _external_begin()
        _external_pump()
    elif held is False:
        if was_active:
            _external_end()
    # held is None：無法判斷（例如 logs/ 權限問題）→ 維持現狀，不要誤判結束


def _external_pump():
    """跟讀中的每個 tick：補上姍姍來遲的 state，並讀取新的日誌內容。

    run_scan.sh 是「先取鎖、才寫 state」，中間有極短的視窗我們會先看到鎖。
    那時 state 還是上一輪的內容，所以在這裡補認這一輪的身分。
    """
    state = read_jobscan_state() or {}
    with SEARCH_LOCK:
        cur = _EXTERNAL["run_id"]
    new_id = state.get("run_id")
    if state.get("phase") == "running" and new_id and new_id != cur:
        rid, trig = _reset_external_follow(state)
        _log(f"[jobscan] 外部掃描身分確認為 run_id={rid} trigger={trig}")
    _follow_live_log()


def _jobscan_watch_loop():
    """背景執行緒：每 5 秒檢查是否有外部掃描，並跟讀它的輸出。"""
    _log("[jobscan] External scan watcher started")
    while not SCHEDULE_STOP.is_set():
        try:
            _jobscan_watch_tick()
        except Exception as e:
            _log(f"[jobscan] watcher error: {e}")
        SCHEDULE_STOP.wait(5)


def watchdog_loop():
    """背景執行緒：定期檢查搜尋子程序（自家的與外部的）是否卡死。"""
    _log("[watchdog] Watchdog thread started")
    while not SCHEDULE_STOP.is_set():
        try:
            pid = kill_stalled_search()
            if pid is not None:
                _log(f"[watchdog] Terminated stalled search PID {pid}")
            target = kill_stalled_external()
            if target is not None:
                _log(f"[watchdog] Terminated stalled external scan: {target}")
            orphan = reap_orphan_search_lock()
            if orphan is not None:
                _log(f"[watchdog] Released orphan scan lock fd={orphan}")
        except Exception as e:
            _log(f"[watchdog] Error: {e}")
        SCHEDULE_STOP.wait(60)


def _data_age_seconds():
    """目前載入的看板檔離上次更新的秒數；無法判斷回 None。"""
    with SEARCH_LOCK:
        name = CURRENT_FILE
    path = _resolve_kanban_path(name) if name else None
    if not path:
        return None
    try:
        return time.time() - os.path.getmtime(path)
    except OSError:
        return None


def get_search_status():
    """合併「看板自己啟動的掃描」與「外部掃描」兩種狀態。

    前端只認 running/run_id/output 這幾個欄位，所以 UI 觸發與 timer 觸發在主控台
    上幾乎沒有差別；external / trigger 只是讓標題能講清楚是誰在跑。
    """
    global _LAST_SCAN_SOURCE
    with SEARCH_LOCK:
        own_running = SEARCH_PROCESS is not None and SEARCH_PROCESS.poll() is None
        own_exit = SEARCH_PROCESS.poll() if SEARCH_PROCESS else None
        own_start = SEARCH_START_TIME
        own_out, own_lines = list(SEARCH_OUTPUT[-80:]), len(SEARCH_OUTPUT)
        ext_active = _EXTERNAL["active"]
        ext_out, ext_lines = list(_EXTERNAL["lines"][-80:]), len(_EXTERNAL["lines"])
        ext_id, ext_trigger = _EXTERNAL["run_id"], _EXTERNAL["trigger"]
        ext_start, ext_finished = _EXTERNAL["started_at"], _EXTERNAL["finished"]
        ext_last = _EXTERNAL["last_read_at"]
        source = _LAST_SCAN_SOURCE
    ext_idle = (time.monotonic() - ext_last) if ext_last else None

    if own_running:
        status = {
            "running": True, "external": False, "trigger": "dashboard",
            "run_id": f"local:{own_start}", "exit_code": None,
            "start_time": own_start, "output": own_out,
            "output_lines": own_lines, "stalled": False,
        }
    elif ext_active:
        status = {
            "running": True, "external": True, "trigger": ext_trigger,
            "run_id": ext_id or f"external:{ext_start}", "exit_code": None,
            "start_time": ext_start, "output": ext_out,
            "output_lines": ext_lines,
            "stalled": bool(ext_idle and ext_idle > SEARCH_STALL_TIMEOUT),
        }
    else:
        # 閒置時仍要回報「最近一次是哪一輪」，否則前端的換檔偵測會失去依據。
        last = ext_finished if source == "external" else None
        status = {
            "running": False, "external": False, "trigger": None,
            "run_id": (last or {}).get("run_id") or (f"local:{own_start}" if own_start else None),
            "exit_code": (last or {}).get("exit_code") if last else own_exit,
            "start_time": own_start if not last else (last or {}).get("finished_at"),
            "output": ext_out if last else own_out,
            "output_lines": ext_lines if last else own_lines,
            "stalled": False,
        }
    status["last_finished"] = ext_finished
    status["timer"] = get_timer_state()
    status["data_age_seconds"] = _data_age_seconds()
    return status


# ═══════════════════════════════════════════════════════════════════════════════
# Scheduler
# ═══════════════════════════════════════════════════════════════════════════════
SCHEDULE_DEFAULT = {
    "enabled": False,
    "mode": "times",        # "times" or "interval"
    "interval_hours": 6,
    "times": ["06:00", "22:00"],  # specific times (HH:MM, 24h)
    "last_run": None,
    "next_run": None,
    "last_run_date": None,
    "_fired_today": {},     # {"2026-07-05": ["06:00", "22:00"]}
}


def load_schedule():
    if os.path.exists(SCHEDULE_FILE):
        try:
            with open(SCHEDULE_FILE) as f:
                cfg = json.load(f)
            for k, v in SCHEDULE_DEFAULT.items():
                cfg.setdefault(k, v)
            return cfg
        except (json.JSONDecodeError, IOError):
            pass
    return dict(SCHEDULE_DEFAULT)


def save_schedule(cfg):
    with open(SCHEDULE_FILE, "w") as f:
        json.dump(cfg, f, indent=2)


_TIMER_CACHE = {"at": 0.0, "value": None}


def get_timer_state():
    """查 jobscan.timer 的下次／上次觸發時間。查不到時回 {"installed": False}。

    用 `list-timers --output=json` 而不是解析人類可讀字串：JSON 裡的 next/last 是
    µs epoch 整數，不會被 locale 或時區縮寫影響。

    快取 30 秒：前端會週期性輪詢，每次都 spawn 一個 systemctl 太浪費，而排程的
    真實值本來就只以分鐘為單位變動。

    installed=False 本身就是一個靜默失敗偵測器 —— 排程跑掉的頭號原因就是 timer
    根本沒裝好，UI 直接顯示出來會比事後翻 journal 快得多。
    """
    now = time.monotonic()
    if _TIMER_CACHE["value"] is not None and now - _TIMER_CACHE["at"] < 30:
        return _TIMER_CACHE["value"]
    result = {"installed": False, "next": None, "last": None,
              "next_iso": None, "last_iso": None}
    try:
        proc = subprocess.run(
            ["systemctl", "--user", "list-timers", "jobscan.timer", "--all",
             "--output=json", "--no-pager"],
            capture_output=True, text=True, timeout=3,
            env={**os.environ, "LC_ALL": "C"},
        )
        if proc.returncode == 0 and proc.stdout.strip():
            for u in json.loads(proc.stdout):
                if u.get("unit") != "jobscan.timer":
                    continue
                result["installed"] = True
                nxt = u.get("next") or None
                result["next"] = nxt
                result["last"] = u.get("last") or None
                # ISO 8601（含 'T'）而非 "YYYY-MM-DD HH:MM:SS"：後者讓瀏覽器自己
                # 猜，Safari 會直接回 Invalid Date。
                if nxt:
                    result["next_iso"] = datetime.fromtimestamp(
                        nxt / 1_000_000).strftime("%Y-%m-%dT%H:%M:%S")
                if u.get("last"):
                    result["last_iso"] = datetime.fromtimestamp(
                        u["last"] / 1_000_000).strftime("%Y-%m-%dT%H:%M:%S")
                break
    except (OSError, subprocess.SubprocessError, ValueError) as e:
        print(f"[timer] 查詢 jobscan.timer 失敗: {e}", flush=True)
    _TIMER_CACHE["at"], _TIMER_CACHE["value"] = now, result
    return result


SCHEDULE_CONFIG = load_schedule()
SCHEDULE_STOP = threading.Event()

# 排程主權移交 systemd 後，內建排程器的 enabled 必須壓平：留著 true 會讓 UI
# 顯示成「已啟用」但實際上不會動作，而哪天閘門被打開就會直接雙軌觸發。
# 只在真的需要變更時才寫檔，避免每次啟動都無謂地改動檔案。
if not INTERNAL_SCHEDULER:
    _sched_changed = False
    if SCHEDULE_CONFIG.get("enabled"):
        SCHEDULE_CONFIG["enabled"] = False
        SCHEDULE_CONFIG["next_run"] = None
        _sched_changed = True
    # _fired_today 是內建排程器的「當日已觸發」紀錄，停用後語意不存在，一律清掉。
    # 條件刻意寫成「非空」而非「鍵存在」：load_schedule() 的 setdefault 每次載入
    # 都會把 SCHEDULE_DEFAULT 裡那個空的 {} 補回來，若照「存在就清」會導致每次
    # 啟動都判定為有變更而寫檔，白白製造 churn。
    if SCHEDULE_CONFIG.get("_fired_today"):
        SCHEDULE_CONFIG.pop("_fired_today", None)
        _sched_changed = True
    if SCHEDULE_CONFIG.get("managed_by") != "systemd-timer":
        SCHEDULE_CONFIG["managed_by"] = "systemd-timer"
        _sched_changed = True
    if _sched_changed:
        save_schedule(SCHEDULE_CONFIG)
        print("[scheduler] Internal scheduler disabled; schedule.enabled cleared "
              "(systemd timer owns the schedule)", flush=True)

# start_search() 失敗後的退避秒數。失敗幾乎都代表已經有一個搜尋在跑（或剛卡死、
# 還沒被 watchdog 清掉），此時每 30 秒重試一次對恢復毫無幫助，只會讓 log 以
# 每小時 120 行的速度膨脹 —— 2026-09-17 那次就是這樣洗了兩天多的版。
SCHEDULER_RETRY_BACKOFF = 300
_scheduler_retry_at = 0.0


def scheduler_loop():
    """Background thread: check every 30s if a scheduled search is due.

    2026-09-19：排程主權已移交 deploy/jobscan.timer，本函式預設不執行。這裡是
    直接結束執行緒，而不是每 30 秒檢查一次旗標 —— 停用後才能真的做到零喚醒、
    零 log、零 CPU。代價只是「要救回來得重啟服務」，而環境變數本來就只在啟動
    時讀取，所以那個代價本來就存在。
    """
    global _scheduler_retry_at
    if not INTERNAL_SCHEDULER:
        print("[scheduler] Internal scheduler DISABLED "
              "(deploy/jobscan.timer owns the schedule). "
              "Set JOB_BOARD_INTERNAL_SCHEDULER=1 to re-enable.", flush=True)
        return
    print("[scheduler] Scheduler thread started", flush=True)
    while not SCHEDULE_STOP.is_set():
        try:
            cfg = load_schedule()
            if cfg.get("enabled"):
                now = datetime.now()
                due = False
                mode = cfg.get("mode", "interval")

                if mode == "times":
                    times = cfg.get("times", ["06:00", "22:00"])
                    today_str = now.strftime("%Y-%m-%d")
                    # Track which (date_time) combos have already fired
                    fired_map = cfg.get("_fired_today", {})
                    already_fired = fired_map.get(today_str, [])
                    for t in times:
                        if t in already_fired:
                            continue
                        target = datetime.strptime(f"{today_str} {t}", "%Y-%m-%d %H:%M")
                        # Fire if current time has passed the scheduled time
                        # (within last 6h so a late-waking machine still catches
                        # the morning scan, while avoiding stale catch-up)
                        if now >= target and (now - target).total_seconds() < 21600:
                            due = True
                            break
                else:
                    interval = cfg.get("interval_hours", 6)
                    last = cfg.get("last_run")
                    if last:
                        if now >= datetime.fromisoformat(last) + timedelta(hours=interval):
                            due = True
                    else:
                        due = True

                if due and time.monotonic() >= _scheduler_retry_at:
                    print(f"[scheduler] Triggering search at {now.strftime('%Y-%m-%d %H:%M')}", flush=True)
                    ok, msg = start_search()
                    if not ok:
                        _scheduler_retry_at = time.monotonic() + SCHEDULER_RETRY_BACKOFF
                        print(
                            f"[scheduler] Cannot start search ({msg}); "
                            f"backing off {SCHEDULER_RETRY_BACKOFF // 60} min",
                            flush=True,
                        )
                    if ok:
                        while True:
                            st = get_search_status()
                            if not st["running"]:
                                break
                            time.sleep(5)
                        global JOBS, DATA_FILE, CURRENT_FILE
                        JOBS, DATA_FILE = load_jobs()
                        CURRENT_FILE = DATA_FILE
                        now2 = datetime.now()
                        cfg["last_run"] = now2.isoformat()
                        cfg["last_run_date"] = now2.strftime("%Y-%m-%d")
                        if mode == "times":
                            fired_map = cfg.get("_fired_today", {})
                            today_str = now2.strftime("%Y-%m-%d")
                            if today_str not in fired_map:
                                fired_map[today_str] = []
                            # Record the time that just fired
                            fired_time = now2.strftime("%H:%M")
                            for t in times:
                                target = datetime.strptime(f"{today_str} {t}", "%Y-%m-%d %H:%M")
                                if abs((now2 - target).total_seconds()) < 21600:
                                    if t not in fired_map[today_str]:
                                        fired_map[today_str].append(t)
                                    break
                            cfg["_fired_today"] = fired_map
                            # Determine next run time
                            future = [t for t in times if t > now2.strftime("%H:%M")]
                            if future:
                                next_t = future[0]
                                next_date = now2.strftime("%Y-%m-%d")
                            else:
                                # All times passed -> next run is tomorrow's first slot
                                next_t = times[0]
                                next_date = (now2 + timedelta(days=1)).strftime("%Y-%m-%d")
                            cfg["next_run"] = f"{next_date} {next_t}"
                            # Clean up entries older than 3 days
                            old_dates = [d for d in fired_map if d < (now2 - timedelta(days=3)).strftime("%Y-%m-%d")]
                            for d in old_dates:
                                del fired_map[d]
                        else:
                            cfg["next_run"] = (now2 + timedelta(hours=cfg.get("interval_hours", 6))).isoformat()
                        save_schedule(cfg)
        except Exception as e:
            print(f"[scheduler] Error: {e}", flush=True)
        SCHEDULE_STOP.wait(30)


_scheduler_thread = threading.Thread(target=scheduler_loop, daemon=True)
_scheduler_thread.start()

_watchdog_thread = threading.Thread(target=watchdog_loop, daemon=True)
_watchdog_thread.start()

# 外部掃描監看：systemd timer 或手動啟動的爬蟲不會經過 SEARCH_PROCESS，看板得
# 靠鎖與日誌自己「看見」它們，否則 UI 會回報「沒在跑」且跑完不換檔。
_jobscan_watch_thread = threading.Thread(target=_jobscan_watch_loop, daemon=True)
_jobscan_watch_thread.start()


# ═══════════════════════════════════════════════════════════════════════════════
# Routes
# ═══════════════════════════════════════════════════════════════════════════════

@app.route("/")
def index():
    # _HTML is a raw string (the page is mostly CSS/JS braces, so an f-string
    # would be a minefield), hence the sentinel substitution here.
    return _HTML.replace("__BOARD_TITLE__", BOARD_TITLE)


@app.route("/api/files")
def api_files():
    return jsonify(list_kanban_files())


@app.route("/api/jobs")
def api_jobs():
    global JOBS, DATA_FILE, CURRENT_FILE
    file_param = request.args.get("file")
    if file_param:
        # 找不到檔案時回 404 且【不動】JOBS —— 舊版會把 JOBS 換成 []，一次打錯
        # 參數就把整個看板清空，而且 CURRENT_FILE 變 None 之後 PATCH 全 500。
        path = _resolve_kanban_path(file_param)
        if path is None:
            return jsonify({"error": f"Unknown kanban file: {file_param}"}), 404
        jobs = try_load_jobs(path)
        if jobs is None:
            return jsonify({"error": f"Cannot parse kanban file: {file_param}"}), 500
        JOBS, DATA_FILE = jobs, os.path.basename(path)
        CURRENT_FILE = DATA_FILE
    return jsonify({
        "data_file": CURRENT_FILE or DATA_FILE,
        "current_file": CURRENT_FILE or DATA_FILE,
        "statuses": STATUSES,
        "tier_order": TIER_ORDER,
        "tier_colors": TIER_COLORS,
        "jobs": JOBS,
        "stats": compute_stats(),
    })


@app.route("/api/jobs/<int:job_index>", methods=["PATCH"])
def api_update_job(job_index):
    global JOBS
    if job_index < 0 or job_index >= len(JOBS):
        return jsonify({"error": "Invalid job index"}), 404
    data = request.get_json()
    new_status = data.get("status", JOBS[job_index].get("status", "New"))
    new_notes = data.get("notes", JOBS[job_index].get("notes", ""))
    JOBS[job_index]["status"] = new_status
    JOBS[job_index]["notes"] = new_notes
    # Persist to BOTH the kanban file AND the global status store
    target = CURRENT_FILE or DATA_FILE
    if not target:
        # 沒有載入任何檔案時 os.path.join(dir, None) 會直接 TypeError → 500。
        # 明確回 409，前端才知道是「沒有目標檔案」而不是伺服器壞了。
        return jsonify({"error": "No kanban file loaded"}), 409
    try:
        save_jobs_to_kanban(JOBS, target)
    except (ValueError, OSError) as e:
        return jsonify({"error": f"Failed to save: {e}"}), 500
    url = JOBS[job_index].get("url", "")
    if url:
        save_global_status(url, new_status, new_notes)
    return jsonify({"ok": True, "job": JOBS[job_index]})


@app.route("/api/reload")
def api_reload():
    global JOBS, DATA_FILE, CURRENT_FILE
    # 先試目前檢視中的檔案；它若已被輪替掉（例如 logrotate 或手動清理），
    # 退回最新的一檔，而不是把畫面清空。
    path = _resolve_kanban_path(CURRENT_FILE) or find_latest_kanban()
    if path is None:
        return jsonify({"ok": False, "error": "No kanban file available"}), 404
    jobs = try_load_jobs(path)
    if jobs is None:
        return jsonify({"ok": False, "error": f"Cannot parse {os.path.basename(path)}"}), 500
    JOBS, DATA_FILE = jobs, os.path.basename(path)
    CURRENT_FILE = DATA_FILE
    return jsonify({"ok": True, "data_file": CURRENT_FILE, "count": len(JOBS)})


@app.route("/api/search", methods=["POST"])
def api_search():
    ok, msg = start_search()
    return jsonify({"ok": ok, "message": msg})


@app.route("/api/search/status")
def api_search_status():
    return jsonify(get_search_status())


@app.route("/api/schedule", methods=["GET", "POST"])
def api_schedule():
    global SCHEDULE_CONFIG
    if request.method == "POST":
        data = request.get_json()
        refused = None
        if "enabled" in data:
            want = bool(data["enabled"])
            # 第三道鎖：即使有人繞過前端（curl、devtools），也不能把內建排程器
            # 重新武裝。兩條觸發路徑同時存在就是 2026-08-13 的並發事故。
            if want and not INTERNAL_SCHEDULER:
                refused = ("內建排程器已停用（排程由 systemd timer 負責），"
                           "enabled 未變更")
                want = False
            SCHEDULE_CONFIG["enabled"] = want
        # 只有在值*真的*變動時才清掉當日追蹤。UI 每次儲存都會把 times/mode 一起
        # 送上來，若照單全收地 pop，任何一次無關的儲存都會清空 _fired_today，
        # 讓排程器誤判當日尚未執行而立刻補跑一次。
        if "mode" in data and data["mode"] != SCHEDULE_CONFIG.get("mode"):
            SCHEDULE_CONFIG["mode"] = data["mode"]
            SCHEDULE_CONFIG.pop("_fired_today", None)  # reset tracking on mode change
        if "interval_hours" in data:
            SCHEDULE_CONFIG["interval_hours"] = int(data["interval_hours"])
        if "times" in data:
            new_times = list(data["times"])
            changed = sorted(new_times) != sorted(SCHEDULE_CONFIG.get("times") or [])
            SCHEDULE_CONFIG["times"] = new_times
            if changed:
                SCHEDULE_CONFIG.pop("_fired_today", None)  # reset tracking on time change
        if SCHEDULE_CONFIG["enabled"]:
            if SCHEDULE_CONFIG.get("mode") == "times":
                times = SCHEDULE_CONFIG.get("times", ["06:00", "22:00"])
                now = datetime.now()
                now_str = now.strftime("%H:%M")
                future = [t for t in sorted(times) if t > now_str]
                if future:
                    next_t = future[0]
                    next_date = now.strftime("%Y-%m-%d")
                else:
                    # All times passed -> next run is tomorrow's first slot
                    next_t = sorted(times)[0]
                    next_date = (now + timedelta(days=1)).strftime("%Y-%m-%d")
                SCHEDULE_CONFIG["next_run"] = f"{next_date} {next_t}"
            else:
                last = SCHEDULE_CONFIG.get("last_run")
                base = datetime.fromisoformat(last) if last else datetime.now()
                SCHEDULE_CONFIG["next_run"] = (base + timedelta(hours=SCHEDULE_CONFIG["interval_hours"])).isoformat()
        else:
            SCHEDULE_CONFIG["next_run"] = None
        save_schedule(SCHEDULE_CONFIG)
        resp = {"ok": True, "schedule": SCHEDULE_CONFIG}
        if refused:
            resp["warning"] = refused
        return jsonify(resp)
    SCHEDULE_CONFIG = load_schedule()
    payload = dict(SCHEDULE_CONFIG)
    if not INTERNAL_SCHEDULER:
        # 排程的真實來源是 systemd，這裡只覆蓋回應內容、不寫檔 —— 免得把
        # systemd 的計算結果持久化成一個會過期的假 state。
        payload["managed_by"] = "systemd-timer"
        payload["timer"] = get_timer_state()
        payload["next_run"] = payload["timer"]["next_iso"]
    return jsonify(payload)


def compute_stats():
    tiers, tracks, sources, columns = {}, {}, {}, {}
    for j in JOBS:
        t = j.get("relevance", "Unknown"); tiers[t] = tiers.get(t, 0) + 1
        tk = j.get("career_track", "Unknown"); tracks[tk] = tracks.get(tk, 0) + 1
        s = j.get("source", "Unknown"); sources[s] = sources.get(s, 0) + 1
        c = j.get("status", "New"); columns[c] = columns.get(c, 0) + 1
    return {"total": len(JOBS), "by_tier": tiers, "by_track": tracks, "by_source": sources, "by_column": columns}


# ═══════════════════════════════════════════════════════════════════════════════
# HTML / CSS / JS
# ═══════════════════════════════════════════════════════════════════════════════

_HTML = r"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>🔍 __BOARD_TITLE__</title>
<style>
*,*::before,*::after{box-sizing:border-box;margin:0;padding:0}
body{
  font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',Roboto,'Helvetica Neue',Arial,sans-serif;
  background:#0f172a;color:#e2e8f0;min-height:100vh;font-size:16px;line-height:1.5;
}

/* ── Header ──────────────────────────────────────────────────────────── */
.header{
  background:#1e293b;border-bottom:2px solid #334155;
  padding:14px 28px;display:flex;align-items:center;justify-content:space-between;
  position:sticky;top:0;z-index:100;gap:16px;flex-wrap:wrap;
}
.header h1{font-size:1.3rem;font-weight:700;}
.header .meta{font-size:0.85rem;color:#94a3b8;display:flex;align-items:center;gap:6px;flex-wrap:wrap;}
.header .meta code{color:#fbbf24;font-size:0.8rem;}
.header .meta select{
  background:#0f172a;color:#e2e8f0;border:1px solid #475569;
  padding:4px 8px;border-radius:6px;font-size:0.78rem;cursor:pointer;max-width:280px;
}
.header .btn-row{display:flex;gap:8px;align-items:center;}

/* ── Buttons ─────────────────────────────────────────────────────────── */
.btn{
  background:#334155;color:#e2e8f0;border:1px solid #475569;
  padding:7px 18px;border-radius:8px;cursor:pointer;font-size:0.85rem;
  transition:all .15s;white-space:nowrap;font-weight:500;
  display:inline-flex;align-items:center;gap:5px;
}
.btn:hover{background:#475569;}
.btn.primary{background:#6366f1;border-color:#6366f1;color:#fff;}
.btn.primary:hover{background:#4f46e5;}
.btn:disabled{opacity:0.4;cursor:not-allowed;}

/* ── Panel (collapsible) ─────────────────────────────────────────────── */
.panel{
  background:#1a2332;border-bottom:1px solid #334155;
  padding:0 28px;overflow:hidden;transition:max-height .3s;max-height:0;
}
.panel.open{max-height:500px;padding:16px 28px;}
.panel-title{font-size:0.9rem;font-weight:600;margin-bottom:8px;display:flex;align-items:center;gap:6px;}
.panel .console{
  background:#0a0f1a;color:#86efac;border:1px solid #1e3a5f;border-radius:8px;
  padding:12px 16px;font-family:'SF Mono','Fira Code','Cascadia Code',monospace;
  font-size:0.75rem;line-height:1.6;max-height:280px;overflow-y:auto;
  white-space:pre-wrap;word-break:break-all;
}
.panel .schedule-row{
  display:flex;align-items:center;gap:12px;flex-wrap:wrap;margin-top:10px;
}
.panel .schedule-row label{font-size:0.85rem;color:#94a3b8;}
.panel .schedule-row select{
  background:#0f172a;color:#e2e8f0;border:1px solid #475569;
  padding:6px 12px;border-radius:8px;font-size:0.85rem;
}

/* Toggle */
.toggle{position:relative;display:inline-block;width:48px;height:26px;}
.toggle input{opacity:0;width:0;height:0;}
.toggle .slider{position:absolute;cursor:pointer;top:0;left:0;right:0;bottom:0;background:#334155;border-radius:26px;transition:.2s;}
.toggle .slider::before{content:"";position:absolute;height:20px;width:20px;left:3px;bottom:3px;background:#94a3b8;border-radius:50%;transition:.2s;}
.toggle input:checked+.slider{background:#6366f1;}
.toggle input:checked+.slider::before{transform:translateX(22px);background:#fff;}

/* ── Toolbar ─────────────────────────────────────────────────────────── */
.toolbar{
  display:flex;gap:12px;padding:12px 28px;background:#1a2332;
  border-bottom:1px solid #334155;flex-wrap:wrap;align-items:center;
}
.toolbar label{font-size:0.8rem;color:#94a3b8;white-space:nowrap;}
.toolbar select,.toolbar input{
  background:#0f172a;color:#e2e8f0;border:1px solid #475569;
  padding:7px 12px;border-radius:8px;font-size:0.85rem;
}
.toolbar input{flex:1;min-width:200px;max-width:340px;}
.toolbar select{cursor:pointer;}
.toolbar .count{font-size:0.8rem;color:#64748b;margin-left:auto;}

/* ── Status tabs ─────────────────────────────────────────────────────── */
.status-tabs{
  display:flex;gap:6px;padding:12px 28px;background:#1a2332;
  border-bottom:1px solid #334155;flex-wrap:wrap;
}
.status-tab{
  padding:8px 18px;border-radius:20px;border:1px solid #475569;
  background:#0f172a;color:#94a3b8;cursor:pointer;font-size:0.85rem;
  transition:all .15s;white-space:nowrap;font-weight:500;
}
.status-tab:hover{background:#1e293b;color:#e2e8f0;}
.status-tab.active{background:#6366f1;border-color:#6366f1;color:#fff;}
.status-tab .badge{
  display:inline-block;background:#00000030;color:inherit;
  padding:1px 9px;border-radius:10px;font-size:0.75rem;margin-left:4px;
}

/* ── Stats bar ───────────────────────────────────────────────────────── */
.stats-bar{
  display:flex;gap:18px;padding:10px 28px;background:#151d2a;
  border-bottom:1px solid #1e293b;flex-wrap:wrap;font-size:0.8rem;color:#94a3b8;
}
.stats-bar strong{color:#e2e8f0;}
.stats-bar .sched-info{color:#fbbf24;margin-left:auto;font-size:0.78rem;}

/* ── Card list ───────────────────────────────────────────────────────── */
.card-list{padding:16px 28px;display:flex;flex-direction:column;gap:14px;}
.card{
  background:#1e293b;border:1px solid #334155;border-radius:12px;
  padding:18px 22px;transition:border-color .15s;display:flex;flex-direction:column;gap:10px;
}
.card:hover{border-color:#6366f1;}
.card.highlight{border-left:4px solid var(--tier-color,#6366f1);}
.card .row1{display:flex;align-items:baseline;gap:12px;flex-wrap:wrap;}
.card .tier-badge{
  font-size:0.75rem;font-weight:700;padding:3px 10px;border-radius:5px;white-space:nowrap;flex-shrink:0;
}
.card .job-title{font-size:1.1rem;font-weight:600;color:#f1f5f9;}
.card .company-name{font-size:0.9rem;color:#94a3b8;white-space:nowrap;}
.card .row2{display:flex;gap:16px;flex-wrap:wrap;font-size:0.85rem;color:#94a3b8;}
.card .row2 span{display:flex;align-items:center;gap:4px;}
.card .row3{display:flex;gap:6px;flex-wrap:wrap;align-items:center;}
.card .tag{font-size:0.72rem;padding:3px 9px;border-radius:4px;white-space:nowrap;font-weight:500;}
.tag-tier{background:#1e3a5f;color:#93c5fd;}
.tag-score{background:#3b1f1f;color:#fca5a5;}
.tag-purity{background:#1e2a1e;color:#86efac;}
.tag-track-ic{background:#064e3b;color:#6ee7b7;}
.tag-track-lead{background:#4a1d96;color:#c4b5fd;}
.tag-track-hybrid{background:#1e3a5f;color:#93c5fd;}
.tag-clearance-ok{background:#064e3b;color:#6ee7b7;}
.tag-clearance-blocked{background:#4a1d1d;color:#fca5a5;}
.tag-kw{background:#1e293b;color:#cbd5e1;border:1px solid #334155;}
.card .row4{display:flex;gap:8px;flex-wrap:wrap;align-items:center;justify-content:space-between;}
.card .row4 .left{display:flex;gap:8px;align-items:center;}
.card .url-btn{
  display:inline-flex;align-items:center;gap:4px;
  color:#818cf8;text-decoration:none;font-size:0.85rem;font-weight:500;
  padding:6px 12px;border-radius:6px;border:1px solid #818cf840;transition:background .15s;
}
.card .url-btn:hover{background:#818cf815;}
.card .status-btns{display:flex;gap:5px;flex-wrap:wrap;}
.card .status-btn{
  font-size:0.75rem;padding:6px 14px;border-radius:6px;
  border:1px solid #475569;background:#0f172a;color:#94a3b8;
  cursor:pointer;transition:all .15s;font-weight:500;
}
.card .status-btn:hover{background:#334155;color:#e2e8f0;border-color:#6366f1;}
.card .current-status{font-size:0.75rem;padding:6px 14px;border-radius:6px;font-weight:600;white-space:nowrap;}

/* ── Misc ────────────────────────────────────────────────────────────── */
.empty-state{text-align:center;padding:60px 20px;color:#475569;}
.empty-state .emoji{font-size:3rem;margin-bottom:12px;}
@keyframes spin{to{transform:rotate(360deg)}}
.spinner{
  display:inline-block;width:16px;height:16px;border:2px solid #475569;
  border-top-color:#818cf8;border-radius:50%;animation:spin .8s linear infinite;
  vertical-align:middle;margin-right:4px;
}
@media(max-width:700px){
  .header,.toolbar,.status-tabs,.stats-bar,.card-list,.panel{padding-left:14px;padding-right:14px;}
  .card{padding:14px 16px;}.card .job-title{font-size:1rem;}
}
</style>
</head>
<body>

<div class="header">
  <div>
    <h1>🔍 __BOARD_TITLE__</h1>
    <div class="meta">
      📂 <select id="file-selector" onchange="switchFile(this.value)"><option>Loading...</option></select>
      <span id="job-count" style="font-size:0.75rem;color:#64748b;">—</span>
      <span id="sched-badge" style="display:none;margin-left:8px;font-size:0.75rem;color:#fbbf24;">⏰ Auto</span>
      <span id="scan-chip" style="display:none;margin-left:8px;font-size:0.75rem;padding:2px 8px;border-radius:9999px;border:1px solid transparent;"></span>
    </div>
  </div>
  <div class="btn-row">
    <button class="btn" onclick="reloadCurrent()">🔄 Reload</button>
    <button class="btn primary" id="btn-search" onclick="triggerSearch()">🔍 Re-Search</button>
    <button class="btn" id="btn-schedule" onclick="toggleSchedPanel()">⏰ Schedule</button>
  </div>
</div>

<!-- Search console -->
<div class="panel" id="search-panel">
  <div class="panel-title">
    <span id="search-status-icon">🔍</span>
    <span id="search-status-text">Search not running</span>
    <span style="font-size:0.7rem;color:#64748b;" id="search-elapsed"></span>
  </div>
  <div class="console" id="search-console">Click 🔍 Re-Search to start a new job scan.</div>
</div>

<!-- Schedule -->
<div class="panel" id="sched-panel">
  <div class="panel-title">⏰ Auto-Search Schedule</div>
  <div id="sched-note" style="display:none;font-size:0.75rem;color:#94a3b8;margin-bottom:12px;line-height:1.6;">
    排程由 <b>systemd timer</b> 負責：每日 <b>06:00</b> 與 <b>22:00</b>（Melbourne 時間）。
    休眠或關機期間錯過的時段，會在機器恢復後自動補跑一次。<br>
    此面板僅供檢視。要改時間請編輯 <code>deploy/jobscan.timer</code>，再
    <code>systemctl --user daemon-reload &amp;&amp; systemctl --user restart jobscan.timer</code>。
  </div>
  <div class="schedule-row">
    <label class="toggle">
      <input type="checkbox" id="sched-enabled" onchange="updateSchedule()"><span class="slider"></span>
    </label>
    <label for="sched-enabled" style="cursor:pointer;">Enable scheduled auto-search</label>
  </div>
  <div class="schedule-row" style="margin-top:8px;">
    <label>Mode:</label>
    <select id="sched-mode" onchange="onSchedModeChange()">
      <option value="times" selected>Specific times</option>
      <option value="interval">Every N hours</option>
    </select>
    <span id="sched-times-group">
      <label style="margin-left:8px;">At:</label>
      <input type="time" id="sched-time1" value="06:00" onchange="updateSchedule()" style="background:#0f172a;color:#e2e8f0;border:1px solid #475569;padding:6px 10px;border-radius:8px;font-size:0.85rem;">
      <input type="time" id="sched-time2" value="22:00" onchange="updateSchedule()" style="background:#0f172a;color:#e2e8f0;border:1px solid #475569;padding:6px 10px;border-radius:8px;font-size:0.85rem;">
    </span>
    <span id="sched-interval-group" style="display:none;">
      <label style="margin-left:8px;">Every</label>
      <select id="sched-interval" onchange="updateSchedule()">
        <option value="3">3 hours</option><option value="6" selected>6 hours</option>
        <option value="12">12 hours</option><option value="24">24 hours</option>
      </select>
    </span>
    <span style="font-size:0.8rem;color:#64748b;" id="sched-next-run"></span>
  </div>
</div>

<div class="toolbar">
  <label>Filter:</label>
  <select id="filter-tier" onchange="renderAll()"><option value="">All Tiers</option></select>
  <select id="filter-track" onchange="renderAll()"><option value="">All Tracks</option></select>
  <select id="filter-source" onchange="renderAll()"><option value="">All Sources</option></select>
  <input type="text" id="filter-search" placeholder="🔍 Search title, company, keywords..." oninput="renderAll()">
  <span class="count" id="filter-count"></span>
</div>

<div class="status-tabs" id="status-tabs"></div>
<div class="stats-bar" id="stats-bar"></div>
<div class="card-list" id="card-list"></div>

<script>
// ═══════════════════════════════════════════════════════════════════════════
// State
// ═══════════════════════════════════════════════════════════════════════════
let JOBS=[], CURRENT_FILE='', STATUSES=[], TIER_COLORS={}, TIER_ORDER=[];
let activeStatus='All', searchPollTimer=null, pollIntervalMs=0, FILES=[];
// 換檔偵測：掃描從「在跑」變成「結束」時才重載一次，而不是每次輪詢都重載。
let sawRunning=false, runIdSeen=null, lastHandledRunId=null, consoleRunId=null;
// 排程主權在 systemd 時，面板只供檢視。
let SCHEDULE_READONLY=false;

// ═══════════════════════════════════════════════════════════════════════════
// Init
// ═══════════════════════════════════════════════════════════════════════════
async function init(){
  await loadFileList();
  await loadData();
  loadSchedule();
  // 一開始就輪詢：掃描可能是 systemd timer 啟動的，頁面若只在按下 Re-Search
  // 之後才開始輪詢，就會完全看不見那種掃描。
  ensurePolling(5000);
  pollSearchStatus();
}

async function loadFileList(){
  try{
    const r=await fetch('/api/files'); FILES=await r.json();
    const sel=document.getElementById('file-selector');
    sel.innerHTML=FILES.map((f,i)=>`<option value="${escHtml(f.path)}"${i===0?' selected':''}>${escHtml(f.display)}</option>`).join('');
  }catch(e){console.error(e);}
}

async function loadData(filePath){
  let url='/api/jobs';
  if(filePath) url+='?file='+encodeURIComponent(filePath);
  const r=await fetch(url);
  if(!r.ok){
    // 伺服器對不存在的檔案刻意回 404 且【不動】JOBS。這裡必須跟著放棄更新，
    // 否則 d.jobs 是 undefined，renderAll() 會直接把整個畫面炸掉。
    console.error('loadData failed:',r.status,await r.text());
    return;
  }
  const d=await r.json();
  JOBS=d.jobs; CURRENT_FILE=d.current_file||d.data_file; STATUSES=d.statuses;
  TIER_COLORS=d.tier_colors; TIER_ORDER=d.tier_order;
  document.getElementById('job-count').textContent=JOBS.length+' jobs';
  // Sync file selector
  const sel=document.getElementById('file-selector');
  for(let i=0;i<sel.options.length;i++){
    if(sel.options[i].value===CURRENT_FILE||FILES[i]&&FILES[i].filename===CURRENT_FILE){
      sel.value=sel.options[i].value; break;
    }
  }
  populateFilters(); renderAll();
}

// ═══════════════════════════════════════════════════════════════════════════
// File switching
// ═══════════════════════════════════════════════════════════════════════════
async function switchFile(path){
  if(!path) return;
  await loadData(path);
}

async function reloadCurrent(){
  try{
    await fetch('/api/reload');
    await loadFileList();
    await loadData(CURRENT_FILE);
  }catch(e){console.error(e);}
}

function populateFilters(){
  const byId=id=>document.getElementById(id);
  [{sel:'filter-tier',vals:TIER_ORDER},
   {sel:'filter-track',vals:[...new Set(JOBS.map(j=>j.career_track))].sort()},
   {sel:'filter-source',vals:[...new Set(JOBS.map(j=>j.source))].sort()}
  ].forEach(({sel,vals})=>{
    const el=byId(sel), cur=el.value;
    el.innerHTML='<option value="">'+({['filter-tier']:'All Tiers',['filter-track']:'All Tracks',['filter-source']:'All Sources'}[sel])+'</option>';
    vals.filter(Boolean).forEach(v=>{const o=document.createElement('option');o.value=v;o.textContent=v;el.appendChild(o);});
    el.value=cur;
  });
}

// ═══════════════════════════════════════════════════════════════════════════
// Filtering
// ═══════════════════════════════════════════════════════════════════════════
function getFiltered(){
  const tier=document.getElementById('filter-tier').value,
        track=document.getElementById('filter-track').value,
        source=document.getElementById('filter-source').value,
        search=document.getElementById('filter-search').value.toLowerCase();
  return JOBS.filter(j=>{
    if(tier && j.relevance!==tier) return false;
    if(track && j.career_track!==track) return false;
    if(source && j.source!==source) return false;
    if(activeStatus!=='All' && (j.status||'New')!==activeStatus) return false;
    if(search){
      const h=`${j.title} ${j.company} ${j.matched_on} ${j.track_detail} ${j.notes}`.toLowerCase();
      if(!h.includes(search)) return false;
    }
    return true;
  });
}

// ═══════════════════════════════════════════════════════════════════════════
// Render
// ═══════════════════════════════════════════════════════════════════════════
function renderAll(){renderStatusTabs();renderStats();renderCards();}

function renderStatusTabs(){
  const bar=document.getElementById('status-tabs');
  let counts={};
  JOBS.forEach(j=>{const s=j.status||'New'; counts[s]=(counts[s]||0)+1;});
  let html=`<div class="status-tab${activeStatus==='All'?' active':''}" onclick="setStatusFilter('All')">📋 All<span class="badge">${JOBS.length}</span></div>`;
  STATUSES.forEach(st=>{
    const c=counts[st.id]||0;
    html+=`<div class="status-tab${activeStatus===st.id?' active':''}" onclick="setStatusFilter('${st.id}')">${st.emoji} ${st.label}<span class="badge">${c}</span></div>`;
  });
  bar.innerHTML=html;
}

function setStatusFilter(s){activeStatus=s;renderAll();}

function renderStats(){
  const bar=document.getElementById('stats-bar');
  const filtered=getFiltered();
  let cols={}; filtered.forEach(j=>{const s=j.status||'New'; cols[s]=(cols[s]||0)+1;});
  bar.innerHTML=`Showing <strong>${filtered.length}</strong> of <strong>${JOBS.length}</strong> jobs`+
    STATUSES.map(st=>` · ${st.emoji} <strong>${cols[st.id]||0}</strong> ${st.label}`).join('')+
    `<span class="sched-info" id="sched-info"></span>`;
}

function renderCards(){
  const list=document.getElementById('card-list');
  const filtered=getFiltered();
  document.getElementById('filter-count').textContent=`${filtered.length} of ${JOBS.length}`;
  if(!filtered.length){
    list.innerHTML=`<div class="empty-state"><div class="emoji">📭</div><p>No jobs match the current filters</p></div>`;
    return;
  }
  let html='';
  filtered.forEach(j=>{
    const idx=JOBS.indexOf(j);
    const tierColor=TIER_COLORS[j.relevance]||'#6b7280';
    const status=j.status||'New';
    const purityPct=j.purity!=null?Math.round(j.purity*100)+'%':'';
    const trackClass=j.career_track&&j.career_track.includes('Lead')?'tag-track-lead'
                   :j.career_track&&j.career_track.includes('Hybrid')?'tag-track-hybrid':'tag-track-ic';
    const blocked=j.clearance_status&&j.clearance_status.includes('BLOCKED');
    let statusBtns='';
    STATUSES.forEach(st=>{
      if(st.id===status){
        statusBtns+=`<span class="current-status" style="background:var(--btn-color,#6366f1);color:#fff;">${st.emoji} ${st.label}</span>`;
      }else{
        statusBtns+=`<button class="status-btn" style="--btn-color:${st.id==='New'?'#6366f1':st.id==='Applied'?'#f59e0b':st.id==='Interview'?'#8b5cf6':st.id==='Offer'?'#10b981':'#6b7280'}" onclick="moveJob(${idx},'${st.id}')">${st.emoji} ${st.label}</button>`;
      }
    });
    let kwTags='';
    if(j.matched_on){
      j.matched_on.split(', ').slice(0,5).forEach(k=>{kwTags+=`<span class="tag tag-kw">#${escHtml(k)}</span>`;});
    }
    html+=`
    <div class="card highlight" style="--tier-color:${tierColor}">
      <div class="row1">
        <span class="tier-badge" style="background:${tierColor}20;color:${tierColor};border:1px solid ${tierColor}40">${escHtml(j.relevance||'?')}</span>
        <span class="job-title">${escHtml(j.title)}</span>
        <span class="company-name">🏢 ${escHtml(j.company)}</span>
      </div>
      <div class="row2">
        <span>📍 ${escHtml(j.location)}</span><span>📅 ${j.date_posted||'?'}</span><span>📎 ${escHtml(j.source||'?')}</span>
        ${j.clearance_status?`<span class="${blocked?'tag-clearance-blocked':'tag-clearance-ok'}" style="font-size:0.75rem;padding:2px 8px;border-radius:4px;">${escHtml(j.clearance_status)}</span>`:''}
      </div>
      <div class="row3">
        ${j.embedded_tier?`<span class="tag tag-tier">🏷 ${escHtml(j.embedded_tier)}</span>`:''}
        ${j.score?`<span class="tag tag-score">⭐ ${j.score}</span>`:''}
        ${purityPct?`<span class="tag tag-purity">🧪 ${purityPct}</span>`:''}
        ${j.career_track?`<span class="tag ${trackClass}">${escHtml(j.career_track)}</span>`:''}
        ${j.track_detail?`<span class="tag tag-tier">${escHtml(j.track_detail)}</span>`:''}
        ${kwTags}
        ${j.matched_on&&j.matched_on.split(', ').length>5?`<span class="tag tag-kw">+${j.matched_on.split(', ').length-5} more</span>`:''}
      </div>
      <div class="row4">
        <div class="left">
          <a class="url-btn" href="${escHtml(j.url)}" target="_blank" rel="noopener" onclick="event.stopPropagation()">🔗 View on ${escHtml(j.source||'site')} →</a>
          ${j.noise_warning && (j.relevance==='🤔 Weak Signal'||j.relevance==='❌ IT Noise / Irrelevant')?`<span style="font-size:0.7rem;color:#f87171;">⚠️ Noise: ${escHtml(j.noise_warning)}</span>`:''}
        </div>
        <div class="status-btns">${statusBtns}</div>
      </div>
    </div>`;
  });
  list.innerHTML=html;
}

function escHtml(s){if(!s)return'';return String(s).replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;').replace(/"/g,'&quot;');}

// ═══════════════════════════════════════════════════════════════════════════
// Job actions
// ═══════════════════════════════════════════════════════════════════════════
async function moveJob(idx,newStatus){
  try{
    const r=await fetch('/api/jobs/'+idx,{method:'PATCH',headers:{'Content-Type':'application/json'},body:JSON.stringify({status:newStatus})});
    if(r.ok){const d=await r.json();JOBS[idx].status=d.job.status;renderAll();}
  }catch(e){console.error(e);}
}

// ═══════════════════════════════════════════════════════════════════════════
// Search
// ═══════════════════════════════════════════════════════════════════════════
async function triggerSearch(){
  const btn=document.getElementById('btn-search'), panel=document.getElementById('search-panel');
  const cel=document.getElementById('search-console');
  btn.disabled=true; btn.innerHTML='<span class="spinner"></span> Starting...';
  panel.classList.add('open'); cel.textContent='Starting search...\n';
  document.getElementById('search-status-text').textContent='Starting...';
  try{
    const r=await fetch('/api/search',{method:'POST'}), d=await r.json();
    if(!d.ok){cel.textContent+='⚠️ '+d.message+'\n';btn.disabled=false;btn.innerHTML='🔍 Re-Search';return;}
    cel.textContent+='✅ '+d.message+'\n'; ensurePolling(2000); pollSearchStatus();
  }catch(e){cel.textContent+='❌ Error: '+e+'\n';btn.disabled=false;btn.innerHTML='🔍 Re-Search';}
}

// 掃描中縮短輪詢間隔、閒置時放長。刻意不在同一輪裡反覆重設 timer，
// 否則每次輪詢都會多打一次 API。
function ensurePolling(ms){
  if(searchPollTimer && pollIntervalMs===ms) return;
  if(searchPollTimer) clearInterval(searchPollTimer);
  pollIntervalMs=ms;
  searchPollTimer=setInterval(pollSearchStatus,ms);
}

function fmtAge(secs){
  if(secs==null) return '未知';
  if(secs<3600) return Math.floor(secs/60)+' 分鐘';
  return (secs/3600).toFixed(1)+' 小時';
}

function setChip(text,color,border){
  const c=document.getElementById('scan-chip');
  if(!text){c.style.display='none';return;}
  c.style.display='inline'; c.textContent=text;
  c.style.color=color; c.style.borderColor=border; c.style.background=border+'22';
}

async function pollSearchStatus(){
  try{
    const r=await fetch('/api/search/status'), s=await r.json();
    const cel=document.getElementById('search-console'), panel=document.getElementById('search-panel');
    const icon=document.getElementById('search-status-icon'), txt=document.getElementById('search-status-text');
    const elapsed=document.getElementById('search-elapsed'), btn=document.getElementById('btn-search');
    // 新的一輪掃描 → 主控台從頭開始，不要把上一輪的逐字稿接在後面。
    if(s.run_id!==consoleRunId){
      consoleRunId=s.run_id;
      if(s.running) cel.textContent='';
    }
    if(s.output&&s.output.length>0){
      const cur=cel.textContent.split('\n').filter(Boolean);
      s.output.forEach(l=>{if(!cur.includes(l[1])) cel.textContent+=`[${l[0]}] ${l[1]}\n`;});
      cel.scrollTop=cel.scrollHeight;
    }
    // timer 未安裝／資料過舊：這兩件事都不會讓 UI 自己壞掉，但都是排程靜默
    // 失敗的徵兆，所以在標題列直接講出來，不必事後翻 journal。
    const t=s.timer||{};
    if(!s.running&&t.installed===false){
      setChip('⚠️ jobscan.timer 未安裝','#f87171','#f87171');
    }else if(!s.running&&s.data_age_seconds>20*3600){
      setChip(`⚠️ 資料已 ${fmtAge(s.data_age_seconds)} 未更新`,'#fbbf24','#fbbf24');
    }else if(s.running){
      setChip(s.external?`⏳ 外部掃描中（${s.trigger||'系統'}）`:'⏳ 掃描中','#38bdf8','#38bdf8');
    }else{
      setChip(null);
    }
    if(s.running){
      icon.textContent='⏳';
      txt.textContent=s.external?`External scan running (${s.trigger||'system'})...`:'Search running...';
      if(s.stalled) txt.textContent+=' — 疑似卡死';
      if(s.start_time){const secs=Math.floor((Date.now()-new Date(s.start_time).getTime())/1000);elapsed.textContent=`(${Math.floor(secs/60)}m ${secs%60}s)`;}
      btn.disabled=true; btn.innerHTML='<span class="spinner"></span> Running...'; panel.classList.add('open');
      sawRunning=true; if(s.run_id) runIdSeen=s.run_id;
      ensurePolling(2000);
    }else{
      // 只有「剛從在跑變成結束」才重載，而不是每次輪詢都重載。
      if(sawRunning&&runIdSeen&&runIdSeen!==lastHandledRunId){
        lastHandledRunId=runIdSeen; sawRunning=false;
        // 走 /api/reload 而非 loadData()：伺服器可能因為「使用者自己換過檔」
        // 而刻意沒有自動換檔，重載目前檢視中的檔案才不會把使用者拉走。
        await reloadCurrent();
      }
      sawRunning=false;
      ensurePolling(5000);
      if(s.exit_code===0){
        icon.textContent='✅'; txt.textContent='Search completed!';
        if(s.output&&s.output.length>0) cel.textContent+='\n✅ Search finished.\n';
        // 換檔已經由上面的 transition 區塊用 reloadCurrent() 做掉了。這裡刻意
        // 不再呼叫 loadData()（它會載入「最新」檔），否則會蓋掉使用者自己選的
        // 檔案，也讓伺服器端「尊重使用者選檔」的判斷形同虛設。
      }else if(s.exit_code!==null){
        icon.textContent='❌'; txt.textContent='Search failed (exit '+s.exit_code+')';
      }
      elapsed.textContent=''; btn.disabled=false; btn.innerHTML='🔍 Re-Search';
    }
  }catch(e){console.error(e);}
}

// ═══════════════════════════════════════════════════════════════════════════
// Schedule
// ═══════════════════════════════════════════════════════════════════════════
function toggleSchedPanel(){document.getElementById('sched-panel').classList.toggle('open');}

function syncSchedVisibility(){
  const mode=document.getElementById('sched-mode').value;
  document.getElementById('sched-times-group').style.display=mode==='times'?'':'none';
  document.getElementById('sched-interval-group').style.display=mode==='interval'?'':'none';
}

function onSchedModeChange(){
  syncSchedVisibility();
  // 唯讀時同步切換欄位顯示，但不要 POST —— 排程主權在 systemd。
  if(!SCHEDULE_READONLY) updateSchedule();
}

function applySchedReadonly(){
  document.getElementById('sched-note').style.display=SCHEDULE_READONLY?'':'none';
  ['sched-enabled','sched-mode','sched-interval','sched-time1','sched-time2'].forEach(id=>{
    const el=document.getElementById(id);
    el.disabled=SCHEDULE_READONLY;
    el.style.opacity=SCHEDULE_READONLY?'0.5':'';
    el.style.pointerEvents=SCHEDULE_READONLY?'none':'';
  });
}

async function loadSchedule(){
  try{
    const r=await fetch('/api/schedule'), s=await r.json();
    SCHEDULE_READONLY=s.managed_by==='systemd-timer';
    document.getElementById('sched-enabled').checked=!!s.enabled;
    document.getElementById('sched-mode').value=s.mode||'times';
    document.getElementById('sched-interval').value=s.interval_hours||6;
    const times=s.times||['06:00','22:00'];
    document.getElementById('sched-time1').value=times[0]||'06:00';
    document.getElementById('sched-time2').value=times[1]||'22:00';
    syncSchedVisibility();
    applySchedReadonly();
    updateSchedDisplay(s);
  }catch(e){console.error(e);}
}

async function updateSchedule(){
  if(SCHEDULE_READONLY) return;   // 排程主權在 systemd，面板唯讀
  const mode=document.getElementById('sched-mode').value;
  const enabled=document.getElementById('sched-enabled').checked;
  const interval_hours=parseInt(document.getElementById('sched-interval').value);
  const t1=document.getElementById('sched-time1').value;
  const t2=document.getElementById('sched-time2').value;
  const times=[t1,t2].filter(Boolean).sort();
  try{
    const r=await fetch('/api/schedule',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({enabled,mode,interval_hours,times})});
    const s=await r.json(); updateSchedDisplay(s.schedule||s);
  }catch(e){console.error(e);}
}

function updateSchedDisplay(s){
  const badge=document.getElementById('sched-badge'), info=document.getElementById('sched-info');
  const nr=document.getElementById('sched-next-run');
  const fmt=iso=>new Date(iso).toLocaleString('en-AU',{month:'short',day:'numeric',hour:'2-digit',minute:'2-digit'});
  if(SCHEDULE_READONLY){
    badge.style.display='inline'; badge.textContent='⏰ systemd';
    const t=s.timer||{};
    let txt='每日 06:00 / 22:00（Melbourne）';
    if(t.installed===false) txt+=' · ⚠️ timer 未安裝，排程不會執行';
    else if(s.next_run) txt+=' · 下次 '+fmt(s.next_run);
    if(t.last_iso) txt+=' · 上次 '+fmt(t.last_iso);
    info.textContent=txt;
    nr.textContent='';
    return;
  }
  badge.textContent='⏰ Auto';
  if(s.enabled){
    badge.style.display='inline';
    const mode=s.mode||'times';
    let t=mode==='times'
      ?`Auto: at ${(s.times||['06:00','22:00']).join(' & ')} daily`
      :`Auto: every ${s.interval_hours}h`;
    if(s.next_run){const d=new Date(s.next_run);t+=` · Next: ${d.toLocaleString('en-AU',{month:'short',day:'numeric',hour:'2-digit',minute:'2-digit'})}`;}
    if(s.last_run){const d=new Date(s.last_run);t+=` · Last: ${d.toLocaleString('en-AU',{month:'short',day:'numeric',hour:'2-digit',minute:'2-digit'})}`;}
    info.textContent=t;
  }else{badge.style.display='none';info.textContent='';}
  nr.textContent=s.next_run&&s.enabled?`Next run: ${new Date(s.next_run).toLocaleString('en-AU',{month:'short',day:'numeric',hour:'2-digit',minute:'2-digit'})}`:'';
}

// Keyboard
document.addEventListener('keydown',e=>{if(e.key==='r'&&e.ctrlKey){e.preventDefault();reloadCurrent();}});

// Boot
init();
</script>
</body>
</html>"""

# ── Main ─────────────────────────────────────────────────────────────────────
if __name__ == "__main__":
    # flush=True 是必要的：stdout 進到 systemd 的 append: 日誌時是 block-buffered，
    # 不強制沖刷的話啟動訊息會卡在緩衝區裡，出問題時翻日誌什麼都看不到。
    print("=" * 60, flush=True)
    print(f"🔍 {BOARD_TITLE}", flush=True)
    print(f"   Data : {DATA_FILE or '(no kanban JSON)'}", flush=True)
    print(f"   Bind : {HOST}:{PORT}", flush=True)
    if INTERNAL_SCHEDULER:
        print(f"   Sched: 內建排程器 ON "
              f"({'每 ' + str(SCHEDULE_CONFIG.get('interval_hours', 6)) + ' 小時' if SCHEDULE_CONFIG.get('mode') == 'interval' else '每日 ' + ' / '.join(SCHEDULE_CONFIG.get('times') or [])})",
              flush=True)
    else:
        _t = get_timer_state()
        print(f"   Sched: 內建排程器 DISABLED，主權在 systemd timer "
              f"({'已安裝' if _t['installed'] else '⚠️ 未安裝'})", flush=True)
        print(f"          下次觸發: {_t['next_iso'] or '(無)'}", flush=True)
    print(f"   Scan : 外部掃描監看中（{JOBSCAN_LOCK}）", flush=True)
    print("=" * 60, flush=True)
    try:
        app.run(host=HOST, port=PORT, debug=False)
    finally:
        SCHEDULE_STOP.set()
