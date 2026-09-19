#!/usr/bin/env python3
"""量測 J 區子行程的 SIGABRT 率 —— **組態必須明寫，因為沒寫組態的數字不是證據。**

背景：`tests/test_scan_lock.py` 的 J 區用 `python -c "<跳線 + import job_board>"`
當子行程。那個寫法在 interpreter finalization 時會踩到：

    Fatal Python error: could not acquire lock for
    <_io.BufferedWriter name='<stdout>'> at interpreter shutdown,
    possibly due to daemon threads

→ SIGABRT（rc = -6），於是「子行程非零退出」的斷言隨機變紅。

**這個腳本存在的理由**：我對這件事的數字發佈過兩次，兩次都被證明不可重現
（第一次審查員重測不出來，第二次我自己重測不出來）。所以量測本身要進版控，
讓「A 是 0/60」這類宣稱可以被任何人一行指令推翻。

三個組態：
    A = 現在的 _J_TRIPWIRE 拿掉 `os._exit(0)`（**保留** `sys.stdout.flush()`）；管線
    B = `d80fb93` 的原始版（沒有 flush、沒有 `os._exit(0)`）；管線
    C = 同 B，但子行程的 stdout 導到**檔案**而非管線

用法：
    .venv/bin/python tests/sigabrt_probe.py            # 每個組態 60 次
    .venv/bin/python tests/sigabrt_probe.py 200        # 想更窄的信賴區間就加大

⚠️ **這個腳本會用子行程 import `job_board`**，也就是會啟動真正的 watchdog /
jobscan 監看執行緒 —— 與 `test_scan_lock.py` 的 J 區同一個風險。它**沒有**像
測試那樣先裝跳線，所以下面的前置檢查是它唯一的護欄，而且是強制的：
掃描狀態必須是 `finished`、排程必須是 `enabled: false`。不成立就直接拒絕執行。
"""
import ast
import json
import os
import subprocess
import sys
import tempfile
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
PY = str(REPO / ".venv/bin/python")
N = int(sys.argv[1]) if len(sys.argv) > 1 else 60


def die(msg):
    print(f"✗ 拒絕執行：{msg}", file=sys.stderr)
    raise SystemExit(2)


# ── 前置檢查（這是護欄，不是提醒）─────────────────────────────────────────────
# 子行程會啟動 watchdog_loop()。若 state 自稱 running 且 pid 看起來像我們的掃描，
# watchdog 會對【生產的】jobscan.service 送出真的 SIGKILL。排程也一樣：閘門若被
# 打開且種子逾期，子行程會在生產目錄跑起真的爬蟲。
_state_p = REPO / "logs/search_state.json"
_sched_p = REPO / ".job_board_schedule.json"
if _state_p.exists():
    try:
        state = json.loads(_state_p.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as e:
        die(f"讀不到 {_state_p}（{e!r}）—— 讀不到就不能證明它是安全的")
    if state.get("phase") == "running":
        die(f"{_state_p} 說 phase=running（pid={state.get('pid')}）—— "
            f"先確認那一輪真的結束了，否則 watchdog 會殺掉它")
if _sched_p.exists():
    try:
        sched = json.loads(_sched_p.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as e:
        die(f"讀不到 {_sched_p}（{e!r}）")
    if sched.get("enabled"):
        die(f"{_sched_p} 的 enabled 是 true —— 子行程的排程器可能真的去掃描")
if subprocess.run(["systemctl", "--user", "is-active", "jobscan.service"],
                  capture_output=True, text=True).stdout.strip() == "active":
    die("jobscan.service 正在執行 —— 不要在真實掃描進行中跑這個")


def extract(text: str, var: str = "_J_TRIPWIRE") -> str:
    """從測試檔取出跳線字串（AST，不是 grep —— 它是隱式串接的多行字面值）。"""
    for node in ast.parse(text).body:
        if isinstance(node, ast.Assign) and any(
                getattr(t, "id", None) == var for t in node.targets):
            return ast.literal_eval(node.value)
    raise SystemExit(f"找不到 {var}")


now = extract((REPO / "tests/test_scan_lock.py").read_text(encoding="utf-8"))
old = extract(subprocess.run(["git", "show", "d80fb93:tests/test_scan_lock.py"],
                             cwd=REPO, capture_output=True, text=True,
                             check=True).stdout)

if not now.endswith("os._exit(0)\n"):
    die("現在的 _J_TRIPWIRE 不是以 os._exit(0) 結尾 —— 這個腳本的前提變了，"
        "A 的定義（拿掉它）已經不成立，請先讀過 test_scan_lock.py 再改這裡")
A = now[: -len("os._exit(0)\n")]
if "sys.stdout.flush()" not in A:
    die("A 的定義是「保留 flush、只拿掉 os._exit(0)」，但 flush 不見了")

CONFIGS = [
    ("A  現在的程式碼 − os._exit(0)（管線）", A, False),
    ("B  d80fb93 原始版（管線）", old, False),
    ("C  同 B，stdout 導到檔案", old, True),
]

print(f"每個組態 {N} 次，子行程 cwd={REPO}\n")
for name, src, to_file in CONFIGS:
    abrt, codes = 0, {}
    with tempfile.TemporaryDirectory() as td:
        logf = os.path.join(td, "child.log")
        for _ in range(N):
            if to_file:
                with open(logf, "wb") as fh:
                    r = subprocess.run([PY, "-c", src], cwd=str(REPO),
                                       stdout=fh, stderr=subprocess.STDOUT)
            else:
                r = subprocess.run([PY, "-c", src], cwd=str(REPO), capture_output=True)
            codes[r.returncode] = codes.get(r.returncode, 0) + 1
            abrt += r.returncode == -6
    print(f"{name:38s}  SIGABRT {abrt:3d}/{N}   全部 rc={dict(sorted(codes.items()))}")

print("\n※ rc=-6 就是 SIGABRT（'could not acquire lock ... at interpreter shutdown'）")
print("※ 兩次獨立的量測可以差距很大（B 實測過 15/60 與 7/60）——")
print("  小樣本只能證明「A 是 0」，不能拿來排名 B 與 C。要下那種結論請加大 N 並重跑多次。")
