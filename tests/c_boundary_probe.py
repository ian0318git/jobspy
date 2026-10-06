#!/usr/bin/env python3
"""量測 (c) 閘門的界線：情境 A–E —— **「窗口」不是判準，`read_ok` 才是。**

背景：第十三輪審查的 MINOR-1。我在 `job_board.py` 與 `DECISIONS.md` 寫過
「(c) 拒絕只會發生在 tick 落進 5ms 窗口的那 0.1%」。那句話把兩件不相干的事
綁在一起。這個腳本用**真正的跟讀函式**（`_reset_external_follow()` +
`_follow_live_log()`）餵五個情境，證明：

- A 非窗口，照樣會落到 (c)（`read_ok=False`）；
- B、E 在窗口內，卻**不會**落到 (c)；
- C 在窗口內且會落到 (c)。

**這個腳本存在的理由**：`DECISIONS.md`〈續四〉那張 A–G 表引用的是 `/tmp` 底下的
腳本 —— 而 `/tmp` 會隨重開機消失，那份證據就變成不可複查的宣稱。與
`tests/sigabrt_probe.py` 同一個原則：**量測本身要進版控**，讓「A 的 read_ok 是
False」這種話可以被任何人一行指令推翻。

用法：
    .venv/bin/python tests/c_boundary_probe.py

⚠️ **讀數要怎麼讀：** 這個腳本印的是**原始的 `read_ok` / `offset` / `pending`**，
不是「會不會落到 (c)」的判決。「會落 (c)」那一欄是由 `read_ok=False` 推出來的
（閘門的 (c) 條件就是 `ever_read` 為假），F、G 兩情境則是直接呼叫
`_stall_gate_refusal()` 問出來的 —— 見 `tests/c_boundary_probe_fg.py`。

⚠️ **這個腳本會 import `job_board`**，也就是會啟動真正的 watchdog /
jobscan 監看執行緒（`job_board.py` 的模組層就有三條 `threading.Thread`）。
它自己會把 `JOBSCAN_LIVE` 指向暫存檔，但 watchdog 執行緒讀的是同一個模組全域，
所以**不能在真的掃描進行中跑**。下面的前置檢查是護欄，不是提醒；不成立就拒絕
執行（與 `sigabrt_probe.py` 同一套）。
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


# ── 前置檢查（護欄）─────────────────────────────────────────────────────────
# import job_board 會啟動 watchdog_loop()。若 state 自稱 running，watchdog 會
# 對【生產的】jobscan.service 送出真的 SIGKILL（那是這整條修正線要防的事，
# 不該由量測腳本自己製造出來）。排程也一樣：閘門若被打開且種子逾期，會跑真的爬蟲。
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

_jb = REPO / "job_board.py"
spec = importlib.util.spec_from_file_location("jb", _jb)
jb = importlib.util.module_from_spec(spec)
spec.loader.exec_module(jb)

st = {"run_id": "r1", "trigger": "t", "started_at": None}


def show(tag, f):
    """印出跟讀狀態（size 是為了讓人看出「檔案裡有幾 bytes」與 offset 的關係）。"""
    print(f"{tag}: read_ok={jb._EXTERNAL['read_ok']!r} offset={jb._EXTERNAL['offset']!r} "
          f"size={os.path.getsize(f.name)} lines={len(jb._EXTERNAL['lines'])}")


# ── A（非窗口）：本輪在【第一次被讀到之前】就寫完 200 bytes 並從此不再產出
#    水位 = 200（第一次讀取時已經寫完）→ 之後一個 byte 都沒多 → read_ok=False
#    → 會落到 (c) 拒絕。與窗口無關。
f = tempfile.NamedTemporaryFile("w", suffix=".log", delete=False); f.write("prev"*10); f.close()
jb.JOBSCAN_LIVE = f.name
open(f.name, "w").close()                        # run_scan.sh 的 : > "$LIVE"
with open(f.name, "w") as fh: fh.write("A"*200)  # 本輪寫完 200 bytes 後卡死（tick 還沒來）
jb._reset_external_follow(st)                    # tick 偵測到本輪 → 水位 = 200
jb._follow_live_log(); jb._follow_live_log()     # 之後的 tick，檔案不再長大
show("A 非窗口、第一次讀取前就停止產出    ", f)
_tmp_a = f.name

# ── B（窗口內）：舊檔 67 bytes，tick 落在窗口，本輪之後寫 30 bytes（<67）後卡死
#    截斷造成 size(30) < offset(67) → resync 歸零重讀 → read_ok=**True**
#    → **不會**落到 (c)。所以「窗口內就會落 (c)」不成立。
f = tempfile.NamedTemporaryFile("w", suffix=".log", delete=False); f.write("B"*67); f.close()
jb.JOBSCAN_LIVE = f.name
jb._reset_external_follow(st); jb._follow_live_log()   # 窗口：size==off，什麼都沒讀
open(f.name, "w").close()                              # 截斷
with open(f.name, "w") as fh: fh.write("C"*30)         # 本輪寫 30 bytes（<67）後卡死
jb._follow_live_log()
show("B 窗口內、寫了 30 bytes（<水位）   ", f)
_tmp_b = f.name

# ── C（窗口內）：本輪完全沒有輸出 → read_ok=False → 會落到 (c)
f = tempfile.NamedTemporaryFile("w", suffix=".log", delete=False); f.write("D"*67); f.close()
jb.JOBSCAN_LIVE = f.name
jb._reset_external_follow(st); jb._follow_live_log()
open(f.name, "w").close()
jb._follow_live_log()
show("C 窗口內、本輪零輸出              ", f)
_tmp_c = f.name

# ── D（我補的）：偵測之後【沒有截斷】、本輪繼續產出（＝看板重啟時跟到已在跑的掃描）
#    這才是「少掉開頭」的主要情境：resync 不會發生，offset 停在舊水位。
f = tempfile.NamedTemporaryFile("w", suffix=".log", delete=False); f.write("E"*500); f.close()
jb.JOBSCAN_LIVE = f.name                              # 本輪已經跑了，檔裡有 500 bytes
jb._reset_external_follow(st)                         # 水位 = 500（沒有截斷會發生）
with open(f.name, "a") as fh: fh.write("NEW\n")       # 本輪繼續產出
jb._follow_live_log()
print(f"D 無截斷、偵測後繼續產出：read_ok={jb._EXTERNAL['read_ok']!r} "
      f"offset={jb._EXTERNAL['offset']!r} 預覽={jb._EXTERNAL['lines']!r}"
      f"  ← 開頭 500 bytes（E×500）不在預覽裡")
_tmp_d = f.name

# ── E（我補的）：窗口內，本輪在一個 tick 內寫出【比舊水位更多】的位元組
#    size(100) > offset(67) → 不觸發 resync → 只讀到 [67,100) → 本輪開頭被跳過。
#    這是【另一種】預覽截斷：所以「窗口沒有這個損失」也是錯的。read_ok 仍正確為 True。
f = tempfile.NamedTemporaryFile("w", suffix=".log", delete=False); f.write("F"*67); f.close()
jb.JOBSCAN_LIVE = f.name
jb._reset_external_follow(st); jb._follow_live_log()   # 水位 = 67
open(f.name, "w").close()                              # 截斷
with open(f.name, "w") as fh: fh.write("G"*100)        # 本輪寫 100 bytes（>67）→ 不觸發 resync
jb._follow_live_log()
print(f"E 窗口內、本輪跑得比水位快：read_ok={jb._EXTERNAL['read_ok']!r} "
      f"offset={jb._EXTERNAL['offset']!r} 讀進 pending={len(jb._EXTERNAL['pending'])} bytes"
      f" ← 本輪開頭 67 bytes 被跳過（read_ok 仍正確為 True）")

for _p in (_tmp_a, _tmp_b, _tmp_c, _tmp_d, f.name):
    os.unlink(_p)

print("""
※ 讀法（前提：idle 已超過門檻、runtime 可判定 —— 這兩道在 _stall_gate_refusal()
  裡都排在 (c) 之前；少了這個前提，「落到 (c)」就不成立）：
    read_ok=False ⟺ 偵測水位之後一個本輪的位元組都沒被讀到 ⟺ 閘門回 (c)。
  A（非窗口）=False、B（窗口內）=True、C（窗口內）=False、D=True、E=True
  ⇒ 窗口【既非充分也非必要】。「會落 (c)」不是窗口的性質，是 read_ok 的性質。
""")
