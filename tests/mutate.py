#!/usr/bin/env python3
"""變異測試：把每一項修正【改回去】，確認 tests/test_scan_lock.py 會 FAIL。

    .venv/bin/python tests/mutate.py

退出碼 0 = 全部變異都被逮捕（已知逃脫不算）；1 = 有變異逃脫，或有需要人看的
INCONCLUSIVE／套用失敗。

**為什麼一定要做這件事**：這個專案已經吃過兩次虧 ——
  * 第三輪：把 `kill_stalled_external()` 改成開頭 `return None`（整個功能死掉），
    39 項檢查**全數通過**。F 區每一條斷言都是「不得開火」的形式，分不出
    「正確地拒絕」與「永遠不開火」。
  * 第四輪：`errors="replace"` 與 TOCTOU 重檢拿掉之後也 39/39 全過。
  * 第五輪：H 區第一版是為「日誌黏行」寫的，結果把修正退回 print()（M11）
    之後 55/55 全過 —— **測試沒逮到它自己要保護的那個修正**。
「有修正、沒有回歸保護」是這個專案最常見的缺陷形狀，所以修正必須配一個
**會失敗的變異**。

## ⚠️ 為什麼在 /tmp 的隔離副本裡跑，而不是在原目錄（2026-09-20 第五輪 MAJOR-1）

原本的作法是直接改寫 repo 裡的 `job_board.py`、跑完再用 `git checkout` 還原。
第五輪審查指出這在**生產主機**上是危險的：15 個變異每個會在磁碟上存在 5–25 秒
（整輪約 2 分鐘），而 `jobboard.service` 是 `Restart=always` + `MemoryMax=512M`
（unit 註解本身就預期會被 OOM 殺掉）。若這段窗口內看板被殺掉而重啟，新行程載入
的**就是那個變異** —— 而變異清單裡正好有 M5（身分檢查）、M6（ActiveState 前置
檢查）、M8（停滯偵測整個 no-op）。那正是第四輪 MAJOR 的事故形狀，只是換個入口。

現在改用 `git archive HEAD` 解到暫存目錄，**生產的 `job_board.py` 從頭到尾不會
被寫入**（結束時以 sha256 驗證）。還原機制不再需要 `git checkout` ——
每一次變異都是從記憶體裡的 ORIG 重新產生，磁碟上不會殘留任何中間狀態。

歷史教訓（2026-09-19，實際發生過）：上一版中止時把 M8 留在檔案裡，而殘留檢查是
`grep -c 'MUTANT\\|if True:'` —— M8 的變異是一個裸的 `return None`，**grep 不到**。
差一點帶著「停滯偵測完全失效」的版本繼續往下做。
另外：**假變異**（改到註解、不可能改變行為）永遠不會 FAIL，看起來像「逃脫」。
看到逃脫先懷疑變異本身。
"""
import ast
import atexit
import hashlib
import os
import re
import shutil
import signal
import subprocess
import sys
import tempfile
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
PROD_TARGET = REPO / "job_board.py"
TEST_REL = "tests/test_scan_lock.py"

# 每個變異的逾時。正常一輪約 6 秒；被逮捕的變異通常更快，但留足餘裕。
TIMEOUT = 400

# 已知會逃脫、且【已經理解為什麼】的變異（用變異 id，不是標籤字串）。
#
# 空集合才是理想狀態 —— 但把「已理解的逃脫」跟「沒被發現的覆蓋缺口」混在一起
# 回報，等於讓這個工具失去訊號。所以：列在這裡的逃脫不影響退出碼，但一定會
# 印出來，而且必須在 DECISIONS.md 有對應的說明。
#
# **反過來也檢查**：如果列在這裡的變異【被逮到了】，代表這個豁免已經過期
# （有人補了測試），會印出警告要你把它刪掉 —— 免得這份清單腐化成裝飾品。
#
# M13（拿掉 _LOG_LOCK）：黏行的成因是 print() 把一行拆成兩次 write()，
#   一行只要一次 write() 就足以讓黏行結構上不可能，鎖不是 load-bearing。
#   留著它是 defence in depth，不是那個修正。
#   **這代表目前沒有任何測試能證明少了鎖會出問題**，這是已知且接受的狀態。
#   限縮前提（本地檔案、行長 < PIPE_BUF）見 job_board.py 的 _out() 上方註解。
#
# M20 在第七輪【已從這份清單移除】。舊版的理由是「那段程式碼結構上測不到：
#   要 INTERNAL_SCHEDULER=1 且跑完一輪 19 分鐘的真掃描」。審查員證明那個理由
#   不成立 —— 帳務邏輯本來就可以抽成純函式來測（現在是 _record_run()），而且
#   那段「測不到」的程式碼裡就藏著 MAJOR-1（跨午夜把隔天早上記成已觸發）。
#   M20 現在改成 _record_run() 的日期那一半，L 區逮得到它。
#   ⚠️ 留這一段的教訓：「測不到」通常只是「還沒抽出來」的另一種說法。
EXPECTED_ESCAPES = {"M13"}

# ── 要用哪個解譯器跑測試 ────────────────────────────────────────────────────
# ⚠️ 第九輪：這裡原本是 `sys.executable`，而它的意思是「**你剛好用哪個解譯器
# 啟動我**」，不是「這個專案該用哪個解譯器」。用 `python3 tests/mutate.py`
# （系統 Python）啟動時，子行程 `import job_board` → `from flask import …`
# → ModuleNotFoundError，於是【每一個】變異都以 `rc=1`、**0 個檢查**收場。
#
# 判定本身是對的（它沒把這讀成「30 個逃脫」——第八輪加的收尾標記判定救了這一輪），
# 但代價是整輪白跑，而且原因得自己猜。生產（run_scan.sh）用的是
# `$DIR/.venv/bin/python`，測試沒有理由用別的。
#
# ⚠️ 真正學到的不是「要寫 .venv/bin/python」——是**這個載具會安靜地量錯東西**。
# 下面那段基準線才是結構性的修法：先確認量尺在已知的良好樣本上讀數正確。
#
# `JOBSPY_MUTATE_PY` 是**測試鉤子**，不是設定選項（同 run_scan.sh 的 JOBSCAN_SCRIPT）。
# 它存在的唯一理由是讓「基準線守衛」本身可以被故意觸發 —— 一個觸發不了的守衛
# 等於沒有守衛，而「沒驗過的守衛」正是這個專案被燒最多次的形狀。驗法：
#     JOBSPY_MUTATE_PY=/usr/bin/python3 .venv/bin/python tests/mutate.py
# 預期：**幾秒內**以「✗ 基準線就不是全綠」中止，而不是產出一張 30 列的假表。
_VENV_PY = REPO / ".venv" / "bin" / "python"
PY = (os.environ.get("JOBSPY_MUTATE_PY")
      or (str(_VENV_PY) if _VENV_PY.is_file() else sys.executable))


def git(*args, check=True):
    return subprocess.run(["git", *args], cwd=REPO, capture_output=True,
                          text=True, check=check).stdout


def sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


# ── 前置檢查：工作區必須乾淨 ────────────────────────────────────────────────
# 判定的正確性同時取決於被測的程式【與測試本身】，所以兩個都要檢查。
# 第五輪 MINOR-4a：原本只檢查 job_board.py，測試檔未提交時產出的表格
# 別人重現不出來 —— 而這個專案的數字已經腐化過三次。
_need_clean = ["job_board.py", TEST_REL]
_dirty = [l for l in git("status", "--porcelain", *_need_clean).splitlines() if l.strip()]
if _dirty:
    sys.exit("✗ 下列檔案有未提交的改動，先 commit 再跑變異測試：\n   "
             + "\n   ".join(_dirty)
             + "\n  （否則驗證的不是 HEAD 的版本，表格別人重現不出來。）")

PROD_BEFORE = sha256(PROD_TARGET)

# ── 建立隔離副本：生產目錄從此不再被寫入 ────────────────────────────────────
WORK = Path(tempfile.mkdtemp(prefix="jobspy-mutate-"))
# ⚠️ 第九輪【自查】：清掃不能只靠下面那個 `finally` —— 它管不到 `try:` **之前**
# 的提早結束。**基準線前置檢查的 `sys.exit(2)` 正好在那之前**，而它是最常被
# 觸發的那條路（兩個探針都走它）。實測留下的孤兒：
#     drwx------ /tmp/jobspy-mutate-tuvnhdx_   12:11（那次以「基準線不可信」中止）
# 一份隔離副本 = 一份完整的 repo 複本，堆積起來還會讓人分不清哪一份是活的
# （這一輪我自己就被兩個目錄困惑過）。
#
# 用 atexit 一次關掉【整類】漏，而不是在每一條 sys.exit() 前面各補一行 ——
# 後者會隨新增的提早結束路徑而腐化，而「新增路徑時忘了補」正是這條漏的成因。
# SIGTERM / SIGINT 另有 `_cleanup_and_die()` 明確處理（atexit 不跑在訊號上）；
# 重複 rmtree 無害（ignore_errors=True）。
atexit.register(lambda: shutil.rmtree(WORK, ignore_errors=True))
try:
    _archive = subprocess.run(["git", "archive", "HEAD"], cwd=REPO,
                              capture_output=True, check=True).stdout
    subprocess.run(["tar", "-x", "-C", str(WORK)], input=_archive, check=True)
except Exception as e:  # noqa: BLE001
    shutil.rmtree(WORK, ignore_errors=True)
    sys.exit(f"✗ 無法建立隔離副本（git archive HEAD | tar -x）：{e}")

TARGET = WORK / "job_board.py"
TEST = WORK / TEST_REL
if not TARGET.is_file() or not TEST.is_file():
    shutil.rmtree(WORK, ignore_errors=True)
    sys.exit("✗ 隔離副本不完整 —— git archive 沒有解出 job_board.py 或測試檔。")
if sha256(TARGET) != PROD_BEFORE:
    shutil.rmtree(WORK, ignore_errors=True)
    sys.exit("✗ 隔離副本的 job_board.py 與工作區不一致（HEAD 與工作區不同？）")

ORIG = TARGET.read_text(encoding="utf-8")

# `JOBSPY_MUTATE_TEST` 是**測試鉤子**，不是設定選項（同 `JOBSPY_MUTATE_PY`）。
# 它存在的唯一理由是讓【基準線的兩道校準】本身可以被故意觸發 —— 一個觸發不了的
# 守衛等於沒有守衛，而「沒驗過的守衛」正是這個專案被燒最多次的形狀。
# 驗法（三個都必須【立刻】中止，且診斷訊息要指向**正確的單一原因**）：
#   A. 把某個 check() 移到條件底下 → 應該以「印出 N 項，但原始碼裡有 N+1 個」中止
#   B. 讓某個 check() 失敗        → 應該以「1 項失敗」中止（**只有這一個理由**；
#                                    rc≠0 與它同源，第九輪退回審查 NIT-1 修掉的
#                                    就是這個重複）
#   C. 跑完全過但 sys.exit(3)     → 應該以「rc=3」中止（零失敗時 rc 才是獨立訊息）
#   sed 's|^check("（承上）目標必須還活著"|if False: check("（承上）目標必須還活著"|' \
#     tests/test_scan_lock.py > /tmp/probe_test.py
#   JOBSPY_MUTATE_TEST=/tmp/probe_test.py .venv/bin/python tests/mutate.py
# A 也可以在檔案結尾（sys.exit(0) 之後）接一個 check()，那會同時證明它抓的是
# 「有呼叫點卻執行不到」而不只是「基準線被改壞」。
_TEST_OVERRIDE = os.environ.get("JOBSPY_MUTATE_TEST")
if _TEST_OVERRIDE:
    _ov = Path(_TEST_OVERRIDE)
    if not _ov.is_file():
        shutil.rmtree(WORK, ignore_errors=True)
        sys.exit(f"✗ JOBSPY_MUTATE_TEST 指向的檔案不存在：{_ov}")
    TEST.write_text(_ov.read_text(encoding="utf-8"), encoding="utf-8")

print(f"隔離副本：{WORK}")
print(f"生產目錄【不會被寫入】：{PROD_TARGET.name} sha256={PROD_BEFORE[:16]}…")
print(f"起始狀態：乾淨，{len(ORIG)} bytes")
if _TEST_OVERRIDE:
    print(f"⚠️  測試檔已被 JOBSPY_MUTATE_TEST 覆蓋：{_TEST_OVERRIDE}"
          "（只影響隔離副本；這不是正常的一輪）")
print()


def _cleanup_and_die(signum, _frame):
    shutil.rmtree(WORK, ignore_errors=True)
    if sha256(PROD_TARGET) != PROD_BEFORE:
        print(f"\n✗ 收到訊號 {signum}，且生產的 job_board.py 已被改動 —— 請立刻檢查！")
        sys.exit(2)
    print(f"\n⚠️ 收到訊號 {signum}，已移除隔離副本（生產目錄未被寫入）")
    sys.exit(130)


signal.signal(signal.SIGTERM, _cleanup_and_die)
signal.signal(signal.SIGINT, _cleanup_and_die)


def sub_once(text, old, new, label):
    n = text.count(old)
    if n != 1:
        raise SystemExit(f"錨點出現 {n} 次，預期 1 次 —— {label} 未套用")
    return text.replace(old, new)


# (id, 標籤, 變異函式)。id 與標籤分開，讓 EXPECTED_ESCAPES 綁在明確的 id 上，
# 而不是從標籤字串切出第一個詞（否則任何以 "M13" 開頭的新標籤都會被自動豁免）。
MUTATIONS = [
    ("M1", "回收判準改回 proc is not None", lambda t: sub_once(
        t, "        if proc is not None and proc.poll() is None:\n",
        "        if proc is not None:\n", "M1")),
    ("M2", "拿掉 try/finally", lambda t: sub_once(
        t, "    finally:\n        # 子程序結束＝這一輪掃描結束，鎖必須立刻放掉，否則 timer 的下一輪會被擋住。\n",
        "    except Exception:\n        raise\n    else:\n", "M2")),
    ("M3", "拿掉 errors=replace", lambda t: sub_once(
        t, '                errors="replace",\n', "", "M3")),
    ("M4", "拿掉 phase 閘門", lambda t: sub_once(
        t, '    if state.get("phase") != "running":\n        return None\n\n    run_id, trigger, pid',
        "    run_id, trigger, pid", "M4")),
    ("M5", "拿掉 cmdline 身分檢查", lambda t: sub_once(
        t, "        if _pid_is_our_scan(pid):\n", "        if True:\n", "M5")),
    ("M6", "拿掉 ActiveState 前置檢查", lambda t: sub_once(
        t, '        if act in ("active", "activating", "reloading"):\n',
        "        if True:\n", "M6")),
    # M7 原本錨在 docstring 的一句註解上 —— 那是【假變異】：改掉註解不會改變任何
    # 行為，所以它永遠不會 FAIL，也就永遠「逃脫」。要真的拿掉閘門才行。
    ("M7", "拿掉 _external_begin 身分閘門", lambda t: sub_once(
        t, '    if state.get("phase") != "running":\n        # 來源不明的鎖持有者',
        '    if False:\n        # 來源不明的鎖持有者', "M7")),
    ("M8", "kill_stalled_external 整個 no-op", lambda t: sub_once(
        t, "    if _we_hold_scan_lock():\n        return None\n    idle = _external_idle_seconds()\n",
        "    return None\n    if _we_hold_scan_lock():\n        return None\n    idle = _external_idle_seconds()\n",
        "M8")),
    ("M9", "拿掉 TOCTOU 重檢", lambda t: sub_once(
        t, "    if _we_hold_scan_lock():\n        return None\n\n    target = None\n",
        "    target = None\n", "M9")),
    ("M10", "_pid_is_our_scan 退回子字串比對", lambda t: sub_once(
        t,
        '    exe = os.path.basename(argv[0])\n    if exe == "run_scan.sh":\n        return True\n',
        '    return any(m in "\\0".join(argv) for m in ("run_scan.sh", "linkedin_job_search.py"))\n'
        '    exe = os.path.basename(argv[0])\n    if exe == "run_scan.sh":\n        return True\n',
        "M10")),

    # ── 日誌整行一次寫出（H 區）────────────────────────────────────────────
    # 三者要回答的是不同的問題：
    #   M11 是【修正前的真實行為】（print 兩次 write、無鎖）→ 必須被逮。
    #   M12 只拿掉「整行一次 write」但保留鎖。若 M12 逃脫，代表真正有效的是鎖、
    #       單次 write 只是加強；那 job_board.py 的註解就寫錯了，必須更正。
    #   M13 問的是：鎖本身是不是必要的？（單次 write 留著，只拿掉鎖）
    ("M11", "_out 退回 print()（修正前行為，無鎖）", lambda t: sub_once(
        t,
        '    with _LOG_LOCK:\n        sys.stdout.write(f"{line}\\n")\n        sys.stdout.flush()\n',
        '    print(line, flush=True)\n', "M11")),
    ("M12", "保留鎖但用 print()（兩次 write）", lambda t: sub_once(
        t,
        '    with _LOG_LOCK:\n        sys.stdout.write(f"{line}\\n")\n        sys.stdout.flush()\n',
        '    with _LOG_LOCK:\n        print(line, flush=True)\n', "M12")),
    ("M13", "拿掉鎖（保留整行一次 write）", lambda t: sub_once(
        t,
        '    with _LOG_LOCK:\n        sys.stdout.write(f"{line}\\n")\n        sys.stdout.flush()\n',
        '    sys.stdout.write(f"{line}\\n")\n    sys.stdout.flush()\n', "M13")),

    # ── 回復舊制的啟動轉移（J 區）──────────────────────────────────────────
    # 2026-09-20 實測發現這條路是靜默失敗：照 deploy/README.md 的復原程序做完，
    # 系統不會再掃描，而畫面顯示排程由一個剛被停用的 timer 管理。
    ("M14", "回復舊制的分支整個不執行（if → elif False）", lambda t: sub_once(
        t, "else:\n    # 回復到舊制（JOB_BOARD_INTERNAL_SCHEDULER=1）。這條路",
        "elif False:\n    # 回復到舊制（JOB_BOARD_INTERNAL_SCHEDULER=1）。這條路", "M14")),
    # 只拿掉 next_run 的計算。回復之後 enabled 會是 true，但面板顯示「下次執行：—」
    # 長達數小時（要等下一個時段真的跑完，排程器迴圈才會補上）。
    ("M15", "回復舊制時不算 next_run", lambda t: sub_once(
        t, '        SCHEDULE_CONFIG["next_run"] = _next\n',
        "        pass\n", "M15")),

    # ── api_schedule 的輸入驗證（K 區）────────────────────────────────────
    # 2026-09-20 第六輪審查的 m2：重構把 `times: []` 從「HTTP 500（大聲）」
    # 變成「HTTP 200 + 一則永遠不會執行的幽靈排程（靜默）」。
    ("M16", "times 不驗證（照單全收，回復幽靈排程）", lambda t: sub_once(
        t, "            new_times = _valid_times(data[\"times\"])\n            if new_times is None:\n",
        "            new_times = list(data[\"times\"])\n            if False:\n", "M16")),
    # 只把 _valid_times 的邊界判斷拿掉（什麼都放行）—— 問的是「驗證本身」有沒有被測到，
    # 與 M16 問的「呼叫端有沒有用它」是兩件事。
    ("M17", "_valid_times 不檢查內容（空清單與畸形時段都放行）", lambda t: sub_once(
        t, "    if not isinstance(value, list) or not value:\n        return None\n    for t in value:\n",
        "    if not isinstance(value, list):\n        return None\n    return list(value)\n    for t in value:\n",
        "M17")),
    # 非 JSON 物件的主體：`null`／`[]`／`"x"` → 舊碼 `"enabled" in data` 會 TypeError。
    ("M18", "拿掉主體型別檢查（非物件主體回到 500）", lambda t: sub_once(
        t, "        if not isinstance(data, dict):\n", "        if False:\n", "M18")),
    # `bool("false")` 是 True：字串 enabled 會把「關掉排程」變成「打開排程」。
    ("M19", "enabled 用 bool() 寬鬆轉換（字串 'true' 就打開排程）", lambda t: sub_once(
        t,
        "            if not isinstance(data[\"enabled\"], bool):\n"
        "                problems.append(\"enabled 只接受 true／false，未變更\"\n"
        "                                f\"（收到 {data['enabled']!r}）\")\n"
        "            else:\n"
        "                want = data[\"enabled\"]\n",
        "            if False:\n"
        "                problems.append(\"enabled 只接受 true／false，未變更\"\n"
        "                                f\"（收到 {data['enabled']!r}）\")\n"
        "            else:\n"
        "                want = bool(data[\"enabled\"])\n",
        "M19")),
    # M20 在第七輪被【重新設計】了。舊版是把 scheduler_loop 的 `sorted(...)` 拿掉，
    # 並被列進 EXPECTED_ESCAPES，理由寫「要 INTERNAL_SCHEDULER=1 且跑完一輪 19 分鐘
    # 的真掃描才會走到，結構上測不到」。審查員證明那個理由不成立（帳務可以抽成純
    # 函式來測），而且那段程式碼裡就藏著 MAJOR-1。
    #
    # 現在 target 是 `_record_run()` 的【日期】那一半：把已觸發時段記在「跑完那天」
    # 而不是「觸發那天」。22:00 起跑、00:30 結束的掃描跨過午夜 → 記到隔天去 →
    # 隔天早上的 06:00 被判定已觸發而靜默跳過（正是 MAJOR-1 的病根之一）。
    #
    # 至於舊版那個「挑錯時段」的另一半，現在【結構上不可表達】：`_record_run()`
    # 不再推導剛剛燒掉哪個時段，它接收那個時段。「不推導」比「推導對了」強 ——
    # 所以那個方向的變異沒有對應的 target 可以改，這是刻意的。
    ("M20", "把已觸發時段記在【跑完那天】而非【觸發那天】（跨午夜記錯日期）",
     lambda t: sub_once(
         t,
         "        existing = _fired_list(cfg, fired_date)\n"
         "        if fired_time not in existing:\n"
         "            existing.append(fired_time)\n"
         "        fired_map[fired_date] = existing\n",
         "        _d = now2.strftime(\"%Y-%m-%d\")\n"
         "        existing = _fired_list(cfg, _d)\n"
         "        if fired_time not in existing:\n"
         "            existing.append(fired_time)\n"
         "        fired_map[_d] = existing\n",
         "M20")),
    # 第三道鎖：這道是三道鎖裡唯一擋得住 curl／devtools 的，到第六輪為止沒有測試。
    ("M21", "第三道鎖失效（停用中仍可用 API 重新武裝內建排程器）", lambda t: sub_once(
        t, "                if want and not INTERNAL_SCHEDULER:\n",
        "                if False:\n", "M21")),
    # ── 第七輪退回的修正，各自的變異 ──────────────────────────────────────────
    ("M22", "interval_hours 的例外處理漏掉 OverflowError（1e400→inf→500）", lambda t: sub_once(
        t, "                except (TypeError, ValueError, OverflowError):\n",
        "                except (TypeError, ValueError):\n", "M22")),
    ("M23", "_valid_times 用 len+isdigit+int 而非 ASCII regex（非 ASCII 數字）",
     lambda t: sub_once(
         t, "        if not isinstance(t, str) or not _TIME_RE.fullmatch(t):\n",
         "        if (not isinstance(t, str) or len(t) != 5 or t[2] != \":\"\n"
         "                or not (t[:2].isdigit() and t[3:].isdigit())\n"
         "                or int(t[:2]) > 23 or int(t[3:]) > 59):\n", "M23")),
    ("M24", "損壞的排程檔靜默退回預設值（不回報、不保留原檔）", lambda t: sub_once(
         t,
         "        salvage = SCHEDULE_FILE + \".corrupt\"\n",
         "        return dict(SCHEDULE_DEFAULT)\n"
         "        salvage = SCHEDULE_FILE + \".corrupt\"\n", "M24")),
    ("M25", "post-run 的 next_run 用【現在】算而不是【跑完的當下】", lambda t: sub_once(
        t, "    cfg[\"next_run\"] = _compute_next_run(cfg, now2)\n",
        "    cfg[\"next_run\"] = _compute_next_run(cfg)\n", "M25")),
    ("M26", "api_schedule 不論如何都回 ok:true（部分拒絕也宣稱成功）", lambda t: sub_once(
        t, "        resp = {\"ok\": not warning, \"schedule\": SCHEDULE_CONFIG}\n",
        "        resp = {\"ok\": True, \"schedule\": SCHEDULE_CONFIG}\n", "M26")),
    ("M27", "_parse_timer_calendar 讀不到時編一個時段出來（而不是回 None）",
     lambda t: sub_once(
         t, "    if not specs:\n        return None\n",
         "    if not specs:\n        return \"06:00 / 22:00\"\n", "M27")),

    # ── 第八輪審查的修正（MINOR-1／MINOR-2／NIT-2）─────────────────────────
    #
    # NIT-2：`_timer_next_text()` 有測試（第 83+ 項）卻【沒有對應的變異】。
    # 這個專案自己的規則寫在 deploy/README.md：「若你新增修正卻找不到會失敗的
    # 變異，代表那個修正沒有被測試覆蓋」。審查員指出我漏了這一個 ——
    # 「有測試」與「測試是 load-bearing 的」是兩件事，而只有變異能區分它們。
    ("M28", "banner 的『下次觸發』退回無條件「（無）」（否認一件會發生的事）",
     lambda t: sub_once(
         t,
         "    if t.get(\"next_iso\"):\n"
         "        return t[\"next_iso\"]\n"
         "    return \"（掃描執行中，systemd 尚未計算下一次）\" if t.get(\"installed\") else \"（無）\"\n",
         "    return t.get(\"next_iso\") or \"（無）\"\n", "M28")),

    # MINOR-1：C 區那兩個呼叫點「今天安全」靠的是 `kill_stalled_external()` 第一行
    # 的 `if _we_hold_scan_lock(): return None` —— 也就是【上游的閘門】，不是那裡
    # 自己的防護。這個變異拿掉閘門，驗證新加的 `mock.patch.object(subprocess, "run")`
    # 與 `c_syscalls == []` 斷言真的會響（而不是只在我腦中成立）。
    # ⚠️ 這個變異【一定要有 mock 才會被逮】：沒有的話它會真的對生產 unit 送出
    # SIGKILL —— 那正是 F4 當初的近失事故。它現在能被安全地測，就是 MINOR-1 的價值。
    ("M29", "拿掉 kill_stalled_external 的持鎖前置檢查（可能殺掉自己的掃描）",
     lambda t: sub_once(
         t,
         "    if _we_hold_scan_lock():\n        return None\n    idle = _external_idle_seconds()\n",
         "    idle = _external_idle_seconds()\n", "M29")),

    # MINOR-2：把消毒整個拿掉，退回「讀取端直接對值呼叫 .get()」的狀態。
    # 這正是審查員證實的那個不可達守衛 —— 症狀是每 30 秒一行的
    # `[scheduler] Error: 'list' object has no attribute 'get'`。
    # 注意這個變異是【不可達守衛】的忠實版本：不是「明著丟例外」，而是
    # 「假設值一定是 dict」—— 後者才是真實程式碼出錯的樣子。
    ("M30", "_fired_today 不消毒（讀取端直接 .get()，畸形值就 AttributeError）",
     lambda t: sub_once(
         t,
         "    raw = cfg.get(\"_fired_today\")\n"
         "    if not isinstance(raw, dict):\n"
         "        return {}\n",
         "    raw = cfg.get(\"_fired_today\")\n"
         "    if raw is None:\n"
         "        return {}\n",
         "M30")),

    # MINOR-3：把三個路徑退回【寫死】，也就是「測試傳的 JOBSCAN_* 被完全忽略」
    # 的那個版本。這正是缺陷當時的樣子 —— 不是明著報錯，而是安靜地用生產路徑。
    ("M31", "JOBSCAN_* 三個路徑退回寫死（測試的 env 隔離變成假的）",
     lambda t: sub_once(
         t,
         "JOBSCAN_LOCK  = (os.environ.get(\"JOBSCAN_LOCK\")\n"
         "                 or os.path.join(LOG_DIR, \"jobscan.lock\"))\n"
         "JOBSCAN_STATE = (os.environ.get(\"JOBSCAN_STATE\")\n"
         "                 or os.path.join(LOG_DIR, \"search_state.json\"))\n"
         "JOBSCAN_LIVE  = (os.environ.get(\"JOBSCAN_LIVE\")\n"
         "                 or os.path.join(LOG_DIR, \"search_current.log\"))\n",
         "JOBSCAN_LOCK  = os.path.join(LOG_DIR, \"jobscan.lock\")\n"
         "JOBSCAN_STATE = os.path.join(LOG_DIR, \"search_state.json\")\n"
         "JOBSCAN_LIVE  = os.path.join(LOG_DIR, \"search_current.log\")\n",
         "M31")),

    # MINOR-2 的另一半：消毒函式留著，但【呼叫端】退回原生 `.get()`。
    # 這正是「只修了一半」的樣子 —— 症狀（每 30 秒一行的 AttributeError、
    # 排程器永久停擺）完全回來，而直接測 `_fired_list()` 的那兩項照樣全綠。
    # 守著它的是 L 區的 AST 靜態不變式。
    ("M32", "呼叫端退回原生 .get(\"_fired_today\")（繞過消毒）",
     lambda t: sub_once(
         t,
         "                    already_fired = _fired_list(cfg, today_str)\n",
         "                    _fm = cfg.get(\"_fired_today\", {})\n"
         "                    already_fired = _fm.get(today_str, [])\n",
         "M32")),
]

# ── 基準線：先確認【沒被變異的】那一份是全綠的 ──────────────────────────────
# 這一步量的是「量尺本身」。如果連原始碼都跑不過，後面每一個變異的 PASS/FAIL
# 都只是同一個既有故障的回音 —— 而表格看起來一模一樣（30 列數字，長得很正常）。
#
# ⚠️ 這一輪就是這樣燒掉一次的：整張表 30 個 INCONCLUSIVE，真正的差別只在
# 啟動載具時用了 `python3` 而不是 `.venv/bin/python`。**「量到 0」和「量尺壞了」
# 在報表上長得一樣**，差別只在有沒有人先驗過量尺。
def _run_suite(test_path):
    r = subprocess.run([PY, str(test_path)], cwd=WORK,
                       capture_output=True, text=True, timeout=TIMEOUT)
    out = r.stdout + r.stderr
    return (out, len(re.findall(r"\[PASS\]", out)),
            len(re.findall(r"\[FAIL\]", out)), r.returncode)


print(f"解譯器：{PY}")

# ── 量尺的第二道校準：印出來的項數必須等於【原始碼裡的 check() 呼叫點數】 ──────
# 第九輪【審查退回】MINOR-3。上面那個「全綠」判定只證明「沒有 FAIL」，
# 不證明「每一項都跑了」。反例（審查員實測）：把 L 區那項 AST 不變式整段註解掉，
# 基準線照樣印 `✅ 全數通過`、rc=0、0 FAIL —— 前置檢查完全放行，
# 而 M32 會被判成 `❌ 逃脫（無回歸保護）`。**那不是逃脫，是量尺短了一格。**
# 載具分不出這兩者，於是把「檢查被跳過」記成「修正沒有回歸保護」，歸因錯誤，
# 文件數字跟著腐化。
#
# 判準用 AST 而不是 `grep -c 'check('` —— 同一個理由在本專案出現過很多次：
# **grep 會把註解與字串裡的 `check(` 算成呼叫**（本檔的說明文字就在引用它）。
#
# 這一項抓到的是「呼叫點還在、但沒被執行到」（被移到條件底下、被 return 跳過）。
# 抓不到的是「有人把某個 check() 整個刪掉」—— 兩邊一起變少，守衛看不出來；
# 那一種由變異表負責（少一個守衛，就會多一個逃脫）。
_AST_CHECKS = sum(
    1 for _n in ast.walk(ast.parse(TEST.read_text(encoding="utf-8")))
    if isinstance(_n, ast.Call) and isinstance(_n.func, ast.Name)
    and _n.func.id == "check")

_bout, _bp, _bf, _brc = _run_suite(TEST)
_bad = []
# ⚠️ 收尾標記有【兩個】，失敗路徑印的是「項失敗：」而不是「✅ 全數通過」。
# 第一版只認後者，於是「1 項失敗」會被多報一句「沒有印出收尾標記（沒跑到底）」
# —— 診斷訊息指向錯的原因。判定要與下面每一個變異用的 `done` 一致。
if "✅ 全數通過" not in _bout and "項失敗：" not in _bout:
    _bad.append("沒有印出收尾標記（沒跑到底）")
if _bf != 0:
    _bad.append(f"{_bf} 項失敗")
# ⚠️ rc 這一條也**只在零失敗時**才列（第九輪退回審查 NIT-1）。測試檔的收尾是
# `if FAILURES: … sys.exit(1)` / `else: … sys.exit(0)`，所以有 FAIL 就必然 rc≠0
# —— 那時再列一次 rc 只是同一件事講兩遍，讀起來像兩個獨立的理由。
# 零失敗而 rc≠0 才是**獨立**訊息（跑到底、0 個 FAIL、卻非零退出：收尾程式碼或
# atexit 出錯），那正是這裡要抓的東西。實測：探針 C（跑完全過但 sys.exit(3)）
# → 「90P/0F rc=3：rc=3」，單一理由。
if _bf == 0 and _brc != 0:
    _bad.append(f"rc={_brc}")
# ⚠️ 只有在【零失敗】時才比對項數。有失敗時 `_bp` 本來就會少幾項，
# 那時再喊「有檢查沒被執行到」是把讀者指向錯的原因（實測：1 項失敗被多報成
# 4 個理由，其中一句是假的）。診斷訊息的準確性與判定本身一樣重要。
if _bf == 0 and _bp != _AST_CHECKS:
    _bad.append(f"印出 {_bp} 項，但原始碼裡有 {_AST_CHECKS} 個 check() 呼叫點"
                " —— 有檢查沒被執行到，這把量尺短了一格")
if _bad:
    print(f"\n✗ 基準線不可信（{_bp}P/{_bf}F rc={_brc}）：{'；'.join(_bad)}")
    print("  否則下面每一個變異都只是這個既有故障的回音，而表格會長得很正常。")
    print("  尾巴：")
    for _l in _bout.strip().splitlines()[-15:]:
        print(f"    │ {_l}")
    shutil.rmtree(WORK, ignore_errors=True)
    sys.exit(2)
# BASE_PASS 不是裝飾品：下面的判定全部拿它當「滿分」的基準（第九輪 MINOR-3
# 之前它被賦值後從未讀取，那正是「宣告了但沒有做到」的最小樣本）。
BASE_PASS = _bp
print(f"基準線：{_bp} 項全過、0 失敗（rc=0）；"
      f"與原始碼的 {_AST_CHECKS} 個 check() 呼叫點相符\n")

# ── 兩個測試鉤子：只跑指定的變異、以及每個變異連跑 N 次 ────────────────────────
# 第九輪【審查退回】MINOR-2 的產物。審查員把整張表重跑一次，得到 M9 = 88P/2F、
# M29 = 89P/1F —— 與我記錄的**剛好相反**。
#
# ⚠️ 我第一版把理由寫成「M9 與 M29 行為等價，所以失敗集合必然相同」—— **那是錯的**，
# 第九輪【退回後】的審查（MINOR-1）抓到了。兩個變異拿掉的是**不同的閘門**：
#     M9  → job_board.py:836，送訊號前的 TOCTOU 重檢（gate B）
#     M29 → job_board.py:805，函式入口的持鎖前置檢查（gate A）
# 在 G2 那個情境下兩者都只剩一次 `_we_hold_scan_lock()` 呼叫、且回 False，
# 於是都真的去殺那個誘餌 —— 但「**在一個情境裡等價**」推不出「**全域等價**」，
# 所以「失敗集合必然相同」不成立。
#
# 正確的說法是弱得多的那一句：**在本套件下【量到的】失敗集合相同**
# （各連跑 6 次都是 88P/2F，且是同樣那兩項）。會相同的機制是：套件只有在 G2
# 這兩項檢查裡讓「我們持有掃描鎖」為真，其餘任何路徑上兩個閘門都回 False，
# 所以拿掉哪一個都觀察不到差別。**機制要說出來，否則它只是兩個相同的數字。**
#
# → 那它還能不能當偵測器？能，但它退化成一個**可證偽的期待值**，不是證明：
#   兩列讀數不同時**值得**先懷疑抽樣雜訊（那次確實是），但那個不同本身
#   不構成「某一項檢查不穩定」的鐵證。要斷定不穩定，只有重跑（見下面 _REPEAT）。
#
# 那個雜訊的來源是 MINOR-1（G2 用 `poll() is None` 當存活斷言，而 SIGKILL 送出到
# 真的死掉之間有窗口）。修掉之後 M9 / M29 在 6 次重跑下都是穩定的 88P/2F。
#
# 用法（只在需要時跑，平常的整張表不受影響）：
#   JOBSPY_MUTATE_ONLY=M9,M29 JOBSPY_MUTATE_REPEAT=6 .venv/bin/python tests/mutate.py
# 讀數只要在重跑之間變化，就標成 UNSTABLE（INCONCLUSIVE，退出碼非 0）——
# 因為那代表【這一列不能寫進文件】：單次抽樣不是變異的性質。
#
# ⚠️ **預設（`JOBSPY_MUTATE_REPEAT` 未設 ⇒ 1）下，UNSTABLE 結構上不可能出現**：
#    `_readings` 只有一個元素，`len(set(_readings)) > 1` 恆為 False。
#    也就是說，平常那一輪「✅ 31/32」**沒有測到任何一列的穩定性**，
#    它只測到「這一次是這樣」。這個取捨是刻意的（整張表 33 次執行 ≈ 4m45s，
#    乘上 REPEAT 就是線性成長），但事實必須寫出來：
#    **要對某一列講「穩定」，就得明跑 REPEAT，沒有別的路徑。**
_ONLY = {s.strip() for s in os.environ.get("JOBSPY_MUTATE_ONLY", "").split(",") if s.strip()}
try:
    _REPEAT = max(1, int(os.environ.get("JOBSPY_MUTATE_REPEAT", "1")))
except ValueError:
    sys.exit("✗ JOBSPY_MUTATE_REPEAT 必須是整數")
_SELECTED = [m for m in MUTATIONS if not _ONLY or m[0] in _ONLY]
if _ONLY and not _SELECTED:
    sys.exit(f"✗ JOBSPY_MUTATE_ONLY={sorted(_ONLY)} 沒有對應任何變異")
if _ONLY or _REPEAT > 1:
    print(f"⚠️  子集模式：{len(_SELECTED)}/{len(MUTATIONS)} 個變異、每個連跑 {_REPEAT} 次"
          "（這不是完整的一輪）\n")

results = []
try:
    for mid, label, mutate in _SELECTED:
        full = f"{mid} {label}"
        try:
            TARGET.write_text(mutate(ORIG), encoding="utf-8")
        except SystemExit as e:
            results.append((mid, full, "ANCHOR-FAIL", str(e), "BAD"))
            print(f"{full:44s} ✗ 錨點失效：{e}")
            continue

        if subprocess.run([PY, "-m", "py_compile", str(TARGET)],
                          capture_output=True).returncode != 0:
            results.append((mid, full, "SYNTAX-ERR", "變異本身語法錯誤", "BAD"))
            print(f"{full:44s} ✗ 變異本身語法錯誤")
            continue

        try:
            r = subprocess.run([PY, str(TEST)], cwd=WORK,
                               capture_output=True, text=True, timeout=TIMEOUT)
        except subprocess.TimeoutExpired:
            # 逾時【不】算被逮：分不出「變異造成死鎖（=有偵測到）」與
            # 「孤兒握著管線的假卡死（=什麼都沒測到）」。第五輪 MINOR-4c：
            # 原本算成「被逮」會讓結論反轉。現在標成 INCONCLUSIVE 並讓退出碼非 0，
            # 逼人去看。
            results.append((mid, full, f">{TIMEOUT}s TIMEOUT", "逾時，原因不明", "INCONCLUSIVE"))
            print(f"{full:44s} ⏱  逾時 {TIMEOUT}s —— INCONCLUSIVE，請查因")
            continue

        _readings = [(len(re.findall(r"\[PASS\]", r.stdout + r.stderr)),
                      len(re.findall(r"\[FAIL\]", r.stdout + r.stderr)),
                      r.returncode)]
        # 重跑模式：同一個變異再跑幾次，讀數只要不一樣就代表【有檢查不穩定】。
        # 這種列不能寫進文件 —— 單次抽樣不是變異的性質（第九輪 MINOR-2 就是這樣
        # 被審查員抓到的：M9 與 M29 拿掉**不同**的閘門，讀數卻與我記錄的相反）。
        # ⚠️ `_REPEAT == 1`（預設）時這一段整個不會執行，UNSTABLE 判定結構上
        #    不可能觸發 —— 平常那一輪並沒有驗證任何一列的穩定性。見上方說明。
        for _ in range(_REPEAT - 1):
            try:
                _r2 = subprocess.run([PY, str(TEST)], cwd=WORK,
                                     capture_output=True, text=True, timeout=TIMEOUT)
            except subprocess.TimeoutExpired:
                _readings.append((-1, -1, -1))
                continue
            _readings.append((len(re.findall(r"\[PASS\]", _r2.stdout + _r2.stderr)),
                              len(re.findall(r"\[FAIL\]", _r2.stdout + _r2.stderr)),
                              _r2.returncode))
        if len(set(_readings)) > 1:
            _all = " ".join(f"{p}P/{f}F" for p, f, _ in _readings)
            results.append((mid, full, _all,
                            f"{_REPEAT} 次重跑讀數不一致 —— 有檢查不穩定，"
                            "這一列不能寫進文件", "UNSTABLE"))
            print(f"{full:44s} 🎲 {_all} —— UNSTABLE：{_REPEAT} 次重跑讀數不一致")
            continue

        out = r.stdout + r.stderr
        passed, failed, rc = _readings[0]

        # ⚠️ 測試檔【必須跑到底】才算數。第八輪實測：M4（拿掉 phase 閘門）會讓
        # F4 那段走到真的 systemctl，被最底層那把「指令含 kill 就拋例外」的跳線
        # 當場拋出 AssertionError → 整個測試檔崩潰 → `[FAIL]` 數是 **0**。
        # 只看 `failed > 0` 的判定會把這記成「逃脫」——也就是說【最嚴重的變異
        # 看起來最無害】，而且判定還會隨生產 unit 當下的狀態翻來翻去。
        # 症狀是「PASS 數遠低於基準」：這裡用收尾標記判定，不看數字。
        #
        # ⚠️⚠️ 第八輪【審查】補記這個判定的兩個已知界線（審查員都造出來了）：
        #
        #   (1) 【反向】誤判：如果在測試檔尾端插入 `check(...False)` 之後才
        #       traceback，收尾標記還沒印 → 會被判 INCONCLUSIVE，儘管它其實
        #       有 FAIL、是「被逮」。實測：`passed=83 failed=1` → INCONCLUSIVE。
        #       這個方向是【保守】的（退出碼非 0、逼人來看），所以留著不修。
        #
        #   (2) 【正向】盲區：收尾標記由【主執行緒】印出，所以**非主執行緒**
        #       （daemon thread）的崩潰不會阻止它 —— 主執行緒照樣跑到底、照樣
        #       印標記、rc=0、`[FAIL]` 數 0 → 記成「逃脫」。
        #       實測：`threading.Thread(target=lambda: 1/0, daemon=True).start()`
        #       之後主執行緒照常印字、rc=0。
        #       這個專案的產品碼**大量使用 daemon thread**（scheduler_loop、
        #       watchdog_loop、_jobscan_watch_loop），所以這不是純理論。
        #
        # 也就是說：這個判定保證的是「**主執行緒**跑到底」，不是「整個行程
        # 健康」。會這樣寫是因為它要解的 M4 正是主執行緒崩潰；非主執行緒的
        # 崩潰需要另一種守衛（例如在收尾時檢查執行緒是否還活著），本輪沒做。
        # **不要把「0 個 INCONCLUSIVE」讀成「判定完美」。**
        done = "✅ 全數通過" in out or "項失敗：" in out
        # 第九輪 MINOR-3 的第二半：**「跑到底」不等於「每一項都跑到了」。**
        # 一個印得出收尾標記、0 個 FAIL 的執行，如果總項數比基準線少，
        # 那就是量尺短了一格 —— 而下面的判定會把它記成 `❌ 逃脫（無回歸保護）`，
        # 歸因錯誤（真正的原因是「檢查沒被執行」，不是「修正沒有守衛」）。
        # 這裡把它拉出來當 INCONCLUSIVE，逼人去看。
        if done and passed + failed != BASE_PASS:
            results.append((mid, full, f"{passed}P/{failed}F rc={rc}",
                            f"總項數 {passed + failed} ≠ 基準線 {BASE_PASS}"
                            "（檢查被跳過？）", "INCONCLUSIVE"))
            print(f"{full:44s} ⚠️  總項數 {passed + failed} ≠ 基準線 {BASE_PASS}"
                  f" —— INCONCLUSIVE，不是逃脫，請查因")
            continue
        if not done:
            # 多印出實際的 P/F 與最後幾行，讓「崩在哪」一眼可見 ——
            # 否則唯一的線索是一個看起來很正常的 `32P/0F`。
            tail_lines = [l for l in out.strip().splitlines()[-4:]]
            results.append((mid, full, f"{passed}P/{failed}F rc={rc}",
                            "測試檔未跑完（崩潰？）—— 這不是逃脫", "INCONCLUSIVE"))
            print(f"{full:44s} ⚠️  測試檔【未跑完】({passed}P/{failed}F "
                  f"rc={rc}) —— INCONCLUSIVE，不是逃脫，請查因")
            for _l in tail_lines:
                print(f"{'':44s}     │ {_l}")
            continue

        if failed > 0:
            verdict, kind = "✅ 被逮", "CAUGHT"
        else:
            verdict, kind = "❌ 逃脫（無回歸保護）", "ESCAPED"
        results.append((mid, full, f"{passed}P/{failed}F rc={rc}", verdict, kind))
        print(f"{full:44s} {passed:>2}P / {failed:>2}F  rc={rc}  {verdict}")
finally:
    shutil.rmtree(WORK, ignore_errors=True)

# ── 收工驗證：生產目錄必須毫髮無傷 ──────────────────────────────────────────
print("\n" + "=" * 80)
PROD_AFTER = sha256(PROD_TARGET)
print(f"生產檔案驗證：sha256 {'不變 ✓' if PROD_AFTER == PROD_BEFORE else '★已改變★'}"
      f"  {PROD_AFTER[:16]}…")
if PROD_AFTER != PROD_BEFORE:
    # ⚠️ 這個警告有兩個成因，而且【處置相反】：
    #   (a) 真的外洩 —— 某個變異寫進了生產檔。要救：`git checkout -- job_board.py`。
    #   (b) 有人在這一輪跑的期間【自己編輯了】job_board.py。要救：什麼都別做，
    #       你的編輯是對的，這一輪的數字不能用而已。
    # 原本這裡直接印「請立刻 git checkout」—— 那會把 (b) 的情況下使用者剛寫好的
    # 工作【整批刪掉】。一個偵測器不該在只知道「檔案變了」的時候，建議一個
    # 會刪掉未提交工作的動作。所以先讓人自己看一眼。
    print("✗ 生產的 job_board.py 在這一輪期間被改動了！")
    print("  先看 diff 再決定 —— 【不要】反射性地 git checkout：")
    print("    git diff --stat job_board.py")
    print("  · 變異外洩 → 只有突變的幾行，救法：git checkout -- job_board.py")
    print("  · 你自己在跑的期間編輯過 → 你的編輯是對的，這一輪的數字作廢，重跑即可")
    sys.exit(2)
print(f"隔離副本已移除：{not WORK.exists()}")

for _mid, full, stat, verdict, _kind in results:
    print(f"  {full:44s} {stat:>18s}  {verdict}")
print("=" * 80)

# ⚠️⚠️ 這個 tuple 是「這一列不能拿來下結論」的完整集合。第九輪加入 UNSTABLE 時
# **漏了這裡** —— 而症狀正是本專案的主旋律缺陷：加了守衛，但守衛不會讓任何東西
# 失敗。實測（探針造出來的）：
#     M13 拿掉鎖（保留整行一次 write）  🎲 90P/1F 91P/0F —— UNSTABLE
#     ✅ 1/1 個變異被逮捕，無逃脫【子集：1/32…】      ← 收尾這樣印，rc=0
# 明明整列被判「不能寫進文件」，結論行卻說它被逮捕了。原因：UNSTABLE 不在這個
# tuple 裡 → 既不算 bad 也不算 escaped → 直接落到下面的成功路徑。
# 教訓：**新增一種判定時，要去找所有「分類」的地方，不是只加到產生它的地方。**
bad = [r for r in results if r[4] in ("BAD", "INCONCLUSIVE", "UNSTABLE")]
escaped = [r for r in results if r[4] == "ESCAPED"]
known = [r for r in escaped if r[0] in EXPECTED_ESCAPES]
new = [r for r in escaped if r[0] not in EXPECTED_ESCAPES]
stale = [r for r in results if r[4] == "CAUGHT" and r[0] in EXPECTED_ESCAPES]

if known:
    print(f"\nℹ️  {len(known)} 個【已知且已理解】的逃脫（見 EXPECTED_ESCAPES 與 DECISIONS.md）：")
    for _mid, full, _s, v, _k in known:
        print(f"   - {full}  {v}")

if stale:
    # 豁免過期 = 有人補了測試。留著它會讓「已知逃脫」清單變成裝飾品。
    print(f"\n⚠️  {len(stale)} 個豁免【已過期】（這些變異現在被逮到了，請從 "
          f"EXPECTED_ESCAPES 移除）：")
    for _mid, full, _s, _v, _k in stale:
        print(f"   - {full}")

if bad:
    print(f"\n⚠️  {len(bad)} 個變異無法判定（不影響結論的正確性，但代表這一輪不完整）：")
    for _mid, full, stat, v, _k in bad:
        print(f"   - {full}  {stat}  {v}")

if new:
    print(f"\n⚠️  {len(new)} 個變異沒有被逮捕（＝那項修正沒有回歸保護）：")
    for _mid, full, _s, v, _k in new:
        print(f"   - {full}  {v}")

if new or bad:
    sys.exit(1)

# ⚠️ 子集模式（JOBSPY_MUTATE_ONLY）下，這裡的 N 是**子集大小**，不是 32。
# 不加標記的話，這一行讀起來跟完整一輪的結論一模一樣 —— 那正是本專案
# 反覆被燒的形狀（把抽樣讀成性質）。所以子集一律在結論行上自我標示。
#
# ⚠️⚠️ 這裡一開始寫成 `_SELECTED is MUTATIONS` —— **永遠是 False**，因為上面的
# 推導式每次都建一個新 list，身分檢查恆不成立。也就是說「防謊報的那一行」
# 本身在完整一輪時會謊報【子集】。
# 教訓與 M13 那段同源：**別用身分／存在與否去推導「我跑了幾項」，直接數。**
# 用 len(_SELECTED) != len(MUTATIONS) 是從資料推導，不依賴任何旗標。
_is_subset = len(_SELECTED) != len(MUTATIONS)
_subset = (f"【子集：{len(_SELECTED)}/{len(MUTATIONS)} 個變異"
           + (f"、每個連跑 {_REPEAT} 次" if _REPEAT > 1 else "") + "】") if _is_subset else ""
print(f"\n✅ {len(results) - len(known)}/{len(results)} 個變異被逮捕"
      + (f"，{len(known)} 個為已知逃脫" if known else "，無逃脫") + _subset)
if _is_subset:
    print("   ⚠️ 這不是完整的一輪，不要把這一行當成 32 個變異的結論。")
sys.exit(0)
