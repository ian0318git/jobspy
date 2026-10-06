#!/usr/bin/env python3
"""變異測試：把每一項修正【改回去】，確認對應的測試檔會 FAIL。

    .venv/bin/python tests/mutate.py

一個 **suite** = 一個生產檔 + 一個測試檔 + 一組變異。目前有三個：

  | suite | 生產檔 | 測試檔 | 變異數 |
  |---|---|---|---|
  | `scan` | `job_board.py` | `tests/test_scan_lock.py` | 53 |
  | `location-filter` | `linkedin_job_search.py` | `tests/test_location_filter.py` | 5 |
  | `location-seek` | `jobspy/seek/__init__.py` | `tests/test_location_filter.py` | 3 |

為什麼刻意限制成「一個 suite 恰好【一個】生產檔」：變異清單才能維持
`(id, 標籤, 函式)` 三元組，不必在每一列重複寫檔名 —— 也就沒有「漏寫檔名」這種錯。
跨檔案的修正就拆成多個 suite（上面後兩個跑同一個測試檔，但各打各的檔案，
所以「是哪一個檔的守衛鬆了」分開看得出來）。

## ⚠️ 多 suite 帶進來的新故障形狀：前一個 suite 的殘留（第十輪，實測到的）

舊版只有一個 target、一個 test，每個變異都從記憶體裡的 `ORIG` 重新產生，
所以「上一個變異還留在磁碟上」這種事**結構上不可能發生**。加了第二個 suite 之後
它立刻發生了，而且是被守衛當場擋下來的：

  當時 `location-filter` 只選了 `L1` 一個變異來跑，跑完之後它**還留在樹上**；
  接著 `location-seek` 跑基準線，於是量到的是**別人的變異**——
  基準線 3 項失敗，指紋正是 L1 的子字串誤收（`prestons`、`eppings`）。
  （污染的量取決於殘留的是哪一個變異：審查員跑 `L1`–`L6` 時留下的是 `L5`，
  基準線失敗 **13 項**。所以「基準線是紅的」還不足以讓人看出原因。）

危險的地方不是它失敗了，是它**差一點不會失敗**：基準線只要剛好 0 失敗，
`base_pass` 就會被污染成一個錯的「滿分」，接下來這個 suite 的每一列判定都拿錯的
基準在比 —— 而且表格看起來完全正常。這正是本檔案開頭那兩個歷史教訓的形狀
（「有修正、沒有回歸保護」與「最嚴重的變異看起來最無害」）的第三個版本。

現在的規則很單純，而且是一條不變式：
**跑任何一次測試之前，樹上必須等於 HEAD，除了正在被測的那【一個】變異。**
由 `_restore_originals()` 在每個變異與每條基準線之前強制執行，收工時再逐檔比對
sha256（不是 grep —— 見上面「為什麼在 /tmp 的隔離副本裡跑」那節的 2026-09-19 教訓）。
⚠️ 這裡刻意【不寫行號】：這一段自己就會讓後面的行號位移，而寫死行號的註解
在三次編輯之後就會指向別的東西 —— 本專案已經吃過「文件數字與現實脫鉤」的虧。

退出碼 0 = 全部變異都被逮捕（已知逃脫不算）；1 = 有變異逃脫、有需要人看的
INCONCLUSIVE／套用失敗，**或某個豁免已過期**（列在 escapes 裡卻被逮到了 ——
第十輪審查 NIT-2 之前這一項只印警告、不影響退出碼）。2 = harness 自己的問題
（基準線不可信、殘留、生產檔被寫入）。

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

## ⚠️ 第十輪：地點那 8 個變異為什麼一定要從 /tmp 搬進來

2026-09-29 的地點誤收修正附了一組 8 個變異，但 harness 只活在 `/tmp`，
`DECISIONS.md` 因此自己標註那張表是「**當次可重現、隔天不可重現**」。搬進來之後
重跑，確認 8 列**全部逐格重現**（3/4/8/2/13/1/2/2）。

⚠️ **這一段的結論被我寫錯過一次，錯誤本身比原本的結論更值得留下來（第十輪審查
MINOR-2）。** 我第一版寫的是「第 8 列（`NON_VIC` 去 `\\b` = 8）重現不出來，
產出原表的那一版 harness 沒有留下來」。**那是假的。** 審查員在磁碟上找到第三份
`/tmp/run_mutations.py`（2026-09-29 23:28），它把 `NON_VIC_STATE_PATTERN` 區塊內
**所有** `\\b` 拿掉（= 兩側），基準線是量出來的，跑出來**逐格重現原表，含那個 8**。

真正發生的事不是「證據消失了」，是**我只掃到兩份就下結論說沒留下來**：

  * `/tmp/mutate.py` —— 錨點 `return raw, None` 已被後續修正改掉，少一列 `M7`；
    而且它的測試檔副本是 09-29 23:06 的舊版，**基準線現在是紅的**，所以它量到的
    「3」是既有故障的回音，不是乾淨的對照。
  * `/tmp/mutate_review.py` —— 寫死 `npass != 53`，而測試現在是 57 項，在基準線
    就中止，**什麼都沒量到**（它根本沒有「給出 3」）。
  * `/tmp/run_mutations.py` —— 就是產出原表的那一份，**它一直在那裡**。

**教訓（兩層，第二層才是我真正犯的錯）**：
  1. 「`/tmp` 有東西」不等於「那份就是對的」—— 同名檔案多份、各自量到不同答案，
     而判斷哪一份可信本身就沒有依據。
  2. **我宣告「證據沒留下來」的時候，並沒有把磁碟掃乾淨。** 一份被錯誤宣告為
     「不存在」的證據，會讓下一個人停止尋找 —— 這比「找不到」更糟，因為它帶著
     結論的權威感。要宣告某個東西不存在，得先證明你找過了，而不是找了一下。
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
from dataclasses import dataclass
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
# ⚠️ 第十輪：`PROD_TARGET` / `TEST_REL` 兩個單一全域【已移除】—— 被測的檔案與
# 測試檔現在是 suite 的屬性（見下方的 `SUITES`），寫在這裡只會變成第二個真相
# 來源。移除時它們已經沒有任何引用點，所以不會有「改了一半」的殘留。

# 每個變異的逾時。正常一輪約 6 秒；被逮捕的變異通常更快，但留足餘裕。
TIMEOUT = 400

# 已知會逃脫、且【已經理解為什麼】的變異（用變異 id，不是標籤字串）。
# 第十輪：這份清單現在【只屬於 scan suite】—— suites 各自帶自己的 escapes，
#          這個名字留著是因為它的內容與歷史理由都是 scan 的。
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

def sub_once(text, old, new, label):
    n = text.count(old)
    if n != 1:
        raise SystemExit(f"錨點出現 {n} 次，預期 1 次 —— {label} 未套用")
    return text.replace(old, new)


# ── kill_stalled_external 開頭那一段 ─────────────────────────────────────────
# M8（整個函式 no-op）與 M29（只拿掉持鎖前置檢查）共用這個錨點。
# 2026-10-06 第十輪退回時，那一行 `return None` 前面多插了一次
# `_record_stall_verdict(...)`，於是【兩個變異同時 ANCHOR-FAIL】——
# harness 把它們列成「無法判定」，而不是靜靜地當成通過。抽成常數之後，
# 以後再改那段只會有一個地方要跟著動。
_HOLD_GATE_ANCHOR = (
    '    if _we_hold_scan_lock():\n'
    '        _record_stall_verdict(False, "掃描鎖在我們手上（互斥），沒有外部掃描")\n'
    '        return None\n'
    '    idle = _external_idle_seconds()\n'
)


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
        t,
        '''    if state.get("phase") != "running":
        _record_stall_verdict(
            False, f"state 自稱 {state.get('phase')!r}，不是進行中的掃描")
        return None

''',
        "", "M4")),
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
        t, _HOLD_GATE_ANCHOR, "    return None\n" + _HOLD_GATE_ANCHOR, "M8")),
    ("M9", "拿掉 TOCTOU 重檢", lambda t: sub_once(
        t,
        '''    if _we_hold_scan_lock():
        _record_stall_verdict(False, "動手前我們取得了掃描鎖，放棄（TOCTOU 重檢）")
        return None

''',
        "", "M9")),
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
         t, _HOLD_GATE_ANCHOR,
         '    idle = _external_idle_seconds()\n', "M29")),

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

    # ── 第十一輪（2026-10-06 事故）：存活閘門 ────────────────────────────────
    # 事故：06:00:53 啟動的掃描，06:01:09 被 watchdog SIGKILL，只活 16 秒。
    # 缺陷本體是「拿自己的讀取時鐘（last_read_at）當對方卡死的依據」，修法是
    # 加一道閘門：目標自身的年齡不可能小於它沉默的時間。
    #
    # 這幾個變異刻意分開，因為它們是【獨立】的失效模式：閘門整個失效、
    # 只有 systemd 那條鬆掉、只有 PID 那條鬆掉、邊界差一格、失敗時倒向放行、
    # 資料來源算錯 —— 任何一個單獨發生都不該能全身而退。
    ("M33", "存活閘門整個失效（_stall_gate_refusal 永遠放行）",
     lambda t: sub_once(
         t,
         '    if runtime is None:\n'
         '        return (f"[watchdog] 無法判定 {what} 已跑多久 → 不動手"\n',
         '    return None\n'
         '    if runtime is None:\n'
         '        return (f"[watchdog] 無法判定 {what} 已跑多久 → 不動手"\n',
         "M33")),
    ("M34", "只拿掉 systemd 路徑的存活閘門（回復 06:01 誤殺）",
     lambda t: sub_once(
         t,
         '            refusal = _stall_gate_refusal(\n'
         '                unit_runtime, idle, "jobscan.service",\n'
         '                ever_read=_EXTERNAL["read_ok"])\n',
         '            refusal = None\n',
         "M34")),
    ("M35", "只拿掉 PID 路徑的存活閘門（手動掃描失去唯一的存活判準）",
     lambda t: sub_once(
         t,
         '            refusal = _stall_gate_refusal(\n'
         '                _target_runtime_seconds(pid), idle, f"pid={pid}",\n'
         '                ever_read=_EXTERNAL["read_ok"])\n',
         '            refusal = None\n',
         "M35")),
    ("M36", "存活閘門邊界差一格（runtime < idle 改成 <=）",
     lambda t: sub_once(
         t,
         '    if runtime < idle:\n',
         '    if runtime <= idle:\n',
         "M36")),
    # ⚠️ 第十輪退回時這一條【逃脫過一次】，原因值得記下來。
    # 它原本把「runtime 未知」替換成 `runtime = SEARCH_STALL_TIMEOUT + 1`(=901)。
    # 舊判準是拿固定門檻比，901 >= 900 就會放行，所以測得出來。
    # 判準改成拿 idle 比之後，901 配上任何真實的 idle（門檻以上、可能是好幾小時）
    # 都會被拒絕 —— 替換本身不再造成 fail-open，測試自然就抓不到了。
    # 這是【變異沒有跟著判準一起更新】。改成忠實版本：直接假裝目標已經夠老。
    ("M37", "查不出年齡時倒向放行而不是拒絕（fail open）",
     lambda t: sub_once(
         t,
         '    if runtime is None:\n'
         '        return (f"[watchdog] 無法判定 {what} 已跑多久 → 不動手"\n'
         '                f"（state 自稱卡死，但無法排除是看板自己的讀取時鐘過期）")\n',
         '    if runtime is None:\n'
         '        runtime = idle + 1\n',      # 假裝目標已經跑得比沉默時間還久
         "M37")),
    ("M38", "/proc/<pid>/stat 取錯欄位（starttime 索引 19 → 18）",
     lambda t: sub_once(
         t,
         '        start_ticks = int(fields[19])          # 第 22 欄；扣掉 pid 與 comm 後索引 19\n',
         '        start_ticks = int(fields[18])          # 第 22 欄；扣掉 pid 與 comm 後索引 19\n',
         "M38")),
    ("M39", "systemd 的微秒沒換算成秒（runtime 恆為大負數 → 矯正成不殺）",
     lambda t: sub_once(
         t,
         '    return state, time.monotonic() - start_us / 1_000_000\n',
         '    return state, time.monotonic() - start_us\n',
         "M39")),

    # ── 第十輪審查退回（MAJOR-1 / MINOR-2 / NIT-1 / NIT-4）─────────────────────
    # 這幾條守的是【第一版修法自己種下的缺陷】：
    #   M40 判準拿固定門檻比而不是拿 idle 比 → 誤殺只是延後到 T+900s
    #   M41/M42 時鐘基準不一致的兩種方向（少算 = 偏嚴、多算 = 偏鬆/fail-open）
    #   M43 判定被後面的分支蓋掉 → UI 看不到真正的結論
    #   M44 ActiveState 解析失效 → systemd 分支整個不執行
    #   M45 非零退出被照單全收 → 拿垃圾值去比大小
    ("M40", "【第十輪 MAJOR-1】判準退回固定門檻（runtime < SEARCH_STALL_TIMEOUT）",
     lambda t: sub_once(
         t,
         '    if runtime < idle:\n',
         '    if runtime < SEARCH_STALL_TIMEOUT:\n',
         "M40")),
    ("M41", "【第十輪 MINOR-2】拿掉 BOOTTIME→MONOTONIC 的校正（suspend 後偏鬆）",
     lambda t: sub_once(
         t,
         '    return uptime - start_ticks / hz - max(0.0, boottime_minus_monotonic)\n',
         '    return uptime - start_ticks / hz\n',
         "M41")),
    ("M42", "【第十輪 MINOR-2】校正方向搞反（改成加上 suspend 總量）",
     lambda t: sub_once(
         t,
         '    return uptime - start_ticks / hz - max(0.0, boottime_minus_monotonic)\n',
         '    return uptime - start_ticks / hz + max(0.0, boottime_minus_monotonic)\n',
         "M42")),
    ("M43", "【第十輪 NIT-4】watchdog 判定改成後講後贏（前面的結論被蓋掉）",
     lambda t: sub_once(
         t,
         '        if verdict is None:\n'
         '            verdict = (stalled, note)\n',
         '        verdict = (stalled, note)\n',
         "M43")),
    ("M44", "【第十輪 NIT-1】ActiveState 解析失效（systemd 分支永遠不執行）",
     lambda t: sub_once(
         t,
         '    state = props.get("ActiveState") or None\n',
         '    state = None\n',
         "M44")),
    ("M45", "【第十輪 NIT-1】systemctl 非零退出被照單全收（fail open）",
     lambda t: sub_once(
         t,
         '    if r.returncode != 0:\n'
         # ⚠️ 這一行的措辭在第十二輪被 MINOR-2 改掉了（「不接手」→「無法確認 unit
         # 在不在跑」）。錨點跟著改 —— 沒跟到的話 harness 會報 ANCHOR-FAIL
         # （「錨點出現 0 次」），也就是「這一條變異根本沒被套用」。
         # 第十二輪第一次跑就是這樣抓到的。
         '        _log(f"[watchdog] 查詢 jobscan.service 狀態失敗（exit={r.returncode}），"\n'
         '             f"無法確認 unit 在不在跑")\n'
         '        return None, None\n',
         '',
         "M45")),

    # ── 第十一輪退回（C-1 CRITICAL）：本輪根本讀不到任何輸出 ──────────────────
    # 缺陷是【算術上不可能成立】的那一種，所以不能靠「換個數字再測一次」抓到：
    #   last_read_at 在【偵測到】一輪時被設成當下，而偵測必定晚於該輪啟動，
    #   於是「一個 chunk 都沒讀到」時恆有 idle <= runtime
    #   → runtime < idle 永遠不成立 → 閘門整個不作用。
    # 真正擋下它的是 read_ok/ever_read。這兩條變異各自拿掉防護的一端：
    #   M46 拿掉判準端的檢查（就算旗標是 False 也照殺）
    #   M47 拿掉旗標端的設定（把「讀到非空內容」放寬成「檔案存在」）
    ("M46", "【第十一輪 C-1】拿掉 ever_read 前置（本輪沒讀到任何輸出也照殺）",
     lambda t: sub_once(
         t,
         '    if not ever_read:\n'
         '        return (f"[watchdog] 拒絕動手：本輪（{what}）從未從 live log 讀到任何輸出 —— "\n'
         '                f"無法區分「掃描真的卡死」與「看板的讀取路徑故障」，不動手")\n',
         '',
         "M46")),
    ("M47", "【第十一輪 C-1】旗標的設定點消失（永遠讀不到「本輪讀過東西」這個事實）",
     lambda t: sub_once(
         t,
         '            _EXTERNAL["read_ok"] = True\n',
         '',
         "M47")),
    ("M48", "【第十一輪 C-1】新的一輪沒有把旗標歸零（沿用上一輪的結論）",
     lambda t: sub_once(
         t,
         '            "read_ok": False,\n',
         '',
         "M48")),
    # ── 第十二輪退回（MAJOR-1）：C-1 的【機率版】───────────────────────────
    # (c) 修好的是「整輪都沒讀到」；這一條打的是「讀到了，但是上一輪的位元組」。
    # run_scan.sh 先取鎖、才 : > "$LIVE"，中間隔 4.6–15.4ms（n=18 實測）。
    # watcher 每 5s 一個 tick，落進去（約 0.1%）就會 reset（offset=0、read_ok=False）
    # 之後立刻讀到【舊的位元組】→ read_ok 在這一輪還沒產出任何東西時就被設成 True，
    # 而截斷的 resync 只清 lines/offset/pending、不清 read_ok。
    # 變異 = 把 offset 退回 0（第十二輪之前的行為），也就是讓「讀到舊位元組」重新
    # 具備把 read_ok 設成 True 的能力。
    # ⚠️ 這一條【必然】要抓得到，因為它對應的是真的會 SIGKILL 一個健康掃描的路徑：
    #    負向對照實測（只退回這一行）→ 5 項 FAIL，且 syscall 清單裡真的有
    #    systemctl --user kill --signal=SIGKILL jobscan.service。
    ("M49", "【第十二輪 MAJOR-1】offset 退回 0（窗口內讀到的上一輪位元組算成本輪證據）",
     lambda t: sub_once(
         t,
         '            "offset": _follow_from,\n',
         '            "offset": 0,\n',
         "M49")),
    # ── 第十二輪 MINOR-2：把不確定講成結論的日誌 ────────────────────────────
    # 這三條變異改的是【措辭】不是行為，看起來像瑣事 —— 但 MINOR-2 的裁決本體就是
    # 措辭：舊句「不接手」在 _jobscan_unit_props() 裡是真的（它回 (None, None)），
    # 放到整條路徑上就是假的 —— 呼叫端在 act is None 時落到 PID 路徑，
    # 而那裡有自己的 cmdline 身分檢查與自己的存活閘門，**會真的開槍**。
    # 留著那句就是製造另一種「把不確定講成結論」。
    # 所以守它的方式不能是「原始碼裡有沒有那個字串」（那是測字串），NIT-6 是跑完整條
    # 路徑、斷言日誌與【真的開槍】同時成立。下面三條各自拿掉其中一句的修正。
    ("M50", "【第十二輪 MINOR-2】systemctl 非零退出那句退回『不接手』",
     lambda t: sub_once(
         t,
         '        _log(f"[watchdog] 查詢 jobscan.service 狀態失敗（exit={r.returncode}），"\n'
         '             f"無法確認 unit 在不在跑")\n',
         '        _log(f"[watchdog] 查詢 jobscan.service 狀態失敗（exit={r.returncode}），不接手")\n',
         "M50")),
    ("M51", "【第十二輪 MINOR-2】systemctl 叫不起來那句退回『不接手』",
     lambda t: sub_once(
         t,
         '        _log(f"[watchdog] 查詢 jobscan.service 狀態失敗（{e}），"\n'
         '             f"無法確認 unit 在不在跑")\n',
         '        _log(f"[watchdog] 查詢 jobscan.service 狀態失敗（{e}），不接手")\n',
         "M51")),
    ("M52", "【第十二輪 MINOR-2】呼叫端那句退回『沒有可終止的掃描』（宣稱我們不知道的事）",
     lambda t: sub_once(
         t,
         '            _log("[watchdog] 查詢 jobscan.service 狀態失敗 → 無法確認 unit 在不在跑，"\n'
         '                 "改走 PID 路徑（並受 cmdline 身分檢查）")\n',
         '            _log("[watchdog] 查詢 jobscan.service 狀態失敗 → 目前為 未知，"\n'
         '                 "沒有可終止的掃描")\n',
         "M52")),
    # ── 第十三輪審查 NIT-2：把 m-2 的【裁決本身】也釘住 ──────────────────────
    # M50–M52 守的是措辭。措辭之所以重要，是因為它描述的【行為】是
    # 「查詢失敗 → 落回 PID 路徑 → 可能真的開槍」。這條變異直接把那個行為改掉：
    # 採 reviewer 當年提的選項 (甲)（`act is None` 就 return None，不落 PID 路徑）。
    # 若 NIT-6 只是測字串，它會逃脫；reviewer 已用同樣的改動驗過會 4 項 FAIL。
    ("M53", "【第十三輪 NIT-2】查詢失敗改採選項（甲）：不落 PID 路徑（m-2 裁決被推翻）",
     lambda t: sub_once(
         t,
         '        elif act is None:\n',
         '        elif act is None:\n'
         '            _record_stall_verdict(False, "查不到 unit 狀態，不接手")\n'
         '            return None\n',
         "M53")),
]


# ══════════════════════════════════════════════════════════════════════════════
# 地點過濾那一組（2026-09-29 第十輪，對應 `62c5823`）
# ══════════════════════════════════════════════════════════════════════════════
# 分成兩個 suite 是因為它們打在不同的生產檔上，而「是哪一個檔的守衛鬆了」必須
# 分開看得出來。兩個 suite 跑同一個測試檔 —— 這是刻意的：`test_location_filter.py`
# 同時涵蓋「解析」（Seek 側）與「判定」（過濾器側），而突變只打其中一邊。
#
# 括號內的 FAIL 數是 2026-09-30 用【這個檔案】實測的（見 docstring：舊表第 8 列
# 與重跑結果不同，原因已查明並記在 L3 的註解裡）。
LOCATION_FILTER_MUTATIONS = [
    ("L1", "白名單去 \\b（子字串比對復辟，3）", lambda t: sub_once(
        t,
        'MELB_AREA_PATTERN = "|".join(rf"\\b{area}\\b" for area in MELB_AREAS)',
        'MELB_AREA_PATTERN = "|".join(MELB_AREAS)', "L1")),
    ("L2", "停用州別關卡（4）", lambda t: sub_once(
        t,
        "    if re.search(NON_VIC_STATE_PATTERN, loc):\n        return False\n",
        "    if False:\n        return False\n", "L2")),
    # ⚠️ 這一列的定義是【兩側的 \b 都拿掉】，不是只拿前緣。差別很大：
    #   兩側都拿 → 8 個 FAIL；只拿前緣 → 3 個 FAIL。舊表的 8 來自兩側版本。
    #   留著這段註解是因為「去 \b」在字面上完全歧義，而兩種寫法的偵測力差 5 項。
    #
    # ⚠️ 機制【原本寫反了】（第十輪審查 MINOR-4，已實測更正）。正確的是：
    #   · 只拿【前緣】→ pattern 變成 `(nt|wa|sa|…)\b`，會在**詞尾**命中 ——
    #     `nt\b` 打中 "mount"、"point"（Mount Waverley、Point Cook）。這 3 項就是
    #     `保留 'Mount Waverley…'`、`保留 'Point Cook…'`、`非 VIC 州別 'point cook'`。
    #   · 兩側都拿 → 連**詞首**也命中 —— `wa` 打中 "watsonia"、`act` 打中 "acton"、
    #     `sa` 打中 "salisbury"。多出來的 5 項是這些。
    #   也就是說「要兩側都鬆掉才會被誤判」的是**詞首**的案例，不是詞尾的。
    ("L3", "NON_VIC 去 \\b（兩側都去，8）", lambda t: sub_once(
        t,
        '    r"\\b(nsw|new south wales|qld|queensland|wa|western australia|"\n'
        '    r"sa|south australia|tas|tasmania|act|australian capital territory|"\n'
        '    r"nt|northern territory)\\b"\n',
        '    r"(nsw|new south wales|qld|queensland|wa|western australia|"\n'
        '    r"sa|south australia|tas|tasmania|act|australian capital territory|"\n'
        '    r"nt|northern territory)"\n', "L3")),
    ("L4", "白名單移除 preston（2）", lambda t: sub_once(
        t,
        '"hawthorn", "kew", "balwyn", "preston", "geelong", "ballarat",',
        '"hawthorn", "kew", "balwyn", "geelong", "ballarat",', "L4")),
    ("L5", "過濾函式永遠回 True（13）", lambda t: sub_once(
        t,
        "    loc = str(location).lower()\n"
        "    if re.search(NON_VIC_STATE_PATTERN, loc):\n"
        "        return False\n"
        "    return bool(re.search(MELB_AREA_PATTERN, loc))",
        "    return True", "L5")),
]

LOCATION_SEEK_MUTATIONS = [
    ("L6", "Seek 不允許郵遞區號（1）", lambda t: sub_once(
        t, '        r"(?:\\s+\\d{4})?$",', '        r"$",', "L6")),
    # 這一列是【還原成修正前的原樣】—— 不是隨手寫一個壞版本，而是把
    # `git show 62c5823^:jobspy/seek/__init__.py` 的那三行貼回來。
    # 變異愈接近「真實可能犯的錯」，它證明的東西愈有用。
    ("L7", "Seek 呼叫點退回 split(\",\")[0]（2）", lambda t: sub_once(
        t,
        "            city, state = self._parse_location(location_raw)\n"
        "            location_obj = Location(\n"
        "                city=city,\n"
        "                state=state,\n",
        "            location_obj = Location(\n"
        '                city=location_raw.split(",")[0].strip(),\n'
        "                state=None,\n", "L7")),
    ("L8", "Seek fallback 捏造州別（2）", lambda t: sub_once(
        t,
        '    return ("" if city.lower() == "australia" else city), None',
        '    return ("" if city.lower() == "australia" else city), "VIC"', "L8")),
]


@dataclass(frozen=True)
class Suite:
    key: str             # 報告用，也是 JOBSPY_MUTATE_SUITE 篩選時比對的值
    title: str
    target_rel: str      # 生產檔（相對 repo 根）
    test_rel: str        # 測試檔（相對 repo 根）
    mutations: tuple
    escapes: frozenset   # 這個 suite 已知且已理解的逃脫（原本是單一全域 EXPECTED_ESCAPES）
    expected_checks: int # 基準線必須印出的項數。改了測試就要同步改這裡，否則大聲中止。


# ⚠️ `expected_checks` 為什麼是【宣告的常數】而不是用 AST 數呼叫點（第十輪實測）：
#   第十輪之前，掃描鎖那條的校準是「印出的項數 == 原始碼裡的 check() 呼叫點數」，
#   而那【只對 test_scan_lock.py 成立】—— 它是一個呼叫點對一項。新接進來的
#   `test_location_filter.py` 是【迴圈驅動】：16 個 check() 呼叫點印出 57 項，
#   於是那條校準一跑就中止（「印出 57 項，但原始碼裡有 16 個呼叫點」）。
#   那不是量尺壞了，是【校準的假設不適用於這種測試檔形狀】。
#
#   改成宣告常數之後兩件事一起變好：
#     1. 兩種形狀都適用（掃描鎖的 142 也是宣告值）。
#     2. 它其實【更強】：AST 那條自己承認「抓不到有人把某個 check() 整個刪掉」
#        （呼叫點與印出項數一起變少），而宣告值會抓到 —— 條數對不上就中止。
#   代價是新增測試時必須一起改這個數字。這個代價是刻意選的：那正是本專案
#   吃過兩次的虧（新增測試後變異表的 FAIL 數過期沒重跑），現在它會變成中止。
#   AST 的呼叫點數仍然算出來並印在基準線那行，當作【下界】的合理性檢查
#   （宣告值 < 呼叫點數 = 這個數字寫錯了）。
SUITES = (
    Suite("scan", "掃描鎖", "job_board.py", "tests/test_scan_lock.py",
          tuple(MUTATIONS), frozenset(EXPECTED_ESCAPES), 142),
    Suite("location-filter", "地點過濾器", "linkedin_job_search.py",
          "tests/test_location_filter.py",
          tuple(LOCATION_FILTER_MUTATIONS), frozenset(), 57),
    Suite("location-seek", "Seek 解析", "jobspy/seek/__init__.py",
          "tests/test_location_filter.py",
          tuple(LOCATION_SEEK_MUTATIONS), frozenset(), 57),
)


# ── 前置檢查：工作區必須乾淨 ────────────────────────────────────────────────
# 判定的正確性同時取決於被測的程式【與測試本身】，所以兩個都要檢查。
# 第五輪 MINOR-4a：原本只檢查 job_board.py，測試檔未提交時產出的表格
# 別人重現不出來 —— 而這個專案的數字已經腐化過三次。
#
# 第十輪：清單改成【從 SUITES 推導】，不是手寫。手寫清單在新增 suite 時會腐化，
# 而腐化的症狀正是最難察覺的那一種：新 suite 的檔案沒提交也照跑，產出一張
# 別人重現不出來的表。
_need_clean = sorted({s.target_rel for s in SUITES} | {s.test_rel for s in SUITES})
_dirty = [l for l in git("status", "--porcelain", *_need_clean).splitlines() if l.strip()]
if _dirty:
    sys.exit("✗ 下列檔案有未提交的改動，先 commit 再跑變異測試：\n   "
             + "\n   ".join(_dirty)
             + "\n  （否則驗證的不是 HEAD 的版本，表格別人重現不出來。）")

# 每個 suite 的生產檔各記一份「跑之前」的 sha256，收工時【逐一】驗證。
# 第十輪之前這裡是單一值（只有 job_board.py 要驗）—— 現在有三個檔要顧。
PROD_BEFORE = {s.target_rel: sha256(REPO / s.target_rel) for s in SUITES}

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

_MISSING = sorted(rel for rel in _need_clean if not (WORK / rel).is_file())
if _MISSING:
    shutil.rmtree(WORK, ignore_errors=True)
    sys.exit(f"✗ 隔離副本不完整 —— git archive 沒有解出：{_MISSING}")
_MISMATCH = sorted(rel for rel, h in PROD_BEFORE.items() if sha256(WORK / rel) != h)
if _MISMATCH:
    shutil.rmtree(WORK, ignore_errors=True)
    sys.exit(f"✗ 隔離副本與工作區不一致（HEAD 與工作區不同？）：{_MISMATCH}")

# 每個 suite 的生產檔原文，開跑前一次讀進記憶體。每一次變異都是從這裡重新產生，
# 所以磁碟上不會殘留任何中間狀態（第五輪的教訓，見 docstring）。
ORIG = {s.target_rel: (WORK / s.target_rel).read_text(encoding="utf-8")
        for s in SUITES}


def _restore_originals():
    """把【所有】suite 的生產檔寫回原文。

    ⚠️ 第十輪新增，因為多 suite 讓一個舊 harness 不可能有的漏洞現形了：
    舊版只有一個 target、一個 test，每個變異都從 `orig` 重新產生，所以
    「上一個變異的殘留」根本不存在。現在兩個 suite 共用
    `tests/test_location_filter.py`，於是【前一個 suite 的最後一個變異會留在磁碟上】
    跑進下一個 suite 的基準線 —— 而基準線是後面每一個判定的「滿分」基準
    （`base_pass`），基準線被污染，整個 suite 的歸因就全錯。

    實測（不是推論）：這個漏洞第一次跑就讓 location-seek 的基準線失敗 3 項
    （`prestons` / `eppings` 被子字串誤收 —— 那正是前一個 suite 的 L1「白名單去
    \\b」的指紋）。校準守衛以「3 項失敗」大聲中止，沒有把它記成逃脫；
    **但如果當天真個剛好是 0 失敗，它會安安靜靜地把別人的變異算在這個 suite 頭上。**

    所以每一次跑測試之前都先回復【全部】的檔案，讓不變式是：
    「樹上等於 HEAD，除了正在被測的那一個變異」。
    """
    for _rel, _text in ORIG.items():
        (WORK / _rel).write_text(_text, encoding="utf-8")

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
#
# 第十輪：測試檔不再只有一個，所以覆蓋必須指名【哪一個 suite】。預設是第一個
# （`scan`），上面那三條驗法因此原樣可用、不必改指令。要對新的 suite 做同樣的
# 探測就加 `JOBSPY_MUTATE_TEST_SUITE=location-filter`（指定要覆蓋【哪一個】
# suite 的測試檔，不是「只跑那個 suite」—— 後者是 `JOBSPY_MUTATE_SUITE`）。
_TEST_OVERRIDE = os.environ.get("JOBSPY_MUTATE_TEST")
_SUITE_KEYS = [s.key for s in SUITES]

# ⚠️ 第十輪審查 MINOR-1：這裡原本把 `JOBSPY_MUTATE_SUITE` 當成【選 suite 的篩選器】，
#    但實作只拿它決定 `JOBSPY_MUTATE_TEST` 要覆蓋哪一個 suite 的測試檔 ——
#    於是 README 寫著「只跑其中一個 suite」，實際跑出來還是掃描鎖那 32 個
#    （32 是第十輪當時的數目，不是現在的 —— 現行見檔頭的表）。
#    那是本專案最常抓的形狀：**宣告 > 實際**，而且是我自己剛寫的文件。
#
#    修法刻意【不是】讓 `JOBSPY_MUTATE_SUITE` 兼做篩選：它的預設值是
#    `SUITES[0].key`（scan），一旦兼做篩選，**不設任何環境變數的平常一輪就會
#    靜靜地只跑 scan suite** —— 用一個新洞補一個舊洞。
#    改成兩個名字講清楚各自的身分：
#      JOBSPY_MUTATE_SUITE      要跑哪些 suite（逗號分隔，預設全部）
#      JOBSPY_MUTATE_TEST_SUITE  `JOBSPY_MUTATE_TEST` 覆蓋【哪一個】suite 的測試檔
_SUITE_KEYS_STR = ",".join(_SUITE_KEYS)
_SUITE_FILTER = {k.strip() for k in
                 os.environ.get("JOBSPY_MUTATE_SUITE", "").split(",") if k.strip()}
_unknown = sorted(_SUITE_FILTER - set(_SUITE_KEYS))
if _unknown:
    shutil.rmtree(WORK, ignore_errors=True)
    sys.exit(f"✗ JOBSPY_MUTATE_SUITE 指名了不存在的 suite：{_unknown}"
             f"（可用：{_SUITE_KEYS_STR}）")

_TEST_OVERRIDE_KEY = os.environ.get("JOBSPY_MUTATE_TEST_SUITE", _SUITE_KEYS[0])
if _TEST_OVERRIDE_KEY not in _SUITE_KEYS:
    shutil.rmtree(WORK, ignore_errors=True)
    sys.exit(f"✗ JOBSPY_MUTATE_TEST_SUITE={_TEST_OVERRIDE_KEY!r} 沒有對應的 suite"
             f"（可用：{_SUITE_KEYS_STR}）")
_ov_suite = next(s for s in SUITES if s.key == _TEST_OVERRIDE_KEY)
if _TEST_OVERRIDE:
    _ov = Path(_TEST_OVERRIDE)
    if not _ov.is_file():
        shutil.rmtree(WORK, ignore_errors=True)
        sys.exit(f"✗ JOBSPY_MUTATE_TEST 指向的檔案不存在：{_ov}")
    (WORK / _ov_suite.test_rel).write_text(_ov.read_text(encoding="utf-8"),
                                           encoding="utf-8")

print(f"隔離副本：{WORK}")
print("生產目錄【不會被寫入】：")
for _rel in sorted(PROD_BEFORE):
    print(f"  {_rel:32s} sha256={PROD_BEFORE[_rel][:16]}…")
print(f"起始狀態：乾淨，{sum(len(v) for v in ORIG.values())} bytes；"
      f"{len(SUITES)} 個 suite、{len(_need_clean)} 個檔案納入前置檢查")
if _TEST_OVERRIDE:
    print(f"⚠️  {_ov_suite.key} 的測試檔已被 JOBSPY_MUTATE_TEST 覆蓋："
          f"{_TEST_OVERRIDE}（只影響隔離副本；這不是正常的一輪）")
print()


def _cleanup_and_die(signum, _frame):
    shutil.rmtree(WORK, ignore_errors=True)
    _changed = sorted(rel for rel, h in PROD_BEFORE.items()
                      if sha256(REPO / rel) != h)
    if _changed:
        print(f"\n✗ 收到訊號 {signum}，且生產檔案已被改動 —— 請立刻檢查：{_changed}")
        sys.exit(2)
    print(f"\n⚠️ 收到訊號 {signum}，已移除隔離副本（生產目錄未被寫入）")
    sys.exit(130)


signal.signal(signal.SIGTERM, _cleanup_and_die)
signal.signal(signal.SIGINT, _cleanup_and_die)



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


def _calibrate(suite):
    """跑一次【沒被變異的】那個測試檔，回傳 (base_pass, ast_checks, bad, out)。

    `bad` 非空代表這把量尺不可信 —— 此時【不可以】繼續跑這個 suite 的變異，
    因為它們的每一個讀數都只是同一個既有故障的回音，而表格看起來一模一樣。
    """
    test_path = WORK / suite.test_rel
    # ── 量尺的第二道校準：印出來的項數必須等於這個 suite 宣告的項數 ──────────
    # 第九輪【審查退回】MINOR-3。上面那個「全綠」判定只證明「沒有 FAIL」，
    # 不證明「每一項都跑了」。反例（審查員實測）：把 L 區那項 AST 不變式整段註解掉，
    # 基準線照樣印 `✅ 全數通過`、rc=0、0 FAIL —— 前置檢查完全放行，
    # 而 M32 會被判成 `❌ 逃脫（無回歸保護）`。**那不是逃脫，是量尺短了一格。**
    # 載具分不出這兩者，於是把「檢查被跳過」記成「修正沒有回歸保護」，歸因錯誤，
    # 文件數字跟著腐化。
    #
    # ⚠️ 判準第十輪從「AST 呼叫點數」改成「suite 宣告的 expected_checks」——
    #    理由與為什麼它其實更強，寫在 Suite 的定義上方（迴圈驅動的測試檔會讓
    #    AST 那條不成立）。AST 數仍然算出來，當作宣告值的【下界】合理性檢查。
    #
    # 判準不用 `grep -c 'check('` 的前提沒有變 —— 同一個理由在本專案出現過很多次：
    # **grep 會把註解與字串裡的 `check(` 算成呼叫**（本檔的說明文字就在引用它）。
    #
    # 這一項抓到的是「呼叫點還在、但沒被執行到」（被移到條件底下、被 return 跳過），
    # 以及「某個 check() 被整個刪掉」—— 兩者都會讓印出項數少於宣告值。
    ast_checks = sum(
        1 for _n in ast.walk(ast.parse(test_path.read_text(encoding="utf-8")))
        if isinstance(_n, ast.Call) and isinstance(_n.func, ast.Name)
        and _n.func.id == "check")

    # 基準線一定要在【乾淨的樹】上跑：前一個 suite 的殘留變異留在磁碟上的話，
    # 這條基準線量的就是別人（見 `_restore_originals` 的實測記錄）。
    _restore_originals()
    out, passed, failed, rc = _run_suite(test_path)
    bad = []
    # ⚠️ 收尾標記有【兩個】，失敗路徑印的是「項失敗：」而不是「✅ 全數通過」。
    # 第一版只認後者，於是「1 項失敗」會被多報一句「沒有印出收尾標記（沒跑到底）」
    # —— 診斷訊息指向錯的原因。判定要與下面每一個變異用的 `done` 一致。
    if "✅ 全數通過" not in out and "項失敗：" not in out:
        bad.append("沒有印出收尾標記（沒跑到底）")
    if failed != 0:
        bad.append(f"{failed} 項失敗")
    # ⚠️ rc 這一條也**只在零失敗時**才列（第九輪退回審查 NIT-1）。測試檔的收尾是
    # `if FAILURES: … sys.exit(1)` / `else: … sys.exit(0)`，所以有 FAIL 就必然 rc≠0
    # —— 那時再列一次 rc 只是同一件事講兩遍，讀起來像兩個獨立的理由。
    # 零失敗而 rc≠0 才是**獨立**訊息（跑到底、0 個 FAIL、卻非零退出：收尾程式碼或
    # atexit 出錯），那正是這裡要抓的東西。實測：探針 C（跑完全過但 sys.exit(3)）
    # → 「90P/0F rc=3：rc=3」，單一理由。
    if failed == 0 and rc != 0:
        bad.append(f"rc={rc}")
    # ⚠️ 只有在【零失敗】時才比對項數。有失敗時 `passed` 本來就會少幾項，
    # 那時再喊「有檢查沒被執行到」是把讀者指向錯的原因（實測：1 項失敗被多報成
    # 4 個理由，其中一句是假的）。診斷訊息的準確性與判定本身一樣重要。
    if failed == 0 and passed != suite.expected_checks:
        bad.append(f"印出 {passed} 項，但 {suite.key} 宣告的是 "
                   f"{suite.expected_checks} 項 —— 有檢查沒被執行到（或測試改了而"
                   "這個數字沒跟著改），這把量尺短了一格")
    # 宣告值不可能小於原始碼裡的 check() 呼叫點數：每個呼叫點至少印一項。
    # 這一條是【寫錯數字】的守衛，不是行為的守衛 —— 所以它與零失敗無關，一律檢查。
    if suite.expected_checks < ast_checks:
        bad.append(f"宣告 {suite.expected_checks} 項 < 原始碼的 {ast_checks} 個 "
                   "check() 呼叫點 —— 這個宣告值本身寫錯了")
    return passed, ast_checks, bad, out


print(f"解譯器：{PY}")

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
# 正確的說法（第九輪【第三次退回】的 MINOR-1 又更正一次 —— 我上一版補的
# 「機制」本身是假的）：**兩個變異的判準逐次相同**。kill_stalled_external() 全函式
# 只在 job_board.py:805 與 :836 兩處讀 _we_hold_scan_lock()（:1036 是另一支函式），
# 兩個變異各拿掉其中一道 `if _we_hold_scan_lock(): return None`，於是各只剩【同一
# 次】讀值當唯一條件，而兩處之間沒有取放鎖的程式碼 ⇒ **失敗集合相同是結構上的**
# （「各連跑 6 次都是 88P/2F、同樣那兩項」是這個結構的觀測結果，不是它的依據）。
# ⚠️ 但它與【真實程式碼】仍不等價（真實程式碼在 G2 放棄、兩者都真的去殺），
# 所以「只有 M9 那一列動 ⇒ 沒波及別處」照樣推不出來。
#
# ⚠️ 舊版寫的「套件只有在 G2 這兩項檢查裡讓『我們持有掃描鎖』為真，其餘任何路徑上
# 兩個閘門都回 False」—— 兩半都錯：C 區真的持有掃描鎖（tests/test_scan_lock.py:320
# / :336 斷言它為 True），六處 mock 則都回 False。真正的分別是【兩道閘門有沒有讀到
# 不同的值】：只有 G2 是（_hold_seq 第一次回 False、之後回 True），其餘各處兩次讀值
# 相同 ⇒ 判斷相同 ⇒ 拿掉哪一道都不改變路徑。
# 教訓：**把過強的宣稱降級時，順手補上的新解釋同樣要驗**，否則只是換一句假話。
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
#    也就是說，平常那一輪「✅ 39/40」**沒有測到任何一列的穩定性**
#    （掃描鎖那個 suite 自己是「✅ 31/32」），它只測到「這一次是這樣」。
#    這個取捨是刻意的（完整一輪 = 3 個 suite 基準線 + 40 個變異 = **43 次測試執行**；
#    ⚠️ 這個 43 是用 `strace -f -e trace=execve` 量子集 `L1,L6`（得到 4 = 2+2）
#    再把規則套出來的，不是估的 —— 第一次量到「8」是 `git` 在 PATH 上失敗的
#    嘗試次數，不是測試執行次數），乘上 REPEAT 就是線性成長，但事實必須寫出來：
#    **要對某一列講「穩定」，就得明跑 REPEAT，沒有別的路徑。**
_ONLY = {s.strip() for s in os.environ.get("JOBSPY_MUTATE_ONLY", "").split(",") if s.strip()}
try:
    _REPEAT = max(1, int(os.environ.get("JOBSPY_MUTATE_REPEAT", "1")))
except ValueError:
    sys.exit("✗ JOBSPY_MUTATE_REPEAT 必須是整數")
# 第十輪：變異 id 全域唯一（M1–M32 / L1–L8），所以 _ONLY 不必指名 suite。
# 但【suite 才是選擇的單位】：一個 suite 若一個變異都沒被選到就整個跳過，
# 連它的基準線都不跑 —— 否則 JOBSPY_MUTATE_ONLY=L1 會連帶校準另外兩個 suite。
_SELECTED = {}
for _s in SUITES:
    # suite 篩選（`JOBSPY_MUTATE_SUITE`）先過，再過變異篩選（`JOBSPY_MUTATE_ONLY`）。
    if _SUITE_FILTER and _s.key not in _SUITE_FILTER:
        continue
    _sel = [m for m in _s.mutations if not _ONLY or m[0] in _ONLY]
    if _sel:
        _SELECTED[_s.key] = _sel
if _ONLY and not _SELECTED:
    sys.exit(f"✗ JOBSPY_MUTATE_ONLY={sorted(_ONLY)} 沒有對應任何變異"
             + (f"（且已被 JOBSPY_MUTATE_SUITE={sorted(_SUITE_FILTER)} 限縮）"
                if _SUITE_FILTER else ""))
if _SUITE_FILTER and not _SELECTED:
    sys.exit(f"✗ JOBSPY_MUTATE_SUITE={sorted(_SUITE_FILTER)} 與 "
             f"JOBSPY_MUTATE_ONLY={sorted(_ONLY)} 的交集是空的")
_SEL_N = sum(len(v) for v in _SELECTED.values())
_ALL_N = sum(len(s.mutations) for s in SUITES)
# ⚠️ 篩選【選了幾個 suite】與【跑不跑全部變異】是兩件事，要分開講：
#    只跑 location-filter 的全部 5 個變異仍然是「完整測了那個 suite」，
#    但它不是「完整一輪」。混淆這兩者正是本專案一直在抓的過度宣稱。
# ⚠️ 判準要用【數出來的數量】，不是環境變數有沒有設 —— 見下面收尾那段的教訓。
#    （第一版寫成 `bool(_SUITE_FILTER)`，那正是「用旗標推導我跑了幾項」，
#      也就是本檔自己警告過的那件事。）
_PARTIAL = _SEL_N != _ALL_N or _REPEAT > 1
if _PARTIAL:
    _why = []
    if _SEL_N != _ALL_N:
        _why.append(f"{_SEL_N}/{_ALL_N} 個變異、"
                    f"{len(_SELECTED)}/{len(SUITES)} 個 suite")
    if _SUITE_FILTER:
        _why.append(f"suite 限縮：{sorted(_SUITE_FILTER)}")
    if _ONLY:
        _why.append(f"變異限縮：{sorted(_ONLY)}")
    if _REPEAT > 1:
        _why.append(f"每個連跑 {_REPEAT} 次")
    print(f"⚠️  子集模式（{'、'.join(_why)}）；這不是完整的一輪\n")


def _run_mutations(suite, selected, results):
    """跑一個 suite 的所有選定變異，結果 append 進 results。

    每一列是 `(suite.key, mid, full, stat, verdict, kind)` —— suite 放在第一欄，
    因為下面的收尾統計必須分辨得出「是哪個 suite 的變異逃脫了」。
    """
    base_pass, ast_checks, cal_bad, cal_out = _calibrate(suite)
    if cal_bad:
        print(f"\n✗ 【{suite.key}】基準線不可信：{'；'.join(cal_bad)}")
        print("  否則下面每一個變異都只是這個既有故障的回音，而表格會長得很正常。")
        print("  尾巴：")
        for _l in cal_out.strip().splitlines()[-15:]:
            print(f"    │ {_l}")
        shutil.rmtree(WORK, ignore_errors=True)
        sys.exit(2)
    # base_pass 不是裝飾品：下面的判定全部拿它當「滿分」的基準（第九輪 MINOR-3
    # 之前它被賦值後從未讀取，那正是「宣告了但沒有做到」的最小樣本）。
    print(f"\n═══ {suite.title}：{suite.target_rel} ← {suite.test_rel} ═══")
    # ⚠️ 這一行【不能】再寫「與原始碼的 N 個 check() 呼叫點相符」—— 那只對
    # test_scan_lock.py 成立（一個呼叫點印一項）。地點那兩個 suite 用的是迴圈驅動的
    # 測試檔，57 項由 16 個呼叫點印出，寫「相符」就是一句當場可證偽的話。
    # 兩個數字都印，並講清楚哪個是判準、哪個是下界。
    print(f"    基準線 {base_pass} 項全過、0 失敗（rc=0）；"
          f"宣告值 {suite.expected_checks} 項、"
          f"原始碼 check() 呼叫點 {ast_checks} 個（下界）")

    target_path = WORK / suite.target_rel
    test_path = WORK / suite.test_rel
    orig = ORIG[suite.target_rel]
    for mid, label, mutate in selected:
        full = f"{mid} {label}"
        # 先把【所有】生產檔寫回原文，再套這一個變異（見 `_restore_originals`）。
        # 少了這一行，前一個 suite 的殘留就會跟著跑進這一個 suite 的變異判定。
        _restore_originals()
        try:
            target_path.write_text(mutate(orig), encoding="utf-8")
        except SystemExit as e:
            results.append((suite.key, mid, full, "ANCHOR-FAIL", str(e), "BAD"))
            print(f"{full:44s} ✗ 錨點失效：{e}")
            continue

        if subprocess.run([PY, "-m", "py_compile", str(target_path)],
                          capture_output=True).returncode != 0:
            results.append((suite.key, mid, full, "SYNTAX-ERR", "變異本身語法錯誤", "BAD"))
            print(f"{full:44s} ✗ 變異本身語法錯誤")
            continue

        try:
            r = subprocess.run([PY, str(test_path)], cwd=WORK,
                               capture_output=True, text=True, timeout=TIMEOUT)
        except subprocess.TimeoutExpired:
            # 逾時【不】算被逮：分不出「變異造成死鎖（=有偵測到）」與
            # 「孤兒握著管線的假卡死（=什麼都沒測到）」。第五輪 MINOR-4c：
            # 原本算成「被逮」會讓結論反轉。現在標成 INCONCLUSIVE 並讓退出碼非 0，
            # 逼人去看。
            results.append((suite.key, mid, full, f">{TIMEOUT}s TIMEOUT", "逾時，原因不明", "INCONCLUSIVE"))
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
                _r2 = subprocess.run([PY, str(test_path)], cwd=WORK,
                                     capture_output=True, text=True, timeout=TIMEOUT)
            except subprocess.TimeoutExpired:
                _readings.append((-1, -1, -1))
                continue
            _readings.append((len(re.findall(r"\[PASS\]", _r2.stdout + _r2.stderr)),
                              len(re.findall(r"\[FAIL\]", _r2.stdout + _r2.stderr)),
                              _r2.returncode))
        if len(set(_readings)) > 1:
            _all = " ".join(f"{p}P/{f}F" for p, f, _ in _readings)
            results.append((suite.key, mid, full, _all,
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
        if done and passed + failed != base_pass:
            results.append((suite.key, mid, full, f"{passed}P/{failed}F rc={rc}",
                            f"總項數 {passed + failed} ≠ 基準線 {base_pass}"
                            "（檢查被跳過？）", "INCONCLUSIVE"))
            print(f"{full:44s} ⚠️  總項數 {passed + failed} ≠ 基準線 {base_pass}"
                  f" —— INCONCLUSIVE，不是逃脫，請查因")
            continue
        if not done:
            # 多印出實際的 P/F 與最後幾行，讓「崩在哪」一眼可見 ——
            # 否則唯一的線索是一個看起來很正常的 `32P/0F`。
            tail_lines = [l for l in out.strip().splitlines()[-4:]]
            results.append((suite.key, mid, full, f"{passed}P/{failed}F rc={rc}",
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
        results.append((suite.key, mid, full, f"{passed}P/{failed}F rc={rc}", verdict, kind))
        print(f"{full:44s} {passed:>2}P / {failed:>2}F  rc={rc}  {verdict}")

results = []
residue = []
try:
    for suite in SUITES:
        if suite.key in _SELECTED:
            _run_mutations(suite, _SELECTED[suite.key], results)
    # ── 殘留檢查（第十輪新增）────────────────────────────────────────────
    # docstring 說「磁碟上不會殘留任何中間狀態」。多 suite 之後那句話【需要被檢查】
    # 才成立：`_restore_originals` 在每個變異之前跑，所以迴圈結束時最後一個變異
    # 還在樹上。把樹寫回原文，然後逐檔比對 sha256 —— 這是有牙齒的版本。
    #
    # ⚠️ 為什麼不是 grep（docstring 45-47 行的歷史教訓）：上一版的殘留檢查是
    # `grep -c 'MUTANT\|if True:'`，而 M8 的變異是一個裸的 `return None`，
    # **grep 不到**，差一點帶著壞掉的版本繼續往下做。雜湊比對沒有這個盲點。
    # ⚠️ 這一段【證明什麼、不證明什麼】要講清楚，因為第一版兩邊都寫錯了：
    #
    #   第一版：先還原、再比對。看似在驗殘留，其實還原之後樹上就是 ORIG 的文字，
    #           而「ORIG == HEAD」在開跑前已經查過了 —— 比對變成一句不可能失敗的
    #           宣告，正是本專案最常抓的那種「宣告 > 實際」。
    #   第二版：先比對、再還原。變成**每一輪都必然亮紅燈** —— 迴圈結束時最後一個
    #           變異本來就還躺在樹上，那是設計如此（`_restore_originals` 就是為此
    #           存在的），不是故障。一個每次都響的警報等於沒有警報。
    #
    # 正確的定位是：這一段驗的是【還原機制本身有沒有漏掉檔案】。
    # 先還原，再拿 PROD_BEFORE 逐檔比對；如果 `_restore_originals()` 漏了某個
    # target（例如新 suite 的 target 沒進 ORIG），那個檔案會保持變異狀態而被抓到。
    # 它【不】證明「跑完當下樹是乾淨的」—— 那從來不是不變式，也不需要是：
    # 真正的中間狀態保護來自 `_restore_originals()` 在每個變異之前執行。
    #
    # 這個守衛【驗過會響】（2026-09-30，不是「應該會響」）：把探針 repo 的
    # `_restore_originals()` 改成跳過 `linkedin_job_search.py`，跑
    # `JOBSPY_MUTATE_ONLY=L1,L4` → 印出
    # `✗ 隔離副本還原後仍有殘留：['linkedin_job_search.py']`、退出碼 2，
    # 而且同一輪的三個生產檔 sha256 全部不變（正確地把「WORK 有殘留」與
    # 「生產檔被寫入」分成兩件事）。
    _restore_originals()
    residue = sorted(rel for rel, h in PROD_BEFORE.items()
                     if sha256(WORK / rel) != h)
finally:
    shutil.rmtree(WORK, ignore_errors=True)

# ── 收工驗證：生產目錄必須毫髮無傷 ──────────────────────────────────────────
print("\n" + "=" * 80)
PROD_AFTER = {rel: sha256(REPO / rel) for rel in PROD_BEFORE}
for _rel in sorted(PROD_BEFORE):
    _same = PROD_AFTER[_rel] == PROD_BEFORE[_rel]
    print(f"生產檔案驗證：{_rel:32s} sha256 "
          f"{'不變 ✓' if _same else '★已改變★'}  {PROD_AFTER[_rel][:16]}…")
_changed = sorted(rel for rel in PROD_BEFORE if PROD_AFTER[rel] != PROD_BEFORE[rel])
if _changed:
    # ⚠️ 這個警告有兩個成因，而且【處置相反】：
    #   (a) 真的外洩 —— 某個變異寫進了生產檔。要救：`git checkout -- <檔案>`。
    #   (b) 有人在這一輪跑的期間【自己編輯了】那個檔。要救：什麼都別做，
    #       你的編輯是對的，這一輪的數字不能用而已。
    # 原本這裡直接印「請立刻 git checkout」—— 那會把 (b) 的情況下使用者剛寫好的
    # 工作【整批刪掉】。一個偵測器不該在只知道「檔案變了」的時候，建議一個
    # 會刪掉未提交工作的動作。所以先讓人自己看一眼。
    #
    # 第十輪：從單檔推廣成【逐一驗證】。清單用 _changed 而不是寫死檔名 ——
    # 上面那句 `git checkout -- job_board.py` 在只有一個檔時是對的，三個檔時
    # 會讓人在修好一個之後以為修完了。
    print(f"✗ 生產檔案在這一輪期間被改動了：{_changed}")
    print("  先看 diff 再決定 —— 【不要】反射性地 git checkout：")
    print(f"    git diff --stat {' '.join(_changed)}")
    print(f"  · 變異外洩 → 只有突變的幾行，救法：git checkout -- {' '.join(_changed)}")
    print("  · 你自己在跑的期間編輯過 → 你的編輯是對的，這一輪的數字作廢，重跑即可")
    sys.exit(2)
if residue:
    # 這一條是【載具自己的】故障，不是被測程式的：樹寫回原文之後仍有差異，
    # 代表還原機制本身漏了一個檔案（新 suite 的 target 忘了進 ORIG？）。
    # 印出來而不是只放進殘留清單 —— 不然它會安靜地讓下一輪的判定失真。
    print(f"✗ 隔離副本還原後仍有殘留：{residue}")
    print("  這是 harness 的問題，不是被測程式的 —— 檢查 Suite.target_rel 與 ORIG。")
    sys.exit(2)
print(f"隔離副本已移除：{not WORK.exists()}")

# 表格依 suite 分組印，每一列都掛著它的 suite key —— 「這一行屬於哪個 suite」
# 是讀這張表時最容易搞錯的一件事（三個 suite 的變異全印進同一個 results）。
_ESCAPES = {s.key: s.escapes for s in SUITES}
print()
for _s in SUITES:
    _rows = [r for r in results if r[0] == _s.key]
    if not _rows:
        continue
    print(f"── {_s.title}（{_s.target_rel} ← {_s.test_rel}）──")
    for _sk, _mid, full, stat, verdict, _kind in _rows:
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
# ⚠️ 第十輪：results 的每一列多了一欄 suite key，所以下面每一個索引都往後移一格
# （kind 從 r[4] 變 r[5]，變異 id 從 r[0] 變 r[1]）。豁免也從單一全域
# EXPECTED_ESCAPES 變成【每個 suite 各自一份】—— 否則 L 系列的 id 要跟 M 系列
# 共用同一張豁免表，而「哪一組的豁免過期了」就分不出來。
bad = [r for r in results if r[5] in ("BAD", "INCONCLUSIVE", "UNSTABLE")]
escaped = [r for r in results if r[5] == "ESCAPED"]
known = [r for r in escaped if r[1] in _ESCAPES[r[0]]]
new = [r for r in escaped if r[1] not in _ESCAPES[r[0]]]
stale = [r for r in results if r[5] == "CAUGHT" and r[1] in _ESCAPES[r[0]]]

if known:
    print(f"\nℹ️  {len(known)} 個【已知且已理解】的逃脫"
          "（見各 suite 的 escapes 與 DECISIONS.md）：")
    for _sk, _mid, full, _s, v, _k in known:
        print(f"   - [{_sk}] {full}  {v}")

if stale:
    # 豁免過期 = 有人補了測試。留著它會讓「已知逃脫」清單變成裝飾品。
    #
    # ⚠️ 第十輪審查 NIT-2：這裡原本【只印警告，退出碼不受影響】。那正是上面
    # 1094-1101 行記著的同一個缺陷形狀 ——「加了守衛，但守衛不會讓任何東西失敗」。
    # 實測（審查員造的）：把 L1 塞進 location-filter 的 escapes，跑 ONLY=L1 →
    # 先印「⚠️ 1 個豁免【已過期】」，下一行照樣「✅ 1/1 個變異被逮捕，無逃脫」，
    # **EXIT=0**。一份不會讓任何東西失敗的守衛，跟沒有一樣；而它的說明文字
    # 還宣稱自己在防止清單腐化 —— 那是「宣告 > 實際」的標準樣本。
    # 現在它與 `new`／`bad` 一起讓退出碼變 1（見下方）。
    print(f"\n⚠️  {len(stale)} 個豁免【已過期】（這些變異現在被逮到了，請從該 suite "
          f"的 escapes 移除）：")
    for _sk, _mid, full, _s, _v, _k in stale:
        print(f"   - [{_sk}] {full}")

if bad:
    print(f"\n⚠️  {len(bad)} 個變異無法判定（不影響結論的正確性，但代表這一輪不完整）：")
    for _sk, _mid, full, stat, v, _k in bad:
        print(f"   - [{_sk}] {full}  {stat}  {v}")

if new:
    print(f"\n⚠️  {len(new)} 個變異沒有被逮捕（＝那項修正沒有回歸保護）：")
    for _sk, _mid, full, _s, v, _k in new:
        print(f"   - [{_sk}] {full}  {v}")

if new or bad or stale:
    sys.exit(1)

# ⚠️ 子集模式（JOBSPY_MUTATE_ONLY）下，這裡的 N 是**子集大小**，不是全部。
# 不加標記的話，這一行讀起來跟完整一輪的結論一模一樣 —— 那正是本專案
# 反覆被燒的形狀（把抽樣讀成性質）。所以子集一律在結論行上自我標示。
#
# ⚠️⚠️ 這裡一開始寫成 `_SELECTED is MUTATIONS` —— **永遠是 False**，因為上面的
# 推導式每次都建一個新 list，身分檢查恆不成立。也就是說「防謊報的那一行」
# 本身在完整一輪時會謊報【子集】。
# 教訓與 M13 那段同源：**別用身分／存在與否去推導「我跑了幾項」，直接數。**
# 第十輪：_SELECTED 變成 dict（key = suite），所以改成比總數 _SEL_N / _ALL_N
# —— 仍然是從資料推導，不依賴任何旗標。
_is_subset = _SEL_N != _ALL_N
_subset = (f"【子集：{_SEL_N}/{_ALL_N} 個變異、"
           f"{len(_SELECTED)}/{len(SUITES)} 個 suite"
           + (f"、每個連跑 {_REPEAT} 次" if _REPEAT > 1 else "") + "】") if _is_subset else ""
print(f"\n✅ {len(results) - len(known)}/{len(results)} 個變異被逮捕"
      + (f"，{len(known)} 個為已知逃脫" if known else "，無逃脫") + _subset)
if _is_subset:
    print(f"   ⚠️ 這不是完整的一輪，不要把這一行當成 {_ALL_N} 個變異的結論。")
sys.exit(0)
