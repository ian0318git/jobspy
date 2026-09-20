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
print(f"隔離副本：{WORK}")
print(f"生產目錄【不會被寫入】：{PROD_TARGET.name} sha256={PROD_BEFORE[:16]}…")
print(f"起始狀態：乾淨，{len(ORIG)} bytes\n")


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
_bout, _bp, _bf, _brc = _run_suite(TEST)
if "✅ 全數通過" not in _bout or _bf != 0 or _brc != 0:
    print(f"\n✗ 基準線就不是全綠（{_bp}P/{_bf}F rc={_brc}）—— 先修好再跑變異。")
    print("  否則下面每一個變異都只是這個既有故障的回音，而表格會長得很正常。")
    print("  尾巴：")
    for _l in _bout.strip().splitlines()[-15:]:
        print(f"    │ {_l}")
    shutil.rmtree(WORK, ignore_errors=True)
    sys.exit(2)
BASE_PASS = _bp
print(f"基準線：{_bp} 項全過、0 失敗（rc=0）\n")

results = []
try:
    for mid, label, mutate in MUTATIONS:
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

        out = r.stdout + r.stderr
        passed = len(re.findall(r"\[PASS\]", out))
        failed = len(re.findall(r"\[FAIL\]", out))

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
        if not done:
            # 多印出實際的 P/F 與最後幾行，讓「崩在哪」一眼可見 ——
            # 否則唯一的線索是一個看起來很正常的 `32P/0F`。
            tail_lines = [l for l in out.strip().splitlines()[-4:]]
            results.append((mid, full, f"{passed}P/{failed}F rc={r.returncode}",
                            "測試檔未跑完（崩潰？）—— 這不是逃脫", "INCONCLUSIVE"))
            print(f"{full:44s} ⚠️  測試檔【未跑完】({passed}P/{failed}F "
                  f"rc={r.returncode}) —— INCONCLUSIVE，不是逃脫，請查因")
            for _l in tail_lines:
                print(f"{'':44s}     │ {_l}")
            continue

        if failed > 0:
            verdict, kind = "✅ 被逮", "CAUGHT"
        else:
            verdict, kind = "❌ 逃脫（無回歸保護）", "ESCAPED"
        results.append((mid, full, f"{passed}P/{failed}F rc={r.returncode}", verdict, kind))
        print(f"{full:44s} {passed:>2}P / {failed:>2}F  rc={r.returncode}  {verdict}")
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

bad = [r for r in results if r[4] in ("BAD", "INCONCLUSIVE")]
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

print(f"\n✅ {len(results) - len(known)}/{len(results)} 個變異被逮捕"
      + (f"，{len(known)} 個為已知逃脫" if known else "，無逃脫"))
sys.exit(0)
