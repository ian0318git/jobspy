#!/usr/bin/env python3
"""量測 (c) 閘門的界線：情境 F、G —— 兩個**反例**，直接問真正的閘門。

第十四輪審查的 MINOR-1：我第十三輪寫的更正句「落到 (c) 的條件是【本輪在被讀到
之後就不再產出】」是**時序**描述，與真判準不等價。這個腳本用真正的
`_stall_gate_refusal()` 問「這個狀態會不會拒絕」，造出兩個反例：

- **G**：本輪**先被讀到**、之後才停止產出 → 那句話字面上完全符合，但
  `read_ok` 已經是 True → **不落 (c)**。所以那句話是錯的。
- **F**：窗口內、本輪產出**恰好等於**偵測水位（`size == off`）→ 一個位元組都
  沒被讀 → `read_ok=False` → **落 (c)**。所以「窗口內會落 (c) 的只有 C（零輸出）」
  是錯的，F 不是 C。

**這個腳本存在的理由**：與 `tests/c_boundary_probe.py` 相同 —— `DECISIONS.md`
〈續四〉那張表引用的是 `/tmp` 底下的腳本，`/tmp` 會消失，證據就變成宣稱。

用法：
    .venv/bin/python tests/c_boundary_probe_fg.py

⚠️ **這個腳本會 import `job_board`**，會啟動真正的 watchdog / jobscan 監看執行緒。
前置檢查是護欄，不是提醒（與 `sigabrt_probe.py` 同一套）。
"""
import importlib.util
import json
import os
import subprocess
import sys
import tempfile
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent


def die(msg):
    print(f"✗ 拒絕執行：{msg}", file=sys.stderr)
    raise SystemExit(2)


# ── 前置檢查（護欄）—— 理由同 c_boundary_probe.py ───────────────────────────
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
        die(f"{_sched_p} 的 enabled 是 true —— 排程器可能真的去掃描")
if subprocess.run(["systemctl", "--user", "is-active", "jobscan.service"],
                  capture_output=True, text=True).stdout.strip() == "active":
    die("jobscan.service 正在執行 —— 不要在真實掃描進行中跑這個")

spec = importlib.util.spec_from_file_location("jb", REPO / "job_board.py")
jb = importlib.util.module_from_spec(spec)
spec.loader.exec_module(jb)
st = {"run_id": "r1", "trigger": "t", "started_at": None}
HOW = jb.SEARCH_STALL_TIMEOUT + 100     # idle：宣稱沉默 1000s
WHY = jb.SEARCH_STALL_TIMEOUT + 300     # runtime：目標真的存在 1200s


def verdict(tag):
    """用真正的閘門問：這個狀態會不會落到 (c) 拒絕？"""
    r = jb._stall_gate_refusal(WHY, HOW, "x", ever_read=jb._EXTERNAL["read_ok"])
    falls = r is not None and "從未從 live log 讀到任何輸出" in r
    print(f"{tag}\n    read_ok={jb._EXTERNAL['read_ok']!r} offset={jb._EXTERNAL['offset']!r}"
          f"  → 落到 (c) 拒絕？ {'會' if falls else '不會'}")
    return falls


def mk(content):
    f = tempfile.NamedTemporaryFile("w", suffix=".log", delete=False)
    f.write(content); f.close(); jb.JOBSCAN_LIVE = f.name; return f.name


# ── F：窗口內，本輪在【偵測之後】有產出，但產出【恰好等於】舊水位
p = mk("F"*67)
jb._reset_external_follow(st); jb._follow_live_log()   # 水位 = 67（窗口：什麼都沒讀）
open(p, "w").close()                                    # 截斷
with open(p, "w") as fh: fh.write("G"*67)               # 本輪寫【恰好 67】→ size == off
jb._follow_live_log()
f_falls = verdict("F 窗口內、本輪產出恰好等於偵測水位（67）：")
os.unlink(p)

# ── G：本輪先被讀到，之後才停止產出
p = mk("H"*40)
jb._reset_external_follow(st)                           # 水位 = 40（無截斷）
with open(p, "a") as fh: fh.write("new line\n")         # 本輪在偵測後產出
jb._follow_live_log()                                   # → 被讀到
jb._follow_live_log()                                   # 之後不再產出
g_falls = verdict("G 先被讀到、之後才停止產出：")
os.unlink(p)

print(f"\n我的句子說「本輪【在被讀到之後】就不再產出 → 落到 (c)」→ 對 G 而言是"
      f"{'對' if g_falls else '錯（G 不落 (c)）'}")
print(f"我的句子說 A「在第一次被讀取【之前】就停止產出 → 落到 (c)」→ B 也符合這句"
      f"，但 B 不落 (c)：兩句的時序描述都【不等價於】真正的判準 read_ok")
print(f"F 不屬於情境 C，卻同樣落在窗口內且會落 (c) → 「C 是唯一屬於窗口的子集」是錯的")
