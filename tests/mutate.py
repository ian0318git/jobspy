#!/usr/bin/env python3
"""變異測試：把每一項修正【改回去】，確認 tests/test_scan_lock.py 會 FAIL。

    .venv/bin/python tests/mutate.py

退出碼 0 = 全部變異都被逮捕；1 = 有變異逃脫（＝那項修正沒有回歸保護）。

**為什麼一定要做這件事**：這個專案已經吃過兩次虧 ——
  * 第三輪：把 `kill_stalled_external()` 改成開頭 `return None`（整個功能死掉），
    39 項檢查**全數通過**。F 區每一條斷言都是「不得開火」的形式，分不出
    「正確地拒絕」與「永遠不開火」。
  * 第四輪：`errors="replace"` 與 TOCTOU 重檢拿掉之後也 39/39 全過。
「有修正、沒有回歸保護」是這個專案最常見的缺陷形狀，所以修正必須配一個
**會失敗的變異**。

⚠️ 這個腳本會【改寫 job_board.py】，請在乾淨的工作區執行（它會自己檢查）。

歷史教訓（2026-09-19，實際發生過）：
  上一版中止時把 M8（`kill_stalled_external` 整個 no-op）留在檔案裡，而殘留檢查是
  `grep -c 'MUTANT\\|if True:'` —— M8 的變異是一個裸的 `return None`，**grep 不到**。
  差一點帶著「停滯偵測完全失效」的版本繼續往下做。所以現在：
    1. 還原一律 `git checkout -- job_board.py`（commit 是唯一可信的已知良好狀態），
       不依賴 finally 有沒有跑到；
    2. 攔 SIGTERM/SIGINT 就地還原；
    3. 啟動前先確認工作區乾淨 —— 否則「還原」會還原到錯的東西；
    4. 結束時用 `git diff --stat` 驗證還原，不是比對樣式。
另外：**假變異**（改到註解、不可能改變行為）永遠不會 FAIL，看起來像「逃脫」。
看到逃脫先懷疑變異本身。
"""
import re
import signal
import subprocess
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
TARGET = REPO / "job_board.py"
TEST = "tests/test_scan_lock.py"

# 每個變異的逾時。正常一輪約 6 秒；被逮捕的變異通常更快，但留足餘裕。
TIMEOUT = 400


def git(*args, check=True):
    return subprocess.run(["git", *args], cwd=REPO, capture_output=True,
                          text=True, check=check).stdout


def restore():
    """還原到 commit 的狀態 —— 唯一的已知良好版本。"""
    subprocess.run(["git", "checkout", "--", "job_board.py"], cwd=REPO,
                   capture_output=True)


dirty = git("status", "--porcelain", "job_board.py").strip()
if dirty:
    sys.exit(f"✗ job_board.py 有未提交的改動（{dirty!r}）—— 先 commit 再跑變異測試，\n"
             f"  否則 git checkout 還原的不是你以為的版本。")
ORIG = TARGET.read_text(encoding="utf-8")
print(f"起始狀態：乾淨，{len(ORIG)} bytes\n")


def _restore_and_die(signum, _frame):
    restore()
    print(f"\n⚠️ 收到訊號 {signum}，已用 git 還原 job_board.py")
    sys.exit(130)


signal.signal(signal.SIGTERM, _restore_and_die)
signal.signal(signal.SIGINT, _restore_and_die)


def sub_once(text, old, new, label):
    n = text.count(old)
    if n != 1:
        raise SystemExit(f"錨點出現 {n} 次，預期 1 次 —— {label} 未套用")
    return text.replace(old, new)


MUTATIONS = [
    ("M1  回收判準改回 proc is not None", lambda t: sub_once(
        t, "        if proc is not None and proc.poll() is None:\n",
        "        if proc is not None:\n", "M1")),
    ("M2  拿掉 try/finally", lambda t: sub_once(
        t, "    finally:\n        # 子程序結束＝這一輪掃描結束，鎖必須立刻放掉，否則 timer 的下一輪會被擋住。\n",
        "    except Exception:\n        raise\n    else:\n", "M2")),
    ("M3  拿掉 errors=replace", lambda t: sub_once(
        t, '                errors="replace",\n', "", "M3")),
    ("M4  拿掉 phase 閘門", lambda t: sub_once(
        t, '    if state.get("phase") != "running":\n        return None\n\n    run_id, trigger, pid',
        "    run_id, trigger, pid", "M4")),
    ("M5  拿掉 cmdline 身分檢查", lambda t: sub_once(
        t, "        if _pid_is_our_scan(pid):\n", "        if True:\n", "M5")),
    ("M6  拿掉 ActiveState 前置檢查", lambda t: sub_once(
        t, '        if act in ("active", "activating", "reloading"):\n',
        "        if True:\n", "M6")),
    # M7 原本錨在 docstring 的一句註解上 —— 那是【假變異】：改掉註解不會改變任何
    # 行為，所以它永遠不會 FAIL，也就永遠「逃脫」。要真的拿掉閘門才行。
    ("M7  拿掉 _external_begin 身分閘門", lambda t: sub_once(
        t, '    if state.get("phase") != "running":\n        # 來源不明的鎖持有者',
        '    if False:\n        # 來源不明的鎖持有者', "M7")),
    ("M8  kill_stalled_external 整個 no-op", lambda t: sub_once(
        t, "    if _we_hold_scan_lock():\n        return None\n    idle = _external_idle_seconds()\n",
        "    return None\n    if _we_hold_scan_lock():\n        return None\n    idle = _external_idle_seconds()\n",
        "M8")),
    ("M9  拿掉 TOCTOU 重檢", lambda t: sub_once(
        t, "    if _we_hold_scan_lock():\n        return None\n\n    target = None\n",
        "    target = None\n", "M9")),
    ("M10 _pid_is_our_scan 退回子字串比對", lambda t: sub_once(
        t,
        '    exe = os.path.basename(argv[0])\n    if exe == "run_scan.sh":\n        return True\n',
        '    return any(m in "\\0".join(argv) for m in ("run_scan.sh", "linkedin_job_search.py"))\n'
        '    exe = os.path.basename(argv[0])\n    if exe == "run_scan.sh":\n        return True\n',
        "M10")),

    # ── 日誌整行一次寫出（H 區）────────────────────────────────────────────
    # 兩者都測，因為它們要回答的是不同的問題：
    #   M11 是【修正前的真實行為】（print 兩次 write、無鎖）→ 必須被逮。
    #   M12 只拿掉「整行一次 write」但保留鎖。若 M12 逃脫，代表真正有效的是鎖、
    #       單次 write 只是加強；那 job_board.py 的註解就寫錯了，必須更正。
    #       誠實面對這個結果比宣稱「兩個都必要」重要。
    ("M11 _out 退回 print()（修正前行為，無鎖）", lambda t: sub_once(
        t,
        '    with _LOG_LOCK:\n        sys.stdout.write(f"{line}\\n")\n        sys.stdout.flush()\n',
        '    print(line, flush=True)\n', "M11")),
    ("M12 保留鎖但用 print()（兩次 write）", lambda t: sub_once(
        t,
        '    with _LOG_LOCK:\n        sys.stdout.write(f"{line}\\n")\n        sys.stdout.flush()\n',
        '    with _LOG_LOCK:\n        print(line, flush=True)\n', "M12")),
    # M13 問的是：鎖本身是不是必要的？（單次 write 留著，只拿掉鎖）
    # 如果 M13 逃脫，代表**單次 write 才是關鍵**，鎖只是加強 ——
    # 那麼 job_board.py 的註解就寫錯了，必須改成誠實的版本，不能宣稱「兩個都必要」。
    ("M13 拿掉鎖（保留整行一次 write）", lambda t: sub_once(
        t,
        '    with _LOG_LOCK:\n        sys.stdout.write(f"{line}\\n")\n        sys.stdout.flush()\n',
        '    sys.stdout.write(f"{line}\\n")\n    sys.stdout.flush()\n', "M13")),
]

results = []
try:
    for label, mutate in MUTATIONS:
        restore()
        try:
            TARGET.write_text(mutate(ORIG), encoding="utf-8")
        except SystemExit as e:
            results.append((label, "ANCHOR-FAIL", str(e)))
            print(f"{label:44s} ✗ 錨點失效：{e}")
            continue

        if subprocess.run([str(REPO / ".venv/bin/python"), "-m", "py_compile",
                           str(TARGET)], capture_output=True).returncode != 0:
            results.append((label, "SYNTAX-ERR", "變異本身語法錯誤，不計"))
            print(f"{label:44s} ✗ 變異本身語法錯誤，不計")
            continue

        try:
            r = subprocess.run([str(REPO / ".venv/bin/python"), TEST],
                               cwd=REPO, capture_output=True, text=True,
                               timeout=TIMEOUT)
        except subprocess.TimeoutExpired:
            # 逾時【也要】算被逮 —— 但先確認不是「孤兒握著管線」那種假卡死。
            results.append((label, f">{TIMEOUT}s TIMEOUT", "⏱ 逾時（視為被逮，但請查因）"))
            print(f"{label:44s} ⏱  逾時 {TIMEOUT}s —— 請確認不是假卡死")
            continue
        out = r.stdout + r.stderr
        passed = len(re.findall(r"\[PASS\]", out))
        failed = len(re.findall(r"\[FAIL\]", out))
        verdict = "✅ 被逮" if failed > 0 else "❌ 逃脫（無回歸保護）"
        results.append((label, f"{passed}P/{failed}F rc={r.returncode}", verdict))
        print(f"{label:44s} {passed:>2}P / {failed:>2}F  rc={r.returncode}  {verdict}")
finally:
    restore()

print("\n" + "=" * 80)
diff = git("diff", "--stat", "job_board.py").strip()
print(f"還原驗證：git diff --stat job_board.py = {diff!r}")
if diff:
    print("✗ 還原失敗！請立刻 git checkout -- job_board.py")
    sys.exit(2)
print("✓ 還原乾淨")

escaped = [r for r in results if "逃脫" in str(r[2]) or r[1] == "ANCHOR-FAIL"]
for label, stat, verdict in results:
    print(f"  {label:44s} {stat:>18s}  {verdict}")
print("=" * 80)
if escaped:
    print(f"\n⚠️  {len(escaped)} 個變異沒有被逮捕：")
    for label, _, v in escaped:
        print(f"   - {label}  {v}")
    sys.exit(1)
print(f"\n✅ 全部 {len(MUTATIONS)} 個變異都被逮捕")
sys.exit(0)
