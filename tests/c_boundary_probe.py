#!/usr/bin/env python3
"""量測 (c) 閘門的界線：情境 A–E（外加與 A 同序列的 H）——
**「窗口」不是判準，`read_ok` 才是。**

背景：第十三輪審查的 MINOR-1。我在 `job_board.py` 與 `DECISIONS.md` 寫過
「(c) 拒絕只會發生在 tick 落進 5ms 窗口的那 0.1%」。那句話把兩件不相干的事
綁在一起。這個腳本用**真正的跟讀函式**（`_reset_external_follow()` +
`_follow_live_log()`）餵五個情境，證明：

- A 非窗口，照樣會落到 (c)（`read_ok=False`）；
- B、E 在窗口內，卻**不會**落到 (c)；
- C 在窗口內且會落到 (c)。

第十五輪 reviewer 另外自造了一個對照 **H**（非窗口、偵測時本輪已寫完 N bytes、
之後不再產出），結論是它與 **A 同一序列** —— 所以這裡把它一起收進來跑，用不同的
N 驗「兩者的正規化狀態相同」。它真正澄清的是：F 的機制（`size == offset`，一個
位元組都沒被讀到）**不需要窗口**，把它列在「窗口內」只是它剛好那樣發生。

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
# import job_board 會啟動 watchdog_loop() 與 jobscan 監看執行緒。
# ⚠️【第十五輪 NIT-2】這裡原本寫「若 state 自稱 running，watchdog 會對生產的
# jobscan.service 送出真的 SIGKILL」—— 那個因果**不成立**：探針行程裡
# `_EXTERNAL["last_read_at"]` 初值是 0.0，`_external_idle_seconds()` 遇到假值回
# None，而任何偵測都會把 last_read_at 設成當下，所以一個一秒內結束的腳本不可能
# 湊到 idle > 900（kill 的前置條件）。
# **真正會被破壞的是讀數**：watcher 執行緒每 5 秒會 _external_begin() /
# _external_pump()，把 _EXTERNAL 換成【真的那一輪】的內容，而這個腳本已經把
# JOBSCAN_LIVE 指到自己的暫存檔 —— 兩者互相汙染，量到的就不是表上那些情境了。
# 排程那道檢查是**保守的、預設組態下擋不到東西**（第十六輪 NIT-1 補正）：排程執行緒
# 只在 `JOB_BOARD_INTERNAL_SCHEDULER=1` 時才跑（預設 `"0"`，job_board.py:83；停用時
# `scheduler_loop()` 直接 return，每次執行都會印 `[scheduler] … DISABLED`），而且
# 面板端也拒絕把 `enabled` 重新武裝（job_board.py:2204）。它擋的是「有人手改
# `.job_board_schedule.json` **又**開了內部排程器」那個組合 —— 留著是保守，不是防線。
# 三條拒絕路徑的**負向對照**（＋放行那一格）在 `tests/guard_controls.sh`：它把探針與
# job_board.py 複製進隔離假樹、用假 systemctl 造出 active，一行指令可複查，不碰生產。
_state_p = REPO / "logs/search_state.json"
_sched_p = REPO / ".job_board_schedule.json"
if _state_p.exists():
    try:
        state = json.loads(_state_p.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as e:
        die(f"讀不到 {_state_p}（{e!r}）—— 讀不到就不能證明它是安全的")
    if state.get("phase") == "running":
        die(f"{_state_p} 說 phase=running（pid={state.get('pid')}）—— "
            f"先確認那一輪真的結束了，否則 watcher 執行緒會把讀數汙染掉")
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
_a_norm = (jb._EXTERNAL["read_ok"], jb._EXTERNAL["offset"] == os.path.getsize(_tmp_a),
           list(jb._EXTERNAL["lines"]))

# ── H（第十五輪 reviewer 自造的對照，**非窗口**）：與 A 是【同一序列】—— 偵測時本輪
#    已經寫完 N bytes（水位 = N），之後不再產出。差別只在 N 的大小、以及這裡沒有
#    先截斷過。刻意用不同的 N 再跑一次，把「H ≡ A」從宣稱變成量到的結果：
#    offset 的**數值**不同（200 vs 111），但正規化之後的狀態相同。
f = tempfile.NamedTemporaryFile("w", suffix=".log", delete=False); f.write("H"*111); f.close()
jb.JOBSCAN_LIVE = f.name
jb._reset_external_follow(st)                    # 水位 = 111（這之前沒有任何截斷）
jb._follow_live_log(); jb._follow_live_log()     # 之後不再產出
show("H 非窗口、偵測時已寫完 111 bytes  ", f)
_tmp_h = f.name
_h_norm = (jb._EXTERNAL["read_ok"], jb._EXTERNAL["offset"] == os.path.getsize(_tmp_h),
           list(jb._EXTERNAL["lines"]))
print(f"    └ 正規化狀態 (read_ok, offset==size, lines)：A={_a_norm} H={_h_norm}"
      f" → 相同？ {_a_norm == _h_norm}（offset 數值 200 vs 111 是唯一差別）")

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

for _p in (_tmp_a, _tmp_h, _tmp_b, _tmp_c, _tmp_d, f.name):
    os.unlink(_p)

print("""
※ 讀法（前提：idle 已超過門檻、runtime 可判定 —— 這兩道在 _stall_gate_refusal()
  裡都排在 (c) 之前；少了這個前提，「落到 (c)」就不成立）：
    read_ok=False ⟺ 偵測那一刻之後才寫進檔案的位元組一個都沒被讀到 ⟺ 閘門回 (c)。
    ⚠️ 只能用時間講，不能用位址講：「水位之後」有第二種讀法，而 B 就是它的反例
    （B 的 read_ok=True 靠 resync 從 0 重讀，讀到位址【低於】水位的位元組，
      那些位元組是本輪寫的）。第十五輪 NIT-3。
  A（非窗口）=False、H（非窗口，與 A 同序列）=False、B（窗口內）=True、
  C（窗口內）=False、D=True、E=True
  ⇒ 窗口【既非充分也非必要】。「會落 (c)」不是窗口的性質，是 read_ok 的性質。
  三個 read_ok=False 的**成因不同**（別把它們講成同一種）：
    A、H：偵測之後檔案沒再變動 → 比對時 size == offset → 無事可讀。
    C   ：偵測之後檔案被截斷且沒有新內容 → size < offset 觸發 resync → 重讀一個
          空檔案（所以 C 的 size == offset 是【重讀之後】才相等的，不是成因）。
    F   ：偵測之後檔案被截斷、又寫回**同一個大小** → size == offset → 無事可讀。
""")
