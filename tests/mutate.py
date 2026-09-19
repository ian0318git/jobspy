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
第五輪審查指出這在**生產主機**上是危險的：13 個變異每個會在磁碟上存在 5–25 秒
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
EXPECTED_ESCAPES = {"M13"}


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
]

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

        if subprocess.run([sys.executable, "-m", "py_compile", str(TARGET)],
                          capture_output=True).returncode != 0:
            results.append((mid, full, "SYNTAX-ERR", "變異本身語法錯誤", "BAD"))
            print(f"{full:44s} ✗ 變異本身語法錯誤")
            continue

        try:
            r = subprocess.run([sys.executable, str(TEST)], cwd=WORK,
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
    print("✗ 生產的 job_board.py 被改動了！請立刻 git checkout -- job_board.py")
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
